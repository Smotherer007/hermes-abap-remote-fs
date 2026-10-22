"""ABAP Debugger over the ADT debugger resources.

SAP's own IDE uses these resources for its debugger, so nothing has to be
installed on the system: a listener is registered for a user or a terminal,
external breakpoints are set, and once a running program hits one, the session
is attached, stepped and read.

The debuggee lives in an ABAP roll area that is only reachable again through
the ``sap-contextid`` cookie, so every call here must run on the *same*
stateful :class:`AdtSession` (``with_client(config, stateful=True)``).

Wire format notes, because they are not obvious:

* ``POST /debugger/listeners`` is a long poll. It answers as soon as a debuggee
  waits (breakpoint hit) or the timeout expires, in which case the body is empty.
* ``/debugger`` itself is the action endpoint; the operation travels in the
  ``method`` query parameter (``attach``, ``stepInto``, ``getVariables``, ...).
* Variables are exchanged as ABAP serialized XML (``asx:abap``), not as
  attributes: one ``STPDA_ADT_VARIABLE`` element per variable.
* ``GET /debugger/breakpoints`` answers empty by design (the breakpoint set is
  the IDE's state), so the ids returned by the ``POST`` are remembered locally.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from xml.sax.saxutils import escape

from . import xmlutil
from .adt_http import AdtSession
from .models import AdtError

DEBUGGER = "/sap/bc/adt/debugger"
XML = "application/xml"
ASX_VARIABLES = "application/vnd.sap.as+xml;charset=UTF-8;dataname=com.sap.adt.debugger.Variables"
ASX_CHILD = "application/vnd.sap.as+xml;charset=UTF-8;dataname=com.sap.adt.debugger.ChildVariables"
ADT_CORE = "http://www.sap.com/adt/core"

TERMINAL_FILE = Path.home() / ".SAP" / "ABAPDebugging" / "terminalId"

STEP_METHODS = (
    "stepInto",
    "stepOver",
    "stepReturn",
    "stepContinue",
    "stepRunToLine",
    "terminateDebuggee",
    "detachDebugger",
)


# Response helpers


def _text(response: Any) -> str:
    body = response.body
    return body if isinstance(body, str) else body.decode("utf-8", "replace")


def _parse(response: Any) -> Dict[str, Any]:
    body = _text(response).strip()
    if not body:
        return {}
    return xmlutil.parse(body, remove_ns=True)


def _find(node: Any, tag: str) -> List[Any]:
    """Every node called *tag* anywhere in the tree, in document order."""
    found: List[Any] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == tag:
                found.extend(value if isinstance(value, list) else [value])
            elif isinstance(value, (dict, list)):
                found.extend(_find(value, tag))
    elif isinstance(node, list):
        for item in node:
            found.extend(_find(item, tag))
    return found


def _first(node: Any, tag: str) -> Optional[Any]:
    items = _find(node, tag)
    return items[0] if items else None


def _flat(node: Any) -> Dict[str, str]:
    """Attribute map of a node (``@_x`` keys) without the marker."""
    if not isinstance(node, dict):
        return {}
    return {key[2:]: value for key, value in node.items() if key.startswith("@_")}


def _fields(node: Any) -> Dict[str, str]:
    """Element children of a node as a flat string map (ASX structures)."""
    out: Dict[str, str] = {}
    if not isinstance(node, dict):
        return out
    for key, value in node.items():
        if key.startswith("@_"):
            out[key[2:]] = str(value)
        elif isinstance(value, str):
            out[key] = value
        elif isinstance(value, dict):
            out[key] = value.get("#text", "") if "#text" in value else json.dumps(value, ensure_ascii=False)
    return out


def _raise_from_exception(raw: Dict[str, Any]) -> None:
    """ADT reports listener conflicts with 200 and an ``exception`` element."""
    exc = _first(raw, "exception")
    if not isinstance(exc, dict):
        return
    localized = _first(exc, "localizedMessage")
    message = _first(exc, "message")
    text = ""
    for candidate in (localized, message):
        if isinstance(candidate, dict) and candidate.get("#text"):
            text = str(candidate["#text"]).strip()
            break
        if isinstance(candidate, str) and candidate.strip():
            text = candidate.strip()
            break
    props = ""
    for entry in _find(exc, "entry"):
        if isinstance(entry, dict) and entry.get("@_key") == "conflictText":
            props = str(entry.get("#text", "")).strip()
    raise AdtError(text or props or "The ADT debugger refused the request.")


# Local breakpoint bookkeeping


def state_file() -> Path:
    home = os.environ.get("HERMES_HOME", "").strip()
    base = Path(home).expanduser() if home else Path.home() / ".hermes"
    return base / "abap-debugger.json"


def _read_state() -> Dict[str, Any]:
    try:
        return json.loads(state_file().read_text())
    except (OSError, ValueError):
        return {}


def _write_state(data: Dict[str, Any]) -> None:
    path = state_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        os.chmod(path, 0o600)
    except OSError:
        pass


def remember_breakpoints(profile: str, entries: List[Dict[str, str]]) -> None:
    data = _read_state()
    kept = [b for b in data.get(profile, []) if b.get("id") not in {e.get("id") for e in entries}]
    data[profile] = kept + entries
    _write_state(data)


def forget_breakpoint(profile: str, breakpoint_id: str) -> None:
    data = _read_state()
    data[profile] = [b for b in data.get(profile, []) if b.get("id") != breakpoint_id]
    _write_state(data)


def known_breakpoints(profile: str) -> List[Dict[str, str]]:
    return _read_state().get(profile, [])


# Operations


def terminal_id() -> str:
    """A stable terminal id, the same one ABAP in Eclipse would use."""
    try:
        existing = TERMINAL_FILE.read_text().strip()
        if existing:
            return existing
    except OSError:
        pass
    value = uuid.uuid4().hex[:16]
    try:
        TERMINAL_FILE.parent.mkdir(parents=True, exist_ok=True)
        TERMINAL_FILE.write_text(value)
    except OSError:
        pass
    return value


def listen(
    h: AdtSession,
    user: Optional[str] = None,
    debugging_mode: str = "user",
    terminal: str = "",
    ide: str = "hermes",
    timeout: int = 60,
) -> Optional[Dict[str, str]]:
    """Wait for a debuggee. Returns None when the timeout expired."""
    qs = {
        "debuggingMode": debugging_mode,
        "terminalId": terminal or terminal_id(),
        "ideId": ide,
        "checkConflict": "true",
        "isNotifiedOnConflict": "true",
        "timeout": timeout,
    }
    if user:
        qs["requestUser"] = user
    response = h.request(
        DEBUGGER + "/listeners",
        "POST",
        qs=qs,
        headers={"Accept": XML},
        timeout=timeout + 20,
    )
    if not _text(response).strip():
        return None
    raw = _parse(response)
    _raise_from_exception(raw)
    debuggee = _first(raw, "STPDA_DEBUGGEE")
    if not isinstance(debuggee, dict):
        return None
    return _fields(debuggee)


def attach(
    h: AdtSession,
    debuggee_id: str,
    user: Optional[str] = None,
    debugging_mode: str = "user",
    dynpro: bool = True,
) -> Dict[str, Any]:
    qs = {
        "method": "attach",
        "debuggeeId": debuggee_id,
        "dynproDebugging": "true" if dynpro else "false",
        "debuggingMode": debugging_mode,
    }
    if user:
        qs["requestUser"] = user
    response = h.request(DEBUGGER, "POST", qs=qs, headers={"Accept": XML})
    raw = _parse(response)
    _raise_from_exception(raw)
    node = _first(raw, "attach")
    out: Dict[str, Any] = dict(_flat(node))
    out["actions"] = [_flat(a) for a in _find(node, "action")]
    out["reachedBreakpoints"] = [_flat(b) for b in _find(node, "breakpoint")]
    return out


def step(h: AdtSession, method: str, uri: str = "") -> Dict[str, Any]:
    if method not in STEP_METHODS:
        raise AdtError(f"Unknown debugger step '{method}'. Use one of: {', '.join(STEP_METHODS)}.")
    qs = {"method": method}
    if uri:
        qs["uri"] = uri
    response = h.request(DEBUGGER, "POST", qs=qs, headers={"Accept": XML})
    raw = _parse(response)
    _raise_from_exception(raw)
    node = _first(raw, "step")
    out: Dict[str, Any] = dict(_flat(node))
    out["actions"] = [_flat(a) for a in _find(node, "action")]
    out["reachedBreakpoints"] = [_flat(b) for b in _find(node, "breakpoint")]
    return out


def stack(h: AdtSession) -> List[Dict[str, str]]:
    response = h.request(
        DEBUGGER + "/stack",
        "GET",
        qs={"method": "getStack", "emode": "_", "semanticURIs": "true"},
        headers={"Accept": XML},
    )
    raw = _parse(response)
    _raise_from_exception(raw)
    return [_flat(entry) for entry in _find(raw, "stackEntry")]


def _asx(payload: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<asx:abap xmlns:asx="http://www.sap.com/abapxml" version="1.0">'
        f"<asx:values><DATA>{payload}</DATA></asx:values></asx:abap>"
    )


def variables(h: AdtSession, parents: List[str]) -> List[Dict[str, str]]:
    payload = "".join(f"<STPDA_ADT_VARIABLE><ID>{escape(p)}</ID></STPDA_ADT_VARIABLE>" for p in parents)
    response = h.request(
        DEBUGGER,
        "POST",
        qs={"method": "getVariables"},
        headers={"Accept": ASX_VARIABLES, "Content-Type": ASX_VARIABLES},
        body=_asx(payload),
    )
    raw = _parse(response)
    _raise_from_exception(raw)
    return [_fields(node) for node in _find(raw, "STPDA_ADT_VARIABLE")]


def child_variables(h: AdtSession, parents: List[str]) -> Dict[str, Any]:
    payload = "".join(
        f"<STPDA_ADT_VARIABLE_HIERARCHY><PARENT_ID>{escape(p)}</PARENT_ID></STPDA_ADT_VARIABLE_HIERARCHY>"
        for p in parents
    )
    body = _asx(f"<HIERARCHIES>{payload}</HIERARCHIES>")
    response = h.request(
        DEBUGGER,
        "POST",
        qs={"method": "getChildVariables"},
        headers={"Accept": ASX_CHILD, "Content-Type": ASX_CHILD},
        body=body,
    )
    raw = _parse(response)
    _raise_from_exception(raw)
    return {
        "variables": [_fields(node) for node in _find(raw, "STPDA_ADT_VARIABLE")],
    }


def set_breakpoints(
    h: AdtSession,
    uris: List[str],
    user: Optional[str] = None,
    debugging_mode: str = "user",
    terminal: str = "",
    ide: str = "hermes",
    scope: str = "external",
    sync_uri: str = "",
) -> List[Dict[str, str]]:
    client_id = uuid.uuid4().hex[:8]
    sync = (
        f'<syncScope mode="partial"><adtcore:objectReference xmlns:adtcore="{ADT_CORE}" adtcore:uri="{escape(sync_uri)}"/></syncScope>'
        if sync_uri
        else '<syncScope mode="full"></syncScope>'
    )
    entries = "".join(
        f'<breakpoint xmlns:adtcore="{ADT_CORE}" kind="line" clientId="{client_id}" skipCount="0" '
        f'adtcore:uri="{escape(uri)}"/>'
        for uri in uris
    )
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<dbg:breakpoints scope="{scope}" debuggingMode="{debugging_mode}"'
        + (f' requestUser="{escape(user)}"' if user else "")
        + f' terminalId="{escape(terminal or terminal_id())}" ideId="{escape(ide)}"'
        ' systemDebugging="false" deactivated="false" xmlns:dbg="http://www.sap.com/adt/debugger">'
        f"{sync}{entries}</dbg:breakpoints>"
    )
    response = h.request(
        DEBUGGER + "/breakpoints",
        "POST",
        headers={"Accept": XML, "Content-Type": XML},
        body=body,
    )
    raw = _parse(response)
    _raise_from_exception(raw)
    out: List[Dict[str, str]] = []
    for node in _find(raw, "breakpoint"):
        item = _flat(node)
        if item:
            out.append(item)
    return out


def delete_breakpoint(
    h: AdtSession,
    breakpoint_id: str,
    user: Optional[str] = None,
    debugging_mode: str = "user",
    terminal: str = "",
    ide: str = "hermes",
    scope: str = "external",
) -> None:
    qs = {
        "scope": scope,
        "debuggingMode": debugging_mode,
        "terminalId": terminal or terminal_id(),
        "ideId": ide,
    }
    if user:
        qs["requestUser"] = user
    h.request(
        DEBUGGER + "/breakpoints/" + escape(breakpoint_id),
        "DELETE",
        qs=qs,
        headers={"Accept": XML},
    )


def detach_listener(
    h: AdtSession,
    user: Optional[str] = None,
    debugging_mode: str = "user",
    terminal: str = "",
    ide: str = "hermes",
) -> None:
    qs = {
        "debuggingMode": debugging_mode,
        "terminalId": terminal or terminal_id(),
        "ideId": ide,
        "checkConflict": "false",
        "notifyConflict": "true",
    }
    if user:
        qs["requestUser"] = user
    h.request(DEBUGGER + "/listeners", "DELETE", qs=qs, headers={"Accept": XML})
