#!/usr/bin/env python3
from pathlib import Path
import os, sys, json, subprocess, re

from config_env import (
    load_env_file,
    find_project_root
)

load_env_file()

SCRIPT_DIR   = Path(__file__).resolve().parent
PROJECT_ROOT = find_project_root(SCRIPT_DIR)

REL_SH = SCRIPT_DIR / "relation.sh"

# 1) Template that SOMETIMES fails (`$cmdb_typeName` etc.) – used for detection
AGNOSTIC_TEMPLATE = PROJECT_ROOT / "groovy/template/relation_template_agnostic_all.groovy"

# 2) Template that NEVER fails – used for inspection (returns {source, target, state})
INSPECT_TEMPLATE  = PROJECT_ROOT / "groovy/template/relation_template_all.groovy"

# --- configure which relation + types we are hunting ---
RELTYPE = "PO_je_poskytovatelom_KS"
TARGET  = "KS"
SOURCE  = "PO"

# page size for scanning (coarse windows)
SCAN_LIMIT = 2048

SUBPROC_TIMEOUT = float(os.getenv("METAIS_SUBPROC_TIMEOUT", "180"))

# scratch + output paths
DEBUG_OUTDIR = PROJECT_ROOT / "test" / "debug_scan_bad_relations"
DEBUG_OUTDIR.mkdir(parents=True, exist_ok=True)

OUTPUT_JSON = PROJECT_ROOT / "test" / f"bad_relations_{RELTYPE}_{TARGET}_{SOURCE}.json"


# ---------------------------------------------------------------------
# low-level helpers
# ---------------------------------------------------------------------

def run_cmd(cmd: str):
    """
    Run a shell command, return (ok, wrote_files, reason, stderr_text).

    reason:
      - "ok"
      - "token-missing"
      - "http-401", "http-403", "http-500", or "http-<code>"
      - "subprocess-timeout"
      - "exit-<code>"
    """
    wrote_files = []
    stderr_buf  = []

    try:
        p = subprocess.run(
            cmd,
            shell=True,
            text=True,
            capture_output=True,
            check=True,
            timeout=SUBPROC_TIMEOUT,
        )

        for line in p.stdout.splitlines():
            if line.startswith("Wrote:"):
                print(line)
                path = (
                    line.split("Wrote:", 1)[1]
                    .strip()
                    .split()[0]
                    .strip("()")
                )
                wrote_files.append(path)
            else:
                print(line)

        if p.stderr:
            for line in p.stderr.splitlines():
                stderr_buf.append(line)
                print(line, file=sys.stderr)

        return True, wrote_files, "ok", "\n".join(stderr_buf)

    except subprocess.TimeoutExpired as e:
        print(f"[ERROR] Subprocess timeout after {SUBPROC_TIMEOUT}s.")
        if e.stdout:
            print(e.stdout)
        if e.stderr:
            stderr_buf.append(e.stderr)
            print(e.stderr, file=sys.stderr)
        return False, wrote_files, "subprocess-timeout", "\n".join(stderr_buf)

    except subprocess.CalledProcessError as e:
        print("[ERROR] Subprocess failed.")
        if e.stdout:
            print(e.stdout)
        if e.stderr:
            for line in e.stderr.splitlines():
                stderr_buf.append(line)
                print(line, file=sys.stderr)

        stderr_text = "\n".join(stderr_buf)

        if "TOKEN env var is required" in stderr_text:
            return False, wrote_files, "token-missing", stderr_text

        m = re.search(r"HTTP ERROR:\s+(\d+)", stderr_text)
        if m:
            code = m.group(1)
            return False, wrote_files, f"http-{code}", stderr_text

        return False, wrote_files, f"exit-{e.returncode}", stderr_text


def load_items(path: str) -> list:
    """
    Load items from JSON:
      {"result":[...]} or {"results":[...]} or raw list.
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, dict):
        if "result" in data:
            return data["result"]
        if "results" in data:
            return data["results"]
    if isinstance(data, list):
        return data
    return []


def build_rel_cmd(template: Path, limit: int, offset: int) -> str:
    """
    Build relation.sh command for given template, limit, offset.
    """
    return (
        f'"{REL_SH}" {TARGET} {SOURCE} {RELTYPE} '
        f'--template "{template}" '
        f'--limit {limit} --offset {offset} '
        f'--outdir "{DEBUG_OUTDIR}" --no-csv'
    )


# ---------------------------------------------------------------------
# detection side (AGNOSTIC template – used only to provoke failures)
# ---------------------------------------------------------------------

def test_window(limit: int, offset: int) -> tuple[str, int]:
    """
    Test a window (offset, limit) using the AGNOSTIC template.

    Returns:
      ("ok", count)         if HTTP 200 and we parsed count rows
      ("empty", 0)          if HTTP 200 and count == 0
      ("http-500", -1)      if server returns HTTP 500
      ("other-error", -1)   for any other failure (raised)
    """
    cmd = build_rel_cmd(AGNOSTIC_TEMPLATE, limit, offset)
    print(f"[TEST] limit={limit}, offset={offset}")

    ok, wrote, reason, stderr_text = run_cmd(cmd)

    if ok:
        count = 0
        if wrote:
            last_path = wrote[-1]
            try:
                items = load_items(last_path)
                count = len(items)
            except Exception as e:
                print(f"[WARN] Could not parse {last_path}: {e}")
        print(f"[TEST] OK, count={count}")
        if count == 0:
            return "empty", 0
        return "ok", count

    if reason == "http-500":
        print(f"[TEST] http-500 at offset={offset}, limit={limit}")
        return "http-500", -1

    raise RuntimeError(
        f"Non-500 error at offset={offset}, limit={limit}: {reason}\n{stderr_text}"
    )


def shrink_failing_window(start: int, end: int) -> tuple[int, int]:
    """
    Given a window [start, end) such that test_window(end-start, start)
    produces http-500, try to shrink it by recursively halving (still using
    the AGNOSTIC template).

    Returns:
      (s, e) where [s, e) is a (possibly minimal) failing window.
    """
    length = end - start
    print(f"[SHRINK] Start shrinking failing window [{start}, {end}) "
          f"(length={length})")

    # Base case
    if length <= 1:
        print(f"[SHRINK] Reached indivisible window [{start}, {end})")
        return start, end

    mid = (start + end) // 2

    left_len  = mid - start
    right_len = end - mid

    left_fails = False
    if left_len > 0:
        status_left, _ = test_window(left_len, start)
        if status_left == "http-500":
            left_fails = True

    if left_fails:
        return shrink_failing_window(start, mid)

    right_fails = False
    if right_len > 0:
        status_right, _ = test_window(right_len, mid)
        if status_right == "http-500":
            right_fails = True

    if right_fails:
        return shrink_failing_window(mid, end)

    # Neither half fails → server only explodes when both halves are together
    print("[SHRINK] Neither half fails; minimal failing window is "
          f"[{start}, {end})")
    return start, end


# ---------------------------------------------------------------------
# inspection side (INSPECT template – never fails)
# ---------------------------------------------------------------------

def inspect_index(idx: int):
    """
    Fetch a single row at offset=idx using INSPECT_TEMPLATE and
    return structured record for bad-relations output.

    This NEVER calls the agnostic template; only the safe one.
    """
    limit = 1
    cmd = build_rel_cmd(INSPECT_TEMPLATE, limit, idx)

    print(f"[INSPECT] offset={idx}, limit=1")
    ok, wrote, reason, stderr_text = run_cmd(cmd)

    if not ok:
        print(f"[INSPECT] ERROR at offset={idx}: {reason}")
        print(stderr_text)
        return None

    if not wrote:
        print(f"[INSPECT] No file written at offset={idx}")
        return None

    last_path = wrote[-1]
    try:
        items = load_items(last_path)
    except Exception as e:
        print(f"[INSPECT] Could not parse {last_path}: {e}")
        return None

    if not items:
        print(f"[INSPECT] Empty items at offset={idx}")
        return None

    first = items[0]
    src   = first.get("source")
    tgt   = first.get("target")
    state = first.get("state")  # DRAFT / INVALIDATED / etc.

    if not src or not tgt:
        print(f"[INSPECT] Missing source/target at offset={idx}")
        return None

    rec = {
        "source":     src,
        "target":     tgt,
        "sourceType": SOURCE,   # citype from script constants
        "targetType": TARGET,   # citype from script constants
        "relType":    RELTYPE,
        "relState":   state,
        "offset":     idx,
    }
    return rec


def inspect_window_collect(start: int, end: int):
    """
    For a (small) failing window [start, end), inspect each index via
    INSPECT_TEMPLATE and return a list of records.

    Safety: don't blindly inspect giant failing windows.
    """
    length = end - start
    print(f"[INSPECT] Inspecting window [{start}, {end}) (length={length})")

    if length > 128:
        print("[INSPECT] Window too large to inspect blindly; "
              "refine further or adjust the safety limit.")
        return []

    records = []
    for idx in range(start, end):
        rec = inspect_index(idx)
        if rec is not None:
            records.append(rec)

    return records


# ---------------------------------------------------------------------
# main scanning loop
# ---------------------------------------------------------------------

def main():
    print(f"=== Scanning for bad relations: {RELTYPE} {TARGET} <- {SOURCE} ===")

    bad_records = []
    seen_pairs  = set()  # (source, target, relType)

    offset = 0

    while True:
        status, count = test_window(SCAN_LIMIT, offset)

        if status == "empty":
            print(f"[SCAN] Reached empty page at offset={offset}; done.")
            break

        if status == "ok":
            # window is clean under AGNOSTIC template → move on
            if count < SCAN_LIMIT:
                print(f"[SCAN] Last partial ok page at offset={offset}, "
                      f"count={count}; done.")
                break
            offset += count
            continue

        if status == "http-500":
            # Found a failing window starting at this offset.
            start = offset
            end   = offset + SCAN_LIMIT
            print(f"[SCAN] Failing window detected at [{start}, {end})")

            # Sanity: re-assert failure before shrinking
            status2, _ = test_window(end - start, start)
            if status2 != "http-500":
                print("[WARN] Failing window no longer fails; skipping.")
                offset = end
                continue

            s, e = shrink_failing_window(start, end)
            print(f"[SCAN] Minimal failing window: [{s}, {e}) "
                  f"(length={e-s})")

            # Collect candidate bad relations from this window using INSPECT_TEMPLATE
            window_recs = inspect_window_collect(s, e)
            for rec in window_recs:
                key = (rec["source"], rec["target"], rec["relType"])
                if key not in seen_pairs:
                    seen_pairs.add(key)
                    bad_records.append(rec)

            # Continue scanning after this window so we find later failures
            offset = e
            continue

        print(f"[SCAN] Unexpected status={status} at offset={offset}; aborting.")
        break

    # Write aggregated results
    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_JSON.open("w", encoding="utf-8") as f:
        json.dump(bad_records, f, ensure_ascii=False, indent=2)

    print("\n=== SUMMARY ===")
    print(f"Found {len(bad_records)} suspect relations.")
    print(f"Written to: {OUTPUT_JSON}")


if __name__ == "__main__":
    main()