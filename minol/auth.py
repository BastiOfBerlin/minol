"""
SAML authentication flow for the Minol Kundenportal.

Orchestrates a 6-step login dance between the browser/script,
SAP Enterprise Portal, and Azure AD B2C. Includes session caching
so subsequent runs skip the full SAML flow when the token is still valid.
"""

import os
import re
import json
import base64
import logging
from datetime import datetime, timedelta, timezone
from http.cookiejar import Cookie
from pathlib import Path
from urllib.parse import urlencode

from minol._constants import PORTAL_BASE, B2C_DOMAIN, B2C_TENANT, DEFAULT_SESSION_PATH
from minol._http import HttpSession, resolve_url
from minol._utils import parse_forms, parse_sap_ticket

__all__ = ["authenticate"]

log = logging.getLogger(__name__)


def _mask_email(email: str) -> str:
    """Return a partially masked email for safe logging (e.g. 'j***@example.com')."""
    if '@' not in email:
        return '***'
    local, domain = email.split('@', 1)
    return re.sub(r'^(.)', r'\1***', local) + '@' + domain


def _step1_portal_entry(session: HttpSession):
    """GET /?redirect2=true -> picks up PortalAlias, saplb_* cookies."""
    log.info("Step 1: Hitting portal entry point...")
    resp = session.get(f"{PORTAL_BASE}/?redirect2=true")
    log.info(f"  Status: {resp.status_code}")
    log.info(f"  Cookies: {session.cookie_names()}")
    return resp


def _step2_trigger_saml(session: HttpSession) -> tuple[str, str]:
    """GET the SAML login initiator -> follows 302 to B2C.

    Returns (b2c_url, b2c_policy).
    """
    log.info("Step 2: Triggering SAML redirect...")
    saml_init_url = (
        f"{PORTAL_BASE}/minol.com~kundenportal~login~saml/"
        f"?logonTargetUrl=https%3A%2F%2Fwebservices.minol.com%2F"
        f"&saml2idp=B2C-Minol"
    )
    resp = session.get(saml_init_url, allow_redirects=False)
    log.info(f"  Status: {resp.status_code}")

    if resp.status_code != 302:
        raise RuntimeError(f"Expected 302, got {resp.status_code}")

    b2c_url = resp.headers.get("location", "")
    log.info(f"  Redirect to B2C: {b2c_url[:100]}...")

    policy_match = re.search(r'/(B2C_1A_[^/]+)/samlp/sso/login', b2c_url)
    if not policy_match:
        raise RuntimeError("Could not extract B2C policy from redirect URL")

    b2c_policy = policy_match.group(1)
    log.info(f"  Detected B2C policy: {b2c_policy}")
    return b2c_url, b2c_policy


def _extract_state_properties(session: HttpSession, page_html: str) -> str:
    """
    Extract the B2C transaction StateProperties token from the login page.

    Tries two methods:
      1. Regex search in the page HTML/JS
      2. Decode the x-ms-cpim-trans cookie to reconstruct the token

    Raises RuntimeError if neither method succeeds.
    """
    # Method 1: Look for it in the page HTML/JS
    tx_match = re.search(r'StateProperties=([A-Za-z0-9+/=_-]+)', page_html)
    if tx_match:
        tx_value = f"StateProperties={tx_match.group(1)}"
        log.info(f"  Transaction state (from page): {tx_value[:60]}...")
        return tx_value

    # Method 2: Decode x-ms-cpim-trans cookie to get the transaction ID
    trans_cookie = session.get_cookie("x-ms-cpim-trans")
    if trans_cookie:
        trans_data = json.loads(base64.b64decode(trans_cookie))
        tid = trans_data.get("C_ID", "")
        if tid:
            state_json = json.dumps({"TID": tid})
            state_b64 = base64.b64encode(state_json.encode()).decode()
            tx_value = f"StateProperties={state_b64}"
            log.info(f"  Transaction state (from cookie): {tx_value[:60]}...")
            return tx_value

    raise RuntimeError("Could not extract transaction StateProperties")


def _step3_load_b2c_login(session: HttpSession, b2c_url: str) -> tuple[str, str, str]:
    """GET the B2C login page -> picks up CSRF token and session cookies.

    Returns (csrf_token, tx_value, page_html).
    """
    log.info("Step 3: Loading B2C login page...")
    resp = session.get(b2c_url)
    log.info(f"  Status: {resp.status_code}")
    log.info(f"  B2C cookies: {session.cookie_names(B2C_DOMAIN)}")

    csrf_token = session.get_cookie("x-ms-cpim-csrf")
    if not csrf_token:
        raise RuntimeError("Could not find x-ms-cpim-csrf cookie")
    log.debug(f"  CSRF token length: {len(csrf_token)}")

    tx_value = _extract_state_properties(session, resp.text)
    return csrf_token, tx_value, resp.text


def _step4_submit_credentials(session: HttpSession, b2c_policy: str,
                              email: str, password: str,
                              csrf_token: str, tx_value: str, page_html: str):
    """POST credentials to B2C's SelfAsserted endpoint."""
    log.info("Step 4: Submitting credentials to B2C SelfAsserted...")

    # Extract the SelfAsserted URL from the B2C page HTML
    self_asserted_path = f"/{B2C_TENANT}/{b2c_policy}/SelfAsserted"
    sa_match = re.search(r'"(/{0,1}[^"]*?/SelfAsserted[^"]*?)"', page_html)
    if sa_match:
        self_asserted_path = sa_match.group(1)
        log.info(f"  SelfAsserted path from page: {self_asserted_path}")

    self_asserted_url = (
        f"https://{B2C_DOMAIN}{self_asserted_path}"
        f"?tx={tx_value}"
        f"&p={b2c_policy}"
    )
    log.info(f"  SelfAsserted URL: {self_asserted_url[:120]}...")

    # Extract the sign-in field name from the B2C page HTML dynamically
    signin_field = "signInName"  # default fallback
    field_match = re.search(
        r'<input[^>]+id="([^"]*(?:signInName|logonIdentifier|email)[^"]*)"',
        page_html, re.IGNORECASE
    )
    if field_match:
        signin_field = field_match.group(1)
        log.info(f"  Sign-in field from page: {signin_field}")

    payload = urlencode({
        "request_type": "RESPONSE",
        signin_field: email,
        "password": password,
    })
    log.debug(f"  Credential check: email='{_mask_email(email)}', "
              f"password length={len(password) if password else 0}")
    log.debug(f"  Encoded payload (password masked): {payload.split('&password=')[0]}&password=***")

    headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-CSRF-TOKEN": csrf_token,
        "X-Requested-With": "XMLHttpRequest",
        "Origin": f"https://{B2C_DOMAIN}",
        "Referer": f"https://{B2C_DOMAIN}/{B2C_TENANT}/{b2c_policy}/samlp/sso/login",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }

    resp = session.post(self_asserted_url, data=payload, headers=headers)
    log.info(f"  Status: {resp.status_code}")
    log.debug(f"  Response: {resp.text[:300] if resp.text else '(empty)'}")

    if resp.status_code == 200 and resp.text.strip() == "":
        log.info("  Credentials accepted (empty 200)")
    elif resp.status_code == 200:
        try:
            result = json.loads(resp.text)
            status = result.get("status", "")
            if status == "200":
                log.info("  Credentials accepted")
            elif status == "400":
                msg = result.get("message", "Unknown error")
                raise RuntimeError(f"B2C authentication failed: {msg}")
            else:
                log.warning(f"  Unexpected status in response: {result}")
        except json.JSONDecodeError:
            log.warning(f"  Non-JSON response: {resp.text[:200]}")
    else:
        raise RuntimeError(f"SelfAsserted request failed with HTTP {resp.status_code}")

    return resp


def _step5_get_saml_response(session: HttpSession, b2c_policy: str,
                             csrf_token: str, tx_value: str) -> tuple[str, dict]:
    """GET CombinedSigninAndSignup/confirmed -> returns (acs_url, form_fields)."""
    log.info("Step 5: Confirming authentication, retrieving SAML response...")

    confirmed_url = (
        f"https://{B2C_DOMAIN}/{B2C_TENANT}/{b2c_policy}"
        f"/api/CombinedSigninAndSignup/confirmed"
        f"?rememberMe=false"
        f"&csrf_token={csrf_token}"
        f"&tx={tx_value}"
        f"&p={b2c_policy}"
    )

    resp = session.get(confirmed_url)
    log.info(f"  Status: {resp.status_code}")

    forms = parse_forms(resp.text)
    if not forms:
        log.warning("  No form found in response, checking for alternative formats...")
        log.debug(f"  Response body: {resp.text[:500]}")
        raise RuntimeError("Could not find SAML response form in B2C response")

    form = next((f for f in forms if "SAMLResponse" in f["fields"]), None)
    if not form:
        log.warning(f"  Found {len(forms)} form(s) but none contain SAMLResponse")
        for f in forms:
            log.debug(f"  Form action={f['action']}, fields={list(f['fields'].keys())}")
        raise RuntimeError("SAMLResponse not found in any form")

    log.info(f"  Form action: {form['action']}")
    log.debug(f"  SAMLResponse length: {len(form['fields']['SAMLResponse'])}")
    for k, v in form["fields"].items():
        if k != "SAMLResponse":
            log.debug(f"  {k} length: {len(v)}")

    return form["action"], form["fields"]


def _step6_post_to_sap_acs(session: HttpSession, acs_url: str, form_data: dict):
    """POST SAMLResponse to SAP's ACS endpoint, follow chained form POSTs."""
    log.info("Step 6: Posting SAML response to SAP ACS...")

    resp = session.post(
        acs_url,
        data=urlencode(form_data),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        allow_redirects=False
    )
    log.info(f"  ACS response status: {resp.status_code}")

    if resp.status_code == 200:
        forms = parse_forms(resp.text)
        if forms:
            next_url = resolve_url(forms[0]["action"])
            log.info(f"  Chained form POST to: {next_url}")

            resp2 = session.post(
                next_url,
                data=urlencode(forms[0]["fields"]),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                allow_redirects=False
            )
            log.info(f"  Portal login response status: {resp2.status_code}")

            if resp2.status_code == 302:
                location = resolve_url(resp2.headers.get("location", ""))
                log.info(f"  Redirect to: {location}")
                resp3 = session.get(location)
                log.info(f"  Final status: {resp3.status_code}")

    elif resp.status_code == 302:
        location = resolve_url(resp.headers.get("location", ""))
        log.info(f"  Direct redirect to: {location}")
        session.get(location)

    mysapsso2 = session.get_cookie("MYSAPSSO2")

    if mysapsso2:
        log.info(f"  MYSAPSSO2 obtained (length: {len(mysapsso2)})")
    else:
        log.error("  MYSAPSSO2 not found in cookies!")
        log.info(f"  All cookies: {session.all_cookies()}")
        raise RuntimeError("Authentication failed: MYSAPSSO2 cookie not set")


def _save_session(session: HttpSession, user_num: str, path: Path, status_fn=None):
    """Serialize cookies and expiry info to a JSON cache file (mode 0o600)."""
    cookies = [
        {"name": c.name, "value": c.value, "domain": c.domain,
         "path": c.path, "secure": c.secure, "expires": c.expires}
        for c in session.cookie_jar
    ]

    expires_at = None
    mysapsso2 = session.get_cookie("MYSAPSSO2")
    if mysapsso2:
        log.info(f"  MYSAPSSO2 value length: {len(mysapsso2)}")
        ticket = parse_sap_ticket(mysapsso2)
        if ticket:
            expiry = ticket["created_at"].replace(tzinfo=timezone.utc) + timedelta(hours=ticket["valid_hours"])
            expires_at = expiry.isoformat()
            log.info(f"  Token created: {ticket['created_at'].isoformat()}, "
                     f"valid for {ticket['valid_hours']}h, "
                     f"expires: {expires_at}")
        else:
            log.warning("  Could not parse MYSAPSSO2 token (expires_at will be null). "
                        "Run with -v for detailed ticket parsing output.")
    else:
        log.warning("  MYSAPSSO2 cookie not found -- cannot extract expiry.")

    cache = {
        "user_num": user_num,
        "expires_at": expires_at,
        "cookies": cookies,
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(cache, f, indent=2)
        if status_fn:
            status_fn(f"Session cached to {path}.")
        log.info(f"Session saved to {path}")
    except OSError as exc:
        log.warning("Could not save session cache to %s: %s", path, exc)


def _restore_session(session: HttpSession, user_num: str, path: Path,
                     status_fn=None) -> bool:
    """
    Restore session cookies from cache if still valid.

    Returns True if the cache was loaded and the token has not expired,
    False if the cache is missing, for a different user, expired, or unreadable.
    When expires_at is absent or unparseable, the cache is rejected.
    """
    if not path.is_file():
        if status_fn:
            status_fn("No cached session found.")
        return False

    try:
        with open(path) as f:
            cache = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.warning(f"Could not read session cache: {e}")
        return False

    if cache.get("user_num") != user_num:
        log.info("Session cache is for a different user number, ignoring.")
        return False

    expires_at = cache.get("expires_at")
    if not expires_at:
        log.info("Session cache has no expiry info, rejecting.")
        return False

    try:
        expiry = datetime.fromisoformat(expires_at)
    except ValueError:
        log.warning(f"Session cache has unparseable expires_at: {expires_at!r}, rejecting.")
        return False

    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)

    if datetime.now(timezone.utc) >= expiry:
        if status_fn:
            status_fn(f"Cached session expired at {expires_at}.")
        return False

    remaining = expiry - datetime.now(timezone.utc)
    log.info(f"Token expires at {expires_at} ({int(remaining.total_seconds()) // 60} min remaining).")

    for c in cache.get("cookies", []):
        cookie = Cookie(
            version=0,
            name=c["name"],
            value=c["value"],
            port=None,
            port_specified=False,
            domain=c["domain"],
            domain_specified=bool(c["domain"]),
            domain_initial_dot=c["domain"].startswith(".") if c["domain"] else False,
            path=c["path"],
            path_specified=bool(c["path"]),
            secure=c["secure"],
            expires=c["expires"],
            discard=c["expires"] is None,
            comment=None,
            comment_url=None,
            rest={},
        )
        session.cookie_jar.set_cookie(cookie)

    log.info(f"Restored {len(cache.get('cookies', []))} cookies from cache.")

    if not session.get_cookie("MYSAPSSO2"):
        log.warning("MYSAPSSO2 not present in restored cookies, rejecting cache.")
        session.cookie_jar.clear()
        return False

    if status_fn:
        status_fn("Using cached session.")
    return True


def authenticate(
    session: HttpSession,
    email: str,
    password: str,
    user_num: str,
    status_fn=None,
    use_cache: bool = True,
    session_path: Path = None,
) -> None:
    """
    Authenticate to the Minol portal.

    Tries to restore a cached session first (unless use_cache=False or
    session_path=None-and-no-default). Falls back to the full 6-step SAML flow,
    then persists the new session to cache.

    Args:
        session: HTTP session to use for requests.
        email: Login email address.
        password: Login password.
        user_num: 12-digit user number (for cache scoping and data requests).
        status_fn: Optional callback for progress messages (called with a string).
        use_cache: If False, always perform a fresh login (skip restore and save).
        session_path: Path to the session cache file. Defaults to DEFAULT_SESSION_PATH.
    """
    path = session_path or DEFAULT_SESSION_PATH

    if use_cache and _restore_session(session, user_num, path, status_fn):
        return

    if status_fn:
        status_fn("Authenticating...")
    log.info("Starting Minol portal authentication...")

    _step1_portal_entry(session)
    b2c_url, b2c_policy = _step2_trigger_saml(session)
    csrf_token, tx_value, page_html = _step3_load_b2c_login(session, b2c_url)
    _step4_submit_credentials(session, b2c_policy, email, password,
                              csrf_token, tx_value, page_html)
    acs_url, form_data = _step5_get_saml_response(session, b2c_policy,
                                                   csrf_token, tx_value)
    _step6_post_to_sap_acs(session, acs_url, form_data)

    if status_fn:
        status_fn("Authentication successful.")

    if use_cache:
        _save_session(session, user_num, path, status_fn)
