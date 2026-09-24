"""The 27 tools end to end, against the fake SAP system."""

from __future__ import annotations

import base64
import json

import pytest

from conftest import call, exception_xml, fixture, lock_xml, sub

config = sub("config")
client = sub("client")


class TestContract:
    def test_errors_come_back_as_json(self, dev):
        out = call("abap_read", {})
        assert out == {"error": "Provide either 'name' or 'objectUrl' to address an object."}

    def test_not_configured(self):
        assert "abap_setup" in call("abap_search", {"query": "Z*"})["error"]

    def test_unknown_object(self, dev):
        assert call("abap_read", {"name": "ZNOPE"})["error"] == 'No ABAP object found matching "ZNOPE".'

    def test_every_session_is_dropped(self, dev):
        call("abap_read", {"name": "ZCL_DEMO"})
        call("abap_search", {"query": "ZCL*"})
        assert dev.open_stateful_sessions() == 0
        # the last request of every session is the stateless drop
        assert dev.calls[-1].path == "/sap/bc/adt/compatibility/graph"


class TestConnection:
    def test_setup_saves_and_verifies(self, sap, isolated_config):
        out = call("abap_setup", {"name": "dev", "url": sap.url + "/", "username": "DEVELOPER",
                                  "password": "secret", "client": "100"})
        assert out["verified"] is True
        assert json.loads(isolated_config.read_text())["profiles"]["dev"] == {
            "url": sap.url, "username": "DEVELOPER", "password": "secret", "client": "100"}

    def test_setup_still_saves_when_the_logon_fails(self, sap):
        out = call("abap_setup", {"name": "dev", "url": sap.url, "username": "DEVELOPER", "password": "bad"})
        assert out["verified"] is False and "401" in out["result"]
        assert config.get_profile("dev") is not None

    def test_setup_requires_a_scheme(self, sap):
        assert "Include the scheme" in call("abap_setup", {"name": "d", "url": "sap:8000", "username": "u",
                                                          "password": "p"})["error"]

    def test_status_and_profile(self, dev):
        status = call("abap_status")
        assert "[active] dev: DEVELOPER@" in status["result"] and "client=100" in status["result"]
        assert "secret" not in json.dumps(status)
        assert "requires a profile name" in call("abap_profile", {"action": "use"})["error"]
        assert call("abap_profile", {"action": "delete", "name": "ghost"})["deleted"] is False

    def test_test_connection(self, dev):
        dev.on("GET", "/sap/bc/adt/core/discovery", fixture("discovery.xml"))
        out = call("abap_test_connection")
        assert out["result"].startswith(f"Connected to {dev.url} as DEVELOPER.")
        assert "http://www.sap.com/adt:core" in out["compatibilityNodes"]
        assert out["discoveryTitles"] == ["Object Repository"]


class TestBrowse:
    def test_search(self, dev):
        out = call("abap_search", {"query": "ZCL_DEMO*", "objectType": "CLAS/OC", "max": "5"})
        req = dev.last("GET", "/sap/bc/adt/repository/informationsystem/search")
        assert req.q("objectType") == "CLAS" and req.q("maxResults") == "5" and req.q("operation") == "quickSearch"
        assert out["count"] == 2
        assert "- ZCL_DEMO | type=CLAS/OC | package=ZDEMO | Demo class  (/sap/bc/adt/oo/classes/zcl_demo)" in out["result"]

    def test_old_systems_name_format_is_split(self, dev):
        out = call("abap_search", {"query": "ZDEMO_REPORT"})
        assert out["results"][0]["name"] == "ZDEMO_REPORT"
        assert out["results"][0]["description"] == "PROGRAM"

    def test_read_prefers_the_exact_name_match(self, dev):
        out = call("abap_read", {"name": "zcl_demo"})
        assert out["objectUrl"] == "/sap/bc/adt/oo/classes/zcl_demo"
        assert out["mainInclude"] == "/sap/bc/adt/oo/classes/zcl_demo/source/main"
        assert out["result"] == "CLASS zcl_demo DEFINITION.\nENDCLASS."

    def test_read_program_by_url(self, dev):
        out = call("abap_read", {"objectUrl": "/sap/bc/adt/programs/programs/zdemo_report"})
        assert out["result"] == "REPORT zdemo_report." and out["lines"] == 1

    def test_object_structure(self, dev):
        out = call("abap_object_structure", {"name": "ZCL_DEMO"})
        assert out["result"] == ("Name: ZCL_DEMO\nType: CLAS/OC\nDescription: Demo class\nResponsible: DEVELOPER\n"
                                 "Language: EN\nIncludes:\n  - definitions\n  - main\n  - testclasses")

    def test_where_used(self, dev):
        dev.on("POST", "/sap/bc/adt/repository/informationsystem/usageReferences", fixture("usage.xml"))
        out = call("abap_where_used", {"name": "ZCL_DEMO"})
        req = dev.last("POST", "/sap/bc/adt/repository/informationsystem/usageReferences")
        assert req.q("uri") == "/sap/bc/adt/oo/classes/zcl_demo/source/main"
        assert out["references"] == [
            {"name": "ZUSE_DEMO", "type": "PROG/P", "description": "Uses the demo"},
            {"name": "ABAPFULLNAME;ZCL_CALLER", "type": "CLAS/OC", "description": None},
        ]

    def test_object_types(self, dev):
        dev.on("GET", "/sap/bc/adt/repository/informationsystem/objecttypes", fixture("objecttypes.xml"))
        out = call("abap_object_types")
        assert out["count"] == 2 and out["types"][0]["usedBy"] == ["quickSearch", "objectSearch"]

    def test_node_contents(self, dev):
        dev.on("POST", "/sap/bc/adt/repository/nodestructure", fixture("nodestructure.xml"))
        out = call("abap_node_contents", {"parentType": "devc/k", "parentName": "ZDEMO"})
        req = dev.last("POST", "/sap/bc/adt/repository/nodestructure")
        assert req.q("parent_type") == "DEVC/K" and req.q("withShortDescriptions") == "true"
        assert out["result"] == "2 node(s):\n- ZCL_DEMO  [CLAS/OC] (Demo class)\n- ZDEMO_REPORT  [PROG/P]"

    def test_node_contents_rejects_other_parents(self, dev):
        assert "Invalid parentType" in call("abap_node_contents", {"parentType": "CLAS/OC"})["error"]

    def test_text_elements(self, dev):
        dev.on("GET", "/sap/bc/adt/textelements/programs/zdemo_report/source/symbols",
               "@MaxLength:20\n001=Hello\n\n002=World=Earth\n")
        out = call("abap_text_elements", {"name": "ZDEMO_REPORT"})
        assert out["textElements"] == [{"id": "001", "text": "Hello", "maxLength": 20},
                                       {"id": "002", "text": "World=Earth"}]
        assert "- 001 = Hello (max 20)" in out["result"]

    def test_text_elements_404_means_none(self, dev):
        out = call("abap_text_elements", {"name": "ZCL_DEMO", "category": "selections"})
        assert out["result"] == "No selections text elements found for zcl_demo."


class TestWriteWithoutHeldLock:
    def test_locks_writes_and_unlocks_in_one_session(self, dev):
        out = call("abap_write", {"name": "ZCL_DEMO", "source": "CLASS new.", "transport": "DEVK900123"})
        assert out["lockReleased"] is True
        put = dev.last("PUT", "/sap/bc/adt/oo/classes/zcl_demo/source/main")
        assert put.q("corrNr") == "DEVK900123" and put.headers["content-type"] == "text/plain; charset=utf-8"
        lock = dev.requests("POST", "/sap/bc/adt/oo/classes/zcl_demo")[0]
        assert lock.q("_action") == "LOCK" and lock.session == put.session
        assert dev.sources["/sap/bc/adt/oo/classes/zcl_demo/source/main"] == "CLASS new."
        assert dev.locks == {} and dev.open_stateful_sessions() == 0

    def test_unlocks_even_when_the_write_fails(self, dev):
        dev.on("PUT", "/sap/bc/adt/oo/classes/zcl_demo/source/main",
               (400, exception_xml("Syntax error in line 1", "ExceptionSyntax")))
        out = call("abap_write", {"name": "ZCL_DEMO", "source": "broken"})
        assert out["error"] == "Syntax error in line 1"
        assert dev.requests("POST", "/sap/bc/adt/oo/classes/zcl_demo")[-1].q("_action") == "UNLOCK"
        assert dev.locks == {}

    def test_foreign_lock_handle_gets_a_useful_error(self, dev):
        out = call("abap_write", {"name": "ZCL_DEMO", "source": "x", "lockHandle": "FROM_BEFORE_RESTART"})
        assert "Invalid lock handle" in out["error"] and "abap_lock" in out["error"]


class TestHeldLocks:
    """pi-abap-fs drops the session after abap_lock, which releases the lock on
    the SAP side. Here the lock survives until abap_unlock."""

    def test_lock_survives_the_tool_call(self, dev):
        out = call("abap_lock", {"name": "ZCL_DEMO"})
        assert out["lockHandle"].startswith("LOCK") and out["corrNr"] == "DEVK900123"
        assert "/sap/bc/adt/oo/classes/zcl_demo" in dev.locks
        assert dev.open_stateful_sessions() == 1

    def test_write_with_handle_uses_the_locking_session(self, dev):
        handle = call("abap_lock", {"name": "ZCL_DEMO"})["lockHandle"]
        out = call("abap_write", {"name": "ZCL_DEMO", "source": "CLASS v2.", "lockHandle": handle})
        assert out["lockReleased"] is False and "lock stays" in out["result"]
        lock_session = dev.requests("POST", "/sap/bc/adt/oo/classes/zcl_demo")[0].session
        assert dev.last("PUT", "/sap/bc/adt/oo/classes/zcl_demo/source/main").session == lock_session
        assert dev.locks["/sap/bc/adt/oo/classes/zcl_demo"][1] == handle

    def test_write_by_name_finds_the_held_lock_without_a_handle(self, dev):
        call("abap_lock", {"objectUrl": "/sap/bc/adt/oo/classes/zcl_demo"})
        out = call("abap_write", {"name": "ZCL_DEMO", "source": "CLASS v3."})
        assert out["lockReleased"] is False
        # no second LOCK: it would have been a conflict with our own session
        assert [r.q("_action") for r in dev.requests("POST", "/sap/bc/adt/oo/classes/zcl_demo")] == ["LOCK"]

    def test_activate_runs_inside_the_held_lock(self, dev):
        dev.on("POST", "/sap/bc/adt/activation", "")
        call("abap_lock", {"name": "ZCL_DEMO"})
        out = call("abap_activate", {"name": "ZCL_DEMO"})
        assert out["success"] is True
        lock_session = dev.requests("POST", "/sap/bc/adt/oo/classes/zcl_demo")[0].session
        assert dev.last("POST", "/sap/bc/adt/activation").session == lock_session
        assert dev.locks  # still held

    def test_lock_twice_returns_the_same_handle(self, dev):
        first = call("abap_lock", {"name": "ZCL_DEMO"})
        second = call("abap_lock", {"name": "zcl_demo"})
        assert second["alreadyHeld"] is True and second["lockHandle"] == first["lockHandle"]

    def test_unlock_releases_and_closes_the_session(self, dev):
        handle = call("abap_lock", {"name": "ZCL_DEMO"})["lockHandle"]
        out = call("abap_unlock", {"lockHandle": handle})
        assert out["result"] == "Unlocked ZCL_DEMO (/sap/bc/adt/oo/classes/zcl_demo)."
        assert dev.locks == {} and dev.open_stateful_sessions() == 0
        assert client.LOCKS.all() == []

    def test_status_lists_held_locks(self, dev):
        handle = call("abap_lock", {"name": "ZCL_DEMO"})["lockHandle"]
        assert f"lockHandle={handle}" in call("abap_status")["result"]

    def test_idle_locks_expire(self, dev):
        call("abap_lock", {"name": "ZCL_DEMO"})
        expired = client.LOCKS.expire(now=10**12)
        assert len(expired) == 1 and dev.locks == {} and dev.open_stateful_sessions() == 0

    def test_ended_server_session_is_reported(self, dev):
        handle = call("abap_lock", {"name": "ZCL_DEMO"})["lockHandle"]
        dev.sessions.clear()  # SAP restarted / session timed out
        dev.locks.clear()
        out = call("abap_write", {"name": "ZCL_DEMO", "source": "x", "lockHandle": handle})
        assert "has ended on the SAP side" in out["error"]
        assert client.LOCKS.all() == []

    def test_lock_conflict_with_someone_else(self, dev):
        dev.locks["/sap/bc/adt/oo/classes/zcl_demo"] = ("OTHER", "THEIRS")
        out = call("abap_lock", {"name": "ZCL_DEMO"})
        assert "locked by another session" in out["error"]
        assert client.LOCKS.all() == [] and dev.open_stateful_sessions() == 0


class TestActivate:
    def test_request_shape(self, dev):
        dev.on("POST", "/sap/bc/adt/activation", "")
        out = call("abap_activate", {"name": "ZCL_DEMO"})
        req = dev.last("POST", "/sap/bc/adt/activation")
        assert req.q("method") == "activate" and req.q("preauditRequested") == "true"
        assert ('adtcore:uri="/sap/bc/adt/oo/classes/zcl_demo?context=%2Fsap%2Fbc%2Fadt%2Foo%2Fclasses%2Fzcl_demo'
                '%2Fsource%2Fmain" adtcore:name="ZCL_DEMO"') in req.body
        assert out["result"] == "Activation successful."

    def test_error_messages_fail_the_activation(self, dev):
        dev.on("POST", "/sap/bc/adt/activation", fixture("activation_errors.xml"))
        out = call("abap_activate", {"name": "ZCL_DEMO"})
        assert out["success"] is False
        assert '  - [E] The field "LV_X" is unknown. (line 12)' in out["result"]

    def test_inactive_dependents_fail_the_activation(self, dev):
        dev.on("POST", "/sap/bc/adt/activation", fixture("activation_inactive.xml"))
        out = call("abap_activate", {"name": "ZCL_DEMO"})
        assert out["success"] is False
        assert "  - ZCL_OTHER (CLAS/OC) by OTHERDEV" in out["result"]


class TestChecks:
    def test_syntax_check_sends_the_source_base64(self, dev):
        dev.on("POST", "/sap/bc/adt/checkruns", fixture("checkrun.xml"))
        out = call("abap_syntax_check", {"name": "ZCL_DEMO", "source": "CLASS x.äö"})
        req = dev.last("POST", "/sap/bc/adt/checkruns")
        assert req.q("reporters") == "abapCheckRun"
        assert base64.b64encode("CLASS x.äö".encode()).decode() in req.body
        assert out["messages"][0] == {"uri": "/sap/bc/adt/oo/classes/zcl_demo/source/main", "line": 12,
                                      "offset": 4, "severity": "E", "text": 'The field "LV_X" is unknown.'}
        assert out["result"].startswith("2 message(s):\n- [E] line 12:")

    def test_syntax_check_of_the_saved_source(self, dev):
        dev.on("POST", "/sap/bc/adt/checkruns", '<chkrun:checkRunReports xmlns:chkrun="http://www.sap.com/adt/checkrun"/>')
        out = call("abap_syntax_check", {"name": "ZCL_DEMO"})
        assert out["result"] == "No syntax errors found."
        encoded = base64.b64encode(b"CLASS zcl_demo DEFINITION.\nENDCLASS.").decode()
        assert encoded in dev.last("POST", "/sap/bc/adt/checkruns").body

    def test_unit_tests(self, dev):
        dev.on("POST", "/sap/bc/adt/abapunit/testruns", fixture("unittest.xml"))
        out = call("abap_unit_test", {"name": "ZCL_DEMO", "medium": True})
        body = dev.last("POST", "/sap/bc/adt/abapunit/testruns").body
        assert 'harmless="true" dangerous="false" critical="false"' in body
        assert 'short="true" medium="true" long="false"' in body
        assert out["result"].startswith("2 test method(s), 1 failure(s).")
        assert "  - TEST_FAIL: FAILED (critical)" in out["result"]
        assert "      Expected [4] Actual [5]\n      \tTest 'LTC_DEMO->TEST_FAIL'" in out["result"]

    def test_atc(self, dev):
        dev.on("POST", "/sap/bc/adt/atc/runs", fixture("atc_run.xml"))
        dev.on("GET", "/sap/bc/adt/atc/worklists/0242AC1100021EEF9BA1B0E1C4D5E6F7", fixture("atc_worklist.xml"))
        out = call("abap_atc", {"name": "ZCL_DEMO", "variant": "MY_VARIANT"})
        assert dev.last("POST", "/sap/bc/adt/atc/runs").q("worklistId") == "MY_VARIANT"
        assert out["findingCount"] == 1
        assert out["findings"][0]["object"] == "ZCL_DEMO" and out["findings"][0]["priority"] == 2
        assert ("- [P2] ZCL_DEMO (CLAS): Extended Program Check (SLIN) — Variable LV_X is not used line 12"
                in out["result"])


class TestData:
    def test_query(self, dev):
        dev.on("POST", "/sap/bc/adt/datapreview/freestyle", fixture("table_data.xml"))
        out = call("abap_query", {"sql": "SELECT * FROM ymu_rap_abook", "rowNumber": 3})
        req = dev.last("POST", "/sap/bc/adt/datapreview/freestyle")
        assert req.q("rowNumber") == "3" and req.body == "SELECT * FROM ymu_rap_abook"
        assert out["rowCount"] == 3 and len(out["columns"]) == 14
        header, rule, first = out["result"].split("\n")[:3]
        assert header.startswith("CLIENT | BOOKING_UUID")
        cells = [c.strip() for c in first.split("|")]
        assert cells[3] == "5"             # BOOKING_ID "0005", type N, decoded to a number
        assert cells[4] == "2021-05-24"    # BOOKING_DATE "20210524", type D

    def test_table_with_filter(self, dev):
        dev.on("POST", "/sap/bc/adt/datapreview/ddic", fixture("table_data.xml"))
        out = call("abap_table", {"table": "sflight", "filter": "CARRID = 'LH'"})
        req = dev.last("POST", "/sap/bc/adt/datapreview/ddic")
        assert req.q("ddicEntityName") == "sflight" and req.body == "WHERE CARRID = 'LH'"
        assert out["table"] == "SFLIGHT"


class TestOperations:
    def test_transports(self, dev):
        dev.on("GET", "/sap/bc/adt/cts/transportrequests", fixture("transports.xml"))
        out = call("abap_transports")
        req = dev.last("GET", "/sap/bc/adt/cts/transportrequests")
        assert req.q("user") == "DEVELOPER" and req.q("targets") == "true"
        assert out["result"] == (
            "Workbench target LOCAL (Local change requests):\n"
            "  - DEVK900123 [D] Demo change (owner=DEVELOPER)\n"
            "  - DEVK900100 [R] Old change (owner=DEVELOPER)"
        )

    def test_transport_details(self, dev):
        dev.on("GET", "/sap/bc/adt/cts/transportrequests/DEVK900123", fixture("transport_details.xml"))
        out = call("abap_transport_details", {"transportNumber": "devk900123"})
        assert dev.last("GET", "/sap/bc/adt/cts/transportrequests/DEVK900123").headers["Accept"] == \
            "application/vnd.sap.adt.transportorganizer.v1+xml"
        assert out["taskCount"] == 1 and out["objectCount"] == 3
        assert "Objects:\n  - ZDEMO (DEVC)\nTasks:\n  DEVK900124: Demo change\n    - ZCL_DEMO (CLAS)" in out["result"]

    def test_dumps(self, dev):
        dev.on("GET", "/sap/bc/adt/runtime/dumps", fixture("dumps.xml"))
        out = call("abap_dumps", {"query": "ZDEMO_REPORT"})
        assert dev.last("GET", "/sap/bc/adt/runtime/dumps").q("$query") == "ZDEMO_REPORT"
        assert out["dumps"][0]["categories"] == ["COMPUTE_INT_ZERODIVIDE", "ZDEMO_REPORT"]
        assert "- COMPUTE_INT_ZERODIVIDE [/sap/bc/adt/runtime/dump/" in out["result"]
        assert "Division by zero & more in ZDEMO_REPORT" in out["result"]

    def test_traces_and_hitlist(self, dev):
        dev.on("GET", "/sap/bc/adt/runtime/traces/abaptraces", fixture("traces.xml"))
        runs = call("abap_traces", {"user": "murbani"})
        assert dev.last("GET", "/sap/bc/adt/runtime/traces/abaptraces").q("user") == "MURBANI"
        trace_id = runs["runs"][0]["id"]
        assert runs["runs"][0]["objectName"] == "YMUHIERTABPERFTEST" and runs["runs"][0]["state"] == "Finished"
        assert "- DEFAULT (YMUHIERTABPERFTEST) [Finished] 5531427s — id=" in runs["result"]

        dev.on("GET", f"{trace_id}/hitlist", fixture("trace_hitlist.xml"))
        dev.on("GET", f"{trace_id}/statements", fixture("trace_statements.xml"))
        out = call("abap_trace_hitlist", {"traceId": trace_id})
        assert out["hitCount"] == 2 and out["statementCount"] == 43
        lines = out["result"].split("\n")
        assert lines[0] == "Statements (aggregated call tree):"
        hits = lines[lines.index("Hit list (hot spots):") + 1:]
        assert hits[0].startswith("  - Runtime Analysis On  — hits=1, gross=4822ms (95.7696%)")

    def test_short_trace_id_is_expanded(self, dev):
        dev.on("GET", "/sap/bc/adt/runtime/traces/abaptraces/abc/hitlist",
               '<trc:hitlist xmlns:trc="http://www.sap.com/adt/runtime/traces/abaptraces"/>')
        dev.on("GET", "/sap/bc/adt/runtime/traces/abaptraces/abc/statements",
               '<trc:statements xmlns:trc="http://www.sap.com/adt/runtime/traces/abaptraces" count="0"/>')
        out = call("abap_trace_hitlist", {"traceId": "abc"})
        assert out["result"] == "Trace has no statements or hit list entries."


class TestTextElementsAndCreate:
    def test_set_text_elements_locks_writes_unlocks(self, dev):
        out = call("abap_set_text_elements", {
            "name": "ZDEMO_REPORT", "category": "symbols", "transport": "DEVK900123",
            "elements": [{"id": "001", "text": "Hello", "maxLength": 20}, {"id": "002", "text": "World"}],
        })
        assert out["count"] == 2
        put = dev.last("PUT", "/sap/bc/adt/textelements/programs/zdemo_report/source/symbols")
        assert put.body == "@MaxLength:20\n001=Hello\n\n002=World\n"
        assert put.headers["Content-Type"] == "application/vnd.sap.adt.textelements.symbols.v1; charset=UTF-8"
        assert dev.locks == {}

    def test_set_text_elements_validates_before_locking(self, dev):
        out = call("abap_set_text_elements", {"name": "ZDEMO_REPORT", "category": "symbols",
                                              "elements": [{"id": "1", "text": "x"}]})
        assert out["error"] == 'Symbol key "1" must be exactly 3 characters'
        assert dev.requests("POST", "/sap/bc/adt/programs/programs/zdemo_report") == []

    def test_create_class(self, dev):
        dev.on("POST", "/sap/bc/adt/oo/classes", "")
        out = call("abap_create_object", {"objectType": "clas/oc", "name": "ZCL_NEW", "parentName": "$TMP",
                                          "description": "New & shiny"})
        req = dev.last("POST", "/sap/bc/adt/oo/classes")
        assert 'adtcore:description="New &amp; shiny"' in req.body
        assert 'adtcore:responsible="DEVELOPER"' in req.body and '<adtcore:packageRef adtcore:name="$TMP"/>' in req.body
        assert req.q("corrNr") is None
        assert out["result"].startswith('Created CLAS/OC "ZCL_NEW" in $TMP.')

    def test_create_function_module_goes_under_its_group(self, dev):
        dev.on("POST", "/sap/bc/adt/functions/groups/zfg_demo/fmodules", "")
        call("abap_create_object", {"objectType": "FUGR/FF", "name": "Z_FM", "parentName": "ZFG_DEMO",
                                    "description": "FM", "transport": "DEVK900123"})
        req = dev.last("POST", "/sap/bc/adt/functions/groups/zfg_demo/fmodules")
        assert 'adtcore:uri="/sap/bc/adt/functions/groups/zfg_demo"' in req.body
        assert req.q("corrNr") == "DEVK900123"

    def test_create_rejects_unknown_and_package_types(self, dev):
        assert "Unsupported object type" in call("abap_create_object", {
            "objectType": "XYZ", "name": "A", "parentName": "B", "description": "C"})["error"]
        assert "Create packages in SE80" in call("abap_create_object", {
            "objectType": "DEVC/K", "name": "ZP", "parentName": "ZSUPER", "description": "C"})["error"]
