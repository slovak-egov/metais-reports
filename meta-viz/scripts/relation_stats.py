#!/usr/bin/env python3
import argparse
import json
import os
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Tuple, Set


# ---------- low-level helpers ----------

def load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_result_array(doc: Any) -> List[Dict[str, Any]]:
    # as in your other scripts
    if isinstance(doc, dict) and isinstance(doc.get("result"), list):
        return doc["result"]
    if isinstance(doc, list):
        return doc
    raise ValueError("Unrecognized raw JSON format for nodes.")


def build_uuid_set_for_type(path: str) -> Set[str]:
    """Return the set of uuids from a node dump."""
    doc = load_json(path)
    uuids = set()
    for o in get_result_array(doc):
        u = o.get("uuid")
        if u:
            uuids.add(str(u))
    return uuids


def parse_relation_table(path: str) -> List[Tuple[str, str]]:
    """
    Parse relation TABLE JSON to a list of (col0_uuid, col1_uuid).
    We do not assume which side is PO/KS yet; we'll infer from node sets.
    """
    doc = load_json(path)
    if doc.get("type") != "TABLE":
        raise ValueError(f"{path} is not a TABLE relation JSON.")

    res = doc.get("result") or {}
    headers = res.get("headers") or []
    rows = res.get("rows") or []

    if len(headers) < 2:
        raise ValueError("Relation TABLE must have at least two columns.")

    pairs: List[Tuple[str, str]] = []
    for row in rows:
        vals = row.get("values") or []
        if len(vals) < 2:
            continue
        u0 = str(vals[0]).strip()
        u1 = str(vals[1]).strip()
        if u0 and u1:
            pairs.append((u0, u1))

    return pairs


def classify_cardinality(src_degrees: Dict[str, int],
                         tgt_degrees: Dict[str, int]) -> str:
    """Return a string: 'empty', 'one-to-one', 'one-to-many', 'many-to-one', 'many-to-many'."""
    if not src_degrees and not tgt_degrees:
        return "empty"

    src_max = max(src_degrees.values()) if src_degrees else 0
    tgt_max = max(tgt_degrees.values()) if tgt_degrees else 0

    if src_max <= 1 and tgt_max <= 1:
        return "one-to-one"
    if src_max > 1 and tgt_max <= 1:
        return "one-to-many"
    if src_max <= 1 and tgt_max > 1:
        return "many-to-one"
    return "many-to-many"


# ---------- main stats function ----------

def compute_relation_stats(
    relation_path: Path,
    source_nodes_path: Path,
    target_nodes_path: Path,
    snapshot_date: str,
) -> Dict[str, Any]:
    relation_name = relation_path.stem  # e.g. PO_je_gestor_KS
    source_type = source_nodes_path.stem  # PO
    target_type = target_nodes_path.stem  # KS

    # Load all node uuids
    src_uuids = build_uuid_set_for_type(str(source_nodes_path))
    tgt_uuids = build_uuid_set_for_type(str(target_nodes_path))

    # Parse raw pairs as they appear in the TABLE
    raw_pairs = parse_relation_table(str(relation_path))

    # Try to orient them as (source_uuid, target_uuid)
    oriented_pairs: List[Tuple[str, str]] = []
    misclassified = 0

    for u0, u1 in raw_pairs:
      # Heuristic: if u0 is in src_uuids and u1 in tgt_uuids, great.
      # Else if u1 in src_uuids and u0 in tgt_uuids, swap.
      # If neither combination matches, we keep (u0,u1) as-is but count it as ambiguous.
        if u0 in src_uuids and u1 in tgt_uuids:
            oriented_pairs.append((u0, u1))
        elif u1 in src_uuids and u0 in tgt_uuids:
            oriented_pairs.append((u1, u0))
        else:
            oriented_pairs.append((u0, u1))
            misclassified += 1

    # Degree counts
    src_deg = Counter()  # PO -> how many KS
    tgt_deg = Counter()  # KS -> how many PO

    # Duplicate edges detection: same (src,tgt) more than once
    pair_counts = Counter()

    for s, t in oriented_pairs:
        src_deg[s] += 1
        tgt_deg[t] += 1
        pair_counts[(s, t)] += 1

    total_edges = len(oriented_pairs)
    unique_pairs = sum(1 for c in pair_counts.values() if c >= 1)
    duplicate_edges = sum(c - 1 for c in pair_counts.values() if c > 1)
    pairs_with_duplicates = sum(1 for c in pair_counts.values() if c > 1)

    def summarize_side(
        total_nodes: int,
        degrees: Counter,
        node_type: str,
    ) -> Dict[str, Any]:
        if degrees:
            deg_values = list(degrees.values())
            connected = len(degrees)
            deg_min = min(deg_values)
            deg_max = max(deg_values)
            deg_avg = sum(deg_values) / len(deg_values)
        else:
            connected = 0
            deg_min = 0
            deg_max = 0
            deg_avg = 0.0

        return {
            "type": node_type,
            "total_nodes": total_nodes,
            "connected_nodes": connected,
            "degree_min": deg_min,
            "degree_max": deg_max,
            "degree_avg": deg_avg,
        }

    # Node counts = all nodes of that type in the snapshot
    src_total_nodes = len(src_uuids)
    tgt_total_nodes = len(tgt_uuids)

    # Cardinality
    cardinality = classify_cardinality(src_deg, tgt_deg)

    stats = {
        "snapshot": snapshot_date,
        "relation_name": relation_name,
        "source_type": source_type,
        "target_type": target_type,
        "edges": {
            "total_edges": total_edges,
            "unique_pairs": unique_pairs,
            "duplicate_edges": duplicate_edges,
            "pairs_with_duplicates": pairs_with_duplicates,
            "ambiguous_pairs": misclassified
        },
        "source": summarize_side(src_total_nodes, src_deg, source_type),
        "target": summarize_side(tgt_total_nodes, tgt_deg, target_type),
        "cardinality": cardinality,
    }

    return stats


def main():
  ap = argparse.ArgumentParser(
      description="Compute relation-level stats (degrees, duplicates, cardinality)."
  )
  ap.add_argument("snapshot", help="Snapshot date (e.g. 2025-11-10)")
  ap.add_argument("relation", help="Relation JSON name without path, e.g. PO_je_gestor_KS")
  ap.add_argument("source_type", help="Source node type, e.g. PO")
  ap.add_argument("target_type", help="Target node type, e.g. KS")
  args = ap.parse_args()

  root = Path(__file__).resolve().parents[1]  # meta-viz/
  # adjust these if your raw data is elsewhere
  rel_path = root / "output" / "relations" / f"{args.relation}.json"
  src_nodes_path = root / "output" / "nodes" / f"{args.source_type}.json"
  tgt_nodes_path = root / "output" / "nodes" / f"{args.target_type}.json"

  if not rel_path.is_file():
      raise FileNotFoundError(f"Relation file not found: {rel_path}")
  if not src_nodes_path.is_file():
      raise FileNotFoundError(f"Source nodes file not found: {src_nodes_path}")
  if not tgt_nodes_path.is_file():
      raise FileNotFoundError(f"Target nodes file not found: {tgt_nodes_path}")

  stats = compute_relation_stats(
      relation_path=rel_path,
      source_nodes_path=src_nodes_path,
      target_nodes_path=tgt_nodes_path,
      snapshot_date=args.snapshot,
  )

  # write under data/stats/<snapshot>/relations/<relation>.json
  out_dir = root / "data" / "stats" / args.snapshot / "relations"
  out_dir.mkdir(parents=True, exist_ok=True)
  out_path = out_dir / f"{args.relation}.json"
  with open(out_path, "w", encoding="utf-8") as f:
      json.dump(stats, f, ensure_ascii=False, indent=2)

  print(f"Wrote relation stats to {out_path}")


if __name__ == "__main__":
  main()