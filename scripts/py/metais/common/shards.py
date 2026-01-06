from __future__ import annotations

from pathlib import Path
from typing import Optional
from dataclasses import dataclass

K_SHARD_PAD = 9


@dataclass(frozen=True, slots=True)
class ShardInfo:
    offset: int
    ndjson_path: Path
    meta_path: Path


def shard_data_path(pages_dir: Path, base: str, offset: int, *, pad: int = K_SHARD_PAD) -> Path:
    return Path(pages_dir) / f"{base}.{offset:0{pad}d}.ndjson"

def shard_meta_path(pages_dir: Path, base: str, offset: int, *, pad: int = K_SHARD_PAD) -> Path:
    return Path(pages_dir) / f"{base}.{offset:0{pad}d}.meta.json"

def parse_offset_from_meta_filename(fname: str, base: str, *, pad: int = K_SHARD_PAD) -> Optional[int]:
    prefix = f"{base}."
    suffix = ".meta.json"

    if not fname.startswith(prefix) or not fname.endswith(suffix):
        return None

    mid = fname[len(prefix) : -len(suffix)]
    if len(mid) != pad or not mid.isdigit():
        return None

    # int(mid) cannot really fail after isdigit, but keep it robust.
    try:
        return int(mid)
    except ValueError:
        return None

def list_shards_by_meta(pages_dir: Path, base: str) -> list[ShardInfo]:
    pages_dir = Path(pages_dir)
    if not pages_dir.exists():
        raise FileNotFoundError(f"Pages dir does not exist: {pages_dir}")

    out: list[ShardInfo] = []
    for entry in pages_dir.iterdir():
        if not entry.is_file():
            continue
        off = parse_offset_from_meta_filename(entry.name, base)
        if off is None:
            continue

        meta_p = shard_meta_path(pages_dir, base, off)
        ndjson_p = shard_data_path(pages_dir, base, off)

        if not ndjson_p.exists():
            raise RuntimeError(f"Found meta shard but missing data file: {ndjson_p}")

        out.append(ShardInfo(offset=off, ndjson_path=ndjson_p, meta_path=meta_p))

    out.sort(key=lambda s: s.offset)
    return out