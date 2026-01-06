from __future__ import annotations

import json as pyjson
import time
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

import requests

from metais.common.url_utils import strip_query
from metais.common.json_utils import load_json_file
from metais.common.step_marker import is_done, mark_done
from metais.common.http_config import resolve_auth_inputs, get_bearer_token, HTTPConfig
from metais.raw.page_sink import PageSink, PageStats
from metais.raw.pager_policy import load_pager_policy
from metais.raw.adaptive_pager import AdaptivePager

from metais.common.shards import (
    K_SHARD_PAD,
    parse_offset_from_meta_filename,
)

#################
# Small helpers #
#################

def _sleep_backoff_ms(attempt: int, base_ms: int, max_ms: int, jitter_ms: int) -> None:
    # attempt: 1..N
    delay = min(max_ms, base_ms * (2 ** (attempt - 1)))
    delay += random.randint(0, max(0, jitter_ms))
    time.sleep(delay / 1000.0)


def _is_timeout_like_status(code: int) -> bool:
    return code in (408, 429, 502, 503, 504)


def _is_hard_page_error(status: int) -> bool:
    # not timeout-like, not auth, non-2xx
    if status in (401, 403):
        return False
    if _is_timeout_like_status(status):
        return False
    return not (200 <= status < 300)


def _error_path_for(errors_dir: Path, base: str, bad_offset: int) -> Path:
    return errors_dir / f"{base}.{bad_offset:0{K_SHARD_PAD}d}.error.json"


def _parse_results_or_throw(body_text: str, tag: str) -> list[dict]:
    """
    Robust-ish parser for report endpoint responses.
    Accepts:
      - a JSON array
      - a JSON object containing a results array under common keys
    """
    try:
        doc = pyjson.loads(body_text)
    except Exception as e:
        preview = body_text[:400].replace("\n", "\\n")
        raise RuntimeError(f"[{tag}] response is not JSON: {e} preview={preview!r}") from e

    if isinstance(doc, list):
        return [x for x in doc if isinstance(x, dict)]

    if isinstance(doc, dict):
        for key in ("results", "result", "data", "items"):
            v = doc.get(key)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]

        # sometimes nested
        v = doc.get("body")
        if isinstance(v, dict):
            for key in ("results", "result", "data", "items"):
                vv = v.get(key)
                if isinstance(vv, list):
                    return [x for x in vv if isinstance(x, dict)]

        raise RuntimeError(f"[{tag}] cannot find results array in response keys={list(doc.keys())}")

    raise RuntimeError(f"[{tag}] unexpected JSON type: {type(doc).__name__}")


def _load_text(p: Path) -> str:
    s = p.read_text(encoding="utf-8")
    return s


def inject_limit_offset(template: str, limit: int, offset: int) -> str:
    """
    Tries multiple placeholder conventions. If your Groovy templates use something else,
    add it here once and you’re done.
    """
    repls = [
        ("__LIMIT__", str(limit)),
        ("__OFFSET__", str(offset)),
        ("{{LIMIT}}", str(limit)),
        ("{{OFFSET}}", str(offset)),
        ("{{limit}}", str(limit)),
        ("{{offset}}", str(offset)),
        ("${LIMIT}", str(limit)),
        ("${OFFSET}", str(offset)),
        ("${limit}", str(limit)),
        ("${offset}", str(offset)),
        ("<<LIMIT>>", str(limit)),
        ("<<OFFSET>>", str(offset)),
    ]

    out = template
    hit = False
    for k, v in repls:
        if k in out:
            out = out.replace(k, v)
            hit = True

    if hit:
        return out

    # last-resort: try a conservative regex replacement for something like:
    #   def limit = 1000
    #   def offset = 0
    out2 = out
    out2, n1 = re.subn(r"(\bdef\s+limit\s*=\s*)\d+\b", rf"\g<1>{limit}", out2, count=1)
    out2, n2 = re.subn(r"(\bdef\s+offset\s*=\s*)\d+\b", rf"\g<1>{offset}", out2, count=1)
    if n1 or n2:
        return out2

    raise RuntimeError(
        "Could not inject limit/offset into Groovy template. "
        "Add placeholders like __LIMIT__/__OFFSET__ (recommended) "
        "or extend inject_limit_offset() to match your template."
    )


###################################
# Resume logic (scan meta shards) #
###################################

@dataclass
class ResumePoint:
    next_offset: int = 0
    last_limit: int = 0
    found: bool = False


def find_resume_point(pages_dir: Path, base: str) -> ResumePoint:
    best = ResumePoint()

    if not pages_dir.exists():
        return best

    for p in pages_dir.iterdir():
        if not p.is_file():
            continue
        off = parse_offset_from_meta_filename(p.name, base)
        if off is None:
            continue

        meta_path = p
        data_path = pages_dir / f"{base}.{off:0{K_SHARD_PAD}d}.ndjson"

        # meta exists but data missing -> delete meta, ignore
        if not data_path.exists():
            try:
                meta_path.unlink()
            except Exception:
                pass
            continue

        # parse meta json
        try:
            meta = load_json_file(meta_path)
        except Exception:
            # broken meta -> delete both, ignore
            try:
                meta_path.unlink()
            except Exception:
                pass
            try:
                data_path.unlink()
            except Exception:
                pass
            continue

        if not isinstance(meta, dict):
            continue
        if "offset" not in meta or "received" not in meta:
            continue
        if not isinstance(meta.get("offset"), int) or not isinstance(meta.get("received"), int):
            continue

        meta_offset = int(meta["offset"])
        received = int(meta["received"])
        if meta_offset != off:
            # mismatch -> delete both
            try:
                meta_path.unlink()
            except Exception:
                pass
            try:
                data_path.unlink()
            except Exception:
                pass
            continue
        if received < 0:
            continue

        limit = int(meta.get("limit", 0)) if isinstance(meta.get("limit", 0), int) else 0

        # Keep highest offset page
        if (not best.found) or (meta_offset > (best.next_offset - 1)):
            best.found = True
            best.last_limit = limit
            best.next_offset = meta_offset + received

    return best


######################
# POST report runner #
######################

@dataclass
class HttpResponse:
    status: int
    seconds: float
    body: str


def run_report_groovy(
    *,
    api_url: str,
    bearer_token: str,
    groovy_code: str,
    params: dict,
    http_cfg: HTTPConfig,
    verify_tls: bool = True,
) -> HttpResponse:
    payload = {"body": groovy_code, "parameters": params}

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {bearer_token}",
        "User-Agent": http_cfg.auth.user_agent,
    }

    timeout = (http_cfg.timeouts.connect_seconds, http_cfg.timeouts.read_seconds)

    t0 = time.perf_counter()
    r = requests.post(
        api_url,
        json=payload,
        headers=headers,
        timeout=timeout,
        allow_redirects=True,
        verify=verify_tls,
    )
    t1 = time.perf_counter()

    return HttpResponse(status=int(r.status_code), seconds=(t1 - t0), body=r.text)


########################################################
# Hard-error isolation helpers (bisection + uuid-only) #
########################################################

def _try_run(
    *,
    tag: str,
    api_url: str,
    bearer_token: str,
    http_cfg: HTTPConfig,
    params: dict,
    make_groovy: Callable[[int, int], str],
    limit: int,
    offset: int,
    verify_tls: bool,
) -> HttpResponse:
    code = make_groovy(limit, offset)
    return run_report_groovy(
        api_url=api_url,
        bearer_token=bearer_token,
        groovy_code=code,
        params=params,
        http_cfg=http_cfg,
        verify_tls=verify_tls,
    )


def bisect_bad_offset(
    *,
    tag: str,
    api_url: str,
    bearer_token: str,
    http_cfg: HTTPConfig,
    params: dict,
    make_full: Callable[[int, int], str],
    offset: int,
    limit: int,
    verify_tls: bool,
) -> int:
    off = int(offset)
    lim = int(limit)

    guard = 0
    while lim > 1:
        guard += 1
        if guard > 64:
            break

        left = lim // 2
        right = lim - left

        r_left = _try_run(
            tag=tag,
            api_url=api_url,
            bearer_token=bearer_token,
            http_cfg=http_cfg,
            params=params,
            make_groovy=make_full,
            limit=left,
            offset=off,
            verify_tls=verify_tls,
        )
        if _is_hard_page_error(r_left.status):
            lim = left
            continue

        off = off + left
        lim = right

    return off


def fetch_uuid_at(
    *,
    tag: str,
    api_url: str,
    bearer_token: str,
    http_cfg: HTTPConfig,
    params: dict,
    make_uuid: Callable[[int, int], str],
    offset: int,
    verify_tls: bool,
) -> Optional[str]:
    r = _try_run(
        tag=tag,
        api_url=api_url,
        bearer_token=bearer_token,
        http_cfg=http_cfg,
        params=params,
        make_groovy=make_uuid,
        limit=1,
        offset=offset,
        verify_tls=verify_tls,
    )
    if not (200 <= r.status < 300):
        return None

    try:
        arr = _parse_results_or_throw(r.body, tag)
        if not arr:
            return None
        obj = arr[0]
        if isinstance(obj, dict):
            if isinstance(obj.get("uuid"), str) and obj["uuid"]:
                return obj["uuid"]
            # fallback: first string that looks uuid-ish
            for v in obj.values():
                if isinstance(v, str) and 32 <= len(v) <= 40:
                    return v
        return None
    except Exception:
        return None


#####################
# Main paged runner #
#####################

def run_paged(
    *,
    tag: str,
    layout,
    uri_cfg,
    http_cfg: HTTPConfig,
    sink: PageSink,
    params: dict,
    make_groovy: Callable[[int, int], str],
    make_groovy_safe: Callable[[int, int], str],
    verbose: bool = True,
    verify_tls: bool = True,
) -> None:
    # done marker is on raw_nodes_dir / raw_rels_dir
    if tag == "NODES":
        done_dir = Path(layout.raw_nodes_dir)
        pages_dir = Path(getattr(layout, "raw_nodes_pages_dir", done_dir / "pages"))
        errs_dir = Path(getattr(layout, "raw_nodes_errors_dir", done_dir / "pages" / "errors"))
        base = "nodes"
    elif tag == "RELS":
        done_dir = Path(layout.raw_rels_dir)
        pages_dir = Path(getattr(layout, "raw_rels_pages_dir", done_dir / "pages"))
        errs_dir = Path(getattr(layout, "raw_rels_errors_dir", done_dir / "pages" / "errors"))
        base = "rels"
    else:
        raise ValueError(f"Unknown tag={tag!r}")

    if is_done(done_dir):
        if verbose:
            print(f"[{tag}] .done marker present in {done_dir} - skipping.")
        return

    errs_dir.mkdir(parents=True, exist_ok=True)
    pages_dir.mkdir(parents=True, exist_ok=True)

    api_url = uri_cfg.report_run_url()

    # Must have bearer
    bearer = (http_cfg.auth.bearer_token or "").strip()
    if http_cfg.auth.mode != "none" and not bearer:
        # try re-auth using configured mode
        resolved = resolve_auth_inputs(http_cfg.auth, verbose=verbose)
        bearer = get_bearer_token(resolved, http_cfg, base=uri_cfg.base_url, verbose=verbose, verify_tls=verify_tls)

    # resume
    rp = find_resume_point(pages_dir, base)
    offset = rp.next_offset if rp.found else 0
    initial_limit = rp.last_limit if (rp.found and rp.last_limit > 0) else http_cfg.paging.page_size

    if rp.found and verbose:
        print(f"[{tag}] resume: next_offset={offset} initial_limit={initial_limit}")

    policy_path = Path(layout.project_root) / "config" / "paging_policy.json"
    pol = load_pager_policy(policy_path)

    # initial_limit precedence:
    # 1) resume last_limit
    # 2) http_cfg.paging.page_size (if set)
    # 3) pol.initial_limit
    initial_limit = (
        rp.last_limit if (rp.found and rp.last_limit > 0)
        else (http_cfg.paging.page_size if getattr(http_cfg, "paging", None) else pol.initial_limit)
    )

    pager = AdaptivePager(initial_limit, pol)

    pages_guard = 0
    while True:
        pages_guard += 1
        if pages_guard > http_cfg.paging.max_pages:
            raise RuntimeError(f"[{tag}] exceeded paging.max_pages={http_cfg.paging.max_pages}")

        limit = pager.limit()

        # request
        try:
            r = run_report_groovy(
                api_url=api_url,
                bearer_token=bearer,
                groovy_code=make_groovy(limit, offset),
                params=params,
                http_cfg=http_cfg,
                verify_tls=verify_tls,
            )
        except (requests.Timeout, requests.ConnectionError) as e:
            if verbose:
                print(f"[{tag}] transport timeout at offset={offset} limit={limit}: {e} -> shrinking and retrying")
            pager.on_timeout_like()
            continue

        # auth failure: try refresh once or twice
        if r.status in (401, 403):
            if verbose:
                print(f"[{tag}] auth HTTP {r.status} at offset={offset} -> refreshing bearer and retrying")
            for _ in range(2):
                resolved = resolve_auth_inputs(http_cfg.auth, verbose=verbose)
                bearer = get_bearer_token(resolved, http_cfg, base=uri_cfg.base_url, verbose=verbose, verify_tls=verify_tls)
                r = run_report_groovy(
                    api_url=api_url,
                    bearer_token=bearer,
                    groovy_code=make_groovy(limit, offset),
                    params=params,
                    http_cfg=http_cfg,
                    verify_tls=verify_tls,
                )
                if r.status not in (401, 403):
                    break
            if r.status in (401, 403):
                raise RuntimeError(f"[{tag}] auth failed after refresh attempts: HTTP {r.status}")

        # retryable HTTP
        if not (200 <= r.status < 300):
            if _is_timeout_like_status(r.status):
                if verbose:
                    print(f"[{tag}] HTTP {r.status} at offset={offset} limit={limit} -> shrinking and retrying")
                pager.on_timeout_like()
                continue

            # hard error: isolate + log + skip 1
            if verbose:
                print(f"[{tag}] HARD HTTP {r.status} at offset={offset} limit={limit} -> isolating via bisection")

            bad_offset = offset if limit == 1 else bisect_bad_offset(
                tag=tag,
                api_url=api_url,
                bearer_token=bearer,
                http_cfg=http_cfg,
                params=params,
                make_full=make_groovy,
                offset=offset,
                limit=limit,
                verify_tls=verify_tls,
            )

            # capture failing single-record response (full template, limit=1)
            r_single = _try_run(
                tag=tag,
                api_url=api_url,
                bearer_token=bearer,
                http_cfg=http_cfg,
                params=params,
                make_groovy=make_groovy,
                limit=1,
                offset=bad_offset,
                verify_tls=verify_tls,
            )

            bad_uuid = fetch_uuid_at(
                tag=tag,
                api_url=api_url,
                bearer_token=bearer,
                http_cfg=http_cfg,
                params=params,
                make_uuid=make_groovy_safe,
                offset=bad_offset,
                verify_tls=verify_tls,
            )

            report = {
                "tag": tag,
                "bad_offset": bad_offset,
                "page_offset": offset,
                "page_limit": limit,
                "page_http_status": r.status,
                "page_seconds": r.seconds,
                "page_error_body": r.body,
                "single_http_status": r_single.status,
                "single_seconds": r_single.seconds,
                "single_error_body": r_single.body,
                "uuid": bad_uuid,
            }

            ep = _error_path_for(errs_dir, base, bad_offset)
            ep.parent.mkdir(parents=True, exist_ok=True)
            ep.write_text(pyjson.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

            if verbose:
                u = bad_uuid or "(unavailable)"
                print(f"[{tag}] Logged bad record offset={bad_offset} uuid={u} -> skipping 1 and continuing")

            offset = bad_offset + 1
            pager.on_timeout_like()
            continue

        # OK: parse results array
        arr = _parse_results_or_throw(r.body, tag)
        n = len(arr)

        sink.begin_page(offset, limit)
        for obj in arr:
            if isinstance(obj, dict):
                sink.write_item(obj)
        sink.end_page(PageStats(offset=offset, limit=limit, received=n, seconds=r.seconds))

        if verbose:
            print(f"[{tag}] offset={offset} limit={limit} got={n} in {r.seconds:.3f}s")

        pager.on_success(r.seconds)

        if n == 0:
            break

        offset += int(n)

    mark_done(done_dir)


##############
# Public API #
##############

def fetch_raw_nodes(layout, uri_cfg, http_cfg: HTTPConfig, sink: PageSink, *, verbose: bool = True) -> None:
    tpl_path = Path(layout.project_root) / "groovy" / "template" / "node.groovy"
    tpl_safe = Path(layout.project_root) / "groovy" / "template" / "node_safe.groovy"
    _fetch_raw_common("NODES", tpl_path, tpl_safe, layout, uri_cfg, http_cfg, sink, verbose=verbose)


def fetch_raw_rels(layout, uri_cfg, http_cfg: HTTPConfig, sink: PageSink, *, verbose: bool = True) -> None:
    tpl_path = Path(layout.project_root) / "groovy" / "template" / "relation.groovy"
    tpl_safe = Path(layout.project_root) / "groovy" / "template" / "relation_safe.groovy"
    _fetch_raw_common("RELS", tpl_path, tpl_safe, layout, uri_cfg, http_cfg, sink, verbose=verbose)


def _fetch_raw_common(
    tag: str,
    tpl_path: Path,
    tpl_path_safe: Path,
    layout,
    uri_cfg,
    http_cfg: HTTPConfig,
    sink: PageSink,
    *,
    verbose: bool,
) -> None:
    if verbose:
        print(f"[{tag}] template path: {strip_query(tpl_path)}")
        print(f"[{tag}] safe template path: {strip_query(tpl_path_safe)}")

    tpl = _load_text(tpl_path)
    tpl_safe = _load_text(tpl_path_safe)
    if not tpl.strip():
        raise RuntimeError(f"Groovy template is empty: {tpl_path}")
    if not tpl_safe.strip():
        raise RuntimeError(f"Safe Groovy template is empty: {tpl_path_safe}")

    params_path = Path(layout.project_root) / "config" / "params.json"
    if not params_path.exists():
        raise RuntimeError(f"params.json not found: {params_path}")

    params = load_json_file(params_path)
    if not isinstance(params, dict):
        raise RuntimeError(f"params.json must be an object: {params_path}")

    run_paged(
        tag=tag,
        layout=layout,
        uri_cfg=uri_cfg,
        http_cfg=http_cfg,
        sink=sink,
        params=params,
        make_groovy=lambda limit, offset: inject_limit_offset(tpl, limit, offset),
        make_groovy_safe=lambda limit, offset: inject_limit_offset(tpl_safe, limit, offset),
        verbose=verbose,
    )