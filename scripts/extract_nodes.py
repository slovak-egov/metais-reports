#!/usr/bin/env python3
import os
import sys
import json
import time
import subprocess
from pathlib import Path
from datetime import date

import requests

from config_env import (
    load_env_file,
    get_include_types,
    get_valid_flag,
    VALID_BOTH,
    VALID_ONLY,
    INVALID_ONLY,
)

# ----------------------------------------------------------------------
# ENV + BASIC PATHS
# ----------------------------------------------------------------------

load_env_file()

def find_project_root(start: Path) -> Path:
    current = start
    while current != current.parent:
        if (current / ".git").exists():
            return current
        current = current.parent
    return start

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = find_project_root(SCRIPT_DIR)
RAW_SH       = SCRIPT_DIR / "raw.sh"

METAIS_DATE = os.getenv("METAIS_DATE", date.today().strftime("%d-%m-%Y"))
RAW_ROOT    = PROJECT_ROOT / os.getenv("METAIS_RAW_ROOT", "output")
DATE_ROOT   = RAW_ROOT / METAIS_DATE

NODES_DIR       = DATE_ROOT / "nodes"
METADATA_ROOT   = DATE_ROOT / "metadata"
META_NODE_DIR   = METADATA_ROOT / "nodes"

NODES_DIR.mkdir(parents=True, exist_ok=True)
METADATA_ROOT.mkdir(parents=True, exist_ok=True)
META_NODE_DIR.mkdir(parents=True, exist_ok=True)

COMPLETE_FLAG   = NODES_DIR / ".complete"
SKIP_COMPLETE   = bool(os.getenv("SKIP_COMPLETE", "False"))

CITYPES_URL        = os.getenv(
    "CITYPES_URL",
    "https://metais.slovensko.sk/api/types-repo/citypes/list"
)
CITYPES_DETAIL_URL = os.getenv(
    "CITYPES_DETAIL_URL",
    "https://metais.slovensko.sk/api/types-repo/citypes/citype"
)

PAGE_SIZE = int(os.getenv("NODE_PAGE_SIZE", "3000"))

CONN_MAX_RETRIES = int(os.getenv("CONNECTION_MAX_RETRIES", "5"))
CONN_RETRY_DELAY = float(os.getenv("CONNECTION_RETRY_DELAY", "0.5"))
FETCH_TIMEOUT    = float(os.getenv("METAIS_FETCH_TIMEOUT", "60"))
SUBPROC_TIMEOUT  = float(os.getenv("METAIS_SUBPROC_TIMEOUT", "180"))

INCLUDE_TYPES   = get_include_types("INCLUDE_TYPES", "application,system,codelist")
VALID_FLAG_MASK = get_valid_flag("VALID_FLAG", "both")

INCLUDE_REGEX = os.getenv("METAIS_INCLUDE_REGEX", "")
EXCLUDE_REGEX = os.getenv("METAIS_EXCLUDE_REGEX", "")

import re
include_re = re.compile(INCLUDE_REGEX) if INCLUDE_REGEX else None
exclude_re = re.compile(EXCLUDE_REGEX) if EXCLUDE_REGEX else None

# RAW command template:
#   - __TYPE__  -> citype technicalName
#   - __LIMIT__ / __OFFSET__ from Python
#   - __OUTDIR__ scratch dir for each page
NODE_TEMPLATE = os.getenv(
    "METAIS_NODE_TEMPLATE",
    "groovy/template/node_template.groovy"
)

RAW_CMD_TEMPLATE = os.getenv(
    "METAIS_RAW_CMD",
    '"%s" {type} --template %s --limit {limit} --offset {offset} --outdir "{outdir}" --no-csv' % (
        RAW_SH,
        NODE_TEMPLATE,
    ),
)

# ----------------------------------------------------------------------
# HELPERS
# ----------------------------------------------------------------------


def fetch_json_with_retries(url: str) -> dict | list:
    """Simple HTTP fetch with retries for metadata endpoints."""

    attempt = 1
    while CONN_MAX_RETRIES <= 0 or attempt <= CONN_MAX_RETRIES:
        try:
            r = requests.get(url, timeout=FETCH_TIMEOUT, headers={"Accept": "application/json"})
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if CONN_MAX_RETRIES > 0 and attempt >= CONN_MAX_RETRIES:
                raise
            print(f"[WARN] fetch_json_with_retries({url}) failed on attempt "
                  f"{attempt}/{CONN_MAX_RETRIES}: {e}; retrying in {CONN_RETRY_DELAY}s...")
            time.sleep(CONN_RETRY_DELAY)
            attempt += 1


def run_cmd(cmd: str) -> tuple[bool, list[str]]:
    """
    Run a shell command, return (ok, wrote_files).

    We rely on raw.sh printing lines like:
       Wrote: /path/to/file.json
    """
    wrote_files: list[str] = []
    try:
        p = subprocess.run(
            cmd,
            shell=True,
            text=True,
            capture_output=True,
            check=True,
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


def load_json(path: str | Path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_raw_json(path: Path, items: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"type": "RAW", "result": items}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    print(f"Wrote: {path} ({path.stat().st_size} bytes)")


def write_nodes_streaming(ctype: str, limit: int):
    out_path = NODES_DIR / f"{ctype}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with out_path.open("w", encoding="utf-8") as f:
        f.write('{"type":"RAW","result":[')
        first = True
        page_index = 0

        while True:
            offset = page_index * limit
            items = fetch_node_page(ctype, limit, offset)
            if not items:
                print(f"[PAGE] {ctype}: empty page at offset={offset}; done.")
                break

            for it in items:
                if not first:
                    f.write(",")
                json.dump(it, f, ensure_ascii=False)
                first = False

            page_index += 1

        f.write("]}")
    print(f"[OK] {ctype}: written streaming JSON to {out_path}")

# ----------------------------------------------------------------------
# METADATA: CITYPE LIST + PER-CITYPE DETAIL
# ----------------------------------------------------------------------


def fetch_citype_list() -> list[dict]:
    """Fetch and cache the citypes list → metadata/citype_list.json."""
    print(f"[META] Fetching citype list from {CITYPES_URL}")
    data = fetch_json_with_retries(CITYPES_URL)

    # Many endpoints return {"results":[...]}; accept both shapes.
    if isinstance(data, dict) and "results" in data:
        results = data["results"]
    else:
        results = data

    out_path = METADATA_ROOT / "citype_list.json"
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"[META] Cached citype list -> {out_path}")

    return results


def fetch_citype_detail(tech_name: str) -> dict | None:
    """Fetch and cache a single citype metadata JSON → metadata/nodes/KS.json, etc."""
    url = f"{CITYPES_DETAIL_URL}/{tech_name}"
    try:
        data = fetch_json_with_retries(url)
    except Exception as e:
        print(f"[WARN] Could not fetch citype detail for {tech_name}: {e}")
        return None

    out_path = META_NODE_DIR / f"{tech_name}.json"
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"[META] Cached citype detail {tech_name} -> {out_path}")
    return data


def build_node_type_list(citypes: list[dict]) -> list[str]:
    """
    Apply INCLUDE_TYPES / VALID_FLAG / regex to decide which technicalName(s)
    we will actually fetch as RAW nodes.
    """

    reports: list[str] = []
    seen = set()

    for item in citypes:
        tech = item.get("technicalName") or item.get("name")
        if not tech:
            continue

        valid_bit = 0 if item.get("valid", False) else 1
        base_type = (item.get("type") or "").strip().lower()
        labels = [(lbl or "").strip().lower() for lbl in (item.get("labels") or [])]

        tags = set(labels)
        if base_type:
            tags.add(base_type)

        if "all" not in INCLUDE_TYPES and not (tags & INCLUDE_TYPES):
            continue
        if not (VALID_FLAG_MASK & (1 << valid_bit)):
            continue

        if include_re and not include_re.search(tech):
            # not included explicitly
            continue
        if exclude_re and exclude_re.search(tech):
            continue

        if tech not in seen:
            seen.add(tech)
            reports.append(tech)

    return reports


# ----------------------------------------------------------------------
# PAGINATED RAW FETCH
# ----------------------------------------------------------------------


def fetch_node_page(ctype: str, limit: int, offset: int) -> list[dict]:
    """
    Run raw.sh for a single page and return the list of node objects.

    We ask raw.sh to write into a scratch subdirectory so the final stitched
    RAW file can be written by this script.
    """
    page_dir = DATE_ROOT / "nodes_parts" / ctype
    page_dir.mkdir(parents=True, exist_ok=True)

    cmd = RAW_CMD_TEMPLATE.format(
        type=ctype,
        limit=limit,
        offset=offset,
        outdir=str(page_dir),
    )
    print(f"[PAGE] {ctype}: limit={limit}, offset={offset}")
    ok, wrote = run_cmd(cmd)
    if not ok or not wrote:
        raise RuntimeError(f"Page fetch failed for {ctype} offset={offset} limit={limit}")

    last_path = wrote[-1]
    part = load_json(last_path)

    if isinstance(part, dict) and "result" in part:
        return part["result"]
    if isinstance(part, list):
        return part

    raise ValueError(f"Unexpected page output shape for {ctype} at offset={offset}")


def fetch_all_nodes_for_type(ctype: str, limit: int) -> list[dict]:
    """Fetch all pages for a given citype until an empty page is encountered."""
    all_items: list[dict] = []
    page_index = 0

    while True:
        offset = page_index * limit
        items = fetch_node_page(ctype, limit, offset)
        if not items:
            print(f"[PAGE] {ctype}: empty page at offset={offset}; done.")
            break
        all_items.extend(items)
        page_index += 1

    print(f"[OK] {ctype}: fetched {len(all_items)} items in total.")
    return all_items


# ----------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------


def main():
    if SKIP_COMPLETE and COMPLETE_FLAG.exists():
        print(f"[INFO] Nodes already marked complete ({COMPLETE_FLAG}), nothing to do.")
        return

    try:
        citypes = fetch_citype_list()
    except Exception as e:
        print(f"[ERROR] Could not fetch citype list: {e}", file=sys.stderr)
        sys.exit(1)

    # Metadata for all types we know about (even those we might not download RAW for)
    for item in citypes:
        tech = item.get("technicalName") or item.get("name")
        if tech:
            fetch_citype_detail(tech)

    # Decide which types we actually dump as RAW nodes
    reports = build_node_type_list(citypes)
    print(f"[INFO] Will process {len(reports)} node types: {', '.join(reports) if reports else '(none)'}")

    failures = 0
    for i, ctype in enumerate(reports, start=1):
        print(f"\n=== Downloading raw nodes {ctype} ({i}/{len(reports)}) ===")
        try:
            write_nodes_streaming(ctype, PAGE_SIZE)
        except Exception as e:
            print(f"[ERROR] Failed to fetch nodes for {ctype}: {e}")
            failures += 1

    print(f"\n[INFO] Completed nodes: {len(reports) - failures} ok / {failures} failed.")

    if failures == 0 and reports:
        COMPLETE_FLAG.touch()
        print(f"[INFO] Marked node dump as complete: {COMPLETE_FLAG}")
    else:
        print("[WARN] Node dump not complete; .complete flag not written.")


if __name__ == "__main__":
    main()