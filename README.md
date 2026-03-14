# Minol Kundenportal Scraper

A Python scraper that authenticates to the Minol Kundenportal and fetches consumption data (heating, warm water, cold water) on a per-room basis. Pure Python — stdlib only, no third-party dependencies.

For authentication internals, data endpoint reference, and debugging, see [DEVELOPMENT.md](DEVELOPMENT.md).

---

## Credentials

Credentials are resolved in order: **CLI arguments > environment variables > config file**.

| Source | Email | Password | User Number |
|---|---|---|---|
| CLI | `--email` | `--password` | `--user-num` |
| Env var | `MINOL_EMAIL` | `MINOL_PASSWORD` | `MINOL_USER_NUM` |
| Config file | `email` | `password` | `user_num` |

The default config file location is `~/.minol.json` (override with `--config`):

```json
{
  "email": "user@example.com",
  "password": "password",
  "user_num": "000000000000"
}
```

### Password security

**Avoid `--password` on shared systems.** Any value passed via `--password` is visible to other local users in the process listing (`ps aux`) and in `/proc/PID/cmdline` for the lifetime of the process.

Safer alternatives, in order of preference:

1. **Config file** — store credentials in `~/.minol.json` and restrict access:
   ```bash
   chmod 600 ~/.minol.json
   ```
   The scraper warns at startup if the file is readable by group or other users.

2. **Environment variables** — set `MINOL_EMAIL`, `MINOL_PASSWORD`, and `MINOL_USER_NUM` in your shell profile or via a secrets manager.

3. **`--password-stdin`** — pipe the password from a secrets store or a variable, avoiding it ever appearing in the argument list:
   ```bash
   echo "$MINOL_PASSWORD" | minol --email 'user@example.com' --user-num '000000000000' --password-stdin
   # Or from a file:
   minol --email 'user@example.com' --user-num '000000000000' --password-stdin < ~/.minol_password
   ```

The session cache (`~/.minol_session.json`) is created with permissions `0600` (owner-read-write only) and contains the session token rather than the plaintext password. See [Session Caching](#session-caching).

---

## Installation

Install from PyPI:

```bash
pip install minol
```

Or install from source (clone the repo first):

```bash
pip install .
```

`python -m minol` also works without installation — just clone the repo and run from the project root.

> **Note for bind-mounted filesystems** (e.g. the container setup in CONTAINER.md): the `/workspace` mount does not support atomic file rename, which causes `pip install` to fail with `EPERM`. Install from a `/tmp` copy instead:
> ```bash
> cp -r /workspace/minol /workspace/pyproject.toml /workspace/README.md /workspace/LICENSE /tmp/minol-build/
> pip install /tmp/minol-build
> ```

---

## Usage

All examples use the `minol` console script installed by `pip install minol`. If you are running from source without installing, substitute `python -m minol` for `minol`.

```bash
# Fetch all consumption types, last 12 months
minol \
  --email 'user@example.com' \
  --password 'password' \
  --user-num '000000000000'

# Heating only, specific date range, verbose, save to file
minol \
  --email 'user@example.com' \
  --password 'password' \
  --user-num '000000000000' \
  --type heating \
  --start 202501 \
  --end 202603 \
  --output consumption.json \
  -v

# Warm water in KWH instead of the default M3
minol \
  --email 'user@example.com' \
  --password 'password' \
  --user-num '000000000000' \
  --type warm_water \
  --unit kwh

# Raw API response (unprocessed JSON from the portal)
minol \
  --email 'user@example.com' \
  --password 'password' \
  --user-num '000000000000' \
  --raw

# Credentials from env vars or ~/.minol.json — no flags needed
minol
```

> **Shell escaping** — Passwords containing `$`, `!`, backticks, or backslashes will be mangled by bash in double quotes. Always use single quotes for `--password` and `--email` on the command line, or use `--password-stdin` to avoid the issue entirely.

---

## Output Format

By default the scraper returns structured data with only the relevant fields:

```json
{
  "unit": "KWH",
  "rooms": {
    "Küche": {
      "total": 111.0,
      "device": "04B648FD82639440",
      "monthly": {
        "202503": 0,
        "202504": 5.107,
        "202505": null
      }
    }
  }
}
```

- **`unit`** — `"KWH"` (heating) or `"M3"` (warm water, cold water) by default. Override with `--unit kwh` or `--unit m3`.
- **`rooms`** — keyed by room name; each entry has `total`, `device`, and `monthly` (`null` for months with no data yet).

Pass `--raw` to get the unprocessed API response instead.

---

## Programmatic Usage

```python
from minol import MinolScraper

scraper = MinolScraper("user@example.com", "password", "000000000000")
scraper.login()

# Parsed structured data (default)
heating = scraper.fetch_heating(timeline_start="202501", timeline_end="202603")
warm = scraper.fetch_warm_water()
cold = scraper.fetch_cold_water()
all_data = scraper.fetch_all()

# Override unit of measurement (warm water defaults to M3)
warm_kwh = scraper.fetch_warm_water(unit="kwh")

# Raw API response
all_raw = scraper.fetch_all_raw()
heating_raw = scraper.fetch_heating(raw=True)

# Force fresh login (skip session cache)
scraper.login(use_cache=False)
```

---

## Session Caching

After a successful login the scraper saves session cookies and the token expiry timestamp to `~/.minol_session.json`. On the next run, expired tokens are rejected immediately without a network request; still-valid tokens are restored from the cache, skipping the full SAML login. Pass `--no-cache` to force a fresh login.
