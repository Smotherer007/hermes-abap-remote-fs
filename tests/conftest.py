"""Test fixtures.

The plugin is imported the way Hermes imports a directory plugin: as a
package whose ``__init__.py`` is the repository root.

``sap`` is a small ADT stand-in on localhost. It behaves like the parts of
an SAP system the client depends on:

* basic auth on every request, 401 for a wrong password
* ``x-csrf-token: fetch`` hands out a token and a session cookie; any
  modifying request without the right token gets 403 ``Required``
* ``X-sap-adt-sessiontype: stateful`` keeps a server-side context per
  session cookie; the first stateless request on it ends the context and
  releases every lock taken in it
* locks are exclusive per object and only usable from the session that
  took them

Everything else is a route table: (method, path) -> reply.
"""

from __future__ import annotations

import base64
import importlib
import importlib.util
import itertools
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "hermes_plugins_test.abap_remote_fs"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load_plugin():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    parent = "hermes_plugins_test"
    if parent not in sys.modules:
        namespace = importlib.util.module_from_spec(importlib.machinery.ModuleSpec(parent, None, is_package=True))
        namespace.__path__ = []  # type: ignore[attr-defined]
        sys.modules[parent] = namespace
    spec = importlib.util.spec_from_file_location(PACKAGE, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)
    return module


plugin = _load_plugin()


def sub(name: str):
    return importlib.import_module(f"{PACKAGE}.{name}")


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


Reply = Any  # str body | (status, body) | (status, body, headers) | callable(request) -> one of those


class CIDict(dict):
    """Header dict with case-insensitive lookup (urllib capitalises names)."""

    def __init__(self, items):
        super().__init__()
        for key, value in items:
            super().__setitem__(key.lower(), value)

    def __getitem__(self, key):
        return super().__getitem__(key.lower())

    def get(self, key, default=None):
        return super().get(key.lower(), default)

    def __contains__(self, key):
        return super().__contains__(key.lower())


class Request:
    def __init__(self, method, path, query, headers, body, session):
        self.method = method
        self.path = path
        self.query = query
        self.headers = headers
        self.body = body
        self.session = session

    def q(self, key: str) -> Optional[str]:
        values = self.query.get(key)
        return values[0] if values else None


def exception_xml(message: str, kind: str = "ExceptionResourceNotFound") -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?><exc:exception '
        'xmlns:exc="http://www.sap.com/abapxml/types/communicationframework">'
        f'<namespace id="com.sap.adt"/><type id="{kind}"/><message lang="EN">{message}</message>'
        f'<localizedMessage lang="EN">{message}</localizedMessage>'
        '<properties><entry key="T100KEY-ID">SADT_REST</entry></properties></exc:exception>'
    )


def lock_xml(handle: str, corrnr: str = "DEVK900123") -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?><asx:abap xmlns:asx="http://www.sap.com/abapxml" version="1.0">'
        f"<asx:values><DATA><LOCK_HANDLE>{handle}</LOCK_HANDLE><CORRNR>{corrnr}</CORRNR>"
        "<CORRUSER>DEVELOPER</CORRUSER><CORRTEXT>Demo change</CORRTEXT><IS_LOCAL/><IS_LINK_UP/>"
        "<MODIFICATION_SUPPORT>ModifyAll</MODIFICATION_SUPPORT></DATA></asx:values></asx:abap>"
    )


class FakeSAP:
    USER = "DEVELOPER"
    PASSWORD = "secret"

    def __init__(self) -> None:
        self.routes: Dict[Tuple[str, str], Reply] = {}
        self.calls: List[Request] = []
        self.sessions: Dict[str, Dict[str, Any]] = {}  # cookie -> {"token", "stateful", "locks": set()}
        self.locks: Dict[str, Tuple[str, str]] = {}    # object url -> (session, handle)
        self.sources: Dict[str, str] = {}              # source url -> content
        self._ids = itertools.count(1)
        self._mutex = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def on(self, method: str, path: str, reply: Reply) -> None:
        self.routes[(method.upper(), path)] = reply

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    # Inspection

    def requests(self, method: Optional[str] = None, path: Optional[str] = None) -> List[Request]:
        return [
            c for c in self.calls
            if (method is None or c.method == method) and (path is None or c.path == path)
        ]

    def last(self, method: str, path: str) -> Request:
        found = self.requests(method, path)
        assert found, f"no {method} {path} in {[(c.method, c.path) for c in self.calls]}"
        return found[-1]

    def open_stateful_sessions(self) -> int:
        return sum(1 for s in self.sessions.values() if s["stateful"])

    # Built-in behaviour

    def _session_for(self, headers) -> Optional[str]:
        cookie = headers.get("Cookie", "")
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "SAP_SESSIONID":
                return value
        return None

    def _end_stateful(self, session: str) -> None:
        state = self.sessions.get(session)
        if not state or not state["stateful"]:
            return
        state["stateful"] = False
        for url in list(state["locks"]):
            self.locks.pop(url, None)
        state["locks"].clear()

    def _builtin(self, req: Request) -> Optional[Tuple[int, str, Dict[str, str]]]:
        # POST <object>?_action=LOCK / UNLOCK
        action = req.q("_action")
        if req.method == "POST" and action == "LOCK":
            if not req.headers.get("X-sap-adt-sessiontype") == "stateful":
                return 400, exception_xml("Lock requires a stateful session", "ExceptionStateless"), {}
            holder = self.locks.get(req.path)
            if holder and holder[0] != req.session:
                return 403, exception_xml(f"{req.path} is locked by another session", "ExceptionResourceLocked"), {}
            handle = holder[1] if holder else f"LOCK{next(self._ids)}"
            self.locks[req.path] = (req.session, handle)
            self.sessions[req.session]["locks"].add(req.path)
            return 200, lock_xml(handle), {}
        if req.method == "POST" and action == "UNLOCK":
            holder = self.locks.get(req.path)
            if holder and holder[0] == req.session and holder[1] == req.q("lockHandle"):
                self.locks.pop(req.path, None)
                self.sessions[req.session]["locks"].discard(req.path)
            return 200, "", {}
        # PUT <source>?lockHandle=
        if req.method == "PUT" and req.q("lockHandle") is not None:
            object_url = req.path.split("/source/")[0].split("/includes/")[0]
            for prefix in ("/sap/bc/adt/textelements/programs/", "/sap/bc/adt/textelements/classes/"):
                if req.path.startswith(prefix):
                    name = req.path[len(prefix):].split("/")[0]
                    kind = "programs/programs" if "programs" in prefix else "oo/classes"
                    object_url = f"/sap/bc/adt/{kind}/{name}"
            holder = self.locks.get(object_url)
            if not holder or holder != (req.session, req.q("lockHandle")):
                return 423, exception_xml("Invalid lock handle", "ExceptionResourceInvalidLockHandle"), {}
            self.sources[req.path] = req.body
            return 200, "", {}
        return None

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, status: int, body: str, headers: Dict[str, str]) -> None:
                data = body.encode("utf-8")
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _handle(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode("utf-8") if length else ""
                parts = urlsplit(self.path)
                query = parse_qs(parts.query, keep_blank_values=True)
                headers = CIDict(self.headers.items())

                with fake._mutex:
                    expected = "Basic " + base64.b64encode(f"{fake.USER}:{fake.PASSWORD}".encode()).decode()
                    if headers.get("Authorization") != expected:
                        fake.calls.append(Request(method, parts.path, query, headers, body, None))
                        return self._reply(401, "", {"WWW-Authenticate": 'Basic realm="SAP"'})

                    session = fake._session_for(headers)
                    extra: Dict[str, str] = {}
                    token = headers.get("x-csrf-token")
                    if session is None or session not in fake.sessions:
                        session = f"S{next(fake._ids)}"
                        fake.sessions[session] = {"token": f"TOKEN-{session}", "stateful": False, "locks": set()}
                        extra["Set-Cookie"] = f"SAP_SESSIONID={session}; path=/; HttpOnly"
                    state = fake.sessions[session]
                    if token == "fetch":
                        extra["x-csrf-token"] = state["token"]
                    elif method not in ("GET", "HEAD") and token != state["token"]:
                        fake.calls.append(Request(method, parts.path, query, headers, body, session))
                        return self._reply(403, "CSRF token validation failed", {"x-csrf-token": "Required"})

                    if headers.get("X-sap-adt-sessiontype") == "stateful":
                        state["stateful"] = True
                    else:
                        fake._end_stateful(session)

                    req = Request(method, parts.path, query, headers, body, session)
                    fake.calls.append(req)

                    reply = fake.routes.get((method, parts.path))
                    if reply is None:
                        builtin = fake._builtin(req)
                        if builtin is not None:
                            status, text, headers_out = builtin
                            return self._reply(status, text, {**extra, **headers_out})
                        if method == "GET" and parts.path == "/sap/bc/adt/compatibility/graph":
                            return self._reply(200, fixture("compat_graph.xml"), extra)
                        if method == "GET" and parts.path in fake.sources:
                            return self._reply(200, fake.sources[parts.path], extra)
                        return self._reply(404, exception_xml(f"No route for {method} {parts.path}"), extra)

                if callable(reply):
                    reply = reply(req)
                if isinstance(reply, str):
                    reply = (200, reply)
                status, text = reply[0], reply[1]
                headers_out = reply[2] if len(reply) > 2 else {}
                return self._reply(status, text, {**extra, **headers_out})

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def do_PUT(self):
                self._handle("PUT")

            def do_DELETE(self):
                self._handle("DELETE")

        return Handler


@pytest.fixture
def sap():
    fake = FakeSAP()
    yield fake
    fake.close()


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("ABAP_CONFIG", str(tmp_path / "abap-config.json"))
    for var in ("ABAP_URL", "ABAP_USER", "ABAP_PASSWORD", "ABAP_CLIENT", "ABAP_LANGUAGE",
                "ABAP_PROFILE_NAME", "ABAP_ALLOW_UNAUTHORIZED"):
        monkeypatch.delenv(var, raising=False)
    config = sub("config")
    client = sub("client")
    config._reset_for_testing()
    yield tmp_path / "abap-config.json"
    client.LOCKS.release_all()
    config._reset_for_testing()


@pytest.fixture
def dev(sap):
    """Profile 'dev' pointing at the fake system, plus the class ZCL_DEMO and program ZDEMO_REPORT."""
    config = sub("config")
    models = sub("models")
    config.save_profile("dev", models.AbapProfile(url=sap.url, username="DEVELOPER", password="secret", client="100"))

    def search(req):
        query = (req.q("query") or "").upper()
        body = fixture("search.xml")
        if query.startswith("ZDEMO_REPORT"):
            body = fixture("search_program.xml")
        elif query.startswith("ZNOPE"):
            body = '<adtcore:objectReferences xmlns:adtcore="http://www.sap.com/adt/core"/>'
        return body

    sap.on("GET", "/sap/bc/adt/repository/informationsystem/search", search)
    sap.on("GET", "/sap/bc/adt/oo/classes/zcl_demo", fixture("class_structure.xml"))
    sap.on("GET", "/sap/bc/adt/programs/programs/zdemo_report", fixture("program_structure.xml"))
    sap.sources["/sap/bc/adt/oo/classes/zcl_demo/source/main"] = "CLASS zcl_demo DEFINITION.\nENDCLASS."
    sap.sources["/sap/bc/adt/programs/programs/zdemo_report/source/main"] = "REPORT zdemo_report."
    return sap


def call(tool: str, args: Optional[dict] = None) -> dict:
    """Invoke a tool handler the way Hermes does and decode its JSON result."""
    tools = sub("tools")
    handler = next(h for s, h in tools.TOOLS if s["name"] == tool)
    raw = handler(args or {}, task_id="t1", session_id="s1")
    assert isinstance(raw, str), "handlers must return a JSON string"
    return json.loads(raw)
