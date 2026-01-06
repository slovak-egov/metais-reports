from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from metais.common.json_utils import load_json_file
from metais.common.atomic_write import atomic_write_json, atomic_write_with
from metais.common.step_marker import is_done, mark_done
from metais.common.binary_io import (
    Uuid128,
    RESOLVER_ROW,
    write_u32le_file,
    write_u64le_file,
    write_uuid16_file,
)

from .ndjson_stream import ndjson_json_range


##########
# Models #
##########

@dataclass
class PrepassStats:
    total_records: int = 0
    missing_type: int = 0

    missing_attributes: int = 0
    bad_attributes_type: int = 0

    missing_uuid: int = 0
    bad_uuid: int = 0


@dataclass
class AttributeCatalog:
    # citype/reltype -> count
    object_count_by_type: dict[str, int] = field(default_factory=dict)
    # citype/reltype -> set(technicalName)
    seen_attrs_by_type: dict[str, set[str]] = field(default_factory=dict)

    def note_object(self, typ: str) -> None:
        self.object_count_by_type[typ] = self.object_count_by_type.get(typ, 0) + 1

    def note_attr(self, typ: str, name: str) -> None:
        s = self.seen_attrs_by_type.get(typ)
        if s is None:
            s = set()
            self.seen_attrs_by_type[typ] = s
        s.add(name)


def _utf8_sort_key(s: str) -> bytes:
    return s.encode("utf-8")


def _canonicalize_obj(v: Any) -> Any:
    if isinstance(v, dict):
        return {k: _canonicalize_obj(v[k]) for k in sorted(v.keys(), key=_utf8_sort_key)}
    if isinstance(v, list):
        return [_canonicalize_obj(x) for x in v]
    if isinstance(v, tuple):
        return [_canonicalize_obj(x) for x in v]
    return v


def canonical_value(v: Any) -> str:
    vv = _canonicalize_obj(v)
    return json.dumps(vv, ensure_ascii=False, separators=(",", ":"), sort_keys=False)


@dataclass
class ValueDictionary:
    _set: set[str] = field(default_factory=set)
    values: list[str] = field(default_factory=list)

    def note(self, json_literal: str) -> None:
        self._set.add(json_literal)

    def finalize_sorted(self) -> None:
        self.values = sorted(self._set, key=_utf8_sort_key)


@dataclass
class PrepassResult:
    nodes: PrepassStats = field(default_factory=PrepassStats)
    rels: PrepassStats = field(default_factory=PrepassStats)

    attrs_ent: AttributeCatalog = field(default_factory=AttributeCatalog)
    attrs_rel: AttributeCatalog = field(default_factory=AttributeCatalog)

    metaAttrs_ent: AttributeCatalog = field(default_factory=AttributeCatalog)
    metaAttrs_rel: AttributeCatalog = field(default_factory=AttributeCatalog)

    value_dict: ValueDictionary = field(default_factory=ValueDictionary)

    uuids_ent: list[Uuid128] = field(default_factory=list)
    uuids_by_citype: dict[str, list[Uuid128]] = field(default_factory=dict)


_LAST_PREPASS: Optional[PrepassResult] = None


##################
# Layout helpers #
##################

def _pages_dir(layout: Any, kind: str) -> Path:
    if kind == "nodes":
        for a in ("raw_nodes_pages_dir", "nodes_pages_dir"):
            if hasattr(layout, a):
                return Path(getattr(layout, a))
        if hasattr(layout, "raw_nodes_dir"):
            return Path(getattr(layout, "raw_nodes_dir")) / "pages"
        return Path(layout.date_root) / "nodes" / "pages"
    else:
        for a in ("raw_rels_pages_dir", "rels_pages_dir", "raw_relations_pages_dir"):
            if hasattr(layout, a):
                return Path(getattr(layout, a))
        if hasattr(layout, "raw_rels_dir"):
            return Path(getattr(layout, "raw_rels_dir")) / "pages"
        if hasattr(layout, "raw_relations_dir"):
            return Path(getattr(layout, "raw_relations_dir")) / "pages"
        return Path(layout.date_root) / "relations" / "pages"


def _meta_dir(layout: Any, kind: str) -> Path:
    if kind == "nodes":
        for a in ("nodes_meta_dir", "meta_nodes_dir", "metadata_nodes_dir"):
            if hasattr(layout, a):
                return Path(getattr(layout, a))
        return Path(layout.date_root) / "metadata" / "nodes"
    else:
        for a in ("rels_meta_dir", "relations_meta_dir", "meta_rels_dir", "metadata_relations_dir"):
            if hasattr(layout, a):
                return Path(getattr(layout, a))
        return Path(layout.date_root) / "metadata" / "relations"


def _citypes_list_path(layout: Any) -> Path:
    for a in ("citypes_list_json", "metadata_citypes_list_json"):
        if hasattr(layout, a):
            return Path(getattr(layout, a))
    return Path(layout.date_root) / "metadata" / "citypes_list.json"


###############################
# Pass 1: Prepass / discovery #
###############################

def run_prepass(layout: Any, *, force: bool = False, verbose: bool = False, skip_bad_json: bool = False) -> None:
    global _LAST_PREPASS

    if (is_done(layout.packed_root, ".pass1_5.done") and pass1_5_outputs_ok(layout)) and not force:
        if verbose:
            print("[pass1] pass1.5 already done; skipping prepass")
        _LAST_PREPASS = None
        return

    pre = PrepassResult()

    # ---- nodes ----
    nodes_pages = _pages_dir(layout, "nodes")
    if verbose:
        print(f"[pass1] nodes pages: {nodes_pages}")

    last_shard_i = -1
    for rec in ndjson_json_range(nodes_pages, "nodes", skip_bad_json=skip_bad_json):
        pre.nodes.total_records += 1
        if rec.shard_index != last_shard_i:
            last_shard_i = rec.shard_index
            if verbose:
                print(f"[pass1:nodes] shard {rec.shard_index+1}/{rec.shard_count} offset={rec.shard_offset}")

        j = rec.obj
        typ = j.get("type")
        if not isinstance(typ, str):
            pre.nodes.missing_type += 1
            continue

        citype = typ
        pre.attrs_ent.note_object(citype)

        u = j.get("uuid")
        if not isinstance(u, str):
            pre.nodes.missing_uuid += 1
        else:
            try:
                uu = Uuid128.from_string(u)
                pre.uuids_ent.append(uu)
                pre.uuids_by_citype.setdefault(citype, []).append(uu)
            except Exception:
                pre.nodes.bad_uuid += 1

        attrs = j.get("attributes")
        if isinstance(attrs, list):
            for a in attrs:
                if not isinstance(a, dict):
                    pre.nodes.bad_attributes_type += 1
                    continue
                name = a.get("name")
                if not isinstance(name, str):
                    continue
                pre.attrs_ent.note_attr(citype, name)
                if "value" in a:
                    pre.value_dict.note(canonical_value(a["value"]))
        else:
            pre.nodes.missing_attributes += 1

        meta = j.get("metaAttributes")
        if isinstance(meta, dict):
            for k, v in meta.items():
                if isinstance(k, str):
                    pre.metaAttrs_ent.note_attr(citype, k)
                    pre.value_dict.note(canonical_value(v))

    # ---- relations ----
    rels_pages = _pages_dir(layout, "rels")
    if verbose:
        print(f"[pass1] rels pages:  {rels_pages}")

    last_shard_i = -1
    for rec in ndjson_json_range(rels_pages, "rels", skip_bad_json=skip_bad_json):
        pre.rels.total_records += 1
        if rec.shard_index != last_shard_i:
            last_shard_i = rec.shard_index
            if verbose:
                print(f"[pass1:rels] shard {rec.shard_index+1}/{rec.shard_count} offset={rec.shard_offset}")

        j = rec.obj
        typ = j.get("type")
        if not isinstance(typ, str):
            pre.rels.missing_type += 1
            continue

        reltype = typ
        pre.attrs_rel.note_object(reltype)

        attrs = j.get("attributes")
        if isinstance(attrs, list):
            for a in attrs:
                if not isinstance(a, dict):
                    pre.rels.bad_attributes_type += 1
                    continue
                name = a.get("name")
                if not isinstance(name, str):
                    continue
                pre.attrs_rel.note_attr(reltype, name)
                if "value" in a:
                    pre.value_dict.note(canonical_value(a["value"]))
        else:
            pre.rels.missing_attributes += 1

        meta = j.get("metaAttributes")
        if isinstance(meta, dict):
            for k, v in meta.items():
                if isinstance(k, str):
                    pre.metaAttrs_rel.note_attr(reltype, k)
                    pre.value_dict.note(canonical_value(v))

    _LAST_PREPASS = pre

    if verbose:
        print("[pass1] done")
        print(f"  nodes: total={pre.nodes.total_records} missing_type={pre.nodes.missing_type} "
              f"missing_uuid={pre.nodes.missing_uuid} bad_uuid={pre.nodes.bad_uuid}")
        print(f"  rels:  total={pre.rels.total_records} missing_type={pre.rels.missing_type}")
        print(f"  citypes={len(pre.uuids_by_citype)} reltypes={len(pre.attrs_rel.object_count_by_type)} "
              f"dict_unique={len(pre.value_dict._set)}")


#######################################
# Pass 1.5: Freeze schema + resolvers #
#######################################

def pass1_5_outputs_ok(layout: Any) -> bool:
    return (
        (Path(layout.dict_dir) / "dict.bin").exists()
        and (Path(layout.dict_dir) / "dict.offsets.bin").exists()
        and (Path(layout.dict_dir) / "meta.json").exists()
        and (Path(layout.uuids_dir) / "citypes.json").exists()
        and (Path(layout.uuids_dir) / "uuids.bin").exists()
        and (Path(layout.uuids_dir) / "resolver.bin").exists()
    )


@dataclass
class _AttrMeta:
    name: str = ""
    description: str = ""
    hasEnum: str = ""
    has: bool = False


def _index_attr_array(out: dict[str, _AttrMeta], arr: Any) -> None:
    if not isinstance(arr, list):
        return
    for a in arr:
        if not isinstance(a, dict):
            continue
        tech = a.get("technicalName")
        if not isinstance(tech, str):
            continue

        m = _AttrMeta(has=True)

        nm = a.get("name")
        if isinstance(nm, str):
            m.name = nm

        desc = a.get("description")
        if isinstance(desc, str):
            m.description = desc

        cons = a.get("constraints")
        if isinstance(cons, list):
            for c in cons:
                if not isinstance(c, dict):
                    continue
                ec = c.get("enumCode")
                if isinstance(ec, str):
                    m.hasEnum = ec
                    break

        out[tech] = m


def _load_type_attr_meta(meta_file: Path) -> dict[str, _AttrMeta]:
    if not meta_file.exists():
        return {}
    try:
        j = load_json_file(meta_file)
    except Exception:
        return {}

    idx: dict[str, _AttrMeta] = {}

    if isinstance(j, dict):
        if "attributes" in j:
            _index_attr_array(idx, j.get("attributes"))

        profs = j.get("attributeProfiles")
        if isinstance(profs, list):
            for prof in profs:
                if isinstance(prof, dict) and "attributes" in prof:
                    _index_attr_array(idx, prof.get("attributes"))

    return idx


def _write_type_schema_files(out_dir: Path, meta_file: Path, observed_attrs: set[str]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    names = sorted(observed_attrs, key=_utf8_sort_key)
    meta_idx = _load_type_attr_meta(meta_file)

    attrs_out: list[dict[str, Any]] = []
    for tech in names:
        it = meta_idx.get(tech)
        if it and it.has:
            attrs_out.append(
                {
                    "technicalName": tech,
                    "name": it.name or None,
                    "description": it.description or None,
                    "hasEnum": it.hasEnum or None,
                }
            )
        else:
            attrs_out.append(
                {
                    "technicalName": tech,
                    "name": None,
                    "description": None,
                    "hasEnum": None,
                }
            )

    fmt = {
        "attributeLayout": "grid",
        "attributeCount": len(names),
        "metaAttributeCount": 6,
    }

    atomic_write_json(out_dir / "attributes.json", attrs_out, ensure_ascii=False, indent=2)
    atomic_write_json(out_dir / "format.json", fmt, ensure_ascii=False, indent=2)


def _write_dict_files(layout: Any, dct: ValueDictionary, verbose: bool) -> None:
    if verbose:
        print("[freeze] finalizing dictionary (sort + offsets)")

    dct.finalize_sorted()

    dict_dir = Path(layout.dict_dir)
    dict_dir.mkdir(parents=True, exist_ok=True)

    values = dct.values

    offs: list[int] = [0]
    cur = 0

    def _write_dict_bin(f) -> None:
        nonlocal cur
        for v in values:
            b = v.encode("utf-8")
            f.write(b)
            cur += len(b)
            offs.append(cur)

    atomic_write_with(dict_dir / "dict.bin", _write_dict_bin)
    write_u64le_file(dict_dir / "dict.offsets.bin", offs)
    atomic_write_json(dict_dir / "meta.json", {"valueCount": len(values)}, ensure_ascii=False, indent=2)

    if verbose:
        print(f"[dict] valueCount={len(values)} bytes={cur}")


def _observed_citypes(pre: PrepassResult) -> list[str]:
    return sorted(pre.uuids_by_citype.keys(), key=_utf8_sort_key)


def _load_citypes_list_keep_order(p: Path) -> list[str]:
    if not p.exists():
        return []
    j = load_json_file(p)
    if not isinstance(j, list):
        raise RuntimeError("citypes_list.json must be a JSON array")

    seen: set[str] = set()
    out: list[str] = []
    for x in j:
        if not isinstance(x, str):
            continue
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out


def _write_citypes(layout: Any, pre: PrepassResult, verbose: bool) -> list[str]:
    observed = _observed_citypes(pre)

    citypes_list_path = _citypes_list_path(layout)
    final_list = _load_citypes_list_keep_order(citypes_list_path)

    if final_list:
        already = set(final_list)
        for c in observed:
            if c not in already:
                final_list.append(c)
    else:
        final_list = observed

    if verbose:
        print(f"[citypes] metadata_list={'yes' if citypes_list_path.exists() else 'no'} "
              f"observed={len(observed)} final={len(final_list)}")

    atomic_write_json(Path(layout.uuids_dir) / "citypes.json", final_list, ensure_ascii=False, indent=2)
    return final_list


def _build_citype_index_map(citypes: list[str]) -> dict[str, int]:
    return {c: i for i, c in enumerate(citypes)}


def freeze_schema(layout: Any, *, force: bool = False, verbose: bool = False) -> None:
    global _LAST_PREPASS

    if (is_done(layout.packed_root, ".pass1_5.done") and pass1_5_outputs_ok(layout)) and not force:
        if verbose:
            print("[pass1.5] already done; skipping")
        return

    if _LAST_PREPASS is None:
        run_prepass(layout, force=True, verbose=verbose)

    pre = _LAST_PREPASS
    if pre is None:
        raise RuntimeError("freeze_schema: missing prepass results")

    if verbose:
        print("[pass1.5] writing per-type schemas")

    nodes_meta_dir = _meta_dir(layout, "nodes")
    rels_meta_dir = _meta_dir(layout, "rels")

    empty: set[str] = set()

    # A) per-citype schema
    for citype, uuids in pre.uuids_by_citype.items():
        observed = pre.attrs_ent.seen_attrs_by_type.get(citype, empty)
        out_dir = Path(layout.nodes_packed) / citype
        meta_file = nodes_meta_dir / f"{citype}.json"
        _write_type_schema_files(out_dir, meta_file, observed)

    # B) per-reltype schema
    reltypes = sorted(pre.attrs_rel.object_count_by_type.keys(), key=_utf8_sort_key)
    for reltype in reltypes:
        observed = pre.attrs_rel.seen_attrs_by_type.get(reltype, empty)
        out_dir = Path(layout.rels_packed) / reltype
        meta_file = rels_meta_dir / f"{reltype}.json"
        _write_type_schema_files(out_dir, meta_file, observed)

    # C) dictionary
    _write_dict_files(layout, pre.value_dict, verbose=verbose)

    # D) citypes list
    citypes_final = _write_citypes(layout, pre, verbose=verbose)
    citype_index_of = _build_citype_index_map(citypes_final)

    # E) per-citype uuids.bin (sort + dedupe)
    if verbose:
        print("[pass1.5] writing per-citype uuids.bin")
    for citype, v in list(pre.uuids_by_citype.items()):
        v.sort()
        dedup: list[Uuid128] = []
        last: Optional[Uuid128] = None
        for u in v:
            if last is None or u != last:
                dedup.append(u)
                last = u
        pre.uuids_by_citype[citype] = dedup
        write_uuid16_file(Path(layout.nodes_packed) / citype / "uuids.bin", dedup)

    # F) global uuids.bin + resolver.bin
    if verbose:
        print("[pass1.5] writing global uuids.bin + resolver.bin")

    recs: list[tuple[Uuid128, int, int]] = []  # (uuid, citype_index, local_index)
    for citype, v in pre.uuids_by_citype.items():
        ci = citype_index_of.get(citype)
        if ci is None:
            raise RuntimeError(f"Citype '{citype}' missing from final citypes list")
        for li, u in enumerate(v):
            recs.append((u, ci, li))

    recs.sort(key=lambda r: r[0])  # Uuid128 ordering

    uuids_out = Path(layout.uuids_dir) / "uuids.bin"
    resolver_out = Path(layout.uuids_dir) / "resolver.bin"

    def _write_global_uuids(f) -> None:
        for u, _, _ in recs:
            f.write(u.bytes16)

    def _write_global_resolver(f) -> None:
        pack = RESOLVER_ROW.pack
        for _, ci, li in recs:
            f.write(pack(int(ci), int(li)))

    atomic_write_with(uuids_out, _write_global_uuids)
    atomic_write_with(resolver_out, _write_global_resolver)

    # G) per-citype global_ids.bin
    if verbose:
        print("[pass1.5] writing per-citype global_ids.bin")

    global_ids: list[list[int]] = [[] for _ in range(len(citypes_final))]
    for ci, citype in enumerate(citypes_final):
        v = pre.uuids_by_citype.get(citype)
        if v:
            global_ids[ci] = [0] * len(v)

    for gid, (_, ci, li) in enumerate(recs):
        global_ids[ci][li] = gid

    for ci, citype in enumerate(citypes_final):
        arr = global_ids[ci]
        if not arr:
            continue
        out = Path(layout.nodes_packed) / citype / "global_ids.bin"
        write_u32le_file(out, arr)

    # H) manifests
    epoch = int(time.time())
    atomic_write_json(
        Path(layout.nodes_packed) / "citypes_manifest.json",
        {"citypes": citypes_final, "count": len(citypes_final), "schemaEpochUtc": str(epoch), "formatVersion": 1},
        ensure_ascii=False,
        indent=2,
    )
    atomic_write_json(
        Path(layout.rels_packed) / "reltypes_manifest.json",
        {"reltypes": reltypes, "count": len(reltypes), "schemaEpochUtc": str(epoch), "formatVersion": 1},
        ensure_ascii=False,
        indent=2,
    )

    # I) done marker
    n_nodes = sum(len(v) for v in pre.uuids_by_citype.values())
    dict_vals = len(pre.value_dict.values)
    ss = []
    ss.append("pass=1.5")
    ss.append(f"date={getattr(layout, 'dump_date', '')}")
    ss.append(f"utc_epoch={epoch}")
    ss.append(f"citypes={len(pre.uuids_by_citype)}")
    ss.append(f"nodes={n_nodes}")
    ss.append(f"dict_values={dict_vals}")
    mark_done(layout.packed_root, ".pass1_5.done", "\n".join(ss) + "\n")

    if verbose:
        print("[pass1.5] done")