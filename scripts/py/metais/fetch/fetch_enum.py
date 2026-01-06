from __future__ import annotations

import json
import copy
from pathlib import Path
from typing import Any, Dict, Optional

from metais.common.http_config import HTTPConfig, open_http_cfg
from metais.common.step_marker import is_done, mark_done
from metais.common.directory_layout import DirectoryLayout
from metais.common.uri_config import URIConfig
from metais.fetch.fetch_open import OpenFetchingSpec, fetch_detail, fetch_element_list


def _extract_enum_code_valid_only(item: Dict[str, Any]) -> Optional[str]:
    valid = item.get("valid", False)
    if valid is not True:
        return None
    code = item.get("code", "")
    return code if isinstance(code, str) and code else None


def _merge_enum_items_into(enum_name: str, enum_items: Any, enum_merged: dict[str, list[str]]) -> None:
    if not isinstance(enum_items, list):
        raise RuntimeError(f"[ENUMS] Expected enumItems array for {enum_name}, got type={type(enum_items).__name__}")

    for enum_item in enum_items:
        if not isinstance(enum_item, dict):
            continue

        enum_key = enum_item.get("code", "")
        enum_value = enum_item.get("value", "")

        if not isinstance(enum_key, str) or not enum_key:
            print("[WARNING] Empty string in enum key. Skipping")
            continue

        vec = enum_merged.setdefault(enum_key, [])
        vec.append(enum_name)
        vec.append(enum_value if isinstance(enum_value, str) else "")


def _handle_merged_enums(enum_merged: dict[str, list[str]]) -> dict[str, list[str]]:
    enum_collisions: dict[str, list[str]] = {}

    for key, vec in list(enum_merged.items()):
        if not vec:
            continue

        if len(vec) % 2 != 0:
            print(f"[ENUMS] WARNING: odd-length value array for key '{key}' (size={len(vec)}). Ignoring last.")
            vec.pop()

        pair_count = len(vec) // 2
        if pair_count <= 0:
            continue

        if pair_count > 1:
            enum_collisions[key] = list(vec)

        # keep only last pair
        last_enum = vec[-2]
        last_value = vec[-1]
        enum_merged[key] = [last_enum, last_value]

    return enum_collisions


def fetch_enum(layout: DirectoryLayout, uri_cfg: URIConfig, http_cfg: HTTPConfig) -> None:
    enums_root = Path(getattr(layout, "enums_root", layout.date_root / "enums"))

    if is_done(enums_root):
        print(f"[ENUMS] .done marker present in {enums_root} - skipping.")
        return

    open_cfg = open_http_cfg(http_cfg)

    s = OpenFetchingSpec(
        out_dir=enums_root,
        out_filename="enums_list.json",
        list_url=uri_cfg.enum_list_url(),
        detail_url_tpl=uri_cfg.enum_detail_url_tpl(),
        tag="ENUM",
        kind="Enum",
        label="Enum list",
        strict_mkdir=True,
        transform=lambda d: d.get("enumItems", []),
    )

    enum_codes = fetch_element_list(s, open_cfg, _extract_enum_code_valid_only)

    s.out_dir = enums_root / "valid"
    s.out_filename = ""

    enum_merged: dict[str, list[str]] = {}
    for enum_name in enum_codes:
        enum_items = fetch_detail(enum_name, open_cfg, s)
        _merge_enum_items_into(enum_name, enum_items, enum_merged)

    enum_collisions = _handle_merged_enums(enum_merged)

    merged_js: dict[str, str] = {}
    for key, vec in enum_merged.items():
        if len(vec) >= 2:
            merged_js[key] = vec[1]

    merged_path = enums_root / "enums_merged.json"
    merged_path.write_text(json.dumps(merged_js, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[ENUMS] Saved enums_merged.json -> {merged_path}")

    if enum_collisions:
        collisions_js = []
        for key, vec in enum_collisions.items():
            entry = {"item_code": key, "sources": []}
            for i in range(0, len(vec) - 1, 2):
                entry["sources"].append({"enum": vec[i], "value": vec[i + 1]})
            collisions_js.append(entry)

        collisions_path = enums_root / "enums_collisions.json"
        collisions_path.write_text(json.dumps(collisions_js, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[ENUMS] Saved enums_collisions.json -> {collisions_path}")

    mark_done(enums_root)