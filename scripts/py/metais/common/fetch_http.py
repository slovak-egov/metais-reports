from __future__ import annotations

import json
import random
import time
from dataclasses import replace
from typing import Any, Dict, Optional

import requests

from .http_config import HTTPConfig


def _sleep_ms(ms: int) -> None:
    time.sleep(ms / 1000.0)


def _compute_delay_ms(attempt: int, base: int, cap: int, jitter: int) -> int:
    # exponential backoff, capped
    d = min(cap, base * (2 ** max(0, attempt - 1)))
    if jitter > 0:
        d += random.randint(0, jitter)
    return d


def get_json(url: str, http_cfg: HTTPConfig, *, verify_tls: bool = True) -> Any:
    """
    GET JSON with retries based on HTTPConfig.

    Auth behavior:
      - if http_cfg.auth.mode == "none": no Authorization header
      - else if bearer_token exists: send "Authorization: Bearer ..."
      - else: no Authorization header (open endpoints should work)
    """
    headers: Dict[str, str] = {
        "User-Agent": http_cfg.auth.user_agent,
        "Accept": "application/json",
    }

    if http_cfg.auth.mode != "none" and http_cfg.auth.bearer_token:
        headers["Authorization"] = f"Bearer {http_cfg.auth.bearer_token}"

    timeout = (float(http_cfg.timeouts.connect_seconds), float(http_cfg.timeouts.read_seconds))

    max_attempts = int(http_cfg.retries.max_attempts)
    retry_http = set(int(x) for x in http_cfg.retries.retry_http)

    last_err: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        try:
            r = requests.get(url, headers=headers, timeout=timeout, verify=verify_tls)
            if r.status_code in retry_http and attempt < max_attempts:
                delay = _compute_delay_ms(
                    attempt,
                    http_cfg.retries.base_delay_ms,
                    http_cfg.retries.max_delay_ms,
                    http_cfg.retries.jitter_ms,
                )
                _sleep_ms(delay)
                continue

            r.raise_for_status()
            return r.json()

        except (requests.RequestException, ValueError) as e:
            last_err = e
            if attempt >= max_attempts:
                break
            delay = _compute_delay_ms(
                attempt,
                http_cfg.retries.base_delay_ms,
                http_cfg.retries.max_delay_ms,
                http_cfg.retries.jitter_ms,
            )
            _sleep_ms(delay)

    raise RuntimeError(f"GET_json failed for {url}: {last_err}")

def get_json_simple(
    url: str,
    *,
    bearer_token: str | None = None,
    user_agent: str = "metais-get-json-simple",
    timeout: tuple[float, float] = (30.0, 60.0),  # (connect, read)
    verify_tls: bool = True,
    # ---- new retry knobs (safe defaults) ----
    max_attempts: int = 8,
    retry_http: tuple[int, ...] = (408, 429, 500, 502, 503, 504),
    base_delay_ms: int = 400,
    max_delay_ms: int = 15_000,
    jitter_ms: int = 400,
) -> Any:
    """
    Simple GET->JSON helper with retry/backoff.

    Retries:
      - on transient HTTP status codes in retry_http (default: 408/429/5xx)
      - on requests exceptions (proxy/tunnel errors, timeouts, connection resets)
    """
    headers: Dict[str, str] = {
        "User-Agent": user_agent,
        "Accept": "application/json",
    }
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"

    last_err: Optional[Exception] = None
    last_status: Optional[int] = None
    last_snippet: str = ""

    for attempt in range(1, max_attempts + 1):
        r = None
        try:
            r = requests.get(url, headers=headers, timeout=timeout, verify=verify_tls)
            last_status = r.status_code
            # Keep a tiny snippet for debugging (HTML error pages, etc.)
            last_snippet = (r.text or "")[:200]

            # Do NOT retry most 4xx (except ones explicitly listed, like 429/408)
            if 400 <= r.status_code < 500 and r.status_code not in retry_http:
                r.raise_for_status()

            # Retry configured transient HTTP statuses
            if r.status_code in retry_http and attempt < max_attempts:
                # Honor Retry-After when present (esp. 429), but keep backoff too
                delay = _compute_delay_ms(attempt, base_delay_ms, max_delay_ms, jitter_ms)
                ra = r.headers.get("Retry-After")
                if ra and ra.isdigit():
                    delay = max(delay, int(ra) * 1000)
                _sleep_ms(delay)
                continue

            r.raise_for_status()
            return r.json()

        except (requests.RequestException, ValueError) as e:
            last_err = e

            # If it's an HTTPError with a non-retryable status, stop immediately
            if isinstance(e, requests.HTTPError) and getattr(e, "response", None) is not None:
                st = e.response.status_code  # type: ignore[union-attr]
                if (400 <= st < 500) and (st not in retry_http):
                    break

            if attempt >= max_attempts:
                break

            delay = _compute_delay_ms(attempt, base_delay_ms, max_delay_ms, jitter_ms)
            _sleep_ms(delay)

    msg = f"get_json_simple failed for {url}"
    if last_status is not None:
        msg += f" (last status {last_status})"
    if last_err is not None:
        msg += f": {last_err}"
    if last_snippet:
        msg += f"\nFirst 200 chars of response:\n{last_snippet}"
    raise RuntimeError(msg) from last_err
