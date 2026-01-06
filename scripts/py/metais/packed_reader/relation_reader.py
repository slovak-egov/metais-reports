import json
import mmap
from pathlib import Path
from os import PathLike
from typing import Union

from .bin_formats import (
    U32_LE,           # 4 bytes for U32 local index
    EDGE_PAIR,        # 8 bytes for (u32, u32)
    EDGE_REC_BYTES,   # 8
    RELID_BYTES,      # 4
)

def _check_local(x: int) -> None:
    if type(x) is not int:
        raise TypeError(f"local index must be int, got {type(x).__name__}")
    if x < 0:
        raise IndexError(f"local index must be >= 0, got {x}")

class RelationReader:
    """
    Reads one reltype edge partition folder: relations/<reltype>/edges/<SRC>__<TGT>/

    Files expected in rel_dir:
      - src.tgt.bin        : rows of (src_local U32, tgt_local U32), sorted by (src_local, tgt_local)
      - src.tgt.relid.bin  : rows of (relid U32), aligned with src.tgt.bin
      - tgt.src.bin        : rows of (tgt_local U32, src_local U32), sorted by (tgt_local, src_local)
      - tgt.src.relid.bin  : rows of (relid U32), aligned with tgt.src.bin
    """

    def __init__(self, rel_dir: Union[str, PathLike, Path]):
        rel_dir = Path(rel_dir)

        self._src_pairs_f = self._src_relid_f = None
        self._tgt_pairs_f = self._tgt_relid_f = None

        self._src_pairs_mm = self._src_relid_mm = None
        self._tgt_pairs_mm = self._tgt_relid_mm = None

        self.row_count = 0

        # memoize ranges: local_index -> (lo, hi)
        self._src_cache: dict[int, tuple[int, int]] = {}
        self._tgt_cache: dict[int, tuple[int, int]] = {}

        # optional metadata
        self.reltype = None
        self.sourceType = None
        self.targetType = None

        try:
            src_pairs_path = rel_dir / "src.tgt.bin"
            src_relid_path = rel_dir / "src.tgt.relid.bin"
            tgt_pairs_path = rel_dir / "tgt.src.bin"
            tgt_relid_path = rel_dir / "tgt.src.relid.bin"

            for p in (src_pairs_path, src_relid_path, tgt_pairs_path, tgt_relid_path):
                if not p.is_file():
                    raise FileNotFoundError(p)

            self._src_pairs_f = src_pairs_path.open("rb")
            self._src_relid_f = src_relid_path.open("rb")
            self._tgt_pairs_f = tgt_pairs_path.open("rb")
            self._tgt_relid_f = tgt_relid_path.open("rb")

            self._src_pairs_mm = mmap.mmap(self._src_pairs_f.fileno(), 0, access=mmap.ACCESS_READ)
            self._src_relid_mm = mmap.mmap(self._src_relid_f.fileno(), 0, access=mmap.ACCESS_READ)
            self._tgt_pairs_mm = mmap.mmap(self._tgt_pairs_f.fileno(), 0, access=mmap.ACCESS_READ)
            self._tgt_relid_mm = mmap.mmap(self._tgt_relid_f.fileno(), 0, access=mmap.ACCESS_READ)

            src_pairs_sz = len(self._src_pairs_mm)
            src_relid_sz = len(self._src_relid_mm)
            tgt_pairs_sz = len(self._tgt_pairs_mm)
            tgt_relid_sz = len(self._tgt_relid_mm)

            if src_pairs_sz % EDGE_REC_BYTES != 0:
                raise ValueError(f"src.tgt.bin size not multiple of {EDGE_REC_BYTES}: {src_pairs_sz}")
            if tgt_pairs_sz % EDGE_REC_BYTES != 0:
                raise ValueError(f"tgt.src.bin size not multiple of {EDGE_REC_BYTES}: {tgt_pairs_sz}")
            if src_relid_sz % RELID_BYTES != 0:
                raise ValueError(f"src.tgt.relid.bin size not multiple of {RELID_BYTES}: {src_relid_sz}")
            if tgt_relid_sz % RELID_BYTES != 0:
                raise ValueError(f"tgt.src.relid.bin size not multiple of {RELID_BYTES}: {tgt_relid_sz}")

            n_src_pairs = src_pairs_sz // EDGE_REC_BYTES
            n_tgt_pairs = tgt_pairs_sz // EDGE_REC_BYTES
            n_src_relid = src_relid_sz // RELID_BYTES
            n_tgt_relid = tgt_relid_sz // RELID_BYTES

            if n_src_pairs != n_src_relid:
                raise ValueError(f"src.tgt rows mismatch: pairs={n_src_pairs} relid={n_src_relid}")
            if n_tgt_pairs != n_tgt_relid:
                raise ValueError(f"tgt.src rows mismatch: pairs={n_tgt_pairs} relid={n_tgt_relid}")
            if n_src_pairs != n_tgt_pairs:
                raise ValueError(f"src.tgt vs tgt.src edge count mismatch: {n_src_pairs} vs {n_tgt_pairs}")

            self.row_count = n_src_pairs

            meta_path = rel_dir / "meta.json"
            if meta_path.is_file():
                meta = json.loads(meta_path.read_text("utf-8"))
                self.reltype = meta.get("reltype")
                self.sourceType = meta.get("sourceType")
                self.targetType = meta.get("targetType")
                rc = meta.get("relationCount")
                if isinstance(rc, int) and rc != self.row_count:
                    raise ValueError(f"meta.json relationCount={rc} but edge files have {self.row_count} rows")

        except Exception:
            self.close()
            raise

    def __enter__(self): return self
    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def close(self) -> None:
        def _close_attr(name: str):
            obj = getattr(self, name, None)
            if obj is None:
                return
            try:
                obj.close()
            finally:
                setattr(self, name, None)

        _close_attr("_src_pairs_mm")
        _close_attr("_src_relid_mm")
        _close_attr("_tgt_pairs_mm")
        _close_attr("_tgt_relid_mm")

        _close_attr("_src_pairs_f")
        _close_attr("_src_relid_f")
        _close_attr("_tgt_pairs_f")
        _close_attr("_tgt_relid_f")

        self._src_cache.clear()
        self._tgt_cache.clear()

    # ---------- internal binary-search helpers ----------

    def _lower_bound_first(self, pairs_mm, first_val: int) -> int:
        lo, hi = 0, self.row_count
        edge_bytes = EDGE_REC_BYTES
        u32 = U32_LE.unpack_from
        while lo < hi:
            mid = (lo + hi) // 2
            (first,) = u32(pairs_mm, mid * edge_bytes)
            if first < first_val:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def _upper_bound_first(self, pairs_mm, first_val: int, lo_hint: int) -> int:
        lo, hi = lo_hint, self.row_count
        edge_bytes = EDGE_REC_BYTES
        u32 = U32_LE.unpack_from
        while lo < hi:
            mid = (lo + hi) // 2
            (first,) = u32(pairs_mm, mid * edge_bytes)
            if first <= first_val:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def _bounds_first_col(self, pairs_mm, first_val: int) -> tuple[int, int]:
        lo = self._lower_bound_first(pairs_mm, first_val)
        hi = self._upper_bound_first(pairs_mm, first_val, lo)
        return lo, hi

    def _src_range(self, src_local: int) -> tuple[int, int]:
        _check_local(src_local)
        r = self._src_cache.get(src_local)
        if r is not None:
            return r
        lo, hi = self._bounds_first_col(self._src_pairs_mm, src_local)
        self._src_cache[src_local] = (lo, hi)
        return lo, hi

    def _tgt_range(self, tgt_local: int) -> tuple[int, int]:
        _check_local(tgt_local)
        r = self._tgt_cache.get(tgt_local)
        if r is not None:
            return r
        lo, hi = self._bounds_first_col(self._tgt_pairs_mm, tgt_local)
        self._tgt_cache[tgt_local] = (lo, hi)
        return lo, hi

    def _iter_neighbors(self, pairs_mm, relid_mm, lo: int, hi: int):
        pair_unpack = EDGE_PAIR.unpack_from
        rel_unpack  = U32_LE.unpack_from
        edge_bytes  = EDGE_REC_BYTES
        rel_bytes   = RELID_BYTES

        base_pairs = lo * edge_bytes
        base_rel   = lo * rel_bytes

        for i in range(hi - lo):
            offp = base_pairs + i * edge_bytes
            _, neighbor_local = pair_unpack(pairs_mm, offp)

            offr = base_rel + i * rel_bytes
            (relid,) = rel_unpack(relid_mm, offr)

            yield (int(neighbor_local), int(relid))

    # ---------- public ----------

    def iter_targets(self, src_local: int):
        if self._src_pairs_mm is None:
            raise RuntimeError("RelationReader is closed")
        lo, hi = self._src_range(src_local)
        return self._iter_neighbors(self._src_pairs_mm, self._src_relid_mm, lo, hi)

    def iter_sources(self, tgt_local: int):
        if self._tgt_pairs_mm is None:
            raise RuntimeError("RelationReader is closed")
        lo, hi = self._tgt_range(tgt_local)
        return self._iter_neighbors(self._tgt_pairs_mm, self._tgt_relid_mm, lo, hi)