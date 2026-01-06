from __future__ import annotations

import json
import mmap
import uuid as _uuid
from dataclasses import dataclass
from pathlib import Path
from os import PathLike
from typing import Dict, Iterator, Iterable, Optional, Union, Tuple, List
from collections import OrderedDict

from .dict_reader import DictReader
from .attribute_reader import AttributeReader
from .relation_reader import RelationReader
from .resolver import GlobalResolver, LocalResolver
from .bin_formats import Uuid128, UUID_U128_BE, UUID_BYTES, U32_LE, MISSING_I32


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
      - iterate_citype(citype, include_attrs=False/True, include_meta=False/True, uuid_format=...)
      - traverse_all_citypes(...)
      - iterate_neighbors(gid, reltype=None, role="either", unique=False) -> yields neighbor gids (or triples)
    """

    def __init__(
        self,
        packed_root: Pathish,
        *,
        dict_cache_size: int | None = 16_384,
        attr_cache_size: int | None = 16_384,
        resolver_cache_size: int | None = 65_536,
        # RelationReader mmaps files; keeping too many open can hit FD limits
        # None for no eviction (infinite)
        open_relation_partitions_max: int | None = 32,
    ):
        self.root = Path(packed_root)
        self.dict_dir = self.root / "dict"
        self.uuids_dir = self.root / "uuids"
        self.nodes_dir = self.root / "nodes"
        self.rels_dir = self.root / "relations"

        self._closed = False

        # Global singletons
        self._dict_cache_size = (None if dict_cache_size is None else int(dict_cache_size))
        self._global_resolver_cached_size = (None if resolver_cache_size is None else int(resolver_cache_size))

        self.dict = DictReader(self.dict_dir, cache_size=self._dict_cache_size)
        self.gr = GlobalResolver(self.uuids_dir, cache_size=self._global_resolver_cached_size)
        self.max_gid = max(0, self.gr.node_count - 1)

        # Per-citype caches
        self._attr_readers: Dict[str, AttributeReader] = {}
        self._local_resolvers: Dict[str, LocalResolver] = {}
        self._local_resolver_cache_size = None if resolver_cache_size is None else int(resolver_cache_size)
        self._attr_cache_size = None if attr_cache_size    is None else int(attr_cache_size)

        # Relation caches:
        # - partitions list per reltype: [(src, tgt, path), ...]
        self._rel_partitions: Dict[str, List[Tuple[str, str, Path]]] = {}

        # - open RelationReader LRU keyed by partition path string
        self._rel_readers_lru: "OrderedDict[str, RelationReader]" = OrderedDict()
        self._rel_open_max = None
        if open_relation_partitions_max is not None:
            self._rel_open_max = int(open_relation_partitions_max)
            if self._rel_open_max <= 0:
                raise ValueError("open_relation_partitions_max must be > 0 (or None for infinite)")


        # Optional accelerator index (relations.json)
        self._rel_index = None  # lazy loaded dict with keys bySource/byTarget

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

        # Close relation readers first (many)
        for _, rr in list(self._rel_readers_lru.items()):
            try:
                rr.close()
            except Exception as e:
                errs.append(e)
        self._rel_readers_lru.clear()

        # Close per-citype readers
        for ar in list(self._attr_readers.values()):
            try:
                ar.close()
            except Exception as e:
                errs.append(e)
        self._attr_readers.clear()

        for lr in list(self._local_resolvers.values()):
            try:
                lr.close()
            except Exception as e:
                errs.append(e)
        self._local_resolvers.clear()

        # Close globals
        try:
            self.gr.close()
        except Exception as e:
            errs.append(e)

        try:
            self.dict.close()
        except Exception as e:
            errs.append(e)

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

    # ----------------- per-citype openers -----------------

    def _get_attr_reader(self, citype: str) -> AttributeReader:
        self._require_open()
        ar = self._attr_readers.get(citype)
        if ar is not None:
            return ar
        path = self.nodes_dir / citype
        ar = AttributeReader(path, cache_size=self._attr_cache_size)
        self._attr_readers[citype] = ar
        return ar

    def _get_local_resolver(self, citype: str) -> LocalResolver:
        self._require_open()
        lr = self._local_resolvers.get(citype)
        if lr is not None:
            return lr
        path = self.nodes_dir / citype
        lr = LocalResolver(path, cache_size=self._local_resolver_cache_size)  # same rationale as AttributeReader above
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

    # ----------------- relation index + partitions -----------------

    def _ensure_rel_index(self) -> None:
        """
        Load relations/relations.json if present.
        Format (per your spec):
          {
            "bySource": { "KS": ["REL_A", ...], ... },
            "byTarget": { "Dokument": ["REL_A", ...], ... }
          }
        """
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

        # If an index exists, and citype isn't in it, assume truly none.
        if has_index:
            return sorted(set(out))

        # Only if there's NO index at all, do the expensive fallback:
        if self.rels_dir.is_dir():
            for p in self.rels_dir.iterdir():
                if p.is_dir():
                    out.append(p.name)

        return sorted(set(out))

    def _get_reltype_partitions(self, reltype: str) -> List[Tuple[str, str, Path]]:
        """
        Return list of (src_citype, tgt_citype, partition_path).
        Cached per reltype.
        """
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
        """
        Open RelationReader for a single <SRC>__<TGT> partition folder.
        Kept in an LRU mainly to avoid too many open file handles.
        """
        self._require_open()

        key = str(partition_path)
        rr = self._rel_readers_lru.get(key)
        if rr is not None:
            # bump LRU
            self._rel_readers_lru.move_to_end(key)
            return rr

        rr = RelationReader(partition_path)
        self._rel_readers_lru[key] = rr
        self._rel_readers_lru.move_to_end(key)

        # enforce open limit
        if self._rel_open_max is not None:
            while len(self._rel_readers_lru) > self._rel_open_max:
                old_key, old_rr = self._rel_readers_lru.popitem(last=False)
                try:
                    old_rr.close()
                except Exception:
                    pass
        return rr

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
        """
        Resolve uuid -> gid -> citype/local -> attributes/meta.
        Returns None if uuid not found.
        """
        self._require_open()
        r = self.gr.resolve_uuid(uuid_str)
        if r is None:
            return None
        gid, citype_index, local_index = r
        citype = self.gr.citypes[citype_index]
        return self.get_node_by_gid(
            gid,
            include_attrs=include_attrs,
            include_meta=include_meta,
        )

    def get_node_by_gid(
        self,
        gid: int,
        *,
        include_attrs: bool = True,
        include_meta: bool = True,
    ) -> NodeRecord:
        """
        gid -> (citype, local) -> uuid + attributes/meta
        """
        self._require_open()
        if type(gid) is not int:
            raise TypeError("gid must be int")
        if gid < 0 or gid >= self.gr.node_count:
            raise IndexError(f"gid out of range: {gid}")

        citype_index, local_index = self.gr.resolve_gid(gid)
        citype = self.gr.citypes[citype_index]

        # uuid hi/lo from global uuids.bin (fast + cached inside GlobalResolver)
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
            uuid_hi=uuid_hi,
            uuid_lo=uuid_lo,
            attr_row=attr_row,
            meta_row=meta_row,
        )

    def get_attribute_dict_index(self, node_or_gid, technical_name: str) -> int | None:
        self._require_open()
        if not isinstance(technical_name, str):
            raise TypeError("technical_name must be str")

        citype, local = self._resolve_citype_local(node_or_gid)
        ar = self._get_attr_reader(citype)

        aidx = ar.attr_index(technical_name)
        if aidx is None:
            return None

        # Use preloaded row if present
        if isinstance(node_or_gid, NodeRecord) and node_or_gid.attr_row is not None:
            didx = node_or_gid.attr_row[aidx]
            return None if didx == MISSING_I32 else didx

        row = ar.get_attr_row(local)  # cached
        didx = row[aidx]
        return None if didx == MISSING_I32 else didx

    def get_attr_value(self, node_or_gid, technical_name: str, default=None):
        didx = self.get_attribute_dict_index(node_or_gid, technical_name)
        if didx is None:
            return default
        return self.dict.get(didx)

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
            "index": aidx,
        }

    # ----------------- citype traversal -----------------

    def iterate_citype(
        self,
        citype: str,
        *,
        include_attrs: bool = False,
        include_meta: bool = False,
        uuid_format: str = "hi_lo",  # "hi_lo" | "str" | "uuid"
        scan: bool = False
    ) -> Iterator[NodeRecord]:
        """
        Sequentially scan one citype without polluting row LRUs.

        uuid_format:
          - "hi_lo": fastest (no uuid object creation)
          - "uuid":  stores uuid_hi/uuid_lo but also easy to call rec.uuid_obj()
          - "str":   user can call rec.uuid_str(); we do NOT precompute string to keep this fast
        """
        self._require_open()
        uuid_format = uuid_format.lower()
        if uuid_format not in ("hi_lo", "uuid", "str"):
            raise ValueError('uuid_format must be "hi_lo", "uuid", or "str"')

        lr = self._get_local_resolver(citype)
        ar = self._get_attr_reader(citype)

        # Bypass AttributeReader LRUs on scans:
        if include_attrs:
            if scan:
                get_attr = ar._get_attr_row_sparse if ar._layout == "sparse" else ar._get_attr_row_grid
            else:
                get_attr = ar.get_attr_row   # cached dense row
        else:
            get_attr = None

        if include_meta:
            if scan:
                get_meta = ar._get_meta_row_grid
            else:
                get_meta = ar.get_meta_row   # cached dense row
        else:
            get_meta = None

        # Direct mmap reads for uuid + gid (avoid LocalResolver caching)
        uu_mm = lr._uu_mm
        gid_mm = lr._gid_mm
        if uu_mm is None or gid_mm is None:
            raise RuntimeError("LocalResolver is closed")

        for local_index in range(lr.local_count):
            uuid_hi, uuid_lo = UUID_U128_BE.unpack_from(uu_mm, local_index * UUID_BYTES)
            (gid,) = U32_LE.unpack_from(gid_mm, local_index * U32_LE.size)

            attr_row = get_attr(local_index) if get_attr is not None else None
            meta_row = get_meta(local_index) if get_meta is not None else None

            # citype_index is available via GlobalResolver, but we can avoid a gid->resolver lookup
            # by mapping name->index once. Use .index() would be slow, so cache:
            # (this list is small; building dict once is fine)
            # We'll lazily create it.
            if not hasattr(self, "_citype_to_index"):
                self._citype_to_index = {name: i for i, name in enumerate(self.gr.citypes)}
            citype_index = self._citype_to_index.get(citype, -1)
            if citype_index < 0:
                raise KeyError(f"Unknown citype (not in global citypes.json): {citype}")

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
    ) -> Iterator[NodeRecord]:
        self._require_open()
        for citype in self.gr.citypes:
            ci_path = self.nodes_dir / citype
            if not ci_path.is_dir():
                continue
            yield from self.iterate_citype(
                citype,
                include_attrs=include_attrs,
                include_meta=include_meta,
                uuid_format=uuid_format,
            )

    # ----------------- neighbor traversal -----------------
    def iterate_neighbors(
        self,
        gid: int,
        *,
        reltype: Optional[Union[str, Iterable[str]]] = None,
        role: str = "either",
        unique: bool = False,
        include_relid: bool = False,
        include_reltype: bool = False,
    ):
        self._require_open()
        if type(gid) is not int:
            raise TypeError("gid must be int")
        if gid < 0 or gid >= self.gr.node_count:
            raise IndexError(f"gid out of range: {gid}")

        role = role.lower()
        if role not in ("source", "target", "either"):
            raise ValueError('role must be "source", "target", or "either"')

        citype, local_index = self.gr.resolve_gid_full(gid)

        if reltype is None:
            reltypes = self._reltypes_for_citype(citype, role)
        elif isinstance(reltype, str):
            reltypes = [reltype]
        else:
            reltypes = sorted(set(x for x in reltype if isinstance(x, str)))

        seen = set() if unique else None

        for rt in reltypes:
            parts = self._get_reltype_partitions(rt)
            if not parts:
                continue

            for src, tgt, path in parts:
                rr = None

                # gid as source (we query src_local -> tgt_local)
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
                        if nb_local < 0 or nb_local >= tgt_count:
                            raise IndexError(f"edge points to invalid tgt_local={nb_local} for citype={tgt} (local_count={tgt_count})")
                        (nb_gid,) = u32(gid_mm, nb_local * step)
                        nb_gid = int(nb_gid)

                        if unique:
                            if nb_gid in seen:
                                continue
                            seen.add(nb_gid)

                        if include_reltype:
                            yield (rt, nb_gid, relid)
                        elif include_relid:
                            yield (nb_gid, relid)
                        else:
                            yield nb_gid

                # gid as target (we query tgt_local -> src_local)
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
                        if nb_local < 0 or nb_local >= src_count:
                            raise IndexError(f"edge points to invalid src_local={nb_local} for citype={src} (local_count={src_count})")
                        (nb_gid,) = u32(gid_mm, nb_local * step)
                        nb_gid = int(nb_gid)

                        if unique:
                            if nb_gid in seen:
                                continue
                            seen.add(nb_gid)

                        if include_reltype:
                            yield (rt, nb_gid, relid)
                        elif include_relid:
                            yield (nb_gid, relid)
                        else:
                            yield nb_gid