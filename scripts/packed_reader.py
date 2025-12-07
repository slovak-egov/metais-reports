#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import json
import struct
import uuid
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

# ---------------------------------------------------------------------
# Common struct
# ---------------------------------------------------------------------

INT32 = struct.Struct("<i")


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

    def __init__(self, dict_dir: Path):
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
        self._u64 = struct.Struct("<Q")

    def close(self) -> None:
        self._offsets_f.close()
        self._values_f.close()

    def _read_offset(self, idx: int) -> int:
        if idx < 0 or idx > self.value_count:
            raise IndexError(f"offset index out of range: {idx}")
        self._offsets_f.seek(idx * 8)
        raw = self._offsets_f.read(8)
        if len(raw) != 8:
            raise IOError("Unexpected EOF in dict.offsets.bin")
        (off,) = self._u64.unpack(raw)
        return off

    def get(self, idx: int) -> Any:
        """
        Return the original JSON value for dict index idx.
        Can be str, int, bool, list, dict, etc.
        """
        if idx < 0 or idx >= self.value_count:
            raise IndexError(f"dict index out of range: {idx}")

        start = self._read_offset(idx)
        end = self._read_offset(idx + 1)
        length = end - start
        if length < 0:
            raise ValueError(f"Negative length for idx {idx}: {length}")

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

      uuid_index/uuids.bin  (16 bytes * N, sorted by UUID bytes)
      uuid_index/meta.json  (recordCount, uuidBytes, ...)
    """

    def __init__(self, uuid_index_dir: Path):
        meta_path = uuid_index_dir / "meta.json"
        uuids_path = uuid_index_dir / "uuids.bin"

        if not meta_path.is_file() or not uuids_path.is_file():
            raise FileNotFoundError(f"Missing uuid_index files under {uuid_index_dir}")

        with meta_path.open("r", encoding="utf-8") as f:
            meta = json.load(f)

        self.record_count: int = int(meta["recordCount"])
        self.uuid_bytes: int = int(meta.get("uuidBytes", 16))

        if self.uuid_bytes != 16:
            raise ValueError("UuidIndex currently only supports 16-byte UUIDs")

        self._uuids_path = uuids_path
        self._uuids_f = uuids_path.open("rb", buffering=0)

    def close(self) -> None:
        self._uuids_f.close()

    # ---- ID -> UUID string ----

    def get_uuid(self, id_: int) -> str:
        if id_ < 0 or id_ >= self.record_count:
            raise IndexError("uuid id out of range")
        self._uuids_f.seek(id_ * self.uuid_bytes)
        raw = self._uuids_f.read(self.uuid_bytes)
        if len(raw) != self.uuid_bytes:
            raise IOError("Unexpected EOF in uuid_index/uuids.bin")
        return str(uuid.UUID(bytes=raw))

    def _read_uuid_bytes(self, idx: int) -> bytes:
        if idx < 0 or idx >= self.record_count:
            raise IndexError("uuid index out of range")
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

    Uses:
      nodes/<TYPE>.bin
      nodes/<TYPE>.meta.json
      nodes/<TYPE>.uuids.bin
    """

    def __init__(self, type_name: str, base_dir: Path, global_dict: StreamingGlobalDict):
        self.type_name = type_name
        self.base_dir = base_dir
        self.global_dict = global_dict

        nodes_dir = base_dir / "nodes"
        meta_path = nodes_dir / f"{type_name}.meta.json"
        bin_path = nodes_dir / f"{type_name}.bin"
        uuid_path = nodes_dir / f"{type_name}.uuids.bin"

        if not meta_path.is_file():
            raise FileNotFoundError(f"Missing meta for type {type_name}: {meta_path}")
        if not bin_path.is_file():
            raise FileNotFoundError(f"Missing bin for type {type_name}: {bin_path}")
        if not uuid_path.is_file():
            raise FileNotFoundError(f"Missing uuids for type {type_name}: {uuid_path}")

        with meta_path.open("r", encoding="utf-8") as f:
            meta = json.load(f)

        self.record_count: int = int(meta["recordCount"])
        self.block_size: int = int(meta["blockSize"])
        self.int_bytes: int = int(meta["intBytes"])
        self.endianness: str = meta["endianness"]
        self.missing: int = int(meta["missingSentinel"])

        raw_attrs = meta["attributes"]

        # attributes = list of technical names (columns in bin file)
        self.attributes: List[str] = []
        # attr_meta = extra info per technical name
        self.attr_meta: Dict[str, Dict[str, Optional[str]]] = {}

        if raw_attrs and isinstance(raw_attrs[0], list):
            # NEW format: [technicalName, humanName, description]
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
            # Future-proof: object format with keys like technicalName/name/description
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
            # OLD format: just ["Gen_Profil_nazov", "EA_Profil_AS_charakter_as", ...]
            self.attributes = list(raw_attrs)
            self.attr_meta = {
                name: {"name": None, "description": None}
                for name in self.attributes
            }

        # Map technical name -> column index
        self.attr_index: Dict[str, int] = {
            name: idx for idx, name in enumerate(self.attributes)
        }

        if self.int_bytes != 4:
            raise ValueError("Currently only intBytes=4 is supported")
        if self.endianness != "LE":
            raise ValueError("Currently only little-endian is supported")

        self._bin_path = bin_path
        self._uuids_path = uuid_path
        self._i32 = INT32


    def list_attributes(self) -> List[str]:
        return self.attributes

    def _open_bin(self):
        return self._bin_path.open("rb", buffering=0)

    def _open_uuids(self):
        return self._uuids_path.open("rb", buffering=0)

    # ---- UUID helpers ----

    def get_uuid(self, record_idx: int) -> str:
        if record_idx < 0 or record_idx >= self.record_count:
            raise IndexError("record index out of range")

        with self._open_uuids() as f:
            f.seek(record_idx * 16)
            raw = f.read(16)
            if len(raw) != 16:
                raise IOError("Unexpected EOF in uuids.bin")
            return str(uuid.UUID(bytes=raw))

    def find_record_index_by_uuid(self, uuid_str: str) -> Optional[int]:
        target = uuid.UUID(uuid_str).bytes
        lo, hi = 0, self.record_count - 1

        with self._open_uuids() as f:
            while lo <= hi:
                mid = (lo + hi) // 2
                f.seek(mid * 16)
                raw = f.read(16)
                if len(raw) != 16:
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
        if col_idx < 0 or col_idx >= self.block_size:
            raise IndexError("column index out of range")

        offset = (record_idx * self.block_size + col_idx) * self.int_bytes
        f.seek(offset)
        raw = f.read(4)
        if len(raw) != 4:
            raise IOError("Unexpected EOF in bin file")
        (val,) = self._i32.unpack(raw)
        return val

    def get_attr_index(self, record_idx: int, attr_name: str) -> Optional[int]:
        col = self.attr_index.get(attr_name)
        if col is None:
            raise KeyError(f"Attribute not found for type {self.type_name}: {attr_name}")
        with self._open_bin() as f:
            idx = self._read_int_at(f, record_idx, col)
        if idx == self.missing:
            return None
        return idx

    def get_attr_value(self, record_idx: int, attr_name: str) -> Any:
        dict_idx = self.get_attr_index(record_idx, attr_name)
        if dict_idx is None:
            return None
        return self.global_dict.get(dict_idx)

    def get_all_non_missing_attrs(self, record_idx: int) -> Dict[str, Any]:
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

    # ---- sequential iteration ----

    def iter_records(
        self,
        attr_names: Optional[Iterable[str]] = None,
    ) -> Iterator[Tuple[str, Dict[str, Any]]]:
        """
        Yield (uuid_string, attrs_dict) for each record.

        If attr_names is provided, only those attributes are resolved
        (much cheaper than resolving all).
        """
        if attr_names is None:
            cols = list(range(self.block_size))
            col_to_name = {i: self.attributes[i] for i in cols}
        else:
            cols = []
            col_to_name = {}
            for name in attr_names:
                col = self.attr_index.get(name)
                if col is None:
                    raise KeyError(f"Attribute not found for type {self.type_name}: {name}")
                cols.append(col)
                col_to_name[col] = name

        block_bytes = self.block_size * self.int_bytes

        with self._open_bin() as f_bin, self._open_uuids() as f_uuid:
            for _ in range(self.record_count):
                raw_uuid = f_uuid.read(16)
                if len(raw_uuid) != 16:
                    raise IOError("Unexpected EOF in uuids.bin")
                uuid_str = str(uuid.UUID(bytes=raw_uuid))

                raw_block = f_bin.read(block_bytes)
                if len(raw_block) != block_bytes:
                    raise IOError("Unexpected EOF in bin file")

                attrs: Dict[str, Any] = {}
                for col in cols:
                    (idx,) = self._i32.unpack_from(raw_block, col * self.int_bytes)
                    if idx == self.missing:
                        continue
                    name = col_to_name[col]
                    attrs[name] = self.global_dict.get(idx)

                yield uuid_str, attrs

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

    def __init__(self, uuid_types_dir: Path):
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

        if self.bytes_per_code == 1:
            self._struct = struct.Struct("<B")
        elif self.bytes_per_code == 2:
            self._struct = struct.Struct("<H")
        elif self.bytes_per_code == 4:
            self._struct = struct.Struct("<I")
        else:
            raise ValueError(f"Unsupported bytesPerCode: {self.bytes_per_code}")

        self._types_f = types_path.open("rb", buffering=0)

    def close(self) -> None:
        self._types_f.close()

    def _read_code_at(self, idx: int) -> int:
        if idx < 0 or idx >= self.record_count:
            raise IndexError("uuid type index out of range")

        offset = idx * self.bytes_per_code
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

    def __init__(self, bin_path: Path, meta_path: Path):
        self.bin_path = bin_path

        with meta_path.open("r", encoding="utf-8") as f:
            meta = json.load(f)

        self.record_count: int = int(meta["recordCount"])
        self.layout: List[str] = meta.get("layout", ["src", "tgt"])
        self.sorted_by: List[str] = meta.get("sortedBy", self.layout)

        self.technical_name: Optional[str] = meta.get("technicalName")
        self.name: Optional[str] = meta.get("name")
        self.description: Optional[str] = meta.get("description")

        if meta.get("intBytes", 4) != 4:
            raise ValueError("RelationFile currently only supports int32 entries")
        if meta.get("endianness", "LE") != "LE":
            raise ValueError("RelationFile currently only supports little-endian")

        if len(self.layout) != 2:
            raise ValueError("RelationFile expects 2-element layout (pairs)")

    def _open_bin(self):
        return self.bin_path.open("rb", buffering=0)

    # -- low-level read helpers --

    def _read_pair_at(self, f, idx: int) -> Tuple[int, int]:
        """
        Read (first, second) pair at given record index.
        """
        if idx < 0 or idx >= self.record_count:
            raise IndexError("relation record index out of range")

        offset = idx * 8  # 8 bytes per pair: 2 × int32
        f.seek(offset)
        raw = f.read(8)
        if len(raw) != 8:
            raise IOError("Unexpected EOF in relation .bin")
        first = INT32.unpack_from(raw, 0)[0]
        second = INT32.unpack_from(raw, 4)[0]
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

    def __init__(self, base_dir: Path, uuid_index: UuidIndex):
        self.base_dir = base_dir
        self.uuid_index = uuid_index
        self._relations: Dict[str, Dict[str, RelationFile]] = {}
        self._load_all_relations()
        self._ctype_index: Dict[str, Dict[str, List[Dict[str, str]]]] = self._load_ctype_index()

    def _load_all_relations(self) -> None:
        rel_dir = self.base_dir / "relations"
        if not rel_dir.is_dir():
            # It's valid to have no relations in some datasets; don't crash
            self._relations = {}
            return

        rel_map: Dict[str, Dict[str, RelationFile]] = {}

        for bin_path in rel_dir.glob("*.bin"):
            stem = bin_path.stem  # e.g. PO_je_gestor_KS.src.tgt
            if stem.endswith(".src.tgt"):
                relname = stem[:-len(".src.tgt")]
                kind = "src.tgt"
            elif stem.endswith(".tgt.src"):
                relname = stem[:-len(".tgt.src")]
                kind = "tgt.src"
            else:
                # ignore unexpected patterns
                continue

            meta_path = bin_path.with_suffix(".meta.json")
            if not meta_path.is_file():
                raise FileNotFoundError(f"Missing meta for relation {bin_path}")

            rel_file = RelationFile(bin_path, meta_path)
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

    def __init__(self, base_dir: Path):
        self.base_dir = base_dir

        dict_dir = base_dir / "dict"
        if not dict_dir.is_dir():
            raise FileNotFoundError(f"Missing dict directory: {dict_dir}")

        uuid_index_dir = base_dir / "uuid_index"
        if not uuid_index_dir.is_dir():
            raise FileNotFoundError(f"Missing uuid_index directory: {uuid_index_dir}")

        uuid_types_dir = base_dir / "uuid_types"
        if not uuid_types_dir.is_dir():
            raise FileNotFoundError(f"Missing uuid_types directory: {uuid_types_dir}")

        self.global_dict = StreamingGlobalDict(dict_dir)
        self.uuid_index = UuidIndex(uuid_index_dir)
        self.uuid_types = UuidTypeIndex(uuid_types_dir)   # 👈 new

        # Optionally cross-check recordCount
        if self.uuid_types.record_count != self.uuid_index.record_count:
            raise ValueError(
                "UuidIndex and UuidTypeIndex have different recordCount values"
            )

        self.relations = RelationStore(base_dir, self.uuid_index)

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

    def list_types(self) -> List[str]:
        """
        List all available node types (based on *.meta.json in nodes/).

        This deliberately ignores helper files like *.uuids.bin.
        """
        nodes_dir = self.base_dir / "nodes"
        types: List[str] = []
        if nodes_dir.is_dir():
            for meta_path in nodes_dir.glob("*.meta.json"):
                # AS.meta.json -> "AS"
                types.append(meta_path.stem)
        return sorted(types)

    def open_type(self, type_name: str) -> TypeView:
        return TypeView(type_name, self.base_dir, self.global_dict)