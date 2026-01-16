from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from metais.common.http_config import HTTPConfig, open_http_cfg
from metais.common.step_marker import is_done, mark_done
from metais.fetch.fetch_open import OpenFetchingSpec, fetch_detail, fetch_element_list
from metais.common.directory_layout import DirectoryLayout
from metais.common.uri_config import URIConfig


def _pick_first_string_field(obj: Dict[str, Any], keys: list[str]) -> Optional[str]:
    for k in keys:
        v = obj.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _extract_citype_code(item: Dict[str, Any]) -> Optional[str]:
    return _pick_first_string_field(item, ["technicalName", "name", "code"])


def _extract_reltype_code(item: Dict[str, Any]) -> Optional[str]:
    return _pick_first_string_field(item, ["technicalName", "name", "code"])


def _path(layout: DirectoryLayout, attr: str, fallback: Path) -> Path:
    return Path(getattr(layout, attr, fallback))


def fetch_metadata(layout: DirectoryLayout, uri_cfg: URIConfig, http_cfg: HTTPConfig) -> None:
    meta_root = _path(layout, "metadata_root", layout.date_root / "metadata")
    nodes_meta_dir = _path(layout, "nodes_meta_dir", meta_root / "nodes")
    rels_meta_dir = _path(layout, "rels_meta_dir", meta_root / "relations")

    if is_done(meta_root):
        print(f"[META] .done marker present in {meta_root} - skipping.")
        return

    # no auth
    open_cfg = open_http_cfg(http_cfg)

    # CITYPES
    if is_done(nodes_meta_dir):
        print(f"[META] .done present in {nodes_meta_dir} - skipping citype metadata.")
    else:
        s = OpenFetchingSpec(
            out_dir=meta_root,
            out_filename="citypes_list.json",
            list_url=uri_cfg.citype_list_url(),
            detail_url_tpl=uri_cfg.citype_detail_url_tpl(),
            tag="META",
            kind="Citype",
            label="Citype list",
            strict_mkdir=True,
        )
        citypes = fetch_element_list(s, open_cfg, _extract_citype_code)
        print(f"[META] Will fetch metadata for {len(citypes)} citypes.")

        s.out_dir = nodes_meta_dir
        s.out_filename = ""
        for code in citypes:
            fetch_detail(code, open_cfg, s)

        mark_done(nodes_meta_dir)

    # RELTYPES
    if is_done(rels_meta_dir):
        print(f"[META] .done present in {rels_meta_dir} - skipping reltype metadata.")
    else:
        s = OpenFetchingSpec(
            out_dir=meta_root,
            out_filename="reltypes_list.json",
            list_url=uri_cfg.reltype_list_url(),
            detail_url_tpl=uri_cfg.reltype_detail_url_tpl(),
            tag="META",
            kind="Reltype",
            label="Reltype list",
            strict_mkdir=True,
        )
        reltypes = fetch_element_list(s, open_cfg, _extract_reltype_code)
        print(f"[META] Will fetch metadata for {len(reltypes)} reltypes.")

        s.out_dir = rels_meta_dir
        s.out_filename = ""
        for code in reltypes:
            fetch_detail(code, open_cfg, s)

        mark_done(rels_meta_dir)

    if is_done(nodes_meta_dir) and is_done(rels_meta_dir):
        mark_done(meta_root)
    else:
        print(f"[META] Not marking {meta_root} done because some substeps are incomplete.")
