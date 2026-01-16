from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from metais.common.json_utils import extract_result_array
from metais.common.fs_utils import ensure_dir
from metais.common.fetch_http import get_json
from metais.common.http_config import HTTPConfig


JsonT = Any



@dataclass(slots=True)
class OpenFetchingSpec:
    # I/O
    out_dir: Path
    out_filename: str

    # URLs
    list_url: str
    detail_url_tpl: str  # contains "{name}"

    # logging/meta
    tag: str = "OPEN"
    kind: str = "Item"
    label: str = "List"
    strict_mkdir: bool = True
    warn_if_created: bool = False
    log_received: bool = True
    log_written: bool = True

    # transform detail payload before writing (identity by default)
    transform: Callable[[JsonT], JsonT] = field(default_factory=lambda: (lambda d: d))


def fetch_element_list(
    spec: OpenFetchingSpec,
    http_cfg: HTTPConfig,
    extract_id: Callable[[Dict[str, Any]], Optional[str]],
) -> List[str]:
    raw_doc = get_json(spec.list_url, http_cfg)
    raw = extract_result_array(raw_doc, keys=("result", "results", "data", "items"))

    if spec.log_received:
        print(f"[{spec.tag}] {spec.label}: received {len(raw)} raw entries from {spec.list_url}")

    ensure_dir(spec.out_dir, strict=spec.strict_mkdir, warn_if_created=spec.warn_if_created, tag=spec.tag)

    list_path = Path(spec.out_dir) / spec.out_filename
    list_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

    if spec.log_written:
        print(f"[{spec.tag}] Saved -> {list_path}")

    ids: List[str] = []
    ids.reserve(len(raw)) if hasattr(ids, "reserve") else None  # harmless no-op on CPython
    for item in raw:
        if not isinstance(item, dict):
            continue
        idv = extract_id(item)
        if idv:
            ids.append(idv)
    return ids


def fetch_detail(
    detail_api_code: str,
    http_cfg: HTTPConfig,
    spec: OpenFetchingSpec,
) -> JsonT:
    url = spec.detail_url_tpl.replace("{name}", detail_api_code)
    detail = get_json(url, http_cfg)

    if spec.log_received:
        print(f"[{spec.tag}] received {detail_api_code} from {url}")

    ensure_dir(spec.out_dir, strict=spec.strict_mkdir, warn_if_created=spec.warn_if_created, tag=spec.tag)

    fname = f"{detail_api_code}.json"
    out_path = Path(spec.out_dir) / fname
    payload = spec.transform(detail)

    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    if spec.log_written:
        print(f"[{spec.tag}] {spec.kind} {detail_api_code} -> {out_path}")

    return payload
