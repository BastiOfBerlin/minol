"""HTTP session helper using only stdlib (urllib)."""

import json
import logging
from http.cookiejar import CookieJar
from urllib.request import build_opener, HTTPCookieProcessor, Request, HTTPRedirectHandler
from urllib.error import HTTPError

from minol._constants import PORTAL_BASE

__all__ = ["HttpSession", "HttpResponse", "resolve_url"]

log = logging.getLogger(__name__)


def resolve_url(url: str) -> str:
    """Resolve a potentially relative SAP URL path against PORTAL_BASE."""
    return f"{PORTAL_BASE}{url}" if url.startswith("/") else url


class _NoRedirectHandler(HTTPRedirectHandler):
    """Redirect handler that surfaces 3xx responses instead of following them."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPError(newurl, code, msg, headers, fp)


class HttpResponse:
    def __init__(self, status_code: int, text: str, headers: dict, url: str):
        self.status_code = status_code
        self.text = text
        self.headers = headers
        self.url = url

    def json(self) -> dict:
        return json.loads(self.text)


class HttpSession:
    """Minimal requests-like session using only urllib."""

    DEFAULT_TIMEOUT = 30  # seconds

    def __init__(self, timeout: int = DEFAULT_TIMEOUT):
        self.timeout = timeout
        self.cookie_jar = CookieJar()
        self._opener = build_opener(HTTPCookieProcessor(self.cookie_jar))
        self._no_redirect_opener = build_opener(
            HTTPCookieProcessor(self.cookie_jar), _NoRedirectHandler()
        )
        self.default_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
        }

    @staticmethod
    def _make_response(resp) -> HttpResponse:
        return HttpResponse(
            resp.getcode(),
            resp.read().decode("utf-8", errors="replace"),
            {k.lower(): v for k, v in resp.headers.items()},
            resp.geturl(),
        )

    @staticmethod
    def _make_error_response(e: HTTPError) -> HttpResponse:
        return HttpResponse(
            e.code,
            e.read().decode("utf-8", errors="replace") if e.fp else "",
            {k.lower(): v for k, v in e.headers.items()},
            e.filename,
        )

    def _request(self, url: str, data: bytes = None, headers: dict = None,
                 allow_redirects: bool = True) -> HttpResponse:
        hdrs = {**self.default_headers, **(headers or {})}
        req = Request(url, data=data, headers=hdrs)
        opener = self._no_redirect_opener if not allow_redirects else self._opener
        try:
            return self._make_response(opener.open(req, timeout=self.timeout))
        except HTTPError as e:
            return self._make_error_response(e)

    def get(self, url: str, headers: dict = None, allow_redirects: bool = True) -> HttpResponse:
        return self._request(url, headers=headers, allow_redirects=allow_redirects)

    def post(self, url: str, data: str = None, json_data: dict = None,
             headers: dict = None, allow_redirects: bool = True) -> HttpResponse:
        hdrs = dict(headers or {})
        if json_data is not None:
            body = json.dumps(json_data).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        elif data is not None:
            body = data.encode("utf-8") if isinstance(data, str) else data
        else:
            body = b""
        return self._request(url, data=body, headers=hdrs, allow_redirects=allow_redirects)

    @staticmethod
    def _domain_matches(cookie_domain: str, domain: str) -> bool:
        """Return True if cookie_domain is an exact match or a subdomain of domain."""
        cd = cookie_domain or ""
        return cd == domain or cd == f".{domain}" or cd.endswith(f".{domain}")

    def get_cookie(self, name: str, domain: str = None) -> str | None:
        for cookie in self.cookie_jar:
            if cookie.name == name:
                if domain is None or self._domain_matches(cookie.domain or "", domain):
                    return cookie.value
        return None

    def cookie_names(self, domain: str = None) -> list[str]:
        return [c.name for c in self.cookie_jar
                if domain is None or self._domain_matches(c.domain or "", domain)]

    def all_cookies(self) -> list[tuple[str, str]]:
        return [(c.name, c.domain) for c in self.cookie_jar]
