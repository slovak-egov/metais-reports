from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

from .project_root import find_project_root


@dataclass(slots=True)
class PathsConfig:
    output_root: str = "output"
    metadata_root: str = "metadata"
    enums_root: str = "enums"
    codelists_root: str = "codelists"
    nodes_root: str = "nodes"
    rels_root: str = "relations"
    packed_root: str = "packed"


def load_paths_config(
    filepath: Optional[Union[str, Path]] = None,
    *,
    project_root: Optional[Path] = None,
    verbose: bool = True,
) -> PathsConfig:
    cfg = PathsConfig()
    try:
        if filepath is None:
            root = project_root or find_project_root()
            filepath = root / "config" / "paths.json"
        else:
            filepath = Path(filepath)

        if not filepath.exists():
            if verbose:
                print(f"[paths_config] using defaults (missing: {filepath})", file=sys.stderr)
            return cfg

        with open(filepath, "r", encoding="utf-8") as f:
            j = json.load(f)

        if not isinstance(j, dict):
            if verbose:
                print(f"[paths_config] WARNING: {filepath} is not a JSON object; using defaults.", file=sys.stderr)
            return cfg

        # overrides...
        if isinstance(j.get("output_root"), str):
            cfg.output_root = j["output_root"]
        if isinstance(j.get("metadata_root"), str):
            cfg.metadata_root = j["metadata_root"]
        if isinstance(j.get("enums_root"), str):
            cfg.enums_root = j["enums_root"]
        if isinstance(j.get("codelists_root"), str):
            cfg.codelists_root = j["codelists_root"]
        if isinstance(j.get("nodes_root"), str):
            cfg.nodes_root = j["nodes_root"]
        if isinstance(j.get("rels_root"), str):
            cfg.rels_root = j["rels_root"]
        if isinstance(j.get("packed_root"), str):
            cfg.packed_root = j["packed_root"]

        if verbose:
            print(f"[paths_config] loaded {filepath}", file=sys.stderr)

    except Exception as e:
        if verbose:
            print(f"[paths_config] WARNING: {e} - using default paths.", file=sys.stderr)

    return cfg