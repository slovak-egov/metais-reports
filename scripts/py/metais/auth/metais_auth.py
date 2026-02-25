"""
MetaIS OIDC Authorization Code + PKCE login helper.

see discovery document: https://metais.slovensko.sk/iam/.well-known/openid-configuration

Public API:
  - bearer_from_user_pass_plain(username, password, verbose=False, ...)
  - bearer_from_client_credentials(cient_id, client_secret, auth_method = "client_secret_basic" | "client_secret_post")

"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import secrets
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qs, urlparse, urlsplit, urlunsplit

import requests

from metais.common.url_utils import strip_query

DEFAULT_BASE = "test"
DEFAULT_CLIENT_ID = "webPortalClient"
ENV_BASES = {
    "metais": "https://metais.slovensko.sk",
    "metais-prod": "https://metais.slovensko.sk",
    "prod": "https://metais.slovensko.sk",
    "test": "https://metais-test.slovensko.sk",
    "metais-test": "https://metais-test.slovensko.sk",
}

def _resolve_base(base: str) -> str:
    b = (base or "").strip()
    if not b: # return test by default
        return "https://metais-test.slovensko.sk"
    b = ENV_BASES.get(b, b)

    # accept hosts like "metais.slovensko.sk" too
    if not re.match(r"^https?://", b):
        b = "https://" + b

    return b.rstrip("/")

def _default_redirect_uri(base: str) -> str:
    return f"{base}/auth-success"

###########
# Helpers #
###########

def _b64url(raw: bytes) -> str:
    """Base64url without '=' padding, as required by PKCE."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _make_pkce() -> tuple[str, str]:
    """Return (code_verifier, code_challenge)."""
    verifier = _b64url(os.urandom(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("utf-8")).digest())
    return verifier, challenge


def _rand_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def _extract_csrf(html: str) -> str:
    """
    Extract CSRF token from HTML.
    Supports:
      - <input type="hidden" name="_csrf" value="...">
      - <meta name="_csrf" content="...">
    """
    m = re.search(r'name=["\']_csrf["\'][^>]*value=["\']([^"\']+)["\']', html)
    if m:
        return m.group(1)

    m = re.search(r'<meta[^>]*name=["\']_csrf["\'][^>]*content=["\']([^"\']+)["\']', html)
    if m:
        return m.group(1)

    raise RuntimeError("Could not find CSRF token in HTML.")


def _sec_to_readable(seconds: float) -> str:
    # keep your original style, but a bit shorter/cleaner
    seconds = float(seconds)
    hrs = int(seconds // 3600)
    seconds -= 3600 * hrs
    mins = int(seconds // 60)
    seconds -= 60 * mins
    whole_s = int(seconds)
    ms = int(round((seconds - whole_s) * 1000))

    parts = []
    if hrs:
        parts.append(f"{hrs}h")
    if mins:
        parts.append(f"{mins}min")
    if whole_s:
        parts.append(f"{whole_s}s")
    if ms:
        parts.append(f"{ms}ms")
    return ", ".join(parts) if parts else "0s"


def _abs_url(base: str, maybe_relative: str) -> str:
    if maybe_relative.startswith("/"):
        return base.rstrip("/") + maybe_relative
    return maybe_relative


# helpers for printing
def _redact(s: str, keep: int = 4) -> str:
    if not s:
        return "<empty>"
    if len(s) <= keep * 2:
        return "<redacted>"
    return f"{s[:keep]}…{s[-keep:]}"

def _cookie_names_only(cookies_dict: dict) -> dict:
    return {k: "<redacted>" for k in cookies_dict.keys()}




@dataclass(frozen=True)
class BearerResult:
    access_token: str
    expires_in: Optional[float] = None
    scope: Optional[str] = None


#############
# Core flow #
#############

def _bearer_from_user_pass_core(
    *,
    username: str,
    password: str,
    verbose: bool = False,
    base: str = DEFAULT_BASE,
    client_id: str = DEFAULT_CLIENT_ID,
    redirect_uri: Optional[str] = None,
    timeout: float = 30.0,
    user_agent: str = "metais-bot/1.0",
    verify_tls: bool = True,
    debug_html_path: Optional[str] = None,
    max_redirect_hops: int = 40,
) -> BearerResult:
    """
    Performs the OIDC Authorization Code + PKCE flow and returns BearerResult.
    """
    base = _resolve_base(base)
    if redirect_uri is None:
        redirect_uri = _default_redirect_uri(base)

    s = requests.Session()
    s.headers["User-Agent"] = user_agent

    code_verifier, code_challenge = _make_pkce()

    state = _rand_token(32)
    nonce = _rand_token(32)

    auth_params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": "openid",
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
        "nonce": nonce,
    }

    # 1) Start authorize flow (allow redirects to build server-side transaction state)
    r = s.get(
        f"{base}/iam/authorize",
        params=auth_params,
        allow_redirects=True,
        timeout=timeout,
        verify=verify_tls,
    )

    if verbose:
        print("After authorize final URL:", strip_query(r.url))
        if r.history:
            print("Redirect chain:")
            for h in r.history:
                print(" ", h.status_code, strip_query(h.url), "->", strip_query(h.headers.get("Location")))
        print("Cookies:", _cookie_names_only(s.cookies.get_dict()))

    # 2) Ensure we are on the username/pass login page
    if "/iam/usernamePassLogin" not in r.url:
        r = s.get(
            f"{base}/iam/usernamePassLogin",
            allow_redirects=True,
            timeout=timeout,
            verify=verify_tls,
        )

    if debug_html_path:
        try:
            with open(debug_html_path, "w", encoding="utf-8") as f:
                f.write(r.text)
        except Exception as e:
            if verbose:
                print(f"[warn] Failed to write debug HTML to {debug_html_path}: {e}")

    csrf = _extract_csrf(r.text)
    if verbose:
        print("CSRF:", _redact(csrf))

    # 3) Submit credentials (expect redirect)
    login = s.post(
        f"{base}/iam/usernamePassLogin",
        data={"_csrf": csrf, "username": username, "password": password},
        allow_redirects=False,
        timeout=timeout,
        verify=verify_tls,
    )

    if verbose:
        print("Login status:", login.status_code)
        print("Login Location:", strip_query(login.headers.get("Location")))
        print("Cookies after login:", _cookie_names_only(s.cookies.get_dict()))

    if login.status_code not in (302, 303):
        body_preview = (login.text or "")[:400]
        raise RuntimeError(
            f"Login did not redirect as expected: {login.status_code}\n{body_preview}"
        )

    next_url = _abs_url(base, login.headers["Location"])

    # 4) Follow redirects until we see ?code=
    final_url = None
    hops = 0
    while True:
        hops += 1
        if hops > max_redirect_hops:
            raise RuntimeError(f"Too many redirects while hunting for code (>{max_redirect_hops}).")

        step = s.get(
            next_url,
            allow_redirects=False,
            timeout=timeout,
            verify=verify_tls,
        )

        # Sometimes code shows up on the response URL already.
        if "code=" in step.url:
            final_url = step.url
            break

        loc = step.headers.get("Location")
        if not loc:
            body_preview = (step.text or "")[:200]
            raise RuntimeError(
                f"Stopped without redirect. URL={strip_query(step.url)} status={step.status_code} body={body_preview}"
            )

        if "code=" in loc:
            final_url = loc
            break

        next_url = _abs_url(base, loc)

    if verbose:
        print("Final redirect containing code:", strip_query(final_url))

    q = parse_qs(urlparse(final_url).query)
    code_list = q.get("code")
    if not code_list:
        raise RuntimeError(f"No code param found in: {strip_query(final_url)}")
    code = code_list[0]

    returned_state = q.get("state", [None])[0]
    if returned_state != state:
        raise RuntimeError(f"OIDC state mismatch: expected={_redact(state)} got={_redact(returned_state or '')}")

    # 5) Exchange code for access token
    token = s.post(
        f"{base}/iam/token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        },
        allow_redirects=False,
        timeout=timeout,
        verify=verify_tls,
    )

    if verbose:
        print("Token status:", token.status_code)

    token.raise_for_status()
    j = token.json()

    access_token = j.get("access_token")
    if not access_token:
        raise RuntimeError(f"No access_token in token response keys={list(j.keys())}")

    expires_in = j.get("expires_in", None)
    scope = j.get("scope", None)

    if verbose:
        if expires_in is not None:
            print("expires_in:", _sec_to_readable(expires_in))
        if scope is not None:
            print("scope:", scope)

    return BearerResult(access_token=access_token, expires_in=expires_in, scope=scope)


##############
# Public API #
##############

def bearer_from_user_pass_plain(
    username: str,
    password: str,
    verbose: bool = False,
    *,
    base: str = DEFAULT_BASE,
    client_id: str = DEFAULT_CLIENT_ID,
    redirect_uri: Optional[str] = None,
    timeout: float = 30.0,
    user_agent: str = "metais-bot/1.0",
    verify_tls: bool = True,
    debug_html_path: Optional[str] = None,
) -> str:
    """
    Login using plaintext password and return Bearer token (access_token).
    """
    res = _bearer_from_user_pass_core(
        username=username,
        password=password,
        verbose=verbose,
        base=base,
        client_id=client_id,
        redirect_uri=redirect_uri,
        timeout=timeout,
        user_agent=user_agent,
        verify_tls=verify_tls,
        debug_html_path=debug_html_path,
    )
    return res.access_token

def bearer_from_client_credentials_basic(
    client_id: str,
    client_secret: str,
    scope: Optional[str] = "openid",
    verbose: bool = False,
    *,
    base: str = DEFAULT_BASE,
    timeout: float = 60.0,
    user_agent: str = "metais-bot/1.0",
    verify_tls: bool = True,
) -> str:
    """
    Obtain an access_token using OAuth2 Client Credentials grant.

    auth_method: client_secret_basic (HTTP Basic auth)

    Equivalent curl:

        curl -sS -X POST "$BASE/iam/token" \
          -H "Content-Type: application/x-www-form-urlencoded" \
          -H "User-Agent: metais-bot/1.0" \
          -u "$CLIENT_ID:$CLIENT_SECRET" \
          --data-urlencode "grant_type=client_credentials" \
          --data-urlencode "scope=openid" \
          --max-time 60
    """
    base = _resolve_base(base)

    s = requests.Session()
    s.headers["User-Agent"] = user_agent

    data = {"grant_type": "client_credentials"}
    if scope:  # allow scope=None or "" to omit
        data["scope"] = scope

    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    token = s.post(
        f"{base}/iam/token",
        headers=headers,
        data=data,
        auth=(client_id, client_secret),
        timeout=timeout,
        verify=verify_tls,
        allow_redirects=False,
    )

    if verbose:
        print("Token status:", token.status_code)

    token.raise_for_status()
    j = token.json()

    access_token = j.get("access_token")
    if not access_token:
        raise RuntimeError(f"No access_token in token response keys={list(j.keys())}")

    expires_in = j.get("expires_in", None)
    returned_scope = j.get("scope", None)

    if verbose:
        if expires_in is not None:
            print("expires_in:", _sec_to_readable(expires_in))
        if returned_scope is not None:
            print("scope:", returned_scope)

    return access_token


def bearer_from_client_credentials_post(
    client_id: str,
    client_secret: str,
    scope: Optional[str] = "openid",
    verbose: bool = False,
    *,
    base: str = DEFAULT_BASE,
    timeout: float = 60.0,
    user_agent: str = "metais-bot/1.0",
    verify_tls: bool = True,
) -> str:
    """
    Obtain an access_token using OAuth2 Client Credentials grant.

    auth_method: client_secret_post (client_id/client_secret in form body)

    Equivalent curl:

        curl -sS -X POST "$BASE/iam/token" \
          -H "Content-Type: application/x-www-form-urlencoded" \
          -H "User-Agent: metais-bot/1.0" \
          --data-urlencode "grant_type=client_credentials" \
          --data-urlencode "scope=openid" \
          --data-urlencode "client_id=$CLIENT_ID" \
          --data-urlencode "client_secret=$CLIENT_SECRET" \
          --max-time 60
    """
    base = _resolve_base(base)
    
    s = requests.Session()
    s.headers["User-Agent"] = user_agent

    data = {"grant_type": "client_credentials"}
    if scope:  # allow scope=None or "" to omit
        data["scope"] = scope

    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    data["client_id"] = client_id
    data["client_secret"] = client_secret

    token = s.post(
        f"{base}/iam/token",
        headers=headers,
        data=data,
        timeout=timeout,
        verify=verify_tls,
        allow_redirects=False,
    )

    if verbose:
        print("Token status:", token.status_code)

    token.raise_for_status()
    j = token.json()

    access_token = j.get("access_token")
    if not access_token:
        raise RuntimeError(f"No access_token in token response keys={list(j.keys())}")

    expires_in = j.get("expires_in", None)
    returned_scope = j.get("scope", None)

    if verbose:
        if expires_in is not None:
            print("expires_in:", _sec_to_readable(expires_in))
        if returned_scope is not None:
            print("scope:", returned_scope)

    return access_token


def bearer_from_client_credentials(
    client_id: str,
    client_secret: str,
    scope: Optional[str] = "openid",
    verbose: bool = False,
    *,
    base: str = DEFAULT_BASE,
    timeout: float = 30.0,
    user_agent: str = "metais-bot/1.0",
    verify_tls: bool = True,
    auth_method: str = "client_secret_basic",  # or "client_secret_post"
) -> str:
    """
    Obtain an access_token using OAuth2 Client Credentials grant.

    This is the machine-to-machine flow (no browser, no username/password).

    Args:
        client_id/client_secret: credentials of a confidential client registered in IAM
        scope: optional; .well-known lists "openid". If IAM expects none, pass scope=None.
        auth_method:
            - "client_secret_basic": HTTP Basic auth
            - "client_secret_post": client_id/client_secret in form body

    Returns:
        access_token (Bearer token)
    """
    if auth_method == "client_secret_basic":
        return bearer_from_client_credentials_basic(
            client_id,
            client_secret,
            scope=scope,
            verbose=verbose,
            base=base,
            timeout=timeout,
            user_agent=user_agent,
            verify_tls=verify_tls,
        )

    if auth_method == "client_secret_post":
        return bearer_from_client_credentials_post(
            client_id,
            client_secret,
            scope=scope,
            verbose=verbose,
            base=base,
            timeout=timeout,
            user_agent=user_agent,
            verify_tls=verify_tls,
        )

    raise ValueError(
        f"Unsupported auth_method={auth_method!r}. "
        "Use 'client_secret_basic' or 'client_secret_post'."
    )
