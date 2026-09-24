"""Session runner, object resolution and the lock registry.

``with_client`` is pi-abap-fs's ``withClient``: a fresh session per tool
call, logged in, used, and dropped afterwards.

One thing is deliberately different. ADT locks live in the *stateful
session* that took them; dropping that session releases the lock. pi-abap-fs
drops the session at the end of ``abap_lock``, so the handle it returns can
no longer protect anything by the time ``abap_write`` runs. Here
``abap_lock`` keeps its session open in :data:`LOCKS`, and every later call
that touches the same object -- write, text elements, activate, unlock --
runs inside that session. ``abap_unlock`` (or the idle timeout) closes it.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional

from . import adt
from .adt_http import AdtSession
from .models import AbapObjectNotFoundError, AbapProfile, AdtError

#: Seconds a held lock may sit unused before it is released. SAP ends idle
#: stateful sessions on its own after a while; this stays below the usual
#: ICM/ADT timeouts so the plugin, not the server, decides.
LOCK_IDLE_SECONDS = 15 * 60


# Session runner


@contextmanager
def with_client(profile: AbapProfile, stateful: bool = False) -> Iterator[AdtSession]:
    """A logged-in session that is dropped afterwards, whatever happens."""
    session = AdtSession(profile, stateful=stateful)
    try:
        session.login()
        yield session
    finally:
        try:
            session.drop_session()
        except Exception:
            pass


# Object resolution


def pick_search_result(results: List[Dict[str, Any]], query: str) -> Optional[Dict[str, Any]]:
    """An exact (case-insensitive) name match wins, otherwise the first hit."""
    if not results:
        return None
    wanted = query.lower()
    return next((r for r in results if str(r.get("adtcore:name", "")).lower() == wanted), results[0])


@dataclass
class ResolvedObject:
    name: str
    type: str
    object_url: str
    structure: Dict[str, Any]
    main_include: str


def resolve_object(
    session: AdtSession,
    name: Optional[str] = None,
    object_url: Optional[str] = None,
    type_hint: Optional[str] = None,
) -> ResolvedObject:
    if object_url:
        structure = adt.object_structure(session, object_url)
        meta = structure["metaData"]
        return ResolvedObject(
            name=meta.get("adtcore:name", ""),
            type=meta.get("adtcore:type", ""),
            object_url=object_url,
            structure=structure,
            main_include=adt.main_include(structure),
        )
    if not name:
        raise AdtError("Provide either 'name' or 'objectUrl' to address an object.")

    results = adt.search_object(session, name, type_hint, 20)
    match = pick_search_result(results, name)
    if match is None:
        raise AbapObjectNotFoundError(name)
    url = match["adtcore:uri"]
    structure = adt.object_structure(session, url)
    meta = structure["metaData"]
    return ResolvedObject(
        name=meta.get("adtcore:name") or match.get("adtcore:name", ""),
        type=meta.get("adtcore:type") or match.get("adtcore:type", ""),
        object_url=url,
        structure=structure,
        main_include=adt.main_include(structure),
    )


# Lock registry


@dataclass
class HeldLock:
    profile_key: str
    object_url: str
    name: str
    lock_handle: str
    session: AdtSession
    lock_result: Dict[str, Any] = field(default_factory=dict)
    last_used: float = field(default_factory=time.monotonic)


def profile_key(profile: AbapProfile) -> str:
    return f"{profile.url}|{profile.client or ''}|{profile.username.upper()}"


class LockRegistry:
    """Open stateful sessions that hold ADT locks, keyed by lock handle."""

    def __init__(self) -> None:
        self._locks: Dict[str, HeldLock] = {}
        self._mutex = threading.RLock()

    def add(self, held: HeldLock) -> None:
        with self._mutex:
            self._locks[held.lock_handle] = held

    def by_handle(self, profile: AbapProfile, handle: str) -> Optional[HeldLock]:
        self.expire()
        with self._mutex:
            held = self._locks.get(handle)
            if held and held.profile_key == profile_key(profile):
                held.last_used = time.monotonic()
                return held
            return None

    def by_object(self, profile: AbapProfile, object_url: str) -> Optional[HeldLock]:
        self.expire()
        key = profile_key(profile)
        with self._mutex:
            for held in self._locks.values():
                if held.profile_key == key and held.object_url == object_url:
                    held.last_used = time.monotonic()
                    return held
            return None

    def by_name(self, profile: AbapProfile, name: str) -> Optional[HeldLock]:
        self.expire()
        key = profile_key(profile)
        wanted = name.upper()
        with self._mutex:
            matches = [h for h in self._locks.values() if h.profile_key == key and h.name.upper() == wanted]
            if len(matches) == 1:  # ambiguous names are resolved by the normal search
                matches[0].last_used = time.monotonic()
                return matches[0]
            return None

    def remove(self, handle: str) -> Optional[HeldLock]:
        with self._mutex:
            return self._locks.pop(handle, None)

    def all(self) -> List[HeldLock]:
        with self._mutex:
            return list(self._locks.values())

    def release(self, held: HeldLock) -> None:
        """Unlock and close the session, quietly."""
        self.remove(held.lock_handle)
        with held.session.lock:
            try:
                adt.unlock(held.session, held.object_url, held.lock_handle)
            except Exception:
                pass
            try:
                held.session.drop_session()
            except Exception:
                pass

    def expire(self, now: Optional[float] = None) -> List[HeldLock]:
        now = time.monotonic() if now is None else now
        with self._mutex:
            stale = [h for h in self._locks.values() if now - h.last_used > LOCK_IDLE_SECONDS]
        for held in stale:
            self.release(held)
        return stale

    def release_all(self) -> None:
        for held in self.all():
            self.release(held)


LOCKS = LockRegistry()


def find_held(
    profile: AbapProfile,
    lock_handle: Optional[str] = None,
    object_url: Optional[str] = None,
    name: Optional[str] = None,
) -> Optional[HeldLock]:
    """The held lock a call refers to: by handle, else by object URL, else by name."""
    if lock_handle:
        held = LOCKS.by_handle(profile, lock_handle)
        if held is not None:
            return held
    if object_url:
        held = LOCKS.by_object(profile, object_url)
        if held is not None:
            return held
    if name:
        return LOCKS.by_name(profile, name)
    return None


@contextmanager
def held_session(held: HeldLock) -> Iterator[AdtSession]:
    """Run inside the session that owns a lock. The session stays open.

    If SAP has ended the session in the meantime, the lock is gone with it:
    the registry entry is dropped and the error says so.
    """
    with held.session.lock:
        try:
            yield held.session
        except AdtError as err:
            if err.is_login_error:
                LOCKS.remove(held.lock_handle)
                raise AdtError(
                    f"The session holding the lock on {held.name} has ended on the SAP side, "
                    f"so the lock is gone. Lock the object again with abap_lock. ({err})",
                    err.status,
                ) from None
            raise
