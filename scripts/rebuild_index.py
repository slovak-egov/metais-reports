#!/usr/bin/env python3
import sys
import os
from pathlib import Path
from datetime import datetime
import json
from typing import Any, Dict, List, Optional


# ---------- Helpers shared with master_loader ----------

def check_date(date_str: str) -> None:
    try:
        datetime.strptime(date_str, "%d-%m-%Y")
    except ValueError:
        raise ValueError(
            f"Invalid date '{date_str}': expected format dd-mm-yyyy and a real calendar date"
        )


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


# ---------- Paths ----------

THIS_FILE     = Path(__file__).resolve()
PROJECT_ROOT  = THIS_FILE.parents[1]

env_path = os.getenv("META_VIZ_DATA_ROOT")
if env_path:
    METAVIZ_OUTPUT_ROOT = (PROJECT_ROOT / env_path).resolve()
else:
    METAVIZ_OUTPUT_ROOT = PROJECT_ROOT / "meta-viz" / "data"

INDEX_PATH = METAVIZ_OUTPUT_ROOT / "index.json"


# ---------- Core logic ----------

def build_snapshot(date: str) -> Optional[Dict[str, Any]]:
    """
    Build a single snapshot entry for the given date from the folder:
      METAVIZ_OUTPUT_ROOT / date / <category> / *.json

    Returns:
      {
        "date": "dd-mm-yyyy",
        "categories": {
          "<category>": [
            {"technicalName": "foo", "name": "Human name"},
            ...
          ]
        }
      }
    or None if the date directory doesn't exist.
    """
    date_dir = METAVIZ_OUTPUT_ROOT / date
    if not date_dir.is_dir():
        print(f"[index] WARNING: snapshot folder {date_dir} does not exist, skipping")
        return None

    categories: Dict[str, List[Dict[str, str]]] = {}

    # each subdir under date is a category (dataset, graph, ...)
    for category_dir in sorted(p for p in date_dir.iterdir() if p.is_dir()):
        category_name = category_dir.name
        instances: List[Dict[str, str]] = []

        for json_path in sorted(category_dir.glob("*.json")):
            technical_name = json_path.stem
            try:
                doc = load_json(json_path)
            except Exception as e:
                print(f"[index] WARNING: failed to load {json_path}: {e}")
                continue

            # Prefer explicit "name" from the dump; fall back to technical_name
            human_name = doc.get("name") or technical_name

            instances.append({
                "technicalName": technical_name,
                "name": human_name,
            })

        categories[category_name] = instances

    return {
        "date": date,
        "categories": categories,
    }


def find_all_snapshot_dates() -> List[str]:
    """
    Scan METAVIZ_OUTPUT_ROOT for subdirectories whose names look like dd-mm-yyyy.
    """
    dates: List[str] = []
    if not METAVIZ_OUTPUT_ROOT.is_dir():
        return dates

    for p in METAVIZ_OUTPUT_ROOT.iterdir():
        if not p.is_dir():
            continue
        name = p.name
        try:
            check_date(name)
        except ValueError:
            # Not a date directory, ignore
            continue
        dates.append(name)

    # sort newest first (optional but nice)
    dates.sort(key=lambda d: datetime.strptime(d, "%d-%m-%Y"), reverse=True)
    return dates


def rebuild_index(date: Optional[str] = None) -> None:
    """
    Rebuild meta-viz/data/index.json.

    - If date is None:
        Rebuild the whole index from all dd-mm-yyyy dirs under METAVIZ_OUTPUT_ROOT.
    - If date is provided:
        Rebuild/insert only that snapshot, keep other dates from existing index.
    """
    METAVIZ_OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

    if date is None:
        # Full rebuild
        print("[index] Full rebuild of index.json")
        snapshots: List[Dict[str, Any]] = []
        for d in find_all_snapshot_dates():
            snap = build_snapshot(d)
            if snap is not None:
                snapshots.append(snap)

    else:
        # Single-date update
        print(f"[index] Rebuilding index entry for {date}")
        try:
            check_date(date)
        except ValueError as e:
            print(f"[index] ERROR: {e}")
            sys.exit(1)

        # Load existing index (if any)
        if INDEX_PATH.exists():
            try:
                existing = load_json(INDEX_PATH)
                existing_snaps = existing.get("snapshots", [])
            except Exception as e:
                print(f"[index] WARNING: failed to read existing index.json: {e}")
                existing_snaps = []
        else:
            existing_snaps = []

        # Reindex existing snapshots into a dict by date
        by_date: Dict[str, Dict[str, Any]] = {
            snap.get("date"): snap for snap in existing_snaps
            if isinstance(snap, dict) and snap.get("date")
        }

        # Build / replace this date
        snap = build_snapshot(date)
        if snap is None:
            # If there's nothing on disk for that date, drop it from index if it existed
            by_date.pop(date, None)
        else:
            by_date[date] = snap

        # Rebuild list, sorted newest-first
        dates_sorted = sorted(
            by_date.keys(),
            key=lambda d: datetime.strptime(d, "%d-%m-%Y"),
            reverse=True
        )
        snapshots = [by_date[d] for d in dates_sorted]

    # Write final index
    payload = {"snapshots": snapshots}
    with INDEX_PATH.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"[index] Wrote {INDEX_PATH}")


# ---------- CLI entry point ----------
if __name__ == "__main__":
    if len(sys.argv) >= 2:
        date_arg = sys.argv[1]
        if date_arg.strip():
            rebuild_index(date_arg)
        else:
            rebuild_index(None)
    else:
        rebuild_index(None)