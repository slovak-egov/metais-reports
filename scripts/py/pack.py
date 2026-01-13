#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from metais.common.date import today_date
from metais.common.project_root import find_project_root
from metais.common.paths_config import load_paths_config
from metais.common.directory_layout import DirectoryLayout

from metais.convert.pass0_bootstrap import bootstrap_packed_root
from metais.convert.pass1_prepass import run_prepass, freeze_schema
from metais.convert.pass2_pack import pack_nodes_and_relations
from metais.convert.pass3_finalize import finalize_relations, optimize_attributes


def main() -> int:
    ap = argparse.ArgumentParser(description="MetaIS convert pipeline (Python rewrite)")
    ap.add_argument("date", nargs="?", default=None, help="Dump date in DD-MM-YYYY (default: today).")
    ap.add_argument("--project-root", default=None, help="Override auto-detected project root.")
    ap.add_argument("--paths", default=None, help="Override path to paths.json")
    ap.add_argument("--skip-bad-json", action="store_true", help="Skip bad JSON records in pass2 instead of failing hard.")
    ap.add_argument("--force-prepass", action="store_true", help="Force rerun of pass 1 + 1.5 even if .pass1_5.done exists.")
    ap.add_argument("--no-finalize-rels", action="store_true", help="Skip finalize_relations.")
    ap.add_argument("--no-optimize-attrs", action="store_true", help="Skip optimize_attributes.")
    ap.add_argument("--quiet", action="store_true", help="Less logging.")
    args = ap.parse_args()

    verbose = not args.quiet

    dump_date = args.date or today_date()
    if verbose:
        print(f"[info] dump date = {dump_date}")

    cwd = Path.cwd()
    project_root = Path(args.project_root) if args.project_root else find_project_root(cwd)

    if verbose:
        print(f"[info] cwd          = {cwd}")
        print(f"[info] project_root = {project_root}")

    path_cfg = load_paths_config(args.paths, project_root=project_root, verbose=verbose)

    dir_layout = DirectoryLayout(cfg=path_cfg, dump_date=dump_date, project_root=project_root)

    if verbose:
        print(f"[info] date_root    = {dir_layout.date_root}")
        if hasattr(dir_layout, "raw_root"):
            print(f"[info] raw_root     = {dir_layout.raw_root}")
        if hasattr(dir_layout, "packed_root"):
            print(f"[info] packed_root  = {dir_layout.packed_root}")

    # Pass 0
    bootstrap_packed_root(dir_layout, verbose=verbose)

    # Pass 1 + 1.5
    run_prepass(dir_layout, force=args.force_prepass, verbose=verbose)
    freeze_schema(dir_layout, force=args.force_prepass, verbose=verbose)

    # Pass 2
    pack_nodes_and_relations(dir_layout, skip_bad_json=args.skip_bad_json, verbose=verbose)

    # Pass 3
    finalize_relations(dir_layout, do_finalize=not args.no_finalize_rels, verbose=verbose)
    optimize_attributes(dir_layout, do_optimize=not args.no_optimize_attrs, verbose=verbose)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        raise SystemExit(130)