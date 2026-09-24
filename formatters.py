"""Pure formatting functions that convert ADT data into display strings.

No I/O, no state -- data in, string out. Line for line the output of
pi-abap-fs, with two deliberate changes: dump summaries arrive as HTML and
are shown as text, and dates in query results are ISO dates.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

from .models import AbapProfile


def _v(value: Any) -> str:
    return "" if value is None else str(value)


# Search


def format_search_results(results: Sequence[Mapping[str, Any]], query: str) -> str:
    if not results:
        return f'No objects found matching "{query}".'
    lines = []
    for r in results:
        parts = [_v(r.get("adtcore:name")), f"type={_v(r.get('adtcore:type'))}"]
        if r.get("adtcore:packageName"):
            parts.append(f"package={r['adtcore:packageName']}")
        if r.get("adtcore:description"):
            parts.append(str(r["adtcore:description"]))
        lines.append(f"- {' | '.join(parts)}  ({_v(r.get('adtcore:uri'))})")
    return f'{len(results)} object(s) matching "{query}":\n' + "\n".join(lines)


# Object structure


def format_object_structure(structure: Mapping[str, Any]) -> str:
    meta = structure.get("metaData", {})
    lines = [f"Name: {_v(meta.get('adtcore:name'))}", f"Type: {_v(meta.get('adtcore:type'))}"]
    if meta.get("adtcore:description"):
        lines.append(f"Description: {meta['adtcore:description']}")
    lines.append(f"Responsible: {meta.get('adtcore:responsible') or '?'}")
    lines.append(f"Language: {meta.get('adtcore:language') or '?'}")
    if isinstance(structure.get("includes"), list):
        lines.append("Includes:")
        for inc in structure["includes"]:
            lines.append(f"  - {_v(inc.get('class:includeType'))}")
    return "\n".join(lines)


# Activation


def format_activation(result: Mapping[str, Any]) -> str:
    if result.get("success"):
        return "Activation successful."
    lines = ["Activation failed."]
    messages = result.get("messages") or []
    if messages:
        lines.append("Messages:")
        for m in messages:
            lines.append(f"  - [{_v(m.get('type'))}] {_v(m.get('shortText'))} (line {_v(m.get('line'))})")
    inactive = result.get("inactive") or []
    if inactive:
        lines.append("Inactive objects:")
        for rec in inactive:
            obj = rec.get("object")
            if obj:
                lines.append(f"  - {_v(obj.get('adtcore:name'))} ({_v(obj.get('adtcore:type'))}) by {_v(obj.get('user'))}")
    return "\n".join(lines)


# Unit tests


def format_unit_tests(classes: Sequence[Mapping[str, Any]]) -> str:
    if not classes:
        return "No test classes found or no tests were executed."
    lines: List[str] = []
    total = 0
    failures = 0
    for c in classes:
        lines.append(f"Class: {_v(c.get('adtcore:name'))} (risk={_v(c.get('riskLevel'))})")
        for m in c.get("testmethods", []):
            total += 1
            alert = next((a for a in m.get("alerts", []) if a.get("severity") in ("critical", "fatal")), None)
            status = f"FAILED ({alert['severity']})" if alert else "passed"
            if alert:
                failures += 1
            lines.append(f"  - {_v(m.get('adtcore:name'))}: {status}")
            if alert:
                lines.append(f"      {_v(alert.get('title'))}")
                for detail in alert.get("details", [])[:5]:
                    lines.append("      " + str(detail).replace("\n", "\n      "))
    lines.insert(0, f"{total} test method(s), {failures} failure(s).")
    return "\n".join(lines)


# Query / table results


def format_query_result(result: Mapping[str, Any]) -> str:
    columns = result.get("columns", [])
    values = result.get("values", [])
    if not values:
        return "Query returned no rows."
    names = [c["name"] for c in columns]
    rows = [[_v(row.get(n)) for n in names] for row in values]
    widths = [max([len(n)] + [len(r[i]) for r in rows]) for i, n in enumerate(names)]

    def fmt(cells: List[str]) -> str:
        return " | ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells))

    lines = [fmt(names), "-+-".join("-" * w for w in widths), *[fmt(r) for r in rows]]
    lines.append(f"\n{len(values)} row(s).")
    return "\n".join(lines)


# Node contents


def format_nodes(structure: Mapping[str, Any]) -> str:
    nodes = structure.get("nodes", [])
    if not nodes:
        return "No child objects found."
    lines = []
    for n in nodes:
        desc = f" ({n['DESCRIPTION']})" if n.get("DESCRIPTION") else ""
        lines.append(f"- {_v(n.get('OBJECT_NAME'))}  [{_v(n.get('OBJECT_TYPE'))}]{desc}")
    return f"{len(nodes)} node(s):\n" + "\n".join(lines)


# Object types


def format_object_types(types: Sequence[Mapping[str, Any]]) -> str:
    if not types:
        return "No object types available."
    lines = [f"- {_v(t.get('name'))} ({_v(t.get('type'))}) — {_v(t.get('description'))}" for t in types]
    return f"{len(types)} object type(s):\n" + "\n".join(lines)


# Transports


def _target_requests(target: Mapping[str, Any]) -> List[str]:
    return [
        f"  - {_v(r.get('tm:number'))} [{_v(r.get('tm:status'))}] {_v(r.get('tm:desc'))} (owner={_v(r.get('tm:owner'))})"
        for r in [*target.get("modifiable", []), *target.get("released", [])]
    ]


def format_transports(transports: Mapping[str, Any]) -> str:
    lines: List[str] = []
    for kind, key in (("Workbench", "workbench"), ("Customizing", "customizing")):
        for target in transports.get(key, []):
            requests = _target_requests(target)
            if not requests:
                continue
            lines.append(f"{kind} target {_v(target.get('tm:name'))} ({_v(target.get('tm:desc'))}):")
            lines.extend(requests)
    return "\n".join(lines) if lines else "No transport requests found for this user."


def format_transport_details(request: Mapping[str, Any]) -> str:
    lines = [
        f"Transport: {_v(request.get('tm:number'))}",
        f"Description: {_v(request.get('tm:desc'))}",
        f"Status: {_v(request.get('tm:status'))}",
        f"Owner: {_v(request.get('tm:owner'))}",
    ]
    own = [f"  - {_v(o.get('tm:name'))} ({_v(o.get('tm:type'))})" for o in request.get("objects", [])]
    if own:
        lines.append("Objects:")
        lines.extend(own)
    tasks = request.get("tasks", [])
    if tasks:
        lines.append("Tasks:")
        for t in tasks:
            lines.append(f"  {_v(t.get('tm:number'))}: {_v(t.get('tm:desc'))}")
            for o in t.get("objects", []):
                lines.append(f"    - {_v(o.get('tm:name'))} ({_v(o.get('tm:type'))})")
    return "\n".join(lines)


def transport_object_count(request: Mapping[str, Any]) -> int:
    return len(request.get("objects", [])) + sum(len(t.get("objects", [])) for t in request.get("tasks", []))


# Where-used


def format_usage_references(references: Sequence[Mapping[str, Any]]) -> str:
    if not references:
        return "No usage references found."
    lines = [
        f"- {r.get('adtcore:name') or r.get('objectIdentifier') or ''} ({r.get('adtcore:type') or '?'})"
        for r in references
    ]
    return f"{len(references)} usage reference(s):\n" + "\n".join(lines)


# Lock


def format_lock(lock: Mapping[str, Any]) -> str:
    lines = [f"Locked. lockHandle={_v(lock.get('LOCK_HANDLE'))}"]
    if lock.get("CORRNR"):
        lines.append(f"Transport: {lock['CORRNR']} ({_v(lock.get('CORRTEXT'))})")
    else:
        lines.append("Transport: (none)")
    if lock.get("CORRUSER"):
        lines.append(f"Correction user: {lock['CORRUSER']}")
    return "\n".join(lines)


# Profiles


def format_profile_status(
    profiles: Mapping[str, AbapProfile], active: Optional[str], env_profiles: Sequence[str] = ()
) -> str:
    names = list(profiles)
    if not names:
        return "No SAP connection configured. Use the abap_setup tool to configure a system."
    lines = [f"{len(names)} SAP profile{'' if len(names) == 1 else 's'} configured:", ""]
    for name in names:
        cfg = profiles[name]
        marker = "[active]" if name == active else "       "
        source = " (from environment)" if name in env_profiles else ""
        lines.append(f"{marker} {name}: {cfg.username}@{cfg.url}{source}")
        extra = [
            f"client={cfg.client}" if cfg.client else "",
            f"lang={cfg.language}" if cfg.language else "",
            "insecure-tls" if cfg.allow_unauthorized else "",
        ]
        extra = [e for e in extra if e]
        if extra:
            lines.append(f"       {', '.join(extra)}")
    return "\n".join(lines)


# Syntax check


def format_syntax_check(results: Sequence[Mapping[str, Any]]) -> str:
    if not results:
        return "No syntax errors found."
    lines = [f"- [{_v(r.get('severity'))}] line {_v(r.get('line'))}: {_v(r.get('text'))}" for r in results]
    return f"{len(results)} message(s):\n" + "\n".join(lines)


# ATC


def atc_findings(worklist: Mapping[str, Any]) -> List[Dict[str, Any]]:
    return [
        {"object": o.get("name", ""), "objectType": o.get("type", ""), **f}
        for o in worklist.get("objects", [])
        for f in o.get("findings", [])
    ]


def format_atc_result(worklist: Mapping[str, Any]) -> str:
    findings = atc_findings(worklist)
    if not findings:
        return "ATC analysis completed with no findings."
    lines = [f"{len(findings)} ATC finding(s):"]
    for f in findings:
        location = f.get("location")
        pos = ""
        if location:
            line = ((location.get("range") or {}).get("start") or {}).get("line")
            pos = f"line {line if line is not None else '?'}"
        lines.append(
            f"- [P{_v(f.get('priority'))}] {_v(f.get('object'))} ({_v(f.get('objectType'))}): "
            f"{_v(f.get('checkTitle'))} — {_v(f.get('messageTitle'))} {pos}"
        )
    return "\n".join(lines)


# Dumps


def format_dumps(feed: Mapping[str, Any]) -> str:
    dumps = feed.get("dumps", [])
    if not dumps:
        return "No runtime dumps found."
    lines = []
    for d in dumps:
        when = f" [{d['id']}]" if d.get("id") else ""
        # The first category's term is the runtime error (e.g. COMPUTE_INT_ZERODIVIDE).
        terms = [c.get("term", "") for c in d.get("categories", []) if c and c.get("term")]
        kind = terms[0] if terms else d.get("type", "")
        lines.append(f"- {kind}{when}: {_v(d.get('text'))[:400]}")
    return f"{len(dumps)} runtime dump(s):\n" + "\n".join(lines)


# Traces


def format_traces(result: Mapping[str, Any]) -> str:
    runs = result.get("runs", [])
    if not runs:
        return "No traces found for this user."
    lines = []
    for r in runs:
        ext = r.get("extendedData") or {}
        obj = f" ({ext['objectName']})" if ext.get("objectName") else ""
        state = (ext.get("state") or {}).get("text", "")
        runtime = f"{round(ext['runtime'])}s" if ext.get("runtime") else ""
        lines.append(f"- {_v(r.get('title'))}{obj} [{state}] {runtime} — id={_v(r.get('id'))}")
    return f"{len(runs)} trace(s):\n" + "\n".join(lines)


def _num(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def format_trace_hitlist(hitlist: Mapping[str, Any], statements: Mapping[str, Any]) -> str:
    lines: List[str] = []
    stmts = statements.get("statements", [])
    if stmts:
        lines.append("Statements (aggregated call tree):")
        for s in stmts[:50]:
            lines.append(
                f"  - {_v(s.get('description'))} — hits={_v(s.get('hitCount'))}, gross={_num(s['grossTime']['time'])}ms"
            )
    entries = hitlist.get("entries", [])
    if entries:
        if lines:
            lines.append("")
        lines.append("Hit list (hot spots):")
        for e in sorted(entries, key=lambda x: x["grossTime"]["time"], reverse=True)[:25]:
            gross = e["grossTime"]
            lines.append(
                f"  - {_v(e.get('description'))} — hits={_v(e.get('hitCount'))}, "
                f"gross={_num(gross['time'])}ms ({_num(gross['percentage'])}%)"
            )
    return "\n".join(lines) if lines else "Trace has no statements or hit list entries."


# Text elements


def format_text_elements(result: Mapping[str, Any], category: str) -> str:
    elements = result.get("textElements", [])
    program = _v(result.get("programName"))
    if not elements:
        return f"No {category} text elements found for {program}."
    lines = []
    for e in elements:
        extra = f" (max {e['maxLength']})" if e.get("maxLength") else ""
        lines.append(f"- {_v(e.get('id'))} = {_v(e.get('text'))}{extra}")
    return f"{len(elements)} {category} text element(s) for {program}:\n" + "\n".join(lines)
