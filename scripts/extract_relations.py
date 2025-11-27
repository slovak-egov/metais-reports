#!/usr/bin/env python3
import os
import sys
import json
import time
import subprocess
from pathlib import Path
from datetime import date, datetime
import gzip
import requests
import re

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
REL_SH       = SCRIPT_DIR / "relation.sh"

METAIS_DATE = os.getenv("METAIS_DATE", date.today().strftime("%d-%m-%Y"))
RAW_ROOT    = PROJECT_ROOT / os.getenv("METAIS_RAW_ROOT", "output")
DATE_ROOT   = RAW_ROOT / METAIS_DATE

RELS_DIR        = DATE_ROOT / "relations"
METADATA_ROOT   = DATE_ROOT / "metadata"
META_REL_DIR    = METADATA_ROOT / "relations"

RELS_DIR.mkdir(parents=True, exist_ok=True)
METADATA_ROOT.mkdir(parents=True, exist_ok=True)
META_REL_DIR.mkdir(parents=True, exist_ok=True)

COMPLETE_FLAG   = RELS_DIR / ".complete"
SKIP_COMPLETE   = bool(os.getenv("SKIP_COMPLETE", "False"))

CITYPES_URL = os.getenv(
    "CITYPES_URL",
    "https://metais.slovensko.sk/api/types-repo/citypes/list"
)
REL_TYPES_URL = os.getenv(
    "RELTYPES_URL",
    "https://metais.slovensko.sk/api/types-repo/relationshiptypes/list"
)
REL_DETAIL_BASE = os.getenv(
    "RELTYPES_DETAIL_URL",
    "https://metais.slovensko.sk/api/types-repo/relationshiptypes/relationshiptype"
)

PAGE_SIZE        = int(os.getenv("REL_PAGE_SIZE", "3000"))
CONN_MAX_RETRIES = int(os.getenv("CONNECTION_MAX_RETRIES", "5"))
CONN_RETRY_DELAY = float(os.getenv("CONNECTION_RETRY_DELAY", "0.5"))
FETCH_TIMEOUT    = float(os.getenv("METAIS_FETCH_TIMEOUT", "60"))
SUBPROC_TIMEOUT  = float(os.getenv("METAIS_SUBPROC_TIMEOUT", "180"))

INCLUDE_TYPES   = get_include_types("INCLUDE_TYPES", "application,system,codelist")
VALID_FLAG_MASK = get_valid_flag("VALID_FLAG", "both")

INCLUDE_REGEX = os.getenv("METAIS_REL_INCLUDE_REGEX", "")
EXCLUDE_REGEX = os.getenv("METAIS_REL_EXCLUDE_REGEX", "")

include_re = re.compile(INCLUDE_REGEX) if INCLUDE_REGEX else None
exclude_re = re.compile(EXCLUDE_REGEX) if EXCLUDE_REGEX else None

USE_GZIP_RAW = os.getenv("METAIS_GZIP_RAW", "False").lower() in ("1", "true", "yes")

# Groovy template for relations
# SOURCE ---RELATION---> TARGET, LIMIT, OFFSET
REL_TEMPLATE = os.getenv(
    "METAIS_REL_TEMPLATE",
    "groovy/template/relation_template.groovy"
)

RAW_CMD_TEMPLATE = os.getenv(
    "METAIS_REL_CMD",
    '"%s" {target} {source} {relation} '
    '--template %s --limit {limit} --offset {offset} --outdir "{outdir}" --no-csv' % (
        REL_SH,
        REL_TEMPLATE,
    ),
)

MAX_RETRIES = int(os.getenv("METAIS_MAX_RETRIES", "10"))
RETRY_DELAY = float(os.getenv("METAIS_RETRY_DELAY", "0.25"))

# ----------------------------------------------------------------------
# HELPERS
# ----------------------------------------------------------------------


def fetch_json_with_retries(url: str) -> dict | list:
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
    Run relation.sh and parse:

      ok, wrote_files, reason, stderr_text

    reason:
      - "ok"
      - "subprocess-timeout"
      - "token-missing"
      - "http-<code>"  (parsed from stderr lines like 'HTTP ERROR: 401 ...')
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

        # 1) explicit message from run.sh
        if "TOKEN env var is required" in stderr_text:
            return False, wrote_files, "token-missing", stderr_text

        # 2) HTTP ERROR: <code> from core.sh
        m = re.search(r"HTTP ERROR:\s+(\d+)", stderr_text)
        if m:
            code = m.group(1)
            return False, wrote_files, f"http-{code}", stderr_text

        # 3) generic non-zero exit
        return False, wrote_files, f"exit-{e.returncode}", stderr_text


def load_json(path: str | Path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_raw_rel_json(path: Path, items: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"type": "RAW_REL", "result": items}

    if USE_GZIP_RAW:
        gz_path = path.with_suffix(path.suffix + ".gz")  # e.g. .json.gz
        with gzip.open(gz_path, "wt", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        print(f"Wrote: {gz_path} ({gz_path.stat().st_size} bytes)")
    else:
        with path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        print(f"Wrote: {path} ({path.stat().st_size} bytes)")


# ----------------------------------------------------------------------
# METADATA: CITYPES + RELATIONTYPES
# ----------------------------------------------------------------------


def fetch_citype_list() -> list[dict]:
    """We may use this later (e.g. to restrict by node types); for now, just fetch."""
    print(f"[META] Fetching citype list from {CITYPES_URL}")
    data = fetch_json_with_retries(CITYPES_URL)
    if isinstance(data, dict) and "results" in data:
        return data["results"]
    return data


def fetch_reltypes_list_and_details() -> dict[str, dict]:
    """
    Fetch relationshiptypes/list and then fetch details for each relation type.

    - output/DATE/metadata/reltypes_list.json
    - output/DATE/metadata/relations/<rel>.json

    Returns: dict mapping technicalName -> full detail metadata.
    """
    print(f"[META] Fetching relationship types list from {REL_TYPES_URL}")
    data = fetch_json_with_retries(REL_TYPES_URL)

    if isinstance(data, dict) and "results" in data:
        rel_list = data["results"]
    else:
        rel_list = data

    list_path = METADATA_ROOT / "reltypes_list.json"
    with list_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"[META] Cached relationship types list -> {list_path}")

    rel_meta: dict[str, dict] = {}

    for item in rel_list:
        tech = item.get("technicalName")
        if not tech:
            continue

        url = f"{REL_DETAIL_BASE}/{tech}"
        try:
            detail = fetch_json_with_retries(url)
        except Exception as e:
            print(f"[WARN] Could not fetch reltype detail for {tech}: {e}")
            continue

        rel_meta[tech] = detail

        out_path = META_REL_DIR / f"{tech}.json"
        with out_path.open("w", encoding="utf-8") as f:
            json.dump(detail, f, ensure_ascii=False, indent=2)
        print(f"[META] Cached reltype detail {tech} -> {out_path}")

    return rel_meta

def endpoint_allowed(ep_valid: bool) -> bool:
    bit = 0 if ep_valid else 1
    return bool(VALID_FLAG_MASK & (1 << bit))

def build_rel_specs(rel_meta: dict[str, dict]) -> list[dict]:
    specs  : list[dict] = []
    broken : list[dict] = []

    for tech, meta in rel_meta.items():
        if not tech:
            continue

        # filter by technicalName regex
        if include_re and not include_re.search(tech):
            continue
        if exclude_re and exclude_re.search(tech):
            continue

        # filter by type/labels/valid
        item_type = (meta.get("type") or "").strip().lower()
        labels = [(lbl or "").strip().lower() for lbl in (meta.get("labels") or [])]
        valid_bit = 0 if meta.get("valid", True) else 1

        tags = set(labels)
        if item_type:
            tags.add(item_type)

        if "all" not in INCLUDE_TYPES and not (tags & INCLUDE_TYPES):
            continue
        if not (VALID_FLAG_MASK & (1 << valid_bit)):
            continue

        sources_all = meta.get("sources", []) or []
        targets_all = meta.get("targets", []) or []

        sources = [s["technicalName"] for s in sources_all if endpoint_allowed(s.get("valid", True))]
        targets = [t["technicalName"] for t in targets_all if endpoint_allowed(t.get("valid", True))]

        if not sources or not targets:
            broken.append({
                "relation": tech,
                "type": meta.get("type"),
                "valid": meta.get("valid"),
                "n_sources": len(sources_all),
                "n_targets": len(targets_all),
            })
            continue

        source = sources[0]
        target = targets[0]

        specs.append({
            "source": sources[0],
            "target": targets[0],
            "relation": tech,
        })
    broken_path = METADATA_ROOT / "broken_reltypes.json"
    with broken_path.open("w", encoding="utf-8") as f:
        json.dump(broken, f, ensure_ascii=False, indent=2)
    print(f"[META] Recorded {len(broken)} broken/incomplete reltypes -> {broken_path}")

    specs.sort(key=lambda s: (s["target"], s["relation"], s["source"]))
    return specs


def group_by_target(specs: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for s in specs:
        out.setdefault(s["target"], []).append(s)
    return out


# ----------------------------------------------------------------------
# PAGINATED RELATION FETCH
# ----------------------------------------------------------------------


def fetch_relation_page(source: str, target: str, relation: str,
                        limit: int, offset: int) -> list[dict]:
    page_dir = DATE_ROOT / "relations_parts" / relation
    page_dir.mkdir(parents=True, exist_ok=True)

    cmd = RAW_CMD_TEMPLATE.format(
        target=target,
        source=source,
        relation=relation,
        limit=limit,
        offset=offset,
        outdir=str(page_dir),
    )

    while True:
        print(f"[PAGE] {relation} ({source}->{target}): limit={limit}, offset={offset}")
        ok, wrote, reason, stderr_text = run_cmd(cmd)

        # success
        if ok and wrote:
            break

        # token problems → interactive fix if possible
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
                # loop again with the new TOKEN
                continue
            else:
                raise RuntimeError(
                    f"TOKEN required but not provided for {relation} "
                    f"({source}->{target}) offset={offset}."
                )

        # non-auth error or non-interactive → fail this page
        raise RuntimeError(
            f"Page fetch failed for {relation} ({source}->{target}) "
            f"offset={offset}, limit={limit}: {reason}"
        )

    # we have a successful run with at least one written file
    last_path = wrote[-1]
    part = load_json(last_path)

    # template can return bare list or {"result":[...]}
    if isinstance(part, dict) and "result" in part:
        return part["result"]
    if isinstance(part, list):
        return part

    raise ValueError(f"Unexpected page output shape for {relation} offset={offset}")


def fetch_all_relations_for_spec(spec: dict, limit: int) -> list[dict]:
    source   = spec["source"]
    target   = spec["target"]
    relation = spec["relation"]

    all_edges: list[dict] = []
    page_index = 0

    while True:
        offset = page_index * limit
        items = fetch_relation_page(source, target, relation, limit, offset)
        if not items:
            print(f"[PAGE] {relation} ({source}->{target}): empty page at offset={offset}; done.")
            break
        all_edges.extend(items)
        page_index += 1

    print(f"[OK] {relation} ({source}->{target}): fetched {len(all_edges)} edges total.")
    return all_edges


def run_one_spec(spec: dict, idx: int, total: int) -> bool:
    label = f"{spec['relation']} ({spec['source']} -> {spec['target']})"
    print(f"\n=== Generating relation {label} ({idx}/{total}) ===")

    attempt = 1
    while attempt <= MAX_RETRIES:
        try:
            edges = fetch_all_relations_for_spec(spec, PAGE_SIZE)
            out_path = RELS_DIR / f"{spec['relation']}.json"
            write_raw_rel_json(out_path, edges)
            print(f"[OK] {label}")
            return True
        except RuntimeError as e:
            # Most likely a subprocess issue; we can retry a few times.
            print(f"[WARN] {label}: attempt {attempt}/{MAX_RETRIES} failed: {e}")
            time.sleep(RETRY_DELAY)
            attempt += 1
        except Exception as e:
            # More serious issue (JSON shape, etc.) – don't hammer retries forever.
            print(f"[ERROR] {label}: unrecoverable error: {e}")
            return False

    print(f"[ERROR] Giving up on {label} after {MAX_RETRIES} attempts.")
    return False


def mark_relations_complete():
    try:
        with COMPLETE_FLAG.open("w", encoding="utf-8") as f:
            f.write(f"completed at {datetime.now().isoformat()}\n")
        print(f"[INFO] Marked relation dump as complete: {COMPLETE_FLAG}")
    except Exception as e:
        print(f"[WARN] Could not write .complete flag for relations: {e}")


# ----------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------


def main():
    if SKIP_COMPLETE and COMPLETE_FLAG.exists():
        print(f"[INFO] Relations already marked complete ({COMPLETE_FLAG}), nothing to do.")
        return

    # You *can* use citype list here for extra filtering if you wish.
    try:
        _citypes = fetch_citype_list()
    except Exception as e:
        print(f"[WARN] Could not fetch citype list: {e}")

    try:
        rel_meta = fetch_reltypes_list_and_details()
    except Exception as e:
        print(f"[ERROR] Failed to fetch relationship type metadata: {e}", file=sys.stderr)
        sys.exit(1)

    specs = build_rel_specs(rel_meta)
    if not specs:
        print("[ERROR] No usable relationship specs found.", file=sys.stderr)
        sys.exit(1)

    by_target = group_by_target(specs)

    arg_target = sys.argv[1].strip() if len(sys.argv) >= 2 else None
    if arg_target and arg_target.lower() not in ("", "all", "*"):
        if arg_target not in by_target:
            avail = ", ".join(sorted(by_target.keys()))
            print(f"[ERROR] No relations for target '{arg_target}'. Available: {avail}", file=sys.stderr)
            sys.exit(1)
        specs = by_target[arg_target]

    total = len(specs)
    print(f"[INFO] Will process {total} relations" +
        (f" for target '{arg_target}'" if arg_target else "") + ".")

    failures = 0
    for i, spec in enumerate(specs, start=1):
        if not run_one_spec(spec, i, total):
            failures += 1

    print(f"\n[INFO] Completed relations: {total - failures} ok / {failures} failed.")
    if total > 0 and failures == 0:
        mark_relations_complete()


if __name__ == "__main__":
    main()