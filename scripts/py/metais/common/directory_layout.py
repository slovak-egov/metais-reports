from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from metais.common.fs_utils import mkdir_all
from .paths_config import PathsConfig


@dataclass(slots=True)
class DirectoryLayout:
    cfg: PathsConfig
    dump_date: str
    project_root: Path

    # These are filled in post-init
    output_root: Path = field(init=False)
    date_root: Path = field(init=False)

    # enums (open API)
    metadata_root: Path = field(init=False)
    enums_root: Path = field(init=False)
    nodes_meta_dir: Path = field(init=False)
    rels_meta_dir: Path = field(init=False)

    # codelists (open API)
    codelists_root: Path = field(init=False)
    codelists_items_dir: Path = field(init=False)
    codelists_headers_json: Path = field(init=False)

    # top level packed
    packed_root: Path = field(init=False)
    dict_dir: Path = field(init=False)
    nodes_packed: Path = field(init=False)
    nodes_uuids_dir: Path = field(init=False)
    rels_uuids_dir: Path = field(init=False)
    rels_packed: Path = field(init=False)

    # raw json dumps
    raw_nodes_dir: Path = field(init=False)
    raw_rels_dir: Path = field(init=False)

    raw_nodes_pages_dir: Path = field(init=False)
    raw_rels_pages_dir: Path = field(init=False)
    raw_nodes_errors_dir: Path = field(init=False)
    raw_rels_errors_dir: Path = field(init=False)

    #tmp_dir: Path = field(init=False)

    # file paths
    rels_index_json: Path = field(init=False)
    citypes_list_json: Path = field(init=False)
    reltypes_list_json: Path = field(init=False)

    def __post_init__(self) -> None:
        pr = Path(self.project_root)
        cfg = self.cfg

        self.output_root = pr / cfg.output_root
        self.date_root = self.output_root / self.dump_date

        self.metadata_root = self.date_root / cfg.metadata_root
        self.enums_root = self.date_root / cfg.enums_root

        self.codelists_root = self.date_root / cfg.codelists_root
        self.codelists_items_dir = self.codelists_root / "codelistitems"
        self.codelists_headers_json = self.codelists_root / "codelistheaders.json"

        self.nodes_meta_dir = self.metadata_root / cfg.nodes_root
        self.rels_meta_dir = self.metadata_root / cfg.rels_root

        self.raw_nodes_dir = self.date_root / cfg.nodes_root
        self.raw_rels_dir = self.date_root / cfg.rels_root

        self.packed_root = self.date_root / cfg.packed_root
        self.dict_dir = self.packed_root / "dict"
        self.nodes_packed = self.packed_root / "nodes"
        self.nodes_uuids_dir = self.packed_root / "nodes_uuids"
        self.rels_uuids_dir = self.packed_root / "relations_uuids"
        self.rels_packed = self.packed_root / "relations"

        self.rels_index_json = self.rels_packed / "relations.json"

        self.raw_nodes_pages_dir = self.date_root / cfg.nodes_root / "pages"
        self.raw_rels_pages_dir = self.date_root / cfg.rels_root / "pages"
        self.raw_nodes_errors_dir = self.raw_nodes_pages_dir / "errors"
        self.raw_rels_errors_dir = self.raw_rels_pages_dir / "errors"

        self.citypes_list_json = self.metadata_root / "citypes_list.json"
        self.reltypes_list_json = self.metadata_root / "reltypes_list.json"

    def create_fetch_dirs(self, verbose: bool = True) -> None:
        dirs = [
            self.metadata_root, self.enums_root, self.nodes_meta_dir, self.rels_meta_dir,
            self.codelists_root, self.codelists_items_dir,
            self.raw_nodes_dir, self.raw_rels_dir,
            self.raw_nodes_pages_dir, self.raw_rels_pages_dir,
            self.raw_nodes_errors_dir, self.raw_rels_errors_dir,
        ]
        mkdir_all(dirs, strict=True, verbose_ok=verbose, tag="mkdir")

    def create_convert_dirs(self, verbose: bool = True) -> None:
        dirs = [
            self.packed_root,
            self.dict_dir,
            self.nodes_packed, self.nodes_uuids_dir,
            self.rels_packed, self.rels_uuids_dir,
        ]
        mkdir_all(dirs, strict=True, verbose_ok=verbose, tag="mkdir")
