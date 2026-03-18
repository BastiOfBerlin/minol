"""Tests for minol._http: resolve_url(), HttpResponse, HttpSession."""

import json
import unittest
from unittest.mock import MagicMock, AsyncMock, patch

from yarl import URL

from minol._constants import PORTAL_BASE
from minol._http import HttpResponse, HttpSession, resolve_url


def _make_cookies(cookies: list[dict]) -> HttpSession:
    """Return an HttpSession pre-loaded with the given cookie dicts."""
    session = HttpSession()
    session.import_cookies(cookies)
    return session


def _make_cm(status=200, text="ok", headers=None, url="https://x.com/"):
    """Return (mock_aio_session, resp_mock, context_manager_mock).

    mock_aio.request(...)  returns cm
    async with cm as resp  yields resp
    resp.text()            returns text
    """
    resp = MagicMock()
    resp.status = status
    resp.text = AsyncMock(return_value=text)
    resp.headers = dict(headers or {})
    resp.url = URL(url)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)

    mock_aio = MagicMock()
    mock_aio.request = MagicMock(return_value=cm)
    return mock_aio, resp, cm


class TestResolveUrl(unittest.TestCase):

    def test_absolute_url_unchanged(self):
        url = "https://other.example.com/path"
        self.assertEqual(resolve_url(url), url)

    def test_relative_url_prepended_with_portal_base(self):
        self.assertEqual(resolve_url("/irj/portal"), f"{PORTAL_BASE}/irj/portal")

    def test_relative_url_with_query(self):
        self.assertEqual(resolve_url("/path?a=1"), f"{PORTAL_BASE}/path?a=1")

    def test_https_url_not_prefixed(self):
        url = "https://webservices.minol.com/already/absolute"
        self.assertEqual(resolve_url(url), url)


class TestHttpResponse(unittest.TestCase):

    def test_attributes_set_correctly(self):
        resp = HttpResponse(200, '{"ok": true}', {"content-type": "application/json"}, "https://x.com/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.text, '{"ok": true}')
        self.assertEqual(resp.headers, {"content-type": "application/json"})
        self.assertEqual(resp.url, "https://x.com/")

    def test_json_parses_text(self):
        resp = HttpResponse(200, '{"key": "value", "n": 42}', {}, "https://x.com/")
        data = resp.json()
        self.assertEqual(data, {"key": "value", "n": 42})

    def test_json_raises_on_invalid(self):
        resp = HttpResponse(200, "not-json", {}, "https://x.com/")
        with self.assertRaises(json.JSONDecodeError):
            resp.json()

    def test_headers_are_case_insensitive(self):
        from multidict import CIMultiDict
        resp = HttpResponse(302, "", CIMultiDict({"Location": "https://redirect.example.com/"}), "https://x.com/")
        self.assertEqual(resp.headers.get("location"), "https://redirect.example.com/")
        self.assertEqual(resp.headers.get("LOCATION"), "https://redirect.example.com/")


class TestHttpSessionCookies(unittest.TestCase):

    def test_get_cookie_returns_value(self):
        session = _make_cookies([
            {"name": "session_id", "value": "abc123", "domain": "example.com",
             "path": "/", "secure": False, "expires": None},
        ])
        self.assertEqual(session.get_cookie("session_id"), "abc123")

    def test_get_cookie_returns_none_if_missing(self):
        session = HttpSession()
        self.assertIsNone(session.get_cookie("nonexistent"))

    def test_get_cookie_with_domain_filter_matches(self):
        session = _make_cookies([
            {"name": "tok", "value": "val", "domain": "example.com",
             "path": "/", "secure": False, "expires": None},
        ])
        self.assertEqual(session.get_cookie("tok", domain="example.com"), "val")

    def test_get_cookie_with_domain_filter_no_match(self):
        session = _make_cookies([
            {"name": "tok", "value": "val", "domain": "other.com",
             "path": "/", "secure": False, "expires": None},
        ])
        self.assertIsNone(session.get_cookie("tok", domain="example.com"))

    def test_get_cookie_subdomain_matches(self):
        # aiohttp strips the leading dot from ".example.com" → "example.com"
        session = _make_cookies([
            {"name": "tok", "value": "val", "domain": ".example.com",
             "path": "/", "secure": False, "expires": None},
        ])
        self.assertEqual(session.get_cookie("tok", domain="example.com"), "val")

    def test_cookie_names_no_filter(self):
        session = _make_cookies([
            {"name": "alpha", "value": "1", "domain": "a.com",
             "path": "/", "secure": False, "expires": None},
            {"name": "beta", "value": "2", "domain": "b.com",
             "path": "/", "secure": False, "expires": None},
        ])
        names = session.cookie_names()
        self.assertIn("alpha", names)
        self.assertIn("beta", names)

    def test_cookie_names_with_domain_filter(self):
        session = _make_cookies([
            {"name": "alpha", "value": "1", "domain": "a.com",
             "path": "/", "secure": False, "expires": None},
            {"name": "beta", "value": "2", "domain": "b.com",
             "path": "/", "secure": False, "expires": None},
        ])
        names = session.cookie_names(domain="a.com")
        self.assertIn("alpha", names)
        self.assertNotIn("beta", names)

    def test_export_cookies_roundtrip(self):
        original = [
            {"name": "MYSAPSSO2", "value": "ticket", "domain": "webservices.minol.com",
             "path": "/", "secure": False, "expires": None},
        ]
        session = _make_cookies(original)
        exported = session.export_cookies()
        self.assertEqual(len(exported), 1)
        self.assertEqual(exported[0]["name"], "MYSAPSSO2")
        self.assertEqual(exported[0]["value"], "ticket")
        self.assertEqual(exported[0]["domain"], "webservices.minol.com")

    def test_clear_cookies_all(self):
        session = _make_cookies([
            {"name": "a", "value": "1", "domain": "foo.com",
             "path": "/", "secure": False, "expires": None},
            {"name": "b", "value": "2", "domain": "bar.com",
             "path": "/", "secure": False, "expires": None},
        ])
        session.clear_cookies()
        self.assertEqual(session.cookie_names(), [])

    def test_clear_cookies_by_domain(self):
        session = _make_cookies([
            {"name": "a", "value": "1", "domain": "foo.com",
             "path": "/", "secure": False, "expires": None},
            {"name": "b", "value": "2", "domain": "bar.com",
             "path": "/", "secure": False, "expires": None},
        ])
        session.clear_cookies("foo.com")
        names = session.cookie_names()
        self.assertNotIn("a", names)
        self.assertIn("b", names)


class TestHttpSessionRequests(unittest.IsolatedAsyncioTestCase):

    async def test_get_returns_http_response(self):
        session = HttpSession()
        mock_aio, _, _ = _make_cm(200, "hello")
        with patch.object(session, "_get_session", return_value=mock_aio):
            resp = await session.get("https://x.com/")
        self.assertIsInstance(resp, HttpResponse)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.text, "hello")

    async def test_post_with_data_string_encodes_utf8(self):
        session = HttpSession()
        mock_aio, _, cm = _make_cm(200, "{}")
        captured = {}

        def fake_request(method, url, **kwargs):
            captured["data"] = kwargs.get("data")
            return cm

        mock_aio.request = MagicMock(side_effect=fake_request)
        with patch.object(session, "_get_session", return_value=mock_aio):
            await session.post("https://x.com/", data="a=1&b=2")

        self.assertEqual(captured["data"], b"a=1&b=2")

    async def test_post_with_json_data_serializes_and_sets_content_type(self):
        session = HttpSession()
        mock_aio, _, cm = _make_cm(200, "{}")
        captured = {}

        def fake_request(method, url, **kwargs):
            captured["data"] = kwargs.get("data")
            captured["headers"] = kwargs.get("headers", {})
            return cm

        mock_aio.request = MagicMock(side_effect=fake_request)
        with patch.object(session, "_get_session", return_value=mock_aio):
            await session.post("https://x.com/", json_data={"key": "value"})

        self.assertEqual(json.loads(captured["data"]), {"key": "value"})
        content_type = captured["headers"].get("Content-Type", "")
        self.assertIn("application/json", content_type)

    async def test_post_with_no_body_sends_empty_bytes(self):
        session = HttpSession()
        mock_aio, _, cm = _make_cm(200, "{}")
        captured = {}

        def fake_request(method, url, **kwargs):
            captured["data"] = kwargs.get("data")
            return cm

        mock_aio.request = MagicMock(side_effect=fake_request)
        with patch.object(session, "_get_session", return_value=mock_aio):
            await session.post("https://x.com/")

        self.assertEqual(captured["data"], b"")

    async def test_allow_redirects_false_passes_kwarg(self):
        session = HttpSession()
        mock_aio, _, cm = _make_cm(302, headers={"location": "https://other.com/"})
        captured = {}

        def fake_request(method, url, **kwargs):
            captured["allow_redirects"] = kwargs.get("allow_redirects")
            return cm

        mock_aio.request = MagicMock(side_effect=fake_request)
        with patch.object(session, "_get_session", return_value=mock_aio):
            resp = await session.get("https://x.com/", allow_redirects=False)

        self.assertFalse(captured["allow_redirects"])
        self.assertEqual(resp.status_code, 302)

    async def test_non_200_status_returned_without_exception(self):
        session = HttpSession()
        mock_aio, _, _ = _make_cm(404, "Not Found")
        with patch.object(session, "_get_session", return_value=mock_aio):
            resp = await session.get("https://x.com/missing")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.text, "Not Found")

    async def test_get_passes_allow_redirects_true_by_default(self):
        session = HttpSession()
        mock_aio, _, cm = _make_cm(200, "body")
        captured = {}

        def fake_request(method, url, **kwargs):
            captured["allow_redirects"] = kwargs.get("allow_redirects")
            return cm

        mock_aio.request = MagicMock(side_effect=fake_request)
        with patch.object(session, "_get_session", return_value=mock_aio):
            await session.get("https://x.com/")

        self.assertTrue(captured["allow_redirects"])

    async def test_close_closes_owned_session(self):
        session = HttpSession()
        mock_aio = AsyncMock()
        session._aio_session = mock_aio
        await session.close()
        mock_aio.close.assert_called_once()
        self.assertIsNone(session._aio_session)

    async def test_close_does_not_close_injected_session(self):
        import aiohttp
        external = AsyncMock(spec=aiohttp.ClientSession)
        external.cookie_jar = HttpSession()._jar
        session = HttpSession(session=external)
        await session.close()
        external.close.assert_not_called()


class TestExtractCookiesFromHeaders(unittest.TestCase):

    def _make_response(self, set_cookie_headers: list[str], url: str = "https://b2c.example.com/") -> HttpResponse:
        from multidict import CIMultiDict
        headers = CIMultiDict()
        for v in set_cookie_headers:
            headers.add("Set-Cookie", v)
        return HttpResponse(200, "", headers, url)

    def test_extracts_single_b2c_style_cookie(self):
        session = HttpSession()
        resp = self._make_response([
            "x-ms-cpim-csrf=abc123; path=/; secure; HttpOnly; SameSite=None"
        ])
        count = session._extract_cookies_from_headers(resp)
        self.assertEqual(count, 1)
        self.assertEqual(session.get_cookie("x-ms-cpim-csrf"), "abc123")

    def test_extracts_multiple_cookies(self):
        session = HttpSession()
        resp = self._make_response([
            "x-ms-cpim-csrf=token1; path=/; secure",
            "x-ms-cpim-trans=eyJhIjoiYiJ9; path=/; secure",
        ])
        count = session._extract_cookies_from_headers(resp)
        self.assertEqual(count, 2)
        self.assertEqual(session.get_cookie("x-ms-cpim-csrf"), "token1")
        self.assertEqual(session.get_cookie("x-ms-cpim-trans"), "eyJhIjoiYiJ9")

    def test_returns_zero_when_no_set_cookie_headers(self):
        session = HttpSession()
        resp = self._make_response([])
        count = session._extract_cookies_from_headers(resp)
        self.assertEqual(count, 0)
        self.assertEqual(session.cookie_names(), [])

    def test_cookie_domain_defaults_to_response_url_host(self):
        session = HttpSession()
        resp = self._make_response(
            ["x-ms-cpim-csrf=abc; path=/"],
            url="https://login.b2c.example.com/path"
        )
        session._extract_cookies_from_headers(resp)
        # Cookie should be retrievable without domain filter
        self.assertEqual(session.get_cookie("x-ms-cpim-csrf"), "abc")

    def test_explicit_cookie_domain_used_when_present(self):
        session = HttpSession()
        resp = self._make_response([
            "tok=val; domain=.b2c.example.com; path=/"
        ])
        session._extract_cookies_from_headers(resp)
        self.assertEqual(session.get_cookie("tok", domain="b2c.example.com"), "val")


class TestGetFollowingRedirects(unittest.IsolatedAsyncioTestCase):

    def _make_redirect(self, location: str, set_cookie: str = None):
        from multidict import CIMultiDict
        hdrs = {"location": location}
        if set_cookie:
            hdrs["Set-Cookie"] = set_cookie
        return HttpResponse(302, "", CIMultiDict(hdrs), "https://b2c.example.com/")

    def _make_final(self, text: str = "ok", set_cookie: str = None):
        from multidict import CIMultiDict
        hdrs = {}
        if set_cookie:
            hdrs["Set-Cookie"] = set_cookie
        return HttpResponse(200, text, CIMultiDict(hdrs), "https://b2c.example.com/done")

    async def test_follows_single_redirect_and_returns_final(self):
        session = HttpSession()
        redirect = self._make_redirect("https://b2c.example.com/done")
        final = self._make_final("body")
        with patch.object(session, "_request", new=AsyncMock(side_effect=[redirect, final])):
            resp = await session.get_following_redirects("https://b2c.example.com/start")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.text, "body")

    async def test_extracts_cookies_from_intermediate_redirect(self):
        session = HttpSession()
        redirect = self._make_redirect(
            "https://b2c.example.com/done",
            set_cookie="x-ms-cpim-trans=txvalue123; domain=b2c.example.com; path=/; secure"
        )
        final = self._make_final()
        with patch.object(session, "_request", new=AsyncMock(side_effect=[redirect, final])):
            await session.get_following_redirects("https://b2c.example.com/start")
        self.assertEqual(session.get_cookie("x-ms-cpim-trans"), "txvalue123")

    async def test_returns_non_redirect_immediately(self):
        session = HttpSession()
        page = self._make_final("login-form")
        with patch.object(session, "_request", new=AsyncMock(return_value=page)) as mock_req:
            resp = await session.get_following_redirects("https://b2c.example.com/")
        self.assertEqual(resp.text, "login-form")
        mock_req.assert_called_once()

    async def test_stops_at_max_redirects(self):
        session = HttpSession()
        loop_resp = self._make_redirect("https://b2c.example.com/loop")
        with patch.object(session, "_request", new=AsyncMock(return_value=loop_resp)):
            resp = await session.get_following_redirects("https://b2c.example.com/loop",
                                                         max_redirects=3)
        self.assertEqual(resp.status_code, 302)


class TestDomainMatches(unittest.TestCase):

    def test_exact_domain_matches(self):
        self.assertTrue(HttpSession._domain_matches("example.com", "example.com"))

    def test_dot_prefixed_domain_matches(self):
        self.assertTrue(HttpSession._domain_matches(".example.com", "example.com"))

    def test_subdomain_matches(self):
        self.assertTrue(HttpSession._domain_matches("sub.example.com", "example.com"))

    def test_different_domain_no_match(self):
        self.assertFalse(HttpSession._domain_matches("other.com", "example.com"))

    def test_empty_domain_no_match(self):
        self.assertFalse(HttpSession._domain_matches("", "example.com"))


if __name__ == "__main__":
    unittest.main()
