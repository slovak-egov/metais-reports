"""
Usage:

    from packed_reader.attribute_reader import AttributeReader

    with AttributeReader("output/packed/nodes/KS", cache_size=16384) as ar:
        n_rows = ar.row_count

        row = ar.get_attr_row(12345)     # list[int] length = attributeCount, -1 for missing
        meta = ar.get_meta_row(12345)    # list[int] length = metaAttributeCount, -1 for missing

        aidx = ar.attr_index("Gen_Profil_nazov")
        if aidx is not None:
            didx = row[aidx]
"""

import os
import json
import mmap
import struct
from pathlib import Path
from os import PathLike
from typing import Any, Union, Optional
from collections.abc import Iterable

from .bin_formats import U32_LE, I32_LE, PAIR, UUID_BYTES
from .cache_utils import maybe_lru


def require_keys(
    obj: dict[str, Any],
    mandatory_keys: Iterable[str],
    obj_name: str = "object",
) -> None:
    missing = [k for k in mandatory_keys if k not in obj]
    if not missing:
        return

    msg = (
        f"{obj_name} is missing keys: {', '.join(missing)}\n"
        f"Full dump: {json.dumps(obj, ensure_ascii=False)}"
    )
    raise ValueError(msg)


def _mmap_ro_or_empty(f):
    # works for empty files too
    if os.fstat(f.fileno()).st_size == 0:
        return memoryview(b"")
    return mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)


class AttributeReader:
    """
    Reads per-entity attributes from packed node directories.

    - attributes.bin supports two layouts:
        * "grid": fixed-width rows of I32 dict_index, missing sentinel -1
        * "sparse": variable-width rows of (U16 attr_index, U32 dict_index) pairs,
                   indexed by attribute_offsets.bin (U32 offsets + sentinel)

    - metaAttributes.bin is ALWAYS "grid" with metaAttributeCount columns (I32),
      missing sentinel -1.

    Public API:
      - row_count
      - attr_count / meta_count
      - attr_names (list[str]) and attr_index(name) -> int|None
      - get_attr_row(row_idx) -> list[int]   (len == attr_count; -1 for missing)
      - get_meta_row(row_idx) -> list[int]   (len == meta_count; -1 for missing)
      - get_attr_pairs(row_idx) -> list[(attr_index, dict_index)]  (compat convenience)
      - get_meta_pairs(row_idx) -> list[(meta_index, dict_index)]  (compat convenience)
      - close() and context manager support

    NOTE: Returned rows are cached objects (if cache_size != 0). Treat them as READ-ONLY.
    """

    def __init__(self, attr_dir: Union[str, PathLike, Path], cache_size: Optional[int] = 4096):
        attr_dir = Path(attr_dir)

        # close-safe defaults
        self._attr_f = self._offs_f = self._attr_mm = self._offs_mm = None
        self._meta_f = self._meta_mm = None

        self._get_attr_cached = None
        self._get_meta_cached = None

        self._layout: Optional[str] = None
        self._attr_count = 0
        self._meta_count = 0
        self.row_count = 0

        # grid machinery
        self._row_width = 0
        self._row_unpack = None

        # meta grid machinery
        self._meta_row_width = 0
        self._meta_row_unpack = None

        # names
        self.attr_names: list[str] = []
        self._attr_name_to_index: Optional[dict[str, int]] = None

        try:
            # ---- format.json ----
            format_path = attr_dir / "format.json"
            if not format_path.is_file():
                raise FileNotFoundError(f"Missing format.json: {format_path}")
            format_json = json.loads(format_path.read_text("utf-8"))

            require_keys(format_json, ["attributeCount", "attributeLayout", "metaAttributeCount"], "format.json")

            self._attr_count = format_json.get("attributeCount")
            if type(self._attr_count) is not int:
                raise TypeError(
                    f'format.json["attributeCount"] must be int, got {type(self._attr_count).__name__}: {self._attr_count!r}'
                )
            if self._attr_count < 0:
                raise ValueError(f'format.json["attributeCount"] must be non-negative, got {self._attr_count}')

            self._meta_count = format_json.get("metaAttributeCount")
            if type(self._meta_count) is not int:
                raise TypeError(
                    f'format.json["metaAttributeCount"] must be int, got {type(self._meta_count).__name__}: {self._meta_count!r}'
                )
            if self._meta_count < 0:
                raise ValueError(f'format.json["metaAttributeCount"] must be non-negative, got {self._meta_count}')

            self._layout = format_json.get("attributeLayout")
            if type(self._layout) is not str:
                raise TypeError(
                    f'format.json["attributeLayout"] must be string, got {type(self._layout).__name__}: {self._layout!r}'
                )
            if self._layout not in ("grid", "sparse"):
                raise ValueError(f'format.json["attributeLayout"] must be "grid" or "sparse", got {self._layout!r}')

            if self._layout == "sparse":
                seb = format_json.get("sparseEntryByteSize")
                if type(seb) is not int:
                    raise TypeError(
                        f'format.json["sparseEntryByteSize"] must be int when attributeLayout is "sparse", got {type(seb).__name__}: {seb!r}'
                    )
                if seb != PAIR.size:
                    raise ValueError(f'sparseEntryByteSize must be {PAIR.size}, got {seb!r}')

            # ---- attributes.json (validate count; also keep tech names) ----
            attr_json_path = attr_dir / "attributes.json"
            if not attr_json_path.is_file():
                raise FileNotFoundError(f"Missing attributes.json: {attr_json_path}")

            attributes_json = json.loads(attr_json_path.read_text("utf-8"))
            if not isinstance(attributes_json, list):
                raise TypeError("attributes.json must be a JSON list")

            self.attr_tech_names = []
            self.attr_human_names = []
            self.attr_descriptions = []
            self.attr_has_enum = []
            self.attr_data_types = []
            self.attr_valid = []
            self.attr_is_array = []

            if all(isinstance(x, str) for x in attributes_json):
                tech_names = attributes_json
                self.attr_tech_names = tech_names
                self.attr_human_names = [None] * len(tech_names)
                self.attr_descriptions = [None] * len(tech_names)
                self.attr_has_enum = [None] * len(tech_names)
            elif all(isinstance(x, dict) for x in attributes_json):
                for i, d in enumerate(attributes_json):
                    tn = d.get("technicalName")
                    if not isinstance(tn, str):
                        raise TypeError(f"attributes.json[{i}].technicalName must be a string")

                    nm = d.get("name")
                    if nm is not None and not isinstance(nm, str):
                        raise TypeError(f"attributes.json[{i}].name must be string or null")

                    desc = d.get("description")
                    if desc is not None and not isinstance(desc, str):
                        raise TypeError(f"attributes.json[{i}].description must be string or null")

                    he = d.get("hasEnum")
                    if he is not None and not isinstance(he, str):
                        raise TypeError(f"attributes.json[{i}].hasEnum must be string or null")

                    dt = d.get("dataType")
                    if dt is not None and not isinstance(dt, str):
                        raise TypeError(f"attributes.json[{i}].dataType must be string or null")

                    vv = d.get("valid")
                    if vv is not None and not isinstance(vv, bool):
                        raise TypeError(f"attributes.json[{i}].valid must be bool or null")

                    ia = d.get("isArray")
                    if ia is not None and not isinstance(ia, bool):
                        raise TypeError(f"attributes.json[{i}].isArray must be bool or null")

                    self.attr_tech_names.append(tn)
                    self.attr_human_names.append(nm)
                    self.attr_descriptions.append(desc)
                    self.attr_has_enum.append(he)
                    self.attr_data_types.append(dt)
                    self.attr_valid.append(vv)
                    self.attr_is_array.append(ia)
            else:
                raise TypeError("attributes.json must be list[str] or list[dict] with technicalName")

            # ---- attributes.bin ----
            self._attr_bin_path = attr_dir / "attributes.bin"
            if not self._attr_bin_path.is_file():
                raise FileNotFoundError(self._attr_bin_path)

            self._attr_f = self._attr_bin_path.open("rb")
            self._attr_mm = _mmap_ro_or_empty(self._attr_f)

            attr_row_count = None

            if self._layout == "sparse":
                self._offsets_path = attr_dir / "attribute_offsets.bin"
                if not self._offsets_path.is_file():
                    raise FileNotFoundError(self._offsets_path)

                sz = self._offsets_path.stat().st_size
                if sz % U32_LE.size != 0:
                    raise ValueError(f"attribute_offsets.bin size not multiple of {U32_LE.size}: {sz}")

                n_offsets = sz // U32_LE.size
                if n_offsets < 2:
                    raise ValueError("attribute_offsets.bin is empty / missing sentinel offset")

                attr_rows = n_offsets - 1
                self.row_count = attr_rows

                self._offs_f = self._offsets_path.open("rb")
                self._offs_mm = mmap.mmap(self._offs_f.fileno(), 0, access=mmap.ACCESS_READ)

                end = self._read_offset(attr_rows)
                if end != len(self._attr_mm):
                    raise ValueError("attribute offsets sentinel does not match attributes.bin size")

                self._get_attr_cached = maybe_lru(cache_size, self._get_attr_row_sparse)

            else:  # grid
                if self._attr_count == 0:
                    if len(self._attr_mm) != 0:
                        raise ValueError("attributeCount is 0 but attributes.bin is not empty")
                    self._row_width = 0
                    self._row_unpack = struct.Struct("<")  # empty row
                    self._get_attr_cached = maybe_lru(cache_size, self._get_attr_row_grid)
                else:
                    self._row_width = self._attr_count * I32_LE.size
                    sz = len(self._attr_mm)
                    if sz % self._row_width != 0:
                        raise ValueError(f"attributes.bin size not multiple of {self._row_width}: {sz}")
                    attr_row_count = sz // self._row_width
                    self.row_count = attr_row_count
                    self._row_unpack = struct.Struct("<" + "i" * self._attr_count)
                    self._get_attr_cached = maybe_lru(cache_size, self._get_attr_row_grid)

            # ---- metaAttributes.bin (always grid) ----
            self._meta_bin_path = attr_dir / "metaAttributes.bin"
            if not self._meta_bin_path.is_file():
                raise FileNotFoundError(self._meta_bin_path)

            self._meta_f = self._meta_bin_path.open("rb")
            self._meta_mm = _mmap_ro_or_empty(self._meta_f)

            self._meta_row_width = self._meta_count * I32_LE.size
            meta_sz = len(self._meta_mm)

            meta_row_count = None

            if self._meta_row_width == 0:
                if meta_sz != 0:
                    raise ValueError("metaAttributeCount is 0 but metaAttributes.bin is not empty")
                self._meta_row_unpack = None
            else:
                if meta_sz % self._meta_row_width != 0:
                    raise ValueError(f"metaAttributes.bin size not multiple of {self._meta_row_width}: {meta_sz}")
                meta_row_count = meta_sz // self._meta_row_width
                self._meta_row_unpack = struct.Struct("<" + "i" * self._meta_count)

            # ---- resolve row_count ----
            if self._layout == "sparse":
                if meta_row_count is not None and meta_row_count != self.row_count:
                    raise ValueError(f"Row count mismatch: attributes rows={self.row_count} but metaAttributes rows={meta_row_count}")
            else:
                if attr_row_count is not None and meta_row_count is not None:
                    if attr_row_count != meta_row_count:
                        raise ValueError(f"Row count mismatch: attributes rows={attr_row_count} but metaAttributes rows={meta_row_count}")
                    self.row_count = attr_row_count
                elif attr_row_count is not None:
                    self.row_count = attr_row_count
                elif meta_row_count is not None:
                    self.row_count = meta_row_count
                else:
                    uu_path = attr_dir / "uuids.bin"
                    if not uu_path.is_file():
                        raise FileNotFoundError(f"Cannot infer row_count: missing uuids.bin: {uu_path}")
                    uu_sz = uu_path.stat().st_size
                    if uu_sz % UUID_BYTES != 0:
                        raise ValueError(f"uuids.bin size not multiple of {UUID_BYTES}: {uu_sz}")
                    self.row_count = uu_sz // UUID_BYTES

            self._get_meta_cached = maybe_lru(cache_size, self._get_meta_row_grid)

        except Exception:
            self.close()
            raise

    # ------------- properties -------------

    @property
    def attr_count(self) -> int:
        return self._attr_count

    @property
    def meta_count(self) -> int:
        return self._meta_count

    # ------------- context/close -------------

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
                c = getattr(obj, "close", None)
                if c is not None:
                    c()
                else:
                    r = getattr(obj, "release", None)
                    if r is not None:
                        r()
            except Exception as e:
                errs.append((name, e))
            finally:
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

        _cache_clear_attr("_get_attr_cached")
        _cache_clear_attr("_get_meta_cached")

        _close_attr("_attr_mm")
        _close_attr("_offs_mm")
        _close_attr("_meta_mm")

        _close_attr("_attr_f")
        _close_attr("_offs_f")
        _close_attr("_meta_f")

        self._attr_name_to_index = None

        if errs:
            where, e = errs[0]
            raise RuntimeError(f"Error closing {where}: {e}") from e

    # -------- internal helpers --------

    def _read_offset(self, i: int) -> int:
        if i < 0 or i > self.row_count:
            raise IndexError("attribute offset index out of range")
        return U32_LE.unpack_from(self._offs_mm, i * U32_LE.size)[0]

    # -------- name -> index --------

    def attr_index(self, technical_name: str) -> Optional[int]:
        m = self._attr_name_to_index
        if m is None:
            m = {n: i for i, n in enumerate(self.attr_tech_names)}
            self._attr_name_to_index = m
        return m.get(technical_name)

    # -------- row builders (core) --------

    def _get_attr_row_sparse(self, idx: int) -> list[int]:
        if idx < 0 or idx >= self.row_count:
            raise IndexError("row index out of range")

        o0 = self._read_offset(idx)
        o1 = self._read_offset(idx + 1)
        if o1 < o0:
            raise ValueError("attribute offsets not monotonic")

        # dense row output
        out = [-1] * self._attr_count
        if o0 == o1:
            return out

        mm = self._attr_mm
        if o1 > len(mm):
            raise ValueError("attribute offset past end of attributes.bin")

        pair_size = PAIR.size
        row_bytes = o1 - o0
        if row_bytes % pair_size != 0:
            raise ValueError("sparse row byte size is not a multiple of PAIR size")

        unpack = PAIR.unpack_from
        attr_count = self._attr_count

        for off in range(o0, o1, pair_size):
            aidx, didx = unpack(mm, off)
            if aidx >= attr_count:
                raise ValueError(f"sparse attr_index out of range: {aidx}")
            out[aidx] = int(didx)

        return out

    def _get_attr_row_grid(self, idx: int) -> list[int]:
        if idx < 0 or idx >= self.row_count:
            raise IndexError("row index out of range")

        if self._attr_count == 0:
            return []

        o0 = idx * self._row_width
        # struct.unpack gives tuple[int,...]
        return list(self._row_unpack.unpack_from(self._attr_mm, o0))

    def _get_meta_row_grid(self, idx: int) -> list[int]:
        if idx < 0 or idx >= self.row_count:
            raise IndexError("row index out of range")

        if self._meta_count == 0:
            return []

        o0 = idx * self._meta_row_width
        return list(self._meta_row_unpack.unpack_from(self._meta_mm, o0))

    # -------- public API --------

    def get_attr_row(self, idx: int) -> list[int]:
        if self._get_attr_cached is None:
            raise RuntimeError("AttributeReader is closed")
        return self._get_attr_cached(idx)

    def get_meta_row(self, idx: int) -> list[int]:
        if self._get_meta_cached is None:
            raise RuntimeError("AttributeReader is closed")
        return self._get_meta_cached(idx)

    def get_meta_cell(self, idx: int, midx: int) -> int:
        """
        Return a single meta cell (I32 dict index) without materializing the whole meta row.
        Returns -1 for missing (same sentinel as get_meta_row()).
        """
        if self._meta_mm is None:
            raise RuntimeError("AttributeReader is closed")
        if idx < 0 or idx >= self.row_count:
            raise IndexError("row index out of range")
        if midx < 0 or midx >= self._meta_count:
            raise IndexError("meta index out of range")
        if self._meta_count == 0:
            return -1
        off = idx * self._meta_row_width + midx * I32_LE.size
        (v,) = I32_LE.unpack_from(self._meta_mm, off)
        return int(v)

    # -------- compatibility helpers --------

    def get_attr_pairs(self, idx: int) -> list[tuple[int, int]]:
        row = self.get_attr_row(idx)
        return [(aidx, didx) for aidx, didx in enumerate(row) if didx != -1]

    def get_meta_pairs(self, idx: int) -> list[tuple[int, int]]:
        row = self.get_meta_row(idx)
        return [(midx, didx) for midx, didx in enumerate(row) if didx != -1]