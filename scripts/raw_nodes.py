#!/usr/bin/env python3
import subprocess, os, re, sys, json
import time
import requests
from pathlib import Path
from datetime import date  # <-- ADD THIS

from config_env import (
    load_env_file,
    get_include_types,
    get_valid_flag,
    VALID_BOTH,
    VALID_ONLY,
    INVALID_ONLY,
)

load_env_file()

SCRIPT_DIR = Path(__file__).resolve().parent
RAW_SH = SCRIPT_DIR / "raw.sh"

PROJECT_ROOT = Path(__file__).resolve().parents[1]

METAIS_DATE = os.getenv("METAIS_DATE", date.today().strftime("%d-%m-%Y"))

RAW_ROOT = PROJECT_ROOT / os.getenv("METAIS_RAW_ROOT", "output")
default_out = RAW_ROOT / METAIS_DATE / "nodes"

STATS_ROOT = PROJECT_ROOT / os.getenv("METAIS_STATS_ROOT", "meta-viz/data/stats")
METADATA_ROOT = PROJECT_ROOT / os.getenv("METAIS_METADATA_ROOT", "meta-viz/data/metadata")

# Final output directory for RAW node dumps
OUT_DIR = Path(
    os.getenv("METAIS_NODES_DIR") or
    os.getenv("NODES_DIR") or
    default_out
)
OUT_DIR.mkdir(parents=True, exist_ok=True)

# How many retries per report

TYPES_URL = os.getenv(
    "CITYPES_URL",
    "https://metais.slovensko.sk/api/types-repo/citypes/list"
)

INCLUDE_TYPES = get_include_types("INCLUDE_TYPES", "application,system,codelist")
VALID_FLAG = get_valid_flag("VALID_FLAG", "both")

INCLUDE_REGEX = os.getenv("METAIS_INCLUDE_REGEX", "")  # e.g. r"^(Agenda|AS|ISVS|KS|Projekt|Program|ZS|InfraSluzba|Integracia|Kanal|KRIS)$"
EXCLUDE_REGEX = os.getenv("METAIS_EXCLUDE_REGEX", "")

FORCE_INCLUDE = set(filter(None, [s.strip() for s in os.getenv("METAIS_FORCE_INCLUDE", "").split(",")]))

# IMPORTANT: tell raw.sh explicitly where to write
RAW_CMD = os.getenv(
    "METAIS_RAW_CMD",
    f'"{RAW_SH}" {{name}} --outdir "{OUT_DIR}"'
)
MAX_RETRIES = int(os.getenv("CONNECTION_MAX_RETRIES", "5"))
RETRY_DELAY = float(os.getenv("CONNECTION_RETRY_DELAY", "0.25"))
FETCH_TIMEOUT = float(os.getenv("METAIS_FETCH_TIMEOUT", "60"))

SUBPROC_TIMEOUT = float(os.getenv("METAIS_SUBPROC_TIMEOUT", "180"))
PAGE_SIZE = int(os.getenv("METAIS_PAGE_SIZE", "5000"))

OUT_DIR = Path(
    os.getenv("METAIS_NODES_DIR") or
    os.getenv("NODES_DIR") or
    default_out
)
OUT_DIR.mkdir(parents=True, exist_ok=True)

LIST_CACHE_DIR = Path(os.getenv("METAIS_LIST_CACHE_DIR", "output/types_meta"))
LIST_CACHE_DIR.mkdir(parents=True, exist_ok=True)

FALLBACK_REPORTS = ["Agenda", "AS", "InfraSluzba", "Integracia", "ISVS", "Kanal", "KRIS", "KS", "Projekt", "Program", "ZS"]

def fetch_citypes():
    headers = {"Accept": "application/json"}

    print(f"[INFO] Fetching types from {TYPES_URL}")
    resp = requests.get(TYPES_URL, headers=headers, timeout=FETCH_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    # cache the whole payload
    with (LIST_CACHE_DIR / "citypes_list.json").open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    if not isinstance(data, dict) or "results" not in data:
        raise ValueError("Unexpected response shape (no 'results' key).")

    return data["results"]

def build_node_set(results):
    include_re = re.compile(INCLUDE_REGEX) if INCLUDE_REGEX else None
    exclude_re = re.compile(EXCLUDE_REGEX) if EXCLUDE_REGEX else None

    reports = []
    for item in results:
        # Defensive parsing
        tech = item.get("technicalName") or item.get("name")
        if not tech:
            continue

        # Filter by type and validity
        valid_bit = 0 if item.get("valid", False) else 1
        base_type = (item.get("type") or "").strip().lower()
        labels = [
            (lbl or "").strip().lower()
            for lbl in (item.get("labels") or [])
        ]

        # all “tags” this citype carries
        tags = set(labels)
        if base_type:
            tags.add(base_type)

        # if "all" not requested, require intersection with INCLUDE_TYPES
        if "all" not in INCLUDE_TYPES and not (tags & INCLUDE_TYPES):
            continue
        if not (VALID_FLAG & (1 << valid_bit)):
            continue

        # Regex filters
        if include_re and not include_re.search(tech):
            # Not in include regex; might still be force-included later
            pass
        elif exclude_re and exclude_re.search(tech):
            continue
        # If included by the include_re, add now
        if (not include_re) or include_re.search(tech):
            reports.append(tech)

    # Add force-includes even if filtered out above
    for name in FORCE_INCLUDE:
        if name and name not in reports:
            reports.append(name)

    # Dedup (preserve order) & sort for stability
    seen = set()
    deduped = [x for x in reports if not (x in seen or seen.add(x))]

    if not deduped and not FORCE_INCLUDE:
        # If filtering is too strict, try a sane broadened set:
        print("[WARN] Filtered list is empty; falling back to your previous hand-picked set.")
        return FALLBACK_REPORTS

    return deduped

def is_empty_raw_file(path: str) -> bool:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            # be robust: only require empty 'result'
            if data.get("type") == "RAW" and isinstance(data.get("result"), list) and len(data["result"]) == 0:
                return True
            # many backends omit 'type'; still consider empty 'result' as empty payload
            if "result" in data and isinstance(data["result"], list) and len(data["result"]) == 0:
                return True
        return False
    except Exception:
        # If it's unreadable or malformed, don't delete silently.
        return False

def run_cmd(cmd: str) -> tuple[bool, list[str]]:
    """
    Run a shell command, return (ok, wrote_files[]). Captures stdout/stderr.
    """
    wrote_files = []
    try:
        p = subprocess.run(
            cmd,
            shell=True,
            text=True,
            check=True,
            capture_output=True,
            timeout=SUBPROC_TIMEOUT,
        )
        for line in p.stdout.splitlines():
            if line.startswith("Wrote:"):
                print(line)
                path = line.split("Wrote:", 1)[1].strip().split()[0].strip("()")
                wrote_files.append(path)
            else:
                print(line)
        if p.stderr:
            for line in p.stderr.splitlines():
                print(line, file=sys.stderr)
        return True, wrote_files
    except subprocess.TimeoutExpired as e:
        print(f"[ERROR] Subprocess timeout after {SUBPROC_TIMEOUT}s.")
        if e.stdout:
            print(e.stdout)
        if e.stderr:
            print(e.stderr, file=sys.stderr)
        return False, wrote_files
    except subprocess.CalledProcessError as e:
        print("[ERROR] Subprocess failed.")
        if e.stdout:
            print(e.stdout)
        if e.stderr:
            print(e.stderr, file=sys.stderr)
        return False, wrote_files

def run_cmd_with_reason(cmd: str) -> tuple[bool, list[str], str, str]:
    """
    Run a shell command, return (ok, wrote_files, reason, stderr_text).

    reason:
      - "ok"                  → success
      - "subprocess-timeout"  → Python's SUBPROC_TIMEOUT hit
      - "http-<code>"         → core.sh printed 'HTTP ERROR: <code>'
      - "exit-<code>"         → generic non-zero exit status
    """
    wrote_files: list[str] = []
    stderr_buf: list[str] = []

    try:
        p = subprocess.run(
            cmd,
            shell=True,
            text=True,
            check=True,
            capture_output=True,
            timeout=SUBPROC_TIMEOUT,
        )

        # stdout
        for line in p.stdout.splitlines():
            if line.startswith("Wrote:"):
                print(line)
                path = line.split("Wrote:", 1)[1].strip().split()[0].strip("()")
                wrote_files.append(path)
            else:
                print(line)

        # stderr
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

        # Try to detect the HTTP code from core.sh's message:
        #   HTTP ERROR: 401 from ...
        m = re.search(r"HTTP ERROR:\s+(\d+)", stderr_text)
        if m:
            http_code = m.group(1)
            reason = f"http-{http_code}"
        else:
            reason = f"exit-{e.returncode}"

        return False, wrote_files, reason, stderr_text

def load_json_file(p: str):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

def write_raw_json(path: Path, items: list):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"type": "RAW", "result": items}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    print(f"Wrote: {path} ({path.stat().st_size} bytes)")

def count_uuids_for_type(t: str) -> int:
    # Write UUIDs to a scratch file per convention; we reuse the runner’s output location.
    # We ask raw.sh to write into a special OUTDIR to avoid clobbering the final file.
    scratch_dir = OUT_DIR / "_uuid_sizing"
    scratch_dir.mkdir(parents=True, exist_ok=True)
    cmd = (
        f'"{RAW_SH}" {t} '
        f'--template groovy/templates/extract_node_uuid_template.groovy '
        f'--outdir "{scratch_dir}" --no-csv'
    )
    ok, wrote = run_cmd(cmd)
    if not ok or not wrote:
        raise RuntimeError(f"UUID sizing for {t} failed.")
    data = load_json_file(wrote[-1])  # last written
    # If runner wrapped it: {"type":"RAW","result":[...]} OR direct list
    if isinstance(data, dict) and "result" in data:
        return len(data["result"])
    if isinstance(data, list):
        return len(data)
    raise ValueError("Unexpected UUID sizing output shape.")

def fetch_page(t: str, limit: int, offset: int, page_index: int) -> list:
    page_dir = OUT_DIR / f"__parts__/{t}"
    page_dir.mkdir(parents=True, exist_ok=True)
    cmd = (
        f'"{RAW_SH}" {t} '
        f'--template groovy/templates/extract_raw_paged_template.groovy '
        f'--limit {limit} --offset {offset} '
        f'--outdir "{page_dir}" --no-csv'
    )
    ok, wrote = run_cmd(cmd)
    if not ok or not wrote:
        raise RuntimeError(f"Page fetch failed for {t} offset={offset} limit={limit}")
    part = load_json_file(wrote[-1])
    if isinstance(part, dict) and "result" in part:
        return part["result"]
    # If template returned bare list of nodes, accept that:
    if isinstance(part, list):
        return part
    raise ValueError("Unexpected page output shape.")

def rebuild_full_from_pages(t: str, total_count: int, limit: int):
    parts_dir = OUT_DIR / f"__parts__/{t}"
    files = sorted(parts_dir.glob("*.json"))  # order doesn't matter for RAW
    out_path = OUT_DIR / f"{t}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write('{"type":"RAW","result":[')
        first = True
        for fp in files:
            part = load_json_file(str(fp))
            items = part["result"] if isinstance(part, dict) and "result" in part else part
            for it in items:
                if not first:
                    f.write(",")
                json.dump(it, f, ensure_ascii=False)
                first = False
        f.write("]}")
    print(f"Wrote: {out_path} ({out_path.stat().st_size} bytes)")

def run_paginated(report_name: str, per_page: int):
    print(f"[PAGE] {report_name}: paginated fetch without sizing (limit={per_page})")
    page_index = 0
    total_items = 0
    max_pages = int(os.getenv("METAIS_MAX_PAGES", "100000"))  # safety cap

    while page_index < max_pages:
        offset = page_index * per_page
        print(f"[PAGE] {report_name}: fetching page {page_index+1} (offset={offset})")
        items = fetch_page(report_name, per_page, offset, page_index)
        if not items:
            print(f"[PAGE] {report_name}: empty page at offset={offset}; done.")
            break

        # Save each page as RAW for audit
        page_path = OUT_DIR / f"__parts__/{report_name}" / f"{report_name}.offset{offset}.limit{per_page}.json"
        write_raw_json(page_path, items)

        total_items += len(items)
        page_index += 1

    # Merge pages
    rebuild_full_from_pages(report_name, total_items, per_page)
    print(f"[OK] {report_name}: paginated rebuild done with {total_items} items.")
    return True

def run_with_retries(report_name, idx, total):
    print(f"\n=== Downloading raw report {report_name} ({idx}/{total}) ===")
    attempt = 1

    while attempt <= MAX_RETRIES:
        cmd = RAW_CMD.format(name=report_name)
        ok, _wrote, reason, stderr_text = run_cmd_with_reason(cmd)

        if ok:
            print(f"[OK] {report_name} downloaded successfully")
            return True

        # 1) Hard timeout → go straight to paginated fetch
        if reason == "subprocess-timeout":
            print(
                f"[FALLBACK] {report_name}: subprocess timed out, "
                f"switching immediately to paginated fetch (PAGE_SIZE={PAGE_SIZE})…"
            )
            try:
                return run_paginated(report_name, PAGE_SIZE)
            except Exception as e:
                print(f"[ERROR] Paginated fetch failed for {report_name}: {e}")
                return False

        # 2) HTTP auth-ish errors → offer interactive TOKEN fix when possible
        if reason in ("http-401", "http-403") and sys.stdin.isatty():
            print(
                "\n[AUTH] The server responded with an authorization error "
                f"({reason}).\n"
                "This usually means your TOKEN is missing or expired.\n"
            )
            print(
                "If you want, paste a new TOKEN now (input will be visible in this shell).\n"
                "Press Enter on an empty line to abort without changing anything."
            )
            try:
                new_token = input("New TOKEN: ").strip()
            except EOFError:
                new_token = ""

            if new_token:
                os.environ["TOKEN"] = new_token
                print("[INFO] TOKEN updated in this process; retrying the same report…")
                # Don't count this as a "retry attempt" – try again with fresh TOKEN
                continue
            else:
                print("[ERROR] No TOKEN provided; aborting this report.")
                return False

        # 3) Any other error → respect MAX_RETRIES
        print(
            f"[WARN] {report_name}: attempt {attempt}/{MAX_RETRIES} failed "
            f"({reason}). Retrying in {RETRY_DELAY}s…"
        )
        time.sleep(RETRY_DELAY)
        attempt += 1

    # 4) Ran out of retries → paginated fallback
    print(
        f"[FALLBACK] {report_name}: switching to paginated fetch after "
        f"{MAX_RETRIES} failed attempts (PAGE_SIZE={PAGE_SIZE})…"
    )
    try:
        return run_paginated(report_name, PAGE_SIZE)
    except Exception as e:
        print(f"[ERROR] Paginated fetch failed for {report_name}: {e}")
        return False

def main():
    try:
        results = fetch_citypes()
        reports = build_node_set(results)
        print(f"[INFO] Will process {len(reports)} types: {', '.join(reports)}")
    except Exception as e:
        print(f"[ERROR] Could not fetch types ({e}). Using fallback list.", file=sys.stderr)
        reports = FALLBACK_REPORTS

    total = len(reports)
    failures = 0
    for i, report in enumerate(reports, start=1):
        ok = run_with_retries(report, i, total)
        if not ok:
            failures += 1

    print(f"\n[INFO] Completed: {total - failures} ok / {failures} failed.")


if __name__ == "__main__":
    main()