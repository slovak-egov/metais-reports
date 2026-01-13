from __future__ import annotations

import json
from pathlib import Path
from typing import Tuple

from metais.common.json_utils import load_json_file

META_KEYS_6: Tuple[str, ...] = (
    "owner",
    "state",
    "createdBy",
    "createdAt",
    "lastModifiedBy",
    "lastModifiedAt",
)

META_COLS = len(META_KEYS_6)

# ---- meta semantics (shared by nodes + relations) ----
STATE_KEY: str = "state"
INVALID_STATE: str = "INVALIDATED"

# Meta validity: nodes/relations are considered invalid when meta["state"] == "INVALIDATED".
# We precompute META_STATE_MIDX so hot loops can check validity without string lookups.
META_KEY_TO_INDEX: dict[str, int] = {k: i for i, k in enumerate(META_KEYS_6)}
META_STATE_MIDX: int = META_KEY_TO_INDEX[STATE_KEY]
assert META_KEYS_6[META_STATE_MIDX] == STATE_KEY

# deterministic JSON text (useful for pass0 stub)
META_KEYS_6_JSON = json.dumps(list(META_KEYS_6), ensure_ascii=False)

def load_meta_keys_strict(meta_json_path: Path) -> Tuple[str, ...]:
    j = load_json_file(meta_json_path)
    if not isinstance(j, list) or not all(isinstance(x, str) for x in j):
        raise TypeError(f"{meta_json_path} must be list[str]")

    keys = tuple(j)
    if keys != META_KEYS_6:
        raise ValueError(f"{meta_json_path} must equal {list(META_KEYS_6)}; got {list(keys)}")
    return keys