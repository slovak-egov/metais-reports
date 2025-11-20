#!/usr/bin/env python3
import os
from pathlib import Path
from datetime import date

from config_env import load_env_file


def dir_is_stale(path: Path, pattern: str = "*.json", require_complete_flag: bool = False) -> bool:
    if not path.exists():
        return True

    if require_complete_flag:
        complete_flag = path / ".complete"
        if not complete_flag.exists():
            return True

    files = list(path.glob(pattern))
    if not files:
        return True

    today = date.today()
    latest_mtime = max(f.stat().st_mtime for f in files)
    latest_date = date.fromtimestamp(latest_mtime)
    return latest_date < today

def main():
    # 1) Load .env (if present)
    load_env_file()

    # 2) Compute today's date string (dd-mm-yyyy)
    today_str = os.getenv("METAIS_DATE") or date.today().strftime("%d-%m-%Y")
    os.environ.setdefault("METAIS_DATE", today_str)

    # 3) Base roots (relative to repo root, or just relative paths if you prefer)
    raw_root   = Path(os.getenv("METAIS_RAW_ROOT",   "output")) / today_str
    stats_root = Path(os.getenv("METAIS_STATS_ROOT", "meta-viz/data/stats")) / today_str
    meta_root  = Path(os.getenv("METAIS_METADATA_ROOT", "meta-viz/data/metadata")) / today_str

    nodes_dir = raw_root / "nodes"
    rels_dir  = raw_root / "relations"

    # 4) Expose dirs to sub-scripts
    os.environ.setdefault("METAIS_NODES_DIR", str(nodes_dir))
    os.environ.setdefault("METAIS_RELATIONS_DIR", str(rels_dir))
    os.environ.setdefault("NODES_DIR", str(nodes_dir))
    os.environ.setdefault("RELATIONS_DIR", str(rels_dir))

    os.environ.setdefault("METAIS_ATTRS_OUT_DIR",     str(stats_root / "nodes"))
    os.environ.setdefault("METAIS_REL_ATTRS_OUT_DIR", str(stats_root / "relations"))
    os.environ.setdefault("METAIS_LIST_CACHE_DIR",    str(meta_root / "nodes"))
    os.environ.setdefault("METAIS_TYPES_META_DIR",    str(meta_root / "nodes"))
    os.environ.setdefault("METAIS_REL_LIST_CACHE_DIR", str(meta_root / "relations"))

    nodes_dir.mkdir(parents=True, exist_ok=True)
    rels_dir.mkdir(parents=True, exist_ok=True)

    # *** IMPORTANT: import AFTER env is configured ***
    from raw_nodes import main as fetch_nodes_main
    from raw_relations import main as fetch_relations_main
    from calculate_reports import main as calc_reports_main

    if dir_is_stale(nodes_dir, require_complete_flag=True):
        print(f"[INFO] Node dumps in {nodes_dir} are stale or missing; fetching raw nodes...")
        fetch_nodes_main()
    else:
        print(f"[INFO] Node dumps in {nodes_dir} look complete and from today; skipping node fetch.")

    if dir_is_stale(rels_dir, require_complete_flag=True):
        print(f"[INFO] Relation dumps in {rels_dir} are stale or missing; fetching raw relations...")
        fetch_relations_main()
    else:
        print(f"[INFO] Relation dumps in {rels_dir} look complete and from today; skipping relation fetch.")

    print("[INFO] Calculating node/relationship reports...")
    calc_reports_main()


if __name__ == "__main__":
    main()