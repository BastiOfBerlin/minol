# Minol Kundenportal Scraper

A Python scraper that authenticates to the Minol Kundenportal (SAP Enterprise Portal + Azure AD B2C via SAML 2.0) and fetches per-room consumption data. One third-party dependency: **aiohttp** (required for native async I/O and Home Assistant websession injection).

## Package Structure

```
minol/
    __init__.py      # Public API: re-exports MinolScraper, CONSUMPTION_TYPES, __version__
    __main__.py      # `python -m minol` support → calls cli.main()
    _constants.py    # PORTAL_BASE, B2C_*, CONSUMPTION_TYPES, default paths
    _http.py         # HttpSession, HttpResponse, resolve_url (aiohttp-based)
    _utils.py        # parse_forms(), parse_sap_ticket()
    auth.py          # 6-step SAML login + session caching as standalone functions
    lib.py           # MinolScraper class (data fetching, login orchestration)
    cli.py           # argparse, load_config(), resolve_credential(), main()
pyproject.toml       # [project.scripts] minol = "minol.cli:main"
```

### Dependency Graph (no cycles)

```
_constants.py  ← leaf, no internal imports
_utils.py      ← leaf, only stdlib
_http.py       ← imports _constants
auth.py        ← imports _constants, _http, _utils
lib.py         ← imports _constants, _http, auth
cli.py         ← imports lib, _constants
__init__.py    ← imports lib, cli, _constants
```

## Design Decisions & Constraints

- **aiohttp as deliberate exception to minimal-dependency principle** — required for native async I/O (HA `async-dependency` quality scale item) and Home Assistant websession injection (`inject-websession`). All other dependencies remain stdlib-only.
- **Async public API** — all I/O methods (`HttpSession.get/post`, `auth.authenticate`, all `MinolScraper` methods) are `async def`. Use `await` or `asyncio.run()`. `cli.main()` remains sync and calls `asyncio.run(_async_main(...))` internally, so CLI usage is unchanged.
- **Native async via aiohttp** — `HttpSession._request()` uses `aiohttp.ClientSession.request()` directly. No `asyncio.to_thread()`.
- **`fetch_all()` parallelism** — uses `asyncio.gather()` to fetch all three consumption types concurrently.
- **Session injection** — `HttpSession` and `MinolScraper` accept an optional `aiohttp.ClientSession`. When provided the caller's session is used (no ownership, `close()` is a no-op). When omitted, a private session is created lazily on the first request.
- **Lazy aiohttp session creation** — `ClientSession` is created on first request to avoid requiring a running event loop at construction time. The `CookieJar` is created eagerly (using a temporary event loop) so cookie helpers work immediately.
- **Cookie access encapsulated** — `export_cookies()` / `import_cookies()` / `clear_cookies()` on `HttpSession` replace direct `CookieJar` access. `clear_cookies(domain)` is domain-targeted, safe for injected sessions.
- **`HttpResponse` stable interface** — aiohttp response details are wrapped in `HttpResponse` so no aiohttp types leak to callers.
- **Auth as standalone functions** — `auth.py` has module-level `_stepN_` functions; `MinolScraper` delegates fully to `auth.authenticate()`.
- **`status_fn` stays sync** — it's a simple `print()` callback; no benefit from async.
- **File I/O in cache functions stays sync** — tiny JSON files, negligible latency; no benefit from async.
- **Logging** — each module uses `logging.getLogger(__name__)`; `logging.basicConfig()` is only called in `cli.main()`, not at import time.
- **Dynamic B2C policy detection** — policy name extracted from the SAP redirect URL, not hardcoded.
- **Regex form parsing** — SAML auto-submit pages are machine-generated; regex is reliable. `parse_forms()` handles both attribute orderings (`name=... value=...` and `value=... name=...`).
- **Relative URL resolution** — SAP may return relative paths in form actions and `Location` headers; resolve against `PORTAL_BASE` before use.
- **Clean Architecture/Clean Code** — Follow design principles to keep a maintainable code base.

## CI/CD

GitHub Actions workflow at `.github/workflows/ci.yml` runs on every push to `main`, every PR, and every `v*` tag.

### Jobs

| Job | Trigger | What it does |
|---|---|---|
| `test` | all | `python -m unittest discover -s tests -v` on Python 3.10 and 3.14 |
| `build` | all | `python -m build` + `twine check dist/*`; uploads `dist/` artifact; detects version tag |
| `publish` | tagged commits only | Downloads artifact, publishes to PyPI via OIDC trusted publishing |

`test` and `build` run in parallel. `publish` requires both to succeed.

Concurrency: in-progress runs on the same branch/PR are cancelled; tag-triggered runs are never cancelled.

#### Mirror quirk: tag detection

The Codeberg→GitHub mirror fires only a **branch push** event when syncing a new tag — it does not trigger a separate tag-push workflow run. Because of this, `publish` cannot gate on `github.ref == refs/tags/v*`.

Instead, the `build` job runs a conditional `git fetch --tags` step (only when `github.ref` is not already a tag ref) and then `git tag --points-at HEAD` to detect whether the current commit carries a `v*` tag. The result is exposed as the `version-tag` job output, and `publish` gates on `needs.build.outputs.version-tag != ''`. This works regardless of which event triggered the workflow.

Note: `fetch-tags: true` on the checkout step is intentionally avoided — it causes a refspec conflict when the workflow is triggered directly by a tag push.

### Release Process

1. Update `version` in `pyproject.toml` and commit.
2. Tag and push:
   ```bash
   git tag v1.0.1
   git push origin v1.0.1
   ```
3. The Codeberg→GitHub mirror syncs both the commit and the tag. The branch-push event triggers the workflow; `build` detects the tag on HEAD and `publish` runs automatically.

## PyPI Publishing

The package is configured for PyPI publishing:

- **License**: MIT (`LICENSE` file, PEP 639 style in `pyproject.toml`)
- **Metadata**: authors, readme, keywords, classifiers, Python version range
- **`__version__`**: exposed via `importlib.metadata.version("minol")` in `minol/__init__.py`
- **Build**: `python -m build` from a `/tmp` copy (see pip install limitation in README.md)
- **Validate**: `twine check dist/*` before uploading

To build and validate locally:
```bash
mkdir -p /tmp/minol-build
cp -r /workspace/minol /workspace/pyproject.toml /workspace/README.md /workspace/LICENSE /tmp/minol-build/
cd /tmp/minol-build && python -m build
twine check dist/*
```

## Testing

The test suite lives in `tests/` and uses `unittest` (stdlib only — consistent with the zero-dependency constraint). Each test file mirrors one source module.

```
tests/
    __init__.py       # empty
    test_utils.py     # parse_forms(), parse_sap_ticket()
    test_http.py      # HttpResponse, HttpSession, resolve_url()
    test_auth.py      # authenticate(), session cache, steps 1–6
    test_lib.py       # MinolScraper (init, login, fetch, parse)
    test_cli.py       # load_config(), resolve_credential(), main()
```

Run all tests:
```bash
python -m unittest discover -s tests -v
```

Run a single file:
```bash
python -m unittest tests/test_utils.py -v
```

`pytest` also works if available (`python -m pytest tests/ -v`), but is not required.

## Things to Keep in Mind

- Determine whether `CLAUDE.md`, `README.md` and/or `DEVELOPMENT.md` should be updated after changes to relevant other files.
- Keep the `CHANGELOG.md` up-to-date.

## Potential Improvements

- Find out how user-num can be acquired by only knowing username/password.
- Add retry logic for transient failures.
- Explore additional API endpoints (the portal likely has more data views).
- Consider whether DEVELOPMENT.md should include B2C client IDs and internal identifiers (makes reconnaissance easier).
- Document why the User-Agent string impersonates Chrome (SAP/B2C may reject non-browser UAs).
- Add contribution guidelines (CONTRIBUTING.md) and/or code of conduct for open-source readiness.
- Review logging level consistency across auth steps (some INFO content should be DEBUG).

## Further Reading

- [README.md](README.md) — installation, credentials, usage, output format
- [CHANGELOG.md](CHANGELOG.md) — version history and release notes
- [DEVELOPMENT.md](DEVELOPMENT.md) — auth flow internals, data endpoint, session cache, debugging, security
