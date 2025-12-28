#!/usr/bin/env python3
"""
MetaIS interactive OIDC login → access_token (Bearer token)

This script automates the same flow your browser does (in broad strokes):
  - Start an OIDC Authorization Code + PKCE flow at /iam/authorize
  - Follow redirects to the login form
  - Submit username/password to /iam/usernamePassLogin (with CSRF)
  - Follow redirects until we reach redirect_uri?code=...
  - Exchange code + PKCE verifier at /iam/token to obtain access_token
  - iam stands for Identity & Access Management

Security notes:
  - Password is read from a prompt (not echoed).
  - Do NOT commit tokens, passwords, or logs containing them to git.
"""

import base64
import getpass
import hashlib
import secrets
import os
import re
import sys
from urllib.parse import parse_qs, urlparse

import requests


BASE = "https://metais.slovensko.sk"

# real client ID used by metais
DEFAULT_CLIENT_ID = "webPortalClient"

# Must be exactly an allowed redirect URI for that client.
DEFAULT_REDIRECT_URI = f"{BASE}/auth-success"


def b64url(raw: bytes) -> str:
    """Base64url without '=' padding, as required by PKCE."""
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def make_pkce():
    """
    PKCE (Proof Key for Code Exchange):
      - code_verifier: a random secret the client keeps locally
      - code_challenge: SHA256(code_verifier) base64url-encoded, sent to /authorize

    Later, /token checks that the verifier matches the earlier challenge.
    This prevents an attacker who steals the authorization code from redeeming it.
    """
    # i)  os.urandom(32) produces 32 bytes (256 bits) of cryptographically secure random data
    #     - raw bytes b'\x9f\x03\xa8\x1c\xef\x91...'
    #     - 32 because we need "sufficient entropy"
    # ii) b64url
    #     - standard base64: A-Z, a-z, 0-9, + /
    #     - safe replaces (+ -> -, / -> _)
    #     - removes = (padding)
    #     - get a standard "random" string like B4pZ0Xn5tS9wWgZp0eDq1R7yQFJ8Z3r3Q6oCz6lT3bA
    verifier = b64url(os.urandom(32))
    # PKCE S256 method:
    # i)   take the verifier and encode into UTF-8
    # ii)  compute SHA-256 hash
    # iii) get 32 bytes of output
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    # now we have:
    # code_verifier = random secret string of 256 bit entropy
    # code_challenge = deterministic, public hash of that secret
    return verifier, challenge

def rand_token(nbytes: int = 32) -> str:
    # URL-safe, already base64-like
    return secrets.token_urlsafe(nbytes)

def extract_csrf(html: str) -> str:
    """
    The login POST expects a CSRF token.
    Different login pages embed it differently; we support:
      - hidden input: <input type="hidden" name="_csrf" value="...">
      - meta tag:     <meta name="_csrf" content="...">
    """
    m = re.search(r'name=["\']_csrf["\'][^>]*value=["\']([^"\']+)["\']', html)
    if m:
        return m.group(1)

    m = re.search(r'<meta[^>]*name=["\']_csrf["\'][^>]*content=["\']([^"\']+)["\']', html)
    if m:
        return m.group(1)

    raise RuntimeError("Could not find CSRF token in HTML.")


def prompt_if_missing(env_key: str, label: str, secret: bool = False) -> str:
    """
    Read value from env var if present; otherwise prompt interactively.
    secret=True uses getpass (no echo).
    """
    v = os.environ.get(env_key)
    if v:
        return v

    if secret:
        return getpass.getpass(f"{label}: ")
    return input(f"{label}: ").strip()


def main() -> int:
    # --- Interactive credentials (env first, prompt second) ---
    username = prompt_if_missing("METAIS_USER", "MetaIS username / email")
    password = prompt_if_missing("METAIS_PASS", "MetaIS password", secret=True)

    client_id = os.environ.get("METAIS_CLIENT_ID", DEFAULT_CLIENT_ID)
    redirect_uri = os.environ.get("METAIS_REDIRECT_URI", DEFAULT_REDIRECT_URI)

    # constructs a Session object: a persistent client that remembers state across requests
    # the object s here keeps the continuity of requests.
    # s is an instance of requests.sessions.Session
    # s stores the cookie jar - to make sure the session cookie stays consistent across redirects
    s = requests.Session()
    # we tell the server who we are. by default it may be something like "curl/8.4.0" or "python-requests/2.31.0"
    # we want to one day have the option to dig into meta's logs and see our own activity. sending the default agent is less useful.
    s.headers["User-Agent"] = "metais-bot/1.0"

    # 0) Create PKCE FIRST (so code_challenge exists)
    # we're not sending anything yet.
    # we're generating a cryptographic binding between
    # /authorize request we are making in 1)
    # /token request we do later
    # we are telling the server that the same client that started the login flow is the one redeeming the auth code (bearer)
    # PKCE requires two values:
    # i) code verifier - high entropy, unpredictable secret generated by the client sent to /token
    # ii) code_challenge derived deterministically from code_verifier, sent publicly to /authorize, stored by the server next to the auth_code
    code_verifier, code_challenge = make_pkce()

    # 1) Start OIDC: /iam/authorize
    #    We expect the server to redirect us into the login flow if we're not logged in.
    state = rand_token(32)
    nonce = rand_token(32)
    auth_params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": "openid",
        "code_challenge": code_challenge, # here we post that code_challenge (hashed random string) we computed in step 0)
        "code_challenge_method": "S256",  # method how the code_verifier was hashed (deterministically) so the server can later compare the provided code_verifier when we request /token
        "state": state,                   # client decides what state it accepts on the final redirect. the server response chain ends with redirect_url?...&state=(something). We check that (something) against what we send here.
        "nonce": nonce,                   # used for ID tokens in many systems (harmless to include)
    }

    # this is an instance of class requests.models.Response
    # represents one http response
    # s remembers cookies, reuses connections, has default headers
    # s.get(...) performs a http request GET with a browser-like session
    # s.get() builds a full URL https://metais.slovensko.sk/iam/authorize?response_type=code&...
    # sends URL, query parameters, headers, cookies
    # receives HTTP status code, headers, body
    # response is conveniently wrapped in a Response object
    r = s.get(f"{BASE}/iam/authorize", params=auth_params, allow_redirects=True, timeout=30)

    #iam/prelogin
    print("After authorize final URL:", r.url)
    if r.history:
        print("Redirect chain:")
        for h in r.history:
            print(" ", h.status_code, h.url, "->", h.headers.get("Location"))
    print("Cookies:", s.cookies.get_dict())
    # note: the iam/authorize redirect chain to iam/prelogin serves to
    # create the authorization transaction context that the server will later resume and finish.
    # servers stores the pending OIDC request (client_id, redirect_uri, state, PKCE challenge...)
    # if we want the access token in the end, we must start with authorize, because only authorize
    # produces the code which can be exchanged at /token

    # 2) Make sure we are on the username/password page
    #    The authorize flow may land us on /iam/prelogin (choose login method),
    #    so we jump to /iam/usernamePassLogin explicitly.
    if "/iam/usernamePassLogin" not in r.url: # skip the menu, take me to the password form
        r = s.get(f"{BASE}/iam/usernamePassLogin", allow_redirects=True, timeout=30)

    # Save for debugging (optional).
    open("/tmp/metais_login_userpass.html", "w", encoding="utf-8").write(r.text)

    # cross-site request forgery protection
    # protects state-changing http requests (login, logout, password change...) against attack
    # malicious website (MW) tricking my browser into submitting a form to metais using my cookies
    # server sends a page with a hidden section <input type="hidden" name="_csrf" value="OC-IwYq3..." />
    # MW can do POST requests to metais, but it can't read its javascript
    csrf = extract_csrf(r.text)
    print("CSRF:", csrf[:8] + "...")

    # 3) POST username/password to /iam/usernamePassLogin
    login = s.post(
        f"{BASE}/iam/usernamePassLogin",
        data={
            "_csrf": csrf,          # cross-site forgery protection
            "username": username,
            "password": password,
        },
        allow_redirects=False,
        timeout=30,
    )

    print("Login status:", login.status_code)
    print("Login Location:", login.headers.get("Location"))
    print("Cookies after login:", s.cookies.get_dict())

    if login.status_code not in (302, 303):
        raise RuntimeError(
            f"Login did not redirect as expected: {login.status_code}\n{login.text[:400]}"
        )

    # 4) Follow redirects until redirect_uri contains ?code=
    next_url = login.headers["Location"]
    # servers can return absolute url (https://metais.slovensko.sk/iam/authorize?...)
    # or relative url (/iam/authorize?...)
    if next_url.startswith("/"): # if it's relative we fix it real quick
        next_url = BASE + next_url

    final = None
    while True:
        step = s.get(next_url, allow_redirects=False, timeout=30)
        loc = step.headers.get("Location")

        # Some systems might return the final page with URL already containing code
        if "code=" in step.url:
            final = step.url
            break

        if not loc:
            raise RuntimeError(
                f"Stopped without redirect. URL={step.url} status={step.status_code} body={step.text[:200]}"
            )

        if "code=" in loc:
            final = loc
            break

        if loc.startswith("/"):
            loc = BASE + loc

        next_url = loc

    print("Final redirect containing code:", final)

    # URL now looks like: https://metais.slovensko.sk/auth-success?code=TRqVe8v1zr...&state=yCdmDXHoF0oVw6...
    q = parse_qs(urlparse(final).query)
    if "code" not in q:
        raise RuntimeError(f"No code param found in: {final}")

    code = q["code"][0]
    
    # we check the state matches right here (nobody inserted anything malicious into the chain)
    returned_state = q.get("state", [None])[0]
    if returned_state != state:
        raise RuntimeError(f"OIDC state mismatch: expected={state!r} got={returned_state!r}")
        
    # 5) Exchange code for tokens at /iam/token
    token = s.post(
        f"{BASE}/iam/token",                # we POST a request to iam/token where we redeem the code for bearer token
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "authorization_code",
            "code": code,                   # we won't get the bearer token without this redeemer code
            "client_id": client_id,         # same client ID we've been using (WebPortalClient)
            "redirect_uri": redirect_uri,   # when the server issued the authorization code, it bound it to cluent_id, code_challenge AND redirect_uri
            "code_verifier": code_verifier, # we sent code_challenge to /authorize earlier. Now we send code_verifier which the server hashes and compares to the stored challenge
        },
        allow_redirects=False,
        timeout=30,
    )                                       # here token is directly the respons of our POST request

    print("Token status:", token.status_code)
    token.raise_for_status()

    j = token.json()

    # This is the thing you use as:
    #   Authorization: Bearer <access_token>
    access_token = j.get("access_token", "")
    if not access_token:
        raise RuntimeError(f"No access_token in token response keys={list(j.keys())}")

    print("\nACCESS TOKEN (Bearer):")
    print(access_token)

    # Optional: print safe metadata
    # print("expires_in:", j.get("expires_in"))
    # print("scope:", j.get("scope"))

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        raise SystemExit(130)