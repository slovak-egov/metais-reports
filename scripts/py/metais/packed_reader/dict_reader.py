"""
Usage:

    from packed_reader.dict_reader import DictReader

    dr = DictReader("output/packed/dict", cache_size=16384)
    try:
        n = dr.value_count
        obj = dr.get(12345)
        obj2 = dr.get(12345)  # cached
    finally:
        dr.close()
"""
import json
import mmap
from pathlib import Path
from os import PathLike
from typing import Any, Union

from .bin_formats import U64_LE
from .cache_utils import maybe_lru

class DictReader:
    def __init__(self, dict_dir: Union[str, PathLike, Path], cache_size: int = 4096):
        # accept str PathLike or Path, normalize on init
        dict_dir = Path(dict_dir)

        # initialize to None so close() can run safely even if __init__ errors mid-way
        self._dict_f = self._offs_f = self._dict_mm = self._offs_mm = self._get_cached = None

        try:
            self._dict_bin_path = dict_dir / "dict.bin"
            self._offsets_path  = dict_dir / "dict.offsets.bin"

            if not self._dict_bin_path.is_file():
                raise FileNotFoundError(self._dict_bin_path)
            if not self._offsets_path.is_file():
                raise FileNotFoundError(self._offsets_path)

            sz = self._offsets_path.stat().st_size
            if sz % U64_LE.size != 0:
                raise ValueError(f"dict.offsets.bin size not multiple of {U64_LE.size}: {sz}")

            # offsets length = value_count + 1 (sentinel)
            n_offsets = sz // U64_LE.size
            if n_offsets < 2:
                raise ValueError("dict.offsets.bin is empty / missing sentinel offset")

            self.value_count = n_offsets - 1

            self._dict_f = self._dict_bin_path.open("rb")
            self._offs_f = self._offsets_path.open("rb")

            self._dict_mm = mmap.mmap(self._dict_f.fileno(), 0, access=mmap.ACCESS_READ)
            self._offs_mm = mmap.mmap(self._offs_f.fileno(), 0, access=mmap.ACCESS_READ)

            end = self._read_offset(self.value_count)
            if end != len(self._dict_mm):
                raise ValueError("dict offsets sentinel does not match dict.bin size")

            self._get_cached = maybe_lru(cache_size, self._get_uncached)
        except Exception:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def close(self) -> None:
        errs = []

        def _close_attr(name: str):
            obj = getattr(self, name, None)
            if obj is None:
                return
            try:
                obj.close()
            except Exception as e:
                errs.append((name, e))
            finally:
                # Prevent double-close issues + break references for GC
                setattr(self, name, None)

        def _cache_clear_attr(name: str):
            obj = getattr(self, name, None)
            if obj is None:
                return
            try:
                cc = getattr(obj, "cache_clear", None)
                if cc is not None:
                    cc()
            except Exception as e:
                errs.append((name + ".cache_clear", e))
            finally:
                setattr(self, name, None)

        # Clear caches first (releases parsed JSON objects, etc.)
        _cache_clear_attr("_get_cached")

        # Close mmaps first, then files
        _close_attr("_dict_mm")
        _close_attr("_offs_mm")
        _close_attr("_dict_f")
        _close_attr("_offs_f")

        if errs:
            # Raise the first error; you can also include details from the rest if you want
            where, e = errs[0]
            raise RuntimeError(f"Error closing {where}: {e}") from e

    def _read_offset(self, i: int) -> int:
        # allow i == value_count (sentinel)
        if i < 0 or i > self.value_count:
            raise IndexError("dict offset index out of range")
        return U64_LE.unpack_from(self._offs_mm, i * U64_LE.size)[0]

    def _read_dict_bytes(self, idx: int) -> bytes:
        if idx < 0 or idx >= self.value_count:
            raise IndexError("dict index out of range")

        o0 = self._read_offset(idx)
        o1 = self._read_offset(idx + 1)

        if o1 < o0:
            raise ValueError("dict offsets not monotonic")
        if o1 > len(self._dict_mm):
            raise IOError("dict offset past end of dict.bin")

        return self._dict_mm[o0:o1]

    def _get_uncached(self, i: int) -> Any:
        raw = self._read_dict_bytes(i)
        return json.loads(raw.decode("utf-8"))

    def get(self, idx: int) -> Any:
        if self._get_cached is None:
            raise RuntimeError("DictReader is closed")
        return self._get_cached(idx)

    def get_bytes(self, idx: int) -> bytes:
        if self._dict_mm is None:
            raise RuntimeError("DictReader is closed")
        return self._read_dict_bytes(idx)