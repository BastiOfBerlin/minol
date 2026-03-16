"""MinolScraper class: data fetching and login orchestration."""

import asyncio
import re
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from minol._constants import PORTAL_BASE, DATA_ENDPOINT, CONSUMPTION_TYPES
from minol._http import HttpSession
from minol import auth

__all__ = ["MinolScraper"]

log = logging.getLogger(__name__)


class MinolScraper:
    _DATA_HEADERS = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{PORTAL_BASE}/irj/portal",
    }

    def __init__(self, email: str, password: str, user_num: str, status_fn=None):
        self.email = email
        self.password = password
        self.user_num = user_num
        self.session = HttpSession()
        self.authenticated = False
        self._status = status_fn or (lambda msg: None)

    # ── Full login flow ────────────────────────────────────────────────────
    async def login(self, use_cache: bool = True, session_path: Path = None,
                    session_data: dict = None) -> "dict | None":
        """
        Authenticate to the Minol portal.

        Delegates to auth.authenticate(), which handles session caching
        transparently (restore on hit, save after fresh login).
        Pass use_cache=False to force a fresh SAML login.
        Pass session_path to override the default cache file location.
        Pass session_data to use in-memory caching with no file I/O; after a
        fresh login the new cache dict is returned so callers can store it
        themselves (e.g. Home Assistant integrations).

        Returns:
            The session cache dict when session_data is provided (existing dict
            on cache hit, new dict after a fresh login); None otherwise (file mode).
        """
        result = await auth.authenticate(
            self.session, self.email, self.password, self.user_num,
            status_fn=self._status, use_cache=use_cache,
            session_path=session_path, session_data=session_data,
        )
        self.authenticated = True
        self.password = ""  # clear plaintext password from memory
        return result

    # ── Data fetching ──────────────────────────────────────────────────────
    @staticmethod
    def _parse_response(raw: dict) -> dict:
        """
        Parse raw API response into structured consumption data.

        Returns:
            {
                "unit": "KWH" or "M3",
                "rooms": {
                    "Kuche": {
                        "total": 111.0,
                        "device": "04B648FD82639440",
                        "monthly": {"202503": 0, "202504": 0, ...}
                    },
                    ...
                }
            }
        """
        rooms = {}
        unit = ""

        for entry in raw.get("table", []):
            room = entry.get("raum", entry.get("raumKey", "unknown"))
            rooms[room] = {
                "total": entry.get("consumption", 0),
                "device": entry.get("gerNr", ""),
                "monthly": {},
            }
            unit = entry.get("unit", "")

        for entry in raw.get("chart", []):
            room = entry.get("keyFigure", "")
            month = entry.get("categoryInt", "")
            if room in rooms and month:
                rooms[room]["monthly"][month] = entry.get("value")

        if raw and not rooms:
            log.warning("API response contained no 'table' data — response shape may have changed. Keys: %s", list(raw.keys()))

        return {"unit": unit, "rooms": rooms}

    async def fetch_consumption(
        self,
        cons_type: str,
        dlg_key: str,
        timeline_start: str = None,
        timeline_end: str = None,
        raw: bool = False,
        unit: str = None,
    ) -> dict:
        """
        Fetch consumption data from the REST endpoint.

        Args:
            cons_type: HEIZUNG | WARMWASSER | KALTWASSER
            dlg_key:   100EHRAUM (heating) | 200RAUM (warm water) | 300RAUM (cold water)
            timeline_start: YYYYMM format, defaults to 12 months ago
            timeline_end:   YYYYMM format, defaults to current month
            raw: If True, return the raw API response instead of parsed data
            unit: "kwh" or "m3" -- controls the valuesInKWH payload parameter.
                  Defaults to KWH for heating, M3 for warm water and cold water.
        """
        if not self.authenticated:
            raise RuntimeError("Not authenticated. Call login() first.")

        now = datetime.now()
        if not timeline_end:
            timeline_end = now.strftime("%Y%m")
        if not timeline_start:
            timeline_start = (now - timedelta(days=365)).strftime("%Y%m")

        for label, value in (("timeline_start", timeline_start), ("timeline_end", timeline_end)):
            if not re.fullmatch(r'\d{6}', value):
                raise ValueError(f"{label} must be in YYYYMM format, got {value!r}")

        start_txt = f"{timeline_start[4:]}.{timeline_start[:4]}"
        end_txt = f"{timeline_end[4:]}.{timeline_end[:4]}"

        values_in_kwh = (unit.lower() == "kwh") if unit else (cons_type == "HEIZUNG")

        payload = {
            "userNum": self.user_num,
            "layer": "NE",
            "scale": "CALMONTH",
            "chartRefUnit": "ABS",
            "refObject": "NOREF",
            "consType": cons_type,
            "dashBoardKey": "PE",
            "timelineStart": timeline_start,
            "timelineStartTxt": start_txt,
            "timelineEnd": timeline_end,
            "timelineEndTxt": end_txt,
            "valuesInKWH": values_in_kwh,
            "dlgKey": dlg_key,
        }

        self._status(f"Fetching {cons_type} ({timeline_start} to {timeline_end})...")
        log.info(f"Fetching {cons_type} data ({timeline_start} -> {timeline_end})...")

        resp = await self.session.post(DATA_ENDPOINT, json_data=payload, headers=self._DATA_HEADERS)
        log.info(f"  Status: {resp.status_code}")

        if resp.status_code != 200:
            log.error(f"  Error: {resp.text[:300]}")
            raise RuntimeError(f"Data fetch failed: {resp.status_code}")

        data = resp.json()
        log.info(f"  Data received: {json.dumps(data, indent=2)[:500]}...")
        return data if raw else self._parse_response(data)

    async def fetch_heating(self, **kwargs) -> dict:
        return await self.fetch_consumption(*CONSUMPTION_TYPES["heating"], **kwargs)

    async def fetch_warm_water(self, **kwargs) -> dict:
        return await self.fetch_consumption(*CONSUMPTION_TYPES["warm_water"], **kwargs)

    async def fetch_cold_water(self, **kwargs) -> dict:
        return await self.fetch_consumption(*CONSUMPTION_TYPES["cold_water"], **kwargs)

    async def fetch_all(self, **kwargs) -> dict:
        """Fetch all three consumption types in parallel."""
        names = list(CONSUMPTION_TYPES.keys())
        results = await asyncio.gather(
            *(self.fetch_consumption(*args, **kwargs) for args in CONSUMPTION_TYPES.values())
        )
        return dict(zip(names, results))

    async def fetch_all_raw(self, **kwargs) -> dict:
        """Fetch all three consumption types as raw API responses."""
        return await self.fetch_all(raw=True, **kwargs)

