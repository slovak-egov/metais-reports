#!/usr/bin/env python3
"""
repack.py

Repack a subset of an existing "packed" MetaIS dataset into a new packed/ directory.

Two uses:

1) CLI + JSON config
   ------------------
   python3 scripts/repack_packed.py \
       --source-packed output/07-12-2025/packed \
       --dest-packed   repacks/po_as_ks_isvs_2025-12-07 \
       --config        repack_config.json

   Example repack_config.json:

   {
     "entities": {
       "includeTypes": ["PO", "AS", "KS", "ISVS", "Projekt"],
       "validity": "all"          // "all" | "valid" | "invalid"
     },
     "relations": {
       "includeTypes": [
         "PO_asociuje_Projekt",
         "Projekt_realizuje_AS",
         "Projekt_realizuje_KS",
         "Projekt_realizuje_ISVS",
         "ISVS_vytvara_ISVS",
         "PO_asociuje_Projekt_invalid",
         "Projekt_realizuje_AS_invalid"
       ],
       "validity": "all"          // "all" | "valid" | "invalid"
     }
   }

   Notes:
     - If includeTypes is omitted or empty, we default to "all types present in source".
     - For relations, names are the packed technicalNames (so `_invalid` variants are normal
       relation types as far as this script is concerned). The "validity" flag can:
         * "all": keep both normal and *_invalid relation types (subject to includeTypes).
         * "valid": drop *_invalid relation types.
         * "invalid": keep only *_invalid relation types.

2) Python API
   -----------
   from pathlib import Path
   from scripts.repack_packed import RepackSpec, repack

   spec = RepackSpec(
       source_packed=Path("output/07-12-2025/packed"),
       dest_packed=Path("repacks/interesting_projects"),
       # Restrict to these node types:
       entity_types={"Projekt", "PO", "AS", "KS", "ISVS"},
       entity_validity="valid",
       # Only include these exact UUIDs (bytes); anything not in the set is dropped:
       uuid_allowlist={ ... },   # set of 16-byte UUIDs
       # Include all relation types, but only those edges where both endpoints are in uuid_allowlist:
       relation_types=None,
       relation_validity="all",
       include_relations_if_both_endpoints_in_allowlist=True,
   )

   repack(spec)

   You can pre-compute the UUID allowlist however you like (e.g. “only projects with
   realized status that have PO, AS, KS, ISVS, and their neighbors”) and then give it
   to the repacker as the “truth set” to slice out.

Implementation notes / simplifications:
  - We initially select rows using the *original global dict* (to detect "INVALIDATED"
    states efficiently).
  - When writing the subset, we:
      * record all dict indices actually used in the repacked nodes,
      * build a new compact dict (values + offsets + meta) that includes only these,
      * remap all node .bin files to use the new dense index space.
  - We rebuild:
      * node type files (bin + uuids + meta with new recordCount),
      * dict/ (compacted),
      * uuid_index/uuids.bin + meta,
      * uuid_types/types.bin + meta,
      * relation files (src.tgt + tgt.src + meta),
      * relations indexes (index_by_reltype.json, index_by_ctype.json, index.json),
      * manifest.json, nodes/index.json.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import struct
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Set, Tuple, Literal

# ---------------------------------------------------------------------------
# Basic constants
# ---------------------------------------------------------------------------

INT32 = struct.Struct("<i")
U64   = struct.Struct("<Q")
MISSING_SENTINEL = -1


@dataclass
class RepackSpec:
    # Paths
    source_packed: Path          # existing packed/ (source of truth)
    dest_packed:   Path          # new packed/ to create

    # Node filters
    entity_types: Optional[Set[str]] = None   # None → all types in source
    entity_validity: Literal["all", "valid", "invalid"] = "all"

    # Relation filters
    relation_types: Optional[Set[str]] = None # None → all reltypes in source
    relation_validity: Literal["all", "valid", "invalid"] = "all"

    # Advanced: explicit UUID allowlist
    # If non-empty, only nodes whose UUID is in this set will be kept,
    # *after* applying entity_types + entity_validity filters.
    uuid_allowlist: Optional[Set[bytes]] = None

    # How to include relations relative to uuid_allowlist:
    # - If True: keep only edges where both endpoints are in the final node-set
    #            (after all node filters + allowlist).
    # - If False: keep edges if at least one endpoint is in the final node-set.
    include_relations_if_both_endpoints_in_allowlist: bool = True

    # For manifest
    profile_name: str = "repack"
    source_dump_date: Optional[str] = None    # If None, taken from source manifest (if present)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def dump_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def read_uuid_index(source_uuid_index_dir: Path) -> List[bytes]:
    """Read global uuid_index/uuids.bin into a list of 16-byte UUIDs."""
    uuids_path = source_uuid_index_dir / "uuids.bin"
    meta_path  = source_uuid_index_dir / "meta.json"
    meta = load_json(meta_path)
    count = int(meta.get("recordCount", 0))
    uuids: List[bytes] = []
    with uuids_path.open("rb") as f:
        for _ in range(count):
            b = f.read(16)
            if len(b) != 16:
                raise IOError(f"Unexpected EOF in {uuids_path}")
            uuids.append(b)
    if len(uuids) != count:
        raise RuntimeError(f"uuid_index meta recordCount={count}, but read {len(uuids)} uuids")
    return uuids


def read_uuid_types(source_uuid_types_dir: Path, uuid_bytes: List[bytes]) -> Dict[bytes, str]:
    """
    Rebuild mapping UUID bytes -> ctype name from uuid_types/{types.bin, meta.json}.
    """
    types_meta_path = source_uuid_types_dir / "meta.json"
    types_bin_path  = source_uuid_types_dir / "types.bin"

    meta = load_json(types_meta_path)
    bytes_per_code = int(meta.get("bytesPerCode", 2))
    type_entries   = meta.get("types", [])
    code_to_type: Dict[int, str] = {
        int(entry["code"]): entry["typeName"]
        for entry in type_entries
    }

    uuid_to_ctype: Dict[bytes, str] = {}
    with types_bin_path.open("rb") as f:
        for i, u in enumerate(uuid_bytes):
            cb = f.read(bytes_per_code)
            if len(cb) != bytes_per_code:
                raise IOError(f"Unexpected EOF in {types_bin_path} at record {i}")
            code = int.from_bytes(cb, "little", signed=False)
            tname = code_to_type.get(code)
            if tname is not None:
                uuid_to_ctype[u] = tname

    return uuid_to_ctype


def find_invalid_state_indices(dict_dir: Path) -> Set[int]:
    """
    Scan dict.values.bin / dict.offsets.bin once and find all dict indices whose
    value is exactly the string "INVALIDATED".

    We use this to classify node validity from __meta__state column without
    having to parse JSON for every row.
    """
    values_path  = dict_dir / "dict.values.bin"
    offsets_path = dict_dir / "dict.offsets.bin"
    meta_path    = dict_dir / "dict.meta.json"

    meta = load_json(meta_path)
    value_count = int(meta.get("valueCount", 0))

    offsets: List[int] = []
    with offsets_path.open("rb") as f_off:
        for _ in range(value_count + 1):
            b = f_off.read(U64.size)
            if len(b) != U64.size:
                raise IOError(f"Unexpected EOF in {offsets_path}")
            (off,) = U64.unpack(b)
            offsets.append(off)

    invalid_indices: Set[int] = set()

    with values_path.open("rb") as f_val:
        for idx in range(value_count):
            start = offsets[idx]
            end   = offsets[idx + 1]
            if end < start:
                raise RuntimeError(f"Invalid offsets for dict index {idx}: {start} > {end}")
            length = end - start
            f_val.seek(start)
            raw = f_val.read(length)
            s = raw.decode("utf-8")
            # Values are stored as JSON strings, so "INVALIDATED" is encoded as '"INVALIDATED"'
            try:
                val = json.loads(s)
            except Exception:
                continue
            if val == "INVALIDATED":
                invalid_indices.add(idx)

    return invalid_indices

def _compact_dict(
    src_dict_dir: Path,
    dst_dict_dir: Path,
    used_indices: Set[int],
) -> Dict[int, int]:
    """
    Build a compact dictionary in dst_dict_dir, containing only values whose
    indices appear in used_indices.

    Returns:
        old_to_new: mapping from old dict index -> new dict index.
    """
    dst_dict_dir.mkdir(parents=True, exist_ok=True)

    values_path_src  = src_dict_dir / "dict.values.bin"
    offsets_path_src = src_dict_dir / "dict.offsets.bin"
    meta_path_src    = src_dict_dir / "dict.meta.json"

    meta_src = load_json(meta_path_src)
    value_count = int(meta_src.get("valueCount", 0))

    # Nothing used → write an empty dict.
    if not used_indices:
        values_path_dst  = dst_dict_dir / "dict.values.bin"
        offsets_path_dst = dst_dict_dir / "dict.offsets.bin"
        meta_path_dst    = dst_dict_dir / "dict.meta.json"

        with values_path_dst.open("wb"):
            pass
        with offsets_path_dst.open("wb") as f_off:
            # one offset (0) for 0 values
            f_off.write(U64.pack(0))

        meta_dst = dict(meta_src)
        meta_dst["valueCount"] = 0
        dump_json(meta_path_dst, meta_dst)

        return {}

    # Filter to valid indices and sort ascending for deterministic layout
    used_sorted = sorted(
        idx for idx in used_indices
        if 0 <= idx < value_count
    )

    old_to_new: Dict[int, int] = {}
    values_path_dst  = dst_dict_dir / "dict.values.bin"
    offsets_path_dst = dst_dict_dir / "dict.offsets.bin"
    meta_path_dst    = dst_dict_dir / "dict.meta.json"

    # Read all offsets from source
    offsets: List[int] = []
    with offsets_path_src.open("rb") as f_off:
        for _ in range(value_count + 1):
            b = f_off.read(U64.size)
            if len(b) != U64.size:
                raise IOError(f"Unexpected EOF in {offsets_path_src}")
            (off,) = U64.unpack(b)
            offsets.append(off)

    # Build new values + offsets
    new_offsets: List[int] = [0]
    current_offset = 0

    with values_path_src.open("rb") as f_val_src, \
         values_path_dst.open("wb") as f_val_dst:

        for new_idx, old_idx in enumerate(used_sorted):
            old_to_new[old_idx] = new_idx

            start = offsets[old_idx]
            end   = offsets[old_idx + 1]
            if end < start:
                raise RuntimeError(f"Invalid offsets for dict index {old_idx}: {start} > {end}")
            length = end - start

            f_val_src.seek(start)
            chunk = f_val_src.read(length)
            if len(chunk) != length:
                raise IOError(f"Unexpected EOF in {values_path_src} at index {old_idx}")

            f_val_dst.write(chunk)
            current_offset += length
            new_offsets.append(current_offset)

    # Write new offsets.bin
    with offsets_path_dst.open("wb") as f_off_dst:
        for off in new_offsets:
            f_off_dst.write(U64.pack(off))

    # Write new meta.json (copy from source, but update valueCount)
    meta_dst = dict(meta_src)
    meta_dst["valueCount"] = len(used_sorted)
    dump_json(meta_path_dst, meta_dst)

    return old_to_new


def _remap_node_bins(
    dst_nodes_dir: Path,
    old_to_new_dict_index: Dict[int, int],
) -> None:
    """
    For every node type in dst_nodes_dir, rewrite its .bin file so that each
    dict index is remapped via old_to_new_dict_index.

    Assumes:
      - meta["recordCount"], meta["blockSize"], meta["intBytes"] are consistent,
      - all non-MISSING_SENTINEL indices appear in old_to_new_dict_index.
    """
    for meta_path in dst_nodes_dir.glob("*.meta.json"):
        tname = meta_path.name[:-len(".meta.json")]
        bin_path = dst_nodes_dir / f"{tname}.bin"

        if not bin_path.is_file():
            continue

        meta = load_json(meta_path)
        record_count = int(meta.get("recordCount", 0))
        block_size   = int(meta.get("blockSize", 0))
        int_bytes    = int(meta.get("intBytes", 4))

        if record_count == 0 or block_size <= 0:
            continue
        if int_bytes != 4:
            raise ValueError(f"[repack] only intBytes=4 supported for type {tname}")

        block_bytes = block_size * int_bytes
        tmp_bin_path = bin_path.with_suffix(".bin.tmp")

        with bin_path.open("rb") as f_in, tmp_bin_path.open("wb") as f_out:
            for rec_idx in range(record_count):
                row_bytes = f_in.read(block_bytes)
                if len(row_bytes) != block_bytes:
                    raise IOError(
                        f"[repack] unexpected EOF in {bin_path} at record {rec_idx}"
                    )

                # Remap every int in the row
                new_row = bytearray(block_bytes)
                offset = 0
                for (val,) in INT32.iter_unpack(row_bytes):
                    if val == MISSING_SENTINEL:
                        new_val = MISSING_SENTINEL
                    else:
                        try:
                            new_val = old_to_new_dict_index[val]
                        except KeyError as e:
                            raise RuntimeError(
                                f"[repack] dict index {val} used in {tname} "
                                "but not in compacted dict"
                            ) from e
                    new_row[offset:offset + INT32.size] = INT32.pack(new_val)
                    offset += INT32.size

                f_out.write(new_row)

        os.replace(tmp_bin_path, bin_path)

def parse_attributes_from_meta(meta: Dict[str, Any]) -> Tuple[List[str], Dict[str, Dict[str, Optional[str]]]]:
    """
    Normalize meta["attributes"] into:
      - attr_order: [technicalName, ...]
      - attr_meta:  technicalName -> {name, description}
    Supports:
      - ["Gen_Profil_nazov", ...] (legacy)
      - [[tech, human, desc], ...] (new)
      - [{"technicalName": ..., "name": ..., "description": ...}, ...] (future)
    """
    raw_attrs = meta.get("attributes", [])
    attr_order: List[str] = []
    attr_meta: Dict[str, Dict[str, Optional[str]]] = {}

    for entry in raw_attrs:
        if isinstance(entry, str):
            tech = entry
            name = None
            desc = None
        elif isinstance(entry, list) and len(entry) >= 1:
            tech = entry[0]
            name = entry[1] if len(entry) > 1 else None
            desc = entry[2] if len(entry) > 2 else None
        elif isinstance(entry, dict):
            tech = entry.get("technicalName") or entry.get("name")
            name = entry.get("name")
            desc = entry.get("description")
        else:
            continue

        if not tech:
            continue

        attr_order.append(tech)
        attr_meta[tech] = {"name": name, "description": desc}

    return attr_order, attr_meta


# ---------------------------------------------------------------------------
# Core repack logic
# ---------------------------------------------------------------------------

def repack(spec: RepackSpec) -> None:
    """
    Main entry point used by both CLI and Python API.
    """
    src = spec.source_packed.resolve()
    dst = spec.dest_packed.resolve()

    if not src.is_dir():
        raise FileNotFoundError(f"source_packed not found: {src}")

    if dst.exists() and any(dst.iterdir()):
        print(f"[repack] WARNING: dest_packed {dst} already exists and is not empty; removing it")
        shutil.rmtree(dst)
    else:
        # parent dirs may not exist yet
        dst.parent.mkdir(parents=True, exist_ok=True)
        
    dst.mkdir(parents=True, exist_ok=True)

    src_nodes_dir    = src / "nodes"
    src_rels_dir     = src / "relations"
    src_dict_dir     = src / "dict"
    src_uuid_index   = src / "uuid_index"
    src_uuid_types   = src / "uuid_types"

    dst_nodes_dir    = dst / "nodes"
    dst_rels_dir     = dst / "relations"
    dst_dict_dir     = dst / "dict"
    dst_uuid_index   = dst / "uuid_index"
    dst_uuid_types   = dst / "uuid_types"

    dst_nodes_dir.mkdir(parents=True, exist_ok=True)
    dst_rels_dir.mkdir(parents=True, exist_ok=True)
    dst_uuid_index.mkdir(parents=True, exist_ok=True)
    dst_uuid_types.mkdir(parents=True, exist_ok=True)

    # 1) Load source manifest if present to get nodeTypes, relationTypes, sourceDumpDate.
    src_manifest_path = src / "manifest.json"
    if src_manifest_path.is_file():
        src_manifest = load_json(src_manifest_path)
        all_node_types_source: List[str] = src_manifest.get("nodeTypes", [])
        all_rel_types_source: List[str]  = src_manifest.get("relationTypes", [])
        source_dump_date = spec.source_dump_date or src_manifest.get("sourceDumpDate")
    else:
        # Fallback: derive from file names
        all_node_types_source = sorted(
            [p.name[:-len(".meta.json")] for p in src_nodes_dir.glob("*.meta.json")]
        )
        all_rel_types_source = sorted(
            # meta files have <rel>.src.tgt.meta.json / <rel>.tgt.src.meta.json
            set(p.name.split(".src.tgt.meta.json")[0]
                if p.name.endswith(".src.tgt.meta.json")
                else p.name.split(".tgt.src.meta.json")[0]
                for p in src_rels_dir.glob("*.meta.json"))
        )
        source_dump_date = spec.source_dump_date or "unknown"

    # Determine which node types and relation types to include.
    if spec.entity_types:
        node_types_to_include = sorted(t for t in all_node_types_source if t in spec.entity_types)
    else:
        node_types_to_include = list(all_node_types_source)

    if spec.relation_types:
        rel_types_to_include = sorted(t for t in all_rel_types_source if t in spec.relation_types)
    else:
        rel_types_to_include = list(all_rel_types_source)

    # Apply relation_validity filter on *_invalid vs non-invalid.
    if spec.relation_validity == "valid":
        rel_types_to_include = [r for r in rel_types_to_include if not r.endswith("_invalid")]
    elif spec.relation_validity == "invalid":
        rel_types_to_include = [r for r in rel_types_to_include if r.endswith("_invalid")]

    # 2) Read global uuid_index and uuid_types from source
    src_uuid_bytes = read_uuid_index(src_uuid_index)
    src_uuid_to_ctype = read_uuid_types(src_uuid_types, src_uuid_bytes)

    # 3) Precompute dict indices that correspond to state == "INVALIDATED"
    invalid_state_indices = find_invalid_state_indices(src_dict_dir)

    # 4) First pass over nodes: decide which rows to keep and collect included UUIDs
    #    per_type_rows[type] = [old_row_index, ...] (ascending)
    per_type_rows: Dict[str, List[int]] = {}
    per_type_record_count: Dict[str, int] = {}
    included_uuid_list: List[bytes] = []

    # entity_validity: "all", "valid", "invalid"
    def _keep_node(is_valid: bool) -> bool:
        if spec.entity_validity == "all":
            return True
        if spec.entity_validity == "valid":
            return is_valid
        if spec.entity_validity == "invalid":
            return not is_valid
        return True

    for tname in node_types_to_include:
        meta_path  = src_nodes_dir / f"{tname}.meta.json"
        bin_path   = src_nodes_dir / f"{tname}.bin"
        uuids_path = src_nodes_dir / f"{tname}.uuids.bin"

        if not (meta_path.is_file() and bin_path.is_file() and uuids_path.is_file()):
            # Nothing to repack for this type
            continue

        meta = load_json(meta_path)
        record_count = int(meta.get("recordCount", 0))
        block_size   = int(meta.get("blockSize", 0))
        int_bytes    = int(meta.get("intBytes", 4))

        if record_count == 0 or block_size <= 0:
            continue

        if int_bytes != 4:
            raise ValueError(f"[repack] only intBytes=4 supported for type {tname}")

        block_bytes = block_size * int_bytes

        attr_order, _attr_meta = parse_attributes_from_meta(meta)
        try:
            state_col_index = attr_order.index("__meta__state")
        except ValueError:
            state_col_index = None

        rows_to_keep: List[int] = []

        with uuids_path.open("rb") as f_uuid, bin_path.open("rb") as f_bin:
            for idx in range(record_count):
                u_bytes = f_uuid.read(16)
                if len(u_bytes) != 16:
                    raise IOError(f"[repack] unexpected EOF in {uuids_path} at record {idx}")

                # Determine validity from __meta__state if available; else treat as valid.
                if state_col_index is None:
                    is_valid = True
                else:
                    offset = idx * block_bytes + state_col_index * int_bytes
                    f_bin.seek(offset)
                    raw = f_bin.read(int_bytes)
                    if len(raw) != int_bytes:
                        raise IOError(f"[repack] unexpected EOF in {bin_path} at record {idx}")
                    (state_idx,) = INT32.unpack(raw)
                    if state_idx == MISSING_SENTINEL:
                        is_valid = True
                    else:
                        is_valid = state_idx not in invalid_state_indices

                # Decide whether to keep this node at all.
                if not _keep_node(is_valid):
                    continue

                # If uuid_allowlist is provided, enforce it.
                if spec.uuid_allowlist is not None and u_bytes not in spec.uuid_allowlist:
                    continue

                rows_to_keep.append(idx)
                included_uuid_list.append(u_bytes)

        if rows_to_keep:
            per_type_rows[tname] = rows_to_keep
            per_type_record_count[tname] = len(rows_to_keep)

    # If nothing was selected, bail out early.
    if not included_uuid_list:
        print("[repack] No nodes selected by filters; writing an empty skeleton and exiting.")
        # Still copy dict and write a minimal manifest.
        shutil.copytree(src_dict_dir, dst_dict_dir, dirs_exist_ok=True)
        _write_empty_manifest(dst, source_dump_date, spec)
        return

    # 5) Build new global UUID index for repack
    unique_uuids = sorted(set(included_uuid_list))
    uuid_to_new_id: Dict[bytes, int] = {b: i for i, b in enumerate(unique_uuids)}

    # Rebuild uuid_to_ctype for subset, using original mapping
    repack_uuid_to_ctype: Dict[bytes, str] = {}
    for u in unique_uuids:
        tname = src_uuid_to_ctype.get(u)
        if tname is not None:
            repack_uuid_to_ctype[u] = tname

    # 6) Write per-type nodes/*.bin + *.uuids.bin + *.meta.json into dest
    #    and collect all dict indices that appear in the subset.
    node_index_entries: List[Dict[str, Any]] = []
    total_nodes = 0
    used_dict_indices: Set[int] = set()

    for tname, rows_to_keep in sorted(per_type_rows.items()):
        meta_path_src  = src_nodes_dir / f"{tname}.meta.json"
        bin_path_src   = src_nodes_dir / f"{tname}.bin"
        uuids_path_src = src_nodes_dir / f"{tname}.uuids.bin"

        meta = load_json(meta_path_src)
        record_count = int(meta.get("recordCount", 0))
        block_size   = int(meta.get("blockSize", 0))
        int_bytes    = int(meta.get("intBytes", 4))
        block_bytes  = block_size * int_bytes

        meta_path_dst  = dst_nodes_dir / f"{tname}.meta.json"
        bin_path_dst   = dst_nodes_dir / f"{tname}.bin"
        uuids_path_dst = dst_nodes_dir / f"{tname}.uuids.bin"

        # Re-copy in sorted order by old index (rows_to_keep comes in ascending order).
        with uuids_path_src.open("rb") as f_uuid_src, \
             bin_path_src.open("rb")   as f_bin_src, \
             uuids_path_dst.open("wb") as f_uuid_dst, \
             bin_path_dst.open("wb")   as f_bin_dst:

            # We'll walk linearly over the source records.
            keep_iter = iter(rows_to_keep)
            next_keep = next(keep_iter, None)

            for idx in range(record_count):
                uuid_bytes = f_uuid_src.read(16)
                row_bytes  = f_bin_src.read(block_bytes)
                if len(uuid_bytes) != 16 or len(row_bytes) != block_bytes:
                    raise IOError(f"[repack] unexpected EOF copying type {tname}, idx={idx}")

                if next_keep is not None and idx == next_keep:
                    f_uuid_dst.write(uuid_bytes)
                    f_bin_dst.write(row_bytes)

                    # Track all dict indices used in this row
                    for (val,) in INT32.iter_unpack(row_bytes):
                        if val != MISSING_SENTINEL:
                            used_dict_indices.add(val)

                    next_keep = next(keep_iter, None)

        # Update meta with new recordCount; sortedBy remains "uuid_bytes" as we kept order.
        meta["recordCount"] = len(rows_to_keep)
        dump_json(meta_path_dst, meta)

        total_nodes += len(rows_to_keep)
        node_index_entries.append({
            "typeName": tname,
            "recordCount": len(rows_to_keep),
            "metaFile": f"{tname}.meta.json",
            "binFile":  f"{tname}.bin",
            "uuidsFile": f"{tname}.uuids.bin",
        })

    # 7) Build compact dictionary containing only values actually used
    #    in this repack, and remap all node bins to the new dict indices.
    old_to_new_dict_index = _compact_dict(
        src_dict_dir=src_dict_dir,
        dst_dict_dir=dst_dict_dir,
        used_indices=used_dict_indices,
    )

    _remap_node_bins(
        dst_nodes_dir=dst_nodes_dir,
        old_to_new_dict_index=old_to_new_dict_index,
    )

    # 8) Write uuid_index and uuid_types for repack
    _write_uuid_index_and_types(
        dst_uuid_index,
        dst_uuid_types,
        unique_uuids,
        repack_uuid_to_ctype,
    )

    # 9) Repack relations based on new UUID set
    rel_index_entries, reltype_index_rel, ctype_index_rel, total_rel_pairs = \
        _repack_relations(
            src_rels_dir=src_rels_dir,
            dst_rels_dir=dst_rels_dir,
            uuid_bytes_source=src_uuid_bytes,
            uuid_to_new_id=uuid_to_new_id,
            uuid_to_ctype=repack_uuid_to_ctype,
            rel_types_to_include=rel_types_to_include,
            require_both_endpoints=spec.include_relations_if_both_endpoints_in_allowlist,
        )

    # 10) Write relation indexes
    if reltype_index_rel:
        dump_json(dst_rels_dir / "index_by_reltype.json", reltype_index_rel)
    if ctype_index_rel:
        dump_json(dst_rels_dir / "index_by_ctype.json", ctype_index_rel)

    # 11) Write nodes/index.json and relations/index.json
    if node_index_entries:
        dump_json(dst_nodes_dir / "index.json", {"types": node_index_entries})
    if rel_index_entries:
        dump_json(dst_rels_dir / "index.json", {"relationTypes": rel_index_entries})

    # 12) Write manifest.json
    manifest = {
        "version": 1,
        "createdAt": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "sourceDumpDate": source_dump_date,
        "profile": spec.profile_name,
        "filters": {
            "mode": "python-api" if spec.uuid_allowlist is not None else "cli-json",
            "entities": {
                "types": sorted(list(node_types_to_include)),
                "validity": spec.entity_validity,
            },
            "relations": {
                "types": sorted(list(rel_types_to_include)),
                "validity": spec.relation_validity,
                "requireBothEndpointsInSubset": spec.include_relations_if_both_endpoints_in_allowlist,
            },
        },
        "nodeTypes": sorted([e["typeName"] for e in node_index_entries]),
        "relationTypes": sorted([e["technicalName"] for e in rel_index_entries]),
        "counts": {
            "nodesTotal": total_nodes,
            "relationPairsTotal": total_rel_pairs,
        },
    }
    dump_json(dst / "manifest.json", manifest)

    print(f"[repack] Done. Nodes={total_nodes}, relations={total_rel_pairs} pairs.")
    print(f"[repack] Source: {src}")
    print(f"[repack] Dest:   {dst}")


def _write_empty_manifest(dst: Path, source_dump_date: str, spec: RepackSpec) -> None:
    manifest = {
        "version": 1,
        "createdAt": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "sourceDumpDate": source_dump_date,
        "profile": spec.profile_name,
        "filters": {
            "mode": "cli-json",
            "entities": {
                "types": [],
                "validity": spec.entity_validity,
            },
            "relations": {
                "types": [],
                "validity": spec.relation_validity,
            },
        },
        "nodeTypes": [],
        "relationTypes": [],
        "counts": {
            "nodesTotal": 0,
            "relationPairsTotal": 0,
        },
    }
    dump_json(dst / "manifest.json", manifest)


def _write_uuid_index_and_types(
    uuid_index_dir: Path,
    uuid_types_dir: Path,
    uuids: List[bytes],
    uuid_to_ctype: Dict[bytes, str],
) -> None:
    """Write uuid_index/{uuids.bin, meta.json} and uuid_types/{types.bin, meta.json}."""
    uuid_index_dir.mkdir(parents=True, exist_ok=True)
    uuid_types_dir.mkdir(parents=True, exist_ok=True)

    uuids_path = uuid_index_dir / "uuids.bin"
    meta_path  = uuid_index_dir / "meta.json"

    with uuids_path.open("wb") as f:
        for b in uuids:
            f.write(b)

    meta = {
        "recordCount": len(uuids),
        "uuidBytes": 16,
        "endianness": "LE",
        "sortedBy": "uuid_bytes",
    }
    dump_json(meta_path, meta)

    # Build type→code mapping from uuid_to_ctype subset
    all_types = sorted({t for t in uuid_to_ctype.values() if t is not None})
    bytes_per_code = 2
    if len(all_types) >= 2 ** (8 * bytes_per_code):
        raise ValueError(f"[repack] Too many types ({len(all_types)}) for bytesPerCode={bytes_per_code}")
    type_to_code = {t: i for i, t in enumerate(all_types)}
    code_to_type = {i: t for t, i in type_to_code.items()}

    types_bin_path  = uuid_types_dir / "types.bin"
    types_meta_path = uuid_types_dir / "meta.json"

    with types_bin_path.open("wb") as f:
        for b in uuids:
            tname = uuid_to_ctype.get(b)
            code = type_to_code.get(tname, 0)
            f.write(code.to_bytes(bytes_per_code, "little", signed=False))

    types_meta = {
        "recordCount": len(uuids),
        "bytesPerCode": bytes_per_code,
        "endianness": "LE",
        "types": [
            {"code": code, "typeName": code_to_type[code]}
            for code in sorted(code_to_type.keys())
        ],
    }
    dump_json(types_meta_path, types_meta)


def _repack_relations(
    src_rels_dir: Path,
    dst_rels_dir: Path,
    uuid_bytes_source: List[bytes],
    uuid_to_new_id: Dict[bytes, int],
    uuid_to_ctype: Dict[bytes, str],
    rel_types_to_include: List[str],
    require_both_endpoints: bool,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any], Dict[str, Any], int]:
    """
    Repack relations:

      - Reads <rel>.src.tgt.bin in source, interprets IDs as indices into uuid_bytes_source.
      - Keeps only edges whose endpoints are in uuid_to_new_id (subset of nodes).
      - Writes <rel>.src.tgt.bin and <rel>.tgt.src.bin in dest, with new int32 IDs
        (indices into new uuid_index).
      - Collects per-reltype and per-ctype indexes.

    Returns:
      (rel_index_entries, index_by_reltype_payload, index_by_ctype_payload, total_pairs)
    """
    dst_rels_dir.mkdir(parents=True, exist_ok=True)
    id_to_uuid = uuid_bytes_source  # index → uuid_bytes

    rel_index_entries: List[Dict[str, Any]] = []
    rel_endpoints: Dict[str, Dict[str, Set[str]]] = {}
    total_pairs = 0

    for rel in sorted(rel_types_to_include):
        src_tgt_meta_path = src_rels_dir / f"{rel}.src.tgt.meta.json"
        src_tgt_bin_path  = src_rels_dir / f"{rel}.src.tgt.bin"

        if not (src_tgt_meta_path.is_file() and src_tgt_bin_path.is_file()):
            # Nothing to repack for this reltype
            continue

        meta = load_json(src_tgt_meta_path)
        int_bytes  = int(meta.get("intBytes", 4))
        record_cnt = int(meta.get("recordCount", 0))

        if int_bytes != 4:
            raise ValueError(f"[repack] only intBytes=4 supported for relations (rel={rel})")

        pairs_src_tgt: List[Tuple[int, int]] = []
        rel_info = rel_endpoints.setdefault(rel, {
            "srcTypes": set(),
            "tgtTypes": set(),
        })

        with src_tgt_bin_path.open("rb") as f:
            for _ in range(record_cnt):
                raw = f.read(2 * INT32.size)
                if len(raw) != 2 * INT32.size:
                    break
                src_id_old, tgt_id_old = INT32.unpack_from(raw, 0), INT32.unpack_from(raw, INT32.size)
                src_id_old = src_id_old[0]
                tgt_id_old = tgt_id_old[0]

                # Map old IDs (global) -> UUID bytes
                try:
                    src_uuid = id_to_uuid[src_id_old]
                    tgt_uuid = id_to_uuid[tgt_id_old]
                except IndexError:
                    # Corrupted or out of range; skip
                    continue

                src_in = src_uuid in uuid_to_new_id
                tgt_in = tgt_uuid in uuid_to_new_id

                if require_both_endpoints:
                    if not (src_in and tgt_in):
                        continue
                else:
                    if not (src_in or tgt_in):
                        continue

                src_id_new = uuid_to_new_id[src_uuid]
                tgt_id_new = uuid_to_new_id[tgt_uuid]
                pairs_src_tgt.append((src_id_new, tgt_id_new))

                src_type = uuid_to_ctype.get(src_uuid)
                tgt_type = uuid_to_ctype.get(tgt_uuid)
                if src_type is not None:
                    rel_info["srcTypes"].add(src_type)
                if tgt_type is not None:
                    rel_info["tgtTypes"].add(tgt_type)

        if not pairs_src_tgt:
            # No edges survived filtering; skip this reltype entirely.
            continue

        # Sort pairs by src, then tgt
        pairs_src_tgt.sort(key=lambda st: (st[0], st[1]))
        record_count_new = len(pairs_src_tgt)
        total_pairs += record_count_new

        # Load human metadata if present
        rel_meta_file = src_rels_dir / f"{rel}.src.tgt.meta.json"
        rel_name_hr: Optional[str] = None
        rel_desc_hr: Optional[str] = None
        technical_name = rel
        if rel_meta_file.is_file():
            raw_meta = load_json(rel_meta_file)
            technical_name = raw_meta.get("technicalName", rel)
            rel_name_hr = raw_meta.get("name")
            rel_desc_hr = raw_meta.get("description")

        # Write src.tgt for repack
        dst_src_tgt_bin  = dst_rels_dir / f"{rel}.src.tgt.bin"
        dst_src_tgt_meta = dst_rels_dir / f"{rel}.src.tgt.meta.json"

        with dst_src_tgt_bin.open("wb") as f_bin:
            for s, t in pairs_src_tgt:
                f_bin.write(INT32.pack(s))
                f_bin.write(INT32.pack(t))

        src_tgt_meta_obj = {
            "recordCount": record_count_new,
            "intBytes": 4,
            "endianness": "LE",
            "layout": ["src", "tgt"],
            "sortedBy": ["src", "tgt"],
            "technicalName": technical_name,
            "name": rel_name_hr,
            "description": rel_desc_hr,
        }
        dump_json(dst_src_tgt_meta, src_tgt_meta_obj)

        # Write tgt.src for repack
        pairs_tgt_src = [(t, s) for s, t in pairs_src_tgt]
        pairs_tgt_src.sort(key=lambda ts: (ts[0], ts[1]))

        dst_tgt_src_bin  = dst_rels_dir / f"{rel}.tgt.src.bin"
        dst_tgt_src_meta = dst_rels_dir / f"{rel}.tgt.src.meta.json"

        with dst_tgt_src_bin.open("wb") as f_bin:
            for t, s in pairs_tgt_src:
                f_bin.write(INT32.pack(t))
                f_bin.write(INT32.pack(s))

        tgt_src_meta_obj = {
            "recordCount": record_count_new,
            "intBytes": 4,
            "endianness": "LE",
            "layout": ["tgt", "src"],
            "sortedBy": ["tgt", "src"],
            "technicalName": technical_name,
            "name": rel_name_hr,
            "description": rel_desc_hr,
        }
        dump_json(dst_tgt_src_meta, tgt_src_meta_obj)

        rel_index_entries.append({
            "technicalName": technical_name,
            "hasSrcTgt": True,
            "hasTgtSrc": True,
            "srcTgtFile": f"{rel}.src.tgt.meta.json",
            "tgtSrcFile": f"{rel}.tgt.src.meta.json",
            "recordCount": record_count_new,
        })

    # Build index_by_reltype and index_by_ctype
    index_by_reltype: Dict[str, Any] = {
        rel: {
            "srcTypes": sorted(list(info["srcTypes"])),
            "tgtTypes": sorted(list(info["tgtTypes"])),
        }
        for rel, info in rel_endpoints.items()
        if info["srcTypes"] or info["tgtTypes"]
    }

    ctype_index: Dict[str, Dict[str, List[Dict[str, str]]]] = {}
    for rel, info in rel_endpoints.items():
        src_set = info["srcTypes"]
        tgt_set = info["tgtTypes"]

        for src_type in src_set:
            for tgt_type in tgt_set:
                entry_src = ctype_index.setdefault(src_type, {
                    "asSource": [],
                    "asTarget": [],
                })
                entry_src["asSource"].append({
                    "reltype": rel,
                    "otherType": tgt_type,
                })

                entry_tgt = ctype_index.setdefault(tgt_type, {
                    "asSource": [],
                    "asTarget": [],
                })
                entry_tgt["asTarget"].append({
                    "reltype": rel,
                    "otherType": src_type,
                })

    # Sort entries for deterministic output
    for ctype, entry in ctype_index.items():
        entry["asSource"].sort(key=lambda d: (d["reltype"], d["otherType"]))
        entry["asTarget"].sort(key=lambda d: (d["reltype"], d["otherType"]))

    return rel_index_entries, index_by_reltype, ctype_index, total_pairs

def run_repack(
    *,
    source_root: Path,
    dest_dir: Path,
    profile: str = "repack",
    entity_uuids: Set[str] | None = None,
    relation_types: Set[str] | None = None,
    only_valid: bool | None = None,
) -> None:
    """
    Thin wrapper used by master_loader.

    - source_root: path to existing packed/ directory (with dict, nodes, relations, ...)
    - dest_dir: where to write the new packed/ subset
    - profile: manifest['profile']
    - entity_uuids: subset of UUID *strings* to keep (optional)
    - relation_types: subset of reltype names to keep (optional)
    - only_valid:
        * True  -> entity_validity = "valid"
        * False/None -> entity_validity = "all"
    """
    import uuid as _uuid

    # Map only_valid → entity_validity enum
    if only_valid is True:
        entity_validity = "valid"
    else:
        entity_validity = "all"

    # For now, keep all relations that match relation_types
    relation_validity = "all"

    # Convert UUID strings → bytes for uuid_allowlist
    uuid_allowlist_bytes = None
    if entity_uuids is not None:
        uuid_allowlist_bytes = {_uuid.UUID(u).bytes for u in entity_uuids}

    spec = RepackSpec(
        source_packed=source_root,
        dest_packed=dest_dir,
        entity_types=None,                     # we restrict by uuid_allowlist instead
        entity_validity=entity_validity,       # type: ignore[arg-type]
        relation_types=relation_types,         # already set or None
        relation_validity=relation_validity,   # type: ignore[arg-type]
        uuid_allowlist=uuid_allowlist_bytes,
        include_relations_if_both_endpoints_in_allowlist=True,
        profile_name=profile,
        source_dump_date=None,  # repack() will pull from manifest if present
    )
    repack(spec)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_cli() -> RepackSpec:
    parser = argparse.ArgumentParser(description="Repack a subset of an existing packed/ directory.")
    parser.add_argument(
        "--source-packed",
        type=Path,
        required=True,
        help="Path to source packed/ directory (full-streaming dump).",
    )
    parser.add_argument(
        "--dest-packed",
        type=Path,
        required=True,
        help="Path to destination packed/ directory (will be created).",
    )
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="JSON config describing entities/relations filters (simple mode).",
    )
    args = parser.parse_args()

    cfg = load_json(args.config)

    ent_cfg = cfg.get("entities", {}) or {}
    rel_cfg = cfg.get("relations", {}) or {}

    entity_types = ent_cfg.get("includeTypes")
    if entity_types:
        entity_types = set(entity_types)
    else:
        entity_types = None

    entity_validity = ent_cfg.get("validity", "all")
    if entity_validity not in ("all", "valid", "invalid"):
        raise ValueError(f"Invalid entities.validity in config: {entity_validity}")

    relation_types = rel_cfg.get("includeTypes")
    if relation_types:
        relation_types = set(relation_types)
    else:
        relation_types = None

    relation_validity = rel_cfg.get("validity", "all")
    if relation_validity not in ("all", "valid", "invalid"):
        raise ValueError(f"Invalid relations.validity in config: {relation_validity}")

    require_both = rel_cfg.get("requireBothEndpointsInSubset", True)

    return RepackSpec(
        source_packed=args.source_packed,
        dest_packed=args.dest_packed,
        entity_types=entity_types,
        entity_validity=entity_validity,          # type: ignore[arg-type]
        relation_types=relation_types,
        relation_validity=relation_validity,      # type: ignore[arg-type]
        uuid_allowlist=None,                      # CLI mode does not supply explicit UUID list
        include_relations_if_both_endpoints_in_allowlist=require_both,
    )


def main() -> None:
    spec = _parse_cli()
    repack(spec)


if __name__ == "__main__":
    main()