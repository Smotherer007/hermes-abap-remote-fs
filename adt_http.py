"""ADT HTTP session: basic auth, cookies, CSRF token, stateless/stateful.

Mirrors abap-adt-api's ``AdtHTTP``:

* ``login()`` is a GET on ``/sap/bc/adt/compatibility/graph`` with
  ``sap-client``/``sap-language`` in the query and ``x-csrf-token: fetch``.
  The token from the response is sent on every later request; the client
  and language live on in the session cookies.
* Every request carries ``X-sap-adt-sessiontype: stateful|stateless``.
  Locks only hold inside one *stateful* session, so lock, write and unlock
  must share a session.
* ``drop_session()`` switches back to stateless with one more request, which
  ends the stateful context on the server (and with it any locks).
* A stateless request that fails with 401 or a CSRF error logs in again once
  and retries. Stateful requests never do: a new login would be a new
  session, and the lock would be gone.

Cookies are kept by hand, name -> value, and always sent, like the original.
That avoids ``http.cookiejar`` refusing ``Secure`` cookies on the plain-HTTP
ports SAP development systems commonly use.
"""

from __future__ import annotations

import base64
import socket
import ssl
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, Mapping, Optional
from urllib.parse import quote, urlencode

from . import xmlutil
from .models import AbapProfile, AdtError

DEFAULT_TIMEOUT_S = 120.0
USER_AGENT = "hermes-abap-remote-fs/1.0.0"

_insecure_context: Optional[ssl.SSLContext] = None


def _ssl_context(profile: AbapProfile) -> Optional[ssl.SSLContext]:
    global _insecure_context
    if not profile.allow_unauthorized:
        return None
    if _insecure_context is None:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        _insecure_context = ctx
    return _insecure_context


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """ADT answers with data, not redirects; a redirect is usually a login page."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


@dataclass
class Response:
    status: int
    body: str
    headers: Mapping[str, str] = field(default_factory=dict)


def _format_qs(qs: Optional[Mapping[str, object]]) -> str:
    """Query string like axios: None dropped, booleans as true/false, lists repeated."""
    if not qs:
        return ""
    pairs = []
    for key, value in qs.items():
        if value is None:
            continue
        values = value if isinstance(value, (list, tuple)) else [value]
        for item in values:
            if isinstance(item, bool):
                item = "true" if item else "false"
            pairs.append((key, str(item)))
    return urlencode(pairs, quote_via=quote, safe="")


def parse_error(status: int, reason: str, body: str) -> AdtError:
    """Translate an error response (abap-adt-api's fromResponse)."""
    if not body:
        return AdtError(f"Error {status}:{reason}", status)
    if "CSRF" in body:
        return AdtError("CSRF token validation failed", status, kind="csrf")
    try:
        parsed = xmlutil.parse(body)
    except Exception:
        parsed = {}
    exc = parsed.get("exc:exception") if isinstance(parsed, dict) else None
    if not isinstance(exc, dict):
        if body.lstrip().startswith("<") and "<html" in body[:500].lower():
            return AdtError(
                f"Error {status}:{reason} (the server answered with an HTML page -- is the URL an ADT base URL?)",
                status,
            )
        snippet = " ".join(body.split())[:300]
        return AdtError(f"Error {status}:{reason} {snippet}".strip(), status)
    properties: Dict[str, str] = {}
    for entry in xmlutil.array(exc, "properties", "entry"):
        key = xmlutil.attrs(entry).get("key")
        if key:
            properties[key] = xmlutil.text(entry).strip()
    message = xmlutil.text(exc.get("localizedMessage")) or xmlutil.text(exc.get("message"))
    kind = xmlutil.attrs(exc.get("type")).get("id", "")
    return AdtError(message or f"Error {status}:{reason}", status, kind=kind, properties=properties)


class AdtSession:
    """One logical ADT connection. Not thread-safe on its own; callers serialise."""

    def __init__(self, profile: AbapProfile, stateful: bool = False, timeout: float = DEFAULT_TIMEOUT_S):
        self.profile = profile
        self.stateful = stateful
        self.timeout = timeout
        self.csrf_token: Optional[str] = None
        self.cookies: Dict[str, str] = {}
        self.lock = threading.RLock()
        handlers = [_NoRedirect()]
        ctx = _ssl_context(profile)
        if ctx is not None:
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        self._opener = urllib.request.build_opener(*handlers)
        auth = f"{profile.username}:{profile.password}".encode("utf-8")
        self._auth_header = "Basic " + base64.b64encode(auth).decode("ascii")

    @property
    def username(self) -> str:
        return self.profile.username

    @property
    def logged_in(self) -> bool:
        return self.csrf_token is not None

    # Public API

    def login(self) -> None:
        self.cookies.clear()
        self.csrf_token = None
        qs = {}
        if self.profile.client:
            qs["sap-client"] = self.profile.client
        if self.profile.language:
            qs["sap-language"] = self.profile.language
        try:
            self._send("GET", "/sap/bc/adt/compatibility/graph", qs=qs)
        except AdtError as err:
            if err.status == 401:
                client = f" in client {self.profile.client}" if self.profile.client else ""
                raise AdtError(
                    f"SAP rejected the logon of {self.profile.username}{client} (401). "
                    "Check user, password and client with abap_setup.",
                    401,
                ) from None
            raise
        if self.csrf_token is None:
            self.csrf_token = ""  # logged in, but the server handed out no token

    def drop_session(self) -> None:
        self.stateful = False
        self._send("GET", "/sap/bc/adt/compatibility/graph")

    def request(
        self,
        path: str,
        method: str = "GET",
        qs: Optional[Mapping[str, object]] = None,
        headers: Optional[Mapping[str, str]] = None,
        body: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> Response:
        with self.lock:
            auto_login = False
            if not self.logged_in:
                auto_login = True
                self.login()
            try:
                return self._send(method, path, qs=qs, headers=headers, body=body, timeout=timeout)
            except AdtError as err:
                # An expired ticket: log in again, unless that would silently
                # replace a stateful session (and drop its locks).
                if err.is_login_error and not auto_login and not self.stateful:
                    self.login()
                    return self._send(method, path, qs=qs, headers=headers, body=body, timeout=timeout)
                raise

    # Transport

    def _send(
        self,
        method: str,
        path: str,
        qs: Optional[Mapping[str, object]] = None,
        headers: Optional[Mapping[str, str]] = None,
        body: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> Response:
        query = _format_qs(qs)
        separator = "&" if "?" in path else "?"
        url = f"{self.profile.url}{path}{separator + query if query else ''}"

        merged: Dict[str, str] = {
            "Accept": "*/*",
            "Cache-Control": "no-cache",
            "x-csrf-token": self.csrf_token or "fetch",  # "" = no token issued
            "X-sap-adt-sessiontype": "stateful" if self.stateful else "stateless",
            "Authorization": self._auth_header,
            "User-Agent": USER_AGENT,
        }
        # Header names are case-insensitive; let the caller's spelling win.
        for key, value in (headers or {}).items():
            for existing in [k for k in merged if k.lower() == key.lower()]:
                del merged[existing]
            merged[key] = value
        if self.cookies:
            merged["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())

        data = body.encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method.upper(), headers=merged)
        try:
            with self._opener.open(request, timeout=timeout or self.timeout) as raw:
                response = Response(
                    status=raw.status,
                    body=raw.read().decode("utf-8", errors="replace"),
                    headers={k.lower(): v for k, v in raw.headers.items()},
                )
                self._store_cookies(raw.headers.get_all("Set-Cookie") or [])
        except urllib.error.HTTPError as err:
            self._store_cookies(err.headers.get_all("Set-Cookie") or [] if err.headers else [])
            try:
                text = err.read().decode("utf-8", errors="replace")
            except Exception:
                text = ""
            status = err.code
            reason = err.reason or ""
            lowered = {k.lower(): v for k, v in (err.headers.items() if err.headers else [])}
            if status in (301, 302, 303, 307, 308):
                raise AdtError(
                    f"The server redirected to {lowered.get('location', '?')} ({status}). "
                    "Check the ADT URL and the logon (a redirect usually means a login page).",
                    status,
                ) from None
            if (status == 403 and lowered.get("x-csrf-token", "").lower() == "required") or (
                status == 400 and reason == "Session timed out"
            ):
                raise AdtError("CSRF token required or session timed out", status, kind="csrf") from None
            raise parse_error(status, str(reason), text) from None
        except (socket.timeout, TimeoutError):
            raise AdtError(
                f"SAP did not respond within {round(timeout or self.timeout)}s ({self.profile.url}{path})."
            ) from None
        except urllib.error.URLError as err:
            if isinstance(err.reason, (socket.timeout, TimeoutError)):
                raise AdtError(
                    f"SAP did not respond within {round(timeout or self.timeout)}s ({self.profile.url}{path})."
                ) from None
            raise AdtError(f"Could not reach SAP at {self.profile.url}: {err.reason}") from None
        except OSError as err:
            raise AdtError(f"Could not reach SAP at {self.profile.url}: {err}") from None

        if self.csrf_token is None:
            token = response.headers.get("x-csrf-token")
            if token and token.lower() not in ("fetch", "required"):
                self.csrf_token = token
        return response

    def _store_cookies(self, raw_cookies) -> None:
        for cookie in raw_cookies:
            pair = cookie.split(";", 1)[0]
            if "=" not in pair:
                continue
            name, value = pair.split("=", 1)
            name = name.strip()
            if name:
                self.cookies[name] = value.strip()
