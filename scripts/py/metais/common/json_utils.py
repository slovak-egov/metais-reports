from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Union, List, Dict, Sequence

K_MAX_JSON_PREVIEW = 200


def _preview_json(obj: Any, max_len: int = K_MAX_JSON_PREVIEW) -> str:
    try:
        s = json.dumps(obj, ensure_ascii=False)
    except Exception:
        return "<dump failed>"
    return s[:max_len]


def load_json_file(filepath: Union[str, Path]) -> Any:
    """
    Load JSON from disk into Python objects (dict/list/str/...)
    Raises RuntimeError with a useful message on failure.
    """
    p = Path(filepath)
    try:
        with p.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError as e:
        raise RuntimeError(f"Cannot open JSON file: {p}") from e
    except OSError as e:
        raise RuntimeError(f"Cannot open JSON file: {p}: {e}") from e
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Failed to parse JSON from file {p}: {e}") from e


def extract_result_array(
    j: Any,
    *,
    keys: Sequence[str] = ("result", "results"),
) -> List[Any]:
    """
    Return a list from:
      - list -> itself
      - dict -> first found list under any key in `keys`
    Raise with a short preview otherwise.
    """
    if isinstance(j, list):
        return j

    if isinstance(j, dict):
        for k in keys:
            v = j.get(k)
            if isinstance(v, list):
                return v
        preview = _preview_json(j)
        raise RuntimeError(
            f"[JSON-extract_result_array] object without any of {tuple(keys)} arrays. preview: {preview}"
        )

    preview = _preview_json(j)
    raise RuntimeError(
        f"[JSON-extract_result_array] expected list or dict-with-list under {tuple(keys)}; "
        f"got type={type(j).__name__} preview: {preview}"
    )