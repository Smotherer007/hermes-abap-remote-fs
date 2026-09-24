"""XML helpers.

abap-adt-api (the library pi-abap-fs builds on) parses ADT responses with
fast-xml-parser, and its parsing code is written against that library's
output: element names keep their namespace prefix (``adtcore:name``),
attributes appear as ``@_<name>``, repeated children become lists, a leaf
element is just its text, and mixed nodes carry their text as ``#text``.

:func:`parse` produces the same shape from the standard library's minidom,
which keeps the prefixes as written. That lets the ADT operations in
``adt.py`` follow the original parsing code closely, which matters because
the only reference for many of these formats is that code.

Values stay strings. Where a number or a date is needed, the caller converts.
"""

from __future__ import annotations

from typing import Any, Dict, List
from urllib.parse import quote
from xml.dom import minidom
from xml.sax.saxutils import escape


def _strip_prefix(name: str) -> str:
    return name.split(":", 1)[1] if ":" in name else name


def _convert(element, remove_ns: bool) -> Any:
    attrs: Dict[str, Any] = {}
    if element.attributes is not None:
        for index in range(element.attributes.length):
            attr = element.attributes.item(index)
            name = attr.name
            if name == "xmlns" or name.startswith("xmlns:"):
                continue  # declarations, never data
            key = _strip_prefix(name) if remove_ns else name
            attrs[f"@_{key}"] = attr.value

    children: Dict[str, Any] = {}
    text_parts: List[str] = []
    for child in element.childNodes:
        if child.nodeType == child.ELEMENT_NODE:
            tag = _strip_prefix(child.tagName) if remove_ns else child.tagName
            value = _convert(child, remove_ns)
            if tag in children:
                existing = children[tag]
                if isinstance(existing, list):
                    existing.append(value)
                else:
                    children[tag] = [existing, value]
            else:
                children[tag] = value
        elif child.nodeType in (child.TEXT_NODE, child.CDATA_SECTION_NODE):
            text_parts.append(child.data)

    text = "".join(text_parts)
    if not attrs and not children:
        return text.strip()
    node: Dict[str, Any] = {**attrs, **children}
    if text.strip():
        node["#text"] = text.strip()
    return node


def parse(xml: str, remove_ns: bool = False) -> Dict[str, Any]:
    """Parse ``xml`` into ``{root_tag: node}``. Empty input gives ``{}``."""
    if not xml or not xml.strip():
        return {}
    document = minidom.parseString(xml.encode("utf-8") if isinstance(xml, str) else xml)
    root = document.documentElement
    tag = _strip_prefix(root.tagName) if remove_ns else root.tagName
    return {tag: _convert(root, remove_ns)}


def root(parsed: Dict[str, Any]) -> Any:
    """The single root node of a parsed document."""
    return next(iter(parsed.values()), {}) if parsed else {}


def node(value: Any, *path: str) -> Any:
    """Walk ``path`` through dicts; ``None`` as soon as a step is missing."""
    current = value
    for key in path:
        if isinstance(current, dict):
            current = current.get(key)
        else:
            return None
        if current is None:
            return None
    return current


def array(value: Any, *path: str) -> List[Any]:
    """Like :func:`node`, but always a list: missing -> [], single -> [x]."""
    found = node(value, *path) if path else value
    if found is None or found == "":
        return []
    return found if isinstance(found, list) else [found]


def flat_array(value: Any, *path: str) -> List[Any]:
    """Collect ``path`` across every list on the way (fast-xml-parser's xmlFlatArray)."""
    if value is None or value == "":
        return []
    if not path:
        return value if isinstance(value, list) else [value]
    if isinstance(value, list):
        out: List[Any] = []
        for item in value:
            out.extend(flat_array(item, *path))
        return out
    if isinstance(value, dict):
        return flat_array(value.get(path[0]), *path[1:])
    return []


def attrs(value: Any) -> Dict[str, Any]:
    """The attributes of a node, without the ``@_`` prefix."""
    if not isinstance(value, dict):
        return {}
    return {key[2:]: val for key, val in value.items() if key.startswith("@_")}


def text(value: Any) -> str:
    """Text of a leaf or mixed node."""
    if value is None:
        return ""
    if isinstance(value, dict):
        return str(value.get("#text", ""))
    return str(value)


def to_int(value: Any) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def to_float(value: Any) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return 0.0


def to_bool(value: Any) -> bool:
    return str(value).strip().lower() in ("true", "x", "1")


def esc(value: Any) -> str:
    """Escape a value for an XML attribute or text node."""
    return escape(str(value), {'"': "&quot;"})


def follow_url(base: str, extra: str) -> str:
    """Resolve a link relative to an object URL (abap-adt-api's followUrl)."""
    if extra.startswith("./"):
        base = base[: base.rfind("/") + 1] if "/" in base else ""
        extra = extra[2:]
    elif extra.startswith("/"):
        extra = extra[1:]
    base = base[:-1] if base.endswith("/") else base
    return f"{base}/{extra}"


def uri_component(value: str) -> str:
    """JavaScript's encodeURIComponent."""
    return quote(value, safe="-_.!~*'()")
