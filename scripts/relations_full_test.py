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
MIN_PAGE_SIZE_FOR_SHRINK = 1
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

def _is_server_500(stderr_text: str) -> bool:
    s = (stderr_text or "").lower()
    return "http error:" in s and " 500" in s

def run_rel_page(
    limit: int,
    offset: int,
    template: Path,
) -> Tuple[bool, Optional[List[dict]], str]:
    """
    Call agnostic.sh for relations with given limit/offset and GROOVY template.

    Returns:
        (ok, items_or_None, error_text_if_any)

    - ok=True  → items is a list (possibly empty).
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

        # --- success path ----------------------------------------------------
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

        # --- non-zero return code -------------------------------------------

        # 1) Auth/TOKEN problem?
        if _is_auth_error(stderr):
            if _prompt_for_new_token():
                env["TOKEN"] = os.environ["TOKEN"]
                # retry *without* counting as a transient attempt
                continue
            else:
                return False, None, last_err_text

        # 2) 504 Gateway Time-out – server choking on big window.
        #    No retries here; caller will shrink limit.
        is_504 = ("HTTP ERROR: 504" in stderr) or ("504 Gateway Time-out" in stderr)
        if is_504:
            return False, None, last_err_text

        # 3) 500 script error (gnr500 / rpt05 / ScriptException).
        #    This is almost certainly a deterministic bad relation, not a transient.
        is_500_script = (
            "HTTP ERROR: 500" in stderr
            or " status=500 INTERNAL_SERVER_ERROR" in stderr
            or "type=rpt05" in stderr
            or "type=gnr500" in stderr
            or "ScriptingException" in stderr
        )
        if is_500_script:
            # Do NOT retry here. Let the caller (coarse scan / binary search)
            # handle it as a real data problem.
            return False, None, last_err_text

        # 4) Other errors → transient, respect PAGE_MAX_RETRIES
        if PAGE_MAX_RETRIES > 0 and attempt >= PAGE_MAX_RETRIES:
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

def scan_all_relations(start_offset: int = 0) -> List[BrokenRelationRecord]:
    """
    Linear scan over all relations ordered by createdAt using TEMPLATE_ALL.

    - Moves in windows of size `window_limit` (initially PAGE_SIZE).
    - On HTTP 504 in a coarse window, shrinks `window_limit` (e.g. halved) and retries.
    - On "too slow" OK windows (runtime > REL_SOFT_TIME_LIMIT), also shrinks window_limit.
    - On true 500/script errors, runs binary search inside [offset, offset + window_limit)
      to find the first bad index, records it, and skips past it.
    - Stops on the first empty OK page.

    Returns a list of BrokenRelationRecord objects.
    """
    broken: List[BrokenRelationRecord] = []

    # dynamic window size, starts at PAGE_SIZE and only shrinks
    window_limit = PAGE_SIZE
    max_limit = PAGE_SIZE

    # how long is "too long" for an OK page before we decide window is too big?
    SOFT_TIME_LIMIT = float(os.getenv("REL_SOFT_TIME_LIMIT", "55"))

    offset = start_offset

    print(f"[INFO] Starting global relation scan with PAGE_SIZE={PAGE_SIZE}, start_offset={start_offset}")
    print(f"[INFO] Using templates:\n  ALL  = {TEMPLATE_ALL}\n  UUID = {TEMPLATE_UUID}\n")
    print(f"[INFO] Initial window_limit={window_limit}, SOFT_TIME_LIMIT={SOFT_TIME_LIMIT}s\n")

    while True:
        print(f"\n[SCAN] Probing coarse window offset={offset}, limit={window_limit}")

        t0 = time.monotonic()
        ok, items, err = run_rel_page(
            limit=window_limit,
            offset=offset,
            template=TEMPLATE_ALL,
        )
        elapsed = time.monotonic() - t0

        # ------------------------------------------------------------
        # Success: OK page
        # ------------------------------------------------------------
        if ok:
            count = len(items)
            print(f"[SCAN] OK, count={count}, limit={window_limit}, time={elapsed:.1f}s")

            if count == 0:
                print("[SCAN] Empty page reached; scan complete.")
                break

            # If the request took too long but succeeded, shrink the window
            # so we don't flirt with the 60s timeout.
            if elapsed > 55 and window_limit > 1:
                new_limit = max(1, int(0.9 * window_limit))
                print(
                    f"[SCAN] Page took {elapsed:.1f}s (> 55s). "
                    f"Shrinking window_limit {window_limit} → {new_limit} for future pages."
                )
                window_limit = new_limit

            if elapsed < 45:
                new_limit = max(1, int(1.2 * window_limit))
                print(
                    f"[SCAN] Page took {elapsed:.1f}s (< 15s). "
                    f"Raising window_limit {window_limit} → {new_limit} for future pages."
                )
                window_limit = new_limit

            # Advance by what we actually requested, not the original PAGE_SIZE
            offset += window_limit
            continue

        # ------------------------------------------------------------
        # Failure: not OK
        # Decide if it's a 504 (performance) or a real 500/script error
        # ------------------------------------------------------------
        err_lower = (err or "").lower()
        is_504 = ("http error: 504" in err) or ("504 gateway time-out" in err_lower)

        # 504: server timed out → shrink window and retry *same* offset
        if is_504 and window_limit > 1:
            new_limit = max(1, window_limit // 2)
            print(
                f"[SCAN] 504 on coarse window offset={offset}, limit={window_limit}. "
                f"Shrinking window_limit → {new_limit} and retrying same offset."
            )
            window_limit = new_limit
            # no offset change; just retry with smaller limit
            continue

        # If it's 504 and we're already at limit==1, there's nothing more we can do
        if is_504 and window_limit == 1:
            raise RuntimeError(
                f"[SCAN] 504 Gateway Time-out even with window_limit=1 at offset={offset}. "
                f"Can't progress further."
            )

        # For non-504 errors here, we expect true script / data errors (500 etc).
        print(
            f"[SCAN] Non-504 error in coarse window offset={offset}, "
            f"limit={window_limit}. Starting binary search..."
        )

        # Use the *current* window_limit as [lo, hi) range for binary search.
        bad_index, err_text = find_failing_index_in_window(
            lo=offset,
            hi=offset + window_limit,
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

        # Skip past this bad index so we don't hit it again
        offset = bad_index + 1

    return broken


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def main() -> None:
    resume_offset = 0

    # simple manual arg parsing
    if len(sys.argv) >= 3 and sys.argv[1] == "--resume":
        resume_offset = int(sys.argv[2])
        print(f"[INFO] Resuming scan from offset={resume_offset}")

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

    broken = scan_all_relations(resume_offset)

    data = [asdict(r) for r in broken]
    with SUMMARY_PATH.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"\n=== SUMMARY ===")
    print(f"Broken relations found: {len(broken)}")
    print(f"Details written to: {SUMMARY_PATH}")


if __name__ == "__main__":
    main()