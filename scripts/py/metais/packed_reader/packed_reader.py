from __future__ import annotations

import json
import mmap
import struct
import uuid as _uuid
from dataclasses import dataclass
from pathlib import Path
from os import PathLike
from typing import Any, Dict, Iterator, Iterable, Optional, Union, Tuple, List
from collections import OrderedDict
from datetime import date, datetime

from .dict_reader import DictReader
from .attribute_reader import AttributeReader
from .relation_reader import RelationReader
from .resolver import GlobalResolver, LocalResolver, RelationGlobalResolver
from .bin_formats import Uuid128, UUID_U128_BE, UUID_BYTES, U32_LE, MISSING_I32
from metais.common.uri_config import load_uri_config
from metais.common.project_root import find_project_root
from metais.common.packed_spec import META_KEYS_6, META_STATE_MIDX, INVALID_STATE
from metais.common.directory_layout import DirectoryLayout
from metais.common.uuid_search import find_uuid_index_u

Pathish = Union[str, PathLike, Path]


@dataclass(frozen=True, slots=True)
class NodeRecord:
    gid: int
    citype: str
    citype_index: int
    local_index: int
    uuid_hi: int
    uuid_lo: int
    # Optional payloads (can be None if you ask PackedReader not to include them)
    attr_row: Optional[list[int]] = None     # len == attributeCount, MISSING_I32 sentinel
    meta_row: Optional[list[int]] = None     # len == metaAttributeCount, MISSING_I32 sentinel

    def uuid_obj(self) -> _uuid.UUID:
        return Uuid128(self.uuid_hi, self.uuid_lo).to_uuid()

    def uuid_str(self) -> str:
        return str(self.uuid_obj())

    def metais_url(self, base_url: str) -> str:
        base = str(base_url).rstrip("/")
        return f"{base}/ci/{self.citype}/{self.uuid_str()}"

@dataclass(frozen=True, slots=True)
class RelationRef:
    reltype: str
    relid: int

@dataclass(frozen=True, slots=True)
class RelationRecord:
    reltype: str
    relid: int
    uuid_hi: int
    uuid_lo: int
    # Optional payloads (None unless requested)
    attr_row: Optional[list[int]] = None
    meta_row: Optional[list[int]] = None

    def uuid_obj(self) -> _uuid.UUID:
        return Uuid128(self.uuid_hi, self.uuid_lo).to_uuid()

    def uuid_str(self) -> str:
        return str(self.uuid_obj())

class _RelUuidIndex:
    """
    Per-reltype UUID index:
      relations/<RELTYPE>/uuids.bin  (sorted by UUID)
    Supports:
      - relid -> uuid_hi/uuid_lo (direct offset read)
      - uuid_str -> relid (binary search)
    """
    def __init__(self, rel_dir: Path):
        self.rel_dir = rel_dir
        self._f = None
        self._mm: mmap.mmap | None = None
        self.count = 0

    def open(self) -> None:
        p = self.rel_dir / "uuids.bin"
        if not p.is_file():
            raise FileNotFoundError(p)
        self._f = p.open("rb")
        self._mm = mmap.mmap(self._f.fileno(), 0, access=mmap.ACCESS_READ)
        sz = len(self._mm)
        if sz % UUID_BYTES != 0:
            raise ValueError(f"{p} size not multiple of {UUID_BYTES}: {sz}")
        self.count = sz // UUID_BYTES

    def close(self) -> None:
        if self._mm is not None:
            try:
                self._mm.close()
            finally:
                self._mm = None
        if self._f is not None:
            try:
                self._f.close()
            finally:
                self._f = None

    def _require(self) -> mmap.mmap:
        if self._mm is None:
            raise RuntimeError("relation uuids index not open")
        return self._mm

    def uuid_hi_lo(self, relid: int) -> tuple[int, int]:
        mm = self._require()
        if relid < 0 or relid >= self.count:
            raise IndexError(f"relid out of range: {relid} (count={self.count})")
        hi, lo = UUID_U128_BE.unpack_from(mm, relid * UUID_BYTES)
        return int(hi), int(lo)

    def uuid_str(self, relid: int) -> str:
        hi, lo = self.uuid_hi_lo(relid)
        return str(Uuid128(hi, lo).to_uuid())

    def find_relid(self, uuid_str: str) -> int | None:
        mm = self._require()
        try:
            return find_uuid_index_u(mm, uuid_str, self.count)
        except Exception:
            return None

class PackedReader:
    """
    High-level facade over:
      - DictReader (global dictionary)
      - GlobalResolver (uuid <-> gid + gid -> (citype, local))
      - LocalResolver per citype (citype uuid -> gid)
      - AttributeReader per citype (node attributes/meta)
      - RelationReader per (reltype, SRC__TGT partition) with small LRU of open partitions

    Directory layout expected:

      packed_root/
        dict/
        uuids/
        nodes/<CITYPE>/
        relations/<RELTYPE>/edges/<SRC>__<TGT>/
        relations/relations.json (optional accelerator)

    Public API (core):
      - get_node_by_uuid(uuid_str, include_attrs=True, include_meta=True) -> NodeRecord | None
      - get_node_by_gid(gid, include_attrs=True, include_meta=True) -> NodeRecord
      - get_node_by_local(citype, local_index, ...) -> NodeRecord
      - iterate_citype(citype, ...) -> Iterator[NodeRecord]
      - traverse_all_citypes(...) -> Iterator[NodeRecord]
      - iterate_neighbors(gid, ...) -> yields neighbor gids (or tuples)
    """

    def __init__(
        self,
        packed_root: Pathish | None = None,
        *,
        layout: DirectoryLayout | None = None,
        date: str | None = None,
        path: Pathish | None = None,
        project_root: Pathish | None = None,
        # caches...
        dict_cache_size: int | None = 16_384,
        attr_cache_size: int | None = 16_384,
        resolver_cache_size: int | None = 65_536,
        open_relation_partitions_max: int | None = 32,
        require_exists: bool = True,
        uri: Pathish | None = None,
        base_url: str | None = None,
    ):
        selectors = [layout is not None, date is not None, path is not None, packed_root is not None]
        if sum(selectors) != 1:
            raise TypeError("Provide exactly one of: layout=..., date=..., path=..., or packed_root=...")

        if layout is not None:
            root = Path(layout.packed_root)
            self.root = root.resolve()

            self.dict_dir = Path(layout.dict_dir)
            self.nodes_uuids_dir = Path(layout.nodes_uuids_dir)
            self.nodes_dir = Path(layout.nodes_packed)
            self.rels_dir = Path(layout.rels_packed)

            self.rels_uuids_dir = Path(layout.rels_uuids_dir)
        else:
            proj_root = Path(project_root) if project_root is not None else find_project_root()
            if date is not None:
                root = proj_root / "output" / str(date) / "packed"
            elif path is not None:
                p = Path(path)
                root = p if p.is_absolute() else (proj_root / p)
            else:
                p = Path(packed_root)  # type: ignore[arg-type]
                root = p if p.is_absolute() else (proj_root / p)

            self.root = root.resolve()

            self.dict_dir = self.root / "dict"
            self.nodes_uuids_dir = self.root / "nodes_uuids"
            self.nodes_dir = self.root / "nodes"
            self.rels_dir = self.root / "relations"

            self.rels_uuids_dir = self.root / "relations_uuids"

        if require_exists and not self.root.is_dir():
            raise FileNotFoundError(f"PackedReader root not found: {self.root}")

        if base_url is not None:
            self.base_url = str(base_url).rstrip("/")
        else:
            # try to load URI.json (and env overrides), but don't print
            try:
                pr = Path(project_root) if project_root is not None else find_project_root(self.root)
            except Exception:
                pr = None

            cfg = load_uri_config(uri, project_root=pr, verbose=False)
            self.base_url = cfg.base_url.rstrip("/")

        self._closed = False

        # ---- meta keys (global, fixed) ----
        self._meta_keys = META_KEYS_6
        self._meta_name_to_index: dict[str, int] = {k: i for i, k in enumerate(self._meta_keys)}
        self._meta_count_expected = len(self._meta_keys)

        # ---- enums (lazy) ----
        self._enum_map: dict[str, Any] | None = None

        # Global singletons
        self._dict_cache_size = None if dict_cache_size is None else int(dict_cache_size)
        self._global_resolver_cached_size = None if resolver_cache_size is None else int(resolver_cache_size)

        self.dict = DictReader(self.dict_dir, cache_size=self._dict_cache_size)
        self.gr = GlobalResolver(self.nodes_uuids_dir, cache_size=self._global_resolver_cached_size)
        self.max_gid = max(0, self.gr.node_count - 1)

        # Per-citype caches
        self._attr_readers: Dict[str, AttributeReader] = {}
        self._local_resolvers: Dict[str, LocalResolver] = {}
        self._local_resolver_cache_size = None if resolver_cache_size is None else int(resolver_cache_size)
        self._attr_cache_size = None if attr_cache_size is None else int(attr_cache_size)

        # Relation caches
        self._rel_partitions: Dict[str, List[Tuple[str, str, Path]]] = {}
        self._rel_readers_lru: "OrderedDict[str, RelationReader]" = OrderedDict()
        self._rel_attr_readers: Dict[str, AttributeReader] = {}

        # dict-index -> validity (so we don't dict.get() repeatedly for state)
        self._state_valid_cache: dict[int, bool] = {}
        self._rel_open_max: int | None = None
        if open_relation_partitions_max is not None:
            self._rel_open_max = int(open_relation_partitions_max)
            if self._rel_open_max <= 0:
                raise ValueError("open_relation_partitions_max must be > 0 (or None for infinite)")

        # Optional accelerator index (relations.json)
        self._rel_index: dict | None = None

        # lazily built citype->index map
        self._citype_to_index: dict[str, int] | None = None
        self._rel_global_resolver: RelationGlobalResolver | None = None

        self._rel_uuid_indexes: dict[str, _RelUuidIndex] = {}

        self._citypes: list[str] = self._load_citypes_manifest()  # canonical order
        self._citype_to_index = {name: i for i, name in enumerate(self._citypes)}

        # reltype+relid -> (src_citype, src_local, tgt_citype, tgt_local)
        self._rel_endpoints_cache: dict[tuple[str, int], tuple[str, int, str, int]] = {}

    def _load_citypes_manifest(self) -> list[str]:
        p = self.nodes_dir / "citypes_manifest.json"
        if not p.is_file():
            # fallback: old layout / dev dumps
            return sorted([d.name for d in self.nodes_dir.iterdir() if d.is_dir()])

        obj = json.loads(p.read_text("utf-8"))
        if not isinstance(obj, dict) or "citypes" not in obj or not isinstance(obj["citypes"], list):
            raise ValueError(f"Invalid citypes_manifest.json format: {p}")

        citypes = [x for x in obj["citypes"] if isinstance(x, str)]
        if not citypes:
            raise ValueError(f"citypes_manifest.json has empty citypes list: {p}")
        return citypes

    # ----------------- validity helpers -----------------

    def _state_didx_is_valid(self, state_didx: int) -> bool:
        # missing sentinel => treat as valid
        if state_didx == MISSING_I32:
            return True
        cached = self._state_valid_cache.get(state_didx)
        if cached is not None:
            return cached
        v = self.dict.get(state_didx)
        ok = (v != INVALID_STATE)
        self._state_valid_cache[state_didx] = ok
        return ok

    def _node_valid_citype_local(self, citype: str, local_index: int) -> bool:
        ar = self._get_attr_reader(citype)
        if ar.meta_count == 0:
            return True
        # fast single-cell read
        state_didx = ar.get_meta_cell(local_index, META_STATE_MIDX)
        return self._state_didx_is_valid(state_didx)

    def _get_rel_attr_reader(self, reltype: str) -> Optional[AttributeReader]:
        self._require_open()
        ar = self._rel_attr_readers.get(reltype)
        if ar is not None:
            return ar

        rel_dir = self.rels_dir / reltype
        if not rel_dir.is_dir():
            return None

        ar = AttributeReader(rel_dir, cache_size=self._attr_cache_size)

        if ar.meta_count not in (0, self._meta_count_expected):
            raise ValueError(
                f"{reltype}: relation metaAttributeCount={ar.meta_count} but expected {self._meta_count_expected} "
                f"(meta keys: {list(self._meta_keys)})"
            )
        self._rel_attr_readers[reltype] = ar
        return ar

    def is_node_valid(self, node_or_gid) -> bool:
        """
        Validity of an entity node: meta.state != INVALIDATED (or meta missing => valid).
        """
        self._require_open()
        if isinstance(node_or_gid, NodeRecord):
            ar = self._get_attr_reader(node_or_gid.citype)
            if ar.meta_count == 0:
                return True
            if node_or_gid.meta_row is not None and len(node_or_gid.meta_row) > META_STATE_MIDX:
                return self._state_didx_is_valid(node_or_gid.meta_row[META_STATE_MIDX])
            # fall back to single-cell
            return self._node_valid_citype_local(node_or_gid.citype, node_or_gid.local_index)

        gid = node_or_gid
        if type(gid) is not int:
            raise TypeError("node_or_gid must be int or NodeRecord")
        citype, local = self.gr.resolve_gid_full(gid)
        return self._node_valid_citype_local(citype, local)

    def is_relation_valid(self, rel_or_reltype, relid: int | None = None) -> bool:
        """
        Validity of a relation: meta.state != INVALIDATED (or meta missing => valid).
        Requires reltype unless you pass RelationRef / RelationRecord.
        """
        self._require_open()

        if isinstance(rel_or_reltype, RelationRecord):
            reltype = rel_or_reltype.reltype
            rid = rel_or_reltype.relid
            ar = self._get_rel_attr_reader(reltype)
            if ar is None or ar.meta_count == 0:
                return True
            if rel_or_reltype.meta_row is not None and len(rel_or_reltype.meta_row) > META_STATE_MIDX:
                return self._state_didx_is_valid(rel_or_reltype.meta_row[META_STATE_MIDX])
            state_didx = ar.get_meta_cell(rid, META_STATE_MIDX)
            return self._state_didx_is_valid(state_didx)

        if isinstance(rel_or_reltype, RelationRef):
            reltype = rel_or_reltype.reltype
            rid = rel_or_reltype.relid
        else:
            reltype = rel_or_reltype
            if not isinstance(reltype, str):
                raise TypeError("reltype must be str (or RelationRef/RelationRecord)")
            if relid is None or type(relid) is not int:
                raise TypeError("relid must be int when reltype is provided as str")
            rid = relid

        ar = self._get_rel_attr_reader(reltype)
        if ar is None or ar.meta_count == 0:
            return True
        state_didx = ar.get_meta_cell(rid, META_STATE_MIDX)
        return self._state_didx_is_valid(state_didx)

    def valid(self, x, *, kind: str = "auto", reltype: str | None = None) -> bool:
        """
        Convenience dispatcher.
          kind="auto": NodeRecord -> node; Relation* -> relation; int -> node unless reltype=...
        """
        kind = (kind or "auto").lower()
        if kind not in ("auto", "node", "relation"):
            raise ValueError('kind must be "auto", "node", or "relation"')

        if kind == "node":
            return self.is_node_valid(x)
        if kind == "relation":
            if reltype is not None:
                return self.is_relation_valid(reltype, int(x))
            return self.is_relation_valid(x)

        # auto
        if isinstance(x, (RelationRef, RelationRecord)):
            return self.is_relation_valid(x)
        if isinstance(x, NodeRecord):
            return self.is_node_valid(x)
        if reltype is not None:
            return self.is_relation_valid(reltype, int(x))
        return self.is_node_valid(x)

    # ----------------- context manager + close -----------------

    def __enter__(self) -> "PackedReader":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def close(self) -> None:
        if self._closed:
            return

        errs: list[Exception] = []

        for _, rr in list(self._rel_readers_lru.items()):
            try:
                rr.close()
            except Exception as e:
                errs.append(e)
        self._rel_readers_lru.clear()

        for ar in list(self._attr_readers.values()):
            try:
                ar.close()
            except Exception as e:
                errs.append(e)
        self._attr_readers.clear()

        for ar in list(self._rel_attr_readers.values()):
            try:
                ar.close()
            except Exception as e:
                errs.append(e)
        self._rel_attr_readers.clear()

        for lr in list(self._local_resolvers.values()):
            try:
                lr.close()
            except Exception as e:
                errs.append(e)
        self._local_resolvers.clear()

        try:
            self.gr.close()
        except Exception as e:
            errs.append(e)

        try:
            self.dict.close()
        except Exception as e:
            errs.append(e)

        if self._rel_global_resolver is not None:
            try:
                self._rel_global_resolver.close()
            except Exception as e:
                errs.append(e)
            finally:
                self._rel_global_resolver = None

        for idx in list(self._rel_uuid_indexes.values()):
            try:
                idx.close()
            except Exception as e:
                errs.append(e)
        self._rel_uuid_indexes.clear()

        self._closed = True

        if errs:
            raise RuntimeError(f"PackedReader close(): {errs[0]}") from errs[0] 

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("PackedReader is closed")

    # ----------------- small JSON helper -----------------

    @staticmethod
    def _load_json(path: Path) -> object:
        return json.loads(path.read_text("utf-8"))

    # ------------------------ enums ------------------------

    def _ensure_enum_map(self) -> None:
        if self._enum_map is not None:
            return

        path = self.root.parent / "enums" / "enums_merged.json"
        if not path.is_file():
            self._enum_map = {}
            return

        obj = json.loads(path.read_text("utf-8"))
        if not isinstance(obj, dict):
            raise TypeError(f"enums_merged.json must be a JSON object, got {type(obj).__name__}")
        self._enum_map = obj

    # ----------------- per-citype openers -----------------

    def _get_attr_reader(self, citype: str) -> AttributeReader:
        self._require_open()
        ar = self._attr_readers.get(citype)
        if ar is not None:
            return ar

        path = self.nodes_dir / citype
        ar = AttributeReader(path, cache_size=self._attr_cache_size)

        # strict meta shape check (meta keys are global)
        if ar.meta_count not in (0, self._meta_count_expected):
            raise ValueError(
                f"{citype}: metaAttributeCount={ar.meta_count} but expected {self._meta_count_expected} "
                f"(meta keys: {list(self._meta_keys)})"
            )

        self._attr_readers[citype] = ar
        return ar

    def _get_local_resolver(self, citype: str) -> LocalResolver:
        self._require_open()
        lr = self._local_resolvers.get(citype)
        if lr is not None:
            return lr
        path = self.nodes_dir / citype
        lr = LocalResolver(path, cache_size=self._local_resolver_cache_size)
        self._local_resolvers[citype] = lr
        return lr

    def _resolve_citype_local(self, node_or_gid):
        if isinstance(node_or_gid, NodeRecord):
            return node_or_gid.citype, node_or_gid.local_index
        gid = node_or_gid
        if type(gid) is not int:
            raise TypeError("gid must be int or NodeRecord")
        citype, local = self.gr.resolve_gid_full(gid)
        return citype, local

    def _ensure_citype_index_map(self) -> dict[str, int]:
        if self._citype_to_index is None:
            self._citype_to_index = {name: i for i, name in enumerate(self.gr.citypes)}
        return self._citype_to_index

    def _try_get_rel_global_resolver(self) -> RelationGlobalResolver | None:
        self._require_open()
        if self._rel_global_resolver is not None:
            return self._rel_global_resolver

        base = Path(self.rels_uuids_dir)
        if not base.is_dir():
            return None
        if not (base / "uuids.bin").is_file():
            return None
        if not (base / "resolver.bin").is_file():
            return None
        if not (base / "reltypes.json").is_file():
            return None

        cache_sz = 4096 if self._global_resolver_cached_size is None else int(self._global_resolver_cached_size)
        self._rel_global_resolver = RelationGlobalResolver(base, cache_size=cache_sz)
        return self._rel_global_resolver


    def resolve_relation_uuid_any(self, uuid_str: str) -> tuple[str, int] | None:
        """
        UUID -> (reltype, relid)

        Fast path: global relations_uuids resolver (Option B).
        Fallback: scan reltypes and binary-search per-reltype uuids.bin.
        """
        rgr = self._try_get_rel_global_resolver()
        if rgr is not None:
            return rgr.resolve_uuid_full(uuid_str)

        # fallback (slower): try each reltype
        if self.rels_dir.is_dir():
            for p in self.rels_dir.iterdir():
                if not p.is_dir():
                    continue
                relid = self.resolve_relation_uuid(p.name, uuid_str)
                if relid is not None:
                    return (p.name, int(relid))
        return None


    def get_relation_by_uuid(
        self,
        uuid_str: str,
        *,
        include_attrs: bool = True,
        include_meta: bool = True,
    ) -> RelationRecord | None:
        r = self.resolve_relation_uuid_any(uuid_str)
        if r is None:
            return None
        reltype, relid = r
        return self.get_relation_by_relid(reltype, relid, include_attrs=include_attrs, include_meta=include_meta)

    def _find_relation_endpoints(self, reltype: str, relid: int) -> tuple[str, int, str, int] | None:
        """
        Locate the single edge instance that carries this relid within this reltype,
        returning (src_citype, src_local, tgt_citype, tgt_local).

        Note: this is a scan across partitions; cached after first hit.
        """
        self._require_open()
        key = (reltype, int(relid))
        hit = self._rel_endpoints_cache.get(key)
        if hit is not None:
            return hit

        parts = self._get_reltype_partitions(reltype)
        if not parts:
            return None

        rid = int(relid)
        for src, tgt, path in parts:
            rr = self._get_relation_reader(path)
            # rr.iter_edges yields (src_local, tgt_local, relid)
            for src_local, tgt_local, r in rr.iter_edges(by="src"):
                if int(r) == rid:
                    out = (src, int(src_local), tgt, int(tgt_local))
                    self._rel_endpoints_cache[key] = out
                    return out

        return None

    # ----------------- relation index + partitions -----------------

    def _ensure_rel_index(self) -> None:
        if self._rel_index is not None:
            return

        idx_path = self.rels_dir / "relations.json"
        if not idx_path.is_file():
            self._rel_index = {}
            return

        obj = self._load_json(idx_path)
        if not isinstance(obj, dict):
            raise TypeError("relations.json must be a JSON object")
        self._rel_index = obj

    def _reltypes_for_citype(self, citype: str, role: str) -> List[str]:
        self._ensure_rel_index()
        idx = self._rel_index or {}

        role = role.lower()
        if role not in ("source", "target", "either"):
            raise ValueError('role must be "source", "target", or "either"')

        has_index = isinstance(idx.get("bySource"), dict) or isinstance(idx.get("byTarget"), dict)
        out: List[str] = []

        if isinstance(idx.get("bySource"), dict) and role in ("source", "either"):
            rs = idx["bySource"].get(citype, [])
            if isinstance(rs, list):
                out.extend([x for x in rs if isinstance(x, str)])

        if isinstance(idx.get("byTarget"), dict) and role in ("target", "either"):
            rt = idx["byTarget"].get(citype, [])
            if isinstance(rt, list):
                out.extend([x for x in rt if isinstance(x, str)])

        if has_index:
            return sorted(set(out))

        # expensive fallback only if no index exists
        if self.rels_dir.is_dir():
            for p in self.rels_dir.iterdir():
                if p.is_dir():
                    out.append(p.name)

        return sorted(set(out))

    def _get_reltype_partitions(self, reltype: str) -> List[Tuple[str, str, Path]]:
        self._require_open()
        cached = self._rel_partitions.get(reltype)
        if cached is not None:
            return cached

        edges_root = self.rels_dir / reltype / "edges"
        parts: List[Tuple[str, str, Path]] = []
        if edges_root.is_dir():
            for d in edges_root.iterdir():
                if not d.is_dir():
                    continue
                name = d.name
                if "__" not in name:
                    continue
                src, tgt = name.split("__", 1)
                parts.append((src, tgt, d))

        self._rel_partitions[reltype] = parts
        return parts

    def _get_relation_reader(self, partition_path: Path) -> RelationReader:
        self._require_open()

        key = str(partition_path)
        rr = self._rel_readers_lru.get(key)
        if rr is not None:
            self._rel_readers_lru.move_to_end(key)
            return rr

        rr = RelationReader(partition_path)
        self._rel_readers_lru[key] = rr
        self._rel_readers_lru.move_to_end(key)

        if self._rel_open_max is not None:
            while len(self._rel_readers_lru) > self._rel_open_max:
                _, old_rr = self._rel_readers_lru.popitem(last=False)
                try:
                    old_rr.close()
                except Exception:
                    pass
        return rr

    # ----------------- typing + enum helpers -----------------

    @staticmethod
    def _force_list_if_array(v):
        if v is None:
            return []
        if isinstance(v, list):
            return v
        return [v]

    @staticmethod
    def _coerce_by_datatype(x, data_type: str | None):
        if x is None or data_type is None:
            return x

        dt = data_type.upper()

        if dt == "BOOLEAN":
            if isinstance(x, bool):
                return x
            if isinstance(x, (int, float)) and not isinstance(x, bool):
                return bool(x)
            if isinstance(x, str):
                s = x.strip().lower()
                if s in ("true", "1", "yes", "y", "áno", "ano", "t"):
                    return True
                if s in ("false", "0", "no", "n", "nie", "f"):
                    return False
            return x

        if dt in ("INTEGER", "LONG"):
            if isinstance(x, bool):
                return int(x)
            if isinstance(x, int):
                return x
            if isinstance(x, float) and x.is_integer():
                return int(x)
            if isinstance(x, str):
                s = x.strip()
                if s:
                    try:
                        return int(s)
                    except ValueError:
                        return x
            return x

        if dt in ("FLOAT", "DOUBLE", "DECIMAL", "NUMBER"):
            if isinstance(x, (int, float)) and not isinstance(x, bool):
                return float(x)
            if isinstance(x, str):
                s = x.strip()
                if s:
                    try:
                        return float(s)
                    except ValueError:
                        return x
            return x

        if dt in ("DATE", "DATETIME"):
            if isinstance(x, (date, datetime)):
                return x
            if isinstance(x, str):
                s = x.strip()
                if not s:
                    return x
                try:
                    # handle Zulu
                    s2 = s.replace("Z", "+00:00")
                    # MetaIS sometimes stores DATE as date-only OR datetime-ish
                    if dt == "DATE" and "T" not in s2 and " " not in s2:
                        return date.fromisoformat(s2)
                    return datetime.fromisoformat(s2)
                except Exception:
                    return x
            return x

        return x

    def _apply_enum(self, v, enum_mode: str):
        enum_mode = (enum_mode or "none").lower()
        if enum_mode == "none":
            return v
        if enum_mode not in ("value", "both"):
            raise ValueError('enum_mode must be "none", "value", or "both"')

        self._ensure_enum_map()
        enum_map = self._enum_map or {}

        def one(x):
            if not isinstance(x, str):
                return x
            label = enum_map.get(x)
            if enum_mode == "value":
                return label if label is not None else x
            return {"code": x, "label": label}

        if isinstance(v, list):
            return [one(x) for x in v]
        return one(v)

    # ----------------- core node getters -----------------

    @staticmethod
    def _uuid_hi_lo_from_str(u: str) -> Tuple[int, int]:
        uu = _uuid.UUID(u)
        x = uu.int
        hi = (x >> 64) & ((1 << 64) - 1)
        lo = x & ((1 << 64) - 1)
        return hi, lo

    def get_node_by_uuid(
        self,
        uuid_str: str,
        *,
        include_attrs: bool = True,
        include_meta: bool = True,
    ) -> Optional[NodeRecord]:
        self._require_open()
        r = self.gr.resolve_uuid(uuid_str)
        if r is None:
            return None
        gid, _, _ = r
        return self.get_node_by_gid(gid, include_attrs=include_attrs, include_meta=include_meta)

    def get_node_by_gid(
        self,
        gid: int,
        *,
        include_attrs: bool = True,
        include_meta: bool = True,
    ) -> NodeRecord:
        self._require_open()
        if type(gid) is not int:
            raise TypeError("gid must be int")
        if gid < 0 or gid >= self.gr.node_count:
            raise IndexError(f"gid out of range: {gid}")

        citype_index, local_index = self.gr.resolve_gid(gid)
        try:
            citype = self._citypes[int(citype_index)]
        except Exception:
            raise RuntimeError(f"citype_index out of range: {citype_index} (len(citypes)={len(self._citypes)})")

        u = self.gr.get_uuid(gid)
        x = u.int
        uuid_hi = (x >> 64) & ((1 << 64) - 1)
        uuid_lo = x & ((1 << 64) - 1)

        ar = self._get_attr_reader(citype)
        attr_row = ar.get_attr_row(local_index) if include_attrs else None
        meta_row = ar.get_meta_row(local_index) if include_meta else None

        return NodeRecord(
            gid=gid,
            citype=citype,
            citype_index=citype_index,
            local_index=local_index,
            uuid_hi=int(uuid_hi),
            uuid_lo=int(uuid_lo),
            attr_row=attr_row,
            meta_row=meta_row,
        )

    def _ui_url(self, path: str) -> str:
        path = (path or "").lstrip("/")
        return f"{self.base_url}/{path}" if path else self.base_url

    def entity_url(self, node_or_gid) -> str:
        """
        base_url/ci/<CITYPE>/<UUID>
        Accepts NodeRecord or gid (int).
        """
        self._require_open()

        if isinstance(node_or_gid, NodeRecord):
            citype = node_or_gid.citype
            uuid_s = node_or_gid.uuid_str()
            return self._ui_url(f"ci/{citype}/{uuid_s}")

        gid = node_or_gid
        if type(gid) is not int:
            raise TypeError("node_or_gid must be NodeRecord or int gid")

        citype_index, _ = self.gr.resolve_gid(gid)
        try:
            citype = self._citypes[int(citype_index)]
        except Exception:
            raise RuntimeError(f"citype_index out of range: {citype_index}")

        uuid_s = str(self.gr.get_uuid(gid))
        return self._ui_url(f"ci/{citype}/{uuid_s}")

    def get_node_by_local(
        self,
        citype: str,
        local_index: int,
        *,
        include_attrs: bool = True,
        include_meta: bool = True,
    ) -> NodeRecord:
        self._require_open()
        if not isinstance(citype, str):
            raise TypeError("citype must be str")
        if type(local_index) is not int:
            raise TypeError("local_index must be int")

        lr = self._get_local_resolver(citype)

        if local_index < 0 or local_index >= lr.local_count:
            raise IndexError(f"local_index out of range for {citype}: {local_index} (local_count={lr.local_count})")

        uu_mm = lr._uu_mm
        gid_mm = lr._gid_mm
        if uu_mm is None or gid_mm is None:
            raise RuntimeError("LocalResolver is closed")

        uuid_hi, uuid_lo = UUID_U128_BE.unpack_from(uu_mm, local_index * UUID_BYTES)
        (gid_u32,) = U32_LE.unpack_from(gid_mm, local_index * U32_LE.size)
        gid = int(gid_u32)

        citype_to_index = self._ensure_citype_index_map()
        citype_index = citype_to_index.get(citype, -1)
        if citype_index < 0:
            raise KeyError(f"Unknown citype (not in global citypes.json): {citype}")

        ar = self._get_attr_reader(citype)
        attr_row = ar.get_attr_row(local_index) if include_attrs else None
        meta_row = ar.get_meta_row(local_index) if include_meta else None

        return NodeRecord(
            gid=gid,
            citype=citype,
            citype_index=int(citype_index),
            local_index=int(local_index),
            uuid_hi=int(uuid_hi),
            uuid_lo=int(uuid_lo),
            attr_row=attr_row,
            meta_row=meta_row,
        )

    # ----------------- attribute getters -----------------

    def get_attribute_dict_index(self, node_or_gid, technical_name: str) -> int | None:
        self._require_open()
        if not isinstance(technical_name, str):
            raise TypeError("technical_name must be str")

        citype, local = self._resolve_citype_local(node_or_gid)
        ar = self._get_attr_reader(citype)

        aidx = ar.attr_index(technical_name)
        if aidx is None:
            return None

        if isinstance(node_or_gid, NodeRecord) and node_or_gid.attr_row is not None:
            didx = node_or_gid.attr_row[aidx]
            return None if didx == MISSING_I32 else didx

        row = ar.get_attr_row(local)
        didx = row[aidx]
        return None if didx == MISSING_I32 else didx

    def get_attr_value(self, node_or_gid, technical_name: str, default=None):
        didx = self.get_attribute_dict_index(node_or_gid, technical_name)
        if didx is None:
            return default
        return self.dict.get(didx)

    def get_attr_value_typed(
        self,
        node_or_gid,
        technical_name: str,
        *,
        default=None,
        enum_mode: str = "none",   # "none" | "value" | "both"
        return_info: bool = False,
    ):
        didx = self.get_attribute_dict_index(node_or_gid, technical_name)
        if didx is None:
            return default

        citype, _ = self._resolve_citype_local(node_or_gid)
        ar = self._get_attr_reader(citype)

        aidx = ar.attr_index(technical_name)
        if aidx is None:
            return default

        raw = self.dict.get(didx)

        data_type = (getattr(ar, "attr_data_types", [None])[aidx])
        is_array  = (getattr(ar, "attr_is_array",  [None])[aidx])
        has_enum  = (getattr(ar, "attr_has_enum",  [None])[aidx])

        v = raw

        if is_array is True:
            v = self._force_list_if_array(v)

        if isinstance(v, list):
            v = [self._coerce_by_datatype(x, data_type) for x in v]
        else:
            v = self._coerce_by_datatype(v, data_type)

        if has_enum is not None:
            v = self._apply_enum(v, enum_mode)

        if not return_info:
            return v

        return {
            "technicalName": technical_name,
            "dictIndex": int(didx),
            "dataType": data_type,
            "isArray": is_array,
            "hasEnum": has_enum,
            "value": v,
        }

    def get_attr_info(self, citype: str, technical_name: str) -> dict | None:
        self._require_open()
        ar = self._get_attr_reader(citype)
        aidx = ar.attr_index(technical_name)
        if aidx is None:
            return None
        return {
            "technicalName": ar.attr_tech_names[aidx],
            "name": ar.attr_human_names[aidx],
            "description": ar.attr_descriptions[aidx],
            "hasEnum": ar.attr_has_enum[aidx],
            "dataType": getattr(ar, "attr_data_types", [None])[aidx],
            "valid": getattr(ar, "attr_valid", [None])[aidx],
            "isArray": getattr(ar, "attr_is_array", [None])[aidx],
            "index": aidx,
        }

    def get_attributes(
        self,
        node_or_gid,
        *,
        include_meta: bool = False,
        include_missing: bool = False,
        meta_prefix: str | None = "meta.",
        raw_indices: bool = False,
    ) -> dict[str, Any] | tuple[dict[str, Any], dict[str, Any]]:
        self._require_open()

        citype, local = self._resolve_citype_local(node_or_gid)
        ar = self._get_attr_reader(citype)

        if isinstance(node_or_gid, NodeRecord) and node_or_gid.attr_row is not None:
            attr_row = node_or_gid.attr_row
        else:
            attr_row = ar.get_attr_row(local)

        out: dict[str, Any] = {}

        tech_names = ar.attr_tech_names
        if len(attr_row) != len(tech_names):
            raise RuntimeError(
                f"attr_row length mismatch for {citype}: row={len(attr_row)} tech_names={len(tech_names)}"
            )

        for i, name in enumerate(tech_names):
            didx = attr_row[i]
            if didx == MISSING_I32:
                if include_missing:
                    out[name] = None
                continue
            out[name] = didx if raw_indices else self.dict.get(didx)

        if not include_meta:
            return out

        if isinstance(node_or_gid, NodeRecord) and node_or_gid.meta_row is not None:
            meta_row = node_or_gid.meta_row
        else:
            meta_row = ar.get_meta_row(local)

        if ar.meta_count == 0:
            meta_out = {k: None for k in self._meta_keys} if include_missing else {}
        else:
            if ar.meta_count != self._meta_count_expected:
                raise RuntimeError(
                    f"{citype}: meta_count mismatch ar.meta_count={ar.meta_count} expected={self._meta_count_expected}"
                )
            if len(meta_row) != self._meta_count_expected:
                raise RuntimeError(
                    f"meta_row length mismatch for {citype}: row={len(meta_row)} expected={self._meta_count_expected}"
                )

            meta_out: dict[str, Any] = {}
            for i, name in enumerate(self._meta_keys):
                didx = meta_row[i]
                if didx == MISSING_I32:
                    if include_missing:
                        meta_out[name] = None
                    continue
                meta_out[name] = didx if raw_indices else self.dict.get(didx)

        if meta_prefix is None:
            return out, meta_out

        if meta_prefix:
            for k, v in meta_out.items():
                out[f"{meta_prefix}{k}"] = v
        else:
            out.update(meta_out)

        return out

    def get_attributes_typed(
        self,
        node_or_gid,
        *,
        include_meta: bool = False,
        include_missing: bool = False,
        meta_prefix: str | None = "meta.",
        enum_mode: str = "none",
        return_info: bool = False,
    ):
        self._require_open()

        citype, local = self._resolve_citype_local(node_or_gid)
        ar = self._get_attr_reader(citype)

        if isinstance(node_or_gid, NodeRecord) and node_or_gid.attr_row is not None:
            attr_row = node_or_gid.attr_row
        else:
            attr_row = ar.get_attr_row(local)

        tech_names = ar.attr_tech_names
        if len(attr_row) != len(tech_names):
            raise RuntimeError(
                f"attr_row length mismatch for {citype}: row={len(attr_row)} tech_names={len(tech_names)}"
            )

        data_types = getattr(ar, "attr_data_types", None)
        is_arrays  = getattr(ar, "attr_is_array", None)
        has_enums  = getattr(ar, "attr_has_enum", None)

        out: dict[str, Any] = {}

        for i, name in enumerate(tech_names):
            didx = attr_row[i]
            dt = data_types[i] if data_types else None
            ia = is_arrays[i]  if is_arrays  else None
            he = has_enums[i]  if has_enums  else None

            if didx == MISSING_I32:
                if include_missing:
                    out[name] = None if not return_info else {
                        "technicalName": name,
                        "dictIndex": None,
                        "dataType": dt,
                        "isArray": ia,
                        "hasEnum": he,
                        "value": None,
                    }
                continue

            raw = self.dict.get(didx)
            v = raw

            if ia is True:
                v = self._force_list_if_array(v)

            if isinstance(v, list):
                v = [self._coerce_by_datatype(x, dt) for x in v]
            else:
                v = self._coerce_by_datatype(v, dt)

            if he is not None:
                v = self._apply_enum(v, enum_mode)

            if not return_info:
                out[name] = v
            else:
                out[name] = {
                    "technicalName": name,
                    "dictIndex": int(didx),
                    "dataType": dt,
                    "isArray": ia,
                    "hasEnum": he,
                    "value": v,
                }

        if not include_meta:
            return out

        if isinstance(node_or_gid, NodeRecord) and node_or_gid.meta_row is not None:
            meta_row = node_or_gid.meta_row
        else:
            meta_row = ar.get_meta_row(local)

        if ar.meta_count == 0:
            meta_out = {k: None for k in self._meta_keys} if include_missing else {}
        else:
            if ar.meta_count != self._meta_count_expected:
                raise RuntimeError(
                    f"{citype}: meta_count mismatch ar.meta_count={ar.meta_count} expected={self._meta_count_expected}"
                )
            if len(meta_row) != self._meta_count_expected:
                raise RuntimeError(
                    f"meta_row length mismatch for {citype}: row={len(meta_row)} expected={self._meta_count_expected}"
                )

            meta_out: dict[str, Any] = {}
            for i, k in enumerate(self._meta_keys):
                didx = meta_row[i]
                if didx == MISSING_I32:
                    if include_missing:
                        meta_out[k] = None
                    continue
                meta_out[k] = self.dict.get(didx)

        if meta_prefix is None:
            return out, meta_out

        if meta_prefix:
            for k, v in meta_out.items():
                out[f"{meta_prefix}{k}"] = v
        else:
            out.update(meta_out)

        return out

    # ----------------- meta attributes -----------------

    @property
    def meta_keys(self) -> Tuple[str, ...]:
        return tuple(self._meta_keys)

    def get_meta_attribute_dict_index(self, node_or_gid, key: str) -> int | None:
        self._require_open()
        if not isinstance(key, str):
            raise TypeError("key must be str")

        midx = self._meta_name_to_index.get(key)
        if midx is None:
            return None

        citype, local = self._resolve_citype_local(node_or_gid)
        ar = self._get_attr_reader(citype)

        if ar.meta_count == 0:
            return None
        if ar.meta_count != self._meta_count_expected:
            raise RuntimeError(
                f"{citype}: meta_count mismatch ar.meta_count={ar.meta_count} expected={self._meta_count_expected}"
            )

        if isinstance(node_or_gid, NodeRecord) and node_or_gid.meta_row is not None:
            row = node_or_gid.meta_row
        else:
            row = ar.get_meta_row(local)

        didx = row[midx]
        return None if didx == MISSING_I32 else didx

    def get_meta_attr_value(self, node_or_gid, key: str, default=None):
        didx = self.get_meta_attribute_dict_index(node_or_gid, key)
        if didx is None:
            return default
        return self.dict.get(didx)

    def get_meta_attributes(
        self,
        node_or_gid,
        *,
        include_missing: bool = False,
        raw_indices: bool = False,
    ) -> dict[str, Any]:
        self._require_open()

        citype, local = self._resolve_citype_local(node_or_gid)
        ar = self._get_attr_reader(citype)

        if ar.meta_count == 0:
            return {k: None for k in self._meta_keys} if include_missing else {}

        if ar.meta_count != self._meta_count_expected:
            raise RuntimeError(
                f"{citype}: meta_count mismatch ar.meta_count={ar.meta_count} expected={self._meta_count_expected}"
            )

        if isinstance(node_or_gid, NodeRecord) and node_or_gid.meta_row is not None:
            row = node_or_gid.meta_row
        else:
            row = ar.get_meta_row(local)

        out: dict[str, Any] = {}
        for i, k in enumerate(self._meta_keys):
            didx = row[i]
            if didx == MISSING_I32:
                if include_missing:
                    out[k] = None
                continue
            out[k] = didx if raw_indices else self.dict.get(didx)

        return out

    # ----------------- citype traversal -----------------

    def iterate_citype(
        self,
        citype: str,
        *,
        include_attrs: bool = False,
        include_meta: bool = False,
        uuid_format: str = "hi_lo",  # "hi_lo" | "str" | "uuid"
        scan: bool = False,
        valid_only: bool = False,
    ) -> Iterator[NodeRecord]:
        self._require_open()
        uuid_format = uuid_format.lower()
        if uuid_format not in ("hi_lo", "uuid", "str"):
            raise ValueError('uuid_format must be "hi_lo", "uuid", or "str"')

        lr = self._get_local_resolver(citype)
        ar = self._get_attr_reader(citype)

        if include_attrs:
            if scan:
                get_attr = ar._get_attr_row_sparse if ar._layout == "sparse" else ar._get_attr_row_grid
            else:
                get_attr = ar.get_attr_row
        else:
            get_attr = None

        if include_meta:
            if scan:
                get_meta = ar._get_meta_row_grid
            else:
                get_meta = ar.get_meta_row
        else:
            get_meta = None

        uu_mm = lr._uu_mm
        gid_mm = lr._gid_mm
        if uu_mm is None or gid_mm is None:
            raise RuntimeError("LocalResolver is closed")

        citype_index = self._ensure_citype_index_map().get(citype, -1)
        if citype_index < 0:
            raise KeyError(f"Unknown citype (not in global citypes.json): {citype}")

        for local_index in range(lr.local_count):
            uuid_hi, uuid_lo = UUID_U128_BE.unpack_from(uu_mm, local_index * UUID_BYTES)
            (gid,) = U32_LE.unpack_from(gid_mm, local_index * U32_LE.size)

            # validity filter (no string key lookup; fast meta cell when possible)
            meta_row_for_check = None
            if valid_only and ar.meta_count != 0:
                if get_meta is not None:
                    meta_row_for_check = get_meta(local_index)
                    state_didx = meta_row_for_check[META_STATE_MIDX] if len(meta_row_for_check) > META_STATE_MIDX else MISSING_I32
                else:
                    state_didx = ar.get_meta_cell(local_index, META_STATE_MIDX)
                if not self._state_didx_is_valid(int(state_didx)):
                    continue

            attr_row = get_attr(local_index) if get_attr is not None else None

            if include_meta:
                meta_row = meta_row_for_check if meta_row_for_check is not None else get_meta(local_index)  # type: ignore[misc]
            else:
                meta_row = None
 
            yield NodeRecord(
                gid=int(gid),
                citype=citype,
                citype_index=int(citype_index),
                local_index=int(local_index),
                uuid_hi=int(uuid_hi),
                uuid_lo=int(uuid_lo),
                attr_row=attr_row,
                meta_row=meta_row,
            )

    def traverse_all_citypes(
        self,
        *,
        include_attrs: bool = False,
        include_meta: bool = False,
        uuid_format: str = "hi_lo",
        valid_only: bool = False,
    ) -> Iterator[NodeRecord]:
        self._require_open()
        for citype in self._citypes:
            ci_path = self.nodes_dir / citype
            if not ci_path.is_dir():
                continue
            yield from self.iterate_citype(
                citype,
                include_attrs=include_attrs,
                include_meta=include_meta,
                uuid_format=uuid_format,
                valid_only=valid_only,
            )

    # ----------------- neighbor traversal -----------------

    def _get_rel_uuid_index(self, reltype: str) -> _RelUuidIndex:
        self._require_open()
        idx = self._rel_uuid_indexes.get(reltype)
        if idx is not None:
            return idx
        rel_dir = self.rels_dir / reltype
        if not rel_dir.is_dir():
            raise FileNotFoundError(f"Unknown reltype dir: {rel_dir}")
        idx = _RelUuidIndex(rel_dir)
        idx.open()
        self._rel_uuid_indexes[reltype] = idx
        return idx


    def resolve_relation_uuid(self, reltype: str, uuid_str: str) -> int | None:
        idx = self._get_rel_uuid_index(reltype)
        return idx.find_relid(uuid_str)

    def get_relation_uuid(self, reltype: str, relid: int, *, fmt: str = "str"):
        idx = self._get_rel_uuid_index(reltype)
        fmt = (fmt or "str").lower()
        if fmt == "str":
            return idx.uuid_str(relid)
        hi, lo = idx.uuid_hi_lo(relid)
        if fmt == "hi_lo":
            return (hi, lo)
        if fmt == "uuid":
            return Uuid128(hi, lo).to_uuid()
        raise ValueError('fmt must be "str", "uuid", or "hi_lo"')

    def get_relation_by_relid(
        self,
        reltype: str,
        relid: int,
        *,
        include_attrs: bool = True,
        include_meta: bool = True,
    ) -> RelationRecord:
        self._require_open()
        if type(relid) is not int:
            raise TypeError("relid must be int")

        # UUID always exists per-reltype in the new format
        ruidx = self._get_rel_uuid_index(reltype)
        uuid_hi, uuid_lo = ruidx.uuid_hi_lo(relid)

        ar = self._get_rel_attr_reader(reltype)
        if ar is None:
            return RelationRecord(reltype=reltype, relid=relid, uuid_hi=uuid_hi, uuid_lo=uuid_lo)

        if relid < 0 or relid >= ar.row_count:
            raise IndexError(f"relid out of range for {reltype}: {relid} (row_count={ar.row_count})")

        attr_row = ar.get_attr_row(relid) if include_attrs else None
        meta_row = ar.get_meta_row(relid) if include_meta else None
        return RelationRecord(reltype=reltype, relid=relid, uuid_hi=uuid_hi, uuid_lo=uuid_lo, attr_row=attr_row, meta_row=meta_row)

    def iterate_neighbors(
        self,
        node_or_gid,
        *,
        reltype: Optional[Union[str, Iterable[str]]] = None,
        role: str = "either",
        unique: bool = False,
        include_relid: bool = False,
        include_reltype: bool = False,
        include_rel_uuid: bool = False,
        rel_uuid_format: str = "str",   # "str" | "uuid" | "hi_lo"
        # a bit more expensive
        as_nodes: bool = False,
        include_attrs: bool = False,
        include_meta: bool = False,
        valid_only: bool = False,
    ):
        self._require_open()

        rel_uuid_format = (rel_uuid_format or "str").lower()
        if rel_uuid_format not in ("str", "uuid", "hi_lo"):
            raise ValueError('rel_uuid_format must be "str", "uuid", or "hi_lo"')

        # Resolve the "from" node
        if isinstance(node_or_gid, NodeRecord):
            gid = node_or_gid.gid
            citype = node_or_gid.citype
            local_index = node_or_gid.local_index
        else:
            gid = node_or_gid
            if type(gid) is not int:
                raise TypeError("node_or_gid must be int or NodeRecord")
            if gid < 0 or gid >= self.gr.node_count:
                raise IndexError(f"gid out of range: {gid}")
            citype, local_index = self.gr.resolve_gid_full(gid)

        if valid_only and not self._node_valid_citype_local(citype, local_index):
            return

        role = role.lower()
        if role not in ("source", "target", "either"):
            raise ValueError('role must be "source", "target", or "either"')

        # Determine reltypes
        if reltype is None:
            reltypes = self._reltypes_for_citype(citype, role)
        elif isinstance(reltype, str):
            reltypes = [reltype]
        else:
            reltypes = sorted(set(x for x in reltype if isinstance(x, str)))

        seen = set() if unique else None
        citype_to_index = self._ensure_citype_index_map()

        def make_node_record(nb_gid: int, nb_citype: str, nb_local: int, lr_nb: LocalResolver) -> NodeRecord:
            uu_mm = lr_nb._uu_mm
            if uu_mm is None:
                raise RuntimeError("LocalResolver is closed")

            uuid_hi, uuid_lo = UUID_U128_BE.unpack_from(uu_mm, nb_local * UUID_BYTES)

            ci = citype_to_index.get(nb_citype, -1)
            if ci < 0:
                raise KeyError(f"Unknown citype (not in global citypes.json): {nb_citype}")

            attr_row = None
            meta_row = None
            if include_attrs or include_meta:
                ar_nb = self._get_attr_reader(nb_citype)
                if include_attrs:
                    attr_row = ar_nb.get_attr_row(nb_local)
                if include_meta:
                    meta_row = ar_nb.get_meta_row(nb_local)

            return NodeRecord(
                gid=int(nb_gid),
                citype=nb_citype,
                citype_index=int(ci),
                local_index=int(nb_local),
                uuid_hi=int(uuid_hi),
                uuid_lo=int(uuid_lo),
                attr_row=attr_row,
                meta_row=meta_row,
            )

        def rel_uuid_from_idx(ruid: _RelUuidIndex, relid: int):
            if rel_uuid_format == "str":
                return ruid.uuid_str(relid)
            hi, lo = ruid.uuid_hi_lo(relid)
            if rel_uuid_format == "hi_lo":
                return (hi, lo)
            return Uuid128(hi, lo).to_uuid()

        def emit(rt: str, nb, relid: int, rel_uuid_val):
            items = []
            if include_reltype:
                items.append(rt)
            items.append(nb)
            if include_relid:
                items.append(relid)
            if include_rel_uuid:
                items.append(rel_uuid_val)
            return items[0] if len(items) == 1 else tuple(items)

        for rt in reltypes:
            parts = self._get_reltype_partitions(rt)
            if not parts:
                continue

            ruidx = self._get_rel_uuid_index(rt) if include_rel_uuid else None

            for src, tgt, path in parts:
                # gid as source -> neighbors are in tgt
                if role in ("source", "either") and src == citype:
                    rr = self._get_relation_reader(path)

                    lr_tgt = self._get_local_resolver(tgt)
                    gid_mm = lr_tgt._gid_mm
                    if gid_mm is None:
                        raise RuntimeError("LocalResolver is closed")

                    u32 = U32_LE.unpack_from
                    step = U32_LE.size
                    tgt_count = lr_tgt.local_count

                    for nb_local, relid in rr.iter_targets(local_index):
                        relid_i = int(relid)

                        if valid_only:
                            if not self.is_relation_valid(rt, relid_i):
                                continue
                            if not self._node_valid_citype_local(tgt, int(nb_local)):
                                continue

                        if nb_local < 0 or nb_local >= tgt_count:
                            raise IndexError(
                                f"edge points to invalid tgt_local={nb_local} for citype={tgt} (local_count={tgt_count})"
                            )
                        (nb_gid_u32,) = u32(gid_mm, nb_local * step)
                        nb_gid = int(nb_gid_u32)

                        if unique:
                            assert seen is not None
                            if nb_gid in seen:
                                continue
                            seen.add(nb_gid)

                        nb_out = make_node_record(nb_gid, tgt, int(nb_local), lr_tgt) if as_nodes else nb_gid
                        rel_uuid_val = rel_uuid_from_idx(ruidx, relid_i) if include_rel_uuid else None  # type: ignore[arg-type]
                        yield emit(rt, nb_out, relid_i, rel_uuid_val)

                # gid as target -> neighbors are in src
                if role in ("target", "either") and tgt == citype:
                    rr = self._get_relation_reader(path)

                    lr_src = self._get_local_resolver(src)
                    gid_mm = lr_src._gid_mm
                    if gid_mm is None:
                        raise RuntimeError("LocalResolver is closed")

                    u32 = U32_LE.unpack_from
                    step = U32_LE.size
                    src_count = lr_src.local_count

                    for nb_local, relid in rr.iter_sources(local_index):
                        relid_i = int(relid)

                        if valid_only:
                            if not self.is_relation_valid(rt, relid_i):
                                continue
                            if not self._node_valid_citype_local(src, int(nb_local)):
                                continue

                        if nb_local < 0 or nb_local >= src_count:
                            raise IndexError(
                                f"edge points to invalid src_local={nb_local} for citype={src} (local_count={src_count})"
                            )
                        (nb_gid_u32,) = u32(gid_mm, nb_local * step)
                        nb_gid = int(nb_gid_u32)

                        if unique:
                            assert seen is not None
                            if nb_gid in seen:
                                continue
                            seen.add(nb_gid)

                        nb_out = make_node_record(nb_gid, src, int(nb_local), lr_src) if as_nodes else nb_gid
                        rel_uuid_val = rel_uuid_from_idx(ruidx, relid_i) if include_rel_uuid else None  # type: ignore[arg-type]
                        yield emit(rt, nb_out, relid_i, rel_uuid_val)


    def relation_exists(
        self,
        node1,
        node2,
        *,
        reltype: Optional[Union[str, Iterable[str]]] = None,
        role: Optional[str] = None,   # None => "either"
    ):
        """
        Checks whether there is at least one relation between node1 and node2.

        Conventions:
          - role refers to node1's role (same as iterate_neighbors):
              "source" => node1 -> node2
              "target" => node2 -> node1
              "either" => either direction

        Return shape (as requested):
          1) reltype is str AND role is not None:
                -> relid (int) or None

          2) reltype is str AND role is None:
                -> list of roles found relative to node1, e.g. ["source"], ["target"], ["source","target"], or None

          3) reltype is None AND role is not None:
                -> list[str] of reltypes for which the relation exists in that role

          4) reltype is None AND role is None:
                -> dict[reltype] = list of roles found (relative to node1)
        """
        self._require_open()

        def _resolve(x):
            if isinstance(x, NodeRecord):
                return x.gid, x.citype, x.local_index
            gid = x
            if type(gid) is not int:
                raise TypeError("node must be int gid or NodeRecord")
            if gid < 0 or gid >= self.gr.node_count:
                raise IndexError(f"gid out of range: {gid}")
            c, li = self.gr.resolve_gid_full(gid)
            return gid, c, li

        gid1, c1, l1 = _resolve(node1)
        gid2, c2, l2 = _resolve(node2)

        role_eff = (role or "either").lower()
        if role_eff not in ("source", "target", "either"):
            raise ValueError('role must be "source", "target", "either", or None')

        # candidate reltypes
        if reltype is None:
            reltypes = self._reltypes_for_citype(c1, role_eff)
        elif isinstance(reltype, str):
            reltypes = [reltype]
        else:
            reltypes = sorted(set(x for x in reltype if isinstance(x, str)))

        single_reltype_mode = isinstance(reltype, str)

        def _scan_one_reltype(rt: str) -> tuple[set[str], dict[str, int]]:
            """
            Returns:
              roles_found: {"source","target"} relative to node1
              relid_by_role: {"source": relid, "target": relid} (first match per direction)
            """
            roles_found: set[str] = set()
            relid_by_role: dict[str, int] = {}

            parts = self._get_reltype_partitions(rt)
            if not parts:
                return roles_found, relid_by_role

            want_src = (role_eff in ("source", "either"))
            want_tgt = (role_eff in ("target", "either"))

            for src, tgt, path in parts:
                # node1 as source: src=c1, tgt=c2
                if want_src and src == c1 and tgt == c2 and "source" not in roles_found:
                    rr = self._get_relation_reader(path)
                    for nb_local, relid in rr.iter_targets(l1):
                        if nb_local == l2:
                            roles_found.add("source")
                            relid_by_role["source"] = int(relid)
                            break

                # node1 as target: src=c2, tgt=c1
                if want_tgt and tgt == c1 and src == c2 and "target" not in roles_found:
                    rr = self._get_relation_reader(path)
                    for nb_local, relid in rr.iter_sources(l1):
                        if nb_local == l2:
                            roles_found.add("target")
                            relid_by_role["target"] = int(relid)
                            break

                # early out if we found everything we care about
                if role_eff == "source" and "source" in roles_found:
                    break
                if role_eff == "target" and "target" in roles_found:
                    break
                if role_eff == "either" and ("source" in roles_found and "target" in roles_found):
                    break

            return roles_found, relid_by_role

        # -------- return-shape logic --------

        if single_reltype_mode:
            rt = reltypes[0] if reltypes else ""
            roles_found, relid_by_role = _scan_one_reltype(rt)

            if role is not None:
                # reltype specified + role specified -> relid or None
                key = role_eff
                if key == "either":
                    # pick any one deterministically if either direction exists
                    if "source" in relid_by_role:
                        return relid_by_role["source"]
                    if "target" in relid_by_role:
                        return relid_by_role["target"]
                    return None
                return relid_by_role.get(key)

            # reltype specified + role not specified -> roles or None
            return sorted(roles_found) if roles_found else None

        # reltype not specified
        if role is not None:
            # role specified -> list of reltypes that match that role
            out: list[str] = []
            for rt in reltypes:
                roles_found, _ = _scan_one_reltype(rt)
                if role_eff == "either":
                    if roles_found:
                        out.append(rt)
                else:
                    if role_eff in roles_found:
                        out.append(rt)
            return out

        # neither specified -> dict reltype -> roles
        out_map: dict[str, list[str]] = {}
        for rt in reltypes:
            roles_found, _ = _scan_one_reltype(rt)
            if roles_found:
                out_map[rt] = sorted(roles_found)
        return out_map


    def iterate_relations(
        self,
        *,
        reltype: Optional[Union[str, Iterable[str]]] = None,
        by: str = "src",  # "src" | "tgt"
        src_citype: Optional[str] = None,
        tgt_citype: Optional[str] = None,
        include_relid: bool = False,
        include_reltype: bool = False,
        include_rel_uuid: bool = False,
        rel_uuid_format: str = "str",   # "str" | "uuid" | "hi_lo"
        as_nodes: bool = False,
        include_attrs: bool = False,
        include_meta: bool = False,
        valid_only: bool = False,
    ) -> Iterator[Any]:
        self._require_open()

        by = (by or "src").lower()
        if by not in ("src", "tgt"):
            raise ValueError('by must be "src" or "tgt"')

        rel_uuid_format = (rel_uuid_format or "str").lower()
        if rel_uuid_format not in ("str", "uuid", "hi_lo"):
            raise ValueError('rel_uuid_format must be "str", "uuid", or "hi_lo"')

        # --- choose reltypes ---
        if reltype is None:
            cand: Optional[set[str]] = None

            if src_citype is not None:
                rs = set(self._reltypes_for_citype(src_citype, "source"))
                cand = rs if cand is None else (cand & rs)

            if tgt_citype is not None:
                rt = set(self._reltypes_for_citype(tgt_citype, "target"))
                cand = rt if cand is None else (cand & rt)

            if cand is None:
                # no filters -> all reltypes
                reltypes = [p.name for p in self.rels_dir.iterdir() if p.is_dir()]
            else:
                reltypes = sorted(cand)
        elif isinstance(reltype, str):
            reltypes = [reltype]
        else:
            reltypes = sorted({x for x in reltype if isinstance(x, str)})

        # --- emit shape ---
        def emit(rt: str, src_out, tgt_out, relid: int, rel_uuid_val):
            items = []
            if include_reltype:
                items.append(rt)
            items.append(src_out)
            items.append(tgt_out)
            if include_relid:
                items.append(relid)
            if include_rel_uuid:
                items.append(rel_uuid_val)
            return tuple(items)

        citype_to_index = self._ensure_citype_index_map()

        def make_node(nb_gid: int, nb_citype: str, nb_local: int, lr_nb):
            uu_mm = lr_nb._uu_mm
            if uu_mm is None:
                raise RuntimeError("LocalResolver is closed")
            uuid_hi, uuid_lo = UUID_U128_BE.unpack_from(uu_mm, nb_local * UUID_BYTES)

            ci = citype_to_index.get(nb_citype, -1)
            if ci < 0:
                raise KeyError(f"Unknown citype: {nb_citype}")

            attr_row = meta_row = None
            if include_attrs or include_meta:
                ar = self._get_attr_reader(nb_citype)
                if include_attrs:
                    attr_row = ar.get_attr_row(nb_local)
                if include_meta:
                    meta_row = ar.get_meta_row(nb_local)

            return NodeRecord(
                gid=int(nb_gid),
                citype=nb_citype,
                citype_index=int(ci),
                local_index=int(nb_local),
                uuid_hi=int(uuid_hi),
                uuid_lo=int(uuid_lo),
                attr_row=attr_row,
                meta_row=meta_row,
            )

        def rel_uuid_from_idx(ruidx: _RelUuidIndex, relid: int):
            if rel_uuid_format == "str":
                return ruidx.uuid_str(relid)
            hi, lo = ruidx.uuid_hi_lo(relid)
            if rel_uuid_format == "hi_lo":
                return (hi, lo)
            return Uuid128(hi, lo).to_uuid()

        # --- stream partitions ---
        for rt in reltypes:
            parts = self._get_reltype_partitions(rt)
            if not parts:
                continue

            # Open once per reltype if needed
            ruidx = self._get_rel_uuid_index(rt) if include_rel_uuid else None

            for src, tgt, path in parts:
                if src_citype is not None and src != src_citype:
                    continue
                if tgt_citype is not None and tgt != tgt_citype:
                    continue

                rr = self._get_relation_reader(path)
                lr_src = self._get_local_resolver(src)
                lr_tgt = self._get_local_resolver(tgt)

                src_gid_mm = lr_src._gid_mm
                tgt_gid_mm = lr_tgt._gid_mm
                if src_gid_mm is None or tgt_gid_mm is None:
                    raise RuntimeError("LocalResolver is closed")

                u32 = U32_LE.unpack_from
                step = U32_LE.size

                for src_local, tgt_local, relid in rr.iter_edges(by=by):
                    src_local_i = int(src_local)
                    tgt_local_i = int(tgt_local)
                    relid_i = int(relid)

                    if valid_only:
                        if not self._node_valid_citype_local(src, src_local_i):
                            continue
                        if not self._node_valid_citype_local(tgt, tgt_local_i):
                            continue
                        if not self.is_relation_valid(rt, relid_i):
                            continue

                    (src_gid,) = u32(src_gid_mm, src_local_i * step)
                    (tgt_gid,) = u32(tgt_gid_mm, tgt_local_i * step)

                    if as_nodes:
                        src_out = make_node(int(src_gid), src, src_local_i, lr_src)
                        tgt_out = make_node(int(tgt_gid), tgt, tgt_local_i, lr_tgt)
                    else:
                        src_out = int(src_gid)
                        tgt_out = int(tgt_gid)

                    rel_uuid_val = rel_uuid_from_idx(ruidx, relid_i) if include_rel_uuid else None  # type: ignore[arg-type]
                    yield emit(rt, src_out, tgt_out, relid_i, rel_uuid_val)

    def relation_url_by_relid(self, reltype: str, relid: int) -> str | None:
        """
        base_url/relation/<CITYPE_SRC>/<UUID_SRC>/<UUID_RELATION>
        Returns None if endpoints cannot be found.
        """
        self._require_open()

        endpoints = self._find_relation_endpoints(reltype, int(relid))
        if endpoints is None:
            return None
        src_citype, src_local, _, _ = endpoints

        # src uuid from local resolver (fast)
        lr = self._get_local_resolver(src_citype)
        uu_mm = lr._uu_mm
        if uu_mm is None:
            raise RuntimeError("LocalResolver is closed")
        src_hi, src_lo = UUID_U128_BE.unpack_from(uu_mm, src_local * UUID_BYTES)
        src_uuid_s = str(Uuid128(int(src_hi), int(src_lo)).to_uuid())

        rel_uuid_s = self.get_relation_uuid(reltype, int(relid), fmt="str")
        return self._ui_url(f"relation/{src_citype}/{src_uuid_s}/{rel_uuid_s}")

    def relation_url(self, rel_uuid_str: str) -> str | None:
        """
        Build the MetaIS UI URL from a relation UUID alone.
        Uses global resolver if available; falls back to scanning reltypes.
        """
        self._require_open()
        r = self.resolve_relation_uuid_any(rel_uuid_str)
        if r is None:
            return None
        reltype, relid = r

        endpoints = self._find_relation_endpoints(reltype, int(relid))
        if endpoints is None:
            return None
        src_citype, src_local, _, _ = endpoints

        lr = self._get_local_resolver(src_citype)
        uu_mm = lr._uu_mm
        if uu_mm is None:
            raise RuntimeError("LocalResolver is closed")
        src_hi, src_lo = UUID_U128_BE.unpack_from(uu_mm, src_local * UUID_BYTES)
        src_uuid_s = str(Uuid128(int(src_hi), int(src_lo)).to_uuid())

        return self._ui_url(f"relation/{src_citype}/{src_uuid_s}/{rel_uuid_str}")