#!/usr/bin/env python3
import json
import time
from pathlib import Path
from requests import get
from requests.exceptions import RequestException, ProxyError, ConnectionError, Timeout

ROOT = Path(__file__).resolve().parents[1]  # meta-viz/
META_ROOT = ROOT / "data" / "metadata"
NODE_DIR = META_ROOT / "nodes"
REL_DIR = META_ROOT / "relations"
NODE_INDEX_PATH = META_ROOT / "node_index.json"
REL_INDEX_PATH = META_ROOT / "relation_index.json"

CITYPES_LIST_URL = "https://metais.slovensko.sk/api/types-repo/citypes/list"
CITYPE_URL = "https://metais.slovensko.sk/api/types-repo/citypes/citype/{technicalName}"

REL_LIST_URL = "https://metais.slovensko.sk/api/types-repo/relationshiptypes/list"
RELTYPE_URL = "https://metais.slovensko.sk/api/types-repo/relationshiptypes/relationshiptype/{technicalName}"


# --------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------

def safe_get(url, max_retries=5, timeout=30, backoff=2):
    """
    Fetch a URL with exponential backoff retrying on network or 5xx errors.
    Returns requests.Response or None on permanent failure.
    """
    delay = 1
    for attempt in range(1, max_retries + 1):
        try:
            r = get(url, timeout=timeout)
            # retry on transient server errors
            if r.status_code >= 500:
                raise RequestException(f"HTTP {r.status_code}")
            return r
        except (ProxyError, ConnectionError, Timeout, RequestException) as e:
            if attempt < max_retries:
                print(f"(!!)  {url} failed ({e}), retrying {attempt}/{max_retries} in {delay}s...")
                time.sleep(delay)
                delay *= backoff
                continue
            else:
                print(f"(X)  {url} failed after {max_retries} attempts: {e}")
                return None

def extract_list(raw, preferred_keys=("result", "items", "types", "data")):
    """Normalize any MetaIS /list response into a list of items or keys."""
    if isinstance(raw, list):
        return raw

    if isinstance(raw, dict):
        for k in preferred_keys:
            v = raw.get(k)
            if isinstance(v, list):
                return v
        for v in raw.values():
            if isinstance(v, list):
                return v
        if all(isinstance(k, str) for k in raw.keys()):
            return list(raw.keys())

    raise RuntimeError(f"Cannot extract list from JSON type={type(raw)}")


def safe_json_write(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------
# Citypes (node types)
# --------------------------------------------------------------------
def fetch_citypes():
    print(f"Fetching node types list from {CITYPES_LIST_URL} ...")
    r = safe_get(CITYPES_LIST_URL)
    r.raise_for_status()
    raw_types = r.json()

    try:
        type_items = extract_list(raw_types)
    except Exception:
        print(json.dumps(raw_types, ensure_ascii=False)[:800])
        raise

    index = {"types": {}}

    for t in type_items:
        if isinstance(t, str):
            technical = t
            base_info = {}
        elif isinstance(t, dict):
            technical = t.get("technicalName") or t.get("name")
            base_info = t
        else:
            continue

        if not technical:
            continue

        print(f"  → Node: {technical}")
        url = CITYPE_URL.format(technicalName=technical)
        r2 = safe_get(url)
        if not r2.ok:
            print(f"    !! Failed {r2.status_code} for {technical}, skipping")
            continue

        meta = r2.json()

        meta_type = meta.get("type") or base_info.get("type")
        labels = base_info.get("labels") or meta.get("labels") or []
        if not isinstance(labels, list):
            labels = []

        is_application = meta_type == "application"
        is_system = meta_type == "system"
        is_codelist = is_application and ("codelist" in labels)

        # save full JSON
        safe_json_write(NODE_DIR / f"{technical}.json", meta)

        # compact entry
        entry = {
            "technicalName": technical,
            "name": meta.get("name") or base_info.get("name"),
            "description": meta.get("description") or base_info.get("description"),
            "typeKind": meta_type,
            "labels": labels,
            "isApplication": is_application,
            "isSystem": is_system,
            "isCodelist": is_codelist,
            "attributes": {},
        }

        for attr in meta.get("attributes", []):
            tech_attr = attr.get("technicalName")
            if not tech_attr:
                continue
            entry["attributes"][tech_attr] = {
                "name": attr.get("name"),
                "description": attr.get("description"),
                "mandatory": (attr.get("mandatory") or {}).get("type"),
                "opendata": attr.get("opendata"),
                "attributeTypeEnum": attr.get("attributeTypeEnum"),
                "readOnly": attr.get("readOnly"),
                "invisible": attr.get("invisible"),
            }

        index["types"][technical] = entry

    safe_json_write(NODE_INDEX_PATH, index)
    print(f"✔ Saved node metadata under {NODE_DIR}")
    print(f"✔ Node index: {NODE_INDEX_PATH}")
    return index


# --------------------------------------------------------------------
# Relationship types
# --------------------------------------------------------------------
def fetch_relationshiptypes():
    print(f"\nFetching relationship types list from {REL_LIST_URL} ...")
    r = safe_get(REL_LIST_URL)
    r.raise_for_status()
    raw_types = r.json()

    try:
        rel_items = extract_list(raw_types)
    except Exception:
        print(json.dumps(raw_types, ensure_ascii=False)[:800])
        raise

    index = {"relations": {}}

    for t in rel_items:
        if isinstance(t, str):
            technical = t
            base_info = {}
        elif isinstance(t, dict):
            technical = t.get("technicalName") or t.get("name")
            base_info = t
        else:
            continue

        if not technical:
            continue

        print(f"  → Relation: {technical}")
        url = RELTYPE_URL.format(technicalName=technical)
        r2 = safe_get(url)
        if not r2.ok:
            print(f"    !! Failed {r2.status_code} for {technical}, skipping")
            continue

        meta = r2.json()

        # save full JSON
        safe_json_write(REL_DIR / f"{technical}.json", meta)

        source = (meta.get("sources") or [None])[0] or {}
        target = (meta.get("targets") or [None])[0] or {}

        entry = {
            "technicalName": technical,
            "name": meta.get("name") or base_info.get("name"),
            "description": meta.get("description") or base_info.get("description"),
            "engDescription": meta.get("engDescription"),
            "type": meta.get("type") or base_info.get("type"),
            "category": meta.get("category"),
            "source": {
                "technicalName": source.get("technicalName"),
                "name": source.get("name"),
                "type": source.get("type"),
                "labels": source.get("labels") or [],
            },
            "target": {
                "technicalName": target.get("technicalName"),
                "name": target.get("name"),
                "type": target.get("type"),
                "labels": target.get("labels") or [],
            },
            "sourceCardinality": meta.get("sourceCardinality"),
            "targetCardinality": meta.get("targetCardinality"),
            "attributes": {},
        }

        for attr in meta.get("attributes", []):
            tech_attr = attr.get("technicalName")
            if not tech_attr:
                continue
            entry["attributes"][tech_attr] = {
                "name": attr.get("name"),
                "description": attr.get("description"),
                "mandatory": (attr.get("mandatory") or {}).get("type"),
                "opendata": attr.get("opendata"),
                "attributeTypeEnum": attr.get("attributeTypeEnum"),
                "readOnly": attr.get("readOnly"),
                "invisible": attr.get("invisible"),
            }

        index["relations"][technical] = entry

    safe_json_write(REL_INDEX_PATH, index)
    print(f"✔ Saved relation metadata under {REL_DIR}")
    print(f"✔ Relation index: {REL_INDEX_PATH}")
    return index


# --------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------
def main():
    META_ROOT.mkdir(parents=True, exist_ok=True)
    NODE_DIR.mkdir(parents=True, exist_ok=True)
    REL_DIR.mkdir(parents=True, exist_ok=True)

    print("=== Fetching MetaIS metadata (nodes + relations) ===")
    fetch_citypes()
    fetch_relationshiptypes()
    print("\n✓ Done. Metadata fully refreshed.")


if __name__ == "__main__":
    main()