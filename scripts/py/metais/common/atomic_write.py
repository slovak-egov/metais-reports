"""
os.replace = atomic commit, a single kernel operation that either runs or doesn't.
if it runs, we have a final file written with no tmp
if it fails to run, we have a tmp file and no final file. If we keep tmp files small and always overwrite them (never treat them as final), our routine is restarteable.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Optional, Union


def atomic_replace(src_tmp: Path, dst_final: Path) -> None:
    """
    Atomic replace (works on Windows too): dst_final will be overwritten if it exists.
    """
    dst_final.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src_tmp, dst_final)


def atomic_write_bytes(final_path: Path, data: bytes, *, fsync: bool = True) -> None:
    """
    Write bytes to <final_path> atomically via <final_path>.tmp then os.replace.
    """
    final_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(final_path) + ".tmp")

    try:
        tmp.unlink()
    except FileNotFoundError:
        pass

    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        if fsync:
            os.fsync(f.fileno())

    os.replace(tmp, final_path)


def atomic_write_file(path: Union[str, Path], write_fn: Callable[[Any], None], *, fsync: bool = True) -> None:
    atomic_write_with(Path(path), write_fn, fsync=fsync)


def atomic_write_text(
    final_path: Path,
    text: str,
    *,
    encoding: str = "utf-8",
    fsync: bool = True,
) -> None:
    """
    Encodes text to bytes and writes atomically
    """
    atomic_write_bytes(final_path, text.encode(encoding), fsync=fsync)


def atomic_write_json(
    final_path: Path,
    obj: Any,
    *,
    ensure_ascii: bool = False,
    indent: Optional[int] = 2,
    separators: Optional[tuple[str, str]] = None,
    sort_keys: bool = False,
    fsync: bool = True,
) -> None:
    s = json.dumps(
        obj,
        ensure_ascii=ensure_ascii,
        indent=indent,
        separators=separators,
        sort_keys=sort_keys,
    )
    """
    Serializes obj, then writes atomically
    """
    atomic_write_text(final_path, s, fsync=fsync)


def atomic_write_with(final_path: Path, write_fn: Callable[[Any], None], *, fsync: bool = True) -> None:
    """
    For cases where we want to stream-write into the tmp file ourselves.
    write_fn(file_handle) should write bytes to file_handle opened in "wb".
    ex:
    def write_u32(f):
        for x in range(10):
            f.write(U32_le.pack(x))
    """
    final_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(final_path) + ".tmp")

    try:
        tmp.unlink()
    except FileNotFoundError:
        pass

    with open(tmp, "wb") as f:
        write_fn(f)
        f.flush()
        if fsync:
            os.fsync(f.fileno())

    os.replace(tmp, final_path)