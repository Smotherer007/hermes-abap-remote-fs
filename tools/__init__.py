"""Tool registry: name -> (schema, handler), in pi-abap-fs's registration order."""

from __future__ import annotations

from . import browse, connection, data, edit, operations, quality

TOOLSET = "abap"

TOOLS = (
    (connection.SETUP_SCHEMA, connection.setup),
    (connection.STATUS_SCHEMA, connection.status),
    (connection.PROFILE_SCHEMA, connection.profile),
    (connection.TEST_CONNECTION_SCHEMA, connection.test_connection),
    (browse.SEARCH_SCHEMA, browse.search),
    (browse.OBJECT_STRUCTURE_SCHEMA, browse.object_structure),
    (browse.READ_SCHEMA, browse.read),
    (edit.LOCK_SCHEMA, edit.lock),
    (edit.UNLOCK_SCHEMA, edit.unlock),
    (edit.WRITE_SCHEMA, edit.write),
    (edit.ACTIVATE_SCHEMA, edit.activate),
    (quality.SYNTAX_CHECK_SCHEMA, quality.syntax_check),
    (quality.UNIT_TEST_SCHEMA, quality.unit_test),
    (data.QUERY_SCHEMA, data.query),
    (data.TABLE_SCHEMA, data.table),
    (browse.WHERE_USED_SCHEMA, browse.where_used),
    (browse.OBJECT_TYPES_SCHEMA, browse.object_types),
    (browse.NODE_CONTENTS_SCHEMA, browse.node_contents),
    (operations.TRANSPORTS_SCHEMA, operations.transports),
    (quality.ATC_SCHEMA, quality.atc),
    (operations.DUMPS_SCHEMA, operations.dumps),
    (operations.TRACES_SCHEMA, operations.traces),
    (operations.TRACE_HITLIST_SCHEMA, operations.trace_hitlist),
    (operations.TRANSPORT_DETAILS_SCHEMA, operations.transport_details),
    (browse.TEXT_ELEMENTS_SCHEMA, browse.text_elements),
    (edit.SET_TEXT_ELEMENTS_SCHEMA, edit.set_text_elements),
    (edit.CREATE_OBJECT_SCHEMA, edit.create_object),
)

#: Tools that change repository objects. The safety level applies to them.
WRITE_TOOLS = frozenset({
    "abap_lock",
    "abap_write",
    "abap_set_text_elements",
    "abap_create_object",
})

#: Activation has its own setting, because "a human activates" is a common rule.
ACTIVATE_TOOL = "abap_activate"
