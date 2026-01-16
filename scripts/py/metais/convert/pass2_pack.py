from __future__ import annotations

import json
import mmap
import struct
import hashlib
import uuid as _uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, Optional, Tuple, Union

from metais.common.binary_io import (
    I32_LE,
    i32_sentinel_row,
    UUID_BYTES,
)
from metais.packed_reader.resolver import GlobalResolver
from metais.common.atomic_write import atomic_write_with
from metais.common.step_marker import is_done, mark_done
from metais.common.uuid_search import find_uuid_index_u
from metais.common.shards import list_shards_by_meta
from metais.common.packed_spec import load_meta_keys_strict
from .ndjson_stream import ndjson_json_range
from metais.common.json_utils import canonical_value

def _sha_allow(s: set[str] | None) -> str:
    if not s:
        return ""
    h = hashlib.sha256()
    for x in sorted(s):
        h.update(x.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()

def _marker_has_sha(marker: Path, key: str, want_sha: str) -> bool:
    if not marker.exists():
        return False
    txt = marker.read_text("utf-8", errors="ignore")
    return f"{key}={want_sha}" in txt

def _should_skip(marker: Path, *, allow_sha_key: str, allow_sha: str, allow_is_none: bool) -> bool:
    # If no allowlist was used, any existing marker is fine (old behavior).
    if allow_is_none:
        return marker.exists()
    # If allowlist was used, skip ONLY if marker matches sha.
    return _marker_has_sha(marker, allow_sha_key, allow_sha)

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
        b = canonical_value(obj).encode("utf-8")
        return self._map.get(b)


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

def _preallocate_file_i32_grid(path: Path, rows: int, cols: int) -> None:
    # allowed to be empty if rows==0 or cols==0
    if rows == 0 or cols == 0:
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


def _load_type_schema(dir_: Path, meta_keys: Tuple[str, ...], *, attrs_optional: bool) -> _TypeSchema:
    fmt = json.loads((dir_ / "format.json").read_text("utf-8"))
    attr_count = fmt.get("attributeCount")
    meta_count = fmt.get("metaAttributeCount")

    if not isinstance(attr_count, int) or attr_count < 0:
        raise ValueError(f"{dir_}/format.json: bad attributeCount")
    if meta_count != len(meta_keys):
        raise ValueError(
            f"{dir_}/format.json: metaAttributeCount={meta_count} "
            f"but metaAttributes.json has {len(meta_keys)}"
        )

    name_to_idx: dict[str, int] = {}
    attrs_path = dir_ / "attributes.json"

    if attrs_path.is_file():
        attrs = json.loads(attrs_path.read_text("utf-8"))
        if isinstance(attrs, list) and all(isinstance(x, str) for x in attrs):
            for i, tn in enumerate(attrs):
                name_to_idx[tn] = i
        elif isinstance(attrs, list) and all(isinstance(x, dict) for x in attrs):
            for i, d in enumerate(attrs):
                tn = d.get("technicalName") or d.get("name")
                if isinstance(tn, str) and tn:
                    name_to_idx[tn] = i
        else:
            raise TypeError(f"{attrs_path} must be list[str] or list[dict]")
    else:
        if not attrs_optional:
            raise FileNotFoundError(attrs_path)

    return _TypeSchema(attr_count=attr_count, attr_name_to_index=name_to_idx, meta_keys=meta_keys)

class _CitypeNodeWriter:
    def __init__(self, citype_dir: Path, dict_lookup: DictLookup, meta_keys: Tuple[str, ...], *, verbose: bool):
        self.citype_dir = citype_dir
        self.dict = dict_lookup
        self.verbose = verbose

        self.schema = _load_type_schema(citype_dir, meta_keys, attrs_optional=False)
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

    def _preallocate_and_mmap(self) -> None:
        _preallocate_file_i32_grid(self.attr_path, self.row_count, self.schema.attr_count)
        _preallocate_file_i32_grid(self.meta_path, self.row_count, len(self.schema.meta_keys))

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
        meta_keys: Tuple[str, ...],
        *,
        verbose: bool,
    ):
        self.rel_dir = rel_dir
        self.dict = dict_lookup
        self.verbose = verbose

        self.schema = _load_type_schema(rel_dir, meta_keys, attrs_optional=True)
        self.reltype = rel_dir.name

        # --- reltype local uuid index (drives rel_local_index) ---
        self.uuid_index = LocalUuidIndex(rel_dir)
        self.uuid_index.load()
        self.row_count = self.uuid_index.local_count

        # --- outputs ---
        self.edges_tmp = rel_dir / "tmp.edges.bin"
        self.attr_path = rel_dir / "attributes.bin"
        self.meta_path = rel_dir / "metaAttributes.bin"

        self._edges_f = open(self.edges_tmp, "wb")

        self._attr_f = None
        self._meta_f = None
        self._attr_mm: Optional[mmap.mmap] = None
        self._meta_mm: Optional[mmap.mmap] = None

        self._attr_row_width = self.schema.attr_count * 4
        self._meta_row_width = len(self.schema.meta_keys) * 4  # typically 6*4

        self._sent_attr_row = b"\xFF\xFF\xFF\xFF" * self.schema.attr_count
        self._sent_meta_row = b"\xFF\xFF\xFF\xFF" * len(self.schema.meta_keys)

        self._seen = bytearray(self.row_count)

        self._preallocate_and_mmap()

        # stats
        self.count = 0
        self.src_types: set[str] = set()
        self.tgt_types: set[str] = set()
        self.unknown_attr = 0
        self.missing_dict = 0
        self.missing_uuid = 0
        self.not_found_uuid = 0

    def close(self) -> None:
        # flush/close mmaps first
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

        try:
            self._edges_f.close()
        except Exception:
            pass

        self.uuid_index.close()

    def _preallocate_and_mmap(self) -> None:
        _preallocate_file_i32_grid(self.attr_path, self.row_count, self.schema.attr_count)
        _preallocate_file_i32_grid(self.meta_path, self.row_count, len(self.schema.meta_keys))

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

    def ingest(self, raw: dict[str, Any], rel_uuid: str, src_gid: int, tgt_gid: int, src_type: str, tgt_type: str) -> None:
        # rel_local_index from reltype/uuids.bin
        if not isinstance(rel_uuid, str) or not rel_uuid:
            self.missing_uuid += 1
            return

        li = self.uuid_index.find_local_index(rel_uuid)
        if li is None:
            self.not_found_uuid += 1
            return

        # tmp.edges.bin triples: (src_gid, tgt_gid, rel_local_index)
        self._edges_f.write(struct.pack("<III", int(src_gid), int(tgt_gid), int(li)))

        # endpoints inference
        self.src_types.add(src_type)
        self.tgt_types.add(tgt_type)

        # reset row if duplicate
        if self._seen[li]:
            if self._attr_mm is not None and self._attr_row_width:
                o = li * self._attr_row_width
                self._attr_mm[o : o + self._attr_row_width] = self._sent_attr_row
            if self._meta_mm is not None and self._meta_row_width:
                o = li * self._meta_row_width
                self._meta_mm[o : o + self._meta_row_width] = self._sent_meta_row
        else:
            self._seen[li] = 1

        # attributes row write at offset li
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

        # metaAttributes row write at offset li
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

        self.count += 1

    def finalize(self) -> None:
        self.close()

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
    def __init__(
        self, nodes_root: Path,
        dict_lookup: DictLookup,
        meta_keys: Tuple[str, ...],
        *,
        verbose: bool,
        node_uuid_allow: set[str] | None = None
    ):
        self.nodes_root = nodes_root
        self.dict = dict_lookup
        self.meta_keys = meta_keys
        self.verbose = verbose
        self._writers: dict[str, _CitypeNodeWriter] = {}
        self.seen = 0
        self.skipped = 0
        self.skipped_allow = 0
        self.node_uuid_allow = node_uuid_allow

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
        uuid_str = obj.get("uuid")
        if self.node_uuid_allow is not None:
            if not isinstance(uuid_str, str) or uuid_str not in self.node_uuid_allow:
                self.skipped_allow += 1
                self.skipped += 1
                return
        self._writer_for(citype).ingest(obj)
        self.seen += 1

    def finalize(self) -> None:
        if self.verbose:
            print(f"[pass2] nodes finalize: writers={len(self._writers)} records={self.seen} skipped={self.skipped} skipped_allow={self.skipped_allow}")
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
        gr: GlobalResolver,
        meta_keys: Tuple[str, ...],
        *,
        verbose: bool,
        rel_uuid_allow: set[str] | None = None
    ):
        self.rels_root = rels_root
        self.dict = dict_lookup
        self.gr = gr
        self.meta_keys = meta_keys
        self.verbose = verbose

        self._writers: dict[str, _ReltypeWriter] = {}
        self.seen = 0
        self.skipped = 0
        self.bad_uuid = 0
        self.skipped_allow = 0
        self.rel_uuid_allow = rel_uuid_allow

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
        if not rel_dir.is_dir():
            raise FileNotFoundError(f"Unknown reltype dir in packed relations: {rel_dir}")
        w = _ReltypeWriter(rel_dir, self.dict, self.meta_keys, verbose=self.verbose)
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

        ru = obj.get("uuid")
        if self.rel_uuid_allow is not None:
            if not isinstance(ru, str) or ru not in self.rel_uuid_allow:
                self.skipped_allow += 1
                self.skipped += 1
                return

        su = obj.get("startUuid")
        tu = obj.get("endUuid")
        if not isinstance(ru, str) or not ru or not isinstance(su, str) or not su or not isinstance(tu, str) or not tu:
            self.bad_uuid += 1
            return

        src = self.gr.resolve_uuid(su)
        tgt = self.gr.resolve_uuid(tu)
        if src is None or tgt is None:
            self.bad_uuid += 1
            return

        src_gid, src_ci, _src_li = src
        tgt_gid, tgt_ci, _tgt_li = tgt

        src_type = self.gr.type_names[int(src_ci)]
        tgt_type = self.gr.type_names[int(tgt_ci)]

        self._writer_for(reltype).ingest(obj, ru, int(src_gid), int(tgt_gid), src_type, tgt_type)
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

def write_rels_manifest_atomic(layout) -> None:
    by_source: dict[str, list[str]] = {}
    by_target: dict[str, list[str]] = {}

    rels_root = Path(layout.rels_packed)
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

    final = Path(layout.rels_index_json)

    def _w(f):
        s = json.dumps(out, ensure_ascii=False, indent=2) + "\n"
        f.write(s.encode("utf-8"))

    atomic_write_with(final, _w)


####################
# Main entry point #
####################

def pack_nodes_and_relations(
    layout,
    *,
    skip_bad_json: bool = False,
    verbose: bool = True,
    node_uuid_allow: set[str] | None = None,
    rel_uuid_allow: set[str] | None = None,
) -> None:
    packed_root = Path(getattr(layout, "packed_root", Path(layout.date_root) / "packed"))

    dict_dir = Path(getattr(layout, "dict_dir", packed_root / "dict"))
    uuids_dir = Path(getattr(layout, "nodes_uuids_dir", packed_root / "nodes_uuids"))
    nodes_root = Path(getattr(layout, "nodes_packed", packed_root / "nodes"))
    rels_root = Path(getattr(layout, "rels_packed", packed_root / "relations"))

    # raw roots (your spec names: nodes + relations)
    date_root = Path(getattr(layout, "date_root"))
    raw_nodes_dir = Path(getattr(layout, "raw_nodes_dir", date_root / "nodes"))
    raw_rels_dir = Path(getattr(layout, "raw_rels_dir", date_root / "relations"))

    if verbose:
        print("[pass2] starting")

    node_sha = _sha_allow(node_uuid_allow)
    rel_sha  = _sha_allow(rel_uuid_allow)
    node_allow_none = (node_uuid_allow is None)
    rel_allow_none  = (rel_uuid_allow is None)

    if not is_done(packed_root, ".pass1_5.done"):
        raise RuntimeError("Pass 2 requires Pass 1.5 outputs")

    pass2_marker = packed_root / ".pass2.done"
    if _should_skip(pass2_marker, allow_sha_key="node_allow_sha256", allow_sha=node_sha, allow_is_none=node_allow_none) \
    and _should_skip(pass2_marker, allow_sha_key="rel_allow_sha256",  allow_sha=rel_sha,  allow_is_none=rel_allow_none):
        if verbose:
            print("[pass2] already done for same allowlists; skipping")
        return

    # meta key order is defined by pass0 outputs and is deterministic
    node_meta_keys = load_meta_keys_strict(nodes_root / "metaAttributes.json")
    rel_meta_keys = load_meta_keys_strict(rels_root / "metaAttributes.json")

    with DictLookup(dict_dir) as dlookup, GlobalResolver(uuids_dir, cache_size=65536) as gr:
        dlookup.load(verbose=verbose)
        if verbose:
            print(f"[pass2] uuids+resolver loaded, N={gr.node_count}")

        # ---- nodes ----
        nodes_marker = packed_root / ".pass2.nodes.done"
        if not _should_skip(nodes_marker, allow_sha_key="node_allow_sha256", allow_sha=node_sha, allow_is_none=node_allow_none):
            if verbose:
                print("[pass2] packing nodes")

            pages_dir = raw_nodes_dir / "pages"
            shards = list_shards_by_meta(pages_dir, "nodes")
            if verbose:
                print(f"[pass2] nodes shards={len(shards)}")

            packer = NodeGridPacker(nodes_root, dlookup, node_meta_keys, verbose=verbose, node_uuid_allow=node_uuid_allow)
            last_shard = -1
            for rec in ndjson_json_range(pages_dir, "nodes", skip_bad_json=skip_bad_json):
                if verbose and rec.shard_index != last_shard:
                    last_shard = rec.shard_index
                    print(f"[pass2] nodes shard {last_shard+1}/{rec.shard_count} (offset={rec.shard_offset})")
                packer.ingest(rec.obj)
            packer.finalize()

            mark_done(
                packed_root,
                ".pass2.nodes.done",
                "pass=2\nkind=nodes\n"
                f"node_allow_sha256={node_sha}\n"
            )
            if verbose:
                print("[pass2] nodes done")
        else:
            if verbose:
                print("[pass2] nodes already done for same allowlist; skipping")

        # ---- relations ----
        rels_marker = packed_root / ".pass2.rels.done"
        if not _should_skip(rels_marker, allow_sha_key="rel_allow_sha256", allow_sha=rel_sha, allow_is_none=rel_allow_none):
            if verbose:
                print("[pass2] packing relations")

            pages_dir = raw_rels_dir / "pages"
            shards = list_shards_by_meta(pages_dir, "rels")
            if verbose:
                print(f"[pass2] rels shards={len(shards)}")

            rpacker = RelationGridPacker(rels_root, dlookup, gr, rel_meta_keys, verbose=verbose, rel_uuid_allow=rel_uuid_allow)
            last_shard = -1
            for rec in ndjson_json_range(pages_dir, "rels", skip_bad_json=skip_bad_json):
                if verbose and rec.shard_index != last_shard:
                    last_shard = rec.shard_index
                    print(f"[pass2] rels shard {last_shard+1}/{rec.shard_count} (offset={rec.shard_offset})")
                rpacker.ingest(rec.obj)
            rpacker.finalize()

            mark_done(
                packed_root,
                ".pass2.rels.done",
                "pass=2\nkind=rels\n"
                f"rel_allow_sha256={rel_sha}\n"
            )
            write_rels_manifest_atomic(layout)

            if verbose:
                print("[pass2] rels done")
        else:
            if verbose:
                print("[pass2] rels already done for same allowlist; skipping")
