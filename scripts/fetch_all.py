#!/usr/bin/env python3
"""
fetch_all.py

One-shot "fetch + pack" script that:

  - Ensures bearer token (env TOKEN or interactive prompt).
  - Fetches metadata (citypes, reltypes, enums) into output/DATE/metadata.
  - Streams nodes via raw.sh into packed/nodes + dict + uuid_index.
  - Streams relations via raw.sh into packed/relations.
"""

from __future__ import annotations

import os
import sys
import json
import time
import struct
import subprocess
from pathlib import Path
from datetime import date, datetime
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple, Set

import requests

from config_env import (
    load_env_file,
    find_project_root
)

# ----------------------------------------------------------------------
# CONSTANTS
# ----------------------------------------------------------------------

from bin_formats import (
    UUID_BYTES,
    ATTR_INDEX_BYTES,
    DICT_INDEX_BYTES,
    ROW_OFFSET_BYTES,
    REL_INT_BYTES,
    REL_PAIR_BYTES,
    INT32_LE,
    U16_LE,
    U64_LE,
    MISSING_SENTINEL
)

ALLOWED_META_ATTRS = [
    "owner",
    "state",
    "createdBy",
    "createdAt",
    "lastModifiedBy",
    "lastModifiedAt"
]

# ----------------------------------------------------------------------
# ENV / BASIC PATHS
# ----------------------------------------------------------------------

load_env_file()

SCRIPT_DIR   = Path(__file__).resolve().parent
PROJECT_ROOT = find_project_root(SCRIPT_DIR)

# If date is passed on CLI, we respect it; otherwise METAIS_DATE env or today
def resolve_dump_date(cli_arg: Optional[str]) -> str:
    if cli_arg:
        return cli_arg
    env_date = os.getenv("METAIS_DATE")
    if env_date:
        return env_date
    return date.today().strftime("%d-%m-%Y")


METAIS_DATE = resolve_dump_date(sys.argv[1] if len(sys.argv) > 1 else None)

RAW_ROOT  = PROJECT_ROOT / os.getenv("METAIS_RAW_ROOT", "output")
DATE_ROOT = RAW_ROOT / METAIS_DATE

METADATA_ROOT   = DATE_ROOT / "metadata"
ENUMS_DIR       = METADATA_ROOT / "enums"
NODES_META_DIR  = METADATA_ROOT / "nodes"
RELS_META_DIR   = METADATA_ROOT / "relations"

PACKED_ROOT     = DATE_ROOT / "packed"
DICT_DIR        = PACKED_ROOT / "dict"
NODES_PACKED    = PACKED_ROOT / "nodes"
UUID_INDEX_DIR  = PACKED_ROOT / "uuid_index"
UUID_TYPES_DIR  = PACKED_ROOT / "uuid_types"
RELS_PACKED     = PACKED_ROOT / "relations"
TMP_DIR         = DATE_ROOT / "tmp"

for d in [METADATA_ROOT, ENUMS_DIR, NODES_META_DIR, RELS_META_DIR,
          PACKED_ROOT, DICT_DIR, NODES_PACKED, UUID_INDEX_DIR,
          UUID_TYPES_DIR, RELS_PACKED, TMP_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# Paging + timeouts
NODE_PAGE_SIZE = int(os.getenv("NODE_PAGE_SIZE", "1000"))
REL_PAGE_SIZE  = int(os.getenv("REL_PAGE_SIZE", "5000"))

FETCH_TIMEOUT        = float(os.getenv("METAIS_FETCH_TIMEOUT", "60"))
CONN_MAX_RETRIES     = int(os.getenv("CONNECTION_MAX_RETRIES", "5"))
CONN_RETRY_DELAY_SEC = float(os.getenv("CONNECTION_RETRY_DELAY", "0.5"))

# Subprocess timeout (raw.sh / relation.sh)
SUBPROC_TIMEOUT = float(os.getenv("METAIS_SUBPROC_TIMEOUT", "180"))

# Retry settings for node pages (broken pipe, curl exit 56, etc.)
NODE_PAGE_MAX_RETRIES = int(os.getenv("NODE_PAGE_MAX_RETRIES", "10"))
NODE_PAGE_RETRY_DELAY = float(os.getenv("NODE_PAGE_RETRY_DELAY", "1.0"))

# Retry settings for relation pages
REL_PAGE_MAX_RETRIES = int(os.getenv("REL_PAGE_MAX_RETRIES", "10"))
REL_PAGE_RETRY_DELAY = float(os.getenv("REL_PAGE_RETRY_DELAY", "1.0"))

# Whether to include INVALIDATED stuff or only valid ones
INCLUDE_INVALID = os.getenv("INCLUDE_INVALID", "true").strip().lower() in (
    "1", "true", "yes", "y", "on", "all"
)

# Node templates: "all" vs "valid-only" (matches extract_nodes.py)
NODE_TEMPLATE_ALL = os.getenv(
    "METAIS_NODE_TEMPLATE_ALL",
    "groovy/template/node_template_all.groovy",
)
NODE_TEMPLATE_VALID_ONLY = os.getenv(
    "METAIS_NODE_TEMPLATE_VALID_ONLY",
    "groovy/template/node_template_valid_only.groovy",
)

# Relation templates: "all" vs "valid-only" (matches fetch_relations.py)
REL_TEMPLATE_ALL = os.getenv(
    "METAIS_REL_TEMPLATE_ALL",
    "groovy/template/relation_template_all.groovy",
)
REL_TEMPLATE_VALID_ONLY = os.getenv(
    "METAIS_REL_TEMPLATE_VALID_ONLY",
    "groovy/template/relation_template_valid_only.groovy",
)

ENTITY_SH = SCRIPT_DIR / os.getenv("METAIS_ENTITY_SH", "raw.sh")
REL_SH = SCRIPT_DIR / os.getenv("METAIS_REL_SH", "relation.sh")

# ----------------------------------------------------------------------
# HELPERS: TOKEN / HTTP / RAW.SH
# ----------------------------------------------------------------------

def get_bearer_token() -> str:
    """
    Get bearer token from env TOKEN/METAIS_TOKEN or prompt the user.
    """
    token = os.getenv("TOKEN") or os.getenv("METAIS_TOKEN")
    if token:
        return token.strip()

    print("[AUTH] No TOKEN/METAIS_TOKEN in environment.")
    token = input("Enter MetaIS bearer token (will not be saved anywhere): ").strip()
    if not token:
        print("[ERROR] Bearer token is required.", file=sys.stderr)
        sys.exit(1)
    os.environ["TOKEN"] = token
    return token


def fetch_json_with_retries(url: str, headers: Optional[Dict[str, str]] = None) -> Any:
    """
    Simple GET with retry loop (for metadata endpoints).
    """
    attempt = 1
    while CONN_MAX_RETRIES <= 0 or attempt <= CONN_MAX_RETRIES:
        try:
            r = requests.get(
                url,
                timeout=FETCH_TIMEOUT,
                headers=headers or {"Accept": "application/json"},
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if CONN_MAX_RETRIES > 0 and attempt >= CONN_MAX_RETRIES:
                print(f"[ERROR] fetch_json_with_retries({url}) failed: {e}", file=sys.stderr)
                raise
            print(
                f"[WARN] fetch_json_with_retries({url}) failed on attempt "
                f"{attempt}/{CONN_MAX_RETRIES}: {e}; retrying in {CONN_RETRY_DELAY_SEC}s..."
            )
            time.sleep(CONN_RETRY_DELAY_SEC)
            attempt += 1


# ----------------------------------------------------------------------
# METADATA FETCH: ENUMS, CITYPES, RELTYPES
# ----------------------------------------------------------------------

def fetch_enums_all() -> None:
    """
    Fetch enums list + per-enum details + merged enums_merged.json.
    Ported from your fetch_enums.py and slightly condensed.
    """
    ENUMS_LIST_URL = os.getenv(
        "METAIS_ENUMS_LIST_URL",
        "https://metais.slovensko.sk/api/enums-repo/enums/list",
    )
    ENUM_DETAIL_BASE = os.getenv(
        "METAIS_ENUM_DETAIL_URL",
        "https://metais.slovensko.sk/api/enums-repo/enums/enum/valid",
    )

    print(f"[META] Fetching enums list from {ENUMS_LIST_URL}")
    data = fetch_json_with_retries(ENUMS_LIST_URL)

    list_path = METADATA_ROOT / "enums_list.json"
    with list_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"[META] Cached enums list -> {list_path}")

    if isinstance(data, dict) and "results" in data:
        enum_items = data["results"]
    else:
        enum_items = data

    all_codes = [e.get("code") for e in enum_items if e.get("code")]
    all_codes = sorted(set(all_codes))
    print(f"[META] Will fetch {len(all_codes)} enums.")

    ok = fail = 0
    for code in all_codes:
        url = f"{ENUM_DETAIL_BASE}/{code}"
        print(f"[ENUM] Fetching {code} from {url}")
        try:
            detail = fetch_json_with_retries(url)
        except Exception as e:
            print(f"[WARN] Could not fetch enum detail for {code}: {e}")
            fail += 1
            continue

        out_path = ENUMS_DIR / f"{code}.json"
        with out_path.open("w", encoding="utf-8") as f:
            json.dump(detail, f, ensure_ascii=False, indent=2)
        print(f"[ENUM] Cached enum {code} -> {out_path}")
        ok += 1

    print(f"[META] Enums fetch: {ok} ok / {fail} failed")

    # Merge
    merged: Dict[str, str] = {}
    collisions: List[Dict] = []

    for code in all_codes:
        enum_path = ENUMS_DIR / f"{code}.json"
        if not enum_path.exists():
            continue
        try:
            with enum_path.open("r", encoding="utf-8") as f:
                enum_data = json.load(f)
        except Exception as e:
            print(f"[WARN] Could not load enum {code} from {enum_path}: {e}")
            continue

        items = enum_data.get("enumItems") or []
        for item in items:
            item_code = item.get("code")
            if not item_code:
                continue

            val = (
                item.get("value") or
                item.get("engValue") or
                item.get("description") or
                item_code
            )

            if item_code in merged and merged[item_code] != val:
                collisions.append({
                    "enum": code,
                    "item_code": item_code,
                    "old_value": merged[item_code],
                    "new_value": val,
                })
                print(
                    f"[WARN] Enum code collision for {item_code}: "
                    f"'{merged[item_code]}' vs '{val}'"
                )
                continue

            merged[item_code] = val

    merged_path = METADATA_ROOT / "enums_merged.json"
    with merged_path.open("w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    print(f"[META] Wrote merged enums -> {merged_path} ({len(merged)} keys)")

    if collisions:
        collisions_path = METADATA_ROOT / "enums_collisions.json"
        with collisions_path.open("w", encoding="utf-8") as f:
            json.dump(collisions, f, ensure_ascii=False, indent=2)
        print(f"[META] Enum collisions -> {collisions_path}")


def fetch_citype_metadata_all() -> List[str]:
    """
    Fetch citype list + per-citype detail into metadata/nodes.

    Env:
      CITYPES_URL
      CITYPES_DETAIL_URL
    Return list of citype technicalNames.
    """
    CITYPES_LIST_URL        = os.getenv(
        "CITYPES_URL",
        "https://metais.slovensko.sk/api/types-repo/citypes/list"
    )
    CITYPES_DETAIL_URL = os.getenv(
        "CITYPES_DETAIL_URL",
        "https://metais.slovensko.sk/api/types-repo/citypes/citype"
    )

    if not CITYPES_LIST_URL or not CITYPES_DETAIL_URL:
        print("[META] CITYPES_URL / CITYPES_DETAIL_URL not set; "
              "skipping citype metadata fetch.", file=sys.stderr)
        return []

    print(f"[META] Fetching citype list from {CITYPES_LIST_URL}")
    data = fetch_json_with_retries(CITYPES_LIST_URL)
    list_path = METADATA_ROOT / "citypes_list.json"
    with list_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"[META] Cached citypes list -> {list_path}")

    # Try to be generic about shape
    if isinstance(data, dict) and "results" in data:
        items = data["results"]
    else:
        items = data

    citypes: List[str] = []
    for item in items:
        code = item.get("technicalName") or item.get("name") or item.get("code")
        if code:
            citypes.append(code)

    citypes = sorted(set(citypes))
    print(f"[META] Will fetch metadata for {len(citypes)} citypes.")

    for code in citypes:
        url = f"{CITYPES_DETAIL_URL}/{code}"
        print(f"[META] Fetching citype {code} from {url}")
        detail = fetch_json_with_retries(url)
        out_path = NODES_META_DIR / f"{code}.json"
        with out_path.open("w", encoding="utf-8") as f:
            json.dump(detail, f, ensure_ascii=False, indent=2)
        print(f"[META] Cached citype {code} -> {out_path}")

    return citypes


def fetch_reltype_metadata_all() -> List[str]:
    """
    Fetch relation type list + per-reltype detail into metadata/relations.

    Env:
      RELTYPES_URL
      RELTYPES_DETAIL_URL
    """
    RELTYPES_LIST_URL = os.getenv(
        "RELTYPES_URL",
        "https://metais.slovensko.sk/api/types-repo/relationshiptypes/list"
    )
    RELTYPES_DETAIL_URL = os.getenv(
        "RELTYPES_DETAIL_URL",
        "https://metais.slovensko.sk/api/types-repo/relationshiptypes/relationshiptype"
    )

    if not RELTYPES_LIST_URL or not RELTYPES_DETAIL_URL:
        print("[META] RELTYPES_URL / RELTYPES_DETAIL_URL not set; "
              "skipping reltype metadata fetch.", file=sys.stderr)
        return []

    print(f"[META] Fetching reltype list from {RELTYPES_LIST_URL}")
    data = fetch_json_with_retries(RELTYPES_LIST_URL)
    list_path = METADATA_ROOT / "reltypes_list.json"
    with list_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"[META] Cached reltypes list -> {list_path}")

    if isinstance(data, dict) and "results" in data:
        items = data["results"]
    else:
        items = data

    reltypes: List[str] = []
    for item in items:
        code = item.get("technicalName") or item.get("name") or item.get("code")
        if code:
            reltypes.append(code)

    reltypes = sorted(set(reltypes))
    print(f"[META] Will fetch metadata for {len(reltypes)} reltypes.")

    for code in reltypes:
        url = f"{RELTYPES_DETAIL_URL}/{code}"
        print(f"[META] Fetching reltype {code} from {url}")
        detail = fetch_json_with_retries(url)
        out_path = RELS_META_DIR / f"{code}.json"
        with out_path.open("w", encoding="utf-8") as f:
            json.dump(detail, f, ensure_ascii=False, indent=2)
        print(f"[META] Cached reltype {code} -> {out_path}")

    return reltypes

# ----------------------------------------------------------------------
# RAW RUNNER HELPERS
# ----------------------------------------------------------------------

def parse_runner_output(stdout: str) -> list[Path]:
    """
    Parse output lines from raw.sh / relation.sh, returning the written file paths.

    Supports both:
      - "[OUT] /path/to/file.json"   (old TEMPLATE/OUT mode)
      - "Wrote: /path/to/file.json"  (current CLI mode used by extract_nodes/fetch_relations)
    """
    paths: list[Path] = []

    for line in stdout.splitlines():
        line = line.strip()

        # Old-style "[OUT] /path"
        if line.startswith("[OUT]"):
            parts = line.split(None, 1)
            if len(parts) == 2:
                paths.append(Path(parts[1]))
            continue

        # New-style "Wrote: /path (123 bytes)" like in extract_nodes.py
        if line.startswith("Wrote:"):
            # same parsing you already use elsewhere
            path = line.split("Wrote:", 1)[1].strip().split()[0].strip("()")
            if path:
                paths.append(Path(path))

    return paths


# ----------------------------------------------------------------------
# Fetching nodes
# ----------------------------------------------------------------------

def fetch_node_page(ctype: str, limit: int, offset: int) -> list[dict]:
    """
    Fetch a single page of nodes for citype `ctype` using raw.sh.

    - Uses METAIS_NODE_TEMPLATE_ALL / METAIS_NODE_TEMPLATE_VALID_ONLY
      based on INCLUDE_INVALID.
    - Retries on non-auth subprocess failures (curl broken pipe etc.).
    - If TOKEN is missing/expired (heuristic: stderr has token/401/403/HTTP ERROR),
      and we're in a TTY, ask for a new TOKEN and retry this page.
    """
    page_dir = DATE_ROOT / "nodes_parts" / ctype
    page_dir.mkdir(parents=True, exist_ok=True)

    print(f"[PAGE] {ctype}: limit={limit}, offset={offset}")

    template = NODE_TEMPLATE_ALL if INCLUDE_INVALID else NODE_TEMPLATE_VALID_ONLY

    cmd = [
        "bash",
        str(ENTITY_SH),
        ctype,
        "--template", template,
        "--limit", str(limit),
        "--offset", str(offset),
        "--no-csv",
        "--outdir", str(page_dir),
    ]

    # We only count retries for non-auth errors; auth errors can be fixed interactively.
    attempt = 1

    while True:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=os.environ.copy(),
            timeout=SUBPROC_TIMEOUT,
        )

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""

        if proc.returncode == 0:
            out_paths = parse_runner_output(stdout)
            if not out_paths:
                raise RuntimeError(
                    f"raw.sh succeeded for {ctype} offset={offset}, "
                    f"but no [OUT] paths were found in stdout.\n"
                    f"stdout:\n{stdout}\n\nstderr:\n{stderr}"
                )
            last_path = out_paths[-1]
            if not last_path.is_file():
                raise FileNotFoundError(
                    f"raw.sh reported [OUT] {last_path}, but the file does not exist."
                )

            with last_path.open("r", encoding="utf-8") as f:
                part = json.load(f)

            if isinstance(part, dict) and "result" in part:
                return part["result"]
            if isinstance(part, list):
                return part

            raise ValueError(
                f"Unexpected JSON shape for {ctype} at offset={offset}: "
                f"top-level type={type(part)}"
            )

        # Non-zero return code → maybe token issue?
        lower_err = (stderr or "").lower()
        stderr_text = stderr or ""

        token_problem = (
            "token env var is required" in lower_err
            or "token" in lower_err
            or "http error:" in lower_err and ("401" in lower_err or "403" in lower_err)
            or " 401 " in lower_err
            or " 403 " in lower_err
        )

        if token_problem and sys.stdin.isatty():
            print(
                "\n[AUTH] raw.sh/run.sh reported an authorization problem "
                f"(returncode={proc.returncode}).\n"
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
                # Do NOT increment attempt here; this is auth, not transport.
                continue
            else:
                raise RuntimeError(
                    f"TOKEN required but not provided for {ctype} (offset={offset}).\n"
                    f"stderr from raw.sh/run.sh:\n{stderr_text}"
                )

        # Not a token problem → retry a few times for transient errors (broken pipe etc.)
        if NODE_PAGE_MAX_RETRIES > 0 and attempt >= NODE_PAGE_MAX_RETRIES:
            raise RuntimeError(
                f"Page fetch failed for {ctype} offset={offset} limit={limit} "
                f"after {attempt} attempts (returncode={proc.returncode}).\n"
                f"stdout:\n{stdout}\n\nstderr:\n{stderr_text}"
            )

        print(
            f"[WARN] Page fetch failed for {ctype} offset={offset} "
            f"(attempt {attempt}/{NODE_PAGE_MAX_RETRIES}, returncode={proc.returncode}).\n"
            f"stderr (truncated): {stderr_text[:400]}\n"
            f"Retrying in {NODE_PAGE_RETRY_DELAY}s..."
        )
        attempt += 1
        time.sleep(NODE_PAGE_RETRY_DELAY)

# ----------------------------------------------------------------------
# METADATA: ATTRIBUTE MAP
# ----------------------------------------------------------------------

def load_node_attribute_metadata(
    type_name: str,
) -> Tuple[Dict[str, Tuple[Optional[str], Optional[str]]], List[str]]:
    """
    Load attribute metadata for citype <type_name> from metadata/nodes/<type_name>.json.

    Returns:
      (attr_meta_map, attr_order)

    where:
      attr_meta_map[technicalName] = (humanName, description)
      attr_order = list of technicalName in the order they appear in
                   attributes + attributeProfiles[*].attributes.
    """
    meta_file = NODES_META_DIR / f"{type_name}.json"
    if not meta_file.is_file():
        # Not fatal, but then we truly don't know the schema. In this case
        # we *could* fall back to discovery, but you explicitly DON'T want that.
        raise FileNotFoundError(
            f"Missing node metadata for {type_name} at {meta_file}"
        )

    with meta_file.open("r", encoding="utf-8") as f:
        raw = json.load(f)

    mapping: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
    attr_order: List[str] = []
    seen: set[str] = set()

    # 1) Top-level "attributes"
    for attr in raw.get("attributes", []):
        tech = attr.get("technicalName")
        if not tech:
            continue
        name = attr.get("name")
        desc = attr.get("description")
        mapping[tech] = (name, desc)
        if tech not in seen:
            seen.add(tech)
            attr_order.append(tech)

    # 2) Attributes in profiles
    for prof in raw.get("attributeProfiles", []):
        for attr in prof.get("attributes", []):
            tech = attr.get("technicalName")
            if not tech:
                continue
            if tech not in mapping:
                name = attr.get("name")
                desc = attr.get("description")
                mapping[tech] = (name, desc)
            if tech not in seen:
                seen.add(tech)
                attr_order.append(tech)

    return mapping, attr_order


# ----------------------------------------------------------------------
# GLOBAL VALUE DICT HELPERS
# ----------------------------------------------------------------------

def canonical_value_repr(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def uuid_to_bytes(u: str) -> bytes:
    if not u:
        return b"\x00" * 16
    return bytes.fromhex(u.replace("-", ""))


def write_global_dict(global_values: List[Any]) -> Dict[str, int]:
    DICT_DIR.mkdir(parents=True, exist_ok=True)
    values_path  = DICT_DIR / "dict.values.bin"
    offsets_path = DICT_DIR / "dict.offsets.bin"
    meta_path    = DICT_DIR / "dict.meta.json"

    print("\n[dict] Writing global dictionary:")
    print(f"[dict]   values  -> {values_path}")
    print(f"[dict]   offsets -> {offsets_path}")
    print(f"[dict]   meta    -> {meta_path}")

    offsets = [0]

    with values_path.open("wb") as f_val:
        for v in global_values:
            s = json.dumps(v, ensure_ascii=False, separators=(",", ":"))
            b = s.encode("utf-8")
            f_val.write(b)
            offsets.append(offsets[-1] + len(b))

    values_size = values_path.stat().st_size

    with offsets_path.open("wb") as f_off:
        for off in offsets:
            f_off.write(U64_LE.pack(off))

    offsets_size = offsets_path.stat().st_size

    meta = {
        "valueCount": len(global_values),
        "offsetByteSize": 8,
        "encoding": "utf-8",
        "format": "json",
        "endianness": "LE",
    }
    with meta_path.open("w", encoding="utf-8") as f_meta:
        json.dump(meta, f_meta, ensure_ascii=False, indent=2)

    meta_size = meta_path.stat().st_size

    print(f"[dict]   Values size : {values_size / (1024*1024):.2f} MiB")
    print(f"[dict]   Offsets size: {offsets_size / (1024*1024):.2f} MiB")
    print(f"[dict]   Meta size   : {meta_size / (1024*1024):.2f} MiB")
    print(f"[dict]   Entries     : {len(global_values)}")

    return {
        "valuesSize": values_size,
        "offsetsSize": offsets_size,
        "metaSize": meta_size,
        "valueCount": len(global_values),
    }

# ----------------------------------------------------------------------
# GLOBAL UUID INDEX
# ----------------------------------------------------------------------

def build_global_uuid_index(
    global_uuid_list: List[bytes],
    global_uuid_to_ctype: Dict[bytes, str],
) -> Tuple[Dict[bytes, int], Dict[str, int], Dict[str, int]]:
    """
    Build:
      packed/uuid_index/{uuids.bin, meta.json}
      packed/uuid_types/{types.bin, meta.json}

    Returns:
      (uuid_to_id, uuid_index_stats, uuid_types_stats)
    """
    print("\n[uuid-index] Building global UUID index ...")

    unique = sorted(set(global_uuid_list))
    count = len(unique)
    print(f"[uuid-index] Unique UUIDs: {count}")

    UUID_INDEX_DIR.mkdir(parents=True, exist_ok=True)
    uuids_path = UUID_INDEX_DIR / "uuids.bin"
    meta_path  = UUID_INDEX_DIR / "meta.json"

    with uuids_path.open("wb") as f:
        for b in unique:
            f.write(b)

    with meta_path.open("w", encoding="utf-8") as f:
        json.dump(
            {
                "recordCount": count,
                "uuidBytes": 16,
                "endianness": "LE",
                "sortedBy": "uuid_bytes",
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    print(f"[uuid-index] uuids.bin size: {uuids_path.stat().st_size / (1024*1024):.2f} MiB")

    uuid_to_id: Dict[bytes, int] = {b: i for i, b in enumerate(unique)}

    uuid_index_stats = {
        "uuidsSize": uuids_path.stat().st_size,
        "metaSize": meta_path.stat().st_size,
        "count": count,
    }

    # uuid_types
    UUID_TYPES_DIR.mkdir(parents=True, exist_ok=True)
    types_bin_path  = UUID_TYPES_DIR / "types.bin"
    types_meta_path = UUID_TYPES_DIR / "meta.json"

    all_types = sorted(set(global_uuid_to_ctype.values()))
    type_count = len(all_types)
    bytes_per_code = 2
    if type_count > 2**(8*bytes_per_code):
        raise ValueError(f"Too many types ({type_count}) for bytesPerCode={bytes_per_code}")

    type_to_code = {t: i for i, t in enumerate(all_types)}
    code_to_type = {i: t for t, i in type_to_code.items()}

    pack_code = struct.Struct("<H").pack

    print(f"[uuid-types] Writing types.bin for {count} UUIDs, {type_count} types")

    with types_bin_path.open("wb") as f:
        for b in unique:
            tname = global_uuid_to_ctype.get(b)
            if tname is None:
                code = 0
            else:
                code = type_to_code[tname]
            f.write(pack_code(code))

    types_meta = {
        "recordCount": count,
        "bytesPerCode": bytes_per_code,
        "endianness": "LE",
        "types": [
            {"code": code, "typeName": code_to_type[code]}
            for code in sorted(code_to_type.keys())
        ],
    }
    with types_meta_path.open("w", encoding="utf-8") as f:
        json.dump(types_meta, f, ensure_ascii=False, indent=2)

    uuid_types_stats = {
        "typesSize": types_bin_path.stat().st_size,
        "metaSize": types_meta_path.stat().st_size,
        "typeCount": type_count,
    }

    print(
        f"[uuid-types] types.bin size: {types_bin_path.stat().st_size / (1024*1024):.2f} MiB "
        f"({type_count} distinct types)"
    )

    return uuid_to_id, uuid_index_stats, uuid_types_stats

# ----------------------------------------------------------------------
# NODE FETCHING + STREAMING PACKING
# ----------------------------------------------------------------------

def iter_node_pages(type_name: str) -> Iterator[List[Dict[str, Any]]]:
    """
    Iterate all pages for a given citype by delegating to fetch_node_page().
    """
    offset = 0

    while True:
        items = fetch_node_page(type_name, NODE_PAGE_SIZE, offset)

        if not items:
            break

        yield items

        if len(items) < NODE_PAGE_SIZE:
            break

        offset += NODE_PAGE_SIZE


def pack_node_type_streaming(
    type_name: str,
    node_template: str,
    attr_order: List[str],
    attr_meta_map: Dict[str, Tuple[Optional[str], Optional[str]]],
    global_key_to_index: Dict[str, int],
    global_values: List[Any],
    global_uuid_list: List[bytes],
    global_uuid_to_ctype: Dict[bytes, str],
    global_valid_uuids: Set[bytes],
) -> Dict[str, Any]:
    """
    Second streaming pass: for each record, write a fixed-size block of int32 indices.
    """
    print(f"[nodes] Streaming pack for type {type_name}")
    NODES_PACKED.mkdir(parents=True, exist_ok=True)

    bin_path   = NODES_PACKED / f"{type_name}.bin"
    meta_path  = NODES_PACKED / f"{type_name}.meta.json"
    uuids_path = NODES_PACKED / f"{type_name}.uuids.bin"

    block_size = len(attr_order)
    int_bytes  = 4

    attr_index: Dict[str, int] = {name: idx for idx, name in enumerate(attr_order)}
    pack_i32 = INT32_LE.pack

    seen_uuids_for_type: set[bytes] = set()

    total_records = 0

    with bin_path.open("wb") as fbin, uuids_path.open("wb") as fuuid:
        for page in iter_node_pages(type_name):
            for rec in page:
                total_records += 1
                u_str = rec.get("uuid")
                if not u_str:
                    raise RuntimeError(f"[nodes] {type_name}: record #{total_records} has missing uuid")
                u_bytes = uuid_to_bytes(u_str)

                # early duplicate check to kill the run if something fetched wrong
                if u_bytes in seen_uuids_for_type:
                    import binascii
                    hexu = binascii.hexlify(u_bytes).decode("ascii")
                    friendly = (
                        f"{hexu[0:8]}-{hexu[8:12]}-{hexu[12:16]}-"
                        f"{hexu[16:20]}-{hexu[20:]}"
                    )
                    raise RuntimeError(
                        f"[nodes] {type_name}: duplicate UUID encountered during streaming "
                        f"packing at record #{total_records}: {friendly}"
                    )
                seen_uuids_for_type.add(u_bytes)

                fuuid.write(u_bytes)
                global_uuid_list.append(u_bytes)
                global_uuid_to_ctype[u_bytes] = type_name

                # Determine node validity from metaAttributes.state
                raw_meta = rec.get("metaAttributes", {}) or {}
                node_state = raw_meta.get("state")
                # Treat anything except explicit "INVALIDATED" as valid
                if node_state != "INVALIDATED":
                    global_valid_uuids.add(u_bytes)

                block = [MISSING_SENTINEL] * block_size

                # attributes
                for attr in rec.get("attributes", []):
                    name = attr.get("name")
                    if name is None:
                        continue
                    pos = attr_index.get(name)
                    if pos is None:
                        # This means data contains an attribute not in metadata
                        raise RuntimeError(
                            f"[nodes] {type_name}: encountered attribute '{name}' "
                            f"not present in metadata schema."
                        )

                    raw_value = attr.get("value")
                    key = canonical_value_repr(raw_value)
                    idx = global_key_to_index.get(key)
                    if idx is None:
                        idx = len(global_values)
                        global_values.append(raw_value)
                        global_key_to_index[key] = idx
                    block[pos] = idx

                # metaAttributes (strict)
                meta = rec.get("metaAttributes", {}) or {}
                for mname, mvalue in meta.items():
                    if mname not in ALLOWED_META_ATTRS:
                        raise RuntimeError(
                            f"[nodes] {type_name}: unexpected metaAttribute '{mname}' "
                            f"encountered. Allowed: {ALLOWED_META_ATTRS}"
                        )

                    full = f"__meta__{mname}"
                    pos = attr_index.get(full)
                    if pos is None:
                        # Should not happen because we appended all ALLOWED_META_ATTRS
                        raise RuntimeError(
                            f"[nodes] {type_name}: meta column '{full}' missing in attr_order."
                        )

                    raw_value = mvalue
                    key = canonical_value_repr(raw_value)
                    idx = global_key_to_index.get(key)
                    if idx is None:
                        idx = len(global_values)
                        global_values.append(raw_value)
                        global_key_to_index[key] = idx
                    block[pos] = idx

                for v in block:
                    fbin.write(pack_i32(v))

    # handle empty types – clean up and bail
    if total_records == 0:
        print(f"[nodes] {type_name}: 0 records, deleting empty files and skipping meta")
        try:
            bin_path.unlink(missing_ok=True)
        except TypeError:
            # Python <3.8 compatibility, if needed
            if bin_path.exists():
                bin_path.unlink()
        try:
            uuids_path.unlink(missing_ok=True)
        except TypeError:
            if uuids_path.exists():
                uuids_path.unlink()

        # Return a minimal stats dict so fetch_all() can skip further work
        return {
            "type": type_name,
            "recordCount": 0,
            "blockSize": len(attr_order),
            "binSize": 0,
            "uuidsSize": 0,
            "metaSize": 0,
            "skippedEmpty": True,
        }

    bin_size   = bin_path.stat().st_size
    uuids_size = uuids_path.stat().st_size

    # Build enriched attribute metadata (technicalName, humanName, description)
    attributes_serialized: List[List[Optional[str]]] = []
    for tech in attr_order:
        human_name: Optional[str] = None
        desc: Optional[str]       = None

        if tech.startswith("__meta__"):
            human_name = tech
            desc       = None
        else:
            if tech in attr_meta_map:
                human_name, desc = attr_meta_map[tech]

        attributes_serialized.append([tech, human_name, desc])

    meta = {
        "recordCount": total_records,
        "layout": "grid",
        "blockSize": block_size,
        "intBytes": int_bytes,
        "endianness": "LE",
        "missingSentinel": MISSING_SENTINEL,
        "attributes": attributes_serialized,
        "sortedBy": "uuid",   # important for your reader's binary search
        "typeName": type_name,
    }
    with meta_path.open("w", encoding="utf-8") as fmeta:
        json.dump(meta, fmeta, ensure_ascii=False, indent=2)

    meta_size = meta_path.stat().st_size

    print(
        f"[nodes] {type_name}: {total_records} rec, "
        f"block={block_size}, "
        f"bin={bin_size / (1024*1024):.2f} MiB, "
        f"uuids={uuids_size / (1024*1024):.2f} MiB, "
        f"meta={meta_size / (1024*1024):.2f} MiB"
    )

    return {
        "type": type_name,
        "recordCount": total_records,
        "blockSize": block_size,
        "binSize": bin_size,
        "uuidsSize": uuids_size,
        "metaSize": meta_size,
    }

def sort_node_type_by_uuid(type_name: str) -> None:
    """
    Post-processing pass: reorders nodes/<TYPE>.uuids.bin and nodes/<TYPE>.bin
    so that rows are sorted by UUID bytes.

    This enables TypeView.find_record_index_by_uuid() to binary-search
    nodes/<TYPE>.uuids.bin correctly.

    Layout:
      nodes/<TYPE>.uuids.bin : 16 bytes * N (unsorted -> sorted)
      nodes/<TYPE>.bin       : N * blockSize * intBytes (reordered in lockstep)
    """
    meta_path  = NODES_PACKED / f"{type_name}.meta.json"
    bin_path   = NODES_PACKED / f"{type_name}.bin"
    uuids_path = NODES_PACKED / f"{type_name}.uuids.bin"

    if not meta_path.is_file() or not bin_path.is_file() or not uuids_path.is_file():
        print(f"[sort-nodes] {type_name}: missing files, skipping")
        return

    with meta_path.open("r", encoding="utf-8") as f:
        meta = json.load(f)

    record_count = int(meta.get("recordCount", 0))
    block_size   = int(meta.get("blockSize", 0))
    int_bytes    = int(meta.get("intBytes", 4))

    if record_count <= 1:
        # Nothing to sort
        return

    if int_bytes != 4:
        raise ValueError(f"[sort-nodes] {type_name}: only intBytes=4 supported")

    block_bytes = block_size * int_bytes

    print(f"[sort-nodes] {type_name}: sorting {record_count} records by UUID")

    # 1) Build (uuid_bytes, old_idx) list
    rows: list[tuple[bytes, int]] = []
    with uuids_path.open("rb") as f_uuid:
        for idx in range(record_count):
            raw = f_uuid.read(16)
            if len(raw) != 16:
                raise IOError(
                    f"[sort-nodes] {type_name}: unexpected EOF in uuids.bin "
                    f"at record {idx}"
                )
            rows.append((raw, idx))

    # 2) Sort by UUID bytes
    rows.sort(key=lambda x: x[0])

    # 3) Rewrite uuids.bin and bin in sorted order
    tmp_uuids = uuids_path.with_suffix(".uuids.tmp")
    tmp_bin   = bin_path.with_suffix(".bin.tmp")

    with uuids_path.open("rb") as f_uuid_in, \
         bin_path.open("rb")   as f_bin_in, \
         tmp_uuids.open("wb")  as f_uuid_out, \
         tmp_bin.open("wb")    as f_bin_out:

        for new_pos, (uuid_bytes, old_idx) in enumerate(rows):
            # Write UUID in sorted order
            f_uuid_out.write(uuid_bytes)

            # Copy the corresponding row of ints from the old bin
            offset = old_idx * block_bytes
            f_bin_in.seek(offset)
            row = f_bin_in.read(block_bytes)
            if len(row) != block_bytes:
                raise IOError(
                    f"[sort-nodes] {type_name}: unexpected EOF in bin file "
                    f"at old_idx={old_idx}"
                )
            f_bin_out.write(row)

    # 4) Atomically replace originals
    os.replace(tmp_uuids, uuids_path)
    os.replace(tmp_bin,   bin_path)

    # 5) Optionally update meta["sortedBy"] to reflect reality
    meta["sortedBy"] = "uuid_bytes"
    with meta_path.open("w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    print(f"[sort-nodes] {type_name}: done (records={record_count})")

def get_rel_endpoints(reltype: str) -> Tuple[str, str]:
    """
    Determine (source_type, target_type) for a reltype from
    metadata/relations/<reltype>.json.

    We use the first source and first target listed there.
    """
    meta_file = RELS_META_DIR / f"{reltype}.json"
    if not meta_file.is_file():
        raise FileNotFoundError(
            f"No relation metadata for {reltype} at {meta_file}. "
            "Make sure fetch_reltype_metadata_all() ran before relations."
        )

    with meta_file.open("r", encoding="utf-8") as f:
        meta = json.load(f)

    sources = meta.get("sources") or []
    targets = meta.get("targets") or []
    if not sources or not targets:
        raise ValueError(
            f"Reltype {reltype} has no sources/targets in metadata; "
            "cannot call relation.sh."
        )

    def _first_tech(items: list[dict]) -> Optional[str]:
        for it in items:
            tech = (
                it.get("technicalName")
                or it.get("name")
                or it.get("code")
            )
            if tech:
                return tech
        return None

    source_type = _first_tech(sources)
    target_type = _first_tech(targets)

    if not source_type or not target_type:
        raise ValueError(
            f"Reltype {reltype}: could not determine source/target technicalName."
        )

    return source_type, target_type

def is_reltype_valid(reltype: str) -> bool:
    """
    Returns True if relationship type <reltype> is marked as valid in its metadata.
    Missing metadata → assume valid.
    """
    meta_file = RELS_META_DIR / f"{reltype}.json"
    if not meta_file.is_file():
        # Be conservative: treat as valid if we don't know
        return True

    try:
        with meta_file.open("r", encoding="utf-8") as f:
            meta = json.load(f)
    except Exception:
        return True

    return bool(meta.get("valid", True))

# ----------------------------------------------------------------------
# RELATIONS: FETCH TO TEMP UUID-PAIR BINS, THEN PACK
# ----------------------------------------------------------------------

def iter_rel_pages(reltype: str) -> Iterator[List[Dict[str, Any]]]:
    """
    Use relation.sh to iterate all pages for a given reltype.

    relation.sh usage:
      relation.sh <TARGET> <SOURCE> <RELATION_TYPE>
          [--template tpl] [--limit N] [--offset N] [--outdir DIR] [--no-csv]

    We:
      - get source/target from metadata/relations/<reltype>.json
      - choose template based on INCLUDE_INVALID (REL_TEMPLATE_ALL vs VALID_ONLY)
      - call relation.sh with paging and retry logic
      - parse run.sh [OUT] lines via parse_runner_output
      - expect JSON with {"result":[...]} or a bare list
    """
    # Determine source/target citypes from metadata
    try:
        source_type, target_type = get_rel_endpoints(reltype)
    except Exception as e:
        print(f"[rels] Skipping {reltype}: cannot determine endpoints: {e}", file=sys.stderr)
        return

    offset = 0
    template = REL_TEMPLATE_ALL if INCLUDE_INVALID else REL_TEMPLATE_VALID_ONLY

    while True:
        page_dir = DATE_ROOT / "relations_parts" / reltype
        page_dir.mkdir(parents=True, exist_ok=True)

        cmd = [
            "bash",
            str(REL_SH),
            target_type,
            source_type,
            reltype,
            "--template", template,
            "--limit", str(REL_PAGE_SIZE),
            "--offset", str(offset),
            "--outdir", str(page_dir),
            "--no-csv",
        ]

        print(f"[PAGE] {reltype} ({source_type}->{target_type}): limit={REL_PAGE_SIZE}, offset={offset}")

        attempt = 1

        while True:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=os.environ.copy(),
                timeout=SUBPROC_TIMEOUT,
            )

            stdout = proc.stdout or ""
            stderr = proc.stderr or ""
            lower_err = (stderr or "").lower()
            stderr_text = stderr or ""

            if proc.returncode == 0:
                out_paths = parse_runner_output(stdout)
                if not out_paths:
                    # No [OUT] file → treat as empty page, end.
                    print(f"[PAGE] {reltype}: no [OUT] paths at offset={offset} -> done.")
                    return

                last_path = out_paths[-1]
                if not last_path.is_file():
                    raise FileNotFoundError(
                        f"relation.sh reported [OUT] {last_path}, but the file does not exist."
                    )

                with last_path.open("r", encoding="utf-8") as f:
                    data = json.load(f)

                # shape may be {"result":[...]} or bare list
                result = data.get("result") if isinstance(data, dict) else data
                result = result or []

                if not result:
                    print(f"[PAGE] {reltype}: empty page at offset={offset}; done.")
                    return

                yield result

                if len(result) < REL_PAGE_SIZE:
                    print(f"[PAGE] {reltype}: last partial page (len={len(result)}) at offset={offset}; done.")
                    return

                # move to the next page
                offset += REL_PAGE_SIZE
                break  # break the inner retry loop, continue outer while

            # Non-zero return code → maybe token issue?
            token_problem = (
                "token env var is required" in lower_err
                or "token" in lower_err
                or "http error:" in lower_err and ("401" in lower_err or "403" in lower_err)
                or " 401 " in lower_err
                or " 403 " in lower_err
            )

            if token_problem and sys.stdin.isatty():
                print(
                    "\n[AUTH] relation.sh/run.sh reported an authorization problem "
                    f"(returncode={proc.returncode}).\n"
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
                    # do NOT increment attempt for auth fixes
                    continue
                else:
                    raise RuntimeError(
                        f"TOKEN required but not provided for {reltype} "
                        f"({source_type}->{target_type}) offset={offset}.\n"
                        f"stderr:\n{stderr_text}"
                    )

            # Non-auth failure → transient error, retry a few times
            if REL_PAGE_MAX_RETRIES > 0 and attempt >= REL_PAGE_MAX_RETRIES:
                raise RuntimeError(
                    f"Page fetch failed for {reltype} ({source_type}->{target_type}) "
                    f"offset={offset}, limit={REL_PAGE_SIZE} "
                    f"after {attempt} attempts (returncode={proc.returncode}).\n"
                    f"stdout:\n{stdout}\n\nstderr:\n{stderr_text}"
                )

            print(
                f"[WARN] Page fetch failed for {reltype} ({source_type}->{target_type}) "
                f"offset={offset} (attempt {attempt}/{REL_PAGE_MAX_RETRIES}, "
                f"returncode={proc.returncode}).\n"
                f"stderr (truncated): {stderr_text[:400]}\n"
                f"Retrying in {REL_PAGE_RETRY_DELAY}s..."
            )
            attempt += 1
            time.sleep(REL_PAGE_RETRY_DELAY)


def fetch_relations_to_tmp_uuid_pairs(
    reltypes: List[str],
    global_valid_uuids: Set[bytes],
) -> List[str]:
    """
    For each reltype, fetch via raw.sh+paging and write tmp uuid-pair bins:

      tmp/rels_pairs/<reltype>.uuidpairs.bin

    Each record: 32 bytes = 16 bytes src_uuid + 16 bytes tgt_uuid.
    """
    pairs_root = TMP_DIR / "rels_pairs"
    pairs_root.mkdir(parents=True, exist_ok=True)

    written_reltypes: List[str] = []

    for rel in sorted(reltypes):
        print(f"[rels] Fetching relations for {rel}")

        rel_is_valid = is_reltype_valid(rel)

        pair_path_valid   = pairs_root / f"{rel}.uuidpairs.bin"
        pair_path_invalid = pairs_root / f"{rel}_invalid.uuidpairs.bin"

        count_valid = 0
        count_invalid = 0

        # Open both files once; we may delete them later if empty
        with pair_path_valid.open("wb") as f_valid, \
             pair_path_invalid.open("wb") as f_invalid:

            for page in iter_rel_pages(rel):
                for rec in page:
                    src_uuid = rec.get("source")
                    tgt_uuid = rec.get("target")

                    if not src_uuid or not tgt_uuid:
                        # Can't interpret this edge; skip (will never pack)
                        continue

                    src_b = uuid_to_bytes(src_uuid)
                    tgt_b = uuid_to_bytes(tgt_uuid)

                    # Relation state from Groovy templates (if present)
                    state = rec.get("state") or rec.get("rel_state")
                    is_invalid_state = (state == "INVALIDATED")

                    src_ok = src_b in global_valid_uuids
                    tgt_ok = tgt_b in global_valid_uuids

                    # Classification rule:
                    #   - entire reltype invalid → always invalid
                    #   - OR relation state INVALIDATED
                    #   - OR any endpoint invalid
                    if (not rel_is_valid) or is_invalid_state or (not src_ok) or (not tgt_ok):
                        f_invalid.write(src_b)
                        f_invalid.write(tgt_b)
                        count_invalid += 1
                    else:
                        f_valid.write(src_b)
                        f_valid.write(tgt_b)
                        count_valid += 1

        # Clean up empty files and record which "reltypes" we actually produced
        if count_valid == 0:
            pair_path_valid.unlink(missing_ok=True)
        else:
            print(f"[rels]   {rel}: {count_valid} VALID edges -> {pair_path_valid}")
            written_reltypes.append(rel)

        if count_invalid == 0:
            pair_path_invalid.unlink(missing_ok=True)
        else:
            invalid_name = f"{rel}_invalid"
            print(f"[rels]   {invalid_name}: {count_invalid} INVALID edges -> {pair_path_invalid}")
            written_reltypes.append(invalid_name)

    return written_reltypes


def pack_relations_from_uuid_pairs(
    reltypes: List[str],
    uuid_to_id: Dict[bytes, int],
    global_uuid_to_ctype: Dict[bytes, str],
) -> List[Dict[str, Any]]:
    """
    Convert tmp uuid-pair bins into final packed relations:

      relations/<rel>.src.tgt.bin
      relations/<rel>.src.tgt.meta.json
      relations/<rel>.tgt.src.bin
      relations/<rel>.tgt.src.meta.json

    Also writes per-reltype and per-ctype indexes like your original packer.
    """
    RELS_PACKED.mkdir(parents=True, exist_ok=True)
    pairs_root = TMP_DIR / "rels_pairs"

    stats_list: List[Dict[str, Any]] = []
    pack_i32 = INT32_LE.pack

    # reltype -> {"srcTypes": set(str), "tgtTypes": set(str)}
    rel_endpoints: Dict[str, Dict[str, Set[str]]] = {}

    for rel in sorted(reltypes):
        pair_path = pairs_root / f"{rel}.uuidpairs.bin"
        if not pair_path.is_file():
            continue

        print(f"[rel-pack] Packing reltype {rel} from {pair_path}")
        rel_info = rel_endpoints.setdefault(rel, {
            "srcTypes": set(),
            "tgtTypes": set(),
        })

        # First: collect pairs as (src_id, tgt_id)
        pairs_src_tgt: List[Tuple[int, int]] = []

        file_size = pair_path.stat().st_size
        if file_size % 32 != 0:
            print(f"[WARN] {pair_path} size not divisible by 32; possible corruption", file=sys.stderr)
        record_count_raw = file_size // 32

        with pair_path.open("rb") as f:
            for _ in range(record_count_raw):
                src_b = f.read(16)
                tgt_b = f.read(16)
                if len(src_b) != 16 or len(tgt_b) != 16:
                    break

                try:
                    src_id = uuid_to_id[src_b]
                    tgt_id = uuid_to_id[tgt_b]
                except KeyError:
                    # Node not present among packed nodes; skip
                    continue

                pairs_src_tgt.append((src_id, tgt_id))

                src_type = global_uuid_to_ctype.get(src_b)
                tgt_type = global_uuid_to_ctype.get(tgt_b)
                if src_type is not None:
                    rel_info["srcTypes"].add(src_type)
                if tgt_type is not None:
                    rel_info["tgtTypes"].add(tgt_type)

        record_count = len(pairs_src_tgt)
        if record_count == 0:
            print(f"[rel-pack] {rel}: 0 edges after filtering; skipping")
            continue

        print(f"[rel-pack] {rel}: {record_count} edges -> packing src.tgt and tgt.src")

        pairs_src_tgt.sort(key=lambda st: (st[0], st[1]))

        # Load human relation meta if available
        rel_meta_file = RELS_META_DIR / f"{rel}.json"
        rel_name_hr: Optional[str] = None
        rel_desc_hr: Optional[str] = None
        if rel_meta_file.is_file():
            with rel_meta_file.open("r", encoding="utf-8") as f:
                rel_meta_raw = json.load(f)
            rel_name_hr = rel_meta_raw.get("name")
            rel_desc_hr = rel_meta_raw.get("description")

        # src.tgt
        src_tgt_bin  = RELS_PACKED / f"{rel}.src.tgt.bin"
        src_tgt_meta = RELS_PACKED / f"{rel}.src.tgt.meta.json"

        with src_tgt_bin.open("wb") as fbin:
            for s, t in pairs_src_tgt:
                fbin.write(pack_i32(s))
                fbin.write(pack_i32(t))

        src_tgt_size = src_tgt_bin.stat().st_size

        with src_tgt_meta.open("w", encoding="utf-8") as fmeta:
            json.dump(
                {
                    "recordCount": record_count,
                    "intBytes": 4,
                    "endianness": "LE",
                    "layout": ["src", "tgt"],
                    "sortedBy": ["src", "tgt"],
                    "technicalName": rel,
                    "name": rel_name_hr,
                    "description": rel_desc_hr,
                },
                fmeta,
                ensure_ascii=False,
                indent=2,
            )

        # tgt.src
        pairs_tgt_src = [(t, s) for s, t in pairs_src_tgt]
        pairs_tgt_src.sort(key=lambda ts: (ts[0], ts[1]))

        tgt_src_bin  = RELS_PACKED / f"{rel}.tgt.src.bin"
        tgt_src_meta = RELS_PACKED / f"{rel}.tgt.src.meta.json"

        with tgt_src_bin.open("wb") as fbin:
            for t, s in pairs_tgt_src:
                fbin.write(pack_i32(t))
                fbin.write(pack_i32(s))

        tgt_src_size = tgt_src_bin.stat().st_size

        with tgt_src_meta.open("w", encoding="utf-8") as fmeta:
            json.dump(
                {
                    "recordCount": record_count,
                    "intBytes": 4,
                    "endianness": "LE",
                    "layout": ["tgt", "src"],
                    "sortedBy": ["tgt", "src"],
                    "technicalName": rel,
                    "name": rel_name_hr,
                    "description": rel_desc_hr,
                },
                fmeta,
                ensure_ascii=False,
                indent=2,
            )

        stats_list.append(
            {
                "name": rel,
                "recordCount": record_count,
                "srcTgtSize": src_tgt_size,
                "tgtSrcSize": tgt_src_size,
            }
        )

    # Build relation indexes
    print("\n[rel-pack] Building relation indexes")
    # 1) per-reltype
    reltype_index_path = RELS_PACKED / "index_by_reltype.json"
    reltype_index_payload = {
        rel: {
            "srcTypes": sorted(list(info["srcTypes"])),
            "tgtTypes": sorted(list(info["tgtTypes"])),
        }
        for rel, info in rel_endpoints.items()
    }
    with reltype_index_path.open("w", encoding="utf-8") as f:
        json.dump(reltype_index_payload, f, ensure_ascii=False, indent=2)
    print(f"[rel-pack] Wrote per-reltype index -> {reltype_index_path}")

    # 2) per-citype
    ctype_index: Dict[str, Dict[str, List[Dict[str, str]]]] = {}
    for rel, info in rel_endpoints.items():
        src_set = info["srcTypes"]
        tgt_set = info["tgtTypes"]

        for src_type in src_set:
            for tgt_type in tgt_set:
                entry_src = ctype_index.setdefault(src_type, {
                    "asSource": [],
                    "asTarget": [],
                })
                entry_src["asSource"].append({
                    "reltype": rel,
                    "otherType": tgt_type,
                })

                entry_tgt = ctype_index.setdefault(tgt_type, {
                    "asSource": [],
                    "asTarget": [],
                })
                entry_tgt["asTarget"].append({
                    "reltype": rel,
                    "otherType": src_type,
                })

    ctype_index_path = RELS_PACKED / "index_by_ctype.json"
    with ctype_index_path.open("w", encoding="utf-8") as f:
        json.dump(ctype_index, f, ensure_ascii=False, indent=2)
    print(f"[rel-pack] Wrote per-ctype index -> {ctype_index_path}")

    return stats_list

# ----------------------------------------------------------------------
# MANIFEST
# ----------------------------------------------------------------------

def write_packed_manifest(
    dump_date_str: str,
    profile: str,
    filters: Optional[Dict] = None,
) -> None:
    if filters is None:
        filters = {}

    nodes_dir = NODES_PACKED
    rels_dir  = RELS_PACKED

    node_types: List[Dict[str, Any]] = []
    nodes_total = 0
    if nodes_dir.is_dir():
        for meta_path in nodes_dir.glob("*.meta.json"):
            name = meta_path.name
            if not name.endswith(".meta.json"):
                continue
            tname = name[:-len(".meta.json")]
            with meta_path.open("r", encoding="utf-8") as f:
                meta = json.load(f)
            rc = int(meta.get("recordCount", 0))
            if rc == 0:
                continue
            nodes_total += rc
            node_types.append({
                "typeName": tname,
                "recordCount": rc,
                "metaFile": name,
                "binFile": f"{tname}.bin",
                "uuidsFile": f"{tname}.uuids.bin",
            })

    node_type_names = sorted({t["typeName"] for t in node_types})

    rel_types: List[Dict[str, Any]] = []
    rel_pairs_total = 0
    if rels_dir.is_dir():
        for meta_path in rels_dir.glob("*.meta.json"):
            name = meta_path.name
            stem = name[:-len(".meta.json")]

            if stem.endswith(".src.tgt"):
                relname = stem[:-len(".src.tgt")]
                kind = "src.tgt"
            elif stem.endswith(".tgt.src"):
                relname = stem[:-len(".tgt.src")]
                kind = "tgt.src"
            else:
                continue

            with meta_path.open("r", encoding="utf-8") as f:
                meta = json.load(f)
            rc = int(meta.get("recordCount", 0))
            if rc == 0:
                continue
            rel_pairs_total += rc

            entry = next((r for r in rel_types if r["technicalName"] == relname), None)
            if entry is None:
                entry = {
                    "technicalName": relname,
                    "hasSrcTgt": False,
                    "hasTgtSrc": False,
                    "srcTgtFile": None,
                    "tgtSrcFile": None,
                    "recordCount": 0,
                }
                rel_types.append(entry)

            if kind == "src.tgt":
                entry["hasSrcTgt"] = True
                entry["srcTgtFile"] = name
            else:
                entry["hasTgtSrc"] = True
                entry["tgtSrcFile"] = name

            entry["recordCount"] = max(entry["recordCount"], rc)

    rel_type_names = sorted({r["technicalName"] for r in rel_types})

    manifest = {
        "version": 1,
        "createdAt": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "sourceDumpDate": dump_date_str,
        "profile": profile,
        "filters": filters,
        "nodeTypes": node_type_names,
        "relationTypes": rel_type_names,
        "counts": {
            "nodesTotal": nodes_total,
            "relationPairsTotal": rel_pairs_total,
        },
    }

    manifest_path = PACKED_ROOT / "manifest.json"
    with manifest_path.open("w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    # nodes index
    if node_types:
        nodes_index_path = nodes_dir / "index.json"
        with nodes_index_path.open("w", encoding="utf-8") as f:
            json.dump({"types": node_types}, f, ensure_ascii=False, indent=2)

    # relations index
    if rel_types:
        rels_index_path = rels_dir / "index.json"
        with rels_index_path.open("w", encoding="utf-8") as f:
            json.dump({"relationTypes": rel_types}, f, ensure_ascii=False, indent=2)

    print(f"[manifest] Wrote manifest -> {manifest_path}")

# ----------------------------------------------------------------------
# TOP-LEVEL: fetch_all()
# ----------------------------------------------------------------------

def fetch_all() -> None:
    """
    Orchestrate everything:

      - Ensure token
      - Fetch metadata (enums, nodes, relations)
      - Stream nodes into packed
      - Build dict + uuid indexes
      - Stream relations into packed
      - Write manifest
    """
    print(f"[INFO] fetch_all for dump date {METAIS_DATE}")
    token = get_bearer_token()
    print(f"[INFO] Using TOKEN length={len(token)} (not printing value)")

    # 1) Metadata
    print("\n=== METADATA ===")
    fetch_enums_all()
    citypes = fetch_citype_metadata_all()
    reltypes = fetch_reltype_metadata_all()

    if not citypes:
        print("[WARN] No citypes discovered from metadata; "
              "you can also pass an explicit list or wire METAIS_CITYPES_LIST_URL.",
              file=sys.stderr)

    if not reltypes:
        print("[WARN] No reltypes discovered from metadata; "
              "you can also pass an explicit list or wire METAIS_RELTYPES_LIST_URL.",
              file=sys.stderr)

    # 2) Nodes: streaming double pass per type
    print("\n=== NODES (streaming) ===")
    # Your raw.sh templates for nodes / invalid nodes
    # Adjust env variables to your actual setup.
    NODE_TEMPLATE = os.getenv("METAIS_NODE_TEMPLATE", "nodes_raw.j2")
    # If you want invalid nodes separately, you can add another template / pass.

    global_key_to_index: Dict[str, int] = {}
    global_values: List[Any] = []
    global_uuid_list: List[bytes] = []
    global_uuid_to_ctype: Dict[bytes, str] = {}
    global_valid_uuids: Set[bytes] = set()

    type_stats: List[Dict[str, Any]] = []

    # If citypes list is empty, you could also hardcode a subset here for testing.
    for type_name in citypes:
        # 1) Get schema from metadata only
        attr_meta_map, attr_order = load_node_attribute_metadata(type_name)

        # 2) Append fixed meta-attributes as separate columns
        #    We'll store them as "__meta__owner", "__meta__state", etc.
        for mname in ALLOWED_META_ATTRS:
            col_name = f"__meta__{mname}"
            if col_name not in attr_order:
                attr_order.append(col_name)

        st = pack_node_type_streaming(
            type_name=type_name,
            node_template=NODE_TEMPLATE,  # unused but harmless
            attr_order=attr_order,
            attr_meta_map=attr_meta_map,
            global_key_to_index=global_key_to_index,
            global_values=global_values,
            global_uuid_list=global_uuid_list,
            global_uuid_to_ctype=global_uuid_to_ctype,
            global_valid_uuids=global_valid_uuids,
        )

        # Skip empty types (0 records) – no meta/bin/uuids, nothing to sort
        if not st or st.get("recordCount", 0) == 0:
            continue

        type_stats.append(st)

    # 3) Post-pass: sort each node type by UUID so per-type uuids.bin is ordered
    print("\n=== NODES (post-pass): sorting by UUID ===")
    for st in type_stats:
        sort_node_type_by_uuid(st["type"])

    # 4) Dictionary + UUID indexes
    print("\n=== GLOBAL DICTIONARY + UUIDS ===")
    dict_stats = write_global_dict(global_values)
    uuid_to_id, uuid_index_stats, uuid_types_stats = build_global_uuid_index(
        global_uuid_list=global_uuid_list,
        global_uuid_to_ctype=global_uuid_to_ctype,
    )

    # 5) Relations: fetch -> tmp uuidpairs -> packed
    print("\n=== RELATIONS (streaming) ===")

    tmp_reltypes = fetch_relations_to_tmp_uuid_pairs(
        reltypes=reltypes,
        global_valid_uuids=global_valid_uuids,
    )
    rel_stats = pack_relations_from_uuid_pairs(
        reltypes=tmp_reltypes,
        uuid_to_id=uuid_to_id,
        global_uuid_to_ctype=global_uuid_to_ctype,
    )

    # 6) Manifest
    print("\n=== MANIFEST ===")
    write_packed_manifest(
        dump_date_str=METAIS_DATE,
        profile="full-streaming",
        filters={
            "onlyValid": not INCLUDE_INVALID,
            "includedTypes": None,
            "nodeLayout": "grid"
        },
    )

    # 7) Summary
    print("\n=== Summary per node type ===")
    for st in type_stats:
        print(
            f"- {st['type']}: "
            f"{st['recordCount']} rec, "
            f"block={st['blockSize']}, "
            f"bin={st['binSize'] / (1024*1024):.2f} MiB, "
            f"uuids={st['uuidsSize'] / (1024*1024):.2f} MiB, "
            f"meta={st['metaSize'] / (1024*1024):.2f} MiB"
        )

    print("\n=== Relation summary ===")
    for st in rel_stats:
        print(
            f"- {st['name']}: {st['recordCount']} edges, "
            f"src.tgt={st['srcTgtSize'] / (1024*1024):.2f} MiB, "
            f"tgt.src={st['tgtSrcSize'] / (1024*1024):.2f} MiB"
        )

    print("\n=== Size summary (packed only) ===")
    total_packed_bytes = 0
    for st in type_stats:
        total_packed_bytes += st["binSize"] + st["uuidsSize"] + st["metaSize"]
    total_packed_bytes += (
        dict_stats["valuesSize"] +
        dict_stats["offsetsSize"] +
        dict_stats["metaSize"]
    )
    total_packed_bytes += (
        uuid_index_stats["uuidsSize"] +
        uuid_index_stats["metaSize"] +
        uuid_types_stats["typesSize"] +
        uuid_types_stats["metaSize"]
    )
    for st in rel_stats:
        total_packed_bytes += st["srcTgtSize"] + st["tgtSrcSize"]

    print(f"[overall] packed total: {total_packed_bytes / (1024*1024):.2f} MiB")
    print("\n[fetch_all] Done.")

# ----------------------------------------------------------------------
# CLI ENTRY
# ----------------------------------------------------------------------

if __name__ == "__main__":
    fetch_all()