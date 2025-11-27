#!/usr/bin/env python3
import sys
import os
import gzip
import shutil
import json
import argparse
from datetime import datetime
from pathlib import Path

from config_env import load_env_file  # same as in extract_nodes/relations


# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------

def check_date(date_str: str) -> None:
    try:
        datetime.strptime(date_str, "%d-%m-%Y")
    except ValueError:
        print(f"Invalid date '{date_str}': expected format dd-mm-yyyy and a real calendar date")
        sys.exit(1)


def find_project_root(start: Path) -> Path:
    """
    Same logic you use elsewhere: walk up until we find .git
    or fall back to the starting directory.
    """
    current = start
    while current != current.parent:
        if (current / ".git").exists():
            return current
        current = current.parent
    return start


def gzip_file(path: Path) -> None:
    """
    Compress a single .json file to .json.gz and delete the original.
    Idempotent w.r.t. existing .gz – caller should only pass plain .json.
    """
    if not path.is_file():
        return

    if path.suffix == ".gz":
        # shouldn't be passed here, but extra guard
        return

    gz_path = path.with_suffix(path.suffix + ".gz")
    if gz_path.exists():
        print(f"[SKIP] {gz_path.name} already exists, not overwriting.")
        return

    size_before = path.stat().st_size

    print(f"[GZIP] {path.name} → {gz_path.name}")
    with path.open("rb") as f_in, gzip.open(gz_path, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)

    size_after = gz_path.stat().st_size
    path.unlink()

    print(f"       {size_before/1024/1024:.2f} MB -> {size_after/1024/1024:.2f} MB")


def gzip_dir_json_files(root: Path, label: str) -> None:
    if not root.is_dir():
        print(f"[WARN] {label} dir {root} does not exist, skipping.")
        return

    print(f"[INFO] Gzipping *.json in {root} ({label})")
    count = 0
    for p in sorted(root.glob("*.json")):
        # ignore partial / scratch files if any pattern needed; for now gzip all
        gzip_file(p)
        count += 1

    print(f"[INFO] {label}: compressed {count} file(s).")


# ------------------------------------------------------------
# Main
# ------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Gzip raw MetaIS node/relation JSON files for a given snapshot date."
    )
    parser.add_argument(
        "date",
        help="Snapshot date in format dd-mm-yyyy (matches output/<DATE>/nodes, relations)",
    )
    args = parser.parse_args()

    DATE = args.date
    check_date(DATE)

    # Load env (.metais.env etc.)
    load_env_file()

    # Locate project root
    SCRIPT_DIR = Path(__file__).resolve().parent
    PROJECT_ROOT = find_project_root(SCRIPT_DIR)

    # Respect METAIS_RAW_ROOT (same as extract_nodes / extract_relations)
    raw_root_name = os.getenv("METAIS_RAW_ROOT", "output")
    RAW_ROOT = PROJECT_ROOT / raw_root_name

    DATE_ROOT = RAW_ROOT / DATE
    NODES_DIR = DATE_ROOT / "nodes"
    RELS_DIR  = DATE_ROOT / "relations"

    print(f"[INFO] PROJECT_ROOT: {PROJECT_ROOT}")
    print(f"[INFO] RAW_ROOT:     {RAW_ROOT}")
    print(f"[INFO] DATE_ROOT:    {DATE_ROOT}")

    if not DATE_ROOT.is_dir():
        print(f"[ERROR] Date root {DATE_ROOT} does not exist.")
        sys.exit(1)

    # Gzip nodes & relations
    gzip_dir_json_files(NODES_DIR, "nodes")
    gzip_dir_json_files(RELS_DIR, "relations")

    print("[DONE] Gzip of raw nodes/relations complete.")


if __name__ == "__main__":
    main()