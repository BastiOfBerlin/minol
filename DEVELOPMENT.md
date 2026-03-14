# Development Guide

Internals, authentication flow, data endpoint reference, debugging, and security notes for contributors and anyone maintaining this scraper.

---

## Authentication Flow

The login is a 6-step SAML dance between three parties:

```
Script ←→ SAP Portal (webservices.minol.com) ←→ Azure B2C (minolauth.b2clogin.com)
```

All steps live in `minol/auth.py` as module-level functions.

### Step 1 — Portal Entry (`_step1_portal_entry`)

`GET /?redirect2=true` — picks up `PortalAlias` and `saplb_*` cookies from SAP. These are required for load balancer stickiness (all subsequent requests must land on the same J2EE node).

### Step 2 — Trigger SAML Redirect (`_step2_trigger_saml`)

`GET /minol.com~kundenportal~login~saml/?logonTargetUrl=...&saml2idp=B2C-Minol` — SAP returns 302 to B2C with a `SAMLRequest`. The B2C policy name is extracted dynamically from the redirect URL (SAP alternates between `B2C_1A_Signup_Signin_Groups_SAML` and `B2C_1A_Signup_Signin_Groups_SAML-4`).

### Step 3 — Load B2C Login Page (`_step3_load_b2c_login`)

`GET` the B2C login page — picks up `x-ms-cpim-csrf`, `x-ms-cpim-cache`, and `x-ms-cpim-trans` cookies. The `StateProperties` transaction token is extracted from the page HTML (regex) or constructed from the `x-ms-cpim-trans` cookie (fallback).

### Step 4 — Submit Credentials (`_step4_submit_credentials`)

`POST` to `/{tenant}/{policy}/SelfAsserted?tx=...&p=...` with form-encoded body:

```
request_type=RESPONSE&signInName=<email>&password=<pass>
```

The SelfAsserted URL path and sign-in field name (`signInName` or `logonIdentifier`) are extracted from the B2C page HTML to handle policy changes. Requires `X-CSRF-TOKEN` and `X-Requested-With: XMLHttpRequest` headers. Returns `{"status":"200"}` on success or empty 200.

**Note**: B2C returns a generic "invalid credentials" message for many failure modes (bad CSRF, wrong field names, wrong Origin/Referer), not just wrong passwords.

### Step 5 — Retrieve SAML Response (`_step5_get_saml_response`)

`GET /{tenant}/{policy}/api/CombinedSigninAndSignup/confirmed?...` — B2C returns HTML with an auto-submit `<form>` containing `SAMLResponse` and `RelayState`.

### Step 6 — POST to SAP ACS (`_step6_post_to_sap_acs`)

`POST SAMLResponse` to `/saml2/sp/acs`, then follow a chained form POST to `/minol.com~kundenportal~login~saml/`. Form actions and `Location` headers may be relative paths — resolved against `PORTAL_BASE`. SAP validates the SAML assertion and issues the `MYSAPSSO2` cookie. The assertion has a ~5-minute validity window; if it expires between steps 5 and 6, the login fails.

---

## Critical Cookies

| Cookie | Issued By | Purpose |
|---|---|---|
| `PortalAlias=portal` | SAP | Portal identification |
| `saplb_*` | SAP | Load balancer stickiness (routes to same J2EE node) |
| `JSESSIONID` | SAP | Java EE session |
| `MYSAPSSO2` | SAP | **The SSO token** — signed SAP Logon Ticket, base64-encoded, contains DSA signature. Only cookie needed for data requests. |
| `JSESSIONMARKID` | SAP | Session marker |
| `x-ms-cpim-csrf` | B2C | CSRF protection for B2C API calls |
| `x-ms-cpim-trans` | B2C | Transaction state (JSON with transaction ID, policy, client ID) |
| `x-ms-cpim-cache\|...` | B2C | Encrypted B2C session state |

## Known B2C Identifiers

- **B2C Domain**: `minolauth.b2clogin.com`
- **B2C Tenant**: `minolauth.onmicrosoft.com`
- **B2C Client ID**: `bc12f28f-d4c0-4862-b496-cc144028dafb`
- **SAML SP Entity ID (SAP)**: `EPP`
- **ACS Endpoint**: `https://webservices.minol.com/saml2/sp/acs`
- **SAML Issuer (B2C)**: `B2C-1`

---

## Data Endpoint

All consumption data is fetched from a single REST endpoint:

```
POST https://webservices.minol.com/minol.com~kundenportal~em~web/rest/EMData/readData
Content-Type: application/json
```

### Payload Structure

```json
{
  "userNum": "000000000000",
  "layer": "NE",
  "scale": "CALMONTH",
  "chartRefUnit": "ABS",
  "refObject": "NOREF",
  "consType": "HEIZUNG",
  "dashBoardKey": "PE",
  "timelineStart": "202502",
  "timelineStartTxt": "02.2025",
  "timelineEnd": "202601",
  "timelineEndTxt": "01.2026",
  "valuesInKWH": true,
  "dlgKey": "100EHRAUM"
}
```

### Field Reference

| Field | Notes |
|---|---|
| `userNum` | 12-digit zero-padded customer number |
| `layer` | Always `NE` (Nutzeinheit / usage unit) |
| `scale` | `CALMONTH` for monthly data |
| `timelineStart` / `timelineEnd` | `YYYYMM` format |
| `timelineStartTxt` / `timelineEndTxt` | `MM.YYYY` (German dot-separated display format) |
| `valuesInKWH` | `true` → KWH; `false` → M3. Defaults to `true` for heating, `false` for water. |
| `consType` + `dlgKey` | See table below |

### Consumption Type Mapping

| Type | `consType` | `dlgKey` |
|---|---|---|
| Heating | `HEIZUNG` | `100EHRAUM` |
| Warm Water | `WARMWASSER` | `200RAUM` |
| Cold Water | `KALTWASSER` | `300RAUM` |

---

## Session Cache Internals

The cache file (`~/.minol_session.json`) stores serialised cookies, the user number, and the token expiry timestamp. It is created with mode `0600` (owner-only).

### MYSAPSSO2 Ticket Format

`parse_sap_ticket()` in `_utils.py` decodes the base64 binary ticket:

- **Pre-processing**: URL-decode (`%3D` → `=`), then replace `!` with `+` (SAP substitutes `+` with `!` in cookie values because `+` is not cookie-safe per RFC 6265).
- **Binary layout**: 6-byte header (version + codepage + space), then a first field with no ID (2-byte length + portal identity string), followed by TLV fields: 1-byte ID, 2-byte BE length, value.
  - ID `0x04` — creation timestamp (`YYYYMMddHHmm` ASCII)
  - ID `0x05` — validity in hours (4-byte BE uint, typically 8)

The computed `expires_at` is persisted in the cache. On restore, if `expires_at` is absent or unparseable, the cache is rejected immediately without a network request.

---

## Debugging

Run with `-v` for verbose logging showing every step, cookie, redirect, and response.

### Common Failure Points

- **Shell escaping** — Passwords with `$`, `!`, backticks, or backslashes are mangled by bash in double quotes. Use single quotes for `--password`/`--email`, or use `--password-stdin`.
- **Step 2 — SAML redirect** — `Location` header from SAP may use non-standard casing; the code uses case-insensitive access.
- **Step 3 — StateProperties** — B2C may change how the transaction token is embedded. Two fallback methods are tried: regex on page HTML, then construction from `x-ms-cpim-trans` cookie.
- **Step 4 — SelfAsserted POST** — CSRF token, Origin, and Referer must match exactly. B2C returns a generic error for many failure modes.
- **Step 5 — SAMLResponse form** — If B2C changes the confirmation page HTML structure, `parse_forms()` may fail. Check raw HTML in debug output.
- **Step 6 — ACS POST chain** — Must follow the chained form POST. If `MYSAPSSO2` is absent, the SAML assertion may have expired (5-minute window).

### Capturing a Fresh HAR

If the login flow breaks, capture a new HAR from the browser:

1. Open Chrome DevTools → Network tab, enable "Preserve log"
2. Navigate to `https://webservices.minol.com/?redirect2=true`
3. Complete the login
4. Right-click in the network list → "Save all as HAR"
5. Filter to Doc/XHR/Fetch to reduce noise

The key request is the `SelfAsserted` POST — capture it as a curl command for exact headers and payload.

---

## Security

### Credential Handling

- **Prefer env vars or config file over `--password`** — CLI passwords are visible in `ps aux` and `/proc/PID/cmdline`.
- **`--password-stdin`** — reads from stdin, avoids the password ever appearing in the argument list.
- **Config file** — `~/.minol.json` contains plaintext credentials; restrict with `chmod 600 ~/.minol.json`. The CLI warns if the file is group- or world-readable.
- **Session cache** — written with mode `0600`. Do not copy to world-readable locations.

### Verbose Logging

`-v` logs a masked email (first char + `***@domain`) and password length only. It never logs password characters or token values. Note that the CSRF token is included in request URLs visible in debug output. Avoid redirecting verbose output to world-readable log files.
