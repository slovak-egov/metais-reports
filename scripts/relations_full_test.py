#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, List, Tuple, Optional

from config_env import load_env_file, find_project_root

# ---------------------------------------------------------------------
# Setup & config
# ---------------------------------------------------------------------

load_env_file()

SCRIPT_DIR   = Path(__file__).resolve().parent
PROJECT_ROOT = find_project_root(SCRIPT_DIR)

AGNOSTIC_SH = SCRIPT_DIR / "agnostic.sh"

# Where to write the summary of all bad relations
OUT_DIR = PROJECT_ROOT / "test" / "debug_broken_global"
OUT_DIR.mkdir(parents=True, exist_ok=True)
SUMMARY_PATH = OUT_DIR / "broken_relations_global.json"

# Groovy templates (relative to GROOVY_DIR or absolute)
GROOVY_DIR = os.getenv("GROOVY_DIR")
if GROOVY_DIR is None:
    # fall back to repo-local default
    GROOVY_DIR = str(PROJECT_ROOT / "groovy" / "template")

TEMPLATE_ALL = Path(GROOVY_DIR) / "relation_template_agnostic_all.groovy"
TEMPLATE_UUID = Path(GROOVY_DIR) / "relation_template_agnostic_uuid.groovy"

PAGE_SIZE = int(os.getenv("REL_PAGE_SIZE", "4096"))
SUBPROC_TIMEOUT = float(os.getenv("METAIS_SUBPROC_TIMEOUT", "180"))
PAGE_MAX_RETRIES = int(os.getenv("PAGE_MAX_RETRIES", "10"))
PAGE_RETRY_DELAY = float(os.getenv("PAGE_RETRY_DELAY", "0.5"))

# ---------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------

@dataclass
class BrokenRelationRecord:
    index: int               # global 0-based relation index by createdAt
    source: Optional[str]    # UUID or None if we couldn't fetch
    target: Optional[str]
    state: Optional[str]
    error: str               # full stderr/error text


# ---------------------------------------------------------------------
# Low-level runner: agnostic.sh --kind rel --stdout
# ---------------------------------------------------------------------

def _parse_items_from_json(payload: Any) -> List[dict]:
    """Support {"result":[...]}, {"results":[...]}, or bare list."""
    if isinstance(payload, dict):
        if "result" in payload and isinstance(payload["result"], list):
            return payload["result"]
        if "results" in payload and isinstance(payload["results"], list):
            return payload["results"]
        # Other dict shapes are unexpected here
        raise ValueError(f"Unexpected JSON dict shape: keys={list(payload.keys())}")
    if isinstance(payload, list):
        return payload
    if payload is None:
        return []
    raise ValueError(f"Unexpected top-level JSON type: {type(payload)}")

def _is_auth_error(stderr_text: str) -> bool:
    """
    Heuristic: does stderr look like a TOKEN / auth problem?

    We look for:
      - 'TOKEN env var is required'
      - 'HTTP ERROR: 401'
      - 'HTTP ERROR: 403'
    """
    s = (stderr_text or "").lower()
    if "token env var is required" in s:
        return True
    if "http error:" in s and (" 401" in s or "401 " in s or " 403" in s or "403 " in s):
        return True
    # Core.sh also prints a hint line for 401/403
    if "your token may be missing" in s:
        return True
    return False


def _prompt_for_new_token() -> bool:
    """
    Ask the user for a new TOKEN (only if stdin is a TTY).
    Returns True if TOKEN was updated, False if user aborted or not interactive.
    """
    if not sys.stdin.isatty():
        return False

    print(
        "\n[AUTH] MetaIS call reported an authorization problem "
        "(TOKEN missing/expired?).\n",
        file=sys.stderr,
    )
    print(
        "If you want, paste a new TOKEN now (input will be visible in this shell).\n"
        "Press Enter on an empty line to abort.\n",
        file=sys.stderr,
    )

    try:
        new_token = input("New TOKEN: ").strip()
    except EOFError:
        new_token = ""

    if new_token:
        os.environ["TOKEN"] = new_token
        print("[AUTH] TOKEN updated for this process, retrying the same request…", file=sys.stderr)
        return True
    else:
        print("[AUTH] No TOKEN provided, aborting current request.", file=sys.stderr)
        return False

def run_rel_page(
    limit: int,
    offset: int,
    template: Path,
) -> Tuple[bool, Optional[List[dict]], str]:
    """
    Call agnostic.sh for relations with given limit/offset and GROOVY template.

    This version:
      - Retries transient failures up to PAGE_MAX_RETRIES.
      - Detects auth/TOKEN problems and lets you paste a new TOKEN interactively.
      - Only returns (ok=False, err) if the error persists *after* retries
        and any requested TOKEN refreshes.

    Returns:
        (ok, items_or_None, error_text_if_any)

    - ok=True → items is a list (possibly empty).
    - ok=False → items is None, error_text has stderr from agnostic.sh/core.sh.
    """
    env = os.environ.copy()
    attempt = 1
    last_err_text = ""

    while True:
        cmd = [
            "bash",
            str(AGNOSTIC_SH),
            "--kind", "rel",
            "--template", str(template),
            "--limit", str(limit),
            "--offset", str(offset),
            "--stdout",
            "--no-csv",
        ]

        try:
            proc = subprocess.run(
                cmd,
                text=True,
                capture_output=True,
                timeout=SUBPROC_TIMEOUT,
                env=env,
            )
        except subprocess.TimeoutExpired as e:
            last_err_text = f"[TIMEOUT] agnostic.sh limit={limit} offset={offset}: {e}"
            # Treat timeout as transient; respect PAGE_MAX_RETRIES
            if PAGE_MAX_RETRIES > 0 and attempt >= PAGE_MAX_RETRIES:
                return False, None, last_err_text
            print(
                f"[WARN] Timeout (attempt {attempt}/{PAGE_MAX_RETRIES}) for "
                f"limit={limit}, offset={offset}; retrying in {PAGE_RETRY_DELAY}s…",
                file=sys.stderr,
            )
            attempt += 1
            time.sleep(PAGE_RETRY_DELAY)
            continue

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        last_err_text = (
            f"[PROC] agnostic.sh failed (rc={proc.returncode}, "
            f"limit={limit}, offset={offset})\n{stderr}"
        )

        # Success path
        if proc.returncode == 0:
            if not stdout.strip():
                # Treat empty stdout as empty page
                return True, [], ""

            try:
                payload = json.loads(stdout)
            except Exception as e:
                err = (
                    f"[PARSE] Failed to parse JSON from agnostic.sh "
                    f"(limit={limit}, offset={offset}): {e}\n"
                    f"stdout:\n{stdout}\n\nstderr:\n{stderr}"
                )
                return False, None, err

            try:
                items = _parse_items_from_json(payload)
            except Exception as e:
                err = (
                    f"[PARSE] Unexpected JSON shape for relations page "
                    f"(limit={limit}, offset={offset}): {e}\n"
                    f"payload:\n{json.dumps(payload, ensure_ascii=False)[:400]}"
                )
                return False, None, err

            return True, items, ""

        # Non-zero return code:
        # 1) Is it clearly an auth/TOKEN problem?
        if _is_auth_error(stderr):
            if _prompt_for_new_token():
                # User updated TOKEN; try again *without* counting it as a transient attempt
                env["TOKEN"] = os.environ["TOKEN"]
                continue
            else:
                # User refused / not interactive → hard failure
                return False, None, last_err_text

        # 2) Not an auth problem → treat as transient up to PAGE_MAX_RETRIES
        if PAGE_MAX_RETRIES > 0 and attempt >= PAGE_MAX_RETRIES:
            # Now we consider this a "real" failure (e.g. broken relation)
            return False, None, last_err_text

        print(
            f"[WARN] agnostic.sh failed (attempt {attempt}/{PAGE_MAX_RETRIES}) "
            f"for limit={limit}, offset={offset}; retrying in {PAGE_RETRY_DELAY}s…\n"
            f"(stderr first 400 chars)\n{stderr[:400]}",
            file=sys.stderr,
        )
        attempt += 1
        time.sleep(PAGE_RETRY_DELAY)


# ---------------------------------------------------------------------
# Binary search to isolate a single failing index
# ---------------------------------------------------------------------

def find_failing_index_in_window(
    lo: int,
    hi: int,
) -> Tuple[int, str]:
    """
    Given a window [lo, hi) where we *know* calling TEMPLATE_ALL fails,
    binary-search to find a single index i in [lo, hi) s.t. limit=1, offset=i fails.

    Returns:
        (bad_index, error_text_from_single)
    """
    assert hi > lo, "Empty window given to find_failing_index_in_window"

    last_err = ""

    # Shrink [lo, hi) to length 1 using left-half checks
    while hi - lo > 1:
        mid = (lo + hi) // 2
        length_left = mid - lo
        if length_left <= 0:
            # Degenerate; safety break
            break

        ok_left, _, err_left = run_rel_page(
            limit=length_left,
            offset=lo,
            template=TEMPLATE_ALL,
        )

        if not ok_left:
            # Failure is inside [lo, mid)
            hi = mid
            last_err = err_left
        else:
            # Left is clean -> failure is in [mid, hi)
            lo = mid

    # Now we *expect* [lo, lo+1) to fail when queried as limit=1, offset=lo.
    bad_index = lo
    ok_single, _, err_single = run_rel_page(
        limit=1,
        offset=bad_index,
        template=TEMPLATE_ALL,
    )

    if ok_single:
        # Inconsistent (should not happen if assumptions hold)
        raise RuntimeError(
            f"Inconsistent: single index {bad_index} did not fail, "
            f"but window was failing. Last window error:\n{last_err}"
        )

    return bad_index, (err_single or last_err)


# ---------------------------------------------------------------------
# UUID-only inspection for a single index
# ---------------------------------------------------------------------

def inspect_bad_index_with_uuid_template(bad_index: int) -> Tuple[Optional[str], Optional[str], Optional[str], str]:
    """
    Call TEMPLATE_UUID at limit=1, offset=bad_index and return (source, target, state, err).

    If the uuid-template also fails or returns weird data, UUIDs may be None,
    and err will describe what happened.
    """
    ok, items, err = run_rel_page(
        limit=1,
        offset=bad_index,
        template=TEMPLATE_UUID,
    )

    if not ok:
        return None, None, None, f"[UUID] uuid-template failed: {err}"

    if not items:
        return None, None, None, "[UUID] uuid-template returned empty result list"

    row = items[0]
    src = row.get("source")
    tgt = row.get("target")
    state = row.get("state")  # may be None if template doesn't set it

    missing = []
    if src is None:
        missing.append("source")
    if tgt is None:
        missing.append("target")

    if missing:
        extra = f" (missing fields: {', '.join(missing)})"
    else:
        extra = ""

    return src, tgt, state, err + extra if err else extra


# ---------------------------------------------------------------------
# Top-level scan: walk all relations until empty page
# ---------------------------------------------------------------------

def scan_all_relations() -> List[BrokenRelationRecord]:
    """
    Linear scan over all relations ordered by createdAt using TEMPLATE_ALL.

    - Moves in coarse windows of PAGE_SIZE.
    - On HTTP 500 in a coarse window, runs binary search to find the first bad index.
    - Uses TEMPLATE_UUID to extract source/target UUIDs.
    - Skips that index and continues scanning until we hit an empty page.

    Returns a list of BrokenRelationRecord objects.
    """
    broken: List[BrokenRelationRecord] = []
    offset = 0

    print(f"[INFO] Starting global relation scan with PAGE_SIZE={PAGE_SIZE}")
    print(f"[INFO] Using templates:\n  ALL  = {TEMPLATE_ALL}\n  UUID = {TEMPLATE_UUID}\n")

    while True:
        print(f"\n[SCAN] Probing coarse window offset={offset}, limit={PAGE_SIZE}")
        ok, items, err = run_rel_page(
            limit=PAGE_SIZE,
            offset=offset,
            template=TEMPLATE_ALL,
        )

        if ok:
            # Successful window: if empty, we're done.
            count = len(items)
            print(f"[SCAN] OK, count={count}")
            if count == 0:
                print("[SCAN] Empty page reached; scan complete.")
                break

            # We don't need to keep these; just mark them as good.
            # Move offset by *PAGE_SIZE* — safe even if last page shorter.
            offset += PAGE_SIZE
            continue

        # Coarse window failed: binary search inside [offset, offset+PAGE_SIZE)
        print(
            f"[SCAN] HTTP error in coarse window offset={offset}, "
            f"limit={PAGE_SIZE}. Starting binary search..."
        )
        bad_index, err_text = find_failing_index_in_window(
            lo=offset,
            hi=offset + PAGE_SIZE,
        )

        print(f"[RESULT] Found failing relation at global index={bad_index}")

        # Use UUID-only template to inspect the specific relation
        src, tgt, state, uuid_err = inspect_bad_index_with_uuid_template(bad_index)

        combined_err = err_text
        if uuid_err:
            combined_err = (combined_err + "\n\n[UUID-INSPECT]\n" + uuid_err).strip()

        record = BrokenRelationRecord(
            index=bad_index,
            source=src,
            target=tgt,
            state=state,
            error=combined_err,
        )
        broken.append(record)

        print(
            f"[BROKEN] index={record.index}, source={record.source}, "
            f"target={record.target}, state={record.state}"
        )

        # IMPORTANT: skip past this bad index so we don't hit it again
        offset = bad_index + 1

    return broken


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def main() -> None:
    # Quick sanity checks
    if not AGNOSTIC_SH.is_file():
        print(f"[ERROR] agnostic.sh not found at {AGNOSTIC_SH}", file=sys.stderr)
        sys.exit(1)

    if not TEMPLATE_ALL.is_file():
        print(f"[ERROR] TEMPLATE_ALL not found at {TEMPLATE_ALL}", file=sys.stderr)
        sys.exit(1)

    if not TEMPLATE_UUID.is_file():
        print(f"[ERROR] TEMPLATE_UUID not found at {TEMPLATE_UUID}", file=sys.stderr)
        sys.exit(1)

    broken = scan_all_relations()

    data = [asdict(r) for r in broken]
    with SUMMARY_PATH.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"\n=== SUMMARY ===")
    print(f"Broken relations found: {len(broken)}")
    print(f"Details written to: {SUMMARY_PATH}")


if __name__ == "__main__":
    main()