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