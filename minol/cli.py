"""Command-line interface for the Minol scraper."""

import os
import sys
import stat
import json
import logging
import argparse
from pathlib import Path

from minol.lib import MinolScraper
from minol._constants import CONSUMPTION_TYPES, DEFAULT_CONFIG_PATH

__all__ = ["load_config", "resolve_credential", "main"]


def load_config(path: Path = None) -> dict:
    """Load credentials from a JSON config file.

    Expected format: {"email": "...", "password": "...", "user_num": "..."}
    Returns an empty dict if the file does not exist.
    Warns if the file is readable by group or other users.
    """
    p = path or DEFAULT_CONFIG_PATH
    if not p.is_file():
        return {}
    mode = os.stat(p).st_mode
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        logging.getLogger(__name__).warning(
            "Config file %s is readable by others (mode %s). "
            "Consider running: chmod 600 %s",
            p, oct(mode & 0o777), p,
        )
    try:
        with open(p) as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise SystemExit(f"Error: config file {p} contains invalid JSON: {e}") from None


def resolve_credential(name: str, cli_value: str | None, env_var: str, config: dict) -> str:
    """Resolve a credential from CLI arg, environment variable, or config file (in that order)."""
    if cli_value is not None:
        value = cli_value
    elif os.environ.get(env_var) is not None:
        value = os.environ.get(env_var)
    else:
        value = config.get(name)
    if not value:
        raise SystemExit(
            f"Error: {name} not provided. Set it via --{name.replace('_', '-')}, "
            f"${env_var}, or {DEFAULT_CONFIG_PATH}"
        )
    return value


def main():
    parser = argparse.ArgumentParser(
        description="Minol Kundenportal Scraper",
        epilog="Credentials are resolved in order: CLI arguments > environment "
               "variables (MINOL_EMAIL, MINOL_PASSWORD, MINOL_USER_NUM) > "
               "config file (~/.minol.json).",
    )
    parser.add_argument("--email", default=None, help="Login email address")
    parser.add_argument("--password", default=None,
                        help="Login password. WARNING: visible in process listing (ps aux). "
                             "Prefer --password-stdin, $MINOL_PASSWORD, or ~/.minol.json (chmod 600).")
    parser.add_argument("--password-stdin", action="store_true",
                        help="Read password from stdin (safer than --password)")
    parser.add_argument("--user-num", default=None, help="User number (e.g., 000000000000)")
    parser.add_argument("--config", default=None, help="Path to JSON config file (default: ~/.minol.json)")
    parser.add_argument("--start", help="Timeline start (YYYYMM)", default=None)
    parser.add_argument("--end", help="Timeline end (YYYYMM)", default=None)
    parser.add_argument("--type", choices=list(CONSUMPTION_TYPES) + ["all"],
                        default="all", help="Consumption type to fetch")
    parser.add_argument("--output", help="Output JSON file", default=None)
    parser.add_argument("--unit", choices=["kwh", "m3"], default=None,
                        help="Unit of measurement (kwh or m3). Heating defaults to kwh, water types default to m3.")
    parser.add_argument("--raw", action="store_true", help="Return raw API response instead of parsed data")
    parser.add_argument("--no-cache", action="store_true", help="Skip session cache, force fresh login")
    parser.add_argument("--session-path", default=None,
                        help="Path to session cache file (default: ~/.minol_session.json)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    config = load_config(Path(args.config) if args.config else None)
    email = resolve_credential("email", args.email, "MINOL_EMAIL", config)

    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\n")
        if not password:
            raise SystemExit("Error: --password-stdin provided but stdin was empty")
    else:
        password = resolve_credential("password", args.password, "MINOL_PASSWORD", config)

    user_num = resolve_credential("user_num", args.user_num, "MINOL_USER_NUM", config)

    scraper = MinolScraper(email, password, user_num)

    try:
        session_path = Path(args.session_path) if args.session_path else None
        scraper.login(use_cache=not args.no_cache, session_path=session_path)

        kwargs = {"raw": args.raw, "unit": args.unit}
        if args.start:
            kwargs["timeline_start"] = args.start
        if args.end:
            kwargs["timeline_end"] = args.end

        if args.type == "all":
            data = scraper.fetch_all(**kwargs)
        else:
            data = scraper.fetch_consumption(*CONSUMPTION_TYPES[args.type], **kwargs)

        if args.output:
            fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
            os.fchmod(fd, 0o640)
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
            print(f"Data written to {args.output}", file=sys.stderr)
        else:
            print(json.dumps(data, indent=2))

    except Exception as e:
        log = logging.getLogger(__name__)
        log.debug("Details:", exc_info=True)
        log.error(f"Failed: {e}")
        sys.exit(1)
