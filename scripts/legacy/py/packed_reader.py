#!/usr/bin/env python3
from __future__ import annotations

import io
from pathlib import Path
import json
import struct
import uuid
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

# ---------------------------------------------------------------------
# Common struct
# ---------------------------------------------------------------------

from bin_formats import (
    UUID_BYTES,
    ATTR_INDEX_BYTES,
    DICT_INDEX_BYTES,
    ROW_OFFSET_BYTES,
    GRID_INT_BYTES,
    REL_INT_BYTES,
    REL_PAIR_BYTES,
    INT32_LE,
    U16_LE,
    U64_LE,
    get_uint_le_struct
)


# ---------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------
    
def interpret_meta_state(state_val) -> bool:
    """
    Return True if entity is considered valid based on __meta__state value.
    None or missing -> valid.
    INVALIDATED     -> invalid.
    """
    if state_val is None:
        return True
    s = str(state_val).strip().upper()
    return s != "INVALIDATED"

# ---------------------------------------------------------------------
# Global dictionary (nodes)
# ---------------------------------------------------------------------

class StreamingGlobalDict:
    """
    Streamed random-access dictionary.

    Layout in base_dir/dict/:

      dict.values.bin  : concatenated JSON-encoded values
      dict.offsets.bin : (valueCount + 1) uint64 LE offsets
      dict.meta.json   : {"valueCount": N, ...}

    We never load the whole thing into memory – we seek per value.
    """

    def __init__(self, dict_dir: Path, eager: bool = False):
        meta_path = dict_dir / "dict.meta.json"
        offsets_path = dict_dir / "dict.offsets.bin"
        values_path = dict_dir / "dict.values.bin"

        if not meta_path.is_file():
            raise FileNotFoundError(f"Missing dict meta: {meta_path}")
        if not offsets_path.is_file():
            raise FileNotFoundError(f"Missing dict offsets: {offsets_path}")
        if not values_path.is_file():
            raise FileNotFoundError(f"Missing dict values: {values_path}")

        with meta_path.open("r", encoding="utf-8") as f:
            meta = json.load(f)
        self.value_count: int = int(meta["valueCount"])

        self._offsets_f = offsets_path.open("rb", buffering=0)
        self._values_f = values_path.open("rb", buffering=0)
        self._u64 = U64_LE
        self._eager = eager

        if eager:
            # fully load into memory
            self._offsets_bytes = offsets_path.read_bytes()
            self._values_bytes = values_path.read_bytes()
            self._offsets_f = None
            self._values_f = None
        else:
            self._offsets_f = offsets_path.open("rb", buffering=0)
            self._values_f = values_path.open("rb", buffering=0)

    def close(self) -> None:
        if not getattr(self, "_eager", False):
            self._offsets_f.close()
            self._values_f.close()

    def _read_offset(self, idx: int) -> int:
        if idx < 0 or idx > self.value_count:
            raise IndexError(f"offset index out of range: {idx}")

        if self._eager:
            size = self._u64.size
            start = idx * size
            end = start + size
            raw = self._offsets_bytes[start:end]
            if len(raw) != size:
                raise IOError("Unexpected EOF in dict.offsets.bin (in-memory)")
        else:
            self._offsets_f.seek(idx * self._u64.size)
            raw = self._offsets_f.read(self._u64.size)
            if len(raw) != self._u64.size:
                raise IOError("Unexpected EOF in dict.offsets.bin")
        (off,) = self._u64.unpack(raw)
        return off

    def get(self, idx: int) -> Any:
        if idx < 0 or idx >= self.value_count:
            raise IndexError(f"dict index out of range: {idx}")

        start = self._read_offset(idx)
        end = self._read_offset(idx + 1)
        length = end - start
        if length < 0:
            raise ValueError(f"Negative length for idx {idx}: {length}")

        if self._eager:
            raw = self._values_bytes[start:end]
            if len(raw) != length:
                raise IOError("Unexpected EOF in dict.values.bin (in-memory)")
        else:
            self._values_f.seek(start)
            raw = self._values_f.read(length)
            if len(raw) != length:
                raise IOError("Unexpected EOF in dict.values.bin")

        s = raw.decode("utf-8")
        return json.loads(s)


# ---------------------------------------------------------------------
# Global UUID index (entities)
# ---------------------------------------------------------------------

class UuidIndex:
    """
    Global UUID <-> int32 ID mapping, backed by:

      uuid_index/uuids.bin  (UUID_BYTES (16) bytes * N, sorted by UUID bytes)
      uuid_index/meta.json  (recordCount, uuidBytes, ...)
    """

    def __init__(self, uuid_index_dir: Path, eager: bool = False):
        meta_path = uuid_index_dir / "meta.json"
        uuids_path = uuid_index_dir / "uuids.bin"

        if not meta_path.is_file() or not uuids_path.is_file():
            raise FileNotFoundError(f"Missing uuid_index files under {uuid_index_dir}")

        with meta_path.open("r", encoding="utf-8") as f:
            meta = json.load(f)

        self.record_count: int = int(meta["recordCount"])
        self.uuid_bytes: int = int(meta.get("uuidBytes", UUID_BYTES))

        if self.uuid_bytes != UUID_BYTES:
            raise ValueError(f"UuidIndex currently only supports {UUID_BYTES}-byte UUIDs")

        self._eager = eager
        self._uuids_path = uuids_path
        self._uuids_f = uuids_path.open("rb", buffering=0)

        if eager:
            self._uuids_bytes = uuids_path.read_bytes()
            self._uuids_f = None
        else:
            self._uuids_f = uuids_path.open("rb", buffering=0)

    def close(self) -> None:
        if not self._eager and self._uuids_f is not None:
            self._uuids_f.close()

    # ---- ID -> UUID string ----

    def get_uuid(self, id_: int) -> str:
        if id_ < 0 or id_ >= self.record_count:
            raise IndexError("uuid id out of range")

        if self._eager:
            start = id_ * self.uuid_bytes
            end = start + self.uuid_bytes
            raw = self._uuids_bytes[start:end]
            if len(raw) != self.uuid_bytes:
                raise IOError("Unexpected EOF in uuid_index/uuids.bin (in-memory)")
        else:
            self._uuids_f.seek(id_ * self.uuid_bytes)
            raw = self._uuids_f.read(self.uuid_bytes)
            if len(raw) != self.uuid_bytes:
                raise IOError("Unexpected EOF in uuid_index/uuids.bin")

        return str(uuid.UUID(bytes=raw))

    def _read_uuid_bytes(self, idx: int) -> bytes:
        if idx < 0 or idx >= self.record_count:
            raise IndexError("uuid index out of range")

        if self._eager:
            start = idx * self.uuid_bytes
            end = start + self.uuid_bytes
            raw = self._uuids_bytes[start:end]
            if len(raw) != self.uuid_bytes:
                raise IOError("Unexpected EOF in uuid_index/uuids.bin (in-memory)")
        else:
            self._uuids_f.seek(idx * self.uuid_bytes)
            raw = self._uuids_f.read(self.uuid_bytes)
            if len(raw) != self.uuid_bytes:
                raise IOError("Unexpected EOF in uuid_index/uuids.bin")
        return raw

    # ---- UUID string -> ID (binary search) ----

    def get_id(self, uuid_str: str) -> Optional[int]:
        target = uuid.UUID(uuid_str).bytes
        lo, hi = 0, self.record_count - 1

        while lo <= hi:
            mid = (lo + hi) // 2
            mid_bytes = self._read_uuid_bytes(mid)

            if mid_bytes == target:
                return mid
            elif mid_bytes < target:
                lo = mid + 1
            else:
                hi = mid - 1

        return None


# ---------------------------------------------------------------------
# Node TypeView
# ---------------------------------------------------------------------

class TypeView:
    """
    View onto a single node type (e.g. "KS").

    Two possible layouts:

    1) grid (old):
         nodes/TYPE.bin
         nodes/TYPE.meta.json (has blockSize, missingSentinel, ...)
         nodes/TYPE.uuids.bin

       Fixed-size blocks of int32 indices with a missing sentinel.

    2) dense (new):
         nodes/TYPE.rows.bin
         nodes/TYPE.rows.offsets.bin
         nodes/TYPE.meta.json (layout="dense", attrIndexBytes, dictIndexBytes)
         nodes/TYPE.uuids.bin

       Variable-length rows; each row stores only non-missing attributes:
         k:uint16 + (attrIndex:uint16, dictIndex:int32)*k
    """

    def __init__(self, type_name: str, base_dir: Path, global_dict: StreamingGlobalDict, in_memory: bool = False):
        self.type_name = type_name
        self.base_dir = base_dir
        self.global_dict = global_dict
        self._in_memory = in_memory

        nodes_dir = base_dir / "nodes"
        meta_path = nodes_dir / f"{type_name}.meta.json"
        uuid_path = nodes_dir / f"{type_name}.uuids.bin"

        if not meta_path.is_file():
            raise FileNotFoundError(f"Missing meta for type {type_name}: {meta_path}")
        if not uuid_path.is_file():
            raise FileNotFoundError(f"Missing uuids for type {type_name}: {uuid_path}")

        with meta_path.open("r", encoding="utf-8") as f:
            meta = json.load(f)

        self.record_count: int = int(meta["recordCount"])
        self.endianness: str = meta.get("endianness", "LE")
        if self.endianness != "LE":
            raise ValueError("Currently only little-endian is supported")

        # Layout: "grid" (default / legacy) vs "dense"
        self.layout: str = meta.get("layout", "grid")

        raw_attrs = meta["attributes"]

        # attributes & attr_meta as before...
        self.attributes: List[str] = []
        self.attr_meta: Dict[str, Dict[str, Optional[str]]] = {}

        if raw_attrs and isinstance(raw_attrs[0], list):
            for triple in raw_attrs:
                technical = triple[0] if len(triple) > 0 else None
                human     = triple[1] if len(triple) > 1 else None
                desc      = triple[2] if len(triple) > 2 else None
                if not technical:
                    continue
                self.attributes.append(technical)
                self.attr_meta[technical] = {
                    "name": human,
                    "description": desc,
                }
        elif raw_attrs and isinstance(raw_attrs[0], dict):
            for item in raw_attrs:
                technical = item.get("technicalName")
                if not technical:
                    continue
                human = item.get("name")
                desc  = item.get("description")
                self.attributes.append(technical)
                self.attr_meta[technical] = {
                    "name": human,
                    "description": desc,
                }
        else:
            self.attributes = list(raw_attrs)
            self.attr_meta = {
                name: {"name": None, "description": None}
                for name in self.attributes
            }

        self.attr_index: Dict[str, int] = {
            name: idx for idx, name in enumerate(self.attributes)
        }

        self._uuids_path = uuid_path
        self._i32 = INT32_LE

        # initialise paths / buffers so attributes always exist
        self._bin_path = None
        self._bin_bytes = None
        self._rows_path = None
        self._row_offsets_path = None
        self._rows_bytes = None
        self._row_offsets_bytes = None
        self._uuids_bytes = None

        # ---- layout-specific setup ----
        if self.layout == "grid":
            # Method pointers
            self._get_attr_index_impl = self._get_attr_index_grid
            self._iter_records_impl   = self._iter_records_grid
            self._get_all_attrs_impl  = self._get_all_non_missing_attrs_grid

            # Files
            bin_path = nodes_dir / f"{type_name}.bin"
            if not bin_path.is_file():
                raise FileNotFoundError(f"Missing bin for type {type_name}: {bin_path}")
            self._bin_path = bin_path

            self.block_size = int(meta["blockSize"])
            self.int_bytes  = int(meta["intBytes"])
            self.missing    = int(meta["missingSentinel"])

            if self.int_bytes != GRID_INT_BYTES:
                raise ValueError(
                    f"Currently only intBytes={GRID_INT_BYTES} is supported for grid layout"
                )

            # Eager-load
            if in_memory:
                self._uuids_bytes = uuid_path.read_bytes()
                self._bin_bytes = bin_path.read_bytes()

        elif self.layout == "dense":
            # Method pointers
            self._get_attr_index_impl = self._get_attr_index_dense
            self._iter_records_impl   = self._iter_records_dense
            self._get_all_attrs_impl  = self._get_all_non_missing_attrs_dense

            # Files
            rows_file = meta.get("rowsFile", f"{type_name}.rows.bin")
            offs_file = meta.get("rowOffsetsFile", f"{type_name}.rows.offsets.bin")

            self._rows_path = nodes_dir / rows_file
            self._row_offsets_path = nodes_dir / offs_file

            if not self._rows_path.is_file():
                raise FileNotFoundError(
                    f"Missing rows.bin for type {type_name}: {self._rows_path}"
                )
            if not self._row_offsets_path.is_file():
                raise FileNotFoundError(
                    f"Missing rows.offsets.bin for type {type_name}: {self._row_offsets_path}"
                )

            self.attr_index_bytes = int(meta.get("attrIndexBytes", ATTR_INDEX_BYTES))
            self.dict_index_bytes = int(meta.get("dictIndexBytes", DICT_INDEX_BYTES))

            if self.attr_index_bytes != ATTR_INDEX_BYTES:
                raise ValueError(
                    f"Dense layout currently only supports attrIndexBytes={ATTR_INDEX_BYTES}"
                )
            if self.dict_index_bytes != DICT_INDEX_BYTES:
                raise ValueError(
                    f"Dense layout currently only supports dictIndexBytes={DICT_INDEX_BYTES}"
                )

            # Eager-load
            if in_memory:
                self._uuids_bytes = uuid_path.read_bytes()
                self._rows_bytes = self._rows_path.read_bytes()
                self._row_offsets_bytes = self._row_offsets_path.read_bytes()

        else:
            raise ValueError(f"Unknown node layout '{self.layout}' for type {type_name}")

    def list_attributes(self) -> List[str]:
        return self.attributes

    def _open_uuids(self):
        if self._in_memory:
            return io.BytesIO(self._uuids_bytes)
        return self._uuids_path.open("rb", buffering=0)

    def _open_bin(self):
        if self.layout != "grid":
            raise RuntimeError("bin not available for non-grid layout")
        if self._in_memory:
            return io.BytesIO(self._bin_bytes)
        return self._bin_path.open("rb", buffering=0)

    def _open_rows(self):
        if self._in_memory:
            return io.BytesIO(self._rows_bytes)
        return self._rows_path.open("rb", buffering=0)

    def _open_row_offsets(self):
        if self._in_memory:
            return io.BytesIO(self._row_offsets_bytes)
        return self._row_offsets_path.open("rb", buffering=0)

    # ---- UUID helpers ----

    def get_uuid(self, record_idx: int) -> str:
        if record_idx < 0 or record_idx >= self.record_count:
            raise IndexError("record index out of range")

        with self._open_uuids() as f:
            f.seek(record_idx * UUID_BYTES)
            raw = f.read(UUID_BYTES)
            if len(raw) != UUID_BYTES:
                raise IOError("Unexpected EOF in uuids.bin")
            return str(uuid.UUID(bytes=raw))

    def find_record_index_by_uuid(self, uuid_str: str) -> Optional[int]:
        target = uuid.UUID(uuid_str).bytes
        lo, hi = 0, self.record_count - 1

        with self._open_uuids() as f:
            while lo <= hi:
                mid = (lo + hi) // 2
                f.seek(mid * UUID_BYTES)
                raw = f.read(UUID_BYTES)
                if len(raw) != UUID_BYTES:
                    break

                if raw == target:
                    return mid
                elif raw < target:
                    lo = mid + 1
                else:
                    hi = mid - 1

        return None

    # ---- attribute helpers ----

    def _read_int_at(self, f, record_idx: int, col_idx: int) -> int:
        if record_idx < 0 or record_idx >= self.record_count:
            raise IndexError("record index out of range")
        if col_idx < 0 or col_idx >= getattr(self, "block_size", 0):
            raise IndexError("column index out of range")

        offset = (record_idx * self.block_size + col_idx) * self.int_bytes
        f.seek(offset)
        raw = f.read(self.int_bytes)
        if len(raw) != self.int_bytes:
            raise IOError("Unexpected EOF in bin file")
        (val,) = self._i32.unpack(raw)
        return val

    def _read_row_offset(self, f_off, idx: int) -> int:
        if idx < 0 or idx > self.record_count:
            raise IndexError("row offset index out of range")
        f_off.seek(idx * ROW_OFFSET_BYTES)
        raw = f_off.read(ROW_OFFSET_BYTES)
        if len(raw) != ROW_OFFSET_BYTES:
            raise IOError("Unexpected EOF in rows.offsets.bin")
        (off,) = U64_LE.unpack(raw)
        return off

    def _get_dense_dict_index(self, record_idx: int, col_idx: int) -> Optional[int]:
        """
        For dense layout: return dictIndex for (record_idx, col_idx), or None if missing.
        """
        if record_idx < 0 or record_idx >= self.record_count:
            raise IndexError("record index out of range")

        with self._open_row_offsets() as f_off, self._open_rows() as f_rows:
            start = self._read_row_offset(f_off, record_idx)
            end   = self._read_row_offset(f_off, record_idx + 1)
            length = end - start
            if length < 0:
                raise ValueError(f"Negative row length for record {record_idx}")

            f_rows.seek(start)
            raw = f_rows.read(length)
            if len(raw) != length:
                raise IOError("Unexpected EOF in rows.bin")

            (k,) = U16_LE.unpack_from(raw, 0)
            pos = U16_LE.size  # after k

            lo, hi = 0, k - 1
            pair_size = self.attr_index_bytes + self.dict_index_bytes

            while lo <= hi:
                mid = (lo + hi) // 2
                off = pos + mid * pair_size
                attr_i = U16_LE.unpack_from(raw, off)[0]  # size ATTR_INDEX_BYTES
                if attr_i == col_idx:
                    dict_idx = INT32_LE.unpack_from(raw, off + self.attr_index_bytes)[0]
                    return dict_idx
                elif attr_i < col_idx:
                    lo = mid + 1
                else:
                    hi = mid - 1

            return None

    def _get_attr_index_grid(self, record_idx: int, col_idx: int) -> Optional[int]:
        with self._open_bin() as f:
            idx = self._read_int_at(f, record_idx, col_idx)
        if idx == self.missing:
            return None
        return idx

    def get_attr_index(self, record_idx: int, attr_name: str) -> Optional[int]:
        col = self.attr_index.get(attr_name)
        if col is None:
            raise KeyError(f"Unknown attribute for type {self.type_name}: {attr_name}")
        return self._get_attr_index_impl(record_idx, col)

    def _get_attr_index_dense(self, record_idx: int, col_idx: int) -> Optional[int]:
        return self._get_dense_dict_index(record_idx, col_idx)

    def get_attr_value(self, record_idx: int, attr_name: str) -> Any:
        dict_idx = self.get_attr_index(record_idx, attr_name)
        if dict_idx is None:
            return None
        return self.global_dict.get(dict_idx)

    def _get_all_non_missing_attrs_grid(self, record_idx: int) -> Dict[str, Any]:
        res: Dict[str, Any] = {}
        with self._open_bin() as f:
            offset = record_idx * self.block_size * self.int_bytes
            f.seek(offset)
            raw = f.read(self.block_size * self.int_bytes)
            if len(raw) != self.block_size * self.int_bytes:
                raise IOError("Unexpected EOF in bin file")

            for col in range(self.block_size):
                (idx,) = self._i32.unpack_from(raw, col * self.int_bytes)
                if idx == self.missing:
                    continue
                name = self.attributes[col]
                res[name] = self.global_dict.get(idx)
        return res
            
    def _get_all_non_missing_attrs_dense(self, record_idx: int) -> Dict[str, Any]:
        res: Dict[str, Any] = {}
        with self._open_row_offsets() as f_off, self._open_rows() as f_rows:
            start = self._read_row_offset(f_off, record_idx)
            end   = self._read_row_offset(f_off, record_idx + 1)
            length = end - start
            if length < 0:
                raise ValueError(f"Negative row length for record {record_idx}")

            f_rows.seek(start)
            raw = f_rows.read(length)
            if len(raw) != length:
                raise IOError("Unexpected EOF in rows.bin")

            (k,) = U16_LE.unpack_from(raw, 0)
            pos = U16_LE.size
            pair_size = self.attr_index_bytes + self.dict_index_bytes

            for _ in range(k):
                attr_i = U16_LE.unpack_from(raw, pos)[0]
                dict_i = INT32_LE.unpack_from(raw, pos + self.attr_index_bytes)[0]
                pos += pair_size

                name = self.attributes[attr_i]
                res[name] = self.global_dict.get(dict_i)

        return res

    def get_all_non_missing_attrs(self, record_idx: int) -> Dict[str, Any]:
        return self._get_all_attrs_impl(record_idx)

    # ---- sequential iteration ----

    def _iter_records_grid(
        self,
        attr_names: Optional[Iterable[str]] = None,
    ) -> Iterator[Tuple[str, Dict[str, Any]]]:
        if attr_names is None:
            wanted_cols: Optional[set[int]] = None
        else:
            wanted_cols = set()
            for name in attr_names:
                col = self.attr_index.get(name)
                if col is None:
                    raise KeyError(f"Attribute not found for type {self.type_name}: {name}")
                wanted_cols.add(col)

        # UUIDs are always the same
        with self._open_uuids() as f_uuid:
            block_bytes = self.block_size * self.int_bytes
            with self._open_bin() as f_bin:
                for _ in range(self.record_count):
                    raw_uuid = f_uuid.read(UUID_BYTES)
                    if len(raw_uuid) != UUID_BYTES:
                        raise IOError("Unexpected EOF in uuids.bin")
                    uuid_str = str(uuid.UUID(bytes=raw_uuid))

                    raw_block = f_bin.read(block_bytes)
                    if len(raw_block) != block_bytes:
                        raise IOError("Unexpected EOF in bin file")

                    attrs: Dict[str, Any] = {}

                    if wanted_cols is None:
                        cols = range(self.block_size)
                    else:
                        cols = wanted_cols

                    for col in cols:
                        (idx,) = self._i32.unpack_from(raw_block, col * self.int_bytes)
                        if idx == self.missing:
                            continue
                        name = self.attributes[col]
                        attrs[name] = self.global_dict.get(idx)

                    yield uuid_str, attrs

    def _iter_records_dense(
            self,
            attr_names: Optional[Iterable[str]] = None,
        ) -> Iterator[Tuple[str, Dict[str, Any]]]:
            if attr_names is None:
                wanted_cols: Optional[set[int]] = None
            else:
                wanted_cols = set()
                for name in attr_names:
                    col = self.attr_index.get(name)
                    if col is None:
                        raise KeyError(f"Attribute not found for type {self.type_name}: {name}")
                    wanted_cols.add(col)

            # UUIDs are always the same
            with self._open_uuids() as f_uuid:
                with self._open_row_offsets() as f_off, self._open_rows() as f_rows:
                    for record_idx in range(self.record_count):
                        raw_uuid = f_uuid.read(UUID_BYTES)
                        if len(raw_uuid) != UUID_BYTES:
                            raise IOError("Unexpected EOF in uuids.bin")
                        uuid_str = str(uuid.UUID(bytes=raw_uuid))

                        start = self._read_row_offset(f_off, record_idx)
                        end   = self._read_row_offset(f_off, record_idx + 1)
                        length = end - start
                        if length < 0:
                            raise ValueError(f"Negative row length for record {record_idx}")

                        f_rows.seek(start)
                        raw = f_rows.read(length)
                        if len(raw) != length:
                            raise IOError("Unexpected EOF in rows.bin")

                        (k,) = U16_LE.unpack_from(raw, 0)
                        pos = U16_LE.size
                        pair_size = self.attr_index_bytes + self.dict_index_bytes

                        attrs: Dict[str, Any] = {}

                        for _ in range(k):
                            attr_i = U16_LE.unpack_from(raw, pos)[0]
                            dict_i = INT32_LE.unpack_from(raw, pos + self.attr_index_bytes)[0]
                            pos += pair_size

                            if wanted_cols is not None and attr_i not in wanted_cols:
                                continue

                            name = self.attributes[attr_i]
                            attrs[name] = self.global_dict.get(dict_i)

                        yield uuid_str, attrs

    def iter_records(self, attr_names: Optional[Iterable[str]] = None):
        yield from self._iter_records_impl(attr_names)

# ---------------------------------------------------------------------
# UUID -> type mapping
# ---------------------------------------------------------------------

class UuidTypeIndex:
    """
    Parallel index to UuidIndex: for each global UUID ID, store a small type code.

    Layout in uuid_types/:

      types.bin   : recordCount * bytesPerCode, int codes
      meta.json   : {
                       "recordCount": N,
                       "bytesPerCode": 2,
                       "endianness": "LE",
                       "types": [{ "code": 0, "typeName": "AS" }, ...]
                    }
    """

    def __init__(self, uuid_types_dir: Path, eager: bool = False):
        meta_path = uuid_types_dir / "meta.json"
        types_path = uuid_types_dir / "types.bin"

        if not meta_path.is_file() or not types_path.is_file():
            raise FileNotFoundError(f"Missing uuid_types files under {uuid_types_dir}")

        with meta_path.open("r", encoding="utf-8") as f:
            meta = json.load(f)

        self.record_count: int = int(meta["recordCount"])
        self.bytes_per_code: int = int(meta.get("bytesPerCode", 2))
        self.endianness: str = meta.get("endianness", "LE")

        if self.endianness != "LE":
            raise ValueError("UuidTypeIndex currently only supports LE")

        # Build code -> typeName and typeName -> code
        self.code_to_type: Dict[int, str] = {}
        self.type_to_code: Dict[str, int] = {}

        for entry in meta.get("types", []):
            code = int(entry["code"])
            tname = entry["typeName"]
            self.code_to_type[code] = tname
            self.type_to_code[tname] = code

        self._struct = get_uint_le_struct(self.bytes_per_code)

        self._types_f = types_path.open("rb", buffering=0)

        self._eager = eager

        if eager:
            self._types_bytes = types_path.read_bytes()
            self._types_f = None
        else:
            self._types_f = types_path.open("rb", buffering=0)

    def close(self) -> None:
        if not self._eager and self._types_f is not None:
            self._types_f.close()

    def _read_code_at(self, idx: int) -> int:
        if idx < 0 or idx >= self.record_count:
            raise IndexError("uuid type index out of range")

        offset = idx * self.bytes_per_code
        if self._eager:
            end = offset + self.bytes_per_code
            raw = self._types_bytes[offset:end]
            if len(raw) != self.bytes_per_code:
                raise IOError("Unexpected EOF in uuid_types/types.bin (in-memory)")
        else:
            self._types_f.seek(offset)
            raw = self._types_f.read(self.bytes_per_code)
            if len(raw) != self.bytes_per_code:
                raise IOError("Unexpected EOF in uuid_types/types.bin")

        (code,) = self._struct.unpack(raw)
        return code

    def get_type_by_id(self, id_: int) -> Optional[str]:
        code = self._read_code_at(id_)
        return self.code_to_type.get(code)

    def get_code_for_type(self, type_name: str) -> Optional[int]:
        return self.type_to_code.get(type_name)


# ---------------------------------------------------------------------
# Relations: single file view
# ---------------------------------------------------------------------

class RelationFile:
    """
    Represents a single packed relation file, e.g.
      PO_je_gestor_KS.src.tgt.bin

    Layout: consecutive (int32, int32) pairs,
    sorted by the FIRST component (src or tgt depending on file).
    """

    def __init__(self, bin_path: Path, meta_path: Path, eager: bool = False):
        self.bin_path = bin_path
        self._eager = eager

        with meta_path.open("r", encoding="utf-8") as f:
            meta = json.load(f)

        self.record_count: int = int(meta["recordCount"])
        self.layout: List[str] = meta.get("layout", ["src", "tgt"])
        self.sorted_by: List[str] = meta.get("sortedBy", self.layout)

        self.technical_name: Optional[str] = meta.get("technicalName")
        self.name: Optional[str] = meta.get("name")
        self.description: Optional[str] = meta.get("description")

        if meta.get("intBytes", REL_INT_BYTES) != REL_INT_BYTES:
            raise ValueError(f"RelationFile currently only supports {REL_INT_BYTES}-byte int entries")

        if meta.get("endianness", "LE") != "LE":
            raise ValueError("RelationFile currently only supports little-endian")

        if len(self.layout) != 2:
            raise ValueError("RelationFile expects 2-element layout (pairs)")

        if self._eager:
            self._bin_bytes = bin_path.read_bytes()

    def _open_bin(self):
        if self._eager:
            return io.BytesIO(self._bin_bytes)
        return self.bin_path.open("rb", buffering=0)

    # -- low-level read helpers --

    def _read_pair_at(self, f, idx: int) -> Tuple[int, int]:
        """
        Read (first, second) pair at given record index.
        """
        if idx < 0 or idx >= self.record_count:
            raise IndexError("relation record index out of range")

        offset = idx * REL_PAIR_BYTES  # 2 × REL_INT_BYTES
        f.seek(offset)
        raw = f.read(REL_PAIR_BYTES)
        if len(raw) != REL_PAIR_BYTES:
            raise IOError("Unexpected EOF in relation .bin")
        first = INT32_LE.unpack_from(raw, 0)[0]
        second = INT32_LE.unpack_from(raw, REL_INT_BYTES)[0]
        return first, second

    def _lower_bound_first(self, f, key: int) -> int:
        """
        First index i such that first[i] >= key, or record_count if none.
        """
        lo, hi = 0, self.record_count
        while lo < hi:
            mid = (lo + hi) // 2
            first, _ = self._read_pair_at(f, mid)
            if first < key:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def _upper_bound_first(self, f, key: int) -> int:
        """
        First index i such that first[i] > key, or record_count if none.
        """
        lo, hi = 0, self.record_count
        while lo < hi:
            mid = (lo + hi) // 2
            first, _ = self._read_pair_at(f, mid)
            if first <= key:
                lo = mid + 1
            else:
                hi = mid
        return lo

    # -- public APIs --

    def iter_pairs_for_first(self, first_id: int) -> Iterator[Tuple[int, int]]:
        """
        Yield all (first, second) pairs with given first_id.
        """
        with self._open_bin() as f:
            start = self._lower_bound_first(f, first_id)
            if start >= self.record_count:
                return
            end = self._upper_bound_first(f, first_id)
            for i in range(start, end):
                yield self._read_pair_at(f, i)

    def has_pair(self, first_id: int, second_id: int) -> bool:
        """
        Check if (first_id, second_id) exists in this file.
        """
        with self._open_bin() as f:
            start = self._lower_bound_first(f, first_id)
            if start >= self.record_count:
                return False
            end = self._upper_bound_first(f, first_id)
            lo, hi = start, end - 1
            while lo <= hi:
                mid = (lo + hi) // 2
                first, second = self._read_pair_at(f, mid)
                if second == second_id:
                    return True
                elif second < second_id:
                    lo = mid + 1
                else:
                    hi = mid - 1
        return False

    def iter_all_pairs(self) -> Iterator[Tuple[int, int]]:
        """
        Iterate all (first, second) pairs stored in this relation file
        """
        with self._open_bin() as f:
            for i in range(self.record_count):
                yield self._read_pair_at(f, i)

# ---------------------------------------------------------------------
# Relation store (both orientations + UUID conversion)
# ---------------------------------------------------------------------

class RelationStore:
    """
    Manages all packed relations (int-ID based).

    Layout:

      base_dir/
        relations/
          <RELTYPE>.src.tgt.bin
          <RELTYPE>.src.tgt.meta.json
          <RELTYPE>.tgt.src.bin
          <RELTYPE>.tgt.src.meta.json
    """

    def __init__(self, base_dir: Path, uuid_index: UuidIndex, eager: bool = False):
        self.base_dir = base_dir
        self.uuid_index = uuid_index
        self._relations: Dict[str, Dict[str, RelationFile]] = {}
        self._eager = eager
        self._load_all_relations()
        self._ctype_index: Dict[str, Dict[str, List[Dict[str, str]]]] = self._load_ctype_index()

    def _load_all_relations(self) -> None:
        rel_dir = self.base_dir / "relations"
        if not rel_dir.is_dir():
            self._relations = {}
            return

        rel_map: Dict[str, Dict[str, RelationFile]] = {}

        for bin_path in rel_dir.glob("*.bin"):
            stem = bin_path.stem
            if stem.endswith(".src.tgt"):
                relname = stem[:-len(".src.tgt")]
                kind = "src.tgt"
            elif stem.endswith(".tgt.src"):
                relname = stem[:-len(".tgt.src")]
                kind = "tgt.src"
            else:
                continue

            meta_path = bin_path.with_suffix(".meta.json")
            if not meta_path.is_file():
                raise FileNotFoundError(f"Missing meta for relation {bin_path}")

            rel_file = RelationFile(bin_path, meta_path, eager=self._eager)
            rel_map.setdefault(relname, {})[kind] = rel_file

        self._relations = rel_map

    def list_relation_types(self) -> List[str]:
        return sorted(self._relations.keys())

    # ---- helpers using UuidIndex ----

    def _uuid_to_id(self, u: str) -> Optional[int]:
        return self.uuid_index.get_id(u)

    def _id_to_uuid(self, i: int) -> Optional[str]:
        try:
            return self.uuid_index.get_uuid(i)
        except Exception:
            return None

    def _load_ctype_index(self) -> Dict[str, Dict[str, List[Dict[str, str]]]]:
        """
        Load packed/relations/index_by_ctype.json if it exists.

        Shape:
          {
            "PO": {
              "asSource": [{"reltype": "...", "otherType": "..."}, ...],
              "asTarget": [...]
            },
            ...
          }
        """
        idx_path = self.base_dir / "relations" / "index_by_ctype.json"
        if not idx_path.is_file():
            return {}
        with idx_path.open("r", encoding="utf-8") as f:
            return json.load(f)

    # ---- public graph API ----

    def neighbors_from(self, reltype: str, src_uuid: str) -> List[str]:
        """
        Given reltype and src UUID, return list of target UUIDs
        where src_uuid is the SOURCE (uses .src.tgt file).
        """
        rel = self._relations.get(reltype)
        if not rel or "src.tgt" not in rel:
            return []

        src_id = self._uuid_to_id(src_uuid)
        if src_id is None:
            return []

        rf = rel["src.tgt"]
        targets: List[str] = []
        for _, tgt_id in rf.iter_pairs_for_first(src_id):
            u = self._id_to_uuid(tgt_id)
            if u is not None:
                targets.append(u)
        return targets

    def neighbors_to(self, reltype: str, tgt_uuid: str) -> List[str]:
        """
        Given reltype and target UUID, return list of source UUIDs
        where tgt_uuid is the TARGET (uses .tgt.src file).
        """
        rel = self._relations.get(reltype)
        if not rel or "tgt.src" not in rel:
            return []

        tgt_id = self._uuid_to_id(tgt_uuid)
        if tgt_id is None:
            return []

        rf = rel["tgt.src"]
        sources: List[str] = []
        for _, src_id in rf.iter_pairs_for_first(tgt_id):
            u = self._id_to_uuid(src_id)
            if u is not None:
                sources.append(u)
        return sources

    def has_relation_src_tgt(self, reltype: str, src_uuid: str, tgt_uuid: str) -> bool:
        """
        Check if there exists an edge src_uuid --reltype--> tgt_uuid.
        """
        rel = self._relations.get(reltype)
        if not rel or "src.tgt" not in rel:
            return False

        src_id = self._uuid_to_id(src_uuid)
        tgt_id = self._uuid_to_id(tgt_uuid)
        if src_id is None or tgt_id is None:
            return False

        rf = rel["src.tgt"]
        return rf.has_pair(src_id, tgt_id)

    def all_relations_between(
        self,
        uuid1: str,
        uuid2: str,
    ) -> List[Tuple[str, str]]:
        """
        Return a list of (reltype, role) where role is 'uuid1->uuid2' or 'uuid2->uuid1'
        for any reltype that connects the pair.

        This scans ALL reltypes; if that becomes too slow, we can index per node later.
        """
        id1 = self._uuid_to_id(uuid1)
        id2 = self._uuid_to_id(uuid2)
        if id1 is None or id2 is None:
            return []

        res: List[Tuple[str, str]] = []

        for reltype, files in self._relations.items():
            rf_src = files.get("src.tgt")
            if not rf_src:
                continue

            if rf_src.has_pair(id1, id2):
                res.append((reltype, "uuid1->uuid2"))
            if rf_src.has_pair(id2, id1):
                res.append((reltype, "uuid2->uuid1"))

        return res

    def list_relations_for_ctype(
        self,
        ctype: str,
        role: str = "any",
    ) -> List[Dict[str, str]]:
        """
        Return a list of relation descriptors for a given citype.

        role:
          - "asSource": relations where this ctype appears on the src side
          - "asTarget": relations where this ctype appears on the tgt side
          - "any"     : both of the above concatenated

        Each descriptor is:
          {"reltype": <technicalName>, "otherType": <ctype_on_other_side>}
        """
        entry = self._ctype_index.get(ctype)
        if not entry:
            return []

        if role == "asSource":
            return list(entry.get("asSource", []))
        elif role == "asTarget":
            return list(entry.get("asTarget", []))
        else:
            # "any"
            return list(entry.get("asSource", [])) + list(entry.get("asTarget", []))

    def list_relations_between_ctypes(
        self,
        ctype1: str,
        ctype2: str,
    ) -> List[str]:
        """
        Return all reltypes that connect ctype1 and ctype2 in ANY direction.

        i.e. reltypes where:
          - ctype1 --reltype--> ctype2   OR
          - ctype2 --reltype--> ctype1

        Result is a sorted list of reltype technicalNames.
        """
        rels: set[str] = set()
        entry1 = self._ctype_index.get(ctype1)
        if entry1:
            for item in entry1.get("asSource", []):
                if item.get("otherType") == ctype2:
                    rels.add(item["reltype"])
            for item in entry1.get("asTarget", []):
                if item.get("otherType") == ctype2:
                    rels.add(item["reltype"])

        # You *could* also inspect entry2, but it's symmetric based on how we built the index
        return sorted(rels)

# ---------------------------------------------------------------------
# Top-level store
# ---------------------------------------------------------------------

class PackedStore:
    """
    Entry point for a packed dataset directory.

    Typical layout:

      base_dir/
        dict/...
        uuid_index/...
        nodes/...
        relations/...
    """

    def __init__(self, base_dir: Path, *, eager: bool = False):
        self.base_dir = base_dir
        self.eager = eager

        dict_dir = base_dir / "dict"
        if not dict_dir.is_dir():
            raise FileNotFoundError(f"Missing dict directory: {dict_dir}")

        uuid_index_dir = base_dir / "uuid_index"
        if not uuid_index_dir.is_dir():
            raise FileNotFoundError(f"Missing uuid_index directory: {uuid_index_dir}")

        uuid_types_dir = base_dir / "uuid_types"
        if not uuid_types_dir.is_dir():
            raise FileNotFoundError(f"Missing uuid_types directory: {uuid_types_dir}")

        self.global_dict = StreamingGlobalDict(dict_dir, eager=eager)
        self.uuid_index = UuidIndex(uuid_index_dir, eager=eager)
        self.uuid_types = UuidTypeIndex(uuid_types_dir, eager=eager)

        self.manifest: dict[str, Any] = {}
        self._node_types_from_manifest: list[str] | None = None
        self._rel_types_from_manifest: list[str] | None = None

        manifest_path = base_dir / "manifest.json"
        if manifest_path.is_file():
            try:
                with manifest_path.open("r", encoding="utf-8") as f:
                    self.manifest = json.load(f)

                node_types = self.manifest.get("nodeTypes")
                if isinstance(node_types, list):
                    # dedupe, keep order
                    seen = set()
                    self._node_types_from_manifest = []
                    for t in node_types:
                        if not isinstance(t, str):
                            continue
                        if t in seen:
                            continue
                        seen.add(t)
                        self._node_types_from_manifest.append(t)

                rel_types = self.manifest.get("relationTypes")
                if isinstance(rel_types, list):
                    seen = set()
                    self._rel_types_from_manifest = []
                    for r in rel_types:
                        if not isinstance(r, str):
                            continue
                        if r in seen:
                            continue
                        seen.add(r)
                        self._rel_types_from_manifest.append(r)

            except Exception as e:
                print(f"[packed] WARNING: failed to load manifest.json: {e}")
                self.manifest = {}
                self._node_types_from_manifest = None
                self._rel_types_from_manifest = None

        # Optionally cross-check recordCount
        if self.uuid_types.record_count != self.uuid_index.record_count:
            raise ValueError(
                "UuidIndex and UuidTypeIndex have different recordCount values"
            )

        self.relations = RelationStore(base_dir, self.uuid_index, eager=eager)

    def close(self) -> None:
        self.global_dict.close()
        self.uuid_index.close()
        self.uuid_types.close()

    # ---- nodes ----

    def get_ctype_for_uuid(self, uuid_str: str) -> Optional[str]:
        """
        Return the node type (citype) for a given UUID string.
        Uses global uuid_index + uuid_types.
        """
        id_ = self.uuid_index.get_id(uuid_str)
        if id_ is None:
            return None
        return self.uuid_types.get_type_by_id(id_)

    def is_valid_entity(self, uuid_str: str) -> bool:
        """
        Returns True if entity is not INVALIDATED.
        Missing __meta__state => treat as valid.
        Unknown UUID         => False.
        """
        # Step 1: Convert UUID -> global ID
        id_ = self.uuid_index.get_id(uuid_str)
        if id_ is None:
            return False

        # Step 2: Get the ctype
        ctype = self.uuid_types.get_type_by_id(id_)
        if ctype is None:
            return False

        # Step 3: Open the correct type view
        tv = self.open_type(ctype)

        # Step 4: Find record index inside that type
        record_idx = tv.find_record_index_by_uuid(uuid_str)
        if record_idx is None:
            return False

        # Step 5: Read __meta__state attribute
        try:
            state_val = tv.get_attr_value(record_idx, "__meta__state")
        except KeyError:
            return True

        return interpret_meta_state(state_val)

    def list_types(self) -> List[str]:
        """
        List all available node types.

        Priority:
          1) nodeTypes from manifest.json (if present)
          2) Fallback: infer from nodes/*.meta.json, ignoring helper files.
        """
        # Prefer manifest
        if self._node_types_from_manifest is not None:
            return list(self._node_types_from_manifest)

        # Fallback – old behavior, but slightly safer
        nodes_dir = self.base_dir / "nodes"
        types: List[str] = []
        if nodes_dir.is_dir():
            for meta_path in nodes_dir.glob("*.meta.json"):
                stem = meta_path.stem  # e.g. "AS" or "AS.meta"
                # Ignore helper/metadata files like "AS.meta.json"
                if stem.endswith(".meta"):
                    continue
                types.append(stem)
        return sorted(types)

    def open_type(self, type_name: str) -> TypeView:
        return TypeView(type_name, self.base_dir, self.global_dict, in_memory=self.eager)