"""Operations and transport tools: abap_transports, abap_transport_details,
abap_dumps, abap_traces, abap_trace_hitlist."""

from __future__ import annotations

from .. import adt
from .. import config as cfg
from ..client import with_client
from ..formatters import (
    format_dumps,
    format_trace_hitlist,
    format_traces,
    format_transport_details,
    format_transports,
    transport_object_count,
)
from ._base import PROFILE_PROPERTY, opt_bool, opt_str, req_str, result, schema, tool_handler

# abap_transports

TRANSPORTS_SCHEMA = schema(
    "abap_transports",
    "List the transport requests of a user. By default lists the transports of the profile's username. Use "
    "the returned transport numbers with abap_write (transport parameter).",
    {
        "profile": PROFILE_PROPERTY,
        "user": {"type": "string", "description": "User to list transports for. Defaults to the connected user."},
    },
)


@tool_handler
def transports(args: dict) -> str:
    config = cfg.resolve_config(opt_str(args, "profile"))
    with with_client(config) as session:
        data = adt.user_transports(session, opt_str(args, "user") or config.username)
    return result(format_transports(data), transports=data)


# abap_transport_details

TRANSPORT_DETAILS_SCHEMA = schema(
    "abap_transport_details",
    "Show details of a single transport request: description, status, owner, its tasks and the objects it "
    "contains. Provide the transport number (e.g. 'DEVK900123') from abap_transports.",
    {
        "profile": PROFILE_PROPERTY,
        "transportNumber": {"type": "string", "description": "Transport request number, e.g. 'DEVK900123'."},
    },
    ["transportNumber"],
)


@tool_handler
def transport_details(args: dict) -> str:
    number = req_str(args, "transportNumber").strip().upper()
    config = cfg.resolve_config(opt_str(args, "profile"))
    with with_client(config) as session:
        request = adt.transport_details(session, number)
    return result(
        format_transport_details(request),
        transport=request.get("tm:number"),
        status=request.get("tm:status"),
        owner=request.get("tm:owner"),
        taskCount=len(request.get("tasks", [])),
        objectCount=transport_object_count(request),
    )


# abap_dumps

DUMPS_SCHEMA = schema(
    "abap_dumps",
    "List ABAP runtime dumps (short dumps) on the connected system. Optionally filter with a query string "
    "(e.g. a program name).",
    {
        "profile": PROFILE_PROPERTY,
        "query": {
            "type": "string",
            "description": "Optional filter, e.g. a program name or '*'. Defaults to all dumps.",
        },
    },
)


@tool_handler
def dumps(args: dict) -> str:
    config = cfg.resolve_config(opt_str(args, "profile"))
    with with_client(config) as session:
        feed = adt.dumps(session, opt_str(args, "query") or "")
    return result(
        format_dumps(feed),
        count=len(feed["dumps"]),
        dumps=[
            {"id": d["id"], "type": d["type"], "text": d["text"][:1000], "author": d["author"],
             "categories": [c.get("term") for c in d["categories"]]}
            for d in feed["dumps"]
        ],
    )


# abap_traces

TRACES_SCHEMA = schema(
    "abap_traces",
    "List ABAP performance traces recorded for a user. By default lists the traces of the profile's "
    "username. Use abap_trace_hitlist with a trace id to analyze a trace.",
    {
        "profile": PROFILE_PROPERTY,
        "user": {"type": "string", "description": "User to list traces for. Defaults to the connected user."},
    },
)


@tool_handler
def traces(args: dict) -> str:
    config = cfg.resolve_config(opt_str(args, "profile"))
    with with_client(config) as session:
        data = adt.traces_list(session, opt_str(args, "user") or config.username)
    return result(
        format_traces(data),
        count=len(data["runs"]),
        runs=[
            {
                "id": r["id"],
                "title": r["title"],
                "objectName": r["extendedData"].get("objectName"),
                "state": (r["extendedData"].get("state") or {}).get("text"),
                "runtime": r["extendedData"].get("runtime"),
            }
            for r in data["runs"]
        ],
    )


# abap_trace_hitlist

TRACE_HITLIST_SCHEMA = schema(
    "abap_trace_hitlist",
    "Analyze an ABAP performance trace: returns the hot-spot hit list and the aggregated statement call "
    "tree. Provide the trace id returned by abap_traces.",
    {
        "profile": PROFILE_PROPERTY,
        "traceId": {
            "type": "string",
            "description": "Trace id from abap_traces, e.g. '/sap/bc/adt/runtime/traces/abaptraces/...' or the "
            "short id.",
        },
        "withSystemEvents": {"type": "boolean", "description": "Include system events (default false)."},
    },
    ["traceId"],
)


@tool_handler
def trace_hitlist(args: dict) -> str:
    trace_id = req_str(args, "traceId").strip()
    events = bool(opt_bool(args, "withSystemEvents"))
    config = cfg.resolve_config(opt_str(args, "profile"))
    with with_client(config) as session:
        hitlist = adt.traces_hitlist(session, trace_id, events)
        statements = adt.traces_statements(session, trace_id, events)
    return result(
        format_trace_hitlist(hitlist, statements),
        hitCount=len(hitlist["entries"]),
        statementCount=statements["count"],
    )
