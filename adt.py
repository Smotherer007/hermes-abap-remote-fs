"""ADT operations.

Each function is a port of the matching abap-adt-api function that
pi-abap-fs calls: same endpoint, method, query, headers and body, and a
result dict in the same shape (``"adtcore:name"``, ``"tm:number"``, ...).
The source file in abap-adt-api is named in each section so a change there
is easy to follow.
"""

from __future__ import annotations

import base64
import re
from typing import Any, Dict, List, Optional, Sequence

from . import xmlutil as X
from .adt_http import AdtSession
from .models import AdtError

# Validation (AdtException.ts)

_OBJECT_URL = re.compile(r"^/sap/bc/adt/[a-z]+/[a-zA-Z%$]?[\w%]+")


def validate_object_url(url: str) -> None:
    if not _OBJECT_URL.match(url or ""):
        raise AdtError(f"Invalid Object URL:{url}", kind="BADOBJECTURL")


def validate_stateful(h: AdtSession) -> None:
    if not h.stateful:
        raise AdtError("This operation can only be performed in stateful mode", kind="STATELESS")


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


# Discovery (api/discovery.ts)


def compatibility_graph(h: AdtSession) -> Dict[str, Any]:
    raw = X.parse(h.request("/sap/bc/adt/compatibility/graph").body)
    nodes = [X.attrs(n) for n in X.array(raw, "compatibility:graph", "nodes", "node")]
    edges = [
        {"sourceNode": X.attrs(e.get("sourceNode")), "targetNode": X.attrs(e.get("targetNode"))}
        for e in X.array(raw, "compatibility:graph", "edges", "edge")
        if isinstance(e, dict)
    ]
    return {"nodes": nodes, "edges": edges}


def core_discovery(h: AdtSession) -> List[Dict[str, Any]]:
    raw = X.parse(h.request("/sap/bc/adt/core/discovery").body)
    result = []
    for workspace in X.array(raw, "app:service", "app:workspace"):
        collection = workspace.get("app:collection") if isinstance(workspace, dict) else None
        if not collection:
            continue
        first = collection[0] if isinstance(collection, list) else collection
        result.append({
            "collection": {
                "category": X.attrs(X.node(first, "atom:category")).get("term", ""),
                "href": X.attrs(first).get("href", ""),
                "title": X.text(X.node(first, "atom:title")),
            },
            "title": X.text(workspace.get("atom:title")),
        })
    return result


def object_types(h: AdtSession) -> List[Dict[str, Any]]:
    response = h.request(
        "/sap/bc/adt/repository/informationsystem/objecttypes",
        qs={"maxItemCount": 999, "name": "*", "data": "usedByProvider"},
    )
    raw = X.parse(response.body)
    types = []
    for item in X.array(raw, "nameditem:namedItemList", "nameditem:namedItem"):
        data = X.text(X.node(item, "nameditem:data"))
        fields: Dict[str, str] = {}
        for part in data.split(";"):
            key, _, value = part.partition(":")
            fields[key] = value
        if fields.get("type") and fields.get("usedBy"):
            types.append({
                "name": X.text(X.node(item, "nameditem:name")),
                "description": X.text(X.node(item, "nameditem:description")),
                "type": fields["type"],
                "usedBy": fields["usedBy"].split(","),
            })
    return types


# Search (api/search.ts)


def search_object(h: AdtSession, query: str, obj_type: Optional[str] = None, max_results: int = 100):
    qs: Dict[str, Any] = {"operation": "quickSearch", "query": query, "maxResults": max_results}
    if obj_type:
        qs["objectType"] = re.sub(r"/.*$", "", obj_type)
    response = h.request(
        "/sap/bc/adt/repository/informationsystem/search", qs=qs, headers={"Accept": "application/*"}
    )
    raw = X.parse(response.body)
    results = []
    for ref in X.array(raw, "adtcore:objectReferences", "adtcore:objectReference"):
        result = X.attrs(ref)
        # older systems return things like "ZREPORT (PROGRAM)"
        match = re.match(r"([^\s]*)\s*\((.*)\)", result.get("adtcore:name", ""))
        if match:
            result["adtcore:name"] = match.group(1)
            if not result.get("adtcore:description"):
                result["adtcore:description"] = match.group(2)
        results.append(result)
    return results


# Object structure (api/objectstructure.ts)


def is_class_structure(structure: Dict[str, Any]) -> bool:
    return structure.get("metaData", {}).get("class:visibility") is not None


def object_structure(h: AdtSession, object_url: str) -> Dict[str, Any]:
    validate_object_url(object_url)
    raw = X.parse(h.request(object_url).body)
    root = X.root(raw)
    meta = X.attrs(root)
    links = [X.attrs(link) for link in X.array(root, "atom:link")]
    structure: Dict[str, Any] = {"objectUrl": object_url, "metaData": meta, "links": links}
    if meta.get("class:visibility") is not None:
        structure["includes"] = [
            {**X.attrs(inc), "links": [X.attrs(link) for link in X.array(inc, "atom:link")]}
            for inc in X.array(root, "class:include")
        ]
    return structure


def main_include(structure: Dict[str, Any], with_default: bool = True) -> str:
    """ADTClient.mainInclude: where the main source of an object lives."""
    object_url = structure["objectUrl"]
    meta = structure.get("metaData", {})
    if meta.get("adtcore:type") == "DEVC/K":
        return object_url
    if is_class_structure(structure):
        main = next((i for i in structure.get("includes", []) if i.get("class:includeType") == "main"), None)
        if main:
            links = main.get("links", [])
            link = next((l for l in links if l.get("type") == "text/plain"), None) or next(
                (l for l in links if not l.get("type")), None
            )
            if link and link.get("href"):
                return X.follow_url(object_url, link["href"])
    else:
        source = meta.get("abapsource:sourceUri")
        if source:
            return X.follow_url(object_url, source)
        link = next((l for l in structure.get("links", []) if l.get("type") == "text/plain"), None)
        if link and link.get("href"):
            return X.follow_url(object_url, link["href"])
    return X.follow_url(object_url, "/source/main") if with_default else object_url


# Source and locks (api/objectcontents.ts)


def get_object_source(h: AdtSession, source_url: str) -> str:
    validate_object_url(source_url)
    return h.request(source_url).body


def set_object_source(
    h: AdtSession, source_url: str, source: str, lock_handle: str, transport: Optional[str] = None
) -> None:
    validate_object_url(source_url)
    validate_stateful(h)
    qs: Dict[str, Any] = {"lockHandle": lock_handle}
    if transport:
        qs["corrNr"] = transport
    ctype = "application/*" if re.match(r"^<\?xml\s", source, re.IGNORECASE) else "text/plain; charset=utf-8"
    h.request(source_url, method="PUT", qs=qs, headers={"content-type": ctype}, body=source)


def lock(h: AdtSession, object_url: str, access_mode: str = "MODIFY") -> Dict[str, Any]:
    validate_object_url(object_url)
    validate_stateful(h)
    response = h.request(
        object_url,
        method="POST",
        qs={"_action": "LOCK", "accessMode": access_mode},
        headers={
            "Accept": "application/*,application/vnd.sap.as+xml;charset=UTF-8;"
            "dataname=com.sap.adt.lock.result"
        },
    )
    raw = X.parse(response.body)
    data = X.array(raw, "asx:abap", "asx:values", "DATA")
    if not data or not isinstance(data[0], dict):
        raise AdtError("SAP returned no lock result.")
    return {k: X.text(v) for k, v in data[0].items()}


def unlock(h: AdtSession, object_url: str, lock_handle: str) -> str:
    validate_object_url(object_url)
    return h.request(object_url, method="POST", qs={"_action": "UNLOCK", "lockHandle": lock_handle}).body


# Activation (api/activate.ts)


def _inactive_element(source: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(source, dict) or not source.get("ioc:ref"):
        return None
    return {
        "deleted": X.to_bool(source.get("@_ioc:deleted")),
        "user": source.get("@_ioc:user", ""),
        **X.attrs(source.get("ioc:ref")),
    }


def activate(
    h: AdtSession, object_name: str, object_url: str, main_include_url: Optional[str] = None,
    preaudit_requested: bool = True,
) -> Dict[str, Any]:
    validate_object_url(object_url)
    context = f"?context={X.uri_component(main_include_url)}" if main_include_url else ""
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<adtcore:objectReferences xmlns:adtcore="http://www.sap.com/adt/core">'
        f'<adtcore:objectReference adtcore:uri="{X.esc(object_url + context)}" adtcore:name="{X.esc(object_name)}"/>'
        "</adtcore:objectReferences>"
    )
    response = h.request(
        "/sap/bc/adt/activation",
        method="POST",
        qs={"method": "activate", "preauditRequested": preaudit_requested},
        body=body,
    )
    messages: List[Dict[str, Any]] = []
    inactive: List[Dict[str, Any]] = []
    success = True
    if response.body:
        raw = X.parse(response.body)
        for entry in X.array(raw, "ioc:inactiveObjects", "ioc:entry"):
            inactive.append({
                "object": _inactive_element(X.node(entry, "ioc:object")),
                "transport": _inactive_element(X.node(entry, "ioc:transport")),
            })
        for msg in X.array(raw, "chkl:messages", "msg"):
            message = X.attrs(msg)
            short = X.node(msg, "shortText", "txt")
            message["shortText"] = X.text(short) or "Syntax error"
            message["line"] = X.to_int(message.get("line"))
            messages.append(message)
        if inactive:
            success = False
        elif any(re.search(r"[EAX]", str(m.get("type", ""))) for m in messages):
            success = False
    return {"success": success, "messages": messages, "inactive": inactive}


# Syntax check and where-used (api/syntax.ts, api/cds.ts)


def _parse_check_results(raw: Any) -> List[Dict[str, Any]]:
    messages = []
    path = ("chkrun:checkRunReports", "chkrun:checkReport", "chkrun:checkMessageList", "chkrun:checkMessage")
    for msg in X.flat_array(raw, *path):
        a = X.attrs(msg)
        raw_uri = a.get("chkrun:uri", "")
        message = {"uri": raw_uri, "line": 0, "offset": 0, "severity": a.get("chkrun:type", ""),
                   "text": a.get("chkrun:shortText", "")}
        match = re.match(r"([^#]+)#start=(\d+),(\d+)", raw_uri)
        if match:
            message.update(uri=match.group(1), line=int(match.group(2)), offset=int(match.group(3)))
        messages.append(message)
    return messages


_CDS_SOURCE = re.compile(r"^/sap/bc/adt/((ddic/ddlx?)|(acm/dcl))/sources/")


def syntax_check(h: AdtSession, include_url: str, main_url: str, content: str) -> List[Dict[str, Any]]:
    """ADTClient.syntaxCheck(url, mainUrl, content): CDS sources use the CDS variant."""
    if _CDS_SOURCE.match(include_url):
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<chkrun:checkObjectList xmlns:adtcore="http://www.sap.com/adt/core" '
            'xmlns:chkrun="http://www.sap.com/adt/checkrun">\n'
            f'  <chkrun:checkObject adtcore:uri="{X.esc(include_url)}" chkrun:version="active">'
            "<chkrun:artifacts>\n"
            f'  <chkrun:artifact chkrun:contentType="text/plain; charset=utf-8" chkrun:uri="{X.esc(main_url)}">\n'
            f"      <chkrun:content>{_b64(content)}</chkrun:content>\n"
            "  </chkrun:artifact>\n</chkrun:artifacts></chkrun:checkObject>\n"
            "</chkrun:checkObjectList>"
        )
        headers = {
            "Content-Type": "application/vnd.sap.adt.checkobjects+xml",
            "Accept": "application/vnd.sap.adt.checkmessages+xml",
        }
    else:
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '  <chkrun:checkObjectList xmlns:chkrun="http://www.sap.com/adt/checkrun" '
            'xmlns:adtcore="http://www.sap.com/adt/core">\n'
            f'  <chkrun:checkObject adtcore:uri="{X.esc(main_url)}" chkrun:version="active">\n'
            "    <chkrun:artifacts>\n"
            f'      <chkrun:artifact chkrun:contentType="text/plain; charset=utf-8" chkrun:uri="{X.esc(include_url)}">\n'
            f"        <chkrun:content>{_b64(content)}</chkrun:content>\n"
            "      </chkrun:artifact>\n    </chkrun:artifacts>\n  </chkrun:checkObject>\n"
            "</chkrun:checkObjectList>"
        )
        headers = {"Content-Type": "application/*"}
    response = h.request("/sap/bc/adt/checkruns?reporters=abapCheckRun", method="POST", headers=headers, body=body)
    return _parse_check_results(X.parse(response.body))


def usage_references(h: AdtSession, url: str) -> List[Dict[str, Any]]:
    body = (
        '<?xml version="1.0" encoding="ASCII"?>\n'
        '  <usagereferences:usageReferenceRequest xmlns:usagereferences="http://www.sap.com/adt/ris/usageReferences">\n'
        "    <usagereferences:affectedObjects/>\n"
        "  </usagereferences:usageReferenceRequest>"
    )
    response = h.request(
        "/sap/bc/adt/repository/informationsystem/usageReferences",
        method="POST",
        qs={"uri": url},
        headers={"Content-Type": "application/*", "Accept": "application/*"},
        body=body,
    )
    raw = X.parse(response.body)
    references = []
    path = ("usageReferences:usageReferenceResult", "usageReferences:referencedObjects",
            "usageReferences:referencedObject")
    for ref in X.array(raw, *path):
        adt_object = X.node(ref, "usageReferences:adtObject")
        reference = {
            **X.attrs(ref),
            **X.attrs(adt_object),
            "packageRef": X.attrs(X.node(ref, "usageReferences:adtObject", "adtcore:packageRef")),
            "objectIdentifier": X.text(X.node(ref, "objectIdentifier")),
        }
        if not reference.get("adtcore:type"):
            # older systems hide the type in the URI hash: ...#type=CLAS/OM;name=...
            hash_part = str(reference.get("uri", "")).partition("#")[2]
            for part in hash_part.split(";"):
                key, _, value = part.partition("=")
                if key == "type":
                    from urllib.parse import unquote

                    reference["adtcore:type"] = unquote(value)
        references.append(reference)
    return references


# Unit tests (api/unittest.ts)


def _parse_alert(alert: Any) -> Dict[str, Any]:
    details = []
    for detail in X.array(alert, "details", "detail"):
        main = X.attrs(detail).get("text", "")
        children = "".join(f"\n\t{X.attrs(d).get('text', '')}" for d in X.array(detail, "details", "detail"))
        if main:
            details.append(main + children)
    stack = [X.attrs(entry) for entry in X.array(alert, "stack", "stackEntry")]
    return {**X.attrs(alert), "details": details, "stack": stack, "title": X.text(X.node(alert, "title"))}


def unit_test_run(h: AdtSession, url: str, flags: Dict[str, bool]) -> List[Dict[str, Any]]:
    def flag(name: str) -> str:
        return "true" if flags.get(name) else "false"

    body = f"""<?xml version="1.0" encoding="UTF-8"?>
  <aunit:runConfiguration xmlns:aunit="http://www.sap.com/adt/aunit">
  <external>
    <coverage active="false"/>
  </external>
  <options>
    <uriType value="semantic"/>
    <testDeterminationStrategy sameProgram="true" assignedTests="false"/>
    <testRiskLevels harmless="{flag('harmless')}" dangerous="{flag('dangerous')}" critical="{flag('critical')}"/>
    <testDurations short="{flag('short')}" medium="{flag('medium')}" long="{flag('long')}"/>
    <withNavigationUri enabled="true"/>
  </options>
  <adtcore:objectSets xmlns:adtcore="http://www.sap.com/adt/core">
    <objectSet kind="inclusive">
      <adtcore:objectReferences>
        <adtcore:objectReference adtcore:uri="{X.esc(url)}"/>
      </adtcore:objectReferences>
    </objectSet>
  </adtcore:objectSets>
</aunit:runConfiguration>"""
    response = h.request(
        "/sap/bc/adt/abapunit/testruns",
        method="POST",
        headers={"Content-Type": "application/*", "Accept": "application/*"},
        body=body,
        timeout=max(h.timeout, 300),
    )
    raw = X.parse(response.body)
    classes = []
    for cls in X.flat_array(raw, "aunit:runResult", "program", "testClasses", "testClass"):
        classes.append({
            **X.attrs(cls),
            "alerts": [_parse_alert(a) for a in X.array(cls, "alerts", "alert")],
            "testmethods": [
                {**X.attrs(m), "alerts": [_parse_alert(a) for a in X.array(m, "alerts", "alert")]}
                for m in X.flat_array(cls, "testMethods", "testMethod")
            ],
        })
    return classes


# Data preview (api/tablecontents.ts)


def _decode_value(kind: str, raw: Any) -> Any:
    value = X.text(raw)
    if kind == "D" and re.match(r"^\d{8}$", value):
        return f"{value[0:4]}-{value[4:6]}-{value[6:8]}"
    if kind in ("/", "a", "e", "F", "N", "%", "P"):
        try:
            return float(value) if any(c in value for c in ".eE") else int(value)
        except ValueError:
            return value
    if kind in ("I", "b", "8", "s"):
        try:
            return int(value)
        except ValueError:
            return value
    return value


def parse_query_response(body: str, decode: bool = True) -> Dict[str, Any]:
    raw = X.parse(body, remove_ns=True)
    fields = []
    for column in X.array(raw, "tableData", "columns"):
        meta = X.attrs(X.node(column, "metadata"))
        values = X.array(column, "dataSet", "data")
        fields.append({
            "meta": {
                "name": meta.get("name", ""),
                "type": meta.get("type", ""),
                "description": meta.get("description", ""),
                "keyAttribute": X.to_bool(meta.get("keyAttribute", False)),
                "colType": meta.get("colType", ""),
                "isKeyFigure": X.to_bool(meta.get("isKeyFigure", False)),
                "length": X.to_int(meta.get("length", 0)),
            },
            "values": values,
        })
    columns = [f["meta"] for f in fields]
    longest = max((len(f["values"]) for f in fields), default=0)
    rows = []
    for index in range(longest):
        row = {}
        for f in fields:
            raw_value = f["values"][index] if index < len(f["values"]) else None
            if raw_value is None:
                row[f["meta"]["name"]] = None
            else:
                row[f["meta"]["name"]] = _decode_value(f["meta"]["type"], raw_value) if decode else X.text(raw_value)
        rows.append(row)
    return {"columns": columns, "values": rows}


def table_contents(h: AdtSession, table: str, row_number: int = 100, sql_query: str = "") -> Dict[str, Any]:
    response = h.request(
        "/sap/bc/adt/datapreview/ddic",
        method="POST",
        qs={"rowNumber": row_number, "ddicEntityName": table},
        headers={"Accept": "application/*", "Content-Type": "text/plain"},
        body=sql_query,
    )
    return parse_query_response(response.body)


def run_query(h: AdtSession, sql: str, row_number: int = 100) -> Dict[str, Any]:
    response = h.request(
        "/sap/bc/adt/datapreview/freestyle",
        method="POST",
        qs={"rowNumber": row_number},
        headers={"Accept": "application/*", "Content-Type": "text/plain"},
        body=sql,
    )
    return parse_query_response(response.body)


# Repository tree (api/nodeContents.ts)

NODE_PARENTS = ("DEVC/K", "PROG/P", "FUGR/F", "PROG/PI")


def node_contents(h: AdtSession, parent_type: str, parent_name: Optional[str] = None) -> Dict[str, Any]:
    qs: Dict[str, Any] = {"parent_type": parent_type, "withShortDescriptions": True}
    if parent_name:
        qs["parent_name"] = parent_name
    response = h.request("/sap/bc/adt/repository/nodestructure", method="POST", qs=qs)
    nodes: List[Dict[str, Any]] = []
    if response.body:
        data = X.node(X.parse(response.body), "asx:abap", "asx:values", "DATA")
        for raw_node in X.array(data, "TREE_CONTENT", "SEU_ADT_REPOSITORY_OBJ_NODE"):
            if isinstance(raw_node, dict):
                entry = {k: X.text(v) for k, v in raw_node.items()}
                entry.setdefault("DESCRIPTION", "")
                nodes.append(entry)
    return {"nodes": nodes}


# Transports (api/transports.ts)


def _parse_task(task: Any) -> Dict[str, Any]:
    return {
        **X.attrs(task),
        "links": [X.attrs(l) for l in X.array(task, "atom:link")],
        "objects": [X.attrs(o) for o in X.array(task, "tm:abap_object")],
    }


def _parse_request(request: Any) -> Dict[str, Any]:
    return {**_parse_task(request), "tasks": [_parse_task(t) for t in X.array(request, "tm:task")]}


def _parse_target(target: Any) -> Dict[str, Any]:
    return {
        **X.attrs(target),
        "modifiable": [_parse_request(r) for r in X.array(target, "tm:modifiable", "tm:request")],
        "released": [_parse_request(r) for r in X.array(target, "tm:released", "tm:request")],
    }


def user_transports(h: AdtSession, user: str) -> Dict[str, Any]:
    response = h.request("/sap/bc/adt/cts/transportrequests", qs={"user": user, "targets": True})
    raw = X.parse(response.body)
    return {
        "workbench": [_parse_target(t) for t in X.array(raw, "tm:root", "tm:workbench", "tm:target")],
        "customizing": [_parse_target(t) for t in X.array(raw, "tm:root", "tm:customizing", "tm:target")],
    }


def transport_details(h: AdtSession, number: str) -> Dict[str, Any]:
    response = h.request(
        f"/sap/bc/adt/cts/transportrequests/{X.uri_component(number)}",
        headers={"Accept": "application/vnd.sap.adt.transportorganizer.v1+xml"},
    )
    request = X.node(X.parse(response.body), "tm:root", "tm:request")
    if not request:
        raise AdtError(f"Transport {number} not found in the response.")
    return _parse_request(request)


# ATC (api/atc.ts)


def parse_uri(source: str) -> Dict[str, Any]:
    """urlparser.ts parseUri: the uri plus the #start=line,col;end=line,col range."""
    match = re.match(r"([^?#]*)(?:\?([^#]*))?(?:#(.*))?", source or "")
    uri, _query, hash_part = match.groups() if match else ("", None, None)
    params: Dict[str, str] = {}
    for part in (hash_part or "").split(";"):
        key, _, value = part.partition("=")
        if key:
            params[key] = value

    def pos(value: Optional[str]) -> Dict[str, int]:
        if not value:
            return {"line": 0, "column": 0}
        line, _, column = value.partition(",")
        return {"line": X.to_int(line), "column": X.to_int(column)}

    start = pos(params.get("start"))
    return {"uri": uri, "range": {"start": start, "end": pos(params["end"]) if params.get("end") else start}}


def create_atc_run(h: AdtSession, variant: str, main_url: str, max_results: int = 100) -> Dict[str, Any]:
    body = f"""<?xml version="1.0" encoding="UTF-8"?>
<atc:run maximumVerdicts="{int(max_results)}" xmlns:atc="http://www.sap.com/adt/atc">
	<objectSets xmlns:adtcore="http://www.sap.com/adt/core">
		<objectSet kind="inclusive">
			<adtcore:objectReferences>
				<adtcore:objectReference adtcore:uri="{X.esc(main_url)}"/>
			</adtcore:objectReferences>
		</objectSet>
	</objectSets>
</atc:run>"""
    response = h.request(
        "/sap/bc/adt/atc/runs",
        method="POST",
        qs={"worklistId": variant},
        headers={"Accept": "application/xml", "Content-Type": "application/xml"},
        body=body,
        timeout=max(h.timeout, 300),
    )
    raw = X.parse(response.body, remove_ns=True)
    run_id = X.text(X.node(raw, "worklistRun", "worklistId"))
    if not run_id:
        raise AdtError("SAP started no ATC run (no worklistId in the response).")
    return {
        "id": run_id,
        "timestamp": X.text(X.node(raw, "worklistRun", "worklistTimestamp")),
        "infos": X.array(raw, "worklistRun", "infos", "info"),
    }


def atc_worklist(h: AdtSession, run_id: str) -> Dict[str, Any]:
    response = h.request(
        f"/sap/bc/adt/atc/worklists/{X.uri_component(run_id)}",
        qs={"includeExemptedFindings": False},
        headers={"Accept": "application/atc.worklist.v1+xml"},
    )
    raw = X.parse(response.body, remove_ns=True)
    root = X.node(raw, "worklist")
    objects = []
    for obj in X.array(root, "objects", "object"):
        findings = []
        for finding in X.array(obj, "findings", "finding"):
            fa = X.attrs(finding)
            findings.append({
                **fa,
                "priority": X.to_int(fa.get("priority")),
                "messageTitle": fa.get("messageTitle", ""),
                "checkTitle": fa.get("checkTitle", ""),
                "location": parse_uri(fa.get("location", "")),
                "messageId": str(fa.get("messageId", "")),
                "link": X.attrs(X.node(finding, "link")),
            })
        objects.append({**X.attrs(obj), "findings": findings})
    return {
        **X.attrs(root),
        "objectSets": [X.attrs(s) for s in X.array(root, "objectSets", "objectSet")],
        "objects": objects,
    }


# Dumps (api/feeds.ts)

_TAG = re.compile(r"<[^>]+>")


def _plain(html: str) -> str:
    """Dump summaries are HTML; the model reads text."""
    import html as _html

    return " ".join(_html.unescape(_TAG.sub(" ", html or "")).split())


def dumps(h: AdtSession, query: str = "") -> Dict[str, Any]:
    qs = {"$query": query} if query else {}
    response = h.request("/sap/bc/adt/runtime/dumps", qs=qs, headers={"Accept": "application/atom+xml;type=feed"})
    feed = X.node(X.parse(response.body, remove_ns=True), "feed") or {}
    entries = []
    for entry in X.array(feed, "entry"):
        summary = X.node(entry, "summary")
        entries.append({
            "categories": [X.attrs(c) for c in X.array(entry, "category")],
            "links": [X.attrs(l) for l in X.array(entry, "link")],
            "id": X.text(X.node(entry, "id")),
            "author": X.text(X.node(entry, "author", "name")),
            "text": _plain(X.text(summary)),
            "type": X.attrs(summary).get("type", ""),
            "updated": X.text(X.node(entry, "updated")),
        })
    return {"title": X.text(feed.get("title")) if isinstance(feed, dict) else "", "dumps": entries}


# Traces (api/traces.ts, api/tracetypes.ts)

TRACE_PREFIX = "/sap/bc/adt/runtime/traces/abaptraces/"


def trace_url(trace_id: str) -> str:
    return trace_id if trace_id.startswith(TRACE_PREFIX) else f"{TRACE_PREFIX}{trace_id}"


def traces_list(h: AdtSession, user: str) -> Dict[str, Any]:
    response = h.request("/sap/bc/adt/runtime/traces/abaptraces", qs={"user": user.upper()})
    feed = X.node(X.parse(response.body, remove_ns=True), "feed") or {}
    runs = []
    for entry in X.array(feed, "entry"):
        extended = X.node(entry, "extendedData") or {}
        runs.append({
            "id": X.text(X.node(entry, "id")),
            "title": X.text(X.node(entry, "title")),
            "author": X.text(X.node(entry, "author", "name")),
            "published": X.text(X.node(entry, "published")),
            "extendedData": {
                "objectName": X.text(X.node(extended, "objectName")),
                "runtime": X.to_float(X.text(X.node(extended, "runtime"))),
                "runtimeABAP": X.to_float(X.text(X.node(extended, "runtimeABAP"))),
                "runtimeDatabase": X.to_float(X.text(X.node(extended, "runtimeDatabase"))),
                "state": X.attrs(X.node(extended, "state")),
                "host": X.text(X.node(extended, "host")),
            },
        })
    return {"runs": runs}


def _time(node: Any) -> Dict[str, float]:
    a = X.attrs(node)
    return {"time": X.to_float(a.get("time")), "percentage": X.to_float(a.get("percentage"))}


def traces_hitlist(h: AdtSession, trace_id: str, with_system_events: bool = False) -> Dict[str, Any]:
    response = h.request(f"{trace_url(trace_id)}/hitlist", qs={"withSystemEvents": with_system_events})
    hitlist = X.node(X.parse(response.body, remove_ns=True), "hitlist") or {}
    entries = []
    for entry in X.array(hitlist, "entry"):
        a = X.attrs(entry)
        entries.append({
            **a,
            "hitCount": X.to_int(a.get("hitCount")),
            "grossTime": _time(X.node(entry, "grossTime")),
            "traceEventNetTime": _time(X.node(entry, "traceEventNetTime")),
            "callingProgram": X.attrs(X.node(entry, "callingProgram")),
        })
    return {"entries": entries}


def _parse_count(value: Any) -> int:
    text = str(value or "0")
    base, _, exp = text.partition("E")
    return X.to_int(base) * (10 ** X.to_int(exp)) if exp else X.to_int(base)


def traces_statements(h: AdtSession, trace_id: str, with_system_events: bool = False) -> Dict[str, Any]:
    response = h.request(
        f"{trace_url(trace_id)}/statements",
        qs={"withSystemEvents": with_system_events},
        headers={"Accept": "application/vnd.sap.adt.runtime.traces.abaptraces.aggcalltree+xml, application/xml"},
    )
    root = X.node(X.parse(response.body, remove_ns=True), "statements") or {}
    statements = []
    for st in X.array(root, "statement"):
        a = X.attrs(st)
        statements.append({
            **a,
            "hitCount": X.to_int(a.get("hitCount")),
            "grossTime": _time(X.node(st, "grossTime")),
            "callingProgram": X.attrs(X.node(st, "callingProgram")),
        })
    return {"statements": statements, "count": _parse_count(X.attrs(root).get("count"))}


# Text elements (api/textelements.ts)

TEXT_CATEGORIES = ("symbols", "selections", "headings")
_VALID_HEADINGS = ("LISTHEADER", "COLUMNHEADER_1", "COLUMNHEADER_2", "COLUMNHEADER_3", "COLUMNHEADER_4")


def text_elements_url(object_type: str, object_name: str) -> str:
    lower = object_name.lower()
    encoded = X.uri_component(lower) if "/" in lower else lower
    upper = object_type.upper()
    if upper.startswith("CLAS"):
        return f"/sap/bc/adt/textelements/classes/{encoded}"
    if upper.startswith("FUGR"):
        return f"/sap/bc/adt/textelements/functiongroups/{encoded}"
    return f"/sap/bc/adt/textelements/programs/{encoded}"


def parse_text_elements(body: str) -> List[Dict[str, Any]]:
    elements = []
    max_length: Optional[int] = None
    ddic_reference: Optional[str] = None
    for raw in body.split("\n"):
        line = raw.strip()
        if line.startswith("@MaxLength:"):
            try:
                max_length = int(line[len("@MaxLength:"):])
            except ValueError:
                max_length = None
        elif line.startswith("@DDICReference:"):
            ddic_reference = line[len("@DDICReference:"):]
        elif "=" in line:
            key, _, value = line.partition("=")
            if key.strip():
                element: Dict[str, Any] = {"id": key.strip(), "text": value}
                if max_length is not None:
                    element["maxLength"] = max_length
                if ddic_reference is not None:
                    element["ddicReference"] = ddic_reference
                elements.append(element)
                max_length, ddic_reference = None, None
    return elements


def validate_text_elements(elements: Sequence[Dict[str, Any]], category: str) -> None:
    for el in elements:
        key = str(el["id"]).upper()
        text = str(el.get("text", ""))
        if category == "symbols":
            if len(key) != 3:
                raise AdtError(f'Symbol key "{el["id"]}" must be exactly 3 characters')
            if re.search(r"\s", key):
                raise AdtError(f'Symbol key "{el["id"]}" must not contain blanks')
            if el.get("maxLength") and len(text) > int(el["maxLength"]):
                raise AdtError(f'Symbol "{el["id"]}" text exceeds maxLength {el["maxLength"]}')
        elif category == "headings":
            if key not in _VALID_HEADINGS:
                raise AdtError(f'Invalid heading key "{el["id"]}". Allowed: {", ".join(_VALID_HEADINGS)}')
            limit = 71 if key == "LISTHEADER" else 255
            if len(text) > limit:
                raise AdtError(f'Heading "{el["id"]}" text exceeds maximum length of {limit}')
        elif category == "selections" and len(text) > 30:
            raise AdtError(f'Selection "{el["id"]}" text exceeds maximum length of 30')


def format_text_elements_body(elements: Sequence[Dict[str, Any]], category: str) -> str:
    validate_text_elements(elements, category)
    lines = []
    for el in elements:
        if el.get("maxLength") and int(el["maxLength"]) > 0 and category == "symbols":
            lines.append(f"@MaxLength:{int(el['maxLength'])}")
        if category == "selections" and el.get("ddicReference"):
            lines.append(f"@DDICReference:{el['ddicReference']}")
        lines.append(f"{str(el['id']).upper()}={el.get('text', '')}")
        if category != "headings":
            lines.append("")
    return "\n".join(lines)


def get_text_elements(h: AdtSession, url: str, category: str = "symbols") -> Dict[str, Any]:
    from urllib.parse import unquote

    program_name = unquote(url.rstrip("/").split("/")[-1])
    try:
        response = h.request(
            f"{url}/source/{category}", headers={"Accept": f"application/vnd.sap.adt.textelements.{category}.v1"}
        )
    except AdtError as err:
        if err.status == 404:
            return {"textElements": [], "programName": program_name}
        raise
    return {"textElements": parse_text_elements(response.body), "programName": program_name}


def set_text_elements(
    h: AdtSession, url: str, category: str, elements: Sequence[Dict[str, Any]], lock_handle: str,
    transport: Optional[str] = None,
) -> None:
    qs: Dict[str, Any] = {"lockHandle": lock_handle}
    if transport:
        qs["corrNr"] = transport
    media = f"application/vnd.sap.adt.textelements.{category}.v1"
    h.request(
        f"{url}/source/{category}",
        method="PUT",
        qs=qs,
        headers={"Content-Type": f"{media}; charset=UTF-8", "Accept": media},
        body=format_text_elements_body(elements, category),
    )


# Object creation (api/objectcreator.ts)

# typeId: (creationPath, rootName, nameSpace, extra)
CREATABLE_TYPES: Dict[str, tuple] = {
    "PROG/P": ("programs/programs", "program:abapProgram", 'xmlns:program="http://www.sap.com/adt/programs/programs"', ""),
    "CLAS/OC": ("oo/classes", "class:abapClass", 'xmlns:class="http://www.sap.com/adt/oo/classes"', ""),
    "INTF/OI": ("oo/interfaces", "intf:abapInterface", 'xmlns:intf="http://www.sap.com/adt/oo/interfaces"', ""),
    "PROG/I": ("programs/includes", "include:abapInclude", 'xmlns:include="http://www.sap.com/adt/programs/includes"', ""),
    "FUGR/F": ("functions/groups", "group:abapFunctionGroup", 'xmlns:group="http://www.sap.com/adt/functions/groups"', ""),
    "FUGR/FF": ("functions/groups/%s/fmodules", "fmodule:abapFunctionModule", 'xmlns:fmodule="http://www.sap.com/adt/functions/fmodules"', ""),
    "FUGR/I": ("functions/groups/%s/includes", "finclude:abapFunctionGroupInclude", 'xmlns:finclude="http://www.sap.com/adt/functions/fincludes"', ""),
    "DDLS/DF": ("ddic/ddl/sources", "ddl:ddlSource", 'xmlns:ddl="http://www.sap.com/adt/ddic/ddlsources"', ""),
    "DCLS/DL": ("acm/dcl/sources", "dcl:dclSource", 'xmlns:dcl="http://www.sap.com/adt/acm/dclsources"', ""),
    "DDLX/EX": ("ddic/ddlx/sources", "ddlx:ddlxSource", 'xmlns:ddlx="http://www.sap.com/adt/ddic/ddlxsources"', ""),
    "DDLA/ADF": ("ddic/ddla/sources", "ddla:ddlaSource", 'xmlns:ddla="http://www.sap.com/adt/ddic/ddlasources"', ""),
    "DEVC/K": ("packages", "pak:package", 'xmlns:pak="http://www.sap.com/adt/packages"', ""),
    "TABL/DT": ("ddic/tables", "blue:blueSource", 'xmlns:blue="http://www.sap.com/wbobj/blue"', ""),
    "SRVD/SRV": ("ddic/srvd/sources", "srvd:srvdSource", 'xmlns:srvd="http://www.sap.com/adt/ddic/srvdsources"', 'srvd:srvdSourceType="S"'),
    "AUTH": ("aps/iam/auth", "auth:auth", 'xmlns:auth="http://www.sap.com/iam/auth"', ""),
    "SUSO/B": ("aps/iam/suso", "susob:suso", 'xmlns:susob="http://www.sap.com/iam/suso"', ""),
    "DTEL/DE": ("ddic/dataelements", "blue:wbobj", 'xmlns:blue="http://www.sap.com/wbobj/dictionary/dtel"', ""),
    "DOMA/DD": ("ddic/domains", "domain:domain", 'xmlns:domain="http://www.sap.com/dictionary/domain"', ""),
    "TABL/DS": ("ddic/structures", "blue:blueSource", 'xmlns:blue="http://www.sap.com/wbobj/blue"', ""),
    "MSAG/N": ("messageclass", "mc:messageClass", 'xmlns:mc="http://www.sap.com/adt/MessageClass"', ""),
}


def create_object(
    h: AdtSession,
    objtype: str,
    name: str,
    parent_name: str,
    description: str,
    parent_path: str,
    transport: Optional[str] = None,
    language: str = "EN",
    responsible: Optional[str] = None,
) -> str:
    """Create an object. Returns the creation URL used."""
    spec = CREATABLE_TYPES.get(objtype)
    if spec is None:
        raise AdtError("Unsupported object type")
    if objtype == "DEVC/K":
        # abap-adt-api needs a software component and transport layer for
        # packages, which this tool does not collect.
        raise AdtError("Can't create a Package with incomplete data (software component and transport layer are required). Create packages in SE80 or Eclipse.")
    creation_path, root_name, namespace, extra = spec
    path = creation_path % X.uri_component(parent_name.lower()) if "%s" in creation_path else creation_path
    url = f"/sap/bc/adt/{path}"
    owner = (responsible or h.username).upper()
    if objtype in ("FUGR/FF", "FUGR/I"):
        body = f"""<?xml version="1.0" encoding="UTF-8"?>
  <{root_name} {namespace}
  xmlns:adtcore="http://www.sap.com/adt/core"
  adtcore:description="{X.esc(description)}"
  adtcore:name="{X.esc(name)}" adtcore:type="{objtype}">
  <adtcore:containerRef adtcore:name="{X.esc(parent_name)}"
    adtcore:type="FUGR/F"
    adtcore:uri="{X.esc(parent_path)}" />
</{root_name}>"""
    else:
        body = f"""<?xml version="1.0" encoding="UTF-8"?>
        <{root_name} {namespace}
          xmlns:adtcore="http://www.sap.com/adt/core"
          adtcore:description="{X.esc(description)}"
          adtcore:name="{X.esc(name)}" adtcore:type="{objtype}"
          adtcore:language="{X.esc(language)}" adtcore:masterLanguage="{X.esc(language)}"
          adtcore:responsible="{X.esc(owner)}" {extra}>
          <adtcore:packageRef adtcore:name="{X.esc(parent_name)}"/>
        </{root_name}>"""
    qs = {"corrNr": transport} if transport else {}
    h.request(url, method="POST", qs=qs, headers={"Content-Type": "application/*"}, body=body)
    return url
