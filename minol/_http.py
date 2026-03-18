"""HTTP session helper using aiohttp."""

import asyncio
import json
import logging

import aiohttp
from multidict import CIMultiDict
from yarl import URL

from minol._constants import PORTAL_BASE

__all__ = ["HttpSession", "HttpResponse", "resolve_url"]

log = logging.getLogger(__name__)

_DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
}


def resolve_url(url: str) -> str:
    """Resolve a potentially relative SAP URL path against PORTAL_BASE."""
    return f"{PORTAL_BASE}{url}" if url.startswith("/") else url


class HttpResponse:
    def __init__(self, status_code: int, text: str, headers: dict, url: str):
        self.status_code = status_code
        self.text = text
        self.headers = headers
        self.url = url

    def json(self) -> dict:
        return json.loads(self.text)


def _make_cookie_jar() -> aiohttp.CookieJar:
    """Create an aiohttp CookieJar, working around the running-loop requirement.

    quote_cookie=False: aiohttp's CookieJar._build_morsel() re-encodes cookie
    values through Python's SimpleCookie._quote(), which wraps base64 strings
    (containing '+', '/', '=') in double quotes.  RFC 6265 §4.2.1 forbids
    quoted-string in Cookie request headers; Azure AD B2C rejects such requests
    with HTTP 400.  Disabling quoting passes cookie values through unchanged.
    """
    try:
        return aiohttp.CookieJar(unsafe=True, quote_cookie=False)
    except RuntimeError:
        # No running event loop (e.g. called from sync test / __init__ context).
        # CookieJar stores the loop ref but never uses it for jar operations.
        _tmp = asyncio.new_event_loop()
        try:
            return aiohttp.CookieJar(unsafe=True, quote_cookie=False, loop=_tmp)
        finally:
            _tmp.close()


class HttpSession:
    """aiohttp-based HTTP session with optional external session injection."""

    DEFAULT_TIMEOUT = 30  # seconds

    def __init__(self, session: "aiohttp.ClientSession | None" = None,
                 timeout: int = DEFAULT_TIMEOUT):
        self.timeout = timeout
        self._owns_session = session is None
        self._aio_session: "aiohttp.ClientSession | None" = session
        # Cookie jar: created eagerly for owned sessions; borrowed from injected sessions.
        self._jar: aiohttp.CookieJar = (
            _make_cookie_jar() if session is None else session.cookie_jar
        )

    def _get_session(self) -> aiohttp.ClientSession:
        """Return the aiohttp session, creating it lazily if owned."""
        if self._aio_session is None:
            self._aio_session = aiohttp.ClientSession(cookie_jar=self._jar)
        return self._aio_session

    async def _request(self, url: str, method: str = "GET", data: bytes = None,
                       headers: dict = None, allow_redirects: bool = True,
                       encoded: bool = False) -> HttpResponse:
        hdrs = {**_DEFAULT_HEADERS, **(headers or {})}
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        # encoded=True tells yarl to skip percent-encoding normalisation.  This is
        # required for URLs that carry an RSA signature over the exact encoded form
        # (e.g. SAML HTTP Redirect binding): yarl would otherwise decode %2F → /,
        # changing the byte sequence and breaking the signature check.
        actual_url = URL(url, encoded=True) if encoded else url
        async with self._get_session().request(
            method, actual_url, data=data, headers=hdrs,
            allow_redirects=allow_redirects, timeout=timeout,
        ) as resp:
            text = await resp.text(encoding="utf-8", errors="replace")
            return HttpResponse(
                resp.status,
                text,
                CIMultiDict(resp.headers),
                str(resp.url),
            )

    async def get(self, url: str, headers: dict = None,
                  allow_redirects: bool = True, encoded: bool = False) -> HttpResponse:
        return await self._request(url, "GET", headers=headers,
                                   allow_redirects=allow_redirects, encoded=encoded)

    async def get_following_redirects(self, url: str, headers: dict = None,
                                      max_redirects: int = 20,
                                      encoded: bool = False) -> HttpResponse:
        """GET url, manually following each redirect and extracting Set-Cookie headers at
        every hop via _extract_cookies_from_headers().

        Use this when the server sets important cookies on intermediate 3xx responses that
        aiohttp's automatic redirect handling silently drops from the jar (observed with
        Azure AD B2C's SAML transaction cookies).

        encoded=True preserves the percent-encoding of the initial URL exactly (needed
        for SAML HTTP Redirect binding URLs signed by the SP).  Redirect hops use
        normal URL handling since those URLs carry no signature.
        """
        first = True
        for _ in range(max_redirects):
            resp = await self._request(url, "GET", headers=headers, allow_redirects=False,
                                       encoded=encoded and first)
            first = False
            self._extract_cookies_from_headers(resp)
            if resp.status_code in {301, 302, 303, 307, 308}:
                location = resp.headers.get("location", "")
                if not location:
                    break
                url = resolve_url(location)
            else:
                return resp
        return resp

    async def post(self, url: str, data: str = None, json_data: dict = None,
                   headers: dict = None, allow_redirects: bool = True,
                   encoded: bool = False) -> HttpResponse:
        hdrs = dict(headers or {})
        if json_data is not None:
            body = json.dumps(json_data).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        elif data is not None:
            body = data.encode("utf-8") if isinstance(data, str) else data
        else:
            body = b""
        return await self._request(url, "POST", data=body, headers=hdrs,
                                   allow_redirects=allow_redirects, encoded=encoded)

    async def close(self) -> None:
        """Close the owned aiohttp session. No-op for injected sessions."""
        if self._owns_session and self._aio_session is not None:
            await self._aio_session.close()
            self._aio_session = None

    # ── Cookie helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _domain_matches(cookie_domain: str, domain: str) -> bool:
        """Return True if cookie_domain is an exact match or a subdomain of domain."""
        cd = cookie_domain or ""
        return cd == domain or cd == f".{domain}" or cd.endswith(f".{domain}")

    def get_cookie(self, name: str, domain: str = None) -> str | None:
        for morsel in self._jar:
            if morsel.key == name:
                if domain is None or self._domain_matches(morsel["domain"], domain):
                    return morsel.value
        return None

    def cookie_names(self, domain: str = None) -> list[str]:
        return [
            m.key for m in self._jar
            if domain is None or self._domain_matches(m["domain"], domain)
        ]

    def all_cookies(self) -> list[tuple[str, str]]:
        return [(m.key, m["domain"]) for m in self._jar]

    def export_cookies(self) -> list[dict]:
        """Serialize all cookies to a list of dicts for session caching."""
        return [
            {
                "name": m.key,
                "value": m.value,
                "domain": m["domain"],
                "path": m["path"] or "/",
                "secure": bool(m["secure"]),
                "expires": m["expires"] or None,
            }
            for m in self._jar
        ]

    def import_cookies(self, cookies: list[dict]) -> None:
        """Restore cookies from a list of dicts into the jar."""
        from http.cookies import SimpleCookie
        for c in cookies:
            sc: SimpleCookie = SimpleCookie()
            sc[c["name"]] = c["value"]
            morsel = sc[c["name"]]
            # SimpleCookie._quote() wraps values containing '/', '+' etc. in double
            # quotes.  That encoding is valid in Set-Cookie response headers but
            # ILLEGAL in Cookie request headers (RFC 6265 §4.2.1 forbids quoted
            # strings).  Force coded_value to equal the plain value so aiohttp
            # sends  Cookie: name=rawvalue  rather than  Cookie: name="rawvalue".
            morsel._coded_value = morsel.value
            if c.get("domain"):
                morsel["domain"] = c["domain"]
            if c.get("path"):
                morsel["path"] = c["path"]
            if c.get("secure"):
                morsel["secure"] = c["secure"]
            if c.get("expires"):
                morsel["expires"] = str(c["expires"])
            domain = c.get("domain", "").lstrip(".")
            resp_url = URL(f"https://{domain}/") if domain else URL()
            self._jar.update_cookies(sc, resp_url)

    def _extract_cookies_from_headers(self, response: "HttpResponse") -> int:
        """Manually parse Set-Cookie headers and inject them into the cookie jar.

        Falls back when aiohttp's automatic cookie processing silently drops cookies
        (e.g. due to unusual B2C cookie formatting). Returns the number of cookies
        extracted.
        """
        from http.cookies import SimpleCookie
        raw_headers = (
            response.headers.getall("Set-Cookie", [])
            if hasattr(response.headers, "getall")
            else ([response.headers["Set-Cookie"]] if "Set-Cookie" in response.headers else [])
        )
        count = 0
        for raw in raw_headers:
            sc: SimpleCookie = SimpleCookie()
            try:
                sc.load(raw)
            except Exception:
                continue
            for name, morsel in sc.items():
                domain = morsel.get("domain", "").lstrip(".")
                self.import_cookies([{
                    "name": name,
                    "value": morsel.value,
                    "domain": domain or response.url.split("/")[2] if response.url else "",
                    "path": morsel.get("path") or "/",
                    "secure": bool(morsel.get("secure")),
                    "expires": morsel.get("expires") or None,
                }])
                count += 1
        return count

    def clear_cookies(self, domain: str | None = None) -> None:
        """Clear cookies, optionally filtered to a specific domain.

        When domain is None, all cookies are cleared.
        When domain is specified, only cookies matching that domain are removed.
        Domain-targeted clearing is safe for injected sessions (avoids clearing
        unrelated cookies managed by the session owner).
        """
        if domain is None:
            self._jar.clear()
        else:
            self._jar.clear_domain(domain)
