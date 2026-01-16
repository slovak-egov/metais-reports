from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from metais.common.fetch_http import get_json
from metais.common.http_config import HTTPConfig, open_http_cfg
from metais.common.step_marker import is_done, mark_done
from metais.common.directory_layout import DirectoryLayout
from metais.common.uri_config import URIConfig


def _extract_valid_codes_from_headers(headers_doc: Any) -> List[str]:
    codes: List[str] = []
    if not isinstance(headers_doc, dict):
        return codes

    codelists = headers_doc.get("codelists")
    if not isinstance(codelists, list):
        return codes

    for it in codelists:
        if not isinstance(it, dict):
            continue
        valid = it.get("valid", True)
        if valid is False:
            continue
        code = it.get("code", "")
        if isinstance(code, str) and code:
            codes.append(code)
    return codes


def _write_pretty_json(p: Path, j: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(j, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_codelist(layout: DirectoryLayout, uri_cfg: URIConfig, http_cfg: HTTPConfig) -> None:
    root = Path(getattr(layout, "codelists_root", layout.date_root / "codelists"))
    items_dir = Path(getattr(layout, "codelists_items_dir", root / "codelistitems"))
    headers_json = Path(getattr(layout, "codelists_headers_json", root / "codelists_headers.json"))

    if is_done(root):
        print(f"[CODELISTS] .done marker present in {root} - skipping.")
        return

    open_cfg = open_http_cfg(http_cfg)

    root.mkdir(parents=True, exist_ok=True)
    items_dir.mkdir(parents=True, exist_ok=True)

    # 1) headers list
    list_url = uri_cfg.codelist_headers_list_url()
    headers_doc = get_json(list_url, open_cfg)
    _write_pretty_json(headers_json, headers_doc)
    print(f"[CODELISTS] Saved headers -> {headers_json}")

    # 2) extract codes
    codes = _extract_valid_codes_from_headers(headers_doc)
    print(f"[CODELISTS] Will fetch {len(codes)} codelists.")

    # 3) items per code
    for code in codes:
        url = uri_cfg.codelist_items_url(code)
        items_doc = get_json(url, open_cfg)
        out_path = items_dir / f"{code}.json"
        _write_pretty_json(out_path, items_doc)
        print(f"[CODELISTS] {code} -> {out_path}")

    mark_done(root)
    print("[CODELISTS] Done.")
