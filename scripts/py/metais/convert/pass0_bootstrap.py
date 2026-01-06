from __future__ import annotations

import os
import time
from pathlib import Path

from metais.common.atomic_write import atomic_write_text
from metais.common.step_marker import is_done, mark_done
from metais.common.packed_spec import META_KEYS_6_JSON


def _now_utc_like() -> str:
    return f"ctime={int(time.time())}"


def bootstrap_packed_root(layout, *, verbose: bool = False) -> None:
    """
    Pass 0: Bootstrap (no reads).
    Creates packed dirs + metaAttributes.json stubs + .pass0.done.
    """
    if is_done(layout.packed_root, ".pass0.done"):
        if verbose:
            print("[pass0] already done; skipping")
        return

    layout.create_convert_dirs(verbose=verbose)

    atomic_write_text(layout.nodes_packed / "metaAttributes.json", META_KEYS_6_JSON)
    atomic_write_text(layout.rels_packed  / "metaAttributes.json", META_KEYS_6_JSON)

    mark_done(layout.packed_root, ".pass0.done", "pass=0\n" + _now_utc_like())
    if verbose:
        print("[pass0] done")