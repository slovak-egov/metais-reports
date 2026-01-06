#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from metais.common.date import today_date
from metais.common.project_root import find_project_root
from metais.common.paths_config import load_paths_config
from metais.common.uri_config import load_uri_config
from metais.common.http_config import load_http_config, resolve_auth_inputs, get_bearer_token
from metais.common.directory_layout import DirectoryLayout
from metais.raw.sharded_ndjson_sink import ShardedNdjsonSink

from metais.fetch.fetch_enum import fetch_enum
from metais.fetch.fetch_codelist import fetch_codelist
from metais.fetch.fetch_metadata import fetch_metadata
from metais.fetch.fetch_raw import fetch_raw_nodes, fetch_raw_rels

def main() -> int:
    ap = argparse.ArgumentParser(description="MetaIS fetch pipeline (Python rewrite)")
    ap.add_argument("--date", default=None, help="Dump date in DD-MM-YYYY (default: today).")
    ap.add_argument("--project-root", default=None, help="Override auto-detected project root.")
    ap.add_argument("--paths", default=None, help="Override path to paths.json")
    ap.add_argument("--uri", default=None, help="Override path to URI.json")
    ap.add_argument("--http", default=None, help="Override path to http_config.json")
    ap.add_argument("--quiet", action="store_true", help="Less logging.")
    args = ap.parse_args()

    verbose = not args.quiet

    # 1) Resolve dump date
    dump_date = args.date or today_date()
    if verbose:
        print(f"[info] dump date = {dump_date}")

    # 2) Decide project root + cwd
    cwd = Path.cwd()
    project_root = Path(args.project_root) if args.project_root else find_project_root(cwd)

    if verbose:
        print(f"[info] cwd          = {cwd}")
        print(f"[info] project_root = {project_root}")

    # 3) Load configs (defaults if missing)
    # If caller didn't provide paths, these loaders should default to <project_root>/config/*.json
    path_cfg = load_paths_config(args.paths, project_root=project_root, verbose=verbose)
    uri_cfg  = load_uri_config(args.uri, project_root=project_root, verbose=verbose)
    http_cfg = load_http_config(args.http, project_root=project_root, verbose=verbose)

    # prompt for username/password if not in env vars
    auth_payload = resolve_auth_inputs(http_cfg.auth, verbose=verbose)

    if http_cfg.auth.mode != "none":
        get_bearer_token(auth_payload, http_cfg, base=uri_cfg.base_url, verbose=verbose)
        if verbose:
            print("[auth] bearer OK", file=sys.stderr)

    # 4) Build directory layout + create dirs
    dir_layout = DirectoryLayout(cfg=path_cfg, dump_date=dump_date, project_root=project_root)
    dir_layout.create_fetch_dirs(verbose=verbose)

    if verbose:
        print(f"[info] date_root    = {dir_layout.date_root}")

    # 5) create data sinks
    nodes_sink = ShardedNdjsonSink(dir_layout.raw_nodes_pages_dir, base_name="nodes", verbose=verbose)
    rels_sink  = ShardedNdjsonSink(dir_layout.raw_rels_pages_dir,  base_name="rels", verbose=verbose)

    # 6) fetch enums/codelists/metadata
    fetch_enum(dir_layout, uri_cfg, http_cfg)
    fetch_codelist(dir_layout, uri_cfg, http_cfg)
    fetch_metadata(dir_layout, uri_cfg, http_cfg)

    # 7) fetch raw nodes/rels
    fetch_raw_nodes(dir_layout, uri_cfg, http_cfg, nodes_sink, verbose=verbose)
    fetch_raw_rels (dir_layout, uri_cfg, http_cfg, rels_sink,  verbose=verbose)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        raise SystemExit(130)