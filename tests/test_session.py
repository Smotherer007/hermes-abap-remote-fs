"""The ADT session: login, CSRF, cookies, stateful sessions, errors."""

from __future__ import annotations

import pytest

from conftest import exception_xml, sub

http = sub("adt_http")
models = sub("models")
xmlutil = sub("xmlutil")


def profile(sap, **kw):
    base = dict(url=sap.url, username="DEVELOPER", password="secret", client="100", language="EN")
    base.update(kw)
    return models.AbapProfile(**base)


class TestLogin:
    def test_fetches_a_token_and_passes_client_and_language(self, sap):
        session = http.AdtSession(profile(sap))
        session.login()
        login = sap.calls[0]
        assert login.path == "/sap/bc/adt/compatibility/graph"
        assert login.q("sap-client") == "100" and login.q("sap-language") == "EN"
        assert login.headers["x-csrf-token"] == "fetch"
        assert session.csrf_token == f"TOKEN-{login.session}"
        assert "SAP_SESSIONID" in session.cookies

    def test_later_requests_carry_token_cookie_and_session_type(self, sap):
        sap.on("POST", "/sap/bc/adt/activation", "")
        session = http.AdtSession(profile(sap), stateful=True)
        session.request("/sap/bc/adt/activation", method="POST", body="<x/>")
        post = sap.last("POST", "/sap/bc/adt/activation")
        assert post.headers["x-csrf-token"].startswith("TOKEN-")
        assert post.headers["X-sap-adt-sessiontype"] == "stateful"
        assert post.session == sap.calls[0].session  # same server session as the login

    def test_wrong_password_is_explained(self, sap):
        session = http.AdtSession(profile(sap, password="wrong"))
        with pytest.raises(models.AdtError, match=r"rejected the logon of DEVELOPER in client 100 \(401\)"):
            session.login()

    def test_stateless_request_logs_in_again_after_a_lost_session(self, sap):
        sap.on("POST", "/sap/bc/adt/activation", "")
        session = http.AdtSession(profile(sap))
        session.login()
        session.csrf_token = "EXPIRED"
        session.request("/sap/bc/adt/activation", method="POST", body="<x/>")
        assert [c.path for c in sap.calls].count("/sap/bc/adt/compatibility/graph") == 2

    def test_stateful_request_never_silently_logs_in_again(self, sap):
        sap.on("POST", "/sap/bc/adt/activation", "")
        session = http.AdtSession(profile(sap), stateful=True)
        session.login()
        session.csrf_token = "EXPIRED"
        with pytest.raises(models.AdtError) as err:
            session.request("/sap/bc/adt/activation", method="POST", body="<x/>")
        assert err.value.is_login_error

    def test_drop_session_ends_the_stateful_context(self, sap):
        session = http.AdtSession(profile(sap), stateful=True)
        session.login()
        assert sap.open_stateful_sessions() == 1
        session.drop_session()
        assert sap.open_stateful_sessions() == 0
        assert sap.calls[-1].headers["X-sap-adt-sessiontype"] == "stateless"


class TestErrors:
    def test_sap_exception_message_reaches_the_model(self, sap):
        sap.on("GET", "/sap/bc/adt/oo/classes/zcl_x", (404, exception_xml("Resource ZCL_X does not exist")))
        session = http.AdtSession(profile(sap))
        with pytest.raises(models.AdtError, match="Resource ZCL_X does not exist") as err:
            session.request("/sap/bc/adt/oo/classes/zcl_x")
        assert err.value.status == 404
        assert err.value.kind == "ExceptionResourceNotFound"
        assert err.value.properties == {"T100KEY-ID": "SADT_REST"}

    def test_html_error_page_hints_at_the_url(self, sap):
        sap.on("GET", "/sap/bc/adt/x/y", (500, "<html><body>ICM error</body></html>"))
        session = http.AdtSession(profile(sap))
        with pytest.raises(models.AdtError, match="HTML page"):
            session.request("/sap/bc/adt/x/y")

    def test_unreachable_host(self):
        session = http.AdtSession(models.AbapProfile(url="http://127.0.0.1:9", username="U", password="P"),
                                  timeout=2)
        with pytest.raises(models.AdtError, match="Could not reach SAP at http://127.0.0.1:9"):
            session.login()

    def test_redirect_is_reported_not_followed(self, sap):
        sap.on("GET", "/sap/bc/adt/redirect/me", (302, "", {"Location": "/sap/public/login"}))
        session = http.AdtSession(profile(sap))
        with pytest.raises(models.AdtError, match="redirected to /sap/public/login"):
            session.request("/sap/bc/adt/redirect/me")


class TestQueryString:
    def test_booleans_none_and_encoding(self):
        qs = http._format_qs({"a": True, "b": None, "c": "x y/z", "d": ["1", "2"]})
        assert qs == "a=true&c=x%20y%2Fz&d=1&d=2"

    def test_insecure_tls_is_per_profile(self):
        assert http._ssl_context(models.AbapProfile(url="https://x", username="u", password="p")) is None
        ctx = http._ssl_context(models.AbapProfile(url="https://x", username="u", password="p",
                                                   allow_unauthorized=True))
        assert ctx is not None and ctx.check_hostname is False


class TestXml:
    def test_prefixes_attributes_lists_and_text(self):
        parsed = xmlutil.parse(
            '<a:root xmlns:a="urn:a" a:id="1"><a:item>x</a:item><a:item k="v">y</a:item><a:empty/></a:root>'
        )
        root = parsed["a:root"]
        assert root["@_a:id"] == "1"
        assert root["a:item"] == ["x", {"@_k": "v", "#text": "y"}]
        assert root["a:empty"] == ""
        assert "@_xmlns:a" not in root

    def test_remove_ns(self):
        parsed = xmlutil.parse('<a:root xmlns:a="urn:a" a:id="1"><a:item>x</a:item></a:root>', remove_ns=True)
        assert parsed == {"root": {"@_id": "1", "item": "x"}}

    def test_array_and_flat_array(self):
        data = {"p": [{"c": {"t": 1}}, {"c": [{"t": 2}, {"t": 3}]}]}
        assert xmlutil.array(data, "p") == data["p"]
        assert xmlutil.array({"x": ""}, "x") == []
        assert xmlutil.flat_array(data, "p", "c", "t") == [1, 2, 3]

    @pytest.mark.parametrize("base, extra, expected", [
        ("/sap/bc/adt/oo/classes/zcl_a", "./zcl_a/source/main", "/sap/bc/adt/oo/classes/zcl_a/source/main"),
        ("/sap/bc/adt/programs/programs/zp", "source/main", "/sap/bc/adt/programs/programs/zp/source/main"),
        ("/sap/bc/adt/programs/programs/zp", "/source/main", "/sap/bc/adt/programs/programs/zp/source/main"),
    ])
    def test_follow_url(self, base, extra, expected):
        assert xmlutil.follow_url(base, extra) == expected
