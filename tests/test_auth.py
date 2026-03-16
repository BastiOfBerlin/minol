"""Tests for minol.auth: authenticate(), session cache, and auth steps."""

import base64
import json
import os
import struct
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch, call

from minol._http import HttpResponse, HttpSession
from minol import auth


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_resp(status=200, text="", headers=None, url="https://x.com/"):
    return HttpResponse(status, text, headers or {}, url)


def _build_ticket(timestamp="202503141200", valid_hours=8):
    header = bytes([2]) + b"4100" + b" "
    field0 = struct.pack(">H", 9) + b"PORTAL_ID"
    ts = timestamp.encode("ascii")
    field04 = bytes([0x04]) + struct.pack(">H", len(ts)) + ts
    field05 = bytes([0x05]) + struct.pack(">H", 4) + struct.pack(">I", valid_hours)
    raw = header + field0 + field04 + field05
    return base64.b64encode(raw).decode()


def _session_with_sso2(ticket_value=None):
    """Return an HttpSession that has a MYSAPSSO2 cookie."""
    from http.cookiejar import Cookie
    session = HttpSession()
    if ticket_value is not None:
        cookie = Cookie(
            version=0, name="MYSAPSSO2", value=ticket_value,
            port=None, port_specified=False,
            domain="webservices.minol.com", domain_specified=True,
            domain_initial_dot=False,
            path="/", path_specified=True,
            secure=False, expires=None, discard=True,
            comment=None, comment_url=None, rest={},
        )
        session.cookie_jar.set_cookie(cookie)
    return session


# ── _mask_email ────────────────────────────────────────────────────────────────

class TestMaskEmail(unittest.TestCase):

    def test_normal_email_masked(self):
        # regex inserts *** after first char; rest of local part is preserved
        self.assertEqual(auth._mask_email("john@example.com"), "j***ohn@example.com")

    def test_no_at_sign_returns_stars(self):
        self.assertEqual(auth._mask_email("notanemail"), "***")

    def test_single_char_local(self):
        self.assertEqual(auth._mask_email("a@b.com"), "a***@b.com")


# ── _build_cache_data ──────────────────────────────────────────────────────────

class TestBuildCacheData(unittest.TestCase):

    def test_returns_dict_with_required_keys(self):
        ticket = _build_ticket(valid_hours=8)
        session = _session_with_sso2(ticket)
        result = auth._build_cache_data(session, "000000000001")
        self.assertIn("user_num", result)
        self.assertIn("expires_at", result)
        self.assertIn("cookies", result)
        self.assertIn("saved_at", result)
        self.assertEqual(result["user_num"], "000000000001")

    def test_cookies_list_contains_mysapsso2(self):
        ticket = _build_ticket(valid_hours=8)
        session = _session_with_sso2(ticket)
        result = auth._build_cache_data(session, "000000000001")
        names = [c["name"] for c in result["cookies"]]
        self.assertIn("MYSAPSSO2", names)

    def test_expires_at_is_none_when_no_mysapsso2(self):
        session = _session_with_sso2(None)
        with self.assertLogs("minol.auth", level="WARNING"):
            result = auth._build_cache_data(session, "000000000001")
        self.assertIsNone(result["expires_at"])


# ── _load_cache_data ───────────────────────────────────────────────────────────

class TestLoadCacheData(unittest.TestCase):

    def _valid_cache(self, user_num="000000000001", hours_ahead=12):
        ticket = _build_ticket(valid_hours=24)
        future = (datetime.now() + timedelta(hours=hours_ahead)).isoformat()
        return {
            "user_num": user_num,
            "expires_at": future,
            "cookies": [
                {"name": "MYSAPSSO2", "value": ticket,
                 "domain": "webservices.minol.com", "path": "/",
                 "secure": False, "expires": None},
            ],
            "saved_at": datetime.now().isoformat(),
        }

    def test_valid_cache_loads_cookies_and_returns_true(self):
        session = HttpSession()
        result = auth._load_cache_data(session, "000000000001", self._valid_cache())
        self.assertTrue(result)
        self.assertIsNotNone(session.get_cookie("MYSAPSSO2"))

    def test_wrong_user_num_returns_false(self):
        session = HttpSession()
        cache = self._valid_cache(user_num="OTHER_USER")
        result = auth._load_cache_data(session, "000000000001", cache)
        self.assertFalse(result)

    def test_expired_returns_false(self):
        session = HttpSession()
        cache = self._valid_cache(hours_ahead=-1)
        result = auth._load_cache_data(session, "000000000001", cache)
        self.assertFalse(result)

    def test_missing_expires_at_returns_false(self):
        session = HttpSession()
        cache = {"user_num": "000000000001", "cookies": []}
        result = auth._load_cache_data(session, "000000000001", cache)
        self.assertFalse(result)

    def test_no_mysapsso2_in_cookies_returns_false(self):
        session = HttpSession()
        future = (datetime.now() + timedelta(hours=12)).isoformat()
        cache = {
            "user_num": "000000000001",
            "expires_at": future,
            "cookies": [
                {"name": "other", "value": "val",
                 "domain": "webservices.minol.com", "path": "/",
                 "secure": False, "expires": None},
            ],
        }
        result = auth._load_cache_data(session, "000000000001", cache)
        self.assertFalse(result)

    def test_status_fn_called_on_success(self):
        session = HttpSession()
        messages = []
        auth._load_cache_data(session, "000000000001", self._valid_cache(),
                              status_fn=messages.append)
        self.assertTrue(any("cached" in m.lower() for m in messages))


# ── _restore_session_data ──────────────────────────────────────────────────────

class TestRestoreSessionData(unittest.TestCase):

    def test_valid_dict_restores_session(self):
        ticket = _build_ticket(valid_hours=24)
        future = (datetime.now() + timedelta(hours=12)).isoformat()
        session = HttpSession()
        cache = {
            "user_num": "000000000001",
            "expires_at": future,
            "cookies": [
                {"name": "MYSAPSSO2", "value": ticket,
                 "domain": "webservices.minol.com", "path": "/",
                 "secure": False, "expires": None},
            ],
        }
        result = auth._restore_session_data(session, "000000000001", cache)
        self.assertTrue(result)
        self.assertIsNotNone(session.get_cookie("MYSAPSSO2"))

    def test_empty_dict_returns_false(self):
        session = HttpSession()
        result = auth._restore_session_data(session, "000000000001", {})
        self.assertFalse(result)


# ── _restore_session ───────────────────────────────────────────────────────────

class TestRestoreSession(unittest.TestCase):

    def _write_cache(self, path, data):
        path.write_text(json.dumps(data))

    def test_file_not_found_returns_false(self):
        session = HttpSession()
        result = auth._restore_session(session, "000000000001",
                                       Path("/nonexistent/path.json"))
        self.assertFalse(result)

    def test_valid_cache_restores_cookies(self):
        session = HttpSession()
        ticket = _build_ticket(valid_hours=24)
        future = (datetime.now() + timedelta(hours=12)).isoformat()

        cache = {
            "user_num": "000000000001",
            "expires_at": future,
            "cookies": [
                {"name": "MYSAPSSO2", "value": ticket,
                 "domain": "webservices.minol.com", "path": "/",
                 "secure": False, "expires": None},
            ],
            "saved_at": datetime.now().isoformat(),
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(cache, f)
            tmp = Path(f.name)

        try:
            result = auth._restore_session(session, "000000000001", tmp)
            self.assertTrue(result)
            self.assertIsNotNone(session.get_cookie("MYSAPSSO2"))
        finally:
            tmp.unlink(missing_ok=True)

    def test_expired_session_returns_false(self):
        session = HttpSession()
        past = (datetime.now() - timedelta(hours=1)).isoformat()
        cache = {
            "user_num": "000000000001",
            "expires_at": past,
            "cookies": [],
            "saved_at": datetime.now().isoformat(),
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(cache, f)
            tmp = Path(f.name)
        try:
            result = auth._restore_session(session, "000000000001", tmp)
            self.assertFalse(result)
        finally:
            tmp.unlink(missing_ok=True)

    def test_wrong_user_num_returns_false(self):
        session = HttpSession()
        future = (datetime.now() + timedelta(hours=12)).isoformat()
        cache = {
            "user_num": "OTHER_USER",
            "expires_at": future,
            "cookies": [],
            "saved_at": datetime.now().isoformat(),
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(cache, f)
            tmp = Path(f.name)
        try:
            result = auth._restore_session(session, "000000000001", tmp)
            self.assertFalse(result)
        finally:
            tmp.unlink(missing_ok=True)

    def test_corrupt_json_returns_false(self):
        session = HttpSession()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("{not valid json{{")
            tmp = Path(f.name)
        try:
            result = auth._restore_session(session, "000000000001", tmp)
            self.assertFalse(result)
        finally:
            tmp.unlink(missing_ok=True)

    def test_missing_expires_at_returns_false(self):
        session = HttpSession()
        cache = {
            "user_num": "000000000001",
            "cookies": [],
            "saved_at": datetime.now().isoformat(),
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(cache, f)
            tmp = Path(f.name)
        try:
            result = auth._restore_session(session, "000000000001", tmp)
            self.assertFalse(result)
        finally:
            tmp.unlink(missing_ok=True)

    def test_no_mysapsso2_in_restored_cookies_returns_false(self):
        """Valid cache, valid expiry, but MYSAPSSO2 cookie not present → False."""
        session = HttpSession()
        future = (datetime.now() + timedelta(hours=12)).isoformat()
        cache = {
            "user_num": "000000000001",
            "expires_at": future,
            "cookies": [
                {"name": "other_cookie", "value": "val",
                 "domain": "webservices.minol.com", "path": "/",
                 "secure": False, "expires": None},
            ],
            "saved_at": datetime.now().isoformat(),
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(cache, f)
            tmp = Path(f.name)
        try:
            result = auth._restore_session(session, "000000000001", tmp)
            self.assertFalse(result)
        finally:
            tmp.unlink(missing_ok=True)

    def test_status_fn_called_on_cache_hit(self):
        session = HttpSession()
        ticket = _build_ticket(valid_hours=24)
        future = (datetime.now() + timedelta(hours=12)).isoformat()
        cache = {
            "user_num": "000000000001",
            "expires_at": future,
            "cookies": [
                {"name": "MYSAPSSO2", "value": ticket,
                 "domain": "webservices.minol.com", "path": "/",
                 "secure": False, "expires": None},
            ],
            "saved_at": datetime.now().isoformat(),
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(cache, f)
            tmp = Path(f.name)
        messages = []
        try:
            auth._restore_session(session, "000000000001", tmp, status_fn=messages.append)
            self.assertTrue(any("cached" in m.lower() for m in messages))
        finally:
            tmp.unlink(missing_ok=True)


# ── _save_session ──────────────────────────────────────────────────────────────

class TestSaveSession(unittest.TestCase):

    def test_writes_json_with_correct_perms(self):
        ticket = _build_ticket(valid_hours=8)
        session = _session_with_sso2(ticket)

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "session.json"
            auth._save_session(session, "000000000001", path)

            # File exists and is readable
            self.assertTrue(path.is_file())
            data = json.loads(path.read_text())
            self.assertEqual(data["user_num"], "000000000001")
            self.assertIn("cookies", data)
            self.assertIn("expires_at", data)

            # File mode is 0o600
            mode = os.stat(path).st_mode & 0o777
            self.assertEqual(mode, 0o600)

    def test_save_session_oserror_logs_warning(self):
        """OSError during os.open is caught and logged as warning."""
        session = _session_with_sso2(_build_ticket())
        path = Path("/nonexistent/dir/session.json")
        with self.assertLogs("minol.auth", level="WARNING") as log:
            auth._save_session(session, "000000000001", path)
        self.assertTrue(any("Could not save session cache" in msg for msg in log.output))

    def test_status_fn_called_after_save(self):
        session = _session_with_sso2(_build_ticket())
        messages = []
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "session.json"
            auth._save_session(session, "000000000001", path, status_fn=messages.append)
        self.assertTrue(any("cached" in m.lower() or "session" in m.lower()
                            for m in messages))


# ── authenticate() ─────────────────────────────────────────────────────────────

class TestAuthenticate(unittest.IsolatedAsyncioTestCase):

    def _patch_steps(self, session, ticket=None):
        """Patch all 6 SAML steps and _save_session so authenticate() won't do real I/O."""
        step1 = patch("minol.auth._step1_portal_entry", return_value=_make_resp())
        step2 = patch("minol.auth._step2_trigger_saml", return_value=("https://b2c.example.com/", "B2C_1A_POLICY"))
        step3 = patch("minol.auth._step3_load_b2c_login", return_value=("csrf_tok", "StateProperties=x", "<html/>"))
        step4 = patch("minol.auth._step4_submit_credentials", return_value=_make_resp())
        step5 = patch("minol.auth._step5_get_saml_response", return_value=("https://sap.example.com/acs", {"SAMLResponse": "data"}))
        step6 = patch("minol.auth._step6_post_to_sap_acs", return_value=None)
        save = patch("minol.auth._save_session")
        return step1, step2, step3, step4, step5, step6, save

    async def test_cache_hit_skips_saml(self):
        session = HttpSession()
        with patch("minol.auth._restore_session", return_value=True) as mock_restore:
            with patch("minol.auth._step1_portal_entry") as mock_step1:
                with tempfile.TemporaryDirectory() as d:
                    await auth.authenticate(session, "u@x.com", "pass", "000000000001",
                                            session_path=Path(d) / "s.json")
        mock_restore.assert_called_once()
        mock_step1.assert_not_called()

    async def test_cache_miss_runs_all_six_steps(self):
        session = HttpSession()
        patches = self._patch_steps(session)
        with patch("minol.auth._restore_session", return_value=False):
            with patches[0] as s1, patches[1] as s2, patches[2] as s3, \
                 patches[3] as s4, patches[4] as s5, patches[5] as s6, patches[6] as save:
                with tempfile.TemporaryDirectory() as d:
                    await auth.authenticate(session, "u@x.com", "pass", "000000000001",
                                            session_path=Path(d) / "s.json")
        s1.assert_called_once()
        s2.assert_called_once()
        s3.assert_called_once()
        s4.assert_called_once()
        s5.assert_called_once()
        s6.assert_called_once()
        save.assert_called_once()

    async def test_use_cache_false_skips_restore_and_save(self):
        session = HttpSession()
        patches = self._patch_steps(session)
        with patch("minol.auth._restore_session") as mock_restore:
            with patches[0] as s1, patches[1] as s2, patches[2] as s3, \
                 patches[3] as s4, patches[4] as s5, patches[5] as s6, patches[6] as save:
                with tempfile.TemporaryDirectory() as d:
                    await auth.authenticate(session, "u@x.com", "pass", "000000000001",
                                            use_cache=False,
                                            session_path=Path(d) / "s.json")
        mock_restore.assert_not_called()
        save.assert_not_called()

    async def test_status_fn_called_with_progress(self):
        session = HttpSession()
        messages = []
        patches = self._patch_steps(session)
        with patch("minol.auth._restore_session", return_value=False):
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
                with tempfile.TemporaryDirectory() as d:
                    await auth.authenticate(session, "u@x.com", "pass", "000000000001",
                                            status_fn=messages.append,
                                            session_path=Path(d) / "s.json")
        self.assertTrue(len(messages) >= 2)
        self.assertTrue(any("authenticat" in m.lower() for m in messages))
        self.assertTrue(any("success" in m.lower() for m in messages))

    async def test_session_data_cache_hit_skips_saml_and_file(self):
        """When session_data is provided and cache is valid, SAML steps and file I/O are skipped."""
        session = HttpSession()
        with patch("minol.auth._restore_session_data", return_value=True) as mock_restore:
            with patch("minol.auth._step1_portal_entry") as mock_step1:
                with patch("minol.auth._save_session") as mock_save:
                    result = await auth.authenticate(
                        session, "u@x.com", "pass", "000000000001",
                        session_data={"user_num": "000000000001"},
                    )
        mock_restore.assert_called_once()
        mock_step1.assert_not_called()
        mock_save.assert_not_called()
        self.assertEqual(result, {"user_num": "000000000001"})

    async def test_session_data_cache_miss_returns_dict_no_file_write(self):
        """When session_data is provided but expired, fresh login runs and cache dict is returned."""
        session = HttpSession()
        patches = self._patch_steps(session)
        cache_dict = {"user_num": "000000000001", "expires_at": "2099-01-01T00:00:00+00:00", "cookies": []}
        with patch("minol.auth._restore_session_data", return_value=False):
            with patch("minol.auth._build_cache_data", return_value=cache_dict) as mock_build:
                with patches[0] as s1, patches[1] as s2, patches[2] as s3, \
                     patches[3] as s4, patches[4] as s5, patches[5] as s6, patches[6] as save:
                    result = await auth.authenticate(
                        session, "u@x.com", "pass", "000000000001",
                        session_data={},
                    )
        s1.assert_called_once()
        s6.assert_called_once()
        save.assert_not_called()
        mock_build.assert_called_once()
        self.assertEqual(result, cache_dict)

    async def test_session_data_use_cache_false_skips_restore_returns_dict(self):
        """session_data + use_cache=False: skip restore, fresh login, return dict."""
        session = HttpSession()
        patches = self._patch_steps(session)
        cache_dict = {"user_num": "000000000001", "cookies": []}
        with patch("minol.auth._restore_session_data") as mock_restore:
            with patch("minol.auth._build_cache_data", return_value=cache_dict):
                with patches[0], patches[1], patches[2], patches[3], \
                     patches[4], patches[5], patches[6] as save:
                    result = await auth.authenticate(
                        session, "u@x.com", "pass", "000000000001",
                        use_cache=False, session_data={},
                    )
        mock_restore.assert_not_called()
        save.assert_not_called()
        self.assertEqual(result, cache_dict)

    async def test_file_mode_fresh_login_returns_none(self):
        """Without session_data (file mode), fresh login returns None."""
        session = HttpSession()
        patches = self._patch_steps(session)
        with patch("minol.auth._restore_session", return_value=False):
            with patches[0], patches[1], patches[2], patches[3], \
                 patches[4], patches[5], patches[6]:
                with tempfile.TemporaryDirectory() as d:
                    result = await auth.authenticate(
                        session, "u@x.com", "pass", "000000000001",
                        session_path=Path(d) / "s.json",
                    )
        self.assertIsNone(result)


# ── Auth step unit tests ───────────────────────────────────────────────────────

class TestStep1PortalEntry(unittest.IsolatedAsyncioTestCase):

    async def test_gets_portal_entry_url(self):
        session = MagicMock()
        session.get = AsyncMock(return_value=_make_resp(200))
        session.cookie_names.return_value = []
        await auth._step1_portal_entry(session)
        session.get.assert_called_once()
        url = session.get.call_args[0][0]
        self.assertIn("redirect2=true", url)


class TestStep2TriggerSaml(unittest.IsolatedAsyncioTestCase):

    async def test_extracts_policy_from_302(self):
        session = MagicMock()
        b2c_url = "https://minolauth.b2clogin.com/minolauth.onmicrosoft.com/B2C_1A_SIGNUP_SIGNIN/samlp/sso/login?foo=bar"
        session.get = AsyncMock(return_value=_make_resp(302, headers={"location": b2c_url}))
        url, policy = await auth._step2_trigger_saml(session)
        self.assertEqual(url, b2c_url)
        self.assertEqual(policy, "B2C_1A_SIGNUP_SIGNIN")

    async def test_raises_on_non_302(self):
        session = MagicMock()
        session.get = AsyncMock(return_value=_make_resp(200))
        with self.assertRaises(RuntimeError):
            await auth._step2_trigger_saml(session)

    async def test_raises_if_no_policy_in_url(self):
        session = MagicMock()
        session.get = AsyncMock(return_value=_make_resp(302, headers={"location": "https://b2c.example.com/no-policy-here"}))
        with self.assertRaises(RuntimeError):
            await auth._step2_trigger_saml(session)


class TestStep3LoadB2cLogin(unittest.IsolatedAsyncioTestCase):

    async def test_extracts_csrf_and_state(self):
        session = MagicMock()
        page_html = '<html><script>var x = "StateProperties=ABCDEF123";</script></html>'
        session.get = AsyncMock(return_value=_make_resp(200, text=page_html))
        session.get_cookie.return_value = "csrf_token_value"
        csrf, tx, html = await auth._step3_load_b2c_login(session, "https://b2c.example.com/")
        self.assertEqual(csrf, "csrf_token_value")
        self.assertIn("StateProperties=", tx)
        self.assertEqual(html, page_html)

    async def test_raises_if_no_csrf_cookie(self):
        session = MagicMock()
        session.get = AsyncMock(return_value=_make_resp(200, text="<html/>"))
        session.get_cookie.return_value = None
        with self.assertRaises(RuntimeError):
            await auth._step3_load_b2c_login(session, "https://b2c.example.com/")


class TestStep4SubmitCredentials(unittest.IsolatedAsyncioTestCase):

    async def test_posts_credentials_successfully(self):
        session = MagicMock()
        resp_200_ok = _make_resp(200, text=json.dumps({"status": "200"}))
        session.post = AsyncMock(return_value=resp_200_ok)
        # Should not raise
        await auth._step4_submit_credentials(
            session, "B2C_1A_POLICY", "user@x.com", "pass",
            "csrf_tok", "StateProperties=X", "<html/>"
        )
        session.post.assert_called_once()

    async def test_raises_on_status_400_json(self):
        session = MagicMock()
        session.post = AsyncMock(return_value=_make_resp(200, text=json.dumps({"status": "400", "message": "Bad creds"})))
        with self.assertRaises(RuntimeError, msg="Bad creds"):
            await auth._step4_submit_credentials(
                session, "B2C_1A_POLICY", "user@x.com", "wrongpass",
                "csrf_tok", "StateProperties=X", "<html/>"
            )

    async def test_raises_on_http_error(self):
        session = MagicMock()
        session.post = AsyncMock(return_value=_make_resp(500, text="Server Error"))
        with self.assertRaises(RuntimeError):
            await auth._step4_submit_credentials(
                session, "B2C_1A_POLICY", "user@x.com", "pass",
                "csrf_tok", "StateProperties=X", "<html/>"
            )


class TestStep5GetSamlResponse(unittest.IsolatedAsyncioTestCase):

    async def test_extracts_saml_form(self):
        session = MagicMock()
        html = (
            '<form action="https://sap.example.com/saml/acs">'
            '<input name="SAMLResponse" value="samlbase64data"/>'
            '<input name="RelayState" value="relay"/>'
            '</form>'
        )
        session.get = AsyncMock(return_value=_make_resp(200, text=html))
        acs_url, fields = await auth._step5_get_saml_response(
            session, "B2C_1A_POLICY", "csrf", "StateProperties=X"
        )
        self.assertEqual(acs_url, "https://sap.example.com/saml/acs")
        self.assertIn("SAMLResponse", fields)
        self.assertEqual(fields["SAMLResponse"], "samlbase64data")

    async def test_raises_if_no_form(self):
        session = MagicMock()
        session.get = AsyncMock(return_value=_make_resp(200, text="<html>no form here</html>"))
        with self.assertRaises(RuntimeError):
            await auth._step5_get_saml_response(
                session, "B2C_1A_POLICY", "csrf", "StateProperties=X"
            )

    async def test_raises_if_form_has_no_saml_response(self):
        session = MagicMock()
        html = '<form action="/x"><input name="other" value="val"/></form>'
        session.get = AsyncMock(return_value=_make_resp(200, text=html))
        with self.assertRaises(RuntimeError):
            await auth._step5_get_saml_response(
                session, "B2C_1A_POLICY", "csrf", "StateProperties=X"
            )


class TestStep6PostToSapAcs(unittest.IsolatedAsyncioTestCase):

    async def test_raises_if_no_mysapsso2_cookie(self):
        session = MagicMock()
        session.post = AsyncMock(return_value=_make_resp(302, headers={"location": "/portal"}))
        session.get = AsyncMock(return_value=_make_resp(200))
        session.get_cookie.return_value = None
        session.all_cookies.return_value = []
        with self.assertRaises(RuntimeError):
            await auth._step6_post_to_sap_acs(session, "https://sap.example.com/acs",
                                              {"SAMLResponse": "data"})

    async def test_success_with_chained_form(self):
        """200 response with chained form → follows the chain, ends with MYSAPSSO2."""
        session = MagicMock()
        chained_html = (
            '<form action="/portal/login">'
            '<input name="sap-token" value="TOKEN"/>'
            '</form>'
        )
        session.post = AsyncMock(side_effect=[
            _make_resp(200, text=chained_html),           # first POST to ACS
            _make_resp(302, headers={"location": "/irj/portal"}),  # chained POST
        ])
        session.get = AsyncMock(return_value=_make_resp(200, text="<html>Portal</html>"))
        session.get_cookie.return_value = "TICKET_VALUE"  # MYSAPSSO2 found

        # Should not raise
        await auth._step6_post_to_sap_acs(session, "https://sap.example.com/acs",
                                          {"SAMLResponse": "data"})
        session.get_cookie.assert_called()

    async def test_success_with_direct_302(self):
        """302 response directly → follows redirect, ends with MYSAPSSO2."""
        session = MagicMock()
        session.post = AsyncMock(return_value=_make_resp(302, headers={"location": "/irj/portal"}))
        session.get = AsyncMock(return_value=_make_resp(200, text="<html>Portal</html>"))
        session.get_cookie.return_value = "TICKET_VALUE"

        await auth._step6_post_to_sap_acs(session, "https://sap.example.com/acs",
                                          {"SAMLResponse": "data"})
        session.get_cookie.assert_called()


if __name__ == "__main__":
    unittest.main()
