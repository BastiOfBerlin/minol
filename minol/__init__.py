"""Minol Kundenportal Scraper — public API."""

try:
    from importlib.metadata import version
    __version__ = version("minol")
except Exception:
    __version__ = "0.0.0-dev"

from minol.lib import MinolScraper
from minol.cli import load_config, resolve_credential
from minol._constants import CONSUMPTION_TYPES

__all__ = ["MinolScraper", "load_config", "resolve_credential", "CONSUMPTION_TYPES", "__version__"]
