from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from metais.common.atomic_write import atomic_write_json
from .page_sink import PageSink, PageStats

from metais.common.shards import (
    shard_data_path,
    shard_meta_path,
)


def now_iso8601_local() -> str:
    """
      "%Y-%m-%dT%H:%M:%S%z" and then insert colon before last two digits -> +01:00
    """
    s = datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")  # e.g. +0100
    if len(s) >= 5:
        s = s[:-2] + ":" + s[-2:]
    return s


class ShardedNdjsonSink(PageSink):
    """
    Writes each page into:
      <pages_dir>/<base>.<offset_padded>.ndjson
      <pages_dir>/<base>.<offset_padded>.meta.json
    """
    def __init__(self, pages_dir: Path, base_name: str = "data", *, verbose: bool = False) -> None:
        self.pages_dir = Path(pages_dir)
        self.base_name = str(base_name)
        self.verbose = bool(verbose)

        self.pages_dir.mkdir(parents=True, exist_ok=True)

        self._current_offset: int = -1
        self._tmp_path: Optional[Path] = None
        self._final_path: Optional[Path] = None
        self._fh = None  # type: ignore[assignment]

    def begin_page(self, offset: int, limit: int) -> None:
        self._current_offset = int(offset)

        final_path = shard_data_path(self.pages_dir, self.base_name, self._current_offset)
        meta_final = shard_meta_path(self.pages_dir, self.base_name, self._current_offset)
        tmp_path = Path(str(final_path) + ".tmp")

        self._final_path = final_path
        self._tmp_path = tmp_path

        has_data = final_path.exists()
        has_meta = meta_final.exists()

        # Remove tmp from any previous crash
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass

        # Idempotency rules
        if has_data and has_meta:
            if self.verbose:
                print(f"[sink:{self.base_name}] skip offset={self._current_offset} (data+meta exist)")
            self._current_offset = -1
            self._final_path = None
            self._tmp_path = None
            return

        if has_data and not has_meta:
            if self.verbose:
                print(f"[sink:{self.base_name}] rewriting offset={self._current_offset} (data w/o meta)")
            try:
                final_path.unlink()
            except FileNotFoundError:
                pass

        if (not has_data) and has_meta:
            if self.verbose:
                print(f"[sink:{self.base_name}] rewriting offset={self._current_offset} (meta w/o data)")
            try:
                meta_final.unlink()
            except FileNotFoundError:
                pass

        # Open tmp for writing (fresh)
        self.close()
        self._fh = open(tmp_path, "wb")

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            finally:
                self._fh = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def write_item(self, obj: Any) -> None:
        if self._current_offset < 0:
            return  # skipped page
        if self._fh is None:
            raise RuntimeError("ShardedNdjsonSink.write_item called without an open page")

        line = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        self._fh.write(line.encode("utf-8"))
        self._fh.write(b"\n")

    def end_page(self, stats: PageStats) -> None:
        if self._current_offset < 0:
            return  # skipped

        if self._fh is None or self._tmp_path is None or self._final_path is None:
            raise RuntimeError("ShardedNdjsonSink.end_page called in invalid state")

        self.close()

        # Atomic replace
        os.replace(self._tmp_path, self._final_path)

        # Write meta (atomic)
        meta_final = shard_meta_path(self.pages_dir, self.base_name, self._current_offset)

        meta = {
            "offset": int(stats.offset),
            "limit": int(stats.limit),
            "received": int(stats.received),
            "seconds": float(stats.seconds),
            "timestamp": now_iso8601_local(),
        }

        atomic_write_json(meta_final, meta, ensure_ascii=False, indent=2)

        # reset
        self._current_offset = -1
        self._tmp_path = None
        self._final_path = None