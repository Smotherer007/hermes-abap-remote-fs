"""Search and read tools: abap_search, abap_object_types, abap_node_contents,
abap_object_structure, abap_read, abap_where_used, abap_text_elements."""

from __future__ import annotations

from .. import adt
from .. import config as cfg
from ..client import resolve_object, with_client
from ..formatters import (
    format_nodes,
    format_object_structure,
    format_object_types,
    format_search_results,
    format_text_elements,
    format_usage_references,
)
from ..models import ToolInputError
from ._base import (
    OBJECT_PROPERTIES,
    PROFILE_PROPERTY,
    object_args,
    opt_int,
    opt_str,
    req_str,
    result,
    schema,
    tool_handler,
)

# abap_search

SEARCH_SCHEMA = schema(
    "abap_search",
    "Search ABAP objects by name pattern (supports wildcards on most systems). Returns object name, type, "
    "package and object URL for each match. Use the returned object URL with abap_read, "
    "abap_object_structure, etc.",
    {
        "profile": PROFILE_PROPERTY,
        "query": {"type": "string", "description": "Search pattern, e.g. 'ZCL_MY_CLASS' or 'Z*PRICING*'"},
        "objectType": {
            "type": "string",
            "description": "Optional object type filter. The first part is used, e.g. 'CLAS', 'PROG', 'FUGR', "
            "'INTF', 'TABL'. Use abap_object_types to see available types.",
        },
        "max": {"type": "number", "description": "Maximum number of results (default 100)."},
    },
    ["query"],
)


@tool_handler
def search(args: dict) -> str:
    config = cfg.resolve_config(opt_str(args, "profile"))
    query = req_str(args, "query")
    max_results = opt_int(args, "max") or 100
    with with_client(config) as session:
        results = adt.search_object(session, query, opt_str(args, "objectType"), max_results)
    return result(
        format_search_results(results, query),
        count=len(results),
        query=query,
        results=[
            {
                "name": r.get("adtcore:name"),
                "type": r.get("adtcore:type"),
                "package": r.get("adtcore:packageName"),
                "description": r.get("adtcore:description"),
                "objectUrl": r.get("adtcore:uri"),
            }
            for r in results
        ],
    )


# abap_object_types

OBJECT_TYPES_SCHEMA = schema(
    "abap_object_types",
    "List the object types available for searching on the connected system. Useful to discover the correct "
    "type hint for abap_search (e.g. CLAS, PROG, FUGR, INTF).",
    {"profile": PROFILE_PROPERTY},
)


@tool_handler
def object_types(args: dict) -> str:
    config = cfg.resolve_config(opt_str(args, "profile"))
    with with_client(config) as session:
        types = adt.object_types(session)
    return result(format_object_types(types), count=len(types), types=types)


# abap_node_contents

NODE_CONTENTS_SCHEMA = schema(
    "abap_node_contents",
    "Browse the contents of an SAP package (or program / function group) to discover the objects inside it. "
    "parentType is usually 'DEVC/K' for packages; pass the package name as parentName. Use '$TMP' for local "
    "objects.",
    {
        "profile": PROFILE_PROPERTY,
        "parentType": {
            "type": "string",
            "enum": list(adt.NODE_PARENTS),
            "description": "Parent object type: DEVC/K (package), PROG/P (program), FUGR/F (function group), "
            "PROG/PI (include).",
        },
        "parentName": {
            "type": "string",
            "description": "Parent object name, e.g. 'Z_MY_PACKAGE' or '$TMP'. Omit to list the top level.",
        },
    },
    ["parentType"],
)


@tool_handler
def node_contents(args: dict) -> str:
    parent_type = req_str(args, "parentType").upper()
    if parent_type not in adt.NODE_PARENTS:
        raise ToolInputError(f'Invalid parentType "{parent_type}". Use one of: {", ".join(adt.NODE_PARENTS)}.')
    config = cfg.resolve_config(opt_str(args, "profile"))
    with with_client(config) as session:
        structure = adt.node_contents(session, parent_type, opt_str(args, "parentName"))
    return result(
        format_nodes(structure),
        nodeCount=len(structure["nodes"]),
        nodes=[
            {"name": n.get("OBJECT_NAME"), "type": n.get("OBJECT_TYPE"),
             "description": n.get("DESCRIPTION"), "uri": n.get("OBJECT_URI")}
            for n in structure["nodes"]
        ],
    )


# abap_object_structure

OBJECT_STRUCTURE_SCHEMA = schema(
    "abap_object_structure",
    "Get metadata (type, description, responsible, language) and include list for an ABAP object, plus the "
    "main source URL. Provide either a name (with optional type hint) or an object URL.",
    {"profile": PROFILE_PROPERTY, **OBJECT_PROPERTIES},
)


@tool_handler
def object_structure(args: dict) -> str:
    target = object_args(args)
    config = cfg.resolve_config(opt_str(args, "profile"))
    with with_client(config) as session:
        resolved = resolve_object(session, **target)
    return result(
        format_object_structure(resolved.structure),
        name=resolved.name,
        type=resolved.type,
        objectUrl=resolved.object_url,
        mainInclude=resolved.main_include,
    )


# abap_read

READ_SCHEMA = schema(
    "abap_read",
    "Read the main source code of an ABAP object (class, program, function group, interface, …). Provide "
    "either a name (with optional type hint) or an object URL. Returns the full source as text.",
    {"profile": PROFILE_PROPERTY, **OBJECT_PROPERTIES},
)


@tool_handler
def read(args: dict) -> str:
    target = object_args(args)
    config = cfg.resolve_config(opt_str(args, "profile"))
    with with_client(config) as session:
        resolved = resolve_object(session, **target)
        source = adt.get_object_source(session, resolved.main_include)
    return result(
        source,
        name=resolved.name,
        type=resolved.type,
        objectUrl=resolved.object_url,
        mainInclude=resolved.main_include,
        lines=len(source.split("\n")),
    )


# abap_where_used

WHERE_USED_SCHEMA = schema(
    "abap_where_used",
    "Find where an ABAP object is referenced (where-used list). Provide the object name or URL. Returns the "
    "list of referencing objects.",
    {"profile": PROFILE_PROPERTY, **OBJECT_PROPERTIES},
)


@tool_handler
def where_used(args: dict) -> str:
    target = object_args(args)
    config = cfg.resolve_config(opt_str(args, "profile"))
    with with_client(config) as session:
        resolved = resolve_object(session, **target)
        references = adt.usage_references(session, resolved.main_include)
    return result(
        format_usage_references(references),
        name=resolved.name,
        count=len(references),
        references=[
            {"name": r.get("adtcore:name") or r.get("objectIdentifier"), "type": r.get("adtcore:type"),
             "description": r.get("adtcore:description")}
            for r in references
        ],
    )


# abap_text_elements

TEXT_ELEMENTS_SCHEMA = schema(
    "abap_text_elements",
    "Read the text elements of an ABAP object: symbols (text symbols), selections (selection texts) or "
    "headings. Provide the object (name or objectUrl).",
    {
        "profile": PROFILE_PROPERTY,
        **OBJECT_PROPERTIES,
        "name": {"type": "string", "description": "Object name, e.g. 'ZCL_MY_CLASS' or a program."},
        "category": {
            "type": "string",
            "enum": list(adt.TEXT_CATEGORIES),
            "description": "Text element category: symbols, selections or headings. Default 'symbols'.",
        },
    },
)


@tool_handler
def text_elements(args: dict) -> str:
    target = object_args(args)
    category = (opt_str(args, "category") or "symbols").lower()
    if category not in adt.TEXT_CATEGORIES:
        raise ToolInputError(f'Invalid category "{category}". Use symbols, selections or headings.')
    config = cfg.resolve_config(opt_str(args, "profile"))
    with with_client(config) as session:
        resolved = resolve_object(session, **target)
        url = adt.text_elements_url(resolved.type, resolved.name)
        data = adt.get_text_elements(session, url, category)
    return result(
        format_text_elements(data, category),
        name=resolved.name,
        category=category,
        count=len(data["textElements"]),
        textElements=data["textElements"],
    )
