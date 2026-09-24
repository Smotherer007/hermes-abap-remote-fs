"""Data tools: abap_query, abap_table.

Both go through ADT's data preview, which only runs SELECTs.
"""

from __future__ import annotations

from .. import adt
from .. import config as cfg
from ..client import with_client
from ..formatters import format_query_result
from ._base import PROFILE_PROPERTY, opt_int, opt_str, req_str, result, schema, tool_handler

# abap_query

QUERY_SCHEMA = schema(
    "abap_query",
    "Run an ABAP SQL SELECT query on the connected SAP system (freestyle data preview). Returns rows as text. "
    "Example: SELECT * FROM sflight UP TO 10 ROWS. Use with care on production systems.",
    {
        "profile": PROFILE_PROPERTY,
        "sql": {
            "type": "string",
            "description": "ABAP SQL SELECT statement, e.g. 'SELECT carrid, connid, fldate FROM sflight UP TO 20 ROWS'.",
        },
        "rowNumber": {"type": "number", "description": "Maximum rows to return (default 100)."},
    },
    ["sql"],
)


@tool_handler
def query(args: dict) -> str:
    sql = req_str(args, "sql")
    config = cfg.resolve_config(opt_str(args, "profile"))
    with with_client(config) as session:
        data = adt.run_query(session, sql, opt_int(args, "rowNumber") or 100)
    return result(
        format_query_result(data),
        rowCount=len(data["values"]),
        columns=[c["name"] for c in data["columns"]],
    )


# abap_table

TABLE_SCHEMA = schema(
    "abap_table",
    "Preview the contents of a DDIC table or view. Returns the first N rows. Optionally provide a WHERE "
    "condition via the filter parameter.",
    {
        "profile": PROFILE_PROPERTY,
        "table": {"type": "string", "description": "DDIC table or view name, e.g. 'SFLIGHT' or 'MARA'."},
        "rowNumber": {"type": "number", "description": "Maximum rows to return (default 100)."},
        "filter": {
            "type": "string",
            "description": "Optional SQL WHERE condition (without the WHERE keyword), e.g. \"CARRID = 'LH'\".",
        },
    },
    ["table"],
)


@tool_handler
def table(args: dict) -> str:
    name = req_str(args, "table").strip()
    condition = opt_str(args, "filter")
    config = cfg.resolve_config(opt_str(args, "profile"))
    with with_client(config) as session:
        data = adt.table_contents(
            session, name, opt_int(args, "rowNumber") or 100, f"WHERE {condition}" if condition else ""
        )
    return result(
        format_query_result(data),
        table=name.upper(),
        rowCount=len(data["values"]),
        columns=[c["name"] for c in data["columns"]],
    )
