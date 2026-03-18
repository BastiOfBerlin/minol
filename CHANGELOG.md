# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.3.0] — 2026-03-18

### Added

- **`aiohttp` dependency** — native async I/O replaces `asyncio.to_thread`-wrapped urllib.
  aiohttp is the sole third-party dependency; the general
  minimal-dependency principle still applies for everything else.
- **Session injection** — `MinolScraper` and `HttpSession` now accept an optional
  `session: aiohttp.ClientSession` parameter. When provided the library uses the
  caller's managed session, and existing cookies in that session are never cleared.
  When omitted, a private session is created and owned by the scraper (same behaviour as before).
- **`MinolScraper.close()`** — async method that closes the underlying HTTP session.
  No-op when an external session was injected.
- **`async with MinolScraper(...)` support** — `__aenter__`/`__aexit__` call `close()`
  automatically. The CLI uses `async with scraper:` internally.
- **`HttpSession` cookie helpers** — `export_cookies()`, `import_cookies()`, and
  `clear_cookies()` encapsulate all cookie-jar access. Existing helpers
  (`get_cookie`, `cookie_names`, `all_cookies`) are unchanged in signature.

### Changed

- **`HttpSession` internals rewritten** — urllib, `http.cookiejar`, `_NoRedirectHandler`,
  and `asyncio.to_thread` are gone. HTTP I/O is now fully native async via
  `aiohttp.ClientSession`. `allow_redirects` is passed per-request natively.
- **`auth.py` cookie access** — `_build_cache_data()` uses `session.export_cookies()`
  and `_load_cache_data()` uses `session.import_cookies()` / `session.clear_cookies()`,
  replacing direct `http.cookiejar.Cookie` construction and `CookieJar` manipulation.
  Cache-rejection cleanup is now domain-targeted (safe for injected sessions).

### Fixed

- **CI: install dependencies before running tests** — the `test` job now runs
  `pip install .` so `aiohttp` (and its transitive dependencies) are available
  when the test suite runs. Previously, tests failed with
  `ModuleNotFoundError: No module named 'aiohttp'`.

- **B2C SSO short-circuit** — if B2C already has an active SSO session it skips
  the login form and returns a SAML assertion directly in step 3. The scraper now
  detects a `SAMLResponse` form in the step-3 response and jumps straight to
  step 6 (ACS POST), bypassing credential submission (steps 4-5) that would
  never succeed in this case.
- **B2C cookie fallback** — `HttpSession._extract_cookies_from_headers()` manually
  parses `Set-Cookie` headers and injects them into the cookie jar when aiohttp's
  automatic processing silently drops them (observed with some B2C response
  formats). The fallback is tried automatically in step 3 before raising.

## [1.2.0] — 2026-03-16

### Added

- **In-memory session caching** — `MinolScraper.login()` now accepts a
  `session_data` dict. Pass `{}` on the first call; the returned dict can be
  stored and passed back on subsequent calls. If the token is still valid it is
  restored without any network requests; if it has expired a fresh login runs
  and a new dict is returned. No files are read or written. Intended for
  integrations that manage their own storage (e.g. Home Assistant).

### Changed

- **Async public API** — all I/O methods on `MinolScraper` (`login`,
  `fetch_consumption`, `fetch_heating`, `fetch_warm_water`, `fetch_cold_water`,
  `fetch_all`, `fetch_all_raw`) and `auth.authenticate()` are now `async def`.
  Use `await scraper.login()` / `await scraper.fetch_all()`, or call from
  `asyncio.run(...)`.  The CLI (`python -m minol`) is unaffected.
- **`fetch_all()` fetches in parallel** — the three consumption types are now
  fetched concurrently via `asyncio.gather()`.
- **`HttpSession.get()` / `.post()` are async** — the underlying urllib I/O
  runs in a thread pool via `asyncio.to_thread()`, preserving the
  zero-dependency constraint.

### Fixed

- **`MinolScraper` default `status_fn` is silent** — constructing
  `MinolScraper` without a `status_fn` no longer prints progress messages to
  stderr. Library callers now get quiet-by-default behaviour, consistent with
  `auth.authenticate()`. The CLI explicitly passes its own stderr printer.

- **`load_config()` and `resolve_credential()` raise `ValueError`** instead of
  `SystemExit`, making them safe to call from library code. `main()` catches
  `ValueError` and converts it to `SystemExit` so CLI behaviour is unchanged.

### Removed

- `load_config` and `resolve_credential` are no longer exported from the
  top-level `minol` package (`__init__.py`). They were CLI helpers that
  shouldn't have been part of the public library API. They remain importable
  directly via `from minol.cli import load_config, resolve_credential`.

## [1.1.1] — 2026-03-15

### Fixed

- CI: avoid `git fetch --tags` refspec conflict when the workflow is triggered
  by a direct tag push (not via the Codeberg mirror).

## [1.1.0] — 2026-03-15

### Added

- `session_path` parameter on `MinolScraper.login()` to override the default
  session cache file location (`~/.minol_session.json`).
- `--session-path` CLI flag to pass a custom session cache file path.

## [1.0.1] — 2026-03-14

### Added

- Initial public release on PyPI.
- 6-step SAML 2.0 authentication flow (SAP Enterprise Portal + Azure AD B2C).
- `MinolScraper` class with `fetch_heating()`, `fetch_warm_water()`,
  `fetch_cold_water()`, and `fetch_all()`.
- File-based session caching with expiry detection.
- Zero third-party dependencies (stdlib only).
- `minol` console script entry point.
- GitHub Actions CI/CD with PyPI trusted publishing via OIDC.

[1.3.0]: https://codeberg.org/BastiOfBerlin/minol/compare/v1.2.0...v1.3.0
[1.2.0]: https://codeberg.org/BastiOfBerlin/minol/compare/v1.1.1...v1.2.0
[1.1.1]: https://codeberg.org/BastiOfBerlin/minol/compare/v1.1.0...v1.1.1
[1.1.0]: https://codeberg.org/BastiOfBerlin/minol/compare/v1.0.1...v1.1.0
[1.0.1]: https://codeberg.org/BastiOfBerlin/minol/releases/tag/v1.0.1
