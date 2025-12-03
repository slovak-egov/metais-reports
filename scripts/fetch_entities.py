#!/usr/bin/env python3
import os
import sys
import json
import time
import subprocess
from pathlib import Path
from datetime import date
import gzip
import shutil
from typing import Any
import requests
import re

from config_env import (
    load_env_file,
    get_include_types,
    get_valid_flag,
    VALID_BOTH,
    VALID_ONLY,
    INVALID_ONLY,
    find_project_root
)

# ----------------------------------------------------------------------
# ENV + BASIC PATHS
# ----------------------------------------------------------------------

load_env_file()

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

# NEW: retry settings for *paged* node fetches (raw.sh / curl issues etc.)
NODE_PAGE_MAX_RETRIES = int(os.getenv("NODE_PAGE_MAX_RETRIES", "10"))
NODE_PAGE_RETRY_DELAY = float(os.getenv("NODE_PAGE_RETRY_DELAY", "1.0"))

INCLUDE_TYPES   = get_include_types("INCLUDE_TYPES", "application,system,codelist")
VALID_FLAG_MASK = get_valid_flag("VALID_FLAG", "both")

INCLUDE_REGEX = os.getenv("METAIS_INCLUDE_REGEX", "")
EXCLUDE_REGEX = os.getenv("METAIS_EXCLUDE_REGEX", "")

include_re = re.compile(INCLUDE_REGEX) if INCLUDE_REGEX else None
exclude_re = re.compile(EXCLUDE_REGEX) if EXCLUDE_REGEX else None

USE_GZIP_RAW_NODES = os.getenv("METAIS_GZIP_RAW", "False").lower() in ("1", "true", "yes")

INCLUDE_INVALID = os.getenv("INCLUDE_INVALID", "true").strip().lower() in (
    "1", "true", "yes", "y", "on", "all"
)

# Two template paths: "all nodes" vs "valid-only"
NODE_TEMPLATE_ALL = os.getenv(
    "METAIS_NODE_TEMPLATE_ALL",
    "groovy/template/node_template_all.groovy",
)
NODE_TEMPLATE_VALID_ONLY = os.getenv(
    "METAIS_NODE_TEMPLATE_VALID_ONLY",
    "groovy/template/node_template_valid_only.groovy",
)

# ----------------------------------------------------------------------
# HELPERS
# ----------------------------------------------------------------------

def build_raw_cmd(ctype: str, limit: int, offset: int, outdir: str) -> str:
    """
    Build the raw.sh command, choosing the Groovy template based on
    global INCLUDE_INVALID flag.
    """
    template = NODE_TEMPLATE_ALL if INCLUDE_INVALID else NODE_TEMPLATE_VALID_ONLY
    return (
        f'"{RAW_SH}" {ctype} --template {template} '
        f'--limit {limit} --offset {offset} --outdir "{outdir}" --no-csv'
    )

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


def run_cmd(cmd: str) -> tuple[bool, list[str], str, str]:
    """
    Run a shell command, return (ok, wrote_files, reason, stderr_text).

    reason:
      - "ok"
      - "token-missing"
      - "http-401", "http-403", or "http-<code>"
      - "subprocess-timeout"
      - "exit-<code>"
    """
    wrote_files: list[str] = []
    stderr_buf: list[str] = []

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

        # 1) Explicit message from run.sh
        if "TOKEN env var is required" in stderr_text:
            return False, wrote_files, "token-missing", stderr_text

        # 2) HTTP ERROR: <code> from core.sh
        m = re.search(r"HTTP ERROR:\s+(\d+)", stderr_text)
        if m:
            code = m.group(1)
            return False, wrote_files, f"http-{code}", stderr_text

        # 3) Generic non-zero exit
        return False, wrote_files, f"exit-{e.returncode}", stderr_text


def load_json(path: str | Path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def open_json_or_gz(path: Path, mode: str = "rt", encoding: str = "utf-8"):
    """
    Open a JSON file that might be plain .json or gzipped (.gz).
    """
    if path.suffix == ".gz":
        return gzip.open(path, mode, encoding=encoding)
    return path.open(mode, encoding=encoding)

def load_json_or_gz(base_dir: Path, stem: str) -> Any:
    """
    Prefer <stem>.json; if missing, try <stem>.json.gz.
    """
    json_path = base_dir / f"{stem}.json"
    gz_path   = base_dir / f"{stem}.json.gz"

    if json_path.is_file():
        with open_json_or_gz(json_path, "rt") as f:
            return json.load(f)

    if gz_path.is_file():
        with open_json_or_gz(gz_path, "rt") as f:
            return json.load(f)

    raise FileNotFoundError(f"No .json or .json.gz for {stem} in {base_dir}")


def write_raw_json(path: Path, items: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"type": "RAW", "result": items}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    print(f"Wrote: {path} ({path.stat().st_size} bytes)")


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

    out_path = METADATA_ROOT / "citypes_list.json"
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

    If TOKEN is missing / expired (401/403), and we're in an interactive TTY,
    prompt the user for a new TOKEN and retry this page until a non-empty
    token is provided or the user aborts.
    """
    page_dir = DATE_ROOT / "nodes_parts" / ctype
    page_dir.mkdir(parents=True, exist_ok=True)

    cmd = build_raw_cmd(ctype, limit, offset, str(page_dir))

    print(f"[PAGE] {ctype}: limit={limit}, offset={offset}")

    while True:
        ok, wrote, reason, stderr_text = run_cmd(cmd)

        # Success path
        if ok and wrote:
            break

        # Token-related problems → interactive fix if possible
        if reason in ("token-missing", "http-401", "http-403") and sys.stdin.isatty():
            print(
                "\n[AUTH] The MetaIS runner reported an authorization problem "
                f"({reason}).\n"
                "This usually means your TOKEN is missing or expired.\n"
            )
            print(
                "If you want, paste a new TOKEN now (input will be visible in this shell).\n"
                "Press Enter on an empty line to abort."
            )
            try:
                new_token = input("New TOKEN: ").strip()
            except EOFError:
                new_token = ""

            if new_token:
                os.environ["TOKEN"] = new_token
                print("[INFO] TOKEN updated in this process; retrying the same page…")
                continue
            else:
                raise RuntimeError(
                    f"TOKEN required but not provided for {ctype} (offset={offset})."
                )

        # Non-auth error → let caller decide about retries
        raise RuntimeError(
            f"Page fetch failed for {ctype} offset={offset} limit={limit} ({reason})"
        )

    # At this point we have a successful run and at least one written file
    last_path = wrote[-1]
    part = load_json(last_path)

    if isinstance(part, dict) and "result" in part:
        return part["result"]
    if isinstance(part, list):
        return part

    raise ValueError(f"Unexpected page output shape for {ctype} at offset={offset}")


def write_nodes_streaming(ctype: str, limit: int, start_offset: int = 0):
    """
    Stream all pages for a given citype into two RAW JSON files:

      - NODES_DIR/<ctype>.json           → non-invalidated (state != "INVALIDATED")
      - NODES_DIR/<ctype>_invalid.json   → invalidated (state == "INVALIDATED")

    Includes retry logic for non-auth failures on individual pages,
    controlled by NODE_PAGE_MAX_RETRIES / NODE_PAGE_RETRY_DELAY.

    If start_offset > 0, we assume that the valid/invalid JSON files already
    contain all records up to that offset, in the same streaming format
    (header + JSON objects, but probably without the final "]}" if a
    previous run crashed). We then open the files in append mode and
    continue writing new records starting at start_offset.
    """
    if start_offset < 0:
        raise ValueError(f"start_offset must be >= 0, got {start_offset}")

    if start_offset % limit != 0:
        raise ValueError(
            f"start_offset {start_offset} is not a multiple of page size {limit}"
        )

    out_valid   = NODES_DIR / f"{ctype}.json"
    out_invalid = NODES_DIR / f"{ctype}_invalid.json"
    out_valid.parent.mkdir(parents=True, exist_ok=True)

    # Calculate starting page index from offset
    page_index = start_offset // limit

    f_valid = None
    f_invalid = None
    first_valid = True
    first_invalid = True

    # Open files: new run vs resume
    if start_offset == 0:
        # Fresh run: overwrite and write header for the "valid" file.
        f_valid = out_valid.open("w", encoding="utf-8")
        f_valid.write('{"type":"RAW","result":[')
        first_valid = True

        # For "invalid" we create lazily when we first see an INVALIDATED entity.
        f_invalid = None
        first_invalid = True
    else:
        # Resume: valid file must exist with previously streamed data
        if not out_valid.exists():
            raise RuntimeError(
                f"Requested resume for {ctype} at offset {start_offset}, "
                f"but {out_valid} does not exist."
            )

        # Append directly; previous run already wrote header + some records.
        f_valid = out_valid.open("a", encoding="utf-8")
        first_valid = False  # assume at least one record exists, like before

        # For invalid file:
        #   - if it exists, append and assume at least one record (first_invalid=False)
        #   - if it doesn't, we will create/initialize it lazily when we first
        #     encounter an INVALIDATED entity.
        if out_invalid.exists():
            f_invalid = out_invalid.open("a", encoding="utf-8")
            first_invalid = False
        else:
            f_invalid = None
            first_invalid = True

    try:
        while True:
            offset = page_index * limit

            # Retry this page on RuntimeError (e.g. curl EXIT 56)
            attempt = 1
            while True:
                try:
                    items = fetch_node_page(ctype, limit, offset)
                    break  # success
                except RuntimeError as e:
                    if NODE_PAGE_MAX_RETRIES > 0 and attempt >= NODE_PAGE_MAX_RETRIES:
                        print(
                            f"[ERROR] {ctype}: giving up on page at offset={offset} "
                            f"after {attempt} attempts: {e}"
                        )
                        raise
                    print(
                        f"[WARN] {ctype}: page offset={offset} attempt "
                        f"{attempt}/{NODE_PAGE_MAX_RETRIES} failed: {e}"
                    )
                    attempt += 1
                    time.sleep(NODE_PAGE_RETRY_DELAY)

            if not items:
                print(f"[PAGE] {ctype}: empty page at offset={offset}; done.")
                break

            for it in items:
                meta = it.get("metaAttributes") or {}
                state = meta.get("state")
                is_invalid = (state == "INVALIDATED")

                # Case 1: we are *not* including invalid nodes at all.
                # The Groovy template should already have filtered them, but we can be safe:
                if not INCLUDE_INVALID:
                    if is_invalid:
                        # Sanity: log and skip if something slipped through
                        print(f"[WARN] {ctype}: got INVALIDATED node despite valid-only template; skipping.")
                        continue

                    if not first_valid:
                        f_valid.write(",")
                    json.dump(it, f_valid, ensure_ascii=False)
                    first_valid = False
                    continue

                # Case 2: we *are* including invalid nodes → split into two files (old behaviour)
                if is_invalid:
                    # Lazily create the invalid file (on first invalid entity).
                    if f_invalid is None:
                        f_invalid = out_invalid.open("w", encoding="utf-8")
                        f_invalid.write('{"type":"RAW","result":[')
                        first_invalid = True

                    if not first_invalid:
                        f_invalid.write(",")
                    json.dump(it, f_invalid, ensure_ascii=False)
                    first_invalid = False
                else:
                    if not first_valid:
                        f_valid.write(",")
                    json.dump(it, f_valid, ensure_ascii=False)
                    first_valid = False

            page_index += 1

        # Close the JSON array/object(s)
        f_valid.write("]}")
        print(f"[OK] {ctype}: written streaming JSON to {out_valid}")

        if f_invalid is not None:
            f_invalid.write("]}")
            print(f"[OK] {ctype}: written streaming JSON of INVALIDATED nodes to {out_invalid}")

    finally:
        if f_valid is not None and not f_valid.closed:
            f_valid.close()
        if f_invalid is not None and not f_invalid.closed:
            f_invalid.close()

# ----------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------

# helper to replace stitched json with gzip
def gzip_node_file(ctype: str):
    """
    Compress NODES_DIR/<ctype>.json -> NODES_DIR/<ctype>.json.gz
    and delete the original .json file.

    If the .json file does not exist (e.g. already gzipped), do nothing.
    """
    src = NODES_DIR / f"{ctype}.json"
    if not src.exists():
        print(f"[GZIP] No raw JSON file for {ctype} to compress ({src} missing).")
        return

    dst = src.with_suffix(src.suffix + ".gz")  # .json.gz

    if dst.exists():
        print(f"[GZIP] Target already exists, not overwriting: {dst}")
        return

    print(f"[GZIP] Compressing {src.name} -> {dst.name}")
    with src.open("rb") as f_in, gzip.open(dst, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)

    size_orig = src.stat().st_size
    size_gz   = dst.stat().st_size
    print(f"[GZIP] {ctype}: {size_orig} -> {size_gz} bytes")

    src.unlink()
    print(f"[GZIP] Deleted original raw file: {src}")

def main():
    # CLI parsing:
    #   python extract_nodes.py          -> all types (with SKIP_COMPLETE)
    #   python extract_nodes.py KS       -> only KS from offset 0
    #   python extract_nodes.py KS 292000 -> only KS, resume at offset 292000
    #   python extract_nodes.py KS AS    -> KS and AS from offset 0
    raw_args = [arg.strip() for arg in sys.argv[1:] if arg.strip()]

    cli_types: list[str] = []
    resume_offset: int = 0

    if raw_args:
        # If the last arg is an integer, treat it as resume offset for a single type
        if len(raw_args) >= 2 and raw_args[-1].isdigit():
            resume_offset = int(raw_args[-1])
            cli_types = raw_args[:-1]
        else:
            cli_types = raw_args

    # If user requested multiple types AND a resume offset, that's ambiguous → error
    if resume_offset > 0 and len(cli_types) > 1:
        print(
            "[ERROR] Resume offset is only supported when a single node type "
            "is requested (e.g. `extract_nodes.py KS 292000`).",
            file=sys.stderr,
        )
        sys.exit(1)

    # If user explicitly requested node types, ignore .complete
    skip_complete = SKIP_COMPLETE and not cli_types

    if skip_complete and COMPLETE_FLAG.exists():
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

    # If user specified types on CLI, restrict to those
    if cli_types:
        # special-case "all" / "*" → ignore filtering and keep full reports list
        if not (len(cli_types) == 1 and cli_types[0].lower() in ("all", "*")):
            wanted = set(cli_types)
            available = set(reports)
            missing = sorted(wanted - available)
            if missing:
                print(
                    "[WARN] Some requested types are not in the allowed list: "
                    + ", ".join(missing),
                    file=sys.stderr,
                )
            reports = [t for t in reports if t in wanted]
            if not reports:
                print("[ERROR] None of the requested node types are available.", file=sys.stderr)
                sys.exit(1)

    print(f"[INFO] Will process {len(reports)} node types: {', '.join(reports) if reports else '(none)'}")

    failures = 0
    for i, ctype in enumerate(reports, start=1):
        print(f"\n=== Downloading raw nodes {ctype} ({i}/{len(reports)}) ===")
        # Only apply resume_offset if there is exactly one report and CLI provided an offset
        this_offset = resume_offset if (resume_offset > 0 and len(reports) == 1) else 0
        try:
            write_nodes_streaming(ctype, PAGE_SIZE, start_offset=this_offset)

            # If streaming finished successfully, optionally gzip the file
            if USE_GZIP_RAW_NODES:
                gzip_node_file(ctype)

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