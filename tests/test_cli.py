"""Tests for minol.cli: load_config(), resolve_credential(), main()."""

import json
import os
import stat
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch, mock_open

from minol.cli import load_config, resolve_credential, main


# ── load_config() ──────────────────────────────────────────────────────────────

class TestLoadConfig(unittest.TestCase):

    def test_valid_json_file_returns_dict(self):
        data = {"email": "user@example.com", "password": "secret", "user_num": "000000000001"}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            tmp = Path(f.name)
        try:
            result = load_config(tmp)
            self.assertEqual(result, data)
        finally:
            tmp.unlink(missing_ok=True)

    def test_file_not_found_returns_empty_dict(self):
        result = load_config(Path("/nonexistent/config.json"))
        self.assertEqual(result, {})

    def test_invalid_json_raises_value_error(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("{not valid json")
            tmp = Path(f.name)
        try:
            with self.assertRaises(ValueError):
                load_config(tmp)
        finally:
            tmp.unlink(missing_ok=True)

    def test_group_readable_file_logs_warning(self):
        data = {"email": "u@x.com"}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            tmp = Path(f.name)
        try:
            # Make it group-readable
            os.chmod(tmp, 0o640)
            with self.assertLogs("minol.cli", level="WARNING") as log:
                result = load_config(tmp)
            self.assertTrue(any("readable by others" in msg or "readable" in msg
                                for msg in log.output))
        finally:
            tmp.unlink(missing_ok=True)


# ── resolve_credential() ───────────────────────────────────────────────────────

class TestResolveCredential(unittest.TestCase):

    def setUp(self):
        # Clean up any test env vars
        for v in ("TEST_CRED_VAR",):
            os.environ.pop(v, None)

    def test_cli_arg_takes_priority(self):
        result = resolve_credential("email", "cli@x.com", "TEST_CRED_VAR",
                                    {"email": "config@x.com"})
        self.assertEqual(result, "cli@x.com")

    def test_env_var_used_when_no_cli_arg(self):
        os.environ["TEST_CRED_VAR"] = "env@x.com"
        try:
            result = resolve_credential("email", None, "TEST_CRED_VAR",
                                        {"email": "config@x.com"})
            self.assertEqual(result, "env@x.com")
        finally:
            os.environ.pop("TEST_CRED_VAR", None)

    def test_config_used_when_no_cli_or_env(self):
        result = resolve_credential("email", None, "TEST_CRED_VAR",
                                    {"email": "config@x.com"})
        self.assertEqual(result, "config@x.com")

    def test_value_error_when_all_missing(self):
        with self.assertRaises(ValueError):
            resolve_credential("email", None, "TEST_CRED_VAR", {})

    def test_cli_arg_overrides_env(self):
        os.environ["TEST_CRED_VAR"] = "env@x.com"
        try:
            result = resolve_credential("email", "cli@x.com", "TEST_CRED_VAR", {})
            self.assertEqual(result, "cli@x.com")
        finally:
            os.environ.pop("TEST_CRED_VAR", None)


# ── main() ─────────────────────────────────────────────────────────────────────

class TestMain(unittest.TestCase):
    """Test main() by mocking MinolScraper and sys.argv."""

    def _run_main(self, argv, stdin_text=None, env=None):
        """Run main() with given argv; returns (stdout, stderr, exit_code)."""
        env_backup = {}
        if env:
            for k, v in env.items():
                env_backup[k] = os.environ.get(k)
                os.environ[k] = v

        stdout = StringIO()
        stderr = StringIO()
        exit_code = 0
        try:
            with patch("sys.argv", ["minol"] + argv):
                with patch("sys.stdout", stdout):
                    with patch("sys.stderr", stderr):
                        if stdin_text is not None:
                            with patch("sys.stdin", StringIO(stdin_text)):
                                main()
                        else:
                            main()
        except SystemExit as e:
            exit_code = e.code if isinstance(e.code, int) else 1
        finally:
            for k, v in env_backup.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        return stdout.getvalue(), stderr.getvalue(), exit_code

    def _mock_scraper_cls(self, fetch_result=None):
        scraper = MagicMock()
        scraper.login = AsyncMock(return_value=None)
        scraper.fetch_all = AsyncMock(return_value=fetch_result or {"heating": {}, "warm_water": {}, "cold_water": {}})
        scraper.fetch_heating = AsyncMock(return_value=fetch_result or {"unit": "KWH", "rooms": {}})
        scraper.fetch_warm_water = AsyncMock(return_value=fetch_result or {"unit": "M3", "rooms": {}})
        scraper.fetch_cold_water = AsyncMock(return_value=fetch_result or {"unit": "M3", "rooms": {}})
        scraper.fetch_consumption = AsyncMock(return_value=fetch_result or {"unit": "", "rooms": {}})
        scraper.__aenter__ = AsyncMock(return_value=scraper)
        scraper.__aexit__ = AsyncMock(return_value=False)
        return scraper

    def test_basic_invocation_fetches_all_and_prints_json(self):
        scraper = self._mock_scraper_cls()
        with patch("minol.cli.MinolScraper", return_value=scraper):
            stdout, _, exit_code = self._run_main(
                ["--email", "u@x.com", "--password", "pass", "--user-num", "000000000001"]
            )
        self.assertEqual(exit_code, 0)
        # Output should be valid JSON
        data = json.loads(stdout)
        self.assertIn("heating", data)

    def test_type_heating_calls_fetch_consumption_with_heating_type(self):
        scraper = self._mock_scraper_cls()
        with patch("minol.cli.MinolScraper", return_value=scraper):
            self._run_main([
                "--email", "u@x.com", "--password", "pass",
                "--user-num", "000000000001", "--type", "heating"
            ])
        scraper.fetch_consumption.assert_called_once()
        args = scraper.fetch_consumption.call_args[0]
        self.assertEqual(args[0], "HEIZUNG")

    def test_type_all_calls_fetch_all(self):
        scraper = self._mock_scraper_cls()
        with patch("minol.cli.MinolScraper", return_value=scraper):
            self._run_main([
                "--email", "u@x.com", "--password", "pass",
                "--user-num", "000000000001", "--type", "all"
            ])
        scraper.fetch_all.assert_called_once()

    def test_raw_flag_passes_raw_true(self):
        scraper = self._mock_scraper_cls()
        with patch("minol.cli.MinolScraper", return_value=scraper):
            self._run_main([
                "--email", "u@x.com", "--password", "pass",
                "--user-num", "000000000001", "--raw"
            ])
        _, kwargs = scraper.fetch_all.call_args
        self.assertTrue(kwargs.get("raw"))

    def test_output_flag_writes_to_file(self):
        scraper = self._mock_scraper_cls({"heating": {}, "warm_water": {}, "cold_water": {}})
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            tmp = f.name
        try:
            with patch("minol.cli.MinolScraper", return_value=scraper):
                self._run_main([
                    "--email", "u@x.com", "--password", "pass",
                    "--user-num", "000000000001", "--output", tmp
                ])
            with open(tmp) as f:
                data = json.load(f)
            self.assertIn("heating", data)
            mode = os.stat(tmp).st_mode & 0o777
            self.assertEqual(mode, 0o640)
        finally:
            os.unlink(tmp)

    def test_no_cache_passes_use_cache_false(self):
        scraper = self._mock_scraper_cls()
        with patch("minol.cli.MinolScraper", return_value=scraper):
            self._run_main([
                "--email", "u@x.com", "--password", "pass",
                "--user-num", "000000000001", "--no-cache"
            ])
        _, kwargs = scraper.login.call_args
        self.assertFalse(kwargs.get("use_cache"))

    def test_session_path_forwarded_to_login(self):
        scraper = self._mock_scraper_cls()
        with patch("minol.cli.MinolScraper", return_value=scraper):
            self._run_main([
                "--email", "u@x.com", "--password", "pass",
                "--user-num", "000000000001", "--session-path", "/tmp/custom.json"
            ])
        _, kwargs = scraper.login.call_args
        self.assertEqual(kwargs.get("session_path"), Path("/tmp/custom.json"))

    def test_password_stdin_reads_from_stdin(self):
        scraper = self._mock_scraper_cls()
        with patch("minol.cli.MinolScraper", return_value=scraper) as mock_cls:
            self._run_main(
                ["--email", "u@x.com", "--user-num", "000000000001", "--password-stdin"],
                stdin_text="mypassword\n"
            )
        _, kwargs = mock_cls.call_args
        # MinolScraper is called positionally: email, password, user_num
        args = mock_cls.call_args[0]
        self.assertEqual(args[1], "mypassword")

    def test_password_stdin_empty_raises_system_exit(self):
        with patch("minol.cli.MinolScraper"):
            _, _, exit_code = self._run_main(
                ["--email", "u@x.com", "--user-num", "000000000001", "--password-stdin"],
                stdin_text=""
            )
        # SystemExit with message about empty stdin
        self.assertNotEqual(exit_code, 0)

    def test_start_end_passed_to_fetch(self):
        scraper = self._mock_scraper_cls()
        with patch("minol.cli.MinolScraper", return_value=scraper):
            self._run_main([
                "--email", "u@x.com", "--password", "pass",
                "--user-num", "000000000001",
                "--start", "202501", "--end", "202503"
            ])
        _, kwargs = scraper.fetch_all.call_args
        self.assertEqual(kwargs.get("timeline_start"), "202501")
        self.assertEqual(kwargs.get("timeline_end"), "202503")

    def test_unit_kwh_passed_through(self):
        scraper = self._mock_scraper_cls()
        with patch("minol.cli.MinolScraper", return_value=scraper):
            self._run_main([
                "--email", "u@x.com", "--password", "pass",
                "--user-num", "000000000001", "--unit", "kwh"
            ])
        _, kwargs = scraper.fetch_all.call_args
        self.assertEqual(kwargs.get("unit"), "kwh")

    def test_verbose_flag_sets_debug_logging(self):
        scraper = self._mock_scraper_cls()
        import logging
        with patch("minol.cli.MinolScraper", return_value=scraper):
            with patch("logging.basicConfig") as mock_logging:
                self._run_main([
                    "--email", "u@x.com", "--password", "pass",
                    "--user-num", "000000000001", "-v"
                ])
        mock_logging.assert_called_once()
        kwargs = mock_logging.call_args[1]
        self.assertEqual(kwargs.get("level"), logging.DEBUG)

    def test_error_during_fetch_exits_with_1(self):
        scraper = MagicMock()
        scraper.login = AsyncMock(return_value=None)
        scraper.fetch_all = AsyncMock(side_effect=RuntimeError("Network error"))
        with patch("minol.cli.MinolScraper", return_value=scraper):
            _, _, exit_code = self._run_main([
                "--email", "u@x.com", "--password", "pass",
                "--user-num", "000000000001"
            ])
        self.assertEqual(exit_code, 1)

    def test_credentials_from_env_vars(self):
        scraper = self._mock_scraper_cls()
        env = {
            "MINOL_EMAIL": "env@x.com",
            "MINOL_PASSWORD": "envpass",
            "MINOL_USER_NUM": "999999999999",
        }
        with patch("minol.cli.MinolScraper", return_value=scraper) as mock_cls:
            stdout, _, exit_code = self._run_main([], env=env)
        self.assertEqual(exit_code, 0)
        args = mock_cls.call_args[0]
        self.assertEqual(args[0], "env@x.com")
        self.assertEqual(args[1], "envpass")
        self.assertEqual(args[2], "999999999999")

    def test_type_warm_water_calls_fetch_consumption_with_warm_water_type(self):
        scraper = self._mock_scraper_cls()
        with patch("minol.cli.MinolScraper", return_value=scraper):
            self._run_main([
                "--email", "u@x.com", "--password", "pass",
                "--user-num", "000000000001", "--type", "warm_water"
            ])
        scraper.fetch_consumption.assert_called_once()
        args = scraper.fetch_consumption.call_args[0]
        self.assertEqual(args[0], "WARMWASSER")


if __name__ == "__main__":
    unittest.main()
