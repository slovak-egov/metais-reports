# scripts/py/repack.py  (CLI wrapper)
from __future__ import annotations
import argparse, sys
from pathlib import Path

from metais.common.date import today_date
from metais.common.project_root import find_project_root
from metais.common.paths_config import load_paths_config
from metais.common.directory_layout import DirectoryLayout

from metais.convert.pass0_bootstrap import bootstrap_packed_root
from metais.convert.pass1_prepass import run_prepass, freeze_schema
from metais.convert.pass2_pack import pack_nodes_and_relations
from metais.convert.pass3_finalize import finalize_relations, optimize_attributes

from metais.repack.helper import load_uuid_list


def main() -> int:
    ap = argparse.ArgumentParser(description="MetaIS repack (raw -> packed subset)")
    ap.add_argument("date", nargs="?", default=None, help="DD-MM-YYYY (default: today if exists / latest)")
    ap.add_argument("--project-root", default=None)
    ap.add_argument("--packed-dest", default=None, help="Destination packed root (default: meta-viz/data/<DATE>/repack/packed)")
    ap.add_argument("--force-dest", action="store_true", help="Allow writing into an existing packed dest")
    ap.add_argument("--paths", default=None)
    ap.add_argument("--nodes", required=True, help="File with node UUIDs (one per line or JSON list)")
    ap.add_argument("--rels", required=True, help="File with relation UUIDs (one per line or JSON list)")
    ap.add_argument("--skip-bad-json", action="store_true")
    ap.add_argument("--force-prepass", action="store_true")
    ap.add_argument("--no-finalize-rels", action="store_true")
    ap.add_argument("--no-optimize-attrs", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    verbose = not args.quiet
    cwd = Path.cwd()
    project_root = Path(args.project_root) if args.project_root else find_project_root(cwd)

    dump_date = args.date or today_date()
    path_cfg = load_paths_config(args.paths, project_root=project_root, verbose=verbose)

    if args.packed_dest:
        packed_dest = Path(args.packed_dest)
        if not packed_dest.is_absolute():
            packed_dest = project_root / packed_dest
    else:
        packed_dest = project_root / "meta-viz" / "data" / dump_date / "repack" / "packed"

    layout = DirectoryLayout(cfg=path_cfg, dump_date=dump_date, project_root=project_root, packed_root_override=packed_dest)

    if layout.packed_root.exists() and any(layout.packed_root.iterdir()) and not args.force_dest:
        print(f"[error] packed dest not empty: {layout.packed_root} (use --force-dest)")
        return 2

    node_uuid_allow = load_uuid_list(Path(args.nodes))
    rel_uuid_allow  = load_uuid_list(Path(args.rels))

    bootstrap_packed_root(layout, verbose=verbose)

    run_prepass(layout, force=args.force_prepass, verbose=verbose,
                node_uuid_allow=node_uuid_allow, rel_uuid_allow=rel_uuid_allow)
    freeze_schema(layout, force=args.force_prepass, verbose=verbose,
                  node_uuid_allow=node_uuid_allow, rel_uuid_allow=rel_uuid_allow)

    pack_nodes_and_relations(layout, skip_bad_json=args.skip_bad_json, verbose=verbose,
                             node_uuid_allow=node_uuid_allow, rel_uuid_allow=rel_uuid_allow)

    finalize_relations(layout, do_finalize=not args.no_finalize_rels, verbose=verbose)
    optimize_attributes(layout, do_optimize=not args.no_optimize_attrs, verbose=verbose)
    return 0

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        raise SystemExit(130)
