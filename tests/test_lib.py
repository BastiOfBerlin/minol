"""Tests for minol.lib: MinolScraper class."""

import json
import sys
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch, call

from minol._constants import CONSUMPTION_TYPES, DATA_ENDPOINT
from minol._http import HttpResponse
from minol.lib import MinolScraper


def _make_resp(status=200, data=None, text=None):
    body = text if text is not None else (json.dumps(data) if data is not None else "{}")
    return HttpResponse(status, body, {}, "https://x.com/")


# ── __init__ ───────────────────────────────────────────────────────────────────

class TestMinolScraperInit(unittest.TestCase):

    def test_stores_credentials(self):
        s = MinolScraper("a@b.com", "secret", "000000000001")
        self.assertEqual(s.email, "a@b.com")
        self.assertEqual(s.password, "secret")
        self.assertEqual(s.user_num, "000000000001")

    def test_authenticated_false_on_init(self):
        s = MinolScraper("a@b.com", "secret", "000000000001")
        self.assertFalse(s.authenticated)

    def test_default_status_fn_is_silent(self):
        s = MinolScraper("a@b.com", "secret", "000000000001")
        captured = StringIO()
        with patch("sys.stderr", captured):
            s._status("hello")
        self.assertEqual(captured.getvalue(), "")

    def test_custom_status_fn(self):
        messages = []
        s = MinolScraper("a@b.com", "secret", "000000000001", status_fn=messages.append)
        s._status("test message")
        self.assertEqual(messages, ["test message"])

    def test_session_injection_uses_injected_session(self):
        import aiohttp
        from minol._http import HttpSession
        external = MagicMock(spec=aiohttp.ClientSession)
        external.cookie_jar = HttpSession()._jar
        s = MinolScraper("a@b.com", "secret", "000000000001", session=external)
        self.assertIs(s.session._aio_session, external)
        self.assertFalse(s.session._owns_session)

    def test_no_session_owns_session(self):
        s = MinolScraper("a@b.com", "secret", "000000000001")
        self.assertTrue(s.session._owns_session)


# ── close() / async context manager ───────────────────────────────────────────

class TestMinolScraperLifecycle(unittest.IsolatedAsyncioTestCase):

    async def test_close_delegates_to_http_session(self):
        s = MinolScraper("a@b.com", "secret", "000000000001")
        with patch.object(s.session, "close", new_callable=AsyncMock) as mock_close:
            await s.close()
        mock_close.assert_called_once()

    async def test_async_context_manager_calls_close(self):
        s = MinolScraper("a@b.com", "secret", "000000000001")
        with patch.object(s.session, "close", new_callable=AsyncMock) as mock_close:
            async with s:
                pass
        mock_close.assert_called_once()

    async def test_async_context_manager_calls_close_on_exception(self):
        s = MinolScraper("a@b.com", "secret", "000000000001")
        with patch.object(s.session, "close", new_callable=AsyncMock) as mock_close:
            try:
                async with s:
                    raise ValueError("test error")
            except ValueError:
                pass
        mock_close.assert_called_once()


# ── login() ────────────────────────────────────────────────────────────────────

class TestMinolScraperLogin(unittest.IsolatedAsyncioTestCase):

    async def test_login_delegates_to_auth_and_sets_authenticated(self):
        s = MinolScraper("a@b.com", "secret", "000000000001")
        with patch("minol.auth.authenticate") as mock_auth:
            await s.login()
        mock_auth.assert_called_once_with(
            s.session, "a@b.com", "secret", "000000000001",
            status_fn=s._status, use_cache=True, session_path=None,
            session_data=None,
        )
        self.assertTrue(s.authenticated)

    async def test_login_forwards_session_path(self):
        s = MinolScraper("a@b.com", "secret", "000000000001")
        with patch("minol.auth.authenticate") as mock_auth:
            await s.login(session_path=Path("/tmp/custom.json"))
        _, kwargs = mock_auth.call_args
        self.assertEqual(kwargs["session_path"], Path("/tmp/custom.json"))

    async def test_login_passes_use_cache_false(self):
        s = MinolScraper("a@b.com", "secret", "000000000001")
        with patch("minol.auth.authenticate") as mock_auth:
            await s.login(use_cache=False)
        _, kwargs = mock_auth.call_args
        self.assertFalse(kwargs["use_cache"])

    async def test_login_forwards_session_data(self):
        s = MinolScraper("a@b.com", "secret", "000000000001")
        cache = {"user_num": "000000000001", "cookies": []}
        with patch("minol.auth.authenticate") as mock_auth:
            await s.login(session_data=cache)
        _, kwargs = mock_auth.call_args
        self.assertEqual(kwargs["session_data"], cache)

    async def test_login_returns_dict_from_authenticate(self):
        """login() passes through the dict returned by authenticate() (in-memory mode)."""
        s = MinolScraper("a@b.com", "secret", "000000000001")
        new_cache = {"user_num": "000000000001", "expires_at": "2099-01-01T00:00:00+00:00", "cookies": []}
        with patch("minol.auth.authenticate", return_value=new_cache):
            result = await s.login(session_data={})
        self.assertEqual(result, new_cache)

    async def test_login_returns_none_in_file_mode(self):
        """login() returns None when no session_data is provided (file mode)."""
        s = MinolScraper("a@b.com", "secret", "000000000001")
        with patch("minol.auth.authenticate", return_value=None):
            result = await s.login()
        self.assertIsNone(result)


# ── _parse_response() ──────────────────────────────────────────────────────────

class TestParseResponse(unittest.TestCase):

    def test_normal_response(self):
        raw = {
            "table": [
                {"raum": "Kueche", "consumption": 111.5, "gerNr": "DEVICE01", "unit": "KWH"},
                {"raum": "Wohnzimmer", "consumption": 88.0, "gerNr": "DEVICE02", "unit": "KWH"},
            ],
            "chart": [
                {"keyFigure": "Kueche", "categoryInt": "202501", "value": 10.0},
                {"keyFigure": "Kueche", "categoryInt": "202502", "value": None},
                {"keyFigure": "Wohnzimmer", "categoryInt": "202501", "value": 5.5},
            ],
        }
        result = MinolScraper._parse_response(raw)
        self.assertEqual(result["unit"], "KWH")
        self.assertIn("Kueche", result["rooms"])
        self.assertEqual(result["rooms"]["Kueche"]["total"], 111.5)
        self.assertEqual(result["rooms"]["Kueche"]["device"], "DEVICE01")
        self.assertEqual(result["rooms"]["Kueche"]["monthly"]["202501"], 10.0)
        self.assertIsNone(result["rooms"]["Kueche"]["monthly"]["202502"])
        self.assertEqual(result["rooms"]["Wohnzimmer"]["monthly"]["202501"], 5.5)

    def test_empty_table_and_chart(self):
        result = MinolScraper._parse_response({"table": [], "chart": []})
        self.assertEqual(result["rooms"], {})
        self.assertEqual(result["unit"], "")

    def test_missing_keys_use_defaults(self):
        raw = {
            "table": [{"raumKey": "Room1"}],
            "chart": [],
        }
        result = MinolScraper._parse_response(raw)
        self.assertIn("Room1", result["rooms"])
        self.assertEqual(result["rooms"]["Room1"]["total"], 0)
        self.assertEqual(result["rooms"]["Room1"]["device"], "")

    def test_chart_room_not_in_table_ignored(self):
        raw = {
            "table": [{"raum": "Room1", "consumption": 10, "gerNr": "D1", "unit": "M3"}],
            "chart": [
                {"keyFigure": "UnknownRoom", "categoryInt": "202501", "value": 5.0},
            ],
        }
        result = MinolScraper._parse_response(raw)
        self.assertNotIn("UnknownRoom", result["rooms"])

    def test_parse_response_warns_on_nonempty_raw_with_no_table(self):
        """Non-empty raw dict without 'table' key triggers a warning."""
        with self.assertLogs("minol.lib", level="WARNING") as log:
            result = MinolScraper._parse_response({"foo": "bar"})
        self.assertEqual(result["rooms"], {})
        self.assertTrue(any("no 'table' data" in msg for msg in log.output))

    def test_parse_response_no_warning_on_empty_input(self):
        """Empty dict should not trigger any warning."""
        with self.assertNoLogs("minol.lib", level="WARNING"):
            result = MinolScraper._parse_response({})
        self.assertEqual(result["rooms"], {})

    def test_null_monthly_values_preserved(self):
        raw = {
            "table": [{"raum": "Room1", "consumption": 0, "gerNr": "", "unit": "M3"}],
            "chart": [{"keyFigure": "Room1", "categoryInt": "202501", "value": None}],
        }
        result = MinolScraper._parse_response(raw)
        self.assertIsNone(result["rooms"]["Room1"]["monthly"]["202501"])


# ── fetch_consumption() ────────────────────────────────────────────────────────

class TestFetchConsumption(unittest.IsolatedAsyncioTestCase):

    def _authenticated_scraper(self):
        s = MinolScraper("a@b.com", "secret", "000000000001",
                         status_fn=lambda _: None)
        s.authenticated = True
        return s

    async def test_raises_if_not_authenticated(self):
        s = MinolScraper("a@b.com", "secret", "000000000001")
        with self.assertRaises(RuntimeError):
            await s.fetch_consumption("HEIZUNG", "100EHRAUM")

    async def test_raises_on_invalid_timeline_format(self):
        s = self._authenticated_scraper()
        with self.assertRaises(ValueError):
            await s.fetch_consumption("HEIZUNG", "100EHRAUM", timeline_start="2025-01")

    async def test_raises_on_invalid_timeline_end(self):
        s = self._authenticated_scraper()
        with self.assertRaises(ValueError):
            await s.fetch_consumption("HEIZUNG", "100EHRAUM",
                                      timeline_start="202501", timeline_end="25-01")

    async def test_valid_request_returns_parsed_data(self):
        s = self._authenticated_scraper()
        api_data = {
            "table": [{"raum": "Room1", "consumption": 5.0, "gerNr": "D1", "unit": "KWH"}],
            "chart": [],
        }
        s.session.post = AsyncMock(return_value=_make_resp(200, data=api_data))
        result = await s.fetch_consumption("HEIZUNG", "100EHRAUM",
                                           timeline_start="202501", timeline_end="202503")
        self.assertIn("rooms", result)
        self.assertIn("Room1", result["rooms"])

    async def test_raw_true_returns_raw_json(self):
        s = self._authenticated_scraper()
        api_data = {"table": [], "chart": [], "extra": "data"}
        s.session.post = AsyncMock(return_value=_make_resp(200, data=api_data))
        result = await s.fetch_consumption("HEIZUNG", "100EHRAUM", raw=True,
                                           timeline_start="202501", timeline_end="202503")
        self.assertEqual(result, api_data)

    async def test_unit_kwh_sets_values_in_kwh_true(self):
        s = self._authenticated_scraper()
        api_data = {"table": [], "chart": []}
        captured = {}

        async def fake_post(url, json_data=None, headers=None, allow_redirects=True):
            captured["payload"] = json_data
            return _make_resp(200, data=api_data)

        s.session.post = AsyncMock(side_effect=fake_post)
        await s.fetch_consumption("WARMWASSER", "200RAUM", unit="kwh",
                                  timeline_start="202501", timeline_end="202503")
        self.assertTrue(captured["payload"]["valuesInKWH"])

    async def test_unit_m3_sets_values_in_kwh_false(self):
        s = self._authenticated_scraper()
        api_data = {"table": [], "chart": []}
        captured = {}

        async def fake_post(url, json_data=None, headers=None, allow_redirects=True):
            captured["payload"] = json_data
            return _make_resp(200, data=api_data)

        s.session.post = AsyncMock(side_effect=fake_post)
        await s.fetch_consumption("HEIZUNG", "100EHRAUM", unit="m3",
                                  timeline_start="202501", timeline_end="202503")
        self.assertFalse(captured["payload"]["valuesInKWH"])

    async def test_heating_default_unit_is_kwh(self):
        s = self._authenticated_scraper()
        api_data = {"table": [], "chart": []}
        captured = {}

        async def fake_post(url, json_data=None, headers=None, allow_redirects=True):
            captured["payload"] = json_data
            return _make_resp(200, data=api_data)

        s.session.post = AsyncMock(side_effect=fake_post)
        await s.fetch_consumption("HEIZUNG", "100EHRAUM",
                                  timeline_start="202501", timeline_end="202503")
        self.assertTrue(captured["payload"]["valuesInKWH"])

    async def test_non_200_raises_runtime_error(self):
        s = self._authenticated_scraper()
        s.session.post = AsyncMock(return_value=_make_resp(500, text="error"))
        with self.assertRaises(RuntimeError):
            await s.fetch_consumption("HEIZUNG", "100EHRAUM",
                                      timeline_start="202501", timeline_end="202503")

    async def test_default_timeline_set_when_not_provided(self):
        s = self._authenticated_scraper()
        api_data = {"table": [], "chart": []}
        captured = {}

        async def fake_post(url, json_data=None, headers=None, allow_redirects=True):
            captured["payload"] = json_data
            return _make_resp(200, data=api_data)

        s.session.post = AsyncMock(side_effect=fake_post)
        await s.fetch_consumption("HEIZUNG", "100EHRAUM")

        self.assertRegex(captured["payload"]["timelineEnd"], r"^\d{6}$")
        self.assertRegex(captured["payload"]["timelineStart"], r"^\d{6}$")


# ── Convenience methods ────────────────────────────────────────────────────────

class TestConvenienceMethods(unittest.IsolatedAsyncioTestCase):

    def _scraper_with_mock_fetch(self):
        s = MinolScraper("a@b.com", "secret", "000000000001",
                         status_fn=lambda _: None)
        s.authenticated = True
        return s

    def _mock_fetch(self, s, return_value=None):
        rv = return_value or {"unit": "", "rooms": {}}
        mock = AsyncMock(return_value=rv)
        s.fetch_consumption = mock
        return mock

    async def test_fetch_heating_uses_correct_type(self):
        s = self._scraper_with_mock_fetch()
        m = self._mock_fetch(s)
        await s.fetch_heating()
        m.assert_called_once_with(*CONSUMPTION_TYPES["heating"])

    async def test_fetch_warm_water_uses_correct_type(self):
        s = self._scraper_with_mock_fetch()
        m = self._mock_fetch(s)
        await s.fetch_warm_water()
        m.assert_called_once_with(*CONSUMPTION_TYPES["warm_water"])

    async def test_fetch_cold_water_uses_correct_type(self):
        s = self._scraper_with_mock_fetch()
        m = self._mock_fetch(s)
        await s.fetch_cold_water()
        m.assert_called_once_with(*CONSUMPTION_TYPES["cold_water"])

    async def test_fetch_all_returns_all_types(self):
        s = self._scraper_with_mock_fetch()
        call_count = [0]
        results = {}

        async def fake_fetch(cons_type, dlg_key, **kwargs):
            call_count[0] += 1
            results[cons_type] = {"unit": "", "rooms": {}}
            return results[cons_type]

        s.fetch_consumption = fake_fetch
        data = await s.fetch_all()
        self.assertEqual(call_count[0], 3)
        self.assertIn("heating", data)
        self.assertIn("warm_water", data)
        self.assertIn("cold_water", data)

    async def test_fetch_all_raw_passes_raw_true(self):
        s = self._scraper_with_mock_fetch()
        captured_kwargs = []

        async def fake_fetch(cons_type, dlg_key, **kwargs):
            captured_kwargs.append(kwargs)
            return {}

        s.fetch_consumption = fake_fetch
        await s.fetch_all_raw()
        self.assertTrue(all(kw.get("raw") is True for kw in captured_kwargs))


if __name__ == "__main__":
    unittest.main()
