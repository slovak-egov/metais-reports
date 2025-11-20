#!/usr/bin/env python3
from datetime import date
import argparse
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median
from typing import Any, Dict, List, Optional, Set, Tuple

import requests

from config_env import load_env_file
load_env_file()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_ROOT = PROJECT_ROOT / os.getenv("METAIS_RAW_ROOT", "output")
STATS_ROOT = PROJECT_ROOT / os.getenv("METAIS_STATS_ROOT", "meta-viz/data/stats")
METADATA_ROOT = PROJECT_ROOT / os.getenv("METAIS_METADATA_ROOT", "meta-viz/data/metadata")

CITYPES_LIST_URL = os.getenv(
    "CITYPES_URL",
    "https://metais.slovensko.sk/api/types-repo/citypes/list",
)
CITYPE_DETAIL_BASE = os.getenv(
    "CITYPES_DETAIL_URL",
    "https://metais.slovensko.sk/api/types-repo/citypes/citype",
)
META_REL_DETAIL_BASE = os.getenv(
    "RELTYPES_DETAIL_URL",
    "https://metais.slovensko.sk/api/types-repo/relationshiptypes/relationshiptype"
)

# Raw dumps (nodes + relations)
RAW_ROOT       = Path(os.getenv("METAIS_RAW_ROOT", "output"))
# Stats used by meta-viz (coverage, relation summaries,...)
STATS_ROOT     = Path(os.getenv("METAIS_STATS_ROOT", "meta-viz/data/stats"))
# Raw metadata (direct citype/relationshiptype API payloads)
METADATA_ROOT  = Path(os.getenv("METAIS_METADATA_ROOT", "meta-viz/data/metadata"))


# dd-mm-yyyy, overridable via env if you ever want a fixed tag
SNAPSHOT_DATE = os.getenv("METAIS_SNAPSHOT_DATE", date.today().strftime("%d-%m-%Y"))
RAW_BASE      = RAW_ROOT / SNAPSHOT_DATE
STATS_BASE    = STATS_ROOT / SNAPSHOT_DATE
META_BASE     = METADATA_ROOT / SNAPSHOT_DATE

NODES_DIR         = RAW_BASE / "nodes"
RELS_DIR          = RAW_BASE / "relations"

ATTRS_OUT_DIR     = STATS_BASE / "nodes"
REL_ATTRS_OUT_DIR = STATS_BASE / "relations"

CITYPE_META_DIR   = META_BASE / "nodes"
RELTYPE_META_DIR  = META_BASE / "relations"

# Optional index files (like old script)
NODE_INDEX_PATH = METADATA_ROOT / "node_index.json"
REL_INDEX_PATH  = METADATA_ROOT / "relation_index.json"

# Make sure dirs exist
CITYPE_META_DIR.mkdir(parents=True, exist_ok=True)
RELTYPE_META_DIR.mkdir(parents=True, exist_ok=True)
ATTRS_OUT_DIR.mkdir(parents=True, exist_ok=True)
REL_ATTRS_OUT_DIR.mkdir(parents=True, exist_ok=True)

HTTP_TIMEOUT = float(os.getenv("METAIS_HTTP_TIMEOUT", "20"))
REL_INCLUDE_REGEX = os.getenv("METAIS_REL_INCLUDE_REGEX", "")   # e.g. r"^(PO_je_gestor_KS|KRIS_.*)$"
REL_EXCLUDE_REGEX = os.getenv("METAIS_REL_EXCLUDE_REGEX", r"^(CMDB_|LATEST_REQUEST|PREVIOUS_REQUEST)$")

# utils
def load_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def fetch_citype_meta(tech_name: str) -> Optional[dict]:
    """
    Get citype metadata for a node type, with caching on disk:
    meta-viz/data/metadata/<DATE>/nodes/citype_{KS}.json etc.
    """
    path = CITYPE_META_DIR / f"citype_{tech_name}.json"

    if path.exists():
        try:
            return load_json(path)
        except Exception:
            print(f"[WARN] Failed to read cached citype meta: {path}, refetching...")

    url = f"{CITYPE_DETAIL_BASE}/{tech_name}"
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT, headers={"Accept": "application/json"})
        r.raise_for_status()
        data = r.json()
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return data
    except Exception as e:
        print(f"[WARN] citype metadata fetch failed for {tech_name}: {e}")
        return None

def get_result_array(doc: Any) -> List[Dict[str, Any]]:
    # Envelope with "result": [ ... ] or a bare list
    if isinstance(doc, dict) and isinstance(doc.get("result"), list):
        return doc["result"]
    if isinstance(doc, dict) and isinstance(doc.get("results"), list):
        return doc["results"]
    if isinstance(doc, list):
        return doc
    raise ValueError("Unrecognized raw JSON format: expected envelope with 'result(s)' or a list.")

# uuid -> json fragment
def build_uuid_index(raw_doc: Any) -> Dict[str, Dict[str, Any]]:
    results = get_result_array(raw_doc)
    idx: Dict[str, Dict[str, Any]] = {}
    missing = False
    miss_count = 0
    for o in results:
        u = o.get("uuid")
        if u:
            idx[u] = o
        else:
            missing = True
            miss_count+=1
    if missing:
        print(f"Warning: JSON missing UUIDs in {miss_count}/{len(results)} elements.")
    return idx

def extract_display_name(node_obj: dict) -> Optional[str]:
    attrs = node_obj.get("attributes", []) or []

    # exact
    for a in attrs:
        if (a.get("name") or "") == "Gen_Profil_nazov":
            v = a.get("value")
            if isinstance(v, str) and v.strip():
                return v.strip()

    # contains "nazov"
    for a in attrs:
        an = (a.get("name") or "").lower()
        if "nazov" in an:
            v = a.get("value")
            if isinstance(v, str) and v.strip():
                return v.strip()

    # any string
    for a in attrs:
        v = a.get("value")
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None

def extract_metais_code(node_obj: dict) -> Optional[str]:
    attrs = node_obj.get("attributes", []) or []

    for a in attrs:
        if (a.get("name") or "") == "Gen_Profil_kod_metais":
            v = a.get("value")
            if isinstance(v, str) and v.strip():
                return v.strip()

    for a in attrs:
        an = (a.get("name") or "").lower()
        if "kod" in an or "code" in an:
            v = a.get("value")
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None

def load_node_uuids_and_names(node_path: Path):
    try:
        data = load_json(node_path)
    except FileNotFoundError:
        return set(), {}, {}

    arr = get_result_array(data)
    uuids: Set[str] = set()
    names: Dict[str, str] = {}
    codes: Dict[str, str] = {}

    for o in arr:
        u = o.get("uuid")
        if not u:
            continue
        uuids.add(u)
        nm = extract_display_name(o)
        if nm:
            names[u] = nm
        cd = extract_metais_code(o)
        if cd:
            codes[u] = cd

    return uuids, names, codes

# ------------------------------ Node attribute coverage ------------------------------
def count_attribute_presence_with_uniques(
    objs: List[Dict[str, Any]]
) -> Tuple[Counter, Dict[str, int], int]:
    total = len(objs)
    presence = Counter()
    uniques_map: Dict[str, set] = defaultdict(set)

    for o in objs:
        seen_names = set()

        # core fields: uuid, type
        for core in ("uuid", "type"):
            v = o.get(core)
            if v is not None:
                if core not in seen_names:
                    presence[core] += 1
                    seen_names.add(core)
                uniques_map[core].add(str(v))

        # standard attributes[]
        attrs = o.get("attributes", []) or []
        for a in attrs:
            name = a.get("name")
            if not name:
                continue

            if name not in seen_names:
                presence[name] += 1
                seen_names.add(name)

            v = a.get("value")
            if v is None or v == "":
                continue
            if isinstance(v, (dict, list)):
                key = json.dumps(v, sort_keys=True, ensure_ascii=False)
            else:
                key = str(v)
            uniques_map[name].add(key)

        # metaAttributes (optional, but very useful)
        meta = o.get("metaAttributes") or {}
        for k, v in meta.items():
            name = f"meta.{k}"  # prefix to distinguish meta from normal attrs
            if name not in seen_names:
                presence[name] += 1
                seen_names.add(name)
            if v is None or v == "":
                continue
            if isinstance(v, (dict, list)):
                key = json.dumps(v, sort_keys=True, ensure_ascii=False)
            else:
                key = str(v)
            uniques_map[name].add(key)

    unique_counts = {k: len(s) for k, s in uniques_map.items()}
    return presence, unique_counts, total

def print_attr_table(counter: Counter, total: int, title: Optional[str] = None) -> None:
    if title:
        print(title)
    print(f"Total objects in dataset: {total}\n")
    print(f"{'Attribute Name':70} {'Count':>7} {'% of Dataset':>12}")
    print("-" * 91)
    rows = sorted(
        ((name, cnt, (cnt / total * 100.0 if total else 0.0)) for name, cnt in counter.items()),
        key=lambda x: x[2],
        reverse=True,
    )
    for name, cnt, pct in rows:
        print(f"{name:70} {cnt:7d} {pct:11.3f}%")
    print()

def infer_node_type_from_file(path: Path, objs: List[Dict[str, Any]]) -> str:
    types = {o.get("type") for o in objs if o.get("type")}
    if len(types) == 1:
        return next(iter(types))
    return path.stem

def write_attributes_json(
    out_dir: Path,
    source_path: Path,
    objs: List[Dict[str, Any]],
    counter: Counter,
    unique_counts: Dict[str, int],
    total: int,
    defined_attrs: Optional[List[Dict[str, Any]]] = None,
) -> Path:
    node_type = infer_node_type_from_file(source_path, objs)
    out_dir.mkdir(parents=True, exist_ok=True)

    attributes = []
    for name, cnt in counter.items():
        pct = (cnt / total * 100.0) if total else 0.0
        uniq = unique_counts.get(name, 0)
        uniq_pct = (uniq / cnt * 100.0) if cnt else 0.0
        attributes.append({
            "name": name,
            "count": cnt,
            "pct": pct,
            "unique_count": uniq,
            "unique_pct": uniq_pct,
        })

    # ensure attributes defined in citype metadata are present even if never seen
    defined_names: Set[str] = set()
    if defined_attrs:
        for da in defined_attrs:
            tech = da.get("technicalName")
            if tech:
                defined_names.add(tech)

    existing_names = {a["name"] for a in attributes}
    for tech_name in sorted(defined_names - existing_names):
        attributes.append({
            "name": tech_name,
            "count": 0,
            "pct": 0.0,
            "unique_count": 0,
            "unique_pct": 0.0,
        })

    attributes.sort(key=lambda a: a["pct"], reverse=True)

    out_path = out_dir / f"{node_type}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"type": node_type, "count": total, "attributes": attributes},
                  f, ensure_ascii=False, indent=2)
    return out_path

# ------------------------------ Relation TABLE parsing ------------------------------
def load_relation_pairs(rel_path: Path) -> List[Tuple[str, str]]:
    data = load_json(rel_path)
    if data.get("type") != "TABLE":
        raise ValueError(f"{rel_path} is not a TABLE relation JSON")
    rows = data["result"]["rows"]
    pairs = []
    for r in rows:
        vals = r.get("values", [])
        if len(vals) >= 2:
            u0, u1 = str(vals[0]).strip(), str(vals[1]).strip()
            if u0 and u1:
                pairs.append((u0, u1))
    return pairs

def summarize_degrees(counter: Counter) -> Optional[Dict[str, float]]:
    if not counter:
        return None
    degs = sorted(counter.values())
    n = len(degs)

    def pct(p: float):
        idx = int(round((p / 100.0) * (n - 1)))
        return degs[idx]

    return {
        "min": degs[0],
        "max": degs[-1],
        "avg": sum(degs) / n,
        "median": median(degs),
        "p90": pct(90),
        "p99": pct(99),
    }

def top_nodes(counter: Counter, name_map: Dict[str, str], code_map: Dict[str, str], limit: Optional[int] = None):
    items = counter.most_common(limit)
    return [{"uuid": u, "degree": int(d), "name": name_map.get(u), "code": code_map.get(u)} for u, d in items]

def classify_cardinality(src_deg: Counter, tgt_deg: Counter) -> str:
    if not src_deg and not tgt_deg: return "empty"
    smax = max(src_deg.values()) if src_deg else 0
    tmax = max(tgt_deg.values()) if tgt_deg else 0
    if smax <= 1 and tmax <= 1: return "one-to-one"
    if smax > 1 and tmax <= 1:  return "one-to-many"
    if smax <= 1 and tmax > 1:  return "many-to-one"
    return "many-to-many"

# DSU for island analysis
class DSU:
    def __init__(self):
        self.parent = {}
        self.rank = {}
    def make(self, x):
        if x not in self.parent:
            self.parent[x] = x
            self.rank[x] = 0
    def find(self, x):
        if self.parent[x] != x:
            self.parent[x] = self.find(self.parent[x])
        return self.parent[x]
    def union(self, a, b):
        self.make(a); self.make(b)
        ra, rb = self.find(a), self.find(b)
        if ra == rb: return
        if self.rank[ra] < self.rank[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        if self.rank[ra] == self.rank[rb]:
            self.rank[ra] += 1

def summarize_islands(src_uuids: Set[str], tgt_uuids: Set[str], pair_counts: Counter, src_deg: Counter, tgt_deg: Counter):
    dsu = DSU()
    for u in src_uuids: dsu.make(f"S:{u}")
    for v in tgt_uuids: dsu.make(f"T:{v}")
    for (s, t), _c in pair_counts.items():
        dsu.union(f"S:{s}", f"T:{t}")

    comps = defaultdict(lambda: {"src": set(), "tgt": set()})
    for u in src_uuids: comps[dsu.find(f"S:{u}")]["src"].add(u)
    for v in tgt_uuids: comps[dsu.find(f"T:{v}")]["tgt"].add(v)

    total_nodes = len(src_uuids) + len(tgt_uuids)
    src_total, tgt_total = len(src_uuids), len(tgt_uuids)

    combined_singleton = 0
    combined_islands = []
    src_islands, src_singleton, src_island_count = [], 0, 0
    tgt_islands, tgt_singleton, tgt_island_count = [], 0, 0

    for comp in comps.values():
        size_src = len(comp["src"])
        size_tgt = len(comp["tgt"])
        size_total = size_src + size_tgt

        # combined
        if size_total <= 1:
            combined_singleton += 1
        else:
            best_src, best_src_deg = None, -1
            for u in comp["src"]:
                d = src_deg.get(u, 0)
                if d > best_src_deg: best_src_deg, best_src = d, u
            best_tgt, best_tgt_deg = None, -1
            for v in comp["tgt"]:
                d = tgt_deg.get(v, 0)
                if d > best_tgt_deg: best_tgt_deg, best_tgt = d, v

            combined_islands.append({
                "size_total": size_total,
                "size_source": size_src,
                "size_target": size_tgt,
                "fraction_total": (size_total / total_nodes) if total_nodes else None,
                "top_source_uuid": best_src,
                "top_source_degree": best_src_deg if best_src is not None else None,
                "top_target_uuid": best_tgt,
                "top_target_degree": best_tgt_deg if best_tgt is not None else None,
            })

        # source-only
        if size_src > 0:
            src_island_count += 1
            if size_src == 1:
                src_singleton += 1
            else:
                best_src, best_src_deg = None, -1
                for u in comp["src"]:
                    d = src_deg.get(u, 0)
                    if d > best_src_deg: best_src_deg, best_src = d, u
                src_islands.append({
                    "size": size_src,
                    "fraction": (size_src / src_total) if src_total else None,
                    "size_total": size_total,
                    "top_uuid": best_src,
                    "top_degree": best_src_deg if best_src is not None else None,
                })

        # target-only
        if size_tgt > 0:
            tgt_island_count += 1
            if size_tgt == 1:
                tgt_singleton += 1
            else:
                best_tgt, best_tgt_deg = None, -1
                for v in comp["tgt"]:
                    d = tgt_deg.get(v, 0)
                    if d > best_tgt_deg: best_tgt_deg, best_tgt = d, v
                tgt_islands.append({
                    "size": size_tgt,
                    "fraction": (size_tgt / tgt_total) if tgt_total else None,
                    "size_total": size_total,
                    "top_uuid": best_tgt,
                    "top_degree": best_tgt_deg if best_tgt is not None else None,
                })

    combined_islands.sort(key=lambda x: x["size_total"], reverse=True)
    src_islands.sort(key=lambda x: x["size"], reverse=True)
    tgt_islands.sort(key=lambda x: x["size"], reverse=True)

    return {
        "total_nodes": total_nodes,
        "total_islands": len(comps),
        "singleton_islands": combined_singleton,
        "multi_islands": combined_islands,
        "source": {
            "total_nodes": src_total,
            "total_islands": src_island_count,
            "singleton_islands": src_singleton,
            "multi_islands": src_islands,
        },
        "target": {
            "total_nodes": tgt_total,
            "total_islands": tgt_island_count,
            "singleton_islands": tgt_singleton,
            "multi_islands": tgt_islands,
        },
    }

# ------------------------------ MetaIS: relation metadata ------------------------------
def fetch_relation_meta(rel_name: str) -> Optional[dict]:
    """
    Get relationshiptype metadata with caching:
    meta-viz/data/metadata/<DATE>/relations/<REL>.json
    """
    path = RELTYPE_META_DIR / f"{rel_name}.json"

    if path.exists():
        try:
            return load_json(path)
        except Exception:
            print(f"[WARN] Failed to read cached relation meta: {path}, refetching...")

    url = f"{META_REL_DETAIL_BASE}/{rel_name}"
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT, headers={"Accept": "application/json"})
        r.raise_for_status()
        data = r.json()
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return data
    except Exception as e:
        print(f"[WARN] metadata fetch failed for {rel_name}: {e}")
        return None

# ------------------------------ Pipelines ------------------------------
def process_nodes(nodes_dir: Path, out_dir: Path, *, only: Optional[re.Pattern] = None, skip: Optional[re.Pattern] = None):
    out_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(nodes_dir.glob("*.json"))
    done = 0
    for fp in files:
        name = fp.stem
        if only and not only.search(name):
            continue
        if skip and skip.search(name):
            continue
        try:
            doc = load_json(fp)
            objs = get_result_array(doc)
            counter, unique_counts, total = count_attribute_presence_with_uniques(objs)

            node_type = infer_node_type_from_file(fp, objs)
            citype_meta = fetch_citype_meta(node_type)
            defined_attrs = citype_meta.get("attributes", []) if citype_meta else []

            print_attr_table(counter, total, title=f"======== {fp.name} (central) ========")
            out_path = write_attributes_json(out_dir, fp, objs, counter, unique_counts, total, defined_attrs)
            print(f"[ATTR] {name} -> {out_path}")
            done += 1
        except Exception as e:
            print(f"[ERR ] {name}: {e}")
    print(f"[INFO] Node attributes: {done} files.")

def process_relations(rels_dir: Path, nodes_dir: Path, out_dir: Path, *, only: Optional[re.Pattern] = None, skip: Optional[re.Pattern] = None):
    out_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(rels_dir.glob("*.json"))
    done = 0
    for rel_file in files:
        rel_name = rel_file.stem
        if only and not only.search(rel_name):
            continue
        if skip and skip.search(rel_name):
            continue

        print(f"[REL ] {rel_name} …")
        meta = fetch_relation_meta(rel_name)
        if not meta:
            print(f"  !! skip (no metadata)")
            continue

        source_type = meta["sources"][0]["technicalName"] if meta.get("sources") else None
        target_type = meta["targets"][0]["technicalName"] if meta.get("targets") else None
        source_name = meta["sources"][0].get("name") if meta.get("sources") else None
        target_name = meta["targets"][0].get("name") if meta.get("targets") else None

        if not source_type or not target_type:
            print(f"  !! skip (missing source/target types in metadata)")
            continue

        src_path = nodes_dir / f"{source_type}.json"
        tgt_path = nodes_dir / f"{target_type}.json"
        if not src_path.exists() or not tgt_path.exists():
            print(f"  !! skip (missing node files: {src_path.name} or {tgt_path.name})")
            continue

        try:
            src_uuids, src_names, src_codes = load_node_uuids_and_names(src_path)
            tgt_uuids, tgt_names, tgt_codes = load_node_uuids_and_names(tgt_path)
            pairs = load_relation_pairs(rel_file)

            src_deg, tgt_deg = Counter(), Counter()
            pair_counts = Counter()
            ambiguous = 0
            for u0, u1 in pairs:
                if u0 in src_uuids and u1 in tgt_uuids:
                    src, tgt = u0, u1
                elif u1 in src_uuids and u0 in tgt_uuids:
                    src, tgt = u1, u0
                else:
                    src, tgt = u0, u1
                    ambiguous += 1
                src_deg[src] += 1
                tgt_deg[tgt] += 1
                pair_counts[(src, tgt)] += 1

            # duplicates
            total_edges = len(pairs)
            unique_pairs = len(pair_counts)
            duplicate_edges = sum(c - 1 for c in pair_counts.values() if c > 1)
            pairs_with_dup = sum(1 for c in pair_counts.values() if c > 1)
            parallel_max = max(pair_counts.values()) if pair_counts else 0

            # degree summaries & tops
            deg_src_summary = summarize_degrees(src_deg)
            deg_tgt_summary = summarize_degrees(tgt_deg)
            top_src = top_nodes(src_deg, src_names, src_codes, limit=None)
            top_tgt = top_nodes(tgt_deg, tgt_names, tgt_codes, limit=None)

            # parallel edges detail
            parallel_pairs_detail = []
            for (s, t), c in pair_counts.items():
                if c > 1:
                    parallel_pairs_detail.append({
                        "count": c,
                        "source": {"uuid": s, "code": src_codes.get(s), "name": src_names.get(s)},
                        "target": {"uuid": t, "code": tgt_codes.get(t), "name": tgt_names.get(t)},
                    })

            # islands
            islands = summarize_islands(src_uuids, tgt_uuids, pair_counts, src_deg, tgt_deg)

            # attributes (from relation metadata)
            attributes = []
            for a in meta.get("attributes", []):
                attributes.append({
                    "technicalName": a.get("technicalName"),
                    "name": a.get("name"),
                    "description": a.get("description"),
                    "mandatory": (a.get("mandatory") or {}).get("type"),
                    "attributeTypeEnum": a.get("attributeTypeEnum"),
                })

            summary = {
                "relation_name": rel_name,
                "name": meta.get("name"),
                "description": meta.get("description"),
                "engDescription": meta.get("engDescription"),
                "type": meta.get("type"),
                "source_type": source_type,
                "source_name": source_name,
                "target_type": target_type,
                "target_name": target_name,
                "sourceCardinality": meta.get("sourceCardinality"),
                "targetCardinality": meta.get("targetCardinality"),
                "stats": {
                    "edges_total": total_edges,
                    "unique_pairs": unique_pairs,
                    "duplicate_edges": duplicate_edges,
                    "pairs_with_duplicates": pairs_with_dup,
                    "parallel_edges": duplicate_edges,
                    "parallel_max": parallel_max,
                    "source_total": len(src_uuids),
                    "target_total": len(tgt_uuids),
                    "source_connected": len(src_deg),
                    "target_connected": len(tgt_deg),
                    "cardinality": classify_cardinality(src_deg, tgt_deg),
                    "ambiguous_pairs": ambiguous,
                    "degree_source": deg_src_summary,
                    "degree_target": deg_tgt_summary,
                    "top_source": top_src,
                    "top_target": top_tgt,
                    "islands": islands,
                    "parallel_pairs_detail": parallel_pairs_detail,
                },
                "attributes": attributes,
            }

            out_path = out_dir / f"{rel_name}.json"
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(summary, f, ensure_ascii=False, indent=2)
            print(f"  --> saved {out_path}")
            done += 1

        except Exception as e:
            print(f"  !! error processing {rel_name}: {e}")

    print(f"[INFO] Relation attributes: {done} files.")

def rebuild_stats_index(stats_root: Path, out_path: Path) -> None:
    """
    Build meta-viz/data/stats/index.json by scanning all snapshot subdirs under stats_root.
    For each snapshot <DATE>, it looks for:
      - <DATE>/nodes/*.json      -> node_types (+ non_empty_node_types)
      - <DATE>/relations/*.json  -> relations (+ non_empty_relations)
    """
    snapshots = []

    for snap_dir in sorted(stats_root.iterdir()):
        if not snap_dir.is_dir():
            continue

        date_str = snap_dir.name  # "19-11-2025"

        nodes_dir = snap_dir / "nodes"
        rels_dir = snap_dir / "relations"

        node_types: list[str] = []
        non_empty_node_types: list[str] = []
        relations: list[str] = []
        non_empty_relations: list[str] = []

        # --- Nodes ---
        if nodes_dir.exists():
            for p in sorted(nodes_dir.glob("*.json")):
                tech = p.stem
                node_types.append(tech)

                try:
                    data = load_json(p)
                except Exception as e:
                    print(f"[WARN] Failed to read node stats {p}: {e}")
                    continue

                # Stats format from write_attributes_json:
                # { "type": ..., "count": <int>, "attributes": [...] }
                cnt = data.get("count", 0)
                if isinstance(cnt, (int, float)) and cnt > 0:
                    non_empty_node_types.append(tech)

        # --- Relations ---
        if rels_dir.exists():
            for p in sorted(rels_dir.glob("*.json")):
                rel_name = p.stem
                relations.append(rel_name)

                try:
                    data = load_json(p)
                except Exception as e:
                    print(f"[WARN] Failed to read relation stats {p}: {e}")
                    continue

                # Relation summary format:
                # { ..., "stats": { "edges_total": <int>, ... } }
                stats = data.get("stats") or {}
                edges_total = stats.get("edges_total", 0)
                if isinstance(edges_total, (int, float)) and edges_total > 0:
                    non_empty_relations.append(rel_name)

        # only add if there is at least something
        if not node_types and not relations:
            continue

        snapshots.append({
            "date": date_str,
            "node_types": node_types,
            "relations": relations,
            "non_empty_node_types": non_empty_node_types,
            "non_empty_relations": non_empty_relations,
        })

    index_obj = {"snapshots": snapshots}

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(index_obj, f, ensure_ascii=False, indent=2)

    print(f"[META] Wrote stats index: {out_path}")

# relation index builder
def build_relation_index_from_meta(meta_dir: Path, out_path: Path) -> None:
    """
    Build relation_index.json from cached <REL>.json files,
    mirroring the old metadata script.
    """
    index = {"relations": {}}

    for fp in sorted(meta_dir.glob("*.json")):
        try:
            meta = load_json(fp)
        except Exception as e:
            print(f"[WARN] Skipping malformed relation meta {fp}: {e}")
            continue

        technical = meta.get("technicalName") or fp.stem
        if not technical:
            continue

        base_type = meta.get("type")
        source = (meta.get("sources") or [None])[0] or {}
        target = (meta.get("targets") or [None])[0] or {}

        entry = {
            "technicalName": technical,
            "name": meta.get("name"),
            "description": meta.get("description"),
            "engDescription": meta.get("engDescription"),
            "type": base_type,
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

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
    print(f"[META] Wrote relation index: {out_path}")

# node index builder
def build_node_index_from_citypes(meta_dir: Path, out_path: Path) -> None:
    """
    Build a compact node_index.json from cached citype_{TECH}.json files,
    mirroring the structure of the old metadata script.
    """
    index = {"types": {}}

    for fp in sorted(meta_dir.glob("citype_*.json")):
        try:
            meta = load_json(fp)
        except Exception as e:
            print(f"[WARN] Skipping malformed citype meta {fp}: {e}")
            continue

        technical = meta.get("technicalName") or fp.stem.replace("citype_", "")
        if not technical:
            continue

        meta_type = meta.get("type")
        labels = meta.get("labels") or []
        if not isinstance(labels, list):
            labels = []

        is_application = meta_type == "application"
        is_system = meta_type == "system"
        is_codelist = is_application and ("codelist" in [str(l).lower() for l in labels])

        entry = {
            "technicalName": technical,
            "name": meta.get("name"),
            "description": meta.get("description"),
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

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
    print(f"[META] Wrote node index: {out_path}")

# ------------------------------ Main ------------------------------
def main():
    ap = argparse.ArgumentParser(description="Build node attribute coverage and relation summaries from existing dumps.")
    ap.add_argument("--nodes-dir", default=str(NODES_DIR), help="Directory with node RAW JSONs (default: output/nodes)")
    ap.add_argument("--relations-dir", default=str(RELS_DIR), help="Directory with relation TABLE JSONs (default: output/relations)")
    ap.add_argument("--out-attrs", default=str(ATTRS_OUT_DIR), help="Output dir for node attribute JSONs (default: output/attributes)")
    ap.add_argument("--out-rel-attrs", default=str(REL_ATTRS_OUT_DIR), help="Output dir for relation summaries (default: output/relation_attributes)")
    ap.add_argument("--only-rel", default="", help="Regex to include only matching relations")
    ap.add_argument("--skip-rel", default=REL_EXCLUDE_REGEX, help="Regex to exclude relations (default excludes CMDB_*, LATEST_REQUEST, PREVIOUS_REQUEST)")
    ap.add_argument("--only-node", default="", help="Regex to include only matching node files")
    ap.add_argument("--skip-node", default="", help="Regex to exclude node files")
    args = ap.parse_args()

    nodes_dir = Path(args.nodes_dir)
    rels_dir = Path(args.relations_dir)
    out_attrs = Path(args.out_attrs)
    out_rel_attrs = Path(args.out_rel_attrs)

    only_rel = re.compile(args.only_rel) if args.only_rel else (re.compile(REL_INCLUDE_REGEX) if REL_INCLUDE_REGEX else None)
    skip_rel = re.compile(args.skip_rel) if args.skip_rel else None
    only_node = re.compile(args.only_node) if args.only_node else None
    skip_node = re.compile(args.skip_node) if args.skip_node else None

    if not nodes_dir.exists():
        print(f"[ERROR] Nodes dir not found: {nodes_dir}", file=sys.stderr)
        sys.exit(1)
    if not rels_dir.exists():
        print(f"[WARN ] Relations dir not found: {rels_dir}")

    print(f"[INFO] Nodes: {nodes_dir} -> {out_attrs}")
    process_nodes(nodes_dir, out_attrs, only=only_node, skip=skip_node)

    if rels_dir.exists():
        print(f"[INFO] Relations: {rels_dir} -> {out_rel_attrs}")
        process_relations(rels_dir, nodes_dir, out_rel_attrs, only=only_rel, skip=skip_rel)

    try:
        rebuild_stats_index(STATS_ROOT, STATS_ROOT / "index.json")
    except Exception as e:
        print(f"[WARN] Failed to rebuild stats index.json: {e}")

    # After building stats, refresh metadata indexes from cached citype/rel JSONs
    try:
        build_node_index_from_citypes(CITYPE_META_DIR, NODE_INDEX_PATH)
    except Exception as e:
        print(f"[WARN] Failed to build node_index.json: {e}")

    try:
        build_relation_index_from_meta(RELTYPE_META_DIR, REL_INDEX_PATH)
    except Exception as e:
        print(f"[WARN] Failed to build relation_index.json: {e}")

if __name__ == "__main__":
    main()