"""Tests for minol.lib: MinolScraper class."""

import json
import sys
import unittest
from io import StringIO
from unittest.mock import MagicMock, patch, call

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

    def test_default_status_fn_prints_to_stderr(self):
        s = MinolScraper("a@b.com", "secret", "000000000001")
        captured = StringIO()
        with patch("sys.stderr", captured):
            s._status("hello")
        self.assertIn("hello", captured.getvalue())

    def test_custom_status_fn(self):
        messages = []
        s = MinolScraper("a@b.com", "secret", "000000000001", status_fn=messages.append)
        s._status("test message")
        self.assertEqual(messages, ["test message"])


# ── login() ────────────────────────────────────────────────────────────────────

class TestMinolScraperLogin(unittest.TestCase):

    def test_login_delegates_to_auth_and_sets_authenticated(self):
        s = MinolScraper("a@b.com", "secret", "000000000001")
        with patch("minol.auth.authenticate") as mock_auth:
            s.login()
        mock_auth.assert_called_once_with(
            s.session, "a@b.com", "secret", "000000000001",
            status_fn=s._status, use_cache=True,
        )
        self.assertTrue(s.authenticated)

    def test_login_passes_use_cache_false(self):
        s = MinolScraper("a@b.com", "secret", "000000000001")
        with patch("minol.auth.authenticate") as mock_auth:
            s.login(use_cache=False)
        _, kwargs = mock_auth.call_args
        self.assertFalse(kwargs["use_cache"])


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

class TestFetchConsumption(unittest.TestCase):

    def _authenticated_scraper(self):
        s = MinolScraper("a@b.com", "secret", "000000000001",
                         status_fn=lambda _: None)
        s.authenticated = True
        return s

    def test_raises_if_not_authenticated(self):
        s = MinolScraper("a@b.com", "secret", "000000000001")
        with self.assertRaises(RuntimeError):
            s.fetch_consumption("HEIZUNG", "100EHRAUM")

    def test_raises_on_invalid_timeline_format(self):
        s = self._authenticated_scraper()
        with self.assertRaises(ValueError):
            s.fetch_consumption("HEIZUNG", "100EHRAUM", timeline_start="2025-01")

    def test_raises_on_invalid_timeline_end(self):
        s = self._authenticated_scraper()
        with self.assertRaises(ValueError):
            s.fetch_consumption("HEIZUNG", "100EHRAUM",
                                timeline_start="202501", timeline_end="25-01")

    def test_valid_request_returns_parsed_data(self):
        s = self._authenticated_scraper()
        api_data = {
            "table": [{"raum": "Room1", "consumption": 5.0, "gerNr": "D1", "unit": "KWH"}],
            "chart": [],
        }
        with patch.object(s.session, "post", return_value=_make_resp(200, data=api_data)):
            result = s.fetch_consumption("HEIZUNG", "100EHRAUM",
                                         timeline_start="202501", timeline_end="202503")
        self.assertIn("rooms", result)
        self.assertIn("Room1", result["rooms"])

    def test_raw_true_returns_raw_json(self):
        s = self._authenticated_scraper()
        api_data = {"table": [], "chart": [], "extra": "data"}
        with patch.object(s.session, "post", return_value=_make_resp(200, data=api_data)):
            result = s.fetch_consumption("HEIZUNG", "100EHRAUM", raw=True,
                                         timeline_start="202501", timeline_end="202503")
        self.assertEqual(result, api_data)

    def test_unit_kwh_sets_values_in_kwh_true(self):
        s = self._authenticated_scraper()
        api_data = {"table": [], "chart": []}
        captured = {}

        def fake_post(url, json_data=None, headers=None, allow_redirects=True):
            captured["payload"] = json_data
            return _make_resp(200, data=api_data)

        with patch.object(s.session, "post", side_effect=fake_post):
            s.fetch_consumption("WARMWASSER", "200RAUM", unit="kwh",
                                timeline_start="202501", timeline_end="202503")
        self.assertTrue(captured["payload"]["valuesInKWH"])

    def test_unit_m3_sets_values_in_kwh_false(self):
        s = self._authenticated_scraper()
        api_data = {"table": [], "chart": []}
        captured = {}

        def fake_post(url, json_data=None, headers=None, allow_redirects=True):
            captured["payload"] = json_data
            return _make_resp(200, data=api_data)

        with patch.object(s.session, "post", side_effect=fake_post):
            s.fetch_consumption("HEIZUNG", "100EHRAUM", unit="m3",
                                timeline_start="202501", timeline_end="202503")
        self.assertFalse(captured["payload"]["valuesInKWH"])

    def test_heating_default_unit_is_kwh(self):
        s = self._authenticated_scraper()
        api_data = {"table": [], "chart": []}
        captured = {}

        def fake_post(url, json_data=None, headers=None, allow_redirects=True):
            captured["payload"] = json_data
            return _make_resp(200, data=api_data)

        with patch.object(s.session, "post", side_effect=fake_post):
            s.fetch_consumption("HEIZUNG", "100EHRAUM",
                                timeline_start="202501", timeline_end="202503")
        self.assertTrue(captured["payload"]["valuesInKWH"])

    def test_non_200_raises_runtime_error(self):
        s = self._authenticated_scraper()
        with patch.object(s.session, "post", return_value=_make_resp(500, text="error")):
            with self.assertRaises(RuntimeError):
                s.fetch_consumption("HEIZUNG", "100EHRAUM",
                                    timeline_start="202501", timeline_end="202503")

    def test_default_timeline_set_when_not_provided(self):
        s = self._authenticated_scraper()
        api_data = {"table": [], "chart": []}
        captured = {}

        def fake_post(url, json_data=None, headers=None, allow_redirects=True):
            captured["payload"] = json_data
            return _make_resp(200, data=api_data)

        with patch.object(s.session, "post", side_effect=fake_post):
            s.fetch_consumption("HEIZUNG", "100EHRAUM")

        self.assertRegex(captured["payload"]["timelineEnd"], r"^\d{6}$")
        self.assertRegex(captured["payload"]["timelineStart"], r"^\d{6}$")


# ── Convenience methods ────────────────────────────────────────────────────────

class TestConvenienceMethods(unittest.TestCase):

    def _scraper_with_mock_fetch(self):
        s = MinolScraper("a@b.com", "secret", "000000000001",
                         status_fn=lambda _: None)
        s.authenticated = True
        return s

    def _mock_fetch(self, s, return_value=None):
        rv = return_value or {"unit": "", "rooms": {}}
        mock = MagicMock(return_value=rv)
        s.fetch_consumption = mock
        return mock

    def test_fetch_heating_uses_correct_type(self):
        s = self._scraper_with_mock_fetch()
        m = self._mock_fetch(s)
        s.fetch_heating()
        m.assert_called_once_with(*CONSUMPTION_TYPES["heating"])

    def test_fetch_warm_water_uses_correct_type(self):
        s = self._scraper_with_mock_fetch()
        m = self._mock_fetch(s)
        s.fetch_warm_water()
        m.assert_called_once_with(*CONSUMPTION_TYPES["warm_water"])

    def test_fetch_cold_water_uses_correct_type(self):
        s = self._scraper_with_mock_fetch()
        m = self._mock_fetch(s)
        s.fetch_cold_water()
        m.assert_called_once_with(*CONSUMPTION_TYPES["cold_water"])

    def test_fetch_all_returns_all_types(self):
        s = self._scraper_with_mock_fetch()
        call_count = [0]
        results = {}

        def fake_fetch(cons_type, dlg_key, **kwargs):
            call_count[0] += 1
            results[cons_type] = {"unit": "", "rooms": {}}
            return results[cons_type]

        s.fetch_consumption = fake_fetch
        data = s.fetch_all()
        self.assertEqual(call_count[0], 3)
        self.assertIn("heating", data)
        self.assertIn("warm_water", data)
        self.assertIn("cold_water", data)

    def test_fetch_all_raw_passes_raw_true(self):
        s = self._scraper_with_mock_fetch()
        captured_kwargs = []

        def fake_fetch(cons_type, dlg_key, **kwargs):
            captured_kwargs.append(kwargs)
            return {}

        s.fetch_consumption = fake_fetch
        s.fetch_all_raw()
        self.assertTrue(all(kw.get("raw") is True for kw in captured_kwargs))


if __name__ == "__main__":
    unittest.main()
