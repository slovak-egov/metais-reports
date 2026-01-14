"""
Usage:

    from packed_reader.resolver import GlobalResolver, LocalResolver

    with GlobalResolver("packed/uuids", cache_size=65536) as gr:
        gid = gr.find_gid("3f2504e0-4f89-11d3-9a0c-0305e82c3301")
        if gid is not None:
            citype_name, local_index = gr.resolve_gid_full(gid)
            print(gid, citype_name, local_index)

    with LocalResolver("packed/nodes/KS", cache_size=65536) as lr:
        gid2 = lr.find_gid("3f2504e0-4f89-11d3-9a0c-0305e82c3301")
        if gid2 is not None:
            print("KS gid:", gid2)
"""

from __future__ import annotations

import json
import mmap
import uuid as _uuid
from pathlib import Path
from os import PathLike
from typing import Optional, Union, Tuple

from metais.common.uuid_search import uuid_at, find_uuid_index
from .cache_utils import maybe_lru
from .bin_formats import (
    U16_LE, U32_LE,
    UUID_U128_BE, UUID_BYTES,
    RESOLVER_ROW, RESOLVER_ROW_BYTES,
    Uuid128, UuidLike, normalize_uuid,
)

class GlobalResolver:
    """
    Global UUID resolver.

    Files:
      - <uuids_file>    : UUID_U128_BE x N, sorted
      - resolver.bin    : rows (type_index U16, local_index U32), aligned
      - <names_file>    : list[str] mapping type_index -> type name
    """

    def __init__(
        self,
        uuids_dir: Union[str, PathLike, Path],
        cache_size: int = 4096,
        *,
        uuids_file: str = "uuids.bin",
        names_file: str = "citypes.json",
    ):
        uuids_dir = Path(uuids_dir)

        self._uu_f = self._res_f = None
        self._uu_mm = self._res_mm = None

        self.type_names: list[str] = []
        self.node_count: int = 0

        self._find_gid_cached = None
        self._resolve_uuid_cached = None
        self._uuid_obj_cached = None

        try:
            uu_path = uuids_dir / uuids_file
            res_path = uuids_dir / "resolver.bin"
            names_path = uuids_dir / names_file

            for p in (uu_path, res_path, names_path):
                if not p.is_file():
                    raise FileNotFoundError(p)

            self.type_names = json.loads(names_path.read_text("utf-8"))
            if not isinstance(self.type_names, list) or not all(isinstance(x, str) for x in self.type_names):
                raise TypeError(f"{names_file} must be JSON list[str]")

            self._uu_f = uu_path.open("rb")
            self._res_f = res_path.open("rb")

            self._uu_mm = mmap.mmap(self._uu_f.fileno(), 0, access=mmap.ACCESS_READ)
            self._res_mm = mmap.mmap(self._res_f.fileno(), 0, access=mmap.ACCESS_READ)

            uu_sz = len(self._uu_mm)
            res_sz = len(self._res_mm)

            if uu_sz % UUID_BYTES != 0:
                raise ValueError(f"uuids.bin size not multiple of {UUID_BYTES}: {uu_sz}")
            if res_sz % RESOLVER_ROW_BYTES != 0:
                raise ValueError(f"resolver.bin size not multiple of {RESOLVER_ROW_BYTES}: {res_sz}")

            n_uu = uu_sz // UUID_BYTES
            n_res = res_sz // RESOLVER_ROW_BYTES
            if n_uu != n_res:
                raise ValueError(f"uuids.bin rows={n_uu} but resolver.bin rows={n_res}")

            self.node_count = n_uu

            # cache on (hi, lo) tuple keys
            self._find_gid_cached = maybe_lru(cache_size, self._find_gid_by_hi_lo)
            self._resolve_uuid_cached = maybe_lru(cache_size, self._resolve_uuid_by_hi_lo)
            self._uuid_obj_cached = maybe_lru(cache_size, self._uuid_obj_for_gid)

        except Exception:
            self.close()
            raise

    def __enter__(self): return self
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
                setattr(self, name, None)

        def _cache_clear_attr(name: str):
            obj = getattr(self, name, None)
            if obj is None:
                return
            cc = getattr(obj, "cache_clear", None)
            if cc is not None:
                try:
                    cc()
                except Exception as e:
                    errs.append((name + ".cache_clear", e))
            setattr(self, name, None)

        _cache_clear_attr("_find_gid_cached")
        _cache_clear_attr("_resolve_uuid_cached")
        _cache_clear_attr("_uuid_obj_cached")

        _close_attr("_uu_mm")
        _close_attr("_res_mm")
        _close_attr("_uu_f")
        _close_attr("_res_f")

        if errs:
            where, e = errs[0]
            raise RuntimeError(f"Error closing {where}: {e}") from e

    # -------- public API --------

    def get_uuid(self, gid: int) -> _uuid.UUID:
        if self._uu_mm is None:
            raise RuntimeError("GlobalResolver is closed")
        if type(gid) is not int:
            raise TypeError("gid must be int")
        if gid < 0 or gid >= self.node_count:
            raise IndexError(f"gid out of range: {gid}")
        return self._uuid_obj_cached(gid)

    def resolve_gid(self, gid: int) -> Tuple[int, int]:
        if self._res_mm is None:
            raise RuntimeError("GlobalResolver is closed")
        if type(gid) is not int:
            raise TypeError("gid must be int")
        if gid < 0 or gid >= self.node_count:
            raise IndexError(f"gid out of range: {gid}")

        type_idx, local_idx = RESOLVER_ROW.unpack_from(self._res_mm, gid * RESOLVER_ROW_BYTES)
        return int(type_idx), int(local_idx)

    def resolve_gid_full(self, gid: int) -> Tuple[str, int]:
        ci, li = self.resolve_gid(gid)
        if ci < 0 or ci >= len(self.type_names):
            raise ValueError(f"type_index out of range in resolver.bin: {ci}")
        return self.type_names[ci], li

    def find_gid(self, u: UuidLike) -> Optional[int]:
        if self._find_gid_cached is None:
            raise RuntimeError("GlobalResolver is closed")
        uu = normalize_uuid(u)
        return self._find_gid_cached((uu.hi, uu.lo))

    def resolve_uuid(self, u: UuidLike) -> Optional[Tuple[int, int, int]]:
        if self._resolve_uuid_cached is None:
            raise RuntimeError("GlobalResolver is closed")
        uu = normalize_uuid(u)
        return self._resolve_uuid_cached((uu.hi, uu.lo))

    # -------- internal cached cores --------

    def _uuid_obj_for_gid(self, gid: int) -> _uuid.UUID:
        hi, lo = uuid_at(self._uu_mm, gid)
        return Uuid128(int(hi), int(lo)).to_uuid()

    def _find_gid_by_hi_lo(self, key: Tuple[int, int]) -> Optional[int]:
        target_hi, target_lo = key
        mm = self._uu_mm
        n = self.node_count

        return find_uuid_index(mm, target_hi, target_lo, n)

    def _resolve_uuid_by_hi_lo(self, key: Tuple[int, int]) -> Optional[Tuple[int, int, int]]:
        gid = self._find_gid_by_hi_lo(key)
        if gid is None:
            return None
        ci, li = self.resolve_gid(gid)
        return gid, ci, li


class LocalResolver:
    """
    Per-type resolver rooted at: root/nodes/<CITYPE>/ or root/relations/<RELTYPE>/

    Files:
      - uuids.bin       : UUID_U128_BE x N_citype, sorted by (hi, lo)
      - global_ids.bin  : U32 x N_citype, aligned with uuids.bin
    """

    def __init__(self, ci_dir: Union[str, PathLike, Path], cache_size: int = 4096):
        ci_dir = Path(ci_dir)

        self._uu_f = self._gid_f = None
        self._uu_mm = self._gid_mm = None

        self.local_count: int = 0

        self._find_local_cached = None
        self._uuid_obj_cached = None

        try:
            uu_path = ci_dir / "uuids.bin"
            gid_path = ci_dir / "global_ids.bin"

            for p in (uu_path, gid_path):
                if not p.is_file():
                    raise FileNotFoundError(p)

            self._uu_f = uu_path.open("rb")
            self._gid_f = gid_path.open("rb")

            self._uu_mm = mmap.mmap(self._uu_f.fileno(), 0, access=mmap.ACCESS_READ)
            self._gid_mm = mmap.mmap(self._gid_f.fileno(), 0, access=mmap.ACCESS_READ)

            uu_sz = len(self._uu_mm)
            gid_sz = len(self._gid_mm)

            if uu_sz % UUID_BYTES != 0:
                raise ValueError(f"uuids.bin size not multiple of {UUID_BYTES}: {uu_sz}")
            if gid_sz % U32_LE.size != 0:
                raise ValueError(f"global_ids.bin size not multiple of {U32_LE.size}: {gid_sz}")

            n_uu = uu_sz // UUID_BYTES
            n_gid = gid_sz // U32_LE.size
            if n_uu != n_gid:
                raise ValueError(f"uuids.bin rows={n_uu} but global_ids.bin rows={n_gid}")

            self.local_count = n_uu

            self._find_local_cached = maybe_lru(cache_size, self._find_local_by_hi_lo)
            self._uuid_obj_cached = maybe_lru(cache_size, self._uuid_obj_for_local)

        except Exception:
            self.close()
            raise

    def __enter__(self): return self
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
                setattr(self, name, None)

        def _cache_clear_attr(name: str):
            obj = getattr(self, name, None)
            if obj is None:
                return
            cc = getattr(obj, "cache_clear", None)
            if cc is not None:
                try:
                    cc()
                except Exception as e:
                    errs.append((name + ".cache_clear", e))
            setattr(self, name, None)

        _cache_clear_attr("_find_local_cached")
        _cache_clear_attr("_uuid_obj_cached")

        _close_attr("_uu_mm")
        _close_attr("_gid_mm")
        _close_attr("_uu_f")
        _close_attr("_gid_f")

        if errs:
            where, e = errs[0]
            raise RuntimeError(f"Error closing {where}: {e}") from e

    # -------- public API --------

    def get_uuid(self, local_index: int) -> _uuid.UUID:
        if self._uu_mm is None:
            raise RuntimeError("LocalResolver is closed")
        if type(local_index) is not int:
            raise TypeError("local_index must be int")
        if local_index < 0 or local_index >= self.local_count:
            raise IndexError(f"local_index out of range: {local_index}")
        return self._uuid_obj_cached(local_index)

    def get_gid(self, local_index: int) -> int:
        if self._gid_mm is None:
            raise RuntimeError("LocalResolver is closed")
        if type(local_index) is not int:
            raise TypeError("local_index must be int")
        if local_index < 0 or local_index >= self.local_count:
            raise IndexError(f"local_index out of range: {local_index}")
        (gid,) = U32_LE.unpack_from(self._gid_mm, local_index * U32_LE.size)
        return int(gid)

    def find_gid(self, u: UuidLike) -> Optional[int]:
        if self._find_local_cached is None:
            raise RuntimeError("LocalResolver is closed")
        uu = normalize_uuid(u)
        local_index = self._find_local_cached((uu.hi, uu.lo))
        if local_index is None:
            return None
        return self.get_gid(local_index)

    # -------- internal cached cores --------

    def _uuid_obj_for_local(self, local_index: int) -> _uuid.UUID:
        hi, lo = uuid_at(self._uu_mm, local_index)
        return Uuid128(int(hi), int(lo)).to_uuid()

    def _find_local_by_hi_lo(self, key: Tuple[int, int]) -> Optional[int]:
        target_hi, target_lo = key
        mm = self._uu_mm
        n = self.local_count

        return find_uuid_index(mm, target_hi, target_lo, n)

class RelationGlobalResolver:
    """
    Global relation UUID resolver rooted at: root/relations_uuids/

    Files:
      - uuids.bin      : UUID_U128_BE x N_relations, sorted by (hi, lo)
      - resolver.bin   : rows (reltype_index U16, relid U32), aligned with uuids.bin
      - reltypes.json  : list[str] mapping reltype_index -> reltype name
    """

    def __init__(
        self,
        uuids_dir: Union[str, PathLike, Path],
        cache_size: int = 4096,
        *,
        uuids_file: str = "uuids.bin",
        names_file: str = "reltypes.json",
    ):
        uuids_dir = Path(uuids_dir)

        self._uu_f = self._res_f = None
        self._uu_mm = self._res_mm = None

        self.reltype_names: list[str] = []
        self.relation_count: int = 0

        self._resolve_uuid_cached = None
        self._uuid_obj_cached = None

        try:
            uu_path = uuids_dir / uuids_file
            res_path = uuids_dir / "resolver.bin"
            names_path = uuids_dir / names_file

            for p in (uu_path, res_path, names_path):
                if not p.is_file():
                    raise FileNotFoundError(p)

            self.reltype_names = json.loads(names_path.read_text("utf-8"))
            if (
                not isinstance(self.reltype_names, list)
                or not all(isinstance(x, str) for x in self.reltype_names)
            ):
                raise TypeError(f"{names_file} must be JSON list[str]")

            self._uu_f = uu_path.open("rb")
            self._res_f = res_path.open("rb")

            self._uu_mm = mmap.mmap(self._uu_f.fileno(), 0, access=mmap.ACCESS_READ)
            self._res_mm = mmap.mmap(self._res_f.fileno(), 0, access=mmap.ACCESS_READ)

            uu_sz = len(self._uu_mm)
            res_sz = len(self._res_mm)

            if uu_sz % UUID_BYTES != 0:
                raise ValueError(f"{uuids_file} size not multiple of {UUID_BYTES}: {uu_sz}")
            if res_sz % RESOLVER_ROW_BYTES != 0:
                raise ValueError(f"resolver.bin size not multiple of {RESOLVER_ROW_BYTES}: {res_sz}")

            n_uu = uu_sz // UUID_BYTES
            n_res = res_sz // RESOLVER_ROW_BYTES
            if n_uu != n_res:
                raise ValueError(f"{uuids_file} rows={n_uu} but resolver.bin rows={n_res}")

            self.relation_count = n_uu

            self._resolve_uuid_cached = maybe_lru(cache_size, self._resolve_uuid_by_hi_lo)
            self._uuid_obj_cached = maybe_lru(cache_size, self._uuid_obj_for_row)

        except Exception:
            self.close()
            raise

    def __enter__(self): return self
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
                setattr(self, name, None)

        def _cache_clear_attr(name: str):
            obj = getattr(self, name, None)
            if obj is None:
                return
            cc = getattr(obj, "cache_clear", None)
            if cc is not None:
                try:
                    cc()
                except Exception as e:
                    errs.append((name + ".cache_clear", e))
            setattr(self, name, None)

        _cache_clear_attr("_resolve_uuid_cached")
        _cache_clear_attr("_uuid_obj_cached")

        _close_attr("_uu_mm")
        _close_attr("_res_mm")
        _close_attr("_uu_f")
        _close_attr("_res_f")

        if errs:
            where, e = errs[0]
            raise RuntimeError(f"Error closing {where}: {e}") from e

    # -------- public API --------

    def get_uuid(self, row_index: int) -> _uuid.UUID:
        if self._uu_mm is None:
            raise RuntimeError("RelationGlobalResolver is closed")
        if type(row_index) is not int:
            raise TypeError("row_index must be int")
        if row_index < 0 or row_index >= self.relation_count:
            raise IndexError(f"row_index out of range: {row_index}")
        return self._uuid_obj_cached(row_index)

    def resolve_uuid(self, u: UuidLike) -> Optional[Tuple[int, int]]:
        """
        Returns (reltype_index, relid) or None.
        """
        if self._resolve_uuid_cached is None:
            raise RuntimeError("RelationGlobalResolver is closed")
        uu = normalize_uuid(u)
        return self._resolve_uuid_cached((uu.hi, uu.lo))

    def resolve_uuid_full(self, u: UuidLike) -> Optional[Tuple[str, int]]:
        """
        Returns (reltype_name, relid) or None.
        """
        r = self.resolve_uuid(u)
        if r is None:
            return None
        reltype_index, relid = r
        if reltype_index < 0 or reltype_index >= len(self.reltype_names):
            raise ValueError(f"reltype_index out of range in resolver.bin: {reltype_index}")
        return self.reltype_names[reltype_index], int(relid)

    # -------- internals --------

    def _uuid_obj_for_row(self, row_index: int) -> _uuid.UUID:
        hi, lo = uuid_at(self._uu_mm, row_index)
        return Uuid128(int(hi), int(lo)).to_uuid()

    def _find_row_index_by_hi_lo(self, key: Tuple[int, int]) -> Optional[int]:
        target_hi, target_lo = key
        mm = self._uu_mm
        n = self.relation_count
        return find_uuid_index(mm, target_hi, target_lo, n)

    def _resolve_uuid_by_hi_lo(self, key: Tuple[int, int]) -> Optional[Tuple[int, int]]:
        row_index = self._find_row_index_by_hi_lo(key)
        if row_index is None:
            return None
        if self._res_mm is None:
            raise RuntimeError("RelationGlobalResolver is closed")
        reltype_index, relid = RESOLVER_ROW.unpack_from(self._res_mm, row_index * RESOLVER_ROW_BYTES)
        return int(reltype_index), int(relid)
