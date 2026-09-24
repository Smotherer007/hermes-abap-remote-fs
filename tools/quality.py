"""Check and test tools: abap_syntax_check, abap_unit_test, abap_atc."""

from __future__ import annotations

from .. import adt
from .. import config as cfg
from ..client import resolve_object, with_client
from ..formatters import atc_findings, format_atc_result, format_syntax_check, format_unit_tests
from ._base import (
    OBJECT_PROPERTIES,
    PROFILE_PROPERTY,
    object_args,
    opt_bool,
    opt_int,
    opt_str,
    result,
    schema,
    tool_handler,
)

# abap_syntax_check

SYNTAX_CHECK_SCHEMA = schema(
    "abap_syntax_check",
    "Run a syntax check on source code. Provide the object (name or objectUrl) and the source to check "
    "(for unsaved content), or omit source to check the object's currently saved main include.",
    {
        "profile": PROFILE_PROPERTY,
        **OBJECT_PROPERTIES,
        "source": {
            "type": "string",
            "description": "Source to check. When omitted, the currently saved main include is checked.",
        },
    },
)


@tool_handler
def syntax_check(args: dict) -> str:
    target = object_args(args)
    source = opt_str(args, "source")
    config = cfg.resolve_config(opt_str(args, "profile"))
    with with_client(config) as session:
        resolved = resolve_object(session, **target)
        content = source if source is not None else adt.get_object_source(session, resolved.main_include)
        messages = adt.syntax_check(session, resolved.main_include, resolved.main_include, content)
    return result(
        format_syntax_check(messages),
        name=resolved.name,
        messageCount=len(messages),
        messages=messages,
    )


# abap_unit_test

UNIT_TEST_SCHEMA = schema(
    "abap_unit_test",
    "Run ABAP unit tests for a class (or program). Provide the object name or URL. By default only short, "
    "harmless tests run; enable dangerous/critical or medium/long durations as needed.",
    {
        "profile": PROFILE_PROPERTY,
        **OBJECT_PROPERTIES,
        "name": {"type": "string", "description": "Class name, e.g. 'ZCL_MY_CLASS'"},
        "dangerous": {"type": "boolean", "description": "Include dangerous tests (default false)."},
        "critical": {"type": "boolean", "description": "Include critical tests (default false)."},
        "medium": {"type": "boolean", "description": "Include medium-duration tests (default false)."},
        "long": {"type": "boolean", "description": "Include long-duration tests (default false)."},
    },
)


@tool_handler
def unit_test(args: dict) -> str:
    target = object_args(args)
    flags = {
        "harmless": True,
        "dangerous": bool(opt_bool(args, "dangerous")),
        "critical": bool(opt_bool(args, "critical")),
        "short": True,
        "medium": bool(opt_bool(args, "medium")),
        "long": bool(opt_bool(args, "long")),
    }
    config = cfg.resolve_config(opt_str(args, "profile"))
    with with_client(config) as session:
        resolved = resolve_object(session, **target)
        classes = adt.unit_test_run(session, resolved.object_url, flags)
    return result(format_unit_tests(classes), name=resolved.name, testClasses=classes)


# abap_atc

ATC_SCHEMA = schema(
    "abap_atc",
    "Run ABAP Test Cockpit (ATC) analysis on an object and return its findings. Provide the object (name or "
    "objectUrl) and optionally a check variant name (default 'DEFAULT'). Use abap_object_types or a known "
    "variant name if the default is unavailable.",
    {
        "profile": PROFILE_PROPERTY,
        **OBJECT_PROPERTIES,
        "variant": {"type": "string", "description": "ATC check variant / worklist id (default 'DEFAULT')."},
        "maxResults": {"type": "number", "description": "Maximum findings to collect (default 100)."},
    },
)


@tool_handler
def atc(args: dict) -> str:
    target = object_args(args)
    variant = opt_str(args, "variant") or "DEFAULT"
    max_results = opt_int(args, "maxResults") or 100
    config = cfg.resolve_config(opt_str(args, "profile"))
    with with_client(config) as session:
        resolved = resolve_object(session, **target)
        run = adt.create_atc_run(session, variant, resolved.main_include, max_results)
        worklist = adt.atc_worklist(session, run["id"])
    findings = atc_findings(worklist)
    return result(
        format_atc_result(worklist),
        name=resolved.name,
        findingCount=len(findings),
        findings=[
            {
                "object": f.get("object"),
                "objectType": f.get("objectType"),
                "checkId": f.get("checkId"),
                "checkTitle": f.get("checkTitle"),
                "messageId": f.get("messageId"),
                "messageTitle": f.get("messageTitle"),
                "priority": f.get("priority"),
            }
            for f in findings
        ],
    )

