"""Tests for minol._http: resolve_url(), HttpResponse, HttpSession."""

import json
import unittest
from http.cookiejar import Cookie
from unittest.mock import MagicMock, patch

from minol._constants import PORTAL_BASE
from minol._http import HttpResponse, HttpSession, resolve_url


def _make_cookie(name, value, domain="example.com", path="/"):
    return Cookie(
        version=0, name=name, value=value,
        port=None, port_specified=False,
        domain=domain, domain_specified=True,
        domain_initial_dot=domain.startswith("."),
        path=path, path_specified=True,
        secure=False, expires=None, discard=True,
        comment=None, comment_url=None, rest={},
    )


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


class TestHttpSessionCookies(unittest.TestCase):

    def _session_with_cookies(self, cookies):
        session = HttpSession()
        for c in cookies:
            session.cookie_jar.set_cookie(c)
        return session

    def test_get_cookie_returns_value(self):
        session = self._session_with_cookies([
            _make_cookie("session_id", "abc123"),
        ])
        self.assertEqual(session.get_cookie("session_id"), "abc123")

    def test_get_cookie_returns_none_if_missing(self):
        session = HttpSession()
        self.assertIsNone(session.get_cookie("nonexistent"))

    def test_get_cookie_with_domain_filter_matches(self):
        session = self._session_with_cookies([
            _make_cookie("tok", "val", domain="example.com"),
        ])
        self.assertEqual(session.get_cookie("tok", domain="example.com"), "val")

    def test_get_cookie_with_domain_filter_no_match(self):
        session = self._session_with_cookies([
            _make_cookie("tok", "val", domain="other.com"),
        ])
        self.assertIsNone(session.get_cookie("tok", domain="example.com"))

    def test_get_cookie_subdomain_matches(self):
        session = self._session_with_cookies([
            _make_cookie("tok", "val", domain=".example.com"),
        ])
        self.assertEqual(session.get_cookie("tok", domain="example.com"), "val")

    def test_cookie_names_no_filter(self):
        session = self._session_with_cookies([
            _make_cookie("alpha", "1", domain="a.com"),
            _make_cookie("beta", "2", domain="b.com"),
        ])
        names = session.cookie_names()
        self.assertIn("alpha", names)
        self.assertIn("beta", names)

    def test_cookie_names_with_domain_filter(self):
        session = self._session_with_cookies([
            _make_cookie("alpha", "1", domain="a.com"),
            _make_cookie("beta", "2", domain="b.com"),
        ])
        names = session.cookie_names(domain="a.com")
        self.assertIn("alpha", names)
        self.assertNotIn("beta", names)


class TestHttpSessionRequests(unittest.TestCase):

    def _fake_response(self, status=200, body=b"ok", headers=None, url="https://x.com/"):
        resp = MagicMock()
        resp.getcode.return_value = status
        resp.read.return_value = body
        resp.geturl.return_value = url
        hdr = MagicMock()
        hdr.items.return_value = list((headers or {}).items())
        resp.headers = hdr
        return resp

    def test_get_returns_http_response(self):
        session = HttpSession()
        fake = self._fake_response(200, b"hello", {}, "https://x.com/")
        with patch.object(session._opener, "open", return_value=fake) as mock_open:
            resp = session.get("https://x.com/")
        self.assertIsInstance(resp, HttpResponse)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.text, "hello")

    def test_post_with_data_string_encodes_utf8(self):
        session = HttpSession()
        captured = {}
        fake = self._fake_response(200, b"{}", {})

        def fake_open(req, timeout=None):
            captured["data"] = req.data
            return fake

        with patch.object(session._opener, "open", side_effect=fake_open):
            session.post("https://x.com/", data="a=1&b=2")

        self.assertEqual(captured["data"], b"a=1&b=2")

    def test_post_with_json_data_serializes_and_sets_content_type(self):
        session = HttpSession()
        captured = {}
        fake = self._fake_response(200, b"{}", {})

        def fake_open(req, timeout=None):
            captured["data"] = req.data
            captured["headers"] = dict(req.headers)
            return fake

        with patch.object(session._opener, "open", side_effect=fake_open):
            session.post("https://x.com/", json_data={"key": "value"})

        self.assertEqual(json.loads(captured["data"]), {"key": "value"})
        # Headers are title-cased by urllib Request
        content_type = captured["headers"].get("Content-type", "")
        self.assertIn("application/json", content_type)

    def test_post_with_no_body_sends_empty_bytes(self):
        session = HttpSession()
        captured = {}
        fake = self._fake_response(200, b"{}", {})

        def fake_open(req, timeout=None):
            captured["data"] = req.data
            return fake

        with patch.object(session._opener, "open", side_effect=fake_open):
            session.post("https://x.com/")

        self.assertEqual(captured["data"], b"")

    def test_allow_redirects_false_uses_no_redirect_opener(self):
        session = HttpSession()
        fake = self._fake_response(302, b"", {"location": "https://other.com/"})
        with patch.object(session._no_redirect_opener, "open", return_value=fake) as mock_open:
            resp = session.get("https://x.com/", allow_redirects=False)
        mock_open.assert_called_once()
        self.assertEqual(resp.status_code, 302)

    def test_http_error_returns_error_response(self):
        from urllib.error import HTTPError
        from io import BytesIO

        session = HttpSession()
        err_body = BytesIO(b"Not Found")
        hdr = MagicMock()
        hdr.items.return_value = []
        http_err = HTTPError("https://x.com/missing", 404, "Not Found", hdr, err_body)

        with patch.object(session._opener, "open", side_effect=http_err):
            resp = session.get("https://x.com/missing")

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.text, "Not Found")

    def test_get_uses_default_opener(self):
        session = HttpSession()
        fake = self._fake_response(200, b"body")
        with patch.object(session._opener, "open", return_value=fake) as mock_open:
            with patch.object(session._no_redirect_opener, "open") as mock_no_redir:
                session.get("https://x.com/")
        mock_open.assert_called_once()
        mock_no_redir.assert_not_called()


class TestDomainMatches(unittest.TestCase):

    def test_exact_domain_matches(self):
        from minol._http import HttpSession
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
