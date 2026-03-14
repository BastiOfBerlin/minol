"""Constants and configuration values for the Minol scraper."""

from pathlib import Path

__all__ = [
    "PORTAL_BASE", "B2C_DOMAIN", "B2C_TENANT", "DATA_ENDPOINT",
    "DEFAULT_CONFIG_PATH", "DEFAULT_SESSION_PATH", "CONSUMPTION_TYPES",
]

# ─── Configuration ────────────────────────────────────────────────────────────

PORTAL_BASE = "https://webservices.minol.com"
B2C_DOMAIN = "minolauth.b2clogin.com"
B2C_TENANT = "minolauth.onmicrosoft.com"

DATA_ENDPOINT = f"{PORTAL_BASE}/minol.com~kundenportal~em~web/rest/EMData/readData"

DEFAULT_CONFIG_PATH = Path.home() / ".minol.json"
DEFAULT_SESSION_PATH = Path.home() / ".minol_session.json"

CONSUMPTION_TYPES = {
    "heating":    ("HEIZUNG",    "100EHRAUM"),
    "warm_water": ("WARMWASSER", "200RAUM"),
    "cold_water": ("KALTWASSER", "300RAUM"),
}
