#!/usr/bin/env python3
import os
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # go up to meta-viz/
STATS_ROOT = ROOT / "data" / "stats"
INDEX_PATH = STATS_ROOT / "index.json"


def main():
    snapshots = []
    if not STATS_ROOT.exists():
        print(f"{STATS_ROOT} does not exist, nothing to index.")
        return

    # Each entry under STATS_ROOT is expected to be a date folder:
    # data/stats/2025-11-10/attributes/*.json, relations/*.json, holistic/
    for date_dir in sorted(STATS_ROOT.iterdir()):
        if not date_dir.is_dir():
            continue

        date = date_dir.name  # e.g. "2025-11-10"

        attrs_dir = date_dir / "attributes"
        rels_dir = date_dir / "relation_attributes"

        node_types = []
        relation_types = []

        # node attributes (for Nodes view)
        if attrs_dir.is_dir():
            for f in sorted(attrs_dir.glob("*.json")):
                node_type = f.stem  # "KS.json" -> "KS"
                node_types.append(node_type)
        else:
            print(f"[WARN] {attrs_dir} not found or not a directory, no node stats for {date}.")

        # relation stats (for Relations view)
        if rels_dir.is_dir():
            for f in sorted(rels_dir.glob("*.json")):
                rel_name = f.stem  # "PO_je_gestor_KS.json" -> "PO_je_gestor_KS"
                relation_types.append(rel_name)
        else:
            # Not necessarily a problem; maybe you haven't generated relation stats yet.
            print(f"[INFO] {rels_dir} not found or not a directory, no relation stats for {date}.")

        if node_types or relation_types:
            snapshots.append({
                "date": date,
                "node_types": node_types,
                "relations": relation_types,
            })

    data = {"snapshots": snapshots}
    STATS_ROOT.mkdir(parents=True, exist_ok=True)
    with open(INDEX_PATH, "w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=2)

    print(f"Wrote {INDEX_PATH} with {len(snapshots)} snapshots.")


if __name__ == "__main__":
    main()