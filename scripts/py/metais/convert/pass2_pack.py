from __future__ import annotations

import json
import mmap
import struct
import uuid as _uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, Optional, Tuple, Union

from metais.common.binary_io import (
    I32_LE,
    i32_sentinel_row,
    UUID_U128_BE,
    UUID_BYTES,
    RESOLVER_ROW,
    RESOLVER_ROW_BYTES,
)
from metais.common.atomic_write import atomic_write_with
from metais.common.step_marker import is_done, mark_done
from metais.common.uuid_search import find_uuid_index_u
from metais.common.shards import list_shards_by_meta
from metais.common.packed_spec import load_meta_keys_strict
from .ndjson_stream import ndjson_json_range


##################
# Spec constants #
##################

SPEC_META_KEYS_6 = (
    "owner",
    "state",
    "createdBy",
    "createdAt",
    "lastModifiedBy",
    "lastModifiedAt",
)


#############
# Utilities #
# ###########

def _iter_attribute_pairs(raw: dict[str, Any]) -> Iterator[Tuple[str, Any]]:
    """
    Raw schema (yours):
      "attributes": [ {"name": "...", "value": ...}, ... ]
    """
    attrs = raw.get("attributes")
    if not isinstance(attrs, list) or not attrs:
        return
    for it in attrs:
        if not isinstance(it, dict):
            continue
        tn = it.get("name")
        if not isinstance(tn, str) or not tn:
            continue
        val = it.get("value", None)
        yield tn, val


def _extract_meta_dict(raw: dict[str, Any]) -> dict[str, Any]:
    """
    Raw schema (yours):
      "metaAttributes": { "owner": "...", "state": "...", ... }
    """
    m = raw.get("metaAttributes")
    return m if isinstance(m, dict) else {}


####################################
# Dict lookup (value -> dictIndex) #
####################################

class DictLookup:
    """
    Builds an in-memory map: dict_value_bytes -> dictIndex
    from dict.bin + dict.offsets.bin.

    Important: we try multiple common json.dumps encodings to match pass1.5.
    """

    def __init__(self, dict_dir: Union[str, Path]):
        self.dict_dir = Path(dict_dir)
        self._dict_f = None
        self._offs_f = None
        self._dict_mm: Optional[mmap.mmap] = None
        self._offs_mm: Optional[mmap.mmap] = None
        self.value_count = 0
        self._map: dict[bytes, int] = {}

    def close(self) -> None:
        for name in ("_dict_mm", "_offs_mm"):
            mm = getattr(self, name, None)
            if mm is not None:
                try:
                    mm.close()
                finally:
                    setattr(self, name, None)
        for name in ("_dict_f", "_offs_f"):
            f = getattr(self, name, None)
            if f is not None:
                try:
                    f.close()
                finally:
                    setattr(self, name, None)

    def __enter__(self): return self
    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def load(self, *, verbose: bool = True) -> None:
        dict_bin = self.dict_dir / "dict.bin"
        offs_bin = self.dict_dir / "dict.offsets.bin"

        if not dict_bin.is_file():
            raise FileNotFoundError(dict_bin)
        if not offs_bin.is_file():
            raise FileNotFoundError(offs_bin)

        self._dict_f = dict_bin.open("rb")
        self._offs_f = offs_bin.open("rb")
        self._dict_mm = mmap.mmap(self._dict_f.fileno(), 0, access=mmap.ACCESS_READ)
        self._offs_mm = mmap.mmap(self._offs_f.fileno(), 0, access=mmap.ACCESS_READ)

        sz = offs_bin.stat().st_size
        if sz % 8 != 0:
            raise ValueError(f"dict.offsets.bin size not multiple of 8: {sz}")
        n_offsets = sz // 8
        if n_offsets < 2:
            raise ValueError("dict.offsets.bin missing sentinel")
        self.value_count = n_offsets - 1

        u64 = struct.Struct("<Q").unpack_from
        mm = self._dict_mm
        omm = self._offs_mm

        if verbose:
            print(f"[pass2] dict lookup building map, N={self.value_count}")

        for i in range(self.value_count):
            o0 = u64(omm, i * 8)[0]
            o1 = u64(omm, (i + 1) * 8)[0]
            if o1 < o0 or o1 > len(mm):
                raise ValueError("dict offsets corrupted / not monotonic")
            b = mm[o0:o1]
            self._map[bytes(b)] = i

        if verbose:
            print("[pass2] dict lookup ready")

    def _candidates(self, obj: Any) -> Iterable[bytes]:
        # Try a small set of common json.dump flavors to match pass1.5 writer.
        # (Keeps pass2 robust if pass1.5 used ensure_ascii True/False or compact separators.)
        yield json.dumps(obj, ensure_ascii=False).encode("utf-8")
        yield json.dumps(obj, ensure_ascii=True).encode("utf-8")
        yield json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        yield json.dumps(obj, ensure_ascii=True, separators=(",", ":")).encode("utf-8")

    def index(self, obj: Any) -> Optional[int]:
        if self._dict_mm is None:
            raise RuntimeError("DictLookup not loaded")
        for b in self._candidates(obj):
            v = self._map.get(b)
            if v is not None:
                return v
        return None


################################
# Global UUID + resolver index #
################################

class GlobalUuidIndex:
    def __init__(self, uuids_dir: Union[str, Path]):
        self.uuids_dir = Path(uuids_dir)
        self._uu_f = None
        self._uu_mm: Optional[mmap.mmap] = None
        self.node_count = 0

    def close(self) -> None:
        if self._uu_mm is not None:
            try: self._uu_mm.close()
            finally: self._uu_mm = None
        if self._uu_f is not None:
            try: self._uu_f.close()
            finally: self._uu_f = None

    def __enter__(self): return self
    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def load(self) -> None:
        p = self.uuids_dir / "uuids.bin"
        if not p.is_file():
            raise FileNotFoundError(p)
        self._uu_f = p.open("rb")
        self._uu_mm = mmap.mmap(self._uu_f.fileno(), 0, access=mmap.ACCESS_READ)
        sz = len(self._uu_mm)
        if sz % UUID_BYTES != 0:
            raise ValueError(f"uuids.bin size not multiple of {UUID_BYTES}: {sz}")
        self.node_count = sz // UUID_BYTES

    def find_gid(self, uuid_str: str) -> Optional[int]:
        if self._uu_mm is None:
            raise RuntimeError("GlobalUuidIndex not loaded")
        try:
            return find_uuid_index_u(self._uu_mm, uuid_str, self.node_count)
        except Exception:
            return None


class GlobalResolverIndex:
    def __init__(self, uuids_dir: Union[str, Path], expected_rows: int):
        self.uuids_dir = Path(uuids_dir)
        self._res_f = None
        self._res_mm: Optional[mmap.mmap] = None
        self.citypes: list[str] = []
        self.rows = 0
        self._expected = expected_rows

    def close(self) -> None:
        if self._res_mm is not None:
            try: self._res_mm.close()
            finally: self._res_mm = None
        if self._res_f is not None:
            try: self._res_f.close()
            finally: self._res_f = None

    def __enter__(self): return self
    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def load(self) -> None:
        citypes_path = self.uuids_dir / "citypes.json"
        res_path = self.uuids_dir / "resolver.bin"
        if not citypes_path.is_file():
            raise FileNotFoundError(citypes_path)
        if not res_path.is_file():
            raise FileNotFoundError(res_path)

        self.citypes = json.loads(citypes_path.read_text("utf-8"))
        if not isinstance(self.citypes, list) or not all(isinstance(x, str) for x in self.citypes):
            raise TypeError("citypes.json must be list[str]")

        self._res_f = res_path.open("rb")
        self._res_mm = mmap.mmap(self._res_f.fileno(), 0, access=mmap.ACCESS_READ)

        sz = len(self._res_mm)
        if sz % RESOLVER_ROW_BYTES != 0:
            raise ValueError(f"resolver.bin size not multiple of {RESOLVER_ROW_BYTES}: {sz}")
        self.rows = sz // RESOLVER_ROW_BYTES

        if self._expected and self.rows != self._expected:
            raise ValueError(f"resolver rows={self.rows} but uuids rows={self._expected}")

    def citype_of_gid(self, gid: int) -> str:
        if self._res_mm is None:
            raise RuntimeError("GlobalResolverIndex not loaded")
        if gid < 0 or gid >= self.rows:
            raise IndexError(f"gid out of range: {gid}")
        ci, _li = RESOLVER_ROW.unpack_from(self._res_mm, gid * RESOLVER_ROW_BYTES)
        ci = int(ci)
        if ci < 0 or ci >= len(self.citypes):
            raise ValueError(f"citype index out of range in resolver.bin: {ci}")
        return self.citypes[ci]


#######################################################
# Local UUID finder (per citype, uuid -> local_index) #
#######################################################

class LocalUuidIndex:
    def __init__(self, citype_dir: Union[str, Path]):
        self.citype_dir = Path(citype_dir)
        self._uu_f = None
        self._uu_mm: Optional[mmap.mmap] = None
        self.local_count = 0

    def close(self) -> None:
        if self._uu_mm is not None:
            try: self._uu_mm.close()
            finally: self._uu_mm = None
        if self._uu_f is not None:
            try: self._uu_f.close()
            finally: self._uu_f = None

    def load(self) -> None:
        p = self.citype_dir / "uuids.bin"
        if not p.is_file():
            raise FileNotFoundError(p)
        self._uu_f = p.open("rb")
        self._uu_mm = mmap.mmap(self._uu_f.fileno(), 0, access=mmap.ACCESS_READ)
        sz = len(self._uu_mm)
        if sz % UUID_BYTES != 0:
            raise ValueError(f"{p} size not multiple of {UUID_BYTES}: {sz}")
        self.local_count = sz // UUID_BYTES

    def find_local_index(self, uuid_str: str) -> Optional[int]:
        if self._uu_mm is None:
            raise RuntimeError("LocalUuidIndex not loaded")
        try:
            return find_uuid_index_u(self._uu_mm, uuid_str, self.local_count)
        except Exception:
            return None


#################################
# Node (per-citype) GRID writer #
#################################

@dataclass(frozen=True)
class _TypeSchema:
    attr_count: int
    attr_name_to_index: dict[str, int]
    meta_keys: Tuple[str, ...]  # always the 6 from metaAttributes.json


class _CitypeNodeWriter:
    def __init__(self, citype_dir: Path, dict_lookup: DictLookup, meta_keys: Tuple[str, ...], *, verbose: bool):
        self.citype_dir = citype_dir
        self.dict = dict_lookup
        self.verbose = verbose

        self.schema = self._load_schema(citype_dir, meta_keys)
        self.uuid_index = LocalUuidIndex(citype_dir)
        self.uuid_index.load()
        self.row_count = self.uuid_index.local_count

        self.attr_path = citype_dir / "attributes.bin"
        self.meta_path = citype_dir / "metaAttributes.bin"

        self._attr_f = None
        self._meta_f = None
        self._attr_mm: Optional[mmap.mmap] = None
        self._meta_mm: Optional[mmap.mmap] = None

        self._seen = bytearray(self.row_count)

        self._attr_row_width = self.schema.attr_count * 4
        self._meta_row_width = len(self.schema.meta_keys) * 4

        self._sent_attr_row = i32_sentinel_row(self.schema.attr_count)
        self._sent_meta_row = i32_sentinel_row(len(self.schema.meta_keys))

        self._preallocate_and_mmap()

        # stats
        self.unknown_attr = 0
        self.missing_dict = 0
        self.missing_uuid = 0
        self.not_found_uuid = 0

    def close(self) -> None:
        for mm_name in ("_attr_mm", "_meta_mm"):
            mm = getattr(self, mm_name, None)
            if mm is not None:
                try:
                    mm.flush()
                    mm.close()
                finally:
                    setattr(self, mm_name, None)
        for f_name in ("_attr_f", "_meta_f"):
            f = getattr(self, f_name, None)
            if f is not None:
                try:
                    f.close()
                finally:
                    setattr(self, f_name, None)
        self.uuid_index.close()

    def _load_schema(self, citype_dir: Path, meta_keys: Tuple[str, ...]) -> _TypeSchema:
        fmt = json.loads((citype_dir / "format.json").read_text("utf-8"))
        attr_count = fmt.get("attributeCount")
        meta_count = fmt.get("metaAttributeCount")

        if not isinstance(attr_count, int) or attr_count < 0:
            raise ValueError(f"{citype_dir}/format.json: bad attributeCount")
        if meta_count != len(meta_keys):
            raise ValueError(
                f"{citype_dir}/format.json: metaAttributeCount={meta_count} "
                f"but metaAttributes.json has {len(meta_keys)}"
            )

        attrs = json.loads((citype_dir / "attributes.json").read_text("utf-8"))
        name_to_idx: dict[str, int] = {}

        # freeze_schema may write list[str] or list[dict]
        if isinstance(attrs, list) and all(isinstance(x, str) for x in attrs):
            for i, tn in enumerate(attrs):
                name_to_idx[tn] = i
        elif isinstance(attrs, list) and all(isinstance(x, dict) for x in attrs):
            for i, d in enumerate(attrs):
                tn = d.get("technicalName") or d.get("name")
                if isinstance(tn, str):
                    name_to_idx[tn] = i
        else:
            raise TypeError(f"{citype_dir}/attributes.json must be list[str] or list[dict]")

        return _TypeSchema(attr_count=attr_count, attr_name_to_index=name_to_idx, meta_keys=meta_keys)

    def _preallocate_file_i32_grid(self, path: Path, rows: int, cols: int) -> None:
        # attributes.bin is allowed to be empty if cols==0
        if cols == 0 or rows == 0:
            path.write_bytes(b"")
            return
        total_i32 = rows * cols
        total_bytes = total_i32 * 4

        with open(path, "wb") as f:
            f.truncate(total_bytes)
            block_i32 = 1 << 16
            block = b"\xFF\xFF\xFF\xFF" * block_i32
            remain = total_i32
            while remain > 0:
                n = block_i32 if remain >= block_i32 else remain
                f.write(block[: n * 4])
                remain -= n

    def _preallocate_and_mmap(self) -> None:
        self._preallocate_file_i32_grid(self.attr_path, self.row_count, self.schema.attr_count)
        # metaAttributes.bin ALWAYS exists and ALWAYS 6 columns
        self._preallocate_file_i32_grid(self.meta_path, self.row_count, len(self.schema.meta_keys))

        self._attr_f = open(self.attr_path, "r+b")
        self._meta_f = open(self.meta_path, "r+b")

        self._attr_mm = (
            mmap.mmap(self._attr_f.fileno(), 0, access=mmap.ACCESS_WRITE)
            if self.row_count > 0 and self.schema.attr_count > 0
            else None
        )
        self._meta_mm = (
            mmap.mmap(self._meta_f.fileno(), 0, access=mmap.ACCESS_WRITE)
            if self.row_count > 0 and len(self.schema.meta_keys) > 0
            else None
        )

    def ingest(self, raw: dict[str, Any]) -> None:
        uuid_str = raw.get("uuid")
        if not isinstance(uuid_str, str) or not uuid_str:
            self.missing_uuid += 1
            return

        li = self.uuid_index.find_local_index(uuid_str)
        if li is None:
            self.not_found_uuid += 1
            return

        # if duplicate local_index, reset row to sentinel before overwriting
        if self._seen[li]:
            if self._attr_mm is not None and self._attr_row_width:
                o = li * self._attr_row_width
                self._attr_mm[o : o + self._attr_row_width] = self._sent_attr_row
            if self._meta_mm is not None and self._meta_row_width:
                o = li * self._meta_row_width
                self._meta_mm[o : o + self._meta_row_width] = self._sent_meta_row
        else:
            self._seen[li] = 1

        # attributes grid row
        if self._attr_mm is not None and self.schema.attr_count > 0:
            base = li * self._attr_row_width
            for tn, val in _iter_attribute_pairs(raw):
                aidx = self.schema.attr_name_to_index.get(tn)
                if aidx is None:
                    self.unknown_attr += 1
                    continue
                didx = self.dict.index(val)
                if didx is None:
                    self.missing_dict += 1
                    continue
                I32_LE.pack_into(self._attr_mm, base + aidx * 4, int(didx))

        # metaAttributes grid row (fixed 6 keys)
        if self._meta_mm is not None:
            meta = _extract_meta_dict(raw)
            base = li * self._meta_row_width
            for col, k in enumerate(self.schema.meta_keys):
                if k not in meta:
                    continue
                didx = self.dict.index(meta.get(k))
                if didx is None:
                    self.missing_dict += 1
                    continue
                I32_LE.pack_into(self._meta_mm, base + col * 4, int(didx))


##################################################
# Relation (per reltype) GRID writer + tmp edges #
##################################################

class _ReltypeWriter:
    def __init__(
        self,
        rel_dir: Path,
        dict_lookup: DictLookup,
        resolver: GlobalResolverIndex,
        meta_keys: Tuple[str, ...],
        *,
        verbose: bool,
    ):
        self.rel_dir = rel_dir
        self.dict = dict_lookup
        self.resolver = resolver
        self.verbose = verbose

        self.schema = self._load_schema(rel_dir, meta_keys)
        self.reltype = rel_dir.name

        self.edges_tmp = rel_dir / "tmp.edges.bin"
        self.meta_tmp = rel_dir / "metaAttributes.bin.tmp"
        self.attr_tmp = rel_dir / "attributes.bin.tmp"  # only if attr_count > 0

        self._edges_f = open(self.edges_tmp, "wb")
        self._meta_f = open(self.meta_tmp, "wb")
        self._attr_f = open(self.attr_tmp, "wb") if self.schema.attr_count > 0 else None

        self._attr_row_width = self.schema.attr_count * 4
        self._meta_row_width = len(self.schema.meta_keys) * 4  # always 6*4

        self._sent_attr_row = b"\xFF\xFF\xFF\xFF" * self.schema.attr_count
        self._sent_meta_row = b"\xFF\xFF\xFF\xFF" * len(self.schema.meta_keys)

        self.count = 0
        self.src_types: set[str] = set()
        self.tgt_types: set[str] = set()

        self.unknown_attr = 0
        self.missing_dict = 0

    def close(self) -> None:
        for f in (self._edges_f, self._meta_f):
            try:
                f.close()
            except Exception:
                pass
        if self._attr_f is not None:
            try:
                self._attr_f.close()
            except Exception:
                pass

    def _load_schema(self, rel_dir: Path, meta_keys: Tuple[str, ...]) -> _TypeSchema:
        fmt = json.loads((rel_dir / "format.json").read_text("utf-8"))
        attr_count = fmt.get("attributeCount")
        meta_count = fmt.get("metaAttributeCount")

        if not isinstance(attr_count, int) or attr_count < 0:
            raise ValueError(f"{rel_dir}/format.json: bad attributeCount")
        if meta_count != len(meta_keys):
            raise ValueError(
                f"{rel_dir}/format.json: metaAttributeCount={meta_count} "
                f"but metaAttributes.json has {len(meta_keys)}"
            )

        attrs_path = rel_dir / "attributes.json"
        name_to_idx: dict[str, int] = {}
        if attrs_path.is_file():
            attrs = json.loads(attrs_path.read_text("utf-8"))
            if isinstance(attrs, list) and all(isinstance(x, str) for x in attrs):
                for i, tn in enumerate(attrs):
                    name_to_idx[tn] = i
            elif isinstance(attrs, list) and all(isinstance(x, dict) for x in attrs):
                for i, d in enumerate(attrs):
                    tn = d.get("technicalName") or d.get("name")
                    if isinstance(tn, str):
                        name_to_idx[tn] = i

        return _TypeSchema(attr_count=attr_count, attr_name_to_index=name_to_idx, meta_keys=meta_keys)

    def ingest(self, raw: dict[str, Any], src_gid: int, tgt_gid: int) -> None:
        # tmp.edges.bin: (U32 src_gid, U32 tgt_gid) encounter order; relid = row index
        self._edges_f.write(struct.pack("<II", int(src_gid), int(tgt_gid)))

        # endpoints inference (for relations.json helper)
        self.src_types.add(self.resolver.citype_of_gid(src_gid))
        self.tgt_types.add(self.resolver.citype_of_gid(tgt_gid))

        # attributes row (grid)
        if self.schema.attr_count > 0 and self._attr_f is not None:
            buf = bytearray(self._sent_attr_row)
            for tn, val in _iter_attribute_pairs(raw):
                aidx = self.schema.attr_name_to_index.get(tn)
                if aidx is None:
                    self.unknown_attr += 1
                    continue
                didx = self.dict.index(val)
                if didx is None:
                    self.missing_dict += 1
                    continue
                I32_LE.pack_into(buf, aidx * 4, int(didx))
            self._attr_f.write(buf)

        # metaAttributes row (ALWAYS 6, grid, separate)
        meta = _extract_meta_dict(raw)
        bufm = bytearray(self._sent_meta_row)
        for col, k in enumerate(self.schema.meta_keys):
            if k not in meta:
                continue
            didx = self.dict.index(meta.get(k))
            if didx is None:
                self.missing_dict += 1
                continue
            I32_LE.pack_into(bufm, col * 4, int(didx))
        self._meta_f.write(bufm)

        self.count += 1

    def finalize(self) -> None:
        self.close()

        def _rename(tmp: Path, final: Path) -> None:
            if final.exists():
                final.unlink()
            tmp.replace(final)

        # metaAttributes.bin always exists
        _rename(self.meta_tmp, self.rel_dir / "metaAttributes.bin")

        # attributes.bin may be omitted if attr_count == 0
        if self.schema.attr_count > 0:
            _rename(self.attr_tmp, self.rel_dir / "attributes.bin")
        else:
            if self.attr_tmp.exists():
                self.attr_tmp.unlink(missing_ok=True)

        # endpoints.json (if not already there from metadata, create inferred one)
        ep_path = self.rel_dir / "endpoints.json"
        if not ep_path.is_file():
            obj = {
                "reltype": self.reltype,
                "sourceTypes": sorted(self.src_types),
                "targetTypes": sorted(self.tgt_types),
            }

            def _w(f):
                s = json.dumps(obj, ensure_ascii=False, indent=2) + "\n"
                f.write(s.encode("utf-8"))

            atomic_write_with(ep_path, _w)


###########
# Packers #
###########

class NodeGridPacker:
    def __init__(self, nodes_root: Path, dict_lookup: DictLookup, meta_keys: Tuple[str, ...], *, verbose: bool):
        self.nodes_root = nodes_root
        self.dict = dict_lookup
        self.meta_keys = meta_keys
        self.verbose = verbose
        self._writers: dict[str, _CitypeNodeWriter] = {}
        self.seen = 0
        self.skipped = 0

    def close(self) -> None:
        for w in list(self._writers.values()):
            w.close()
        self._writers.clear()

    def _writer_for(self, citype: str) -> _CitypeNodeWriter:
        w = self._writers.get(citype)
        if w is not None:
            return w
        ci_dir = self.nodes_root / citype
        if not ci_dir.is_dir():
            raise FileNotFoundError(f"Unknown citype dir in packed nodes: {ci_dir}")
        w = _CitypeNodeWriter(ci_dir, self.dict, self.meta_keys, verbose=self.verbose)
        self._writers[citype] = w
        return w

    def ingest(self, obj: Any) -> None:
        if not isinstance(obj, dict):
            self.skipped += 1
            return
        citype = obj.get("type")
        if not isinstance(citype, str) or not citype:
            self.skipped += 1
            return
        self._writer_for(citype).ingest(obj)
        self.seen += 1

    def finalize(self) -> None:
        if self.verbose:
            print(f"[pass2] nodes finalize: writers={len(self._writers)} records={self.seen} skipped={self.skipped}")
            for citype, w in self._writers.items():
                if w.unknown_attr or w.missing_uuid or w.not_found_uuid or w.missing_dict:
                    print(
                        f"[pass2] nodes[{citype}] unknown_attr={w.unknown_attr} "
                        f"missing_uuid={w.missing_uuid} not_found_uuid={w.not_found_uuid} missing_dict={w.missing_dict}"
                    )
        self.close()


class RelationGridPacker:
    def __init__(
        self,
        rels_root: Path,
        dict_lookup: DictLookup,
        gu: GlobalUuidIndex,
        gr: GlobalResolverIndex,
        meta_keys: Tuple[str, ...],
        *,
        verbose: bool,
    ):
        self.rels_root = rels_root
        self.dict = dict_lookup
        self.gu = gu
        self.gr = gr
        self.meta_keys = meta_keys
        self.verbose = verbose

        self._writers: dict[str, _ReltypeWriter] = {}
        self.seen = 0
        self.skipped = 0
        self.bad_uuid = 0

    def close(self) -> None:
        for w in list(self._writers.values()):
            try:
                w.close()
            except Exception:
                pass
        self._writers.clear()

    def _writer_for(self, reltype: str) -> _ReltypeWriter:
        w = self._writers.get(reltype)
        if w is not None:
            return w
        rel_dir = self.rels_root / reltype
        rel_dir.mkdir(parents=True, exist_ok=True)
        w = _ReltypeWriter(rel_dir, self.dict, self.gr, self.meta_keys, verbose=self.verbose)
        self._writers[reltype] = w
        return w

    def ingest(self, obj: Any) -> None:
        if not isinstance(obj, dict):
            self.skipped += 1
            return

        reltype = obj.get("type")
        if not isinstance(reltype, str) or not reltype:
            self.skipped += 1
            return

        su = obj.get("startUuid")
        tu = obj.get("endUuid")
        if not isinstance(su, str) or not su or not isinstance(tu, str) or not tu:
            self.bad_uuid += 1
            return

        src_gid = self.gu.find_gid(su)
        tgt_gid = self.gu.find_gid(tu)
        if src_gid is None or tgt_gid is None:
            self.bad_uuid += 1
            return

        self._writer_for(reltype).ingest(obj, int(src_gid), int(tgt_gid))
        self.seen += 1

    def finalize(self) -> None:
        if self.verbose:
            print(f"[pass2] rels finalize: reltypes={len(self._writers)} records={self.seen} skipped={self.skipped} bad_uuid={self.bad_uuid}")
        for _reltype, w in self._writers.items():
            w.finalize()
        self.close()


#############################################
# relations.json helper (bySource/byTarget) #
#############################################

def write_rels_manifest_atomic(rels_root: Path) -> None:
    by_source: dict[str, list[str]] = {}
    by_target: dict[str, list[str]] = {}

    for ent in rels_root.iterdir():
        if not ent.is_dir():
            continue
        reltype = ent.name
        ep_path = ent / "endpoints.json"
        if not ep_path.is_file():
            continue
        try:
            ep = json.loads(ep_path.read_text("utf-8"))
        except Exception:
            continue

        st = ep.get("sourceTypes")
        tt = ep.get("targetTypes")

        if isinstance(st, list):
            for s in st:
                if isinstance(s, str):
                    by_source.setdefault(s, []).append(reltype)
        if isinstance(tt, list):
            for t in tt:
                if isinstance(t, str):
                    by_target.setdefault(t, []).append(reltype)

    def _sort_dedupe(v: list[str]) -> list[str]:
        v = sorted(v)
        out: list[str] = []
        last = None
        for x in v:
            if x != last:
                out.append(x)
                last = x
        return out

    for k in list(by_source.keys()):
        by_source[k] = _sort_dedupe(by_source[k])
    for k in list(by_target.keys()):
        by_target[k] = _sort_dedupe(by_target[k])

    out = {
        "bySource": {k: by_source[k] for k in sorted(by_source.keys())},
        "byTarget": {k: by_target[k] for k in sorted(by_target.keys())},
    }

    final = rels_root / "relations.json"

    def _w(f):
        s = json.dumps(out, ensure_ascii=False, indent=2) + "\n"
        f.write(s.encode("utf-8"))

    atomic_write_with(final, _w)


####################
# Main entry point #
####################

def pack_nodes_and_relations(layout, *, skip_bad_json: bool = False, verbose: bool = True) -> None:
    packed_root = Path(getattr(layout, "packed_root", Path(layout.date_root) / "packed"))

    dict_dir = Path(getattr(layout, "dict_dir", packed_root / "dict"))
    uuids_dir = Path(getattr(layout, "uuids_dir", packed_root / "uuids"))
    nodes_root = Path(getattr(layout, "nodes_packed", packed_root / "nodes"))
    rels_root = Path(getattr(layout, "rels_packed", packed_root / "relations"))

    # raw roots (your spec names: nodes + relations)
    date_root = Path(getattr(layout, "date_root"))
    raw_nodes_dir = Path(getattr(layout, "raw_nodes_dir", date_root / "nodes"))
    raw_rels_dir = Path(getattr(layout, "raw_rels_dir", date_root / "relations"))

    if verbose:
        print("[pass2] starting")

    if not is_done(packed_root, ".pass1_5.done"):
        raise RuntimeError("Pass 2 requires Pass 1.5 outputs")

    if is_done(packed_root, ".pass2.done"):
        if verbose:
            print("[pass2] already done; skipping")
        return

    # meta key order is defined by pass0 outputs and is deterministic
    node_meta_keys = load_meta_keys_strict(nodes_root / "metaAttributes.json")
    rel_meta_keys = load_meta_keys_strict(rels_root / "metaAttributes.json")

    with DictLookup(dict_dir) as dlookup, GlobalUuidIndex(uuids_dir) as gu:
        dlookup.load(verbose=verbose)
        gu.load()
        if verbose:
            print(f"[pass2] uuids loaded, N={gu.node_count}")

        with GlobalResolverIndex(uuids_dir, expected_rows=gu.node_count) as gr:
            gr.load()
            if verbose:
                print("[pass2] resolver loaded")

            # ---- nodes ----
            if not is_done(packed_root, ".pass2.nodes.done"):
                if verbose:
                    print("[pass2] packing nodes")

                pages_dir = raw_nodes_dir / "pages"
                shards = list_shards_by_meta(pages_dir, "nodes")
                if verbose:
                    print(f"[pass2] nodes shards={len(shards)}")

                packer = NodeGridPacker(nodes_root, dlookup, node_meta_keys, verbose=verbose)
                last_shard = -1
                for rec in ndjson_json_range(pages_dir, "nodes", skip_bad_json=skip_bad_json):
                    if verbose and rec.shard_index != last_shard:
                        last_shard = rec.shard_index
                        print(f"[pass2] nodes shard {last_shard+1}/{rec.shard_count} (offset={rec.shard_offset})")
                    packer.ingest(rec.obj)
                packer.finalize()

                mark_done(packed_root, ".pass2.nodes.done", "pass=2\nkind=nodes\n")
                if verbose:
                    print("[pass2] nodes done")
            else:
                if verbose:
                    print("[pass2] nodes already done; skipping")

            # ---- relations ----
            if not is_done(packed_root, ".pass2.rels.done"):
                if verbose:
                    print("[pass2] packing relations")

                pages_dir = raw_rels_dir / "pages"
                shards = list_shards_by_meta(pages_dir, "rels")
                if verbose:
                    print(f"[pass2] rels shards={len(shards)}")

                rpacker = RelationGridPacker(rels_root, dlookup, gu, gr, rel_meta_keys, verbose=verbose)
                last_shard = -1
                for rec in ndjson_json_range(pages_dir, "rels", skip_bad_json=skip_bad_json):
                    if verbose and rec.shard_index != last_shard:
                        last_shard = rec.shard_index
                        print(f"[pass2] rels shard {last_shard+1}/{rec.shard_count} (offset={rec.shard_offset})")
                    rpacker.ingest(rec.obj)
                rpacker.finalize()

                mark_done(packed_root, ".pass2.rels.done", "pass=2\nkind=rels\n")
                write_rels_manifest_atomic(rels_root)

                if verbose:
                    print("[pass2] rels done")
            else:
                if verbose:
                    print("[pass2] rels already done; skipping")

    if is_done(packed_root, ".pass2.nodes.done") and is_done(packed_root, ".pass2.rels.done"):
        mark_done(packed_root, ".pass2.done", "pass=2\n")
        if verbose:
            print("[pass2] done")