"""Tools that change the system: abap_lock, abap_unlock, abap_write, abap_activate,
abap_set_text_elements, abap_create_object.

Every one of them first looks for a lock this agent already holds on the
object (see ``client.LOCKS``) and, if there is one, works inside that lock's
session. Otherwise it behaves like pi-abap-fs: lock, change, unlock in one
session of its own.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Tuple

from .. import adt, xmlutil
from .. import config as cfg
from ..adt_http import AdtSession
from ..client import (
    LOCKS,
    HeldLock,
    ResolvedObject,
    find_held,
    held_session,
    profile_key,
    resolve_object,
    with_client,
)
from ..formatters import format_activation, format_lock
from ..models import AbapProfile, AdtError, ToolInputError
from ._base import (
    OBJECT_PROPERTIES,
    PROFILE_PROPERTY,
    object_args,
    opt_bool,
    opt_str,
    req_str,
    result,
    schema,
    tool_handler,
)

TRANSPORT_PROPERTY = {
    "type": "string",
    "description": "Transport / correction request number, e.g. 'DEVK900123'.",
}


def _in_held_or_fresh(
    config: AbapProfile,
    target: Dict[str, Any],
    work: Callable[[AdtSession, ResolvedObject, Optional[HeldLock]], Any],
    lock_handle: Optional[str] = None,
    stateful: bool = True,
) -> Tuple[Any, ResolvedObject, Optional[HeldLock]]:
    """Run ``work`` in the session of a held lock on the object, or in a fresh one."""
    held = find_held(config, lock_handle, target.get("object_url"), target.get("name"))
    if held is not None:
        with held_session(held) as session:
            resolved = resolve_object(session, object_url=held.object_url)
            return work(session, resolved, held), resolved, held

    with with_client(config, stateful=stateful) as session:
        resolved = resolve_object(session, **target)
        # Addressed by name, the object may still be one we hold a lock on.
        held = LOCKS.by_object(config, resolved.object_url)
        if held is None:
            return work(session, resolved, None), resolved, None
    with held_session(held) as session:
        return work(session, resolved, held), resolved, held


# abap_lock

LOCK_SCHEMA = schema(
    "abap_lock",
    "Lock an ABAP object for editing. Returns a lock handle; the lock stays held (in its own SAP session) "
    "until abap_unlock, or 15 minutes without use. While it is held, abap_write, abap_set_text_elements and "
    "abap_activate on that object work inside the lock automatically. Provide either a name (with optional "
    "type hint) or an object URL.",
    {
        "profile": PROFILE_PROPERTY,
        **OBJECT_PROPERTIES,
        "accessMode": {"type": "string", "description": "Lock access mode, e.g. 'MODIFY' (default) or 'EDIT'."},
    },
)


@tool_handler
def lock(args: dict) -> str:
    target = object_args(args)
    config = cfg.resolve_config(opt_str(args, "profile"))
    existing = find_held(config, None, target.get("object_url"), target.get("name"))
    if existing is not None:
        return result(
            f"Already locked by this agent. lockHandle={existing.lock_handle}",
            name=existing.name,
            objectUrl=existing.object_url,
            lockHandle=existing.lock_handle,
            corrNr=existing.lock_result.get("CORRNR"),
            alreadyHeld=True,
        )

    session = AdtSession(config, stateful=True)
    try:
        session.login()
        resolved = resolve_object(session, **target)
        held = LOCKS.by_object(config, resolved.object_url)
        if held is not None:  # addressed by name, already held
            session.drop_session()
            return result(
                f"Already locked by this agent. lockHandle={held.lock_handle}",
                name=held.name, objectUrl=held.object_url, lockHandle=held.lock_handle,
                corrNr=held.lock_result.get("CORRNR"), alreadyHeld=True,
            )
        lock_result = adt.lock(session, resolved.object_url, opt_str(args, "accessMode") or "MODIFY")
    except Exception:
        try:
            session.drop_session()
        except Exception:
            pass
        raise

    LOCKS.add(HeldLock(
        profile_key=profile_key(config),
        object_url=resolved.object_url,
        name=resolved.name,
        lock_handle=lock_result.get("LOCK_HANDLE", ""),
        session=session,
        lock_result=lock_result,
    ))
    return result(
        format_lock(lock_result) + "\nThe lock is held until abap_unlock (or 15 minutes without use).",
        name=resolved.name,
        objectUrl=resolved.object_url,
        mainInclude=resolved.main_include,
        lockHandle=lock_result.get("LOCK_HANDLE"),
        corrNr=lock_result.get("CORRNR"),
    )


# abap_unlock

UNLOCK_SCHEMA = schema(
    "abap_unlock",
    "Release a lock on an ABAP object. Provide the object URL (or name) and the lock handle returned by "
    "abap_lock.",
    {
        "profile": PROFILE_PROPERTY,
        "objectUrl": {"type": "string", "description": "Object URL, e.g. '/sap/bc/adt/oo/classes/zcl_my_class'."},
        "name": {"type": "string", "description": "Object name (used only if objectUrl omitted)."},
        "type": OBJECT_PROPERTIES["type"],
        "lockHandle": {"type": "string", "description": "The lock handle returned by abap_lock."},
    },
    ["lockHandle"],
)


@tool_handler
def unlock(args: dict) -> str:
    handle = req_str(args, "lockHandle")
    config = cfg.resolve_config(opt_str(args, "profile"))
    held = LOCKS.by_handle(config, handle)
    if held is not None:
        LOCKS.remove(handle)
        with held.session.lock:
            try:
                adt.unlock(held.session, held.object_url, handle)
            finally:
                try:
                    held.session.drop_session()
                except Exception:
                    pass
        return result(f"Unlocked {held.name} ({held.object_url}).", name=held.name, objectUrl=held.object_url)

    # Not a lock this process holds (e.g. from before a restart): try it the
    # way pi-abap-fs does. If the session behind it is gone, so is the lock.
    target = object_args(args)
    with with_client(config, stateful=True) as session:
        resolved = resolve_object(session, **target)
        adt.unlock(session, resolved.object_url, handle)
    return result(
        f"Unlocked {resolved.name} ({resolved.object_url}).",
        name=resolved.name,
        objectUrl=resolved.object_url,
    )


# abap_write

WRITE_SCHEMA = schema(
    "abap_write",
    "Write (save) source code to an ABAP object. Provide the object (name or objectUrl), the full new "
    "source, and optionally a transport request number. If this agent holds a lock on the object (from "
    "abap_lock), the write runs inside it and the lock stays; otherwise the object is locked, written, and "
    "unlocked automatically. Use abap_activate afterwards to activate the change.",
    {
        "profile": PROFILE_PROPERTY,
        **OBJECT_PROPERTIES,
        "source": {"type": "string", "description": "The full new source code to write."},
        "transport": {
            "type": "string",
            "description": "Transport / correction request number, e.g. 'DEVK900123'.",
        },
        "lockHandle": {
            "type": "string",
            "description": "Optional lock handle from a previous abap_lock call. The lock is used and NOT "
            "released automatically.",
        },
    },
    ["source"],
)


@tool_handler
def write(args: dict) -> str:
    target = object_args(args)
    source = opt_str(args, "source")
    if source is None:
        raise ToolInputError("source must be given (the full new source code).")
    transport = opt_str(args, "transport")
    lock_handle = opt_str(args, "lockHandle")
    config = cfg.resolve_config(opt_str(args, "profile"))

    def work(session: AdtSession, resolved: ResolvedObject, held: Optional[HeldLock]) -> bool:
        if held is not None:
            adt.set_object_source(session, resolved.main_include, source, held.lock_handle, transport)
            return False
        if lock_handle:
            # A handle this process does not hold: pass it through as pi-abap-fs does.
            try:
                adt.set_object_source(session, resolved.main_include, source, lock_handle, transport)
            except AdtError as err:
                raise AdtError(
                    f"{err} -- the lock handle is not held by this agent; ADT locks end with the session "
                    "that took them. Omit lockHandle to lock, write and unlock in one step, or lock again "
                    "with abap_lock.",
                    err.status,
                ) from None
            return False
        lock_result = adt.lock(session, resolved.object_url, "MODIFY")
        handle = lock_result.get("LOCK_HANDLE", "")
        try:
            adt.set_object_source(session, resolved.main_include, source, handle, transport)
        finally:
            adt.unlock(session, resolved.object_url, handle)
        return True

    self_locked, resolved, held = _in_held_or_fresh(config, target, work, lock_handle)
    if self_locked:
        lock_note = "Lock released automatically."
    elif held is not None:
        lock_note = "Written inside the lock held by this agent (lock stays)."
    else:
        lock_note = "Lock left in place (caller owns it)."
    return result(
        f"Source written to {resolved.name}. {lock_note} Use abap_activate to activate the change.",
        name=resolved.name,
        type=resolved.type,
        objectUrl=resolved.object_url,
        lines=len(source.split("\n")),
        lockReleased=self_locked,
    )


# abap_activate

ACTIVATE_SCHEMA = schema(
    "abap_activate",
    "Activate an ABAP object after writing changes. Provide the object (name or objectUrl). Returns "
    "activation messages and any inactive objects that resulted.",
    {
        "profile": PROFILE_PROPERTY,
        **OBJECT_PROPERTIES,
        "preauditRequested": {"type": "boolean", "description": "Request a pre-activation audit (default true)."},
    },
)


@tool_handler
def activate(args: dict) -> str:
    target = object_args(args)
    preaudit = opt_bool(args, "preauditRequested")
    config = cfg.resolve_config(opt_str(args, "profile"))

    def work(session: AdtSession, resolved: ResolvedObject, _held: Optional[HeldLock]) -> Dict[str, Any]:
        return adt.activate(
            session, resolved.name, resolved.object_url, resolved.main_include,
            True if preaudit is None else preaudit,
        )

    activation, resolved, _ = _in_held_or_fresh(config, target, work, stateful=False)
    return result(
        format_activation(activation),
        name=resolved.name,
        success=activation["success"],
        messages=activation["messages"],
        inactive=activation["inactive"],
    )


# abap_set_text_elements

SET_TEXT_ELEMENTS_SCHEMA = schema(
    "abap_set_text_elements",
    "Write (save) text elements for an ABAP object. Provide the object (name or objectUrl), the category, "
    "and an array of { id, text, maxLength?, ddicReference? }. The object is locked, written and unlocked "
    "automatically (or written inside a lock this agent already holds). Activate afterwards with "
    "abap_activate if needed.",
    {
        "profile": PROFILE_PROPERTY,
        **OBJECT_PROPERTIES,
        "name": {"type": "string", "description": "Object name, e.g. 'ZCL_MY_CLASS' or a program."},
        "category": {
            "type": "string",
            "enum": list(adt.TEXT_CATEGORIES),
            "description": "Text element category: symbols, selections or headings.",
        },
        "elements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Text element key, e.g. '001' or 'LISTHEADER'."},
                    "text": {"type": "string", "description": "Text content."},
                    "maxLength": {"type": "number", "description": "Maximum length (symbols only)."},
                    "ddicReference": {"type": "string", "description": "DDIC reference (selections only)."},
                },
                "required": ["id", "text"],
            },
        },
        "transport": TRANSPORT_PROPERTY,
    },
    ["category", "elements"],
)


def _elements(args: dict) -> list:
    raw = args.get("elements")
    if isinstance(raw, str):
        import json

        try:
            raw = json.loads(raw)
        except ValueError:
            raise ToolInputError("elements must be an array of { id, text } objects.") from None
    if not isinstance(raw, list) or not raw:
        raise ToolInputError("elements must be a non-empty array of { id, text } objects.")
    elements = []
    for item in raw:
        if not isinstance(item, dict) or "id" not in item or "text" not in item:
            raise ToolInputError("Every element needs an id and a text.")
        element: Dict[str, Any] = {"id": str(item["id"]), "text": str(item["text"])}
        if item.get("maxLength") not in (None, ""):
            element["maxLength"] = int(item["maxLength"])
        if item.get("ddicReference"):
            element["ddicReference"] = str(item["ddicReference"])
        elements.append(element)
    return elements


@tool_handler
def set_text_elements(args: dict) -> str:
    target = object_args(args)
    category = req_str(args, "category").lower()
    if category not in adt.TEXT_CATEGORIES:
        raise ToolInputError(f'Invalid category "{category}". Use symbols, selections or headings.')
    elements = _elements(args)
    adt.validate_text_elements(elements, category)  # fail before anything is locked
    transport = opt_str(args, "transport")
    config = cfg.resolve_config(opt_str(args, "profile"))

    def work(session: AdtSession, resolved: ResolvedObject, held: Optional[HeldLock]) -> None:
        url = adt.text_elements_url(resolved.type, resolved.name)
        if held is not None:
            adt.set_text_elements(session, url, category, elements, held.lock_handle, transport)
            return
        lock_result = adt.lock(session, resolved.object_url, "MODIFY")
        handle = lock_result.get("LOCK_HANDLE", "")
        try:
            adt.set_text_elements(session, url, category, elements, handle, transport)
        finally:
            adt.unlock(session, resolved.object_url, handle)

    _, resolved, _ = _in_held_or_fresh(config, target, work)
    return result(
        f"Wrote {len(elements)} {category} text element(s) to {resolved.name}. "
        "Activate the object afterwards if required.",
        name=resolved.name,
        category=category,
        count=len(elements),
    )


# abap_create_object

CREATE_OBJECT_SCHEMA = schema(
    "abap_create_object",
    "Create a new ABAP object programmatically. Provide the object type (e.g. CLAS/OC, INTF/OI, PROG/P, "
    "PROG/I, FUGR/F, TABL/DT, DTEL/DE, DOMA/DD, DDLS/DF), the name, the parent package, and a description. "
    "The object is created in the given transport (or locally when omitted).",
    {
        "profile": PROFILE_PROPERTY,
        "objectType": {
            "type": "string",
            "description": "Object type, e.g. 'CLAS/OC' (class), 'INTF/OI' (interface), 'PROG/P' (program), "
            "'PROG/I' (include), 'FUGR/F' (function group), 'FUGR/FF' (function module), 'TABL/DT' (table), "
            "'DTEL/DE' (data element), 'DOMA/DD' (domain), 'DDLS/DF' (CDS view).",
        },
        "name": {"type": "string", "description": "Object name, e.g. 'ZCL_MY_CLASS'."},
        "parentName": {
            "type": "string",
            "description": "Parent package name (e.g. 'Z_MY_PACKAGE') — or the function group name for FUGR/FF "
            "and FUGR/I. Use '$TMP' for local objects.",
        },
        "description": {"type": "string", "description": "Object description."},
        "transport": TRANSPORT_PROPERTY,
    },
    ["objectType", "name", "parentName", "description"],
)


def _parent_path(object_type: str, parent_name: str) -> str:
    encoded = xmlutil.uri_component(parent_name.lower())
    if object_type in ("FUGR/F", "FUGR/FF", "FUGR/I"):
        return f"/sap/bc/adt/functions/groups/{encoded}"
    return f"/sap/bc/adt/packages/{encoded}" if parent_name else ""


@tool_handler
def create_object(args: dict) -> str:
    object_type = req_str(args, "objectType").upper()
    if object_type not in adt.CREATABLE_TYPES:
        raise ToolInputError(
            f'Unsupported object type "{args.get("objectType")}". Supported: {", ".join(adt.CREATABLE_TYPES)}'
        )
    name = req_str(args, "name").strip()
    parent = req_str(args, "parentName").strip()
    description = req_str(args, "description")
    transport = opt_str(args, "transport")
    config = cfg.resolve_config(opt_str(args, "profile"))
    with with_client(config) as session:
        adt.create_object(
            session, object_type, name, parent, description, _parent_path(object_type, parent),
            transport=transport, language=config.language or "EN",
        )
    return result(
        f'Created {object_type} "{name}" in {parent}.'
        + (f" Transport: {transport}." if transport else "")
        + " Use abap_read to verify and abap_write to add source code.",
        objectType=object_type,
        name=name,
        parentName=parent,
        transport=transport,
    )
