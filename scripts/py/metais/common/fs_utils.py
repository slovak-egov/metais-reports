from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Union

PathLike = Union[str, Path]


def _fsbytes_repr(p: Path) -> str:
    """
    Helpful when debugging weird filesystem paths (control chars, bad surrogates, etc.).
    This shows the OS-level encoded bytes representation.
    """
    try:
        return repr(os.fsencode(str(p)))
    except Exception:
        # Fallback: best-effort string repr
        return repr(str(p))


def ensure_dir(
    dir_path: PathLike,
    *,
    strict: bool = True,
    warn_if_created: bool = False,
    verbose_ok: bool = False,
    tag: str = "mkdir",
) -> bool:
    """
    Ensure directory exists.

    Returns:
      True  if the directory was (likely) created by this call
      False if it already existed (or if non-strict mode failed and we gave up)

    Behavior:
      - If path exists but is not a directory:
          strict=True  -> raise
          strict=False -> warn and return False
      - If mkdir fails:
          strict=True  -> raise
          strict=False -> warn and return False
    """
    p = Path(dir_path)

    existed_before = p.exists()
    if existed_before:
        if p.is_dir():
            return False
        msg = f"Path exists but is not a directory: {p} (fsbytes={_fsbytes_repr(p)})"
        if strict:
            raise RuntimeError(msg)
        print(f"[{tag}] WARNING: {msg}")
        return False

    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        msg = f"Failed to create directory '{p}': {e} (fsbytes={_fsbytes_repr(p)})"
        if strict:
            raise RuntimeError(msg) from e
        print(f"[{tag}] WARNING: {msg}")
        return False

    # If we got here, it should exist; if not, something is seriously wrong.
    if not p.is_dir():
        msg = f"Path exists but is not a usable directory: {p} (fsbytes={_fsbytes_repr(p)})"
        if strict:
            raise RuntimeError(msg)
        print(f"[{tag}] WARNING: {msg}")
        return False

    created = True  # best-effort; raced mkdir could make this “not really created”
    if warn_if_created:
        print(f"[{tag}] WARNING: output dir did not exist; created: {p}")
    if verbose_ok:
        print(f"[{tag}] ok {p}")
    return created


def mkdir_all(
    dirs: Iterable[PathLike],
    *,
    strict: bool = True,
    warn_if_created: bool = False,
    verbose_ok: bool = False,
    tag: str = "mkdir",
) -> None:
    for d in dirs:
        ensure_dir(
            d,
            strict=strict,
            warn_if_created=warn_if_created,
            verbose_ok=verbose_ok,
            tag=tag,
        )