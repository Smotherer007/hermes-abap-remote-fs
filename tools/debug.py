"""ABAP debugger tools: abap_debug_listen, abap_debug_attach, abap_debug_step,
abap_debug_stack, abap_debug_variables, abap_debug_breakpoint.

These talk to the same ADT resources SAP's own IDE uses, so the debug session
belongs to the ADT session and lives in an ABAP roll area. Every handler
therefore runs on a *stateful* session and keeps it open for the duration of one
call; the debug session itself is identified by SAP through the
``sap-contextid`` cookie and the listener, not by anything we hold.
"""

from __future__ import annotations

from typing import Any, Dict, List

from .. import adt_debug as dbg
from .. import config as cfg
from ..client import resolve_object, with_client
from ._base import PROFILE_PROPERTY, error, opt_bool, opt_int, opt_str, req_str, result, schema, tool_handler

DEBUG_LEVELS = ("user", "terminal")

# abap_debug_listen

LISTEN_SCHEMA = schema(
    "abap_debug_listen",
    "Wait for a program of this user to reach a breakpoint and return the debuggee. Blocks until a "
    "breakpoint is hit or the timeout expires. Set the breakpoint first (abap_debug_breakpoint), then "
    "start the program in SAP GUI, then call this.",
    {
        "profile": PROFILE_PROPERTY,
        "user": {"type": "string", "description": "SAP user to debug. Default: the profile's user."},
        "timeout": {"type": "integer", "description": "Seconds to wait. Default 60."},
        "debuggingMode": {
            "type": "string",
            "enum": list(DEBUG_LEVELS),
            "description": "user (all sessions of the user) or terminal (this terminal only). Default user.",
        },
    },
)


@tool_handler
def listen(args: dict) -> str:
    user = opt_str(args, "user")
    timeout = opt_int(args, "timeout") or 60
    mode = (opt_str(args, "debuggingMode") or "user").lower()
    if mode not in DEBUG_LEVELS:
        return error(f"debuggingMode must be one of {', '.join(DEBUG_LEVELS)}.")
    config = cfg.resolve_config(opt_str(args, "profile"))
    with with_client(config, stateful=True) as session:
        debuggee = dbg.listen(session, user=user, debugging_mode=mode, timeout=timeout)
    if not debuggee:
        return result(
            f"No debuggee within {timeout}s. Is the breakpoint set and the program running in SAP GUI?",
            debuggee=None,
        )
    lines = [
        "Debuggee waiting:",
        f"  id      {debuggee.get('DEBUGGEE_ID', '')}",
        f"  user    {debuggee.get('DEBUGGEE_USER', '')}  system {debuggee.get('SYSID', '')}/{debuggee.get('CLIENT', '')}",
        f"  program {debuggee.get('PRG_CURR', '')}  include {debuggee.get('INCL_CURR', '')}  line {debuggee.get('LINE_CURR', '')}",
    ]
    return result("\n".join(lines), debuggee=debuggee)


# abap_debug_attach

ATTACH_SCHEMA = schema(
    "abap_debug_attach",
    "Attach the debugger to a waiting debuggee (id from abap_debug_listen). After attaching the program is "
    "stopped and can be stepped and inspected.",
    {
        "profile": PROFILE_PROPERTY,
        "debuggeeId": {"type": "string", "description": "DEBUGGEE_ID from abap_debug_listen."},
        "user": {"type": "string", "description": "SAP user. Default: the profile's user."},
    },
    ["debuggeeId"],
)


@tool_handler
def attach(args: dict) -> str:
    debuggee_id = req_str(args, "debuggeeId")
    config = cfg.resolve_config(opt_str(args, "profile"))
    with with_client(config, stateful=True) as session:
        state = dbg.attach(session, debuggee_id, user=opt_str(args, "user"))
    text = [
        "Attached. The program is stopped.",
        f"  session  {state.get('debugSessionId', '')}",
        f"  process  {state.get('processId', '')}  server {state.get('serverName', '')}",
        f"  changes  {'allowed' if str(state.get('isUserAuthorizedForChanges', '')).lower() == 'true' else 'read only'}",
    ]
    breakpoints = [b for b in state.get("reachedBreakpoints", []) if b]
    if breakpoints:
        text.append("  reached  " + ", ".join(b.get("id", "?") for b in breakpoints))
    actions = [a.get("name", "") for a in state.get("actions", []) if a.get("name")]
    if actions:
        text.append("  actions  " + ", ".join(actions))
    return result("\n".join(text), state=state)


# abap_debug_step

STEP_SCHEMA = schema(
    "abap_debug_step",
    "Continue a debugged program: stepInto, stepOver, stepReturn, stepContinue (run to the next "
    "breakpoint), stepRunToLine (with uri), terminateDebuggee or detachDebugger.",
    {
        "profile": PROFILE_PROPERTY,
        "action": {"type": "string", "enum": list(dbg.STEP_METHODS), "description": "What to do."},
        "uri": {"type": "string", "description": "Position for stepRunToLine (source uri with #start=<line>)."},
    },
    ["action"],
)


@tool_handler
def step(args: dict) -> str:
    action = req_str(args, "action")
    config = cfg.resolve_config(opt_str(args, "profile"))
    with with_client(config, stateful=True) as session:
        state = dbg.step(session, action, opt_str(args, "uri") or "")
    lines = [f"{action} done."]
    if state.get("isDebuggeeChanged") is not None:
        lines.append(f"  debuggee changed  {state.get('isDebuggeeChanged')}")
    if state.get("isTerminationPossible") is not None:
        lines.append(f"  can terminate     {state.get('isTerminationPossible')}")
    stack = dbg_stack_summary(config)
    if stack:
        lines.extend(stack)
    return result("\n".join(lines), state=state)


def dbg_stack_summary(config) -> List[str]:
    """Current position after a step, as a few readable lines."""
    try:
        with with_client(config, stateful=True) as session:
            frames = dbg.stack(session)
    except Exception:  # a terminated debuggee has no stack; that is not an error
        return []
    if not frames:
        return []
    out = ["  current position:"]
    for frame in frames[:5]:
        out.append(
            "    " + str(frame.get("stackPosition", "?")) + "  "
            + str(frame.get("programName", "")) + "  "
            + str(frame.get("includeName", "")) + "  line " + str(frame.get("line", "")) + "  "
            + str(frame.get("eventType", "")) + " " + str(frame.get("eventName", ""))
        )
    return out


# abap_debug_stack

STACK_SCHEMA = schema(
    "abap_debug_stack",
    "Read the call stack of the attached debuggee: program, include, line and event per frame.",
    {"profile": PROFILE_PROPERTY},
)


@tool_handler
def stack(args: dict) -> str:
    config = cfg.resolve_config(opt_str(args, "profile"))
    with with_client(config, stateful=True) as session:
        frames = dbg.stack(session)
    if not frames:
        return result("No stack. Is the debugger attached and the program stopped?")
    lines = [f"Call stack ({len(frames)} frames):"]
    for frame in frames:
        lines.append(
            "  " + str(frame.get("stackPosition", "?")) + "  "
            + str(frame.get("programName", "")) + "  "
            + str(frame.get("includeName", "")) + "  line " + str(frame.get("line", "")) + "  "
            + str(frame.get("eventType", "")) + " " + str(frame.get("eventName", ""))
            + ("  [system]" if str(frame.get("systemProgram", "")).lower() == "true" else "")
        )
    return result("\n".join(lines), stack=frames)


# abap_debug_variables

VARIABLES_SCHEMA = schema(
    "abap_debug_variables",
    "Read the variables of the stopped program. Without parent the visible variables (@ROOT) are read; "
    "with parent the components of that variable (structure, table, object).",
    {
        "profile": PROFILE_PROPERTY,
        "parent": {"type": "string", "description": "Variable id to expand, e.g. from a previous answer."},
        "maxRows": {"type": "integer", "description": "Maximum variables to show. Default 60."},
    },
)


def _variable_line(var: Dict[str, Any]) -> str:
    name = str(var.get("NAME", "")).strip()
    kind = str(var.get("PARAMETER_KIND", "")).strip().lower()
    declared = str(var.get("DECLARED_TYPE_NAME", "")).strip()
    meta = str(var.get("META_TYPE", "")).strip()
    value = str(var.get("VALUE", "")).strip()
    lines = str(var.get("TABLE_LINES", "") or "").strip()
    parts = ["  " + (kind + " " if kind else "") + name.ljust(18), declared]
    if meta:
        parts.append(f"[{meta}]")
    if lines:
        parts.append(f"rows={lines}")
    if value:
        parts.append("= " + value.replace("\n", " ")[:120])
    return "  ".join(parts)


@tool_handler
def variables(args: dict) -> str:
    parent = opt_str(args, "parent")
    limit = opt_int(args, "maxRows") or 60
    config = cfg.resolve_config(opt_str(args, "profile"))
    with with_client(config, stateful=True) as session:
        if parent:
            result_set = dbg.child_variables(session, [parent])
            items = result_set.get("variables", [])
            heading = f"Components of {parent} ({len(items)}):"
        else:
            items = dbg.variables(session, ["@ROOT"])
            heading = f"Variables in scope ({len(items)}):"
    if not items:
        return result("No variables. Is the debugger attached and the program stopped?")
    lines = [heading]
    for var in items[:limit]:
        lines.append(_variable_line(var))
    if len(items) > limit:
        lines.append(f"  ... {len(items) - limit} more; raise maxRows or expand a parent.")
    lines.append("  (expand with parent=<ID>; IDs are the technical names in the last column of the raw data)")
    return result("\n".join(lines), variables=items)


# abap_debug_breakpoint

BREAKPOINT_SCHEMA = schema(
    "abap_debug_breakpoint",
    "Manage ABAP breakpoints: action=set sets a line breakpoint (objectUrl or name plus line), "
    "action=delete removes it by breakpointId, action=list shows the breakpoints this agent set.",
    {
        "profile": PROFILE_PROPERTY,
        "action": {"type": "string", "enum": ["set", "delete", "list"], "description": "What to do."},
        "name": {"type": "string", "description": "Object name, e.g. RSPARAM or ZCL_X."},
        "objectUrl": {"type": "string", "description": "Object URL instead of name."},
        "line": {"type": "integer", "description": "Line number for action=set."},
        "breakpointId": {"type": "string", "description": "Breakpoint id for action=delete."},
        "scope": {
            "type": "string",
            "enum": ["external", "debugger"],
            "description": "external (survives sessions, default) or debugger (this session only).",
        },
        "user": {"type": "string", "description": "SAP user the breakpoint applies to. Default: profile user."},
    },
    ["action"],
)


def _source_uri(session, args: dict) -> str:
    target = {"name": opt_str(args, "name"), "objectUrl": opt_str(args, "objectUrl")}
    if not target["name"] and not target["objectUrl"]:
        raise ValueError("Give name or objectUrl.")
    resolved = resolve_object(session, **target)
    return resolved.main_include


@tool_handler
def breakpoint(args: dict) -> str:
    action = req_str(args, "action").lower()
    profile_name = opt_str(args, "profile")
    config = cfg.resolve_config(profile_name)
    user = opt_str(args, "user") or config.username
    scope = (opt_str(args, "scope") or "external").lower()

    if action == "list":
        known = dbg.known_breakpoints(config.url)
        if not known:
            return result("No breakpoints recorded for this system.")
        lines = [f"Breakpoints recorded for {config.url}:"]
        for entry in known:
            lines.append(f"  {entry.get('id', '?')}  {entry.get('uri', '')}  {entry.get('kind', '')}")
        return result("\n".join(lines), breakpoints=known)

    if action == "delete":
        breakpoint_id = req_str(args, "breakpointId")
        with with_client(config, stateful=True) as session:
            dbg.delete_breakpoint(session, breakpoint_id, user=user, scope=scope)
        dbg.forget_breakpoint(config.url, breakpoint_id)
        return result(f"Breakpoint {breakpoint_id} deleted.")

    line = opt_int(args, "line")
    if not line:
        return error("action=set needs line.")
    with with_client(config, stateful=True) as session:
        source_uri = _source_uri(session, args)
        uri = f"{source_uri}#start={line}"
        created = dbg.set_breakpoints(
            session, [uri], user=user, scope=scope, sync_uri=source_uri
        )
    dbg.remember_breakpoints(config.url, created)
    if not created:
        return result(
            "SAP accepted the request but returned no breakpoint. Check the line number (the program must "
            "have a statement there) and whether the user may debug this object.",
            requested=uri,
        )
    lines = ["Breakpoint set:"]
    for entry in created:
        lines.append(f"  {entry.get('id', '?')}  {entry.get('uri', uri)}")
    if str(created[0].get("errorMessage", "")).strip():
        lines.append("  SAP said: " + str(created[0]["errorMessage"]))
    return result("\n".join(lines), breakpoints=created)


__all__ = [
    "LISTEN_SCHEMA",
    "listen",
    "ATTACH_SCHEMA",
    "attach",
    "STEP_SCHEMA",
    "step",
    "STACK_SCHEMA",
    "stack",
    "VARIABLES_SCHEMA",
    "variables",
    "BREAKPOINT_SCHEMA",
    "breakpoint",
]
