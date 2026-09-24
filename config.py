"""Configuration persistence and state.

Stores named SAP connection profiles in ``$HERMES_HOME/abap-config.json``
(override with ``ABAP_CONFIG``). Because ``HERMES_HOME`` is per Hermes
profile, every Hermes profile gets its own set of systems.

File format -- identical to pi-abap-fs, so the same file works for both::

    { "profiles": { "dev": {"url": ..., "username": ..., "password": ...} }, "activeProfile": "dev" }

Environment fallback: ``ABAP_URL`` + ``ABAP_USER`` + ``ABAP_PASSWORD``
(optionally ``ABAP_CLIENT``, ``ABAP_LANGUAGE``, ``ABAP_ALLOW_UNAUTHORIZED``,
``ABAP_PROFILE_NAME``) define one more profile that lives only in memory.
That suits containers, where the password arrives as an environment variable
and should not sit in a file the agent can edit. A file profile with the
same name wins.

Hermes runs tools on several threads and the gateway and CLI can be separate
processes, so all access goes through a lock and the file is re-read when its
modification time changes.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlparse

from .models import AbapNotConfiguredError, AbapProfile, AbapProfileNotFoundError

_lock = threading.RLock()

# Mutable state
_profiles: Dict[str, AbapProfile] = {}
_active: Optional[str] = None
_loaded_mtime: Optional[int] = None
_loaded_path: Optional[Path] = None


# Path resolution


def _hermes_home() -> Path:
    try:  # the documented resolver honours profile overrides; fall back outside Hermes
        from hermes_constants import get_hermes_home  # type: ignore

        return Path(get_hermes_home())
    except Exception:
        home = os.environ.get("HERMES_HOME", "").strip()
        return Path(home).expanduser() if home else Path.home() / ".hermes"


def config_path() -> Path:
    override = os.environ.get("ABAP_CONFIG", "").strip()
    if override:
        return Path(override).expanduser()
    return _hermes_home() / "abap-config.json"


# URL normalisation


def normalize_base_url(raw: str) -> str:
    """The ADT base URL without trailing slashes. The scheme is required:
    on-premise dev systems often speak plain HTTP on port 80xx, so guessing
    https would be wrong as often as right."""
    url = str(raw if raw is not None else "").strip().rstrip("/")
    if not url:
        raise ValueError("The ADT URL must not be empty.")
    if url.lower().endswith("/sap/bc/adt"):
        url = url[: -len("/sap/bc/adt")]
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc or " " in url:
        raise ValueError(
            f'"{raw}" is not a valid ADT base URL. Include the scheme and port, '
            "e.g. http://sap.example.com:8000 or https://sap.example.com:44300."
        )
    return url


# Environment profile


def _env_profile() -> Optional[tuple]:
    url = os.environ.get("ABAP_URL", "").strip()
    user = os.environ.get("ABAP_USER", "").strip()
    password = os.environ.get("ABAP_PASSWORD", "")
    if not url or not user or not password:
        return None
    try:
        normalized = normalize_base_url(url)
    except ValueError:
        return None
    name = os.environ.get("ABAP_PROFILE_NAME", "").strip() or "default"
    return name, AbapProfile(
        url=normalized,
        username=user,
        password=password,
        client=os.environ.get("ABAP_CLIENT", "").strip() or None,
        language=os.environ.get("ABAP_LANGUAGE", "").strip() or None,
        allow_unauthorized=os.environ.get("ABAP_ALLOW_UNAUTHORIZED", "").strip().lower() in ("1", "true", "yes"),
    )


# Persistence


def _persist() -> None:
    """Write the profile store.

    The file holds plaintext API keys, so it must never be group- or
    world-readable. We write to a private temp file and rename it into place:
    that fixes the mode of an existing file and is atomic, so a crash
    mid-write cannot truncate the config. Environment profiles are never
    written.
    """
    global _loaded_mtime, _loaded_path
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    env = _env_profile()
    file_profiles = {
        name: cfg.to_json()
        for name, cfg in _profiles.items()
        if not (env and name == env[0] and cfg == env[1])
    }
    data = {"profiles": file_profiles, "activeProfile": _active}
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    _loaded_path = path
    try:
        _loaded_mtime = path.stat().st_mtime_ns
    except OSError:
        _loaded_mtime = None


def load_config() -> None:
    """(Re)load profiles from disk and the environment."""
    global _profiles, _active, _loaded_mtime, _loaded_path
    with _lock:
        path = config_path()
        profiles: Dict[str, AbapProfile] = {}
        active: Optional[str] = None
        mtime: Optional[int] = None

        try:
            if path.exists():
                mtime = path.stat().st_mtime_ns
                raw = json.loads(path.read_text(encoding="utf-8"))
                # Tighten permissions on files written by older versions or by hand.
                try:
                    if (path.stat().st_mode & 0o777) != 0o600:
                        os.chmod(path, 0o600)
                except OSError:
                    pass

                if isinstance(raw, dict) and isinstance(raw.get("profiles"), dict):
                    profiles = {
                        str(name): AbapProfile.from_json(value)
                        for name, value in raw["profiles"].items()
                        if isinstance(value, dict)
                    }
                    requested = raw.get("activeProfile")
                    active = requested if requested in profiles else None
        except Exception:
            profiles, active = {}, None

        env = _env_profile()
        if env and env[0] not in profiles:
            profiles[env[0]] = env[1]
        if active is None and profiles:
            active = next(iter(profiles))

        _profiles, _active = profiles, active
        _loaded_path, _loaded_mtime = path, mtime


def _refresh_if_changed() -> None:
    """Pick up edits made by another process (e.g. abap_setup in the CLI while the gateway runs)."""
    path = config_path()
    try:
        mtime = path.stat().st_mtime_ns if path.exists() else None
    except OSError:
        mtime = None
    if path != _loaded_path or mtime != _loaded_mtime:
        load_config()


# Access


def get_config() -> Optional[AbapProfile]:
    with _lock:
        _refresh_if_changed()
        if not _active:
            return None
        return _profiles.get(_active)


def get_config_or_raise() -> AbapProfile:
    config = get_config()
    if config is None:
        raise AbapNotConfiguredError()
    return config


def get_profile(name: str) -> Optional[AbapProfile]:
    with _lock:
        _refresh_if_changed()
        return _profiles.get(name)


def resolve_config(profile_name: Optional[str] = None) -> AbapProfile:
    """Return the named profile (or raise), or the active one."""
    if profile_name:
        with _lock:
            _refresh_if_changed()
            config = _profiles.get(profile_name)
            if config is None:
                raise AbapProfileNotFoundError(profile_name, list(_profiles))
            return config
    return get_config_or_raise()


def get_profiles() -> Dict[str, AbapProfile]:
    with _lock:
        _refresh_if_changed()
        return dict(_profiles)


def get_active_profile() -> Optional[str]:
    with _lock:
        _refresh_if_changed()
        return _active


def is_env_profile(name: str) -> bool:
    env = _env_profile()
    with _lock:
        return bool(env and name == env[0] and _profiles.get(name) == env[1])


# Mutations


def save_profile(name: str, config: AbapProfile) -> None:
    global _active
    with _lock:
        _refresh_if_changed()
        normalized = AbapProfile(
            url=normalize_base_url(config.url),
            username=config.username,
            password=config.password,
            client=config.client,
            language=config.language,
            allow_unauthorized=config.allow_unauthorized,
        )
        _profiles[name] = normalized
        if not _active or len(_profiles) == 1:
            _active = name
        _persist()


def set_active_profile(name: str) -> None:
    global _active
    with _lock:
        _refresh_if_changed()
        if name not in _profiles:
            raise AbapProfileNotFoundError(name, list(_profiles))
        _active = name
        _persist()


def delete_profile(name: str) -> bool:
    global _active
    with _lock:
        _refresh_if_changed()
        if name not in _profiles:
            return False
        if is_env_profile(name):
            raise ValueError(
                f'Profile "{name}" comes from the ABAP_URL / ABAP_USER / ABAP_PASSWORD environment '
                "variables. Remove it there instead."
            )
        del _profiles[name]
        if _active == name:
            _active = next(iter(_profiles), None)
        _persist()
        return True


def _reset_for_testing() -> None:
    global _profiles, _active, _loaded_mtime, _loaded_path
    with _lock:
        _profiles, _active = {}, None
        _loaded_mtime, _loaded_path = None, None
