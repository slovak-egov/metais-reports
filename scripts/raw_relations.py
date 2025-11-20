#!/usr/bin/env python3
import subprocess
import time
import sys
import os, re, requests, json
from pathlib import Path
from datetime import date

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
REL_SH = SCRIPT_DIR / "relation.sh"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
METAIS_DATE = os.getenv("METAIS_DATE", date.today().strftime("%d-%m-%Y"))

RAW_ROOT = PROJECT_ROOT / os.getenv("METAIS_RAW_ROOT", "output")
default_rels = RAW_ROOT / METAIS_DATE / "relations"

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

RELS_DIR = Path(
    os.getenv("RELATIONS_DIR") or
    os.getenv("METAIS_RELATIONS_DIR") or
    default_rels
)
RELS_DIR.mkdir(parents=True, exist_ok=True)

# Optional cache for the relationshiptypes/list metadata
REL_LIST_CACHE_DIR = Path(os.getenv("METAIS_REL_LIST_CACHE_DIR", "output/reltypes_meta"))
REL_LIST_CACHE_DIR.mkdir(parents=True, exist_ok=True)

SUBPROC_TIMEOUT = float(os.getenv("METAIS_SUBPROC_TIMEOUT", "180"))

INCLUDE_TYPES = get_include_types("INCLUDE_TYPES", "application,system,codelist")
VALID_FLAG = get_valid_flag("VALID_FLAG", "both")

INCLUDE_REGEX = os.getenv("METAIS_REL_INCLUDE_REGEX", "")
EXCLUDE_REGEX = os.getenv("METAIS_REL_EXCLUDE_REGEX", r"^(CMDB_|LATEST_REQUEST|PREVIOUS_REQUEST)$")

RAW_CMD = os.getenv(
    "METAIS_REL_CMD",
    f'"{REL_SH}" {{central}} {{outer}} {{relation}} --no-csv --outdir "{RELS_DIR}"'
)
MAX_RETRIES = int(os.getenv("METAIS_MAX_RETRIES", "10"))
RETRY_DELAY = float(os.getenv("METAIS_RETRY_DELAY", "0.25"))
TIMEOUT = float(os.getenv("METAIS_FETCH_TIMEOUT", "25"))

# -----------------------------------------------------------------------

def fetch_json(url: str):
    r = requests.get(url, timeout=TIMEOUT, headers={"Accept": "application/json"})
    r.raise_for_status()
    data = r.json()

    # Cache full payloads for metadata inspection
    try:
        if url == REL_TYPES_URL:
            # relationshiptypes/list -> metadata/DATE/relations/reltypes_list.json
            out_path = REL_LIST_CACHE_DIR / "reltypes_list.json"
            with out_path.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            print(f"[META] Cached relationship types list -> {out_path}")
        elif url == CITYPES_URL:
            # (Optional) also allow caching here if you want from this script
            pass
    except Exception as e:
        print(f"[WARN] Failed to cache metadata list from {url}: {e}")

    return data.get("results", [])

def build_node_set(citypes):
    nodes = set()
    for item in citypes:
        valid_bit = 0 if item.get("valid", False) else 1
        base_type = (item.get("type") or "").strip().lower()
        labels = [
            (lbl or "").strip().lower()
            for lbl in (item.get("labels") or [])
        ]

        tags = set(labels)
        if base_type:
            tags.add(base_type)

        if "all" not in INCLUDE_TYPES and not (tags & INCLUDE_TYPES):
            continue
        if not (VALID_FLAG & (1 << valid_bit)):
            continue

        name = item.get("technicalName") or item.get("name")
        if name:
            nodes.add(name)
    return nodes

def fetch_relation_metadata(relname: str):
    url = f"{REL_DETAIL_BASE}/{relname}"
    try:
        r = requests.get(url, headers={"Accept": "application/json"}, timeout=10)
        r.raise_for_status()
        data = r.json()
        sources = [s["technicalName"] for s in data.get("sources", []) if s.get("valid", True)]
        targets = [t["technicalName"] for t in data.get("targets", []) if t.get("valid", True)]
        return {
            "rel": relname,
            "outer": sources,
            "central": targets,
            "valid": data.get("valid", True),
            "type": data.get("type"),
        }
    except Exception as e:
        print(f"[WARN] could not fetch metadata for {relname}: {e}")
        return None

def build_rel_specs(reltypes):
    include_re = re.compile(INCLUDE_REGEX) if INCLUDE_REGEX else None
    exclude_re = re.compile(EXCLUDE_REGEX) if EXCLUDE_REGEX else None

    specs = []
    for item in reltypes:
        # Filter by type and validity

        valid_bit = 0 if item.get("valid", False) else 1
        base_type = (item.get("type") or "").strip().lower()
        labels = [
            (lbl or "").strip().lower()
            for lbl in (item.get("labels") or [])
        ]

        tags = set(labels)
        if base_type:
            tags.add(base_type)

        if "all" not in INCLUDE_TYPES and not (tags & INCLUDE_TYPES):
            continue
        if not (VALID_FLAG & (1 << valid_bit)):
            continue

        tech = item.get("technicalName")
        if not tech:
            continue
        if include_re and not include_re.search(tech):
            continue
        if exclude_re and exclude_re.search(tech):
            continue

        meta = fetch_relation_metadata(tech)
        if not meta or not meta["outer"] or not meta["central"]:
            continue

        outer   = meta["outer"][0]
        central = meta["central"][0]

        specs.append({
            "central": central,
            "outer": outer,
            "relation": tech,
        })

    specs.sort(key=lambda s: (s["central"], s["relation"], s["outer"]))
    return specs

def is_empty_table_file(path: str) -> bool:
    try:
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            return True
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return False
        if (data.get("type") or "").upper() != "TABLE":
            return False
        result = data.get("result", {})
        rows = result.get("rows")
        total = data.get("totalCount")
        if isinstance(rows, list) and not rows:
            return True
        if total == 0 and (rows is None or (isinstance(rows, list) and not rows)):
            return True
        return False
    except Exception:
        return False

def run_cmd_with_reason(cmd: str) -> tuple[bool, list[str], str, str]:
    """
    Run a shell command, return (ok, wrote_files, reason, stderr_text).

    reason:
      - "ok"                  -> success
      - "subprocess-timeout"  -> Python's SUBPROC_TIMEOUT hit
      - "http-<code>"         -> core.sh printed 'HTTP ERROR: <code>'
      - "exit-<code>"         -> generic non-zero exit status
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

def run_one(spec, idx, total):
    central, outer, relation = spec["central"], spec["outer"], spec["relation"]
    label = f"{relation}  ({outer} -> {central})"

    cmd = RAW_CMD.format(central=central, outer=outer, relation=relation)

    print(f"\n=== Generating relation {label}  ({idx}/{total}) ===")
    attempt = 1

    while attempt <= MAX_RETRIES:
        ok, _wrote, reason, stderr_text = run_cmd_with_reason(cmd)

        if ok:
            print(f"[OK] {label}")
            return True

        # 0) Shell complained that TOKEN is missing (run.sh)
        if "TOKEN env var is required" in stderr_text:
            if sys.stdin.isatty():
                print(
                    "\n[AUTH] The runner script reports that TOKEN is missing.\n"
                    "Please paste a valid MetaIS TOKEN.\n"
                    "Press Enter on an empty line to abort without changing anything."
                )
                try:
                    new_token = input("New TOKEN: ").strip()
                except EOFError:
                    new_token = ""

                if new_token:
                    os.environ["TOKEN"] = new_token
                    print("[INFO] TOKEN updated in this process; retrying the same relation…")
                    # Do NOT burn a retry attempt here – just re-run immediately
                    continue
                else:
                    print("[ERROR] No TOKEN provided; aborting this relation.")
                    return False
            else:
                print("[ERROR] TOKEN is missing and no interactive TTY is available; aborting.")
                return False

        # 1) HTTP auth-ish errors -> offer interactive TOKEN fix when possible
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
                print("[INFO] TOKEN updated in this process; retrying the same relation…")
                # Don't burn a retry attempt here – re-run immediately with fresh TOKEN
                continue
            else:
                print("[ERROR] No TOKEN provided; aborting this relation.")
                return False

        # 2) Everything else -> normal retry behavior
        print(
            f"[WARN] {label}: attempt {attempt}/{MAX_RETRIES} failed "
            f"({reason}). Retrying in {RETRY_DELAY}s..."
        )
        time.sleep(RETRY_DELAY)
        attempt += 1

    print(f"[ERROR] Giving up on {label} after {MAX_RETRIES} attempts.")
    return False

def group_by_central(specs):
    by_central = {}
    for s in specs:
        by_central.setdefault(s["central"], []).append(s)
    return by_central

def mark_relations_dir_complete(rels_dir: Path):
    """
    Drop a .complete marker into the relations output directory.
    This is used by metais_pipeline.py to decide whether to skip refetching.
    """
    flag = rels_dir / ".complete"
    try:
        with flag.open("w", encoding="utf-8") as f:
            f.write(f"completed at {datetime.now().isoformat()}\n")
        print(f"[INFO] Marked relation dump as complete: {flag}")
    except Exception as e:
        print(f"[WARN] Could not write .complete flag for relations: {e}")

def main():
    try:
        citypes = fetch_json(CITYPES_URL)
        node_set = build_node_set(citypes)
    except Exception as e:
        print(f"[ERROR] Failed to fetch or parse node types: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        reltypes = fetch_json(REL_TYPES_URL)
    except Exception as e:
        print(f"[ERROR] Failed to fetch relationship types: {e}", file=sys.stderr)
        sys.exit(1)

    specs = build_rel_specs(reltypes)
    if not specs:
        print("[ERROR] No usable relationship types found.", file=sys.stderr)
        sys.exit(1)

    by_central = group_by_central(specs)

    arg_central = sys.argv[1].strip() if len(sys.argv) >= 2 else None
    if arg_central and arg_central.lower() not in ("", "all", "*"):
        if arg_central not in by_central:
            avail = ", ".join(sorted(by_central.keys()))
            print(f"[ERROR] No relations for central '{arg_central}'. Available: {avail}", file=sys.stderr)
            sys.exit(1)
        specs = by_central[arg_central]

    total = len(specs)
    print(f"[INFO] Will process {total} relations" +
          (f" for central '{arg_central}'" if arg_central else "") + ".")

    failures = 0
    for i, spec in enumerate(specs, start=1):
        if not run_one(spec, i, total):
            failures += 1

    print(f"\n[INFO] Completed: {total - failures} ok / {failures} failed.")

    if total > 0 and failures == 0:
        mark_relations_dir_complete(RELS_DIR)

if __name__ == "__main__":
    main()