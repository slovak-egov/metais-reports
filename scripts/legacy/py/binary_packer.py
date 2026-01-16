#!/usr/bin/env python3
import sys
import json
from pathlib import Path
import argparse
import struct
import os
from datetime import datetime
from collections import defaultdict

# Sentinel for "missing value"
MISSING_SENTINEL = -1


def get_rss_bytes() -> int:
    with open("/proc/self/statm", "r") as f:
        fields = f.read().split()
    resident_pages = int(fields[1])
    page_size = os.sysconf("SC_PAGE_SIZE")
    return resident_pages * page_size


def canonical_value_repr(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def uuid_to_bytes(u: str) -> bytes:
    if not u:
        return b"\x00" * 16
    return bytes.fromhex(u.replace("-", ""))


# ---------- Node packing ----------

def discover_node_files(root: Path) -> list[Path]:
    nodes_dir = root / "nodes"
    if not nodes_dir.is_dir():
        print(f"[pack] No nodes/ directory under {root}", file=sys.stderr)
        return []
    return sorted(nodes_dir.glob("*.json"))


def discover_relation_files(root: Path) -> list[Path]:
    rel_dir = root / "relations"
    if not rel_dir.is_dir():
        print(f"[pack] No relations/ directory under {root}", file=sys.stderr)
        return []
    return sorted(rel_dir.glob("*.json"))


def load_raw_file(path: Path) -> dict:
    print(f"[pack] Loading {path} ...")
    with path.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    if raw.get("type") != "RAW" or "result" not in raw:
        raise ValueError(f"{path} does not look like a RAW dump")
    return raw


def load_raw_rel_file(path: Path) -> dict:
    print(f"[rel-pack] Loading {path} ...")
    with path.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    if raw.get("type") != "RAW_REL" or "result" not in raw:
        raise ValueError(f"{path} does not look like a RAW_REL dump")
    return raw

def load_node_attribute_metadata(metadata_nodes_dir: Path, type_name: str) -> dict[str, tuple[str | None, str | None]]:
    """
    Load attribute metadata for a given citype (e.g. 'AS') from

      metadata/nodes/<type_name>.json

    and return:
      { technicalName: (name, description) }

    We merge:
      - top-level "attributes"
      - each attributeProfiles[].attributes
    """
    meta_file = metadata_nodes_dir / f"{type_name}.json"
    if not meta_file.is_file():
        print(f"[meta] No node metadata file for type {type_name}: {meta_file}", file=sys.stderr)
        return {}

    with meta_file.open("r", encoding="utf-8") as f:
        raw = json.load(f)

    mapping: dict[str, tuple[str | None, str | None]] = {}

    # top-level attributes
    for attr in raw.get("attributes", []):
        tech = attr.get("technicalName")
        if not tech:
            continue
        name = attr.get("name")
        desc = attr.get("description")
        mapping[tech] = (name, desc)

    # attributeProfiles[*].attributes
    for prof in raw.get("attributeProfiles", []):
        for attr in prof.get("attributes", []):
            tech = attr.get("technicalName")
            if not tech:
                continue
            # don't overwrite if already present
            if tech in mapping:
                continue
            name = attr.get("name")
            desc = attr.get("description")
            mapping[tech] = (name, desc)

    return mapping

def group_node_files_by_type(files: list[Path]) -> dict[str, list[Path]]:
    grouped: dict[str, list[Path]] = defaultdict(list)
    for path in files:
        raw = load_raw_file(path)
        result = raw.get("result", [])
        if not result:
            print(f"[warn] Node file {path} has empty result[]; skipping", file=sys.stderr)
            continue
        type_name = result[0].get("type", "UNKNOWN")
        grouped[type_name].append(path)
    return grouped


def process_node_type(
    type_name: str,
    files_for_type: list[Path],
    nodes_packed_dir: Path,
    global_key_to_index: dict,
    global_values: list,
    mem_stats: dict,
    global_uuid_list: list[bytes],
    global_uuid_to_ctype: dict[bytes, str],
    metadata_nodes_dir: Path,
):
    """
    Process node type (e.g. 'KS'):

      - writes <type>.bin / <type>.meta.json / <type>.uuids.bin
      - appends each UUID bytes to global_uuid_list for later global ID indexing
    """
    print(f"\n=== [type {type_name}] processing {len(files_for_type)} file(s) ===")

    attr_order: list[str] = []
    attr_pos: dict[str, int] = {}

    records: list[tuple[bytes, dict[str, int]]] = []
    total_records = 0

    for path in files_for_type:
        raw = load_raw_file(path)
        result = raw["result"]

        for rec in result:
            total_records += 1
            u_bytes = uuid_to_bytes(rec.get("uuid", ""))

            # remember which ctype this uuid belongs to
            global_uuid_to_ctype[u_bytes] = type_name

            rec_map: dict[str, int] = {}

            # normal attributes
            for attr in rec.get("attributes", []):
                name = attr["name"]
                if name not in attr_pos:
                    attr_pos[name] = len(attr_order)
                    attr_order.append(name)

                raw_value = attr.get("value")
                key = canonical_value_repr(raw_value)
                idx = global_key_to_index.get(key)
                if idx is None:
                    idx = len(global_values)
                    global_values.append(raw_value)
                    global_key_to_index[key] = idx
                rec_map[name] = idx

            # metaAttributes – prefix to avoid collision
            meta = rec.get("metaAttributes", {})
            for mname, mvalue in meta.items():
                full_name = f"__meta__{mname}"
                if full_name not in attr_pos:
                    attr_pos[full_name] = len(attr_order)
                    attr_order.append(full_name)

                raw_value = mvalue
                key = canonical_value_repr(raw_value)
                idx = global_key_to_index.get(key)
                if idx is None:
                    idx = len(global_values)
                    global_values.append(raw_value)
                    global_key_to_index[key] = idx
                rec_map[full_name] = idx

            records.append((u_bytes, rec_map))

            mem_stats["recordsSeen"] += 1
            if mem_stats["recordsSeen"] % mem_stats["sampleEvery"] == 0:
                rss = get_rss_bytes()
                mem_stats["sampleCount"] += 1
                mem_stats["rssSumBytes"] += rss
                if rss > mem_stats["rssPeakBytes"]:
                    mem_stats["rssPeakBytes"] = rss

    print(f"[type {type_name}] Total records: {total_records}")
    print(f"[type {type_name}] Unique attributes (incl. meta): {len(attr_order)}")

    print(f"[type {type_name}] Sorting records by UUID ...")
    records.sort(key=lambda pair: pair[0])

    nodes_packed_dir.mkdir(parents=True, exist_ok=True)
    bin_path   = nodes_packed_dir / f"{type_name}.bin"
    meta_path  = nodes_packed_dir / f"{type_name}.meta.json"
    uuids_path = nodes_packed_dir / f"{type_name}.uuids.bin"

    block_size = len(attr_order)
    int_bytes = 4

    print(f"[type {type_name}] Writing binary blocks -> {bin_path}")
    with bin_path.open("wb") as fbin:
        pack_int = struct.Struct("<i").pack
        for u_bytes, rec_map in records:
            for name in attr_order:
                idx = rec_map.get(name, MISSING_SENTINEL)
                fbin.write(pack_int(idx))

    bin_size = bin_path.stat().st_size

    # --- build enriched attribute metadata ---
    # We store as:
    #   "attributes": [
    #     [technicalName, humanName, description],
    #     ...
    #   ]
    attr_meta_map = load_node_attribute_metadata(metadata_nodes_dir, type_name)

    attributes_serialized: list[list[str | None]] = []
    for tech in attr_order:
        # technical name
        human_name: str | None = None
        desc: str | None = None

        if tech.startswith("__meta__"):
            # our synthetic metaAttributes
            human_name = tech  # or tech[len("__meta__"):] if you want shorter
            desc = None
        else:
            if tech in attr_meta_map:
                human_name, desc = attr_meta_map[tech]

        attributes_serialized.append([tech, human_name, desc])

    meta = {
        "recordCount": total_records,
        "blockSize": block_size,
        "intBytes": int_bytes,
        "endianness": "LE",
        "missingSentinel": MISSING_SENTINEL,
        "attributes": attributes_serialized,   # 👈 now triple arrays
        "sortedBy": "uuid",
        "typeName": type_name,
    }
    print(f"[type {type_name}] Writing meta -> {meta_path}")
    with meta_path.open("w", encoding="utf-8") as fmeta:
        json.dump(meta, fmeta, ensure_ascii=False, indent=2)

    meta_size = meta_path.stat().st_size

    print(f"[type {type_name}] Writing UUIDs -> {uuids_path}")
    with uuids_path.open("wb") as fu:
        for u_bytes, _ in records:
            fu.write(u_bytes)
            global_uuid_list.append(u_bytes)
            global_uuid_to_ctype.setdefault(u_bytes, type_name)
            # For global index we track type by raw UUID bytes
            prev = global_uuid_to_ctype.get(u_bytes)
            if prev is None:
                global_uuid_to_ctype[u_bytes] = type_name
            elif prev != type_name:
                # sanity check – should not happen if UUIDs are globally unique
                print(
                    f"[warn] UUID appears with two types: {prev} and {type_name}",
                    file=sys.stderr,
                )

    uuids_bytes = uuids_path.stat().st_size

    print(f"[type {type_name}]   Binary data: {bin_size / (1024*1024):.2f} MiB")
    print(f"[type {type_name}]   UUIDs bin  : {uuids_bytes / (1024*1024):.2f} MiB")
    print(f"[type {type_name}]   Meta JSON  : {meta_size / (1024*1024):.2f} MiB")

    return {
        "type": type_name,
        "recordCount": total_records,
        "binSize": bin_size,
        "uuidsSize": uuids_bytes,
        "metaSize": meta_size,
        "blockSize": block_size,
    }


def write_global_dict(global_values, dict_dir: Path):
    dict_dir.mkdir(parents=True, exist_ok=True)
    values_path  = dict_dir / "dict.values.bin"
    offsets_path = dict_dir / "dict.offsets.bin"
    meta_path    = dict_dir / "dict.meta.json"

    print("\n[dict] Writing global dictionary:")
    print(f"[dict]   values  -> {values_path}")
    print(f"[dict]   offsets -> {offsets_path}")
    print(f"[dict]   meta    -> {meta_path}")

    offsets = [0]

    with values_path.open("wb") as f_val:
        for v in global_values:
            s = json.dumps(v, ensure_ascii=False, separators=(",", ":"))
            b = s.encode("utf-8")
            f_val.write(b)
            offsets.append(offsets[-1] + len(b))

    values_size = values_path.stat().st_size

    with offsets_path.open("wb") as f_off:
        for off in offsets:
            f_off.write(struct.pack("<Q", off))

    offsets_size = offsets_path.stat().st_size

    meta = {
        "valueCount": len(global_values),
        "offsetByteSize": 8,
        "encoding": "utf-8",
        "format": "json",
        "endianness": "LE",
    }
    with meta_path.open("w", encoding="utf-8") as f_meta:
        json.dump(meta, f_meta, ensure_ascii=False, indent=2)

    meta_size = meta_path.stat().st_size

    print(f"[dict]   Values size : {values_size / (1024*1024):.2f} MiB")
    print(f"[dict]   Offsets size: {offsets_size / (1024*1024):.2f} MiB")
    print(f"[dict]   Meta size   : {meta_size / (1024*1024):.2f} MiB")
    print(f"[dict]   Entries     : {len(global_values)}")

    return {
        "valuesSize": values_size,
        "offsetsSize": offsets_size,
        "metaSize": meta_size,
        "valueCount": len(global_values),
    }


# ---------- Global UUID index (for relations) ----------

def build_global_uuid_index(
    global_uuid_list: list[bytes],
    global_uuid_to_ctype: dict[bytes, str],
    uuid_index_dir: Path,
    uuid_types_dir: Path,
):
    """
    Build:
      - uuid_index/uuids.bin + meta.json   (sorted unique UUID bytes)
      - uuid_types/types.bin + meta.json   (parallel type codes for each UUID)

    Returns:
      (uuid_to_id, uuid_index_stats, uuid_types_stats)
    """
    print("\n[uuid-index] Building global UUID index ...")

    # Deduplicate and sort
    unique = sorted(set(global_uuid_list))
    count = len(unique)
    print(f"[uuid-index] Unique UUIDs: {count}")

    uuid_index_dir.mkdir(parents=True, exist_ok=True)
    uuids_path = uuid_index_dir / "uuids.bin"
    meta_path  = uuid_index_dir / "meta.json"

    # Write uuids.bin
    with uuids_path.open("wb") as f:
        for b in unique:
            f.write(b)

    # Write uuid_index meta
    with meta_path.open("w", encoding="utf-8") as f:
        json.dump(
            {
                "recordCount": count,
                "uuidBytes": 16,
                "endianness": "LE",
                "sortedBy": "uuid_bytes",
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    print(f"[uuid-index] uuids.bin size: {uuids_path.stat().st_size / (1024*1024):.2f} MiB")

    # Build lookup dict for relation packing: uuid_bytes -> int32 id
    uuid_to_id: dict[bytes, int] = {b: i for i, b in enumerate(unique)}

    uuid_index_stats = {
        "count": count,
        "uuidsSize": uuids_path.stat().st_size,
        "metaSize": meta_path.stat().st_size,
    }

    # ------------------------------
    # Build uuid_types/*
    # ------------------------------
    uuid_types_dir.mkdir(parents=True, exist_ok=True)
    types_bin_path  = uuid_types_dir / "types.bin"
    types_meta_path = uuid_types_dir / "meta.json"

    # Collect all citypes
    all_types = sorted(set(global_uuid_to_ctype.values()))
    type_count = len(all_types)

    # For now, fix bytesPerCode = 2 (uint16), and assert it's enough
    bytes_per_code = 2
    if type_count > 2**(8 * bytes_per_code):
        raise ValueError(
            f"Too many types ({type_count}) for bytesPerCode={bytes_per_code}"
        )

    # Map typeName -> code
    type_to_code = {t: i for i, t in enumerate(all_types)}
    code_to_type = {i: t for t, i in type_to_code.items()}

    pack_code = struct.Struct("<H").pack  # uint16 LE

    print(f"[uuid-types] Writing types.bin for {count} UUIDs, {type_count} types")

    with types_bin_path.open("wb") as f:
        for b in unique:
            tname = global_uuid_to_ctype.get(b)
            if tname is None:
                # Shouldn't happen, but be robust
                code = 0
            else:
                code = type_to_code[tname]
            f.write(pack_code(code))

    # Write uuid_types meta
    types_meta = {
        "recordCount": count,
        "bytesPerCode": bytes_per_code,
        "endianness": "LE",
        "types": [
            {"code": code, "typeName": code_to_type[code]}
            for code in sorted(code_to_type.keys())
        ],
    }
    with types_meta_path.open("w", encoding="utf-8") as f:
        json.dump(types_meta, f, ensure_ascii=False, indent=2)

    uuid_types_stats = {
        "typesSize": types_bin_path.stat().st_size,
        "metaSize": types_meta_path.stat().st_size,
        "typeCount": type_count,
    }

    print(
        f"[uuid-types] types.bin size: {types_bin_path.stat().st_size / (1024*1024):.2f} MiB "
        f"({type_count} distinct types)"
    )

    return uuid_to_id, uuid_index_stats, uuid_types_stats


# ---------- Relation packing (using global UUID -> ID) ----------

def pack_relations(
    root: Path,
    rel_files: list[Path],
    uuid_to_id: dict[bytes, int],
    rel_packed_dir: Path,
    global_uuid_to_ctype: dict[bytes, str],
):
    rel_packed_dir.mkdir(parents=True, exist_ok=True)
    stats_list = []
    pack_i32 = struct.Struct("<i").pack

    # reltype -> {"srcTypes": set(str), "tgtTypes": set(str)}
    rel_endpoints: dict[str, dict[str, set[str]]] = {}

    for path in rel_files:
        raw = load_raw_rel_file(path)
        result = raw["result"]

        pairs_src_tgt: list[tuple[int, int]] = []
        stem = path.stem  # e.g. "PO_je_gestor_KS"

        # Ensure rel_endpoints entry exists for this relation
        rel_info = rel_endpoints.setdefault(stem, {
            "srcTypes": set(),
            "tgtTypes": set(),
        })

        for rec in result:
            src_b = uuid_to_bytes(rec.get("source", ""))
            tgt_b = uuid_to_bytes(rec.get("target", ""))

            # Determine integer IDs for packed relations
            try:
                src_id = uuid_to_id[src_b]
                tgt_id = uuid_to_id[tgt_b]
            except KeyError:
                # Node not present among packed nodes; skip this edge.
                continue

            pairs_src_tgt.append((src_id, tgt_id))

            # NEW: track citype of src/tgt for this reltype, based on the *real* data
            src_type = global_uuid_to_ctype.get(src_b)
            tgt_type = global_uuid_to_ctype.get(tgt_b)
            if src_type is not None:
                rel_info["srcTypes"].add(src_type)
            if tgt_type is not None:
                rel_info["tgtTypes"].add(tgt_type)

        record_count = len(pairs_src_tgt)
        if record_count == 0:
            print(f"[rel-pack] {path.name}: 0 edges after filtering; skipping")
            continue

        print(f"[rel-pack] {path.name}: {record_count} edges")

        # --- src.tgt ---
        pairs_src_tgt.sort(key=lambda st: (st[0], st[1]))
        stem = path.stem  # e.g. "PO_je_gestor_KS"

        # --- load human-readable relation meta, if available ---
        rel_meta_file = root / "metadata" / "relations" / f"{stem}.json"
        rel_name_hr: str | None = None
        rel_desc_hr: str | None = None
        if rel_meta_file.is_file():
            with rel_meta_file.open("r", encoding="utf-8") as f:
                rel_meta_raw = json.load(f)
            rel_name_hr = rel_meta_raw.get("name")
            rel_desc_hr = rel_meta_raw.get("description")
        else:
            print(f"[rel-pack] No metadata for relation {stem}: {rel_meta_file}", file=sys.stderr)

        src_tgt_bin = rel_packed_dir / f"{stem}.src.tgt.bin"
        src_tgt_meta = rel_packed_dir / f"{stem}.src.tgt.meta.json"

        with src_tgt_bin.open("wb") as fbin:
            for s, t in pairs_src_tgt:
                fbin.write(pack_i32(s))
                fbin.write(pack_i32(t))

        src_tgt_size = src_tgt_bin.stat().st_size

        with src_tgt_meta.open("w", encoding="utf-8") as fmeta:
            json.dump(
                {
                    "recordCount": record_count,
                    "intBytes": 4,
                    "endianness": "LE",
                    "layout": ["src", "tgt"],
                    "sortedBy": ["src", "tgt"],
                    "technicalName": stem,
                    "name": rel_name_hr,
                    "description": rel_desc_hr,
                },
                fmeta,
                ensure_ascii=False,
                indent=2,
            )

        # --- tgt.src ---
        pairs_tgt_src = [(t, s) for s, t in pairs_src_tgt]
        pairs_tgt_src.sort(key=lambda ts: (ts[0], ts[1]))

        tgt_src_bin = rel_packed_dir / f"{stem}.tgt.src.bin"
        tgt_src_meta = rel_packed_dir / f"{stem}.tgt.src.meta.json"

        with tgt_src_bin.open("wb") as fbin:
            for t, s in pairs_tgt_src:
                fbin.write(pack_i32(t))
                fbin.write(pack_i32(s))

        tgt_src_size = tgt_src_bin.stat().st_size

        with tgt_src_meta.open("w", encoding="utf-8") as fmeta:
            json.dump(
                {
                    "recordCount": record_count,
                    "intBytes": 4,
                    "endianness": "LE",
                    "layout": ["tgt", "src"],
                    "sortedBy": ["tgt", "src"],
                    "technicalName": stem,
                    "name": rel_name_hr,
                    "description": rel_desc_hr,
                },
                fmeta,
                ensure_ascii=False,
                indent=2,
            )

        stats_list.append(
            {
                "name": stem,
                "recordCount": record_count,
                "srcTgtSize": src_tgt_size,
                "tgtSrcSize": tgt_src_size,
            }
        )

    print("\n=== Relation summary ===")
    for st in stats_list:
        print(
            f"- {st['name']}: {st['recordCount']} edges, "
            f"src.tgt={st['srcTgtSize'] / (1024*1024):.2f} MiB, "
            f"tgt.src={st['tgtSrcSize'] / (1024*1024):.2f} MiB"
        )

    # --- NEW: write helper indexes ---

    # 1) Per-reltype overview: which citypes appear on src/tgt side
    reltype_index_path = rel_packed_dir / "index_by_reltype.json"
    reltype_index_payload = {
        rel: {
            "srcTypes": sorted(list(info["srcTypes"])),
            "tgtTypes": sorted(list(info["tgtTypes"])),
        }
        for rel, info in rel_endpoints.items()
    }
    with reltype_index_path.open("w", encoding="utf-8") as f:
        json.dump(reltype_index_payload, f, ensure_ascii=False, indent=2)
    print(f"[rel-pack] Wrote per-reltype index -> {reltype_index_path}")

    # 2) Per-citype index: for each ctype, which relations it participates in
    ctype_index: dict[str, dict[str, list[dict]]] = {}
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

    ctype_index_path = rel_packed_dir / "index_by_ctype.json"
    with ctype_index_path.open("w", encoding="utf-8") as f:
        json.dump(ctype_index, f, ensure_ascii=False, indent=2)
    print(f"[rel-pack] Wrote per-ctype index -> {ctype_index_path}")

    return stats_list

# build the manifest file and index for entities and relations
def write_packed_manifest(
    packed_dir: Path,
    dump_date_str: str,
    profile: str,
    filters: dict | None = None,
) -> None:
    if filters is None:
        filters = {}

    nodes_dir = packed_dir / "nodes"
    rels_dir  = packed_dir / "relations"

    # ---- collect node types from actual meta files ----
    node_types: list[dict] = []
    nodes_total = 0
    if nodes_dir.is_dir():
        for meta_path in nodes_dir.glob("*.meta.json"):
            name = meta_path.name            # e.g. "AS.meta.json"
            if not name.endswith(".meta.json"):
                continue
            tname = name[:-len(".meta.json")]  # "AS"

            with meta_path.open("r", encoding="utf-8") as f:
                meta = json.load(f)

            rc = int(meta.get("recordCount", 0))
            nodes_total += rc

            node_types.append({
                "typeName": tname,
                "recordCount": rc,
                "metaFile": name,
                "binFile":  f"{tname}.bin",
                "uuidsFile": f"{tname}.uuids.bin",
            })

    node_type_names = sorted({t["typeName"] for t in node_types})

    # ---- collect relation types from .meta.json ----
    rel_types: list[dict] = []
    rel_pairs_total = 0
    if rels_dir.is_dir():
        for meta_path in rels_dir.glob("*.meta.json"):
            name = meta_path.name  # e.g. "PO_je_gestor_KS.src.tgt.meta.json"
            stem = name[:-len(".meta.json")]  # "PO_je_gestor_KS.src.tgt"

            if stem.endswith(".src.tgt"):
                relname = stem[:-len(".src.tgt")]
                kind    = "src.tgt"
            elif stem.endswith(".tgt.src"):
                relname = stem[:-len(".tgt.src")]
                kind    = "tgt.src"
            else:
                continue

            with meta_path.open("r", encoding="utf-8") as f:
                meta = json.load(f)
            rc = int(meta.get("recordCount", 0))
            rel_pairs_total += rc

            # aggregate info per reltype
            entry = next((r for r in rel_types if r["technicalName"] == relname), None)
            if entry is None:
                entry = {
                    "technicalName": relname,
                    "hasSrcTgt": False,
                    "hasTgtSrc": False,
                    "srcTgtFile": None,
                    "tgtSrcFile": None,
                    "recordCount": 0
                }
                rel_types.append(entry)

            if kind == "src.tgt":
                entry["hasSrcTgt"] = True
                entry["srcTgtFile"] = name
            else:
                entry["hasTgtSrc"] = True
                entry["tgtSrcFile"] = name

            # store max just in case they differ slightly
            entry["recordCount"] = max(entry["recordCount"], rc)

    rel_type_names = sorted({r["technicalName"] for r in rel_types})

    manifest = {
        "version": 1,
        "createdAt": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "sourceDumpDate": dump_date_str,
        "profile": profile,
        "filters": filters,
        "nodeTypes": node_type_names,
        "relationTypes": rel_type_names,
        "counts": {
            "nodesTotal": nodes_total,
            "relationPairsTotal": rel_pairs_total,
        },
    }

    # write global manifest
    manifest_path = packed_dir / "manifest.json"
    with manifest_path.open("w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    # write nodes/index.json
    if node_types:
        nodes_index_path = nodes_dir / "index.json"
        with nodes_index_path.open("w", encoding="utf-8") as f:
            json.dump({"types": node_types}, f, ensure_ascii=False, indent=2)

    # write relations/index.json
    if rel_types:
        rels_index_path = rels_dir / "index.json"
        with rels_index_path.open("w", encoding="utf-8") as f:
            json.dump({"relationTypes": rel_types}, f, ensure_ascii=False, indent=2)


# ---------- Main ----------

def main():
    ap = argparse.ArgumentParser(
        description=(
            "Pack RAW node + relation JSON files under a root, e.g.\n"
            "  output/05-12-2025/\n\n"
            "Produces:\n"
            "  packed/dict/...\n"
            "  packed/nodes/...\n"
            "  packed/uuid_index/...\n"
            "  packed/relations/...\n"
        )
    )
    ap.add_argument(
        "root",
        help="Root directory containing nodes/, relations/, metadata/",
    )
    args = ap.parse_args()

    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"[error] Root directory not found: {root}", file=sys.stderr)
        sys.exit(1)

    date_str = root.name

    node_files = discover_node_files(root)
    rel_files = discover_relation_files(root)

    print(f"[pack] Found {len(node_files)} node JSON file(s)")
    print(f"[pack] Found {len(rel_files)} relation JSON file(s)")

    orig_total_bytes = sum(f.stat().st_size for f in node_files + rel_files)

    packed_root = root / "packed"
    nodes_packed_dir = packed_root / "nodes"
    dict_dir = packed_root / "dict"
    uuid_index_dir = packed_root / "uuid_index"
    rel_packed_dir = packed_root / "relations"
    metadata_nodes_dir = root / "metadata" / "nodes"
    uuid_types_dir = packed_root / "uuid_types"

    print(f"[pack] Packed output root: {packed_root}")
    print(f"[pack]   Nodes      -> {nodes_packed_dir}")
    print(f"[pack]   Dict       -> {dict_dir}")
    print(f"[pack]   UUID index -> {uuid_index_dir}")
    print(f"[pack]   Relations  -> {rel_packed_dir}")

    # --- NODE PACKING ---

    grouped_nodes = group_node_files_by_type(node_files)

    global_key_to_index: dict[str, int] = {}
    global_values: list[object] = []

    mem_stats = {
        "recordsSeen": 0,
        "sampleEvery": 2000,
        "sampleCount": 0,
        "rssSumBytes": 0,
        "rssPeakBytes": 0,
    }

    type_stats = []
    new_total_bytes = 0
    global_uuid_list: list[bytes] = []
    global_uuid_to_ctype: dict[bytes, str] = {}

    for type_name, files_for_type in grouped_nodes.items():
        st = process_node_type(
            type_name=type_name,
            files_for_type=files_for_type,
            nodes_packed_dir=nodes_packed_dir,
            global_key_to_index=global_key_to_index,
            global_values=global_values,
            mem_stats=mem_stats,
            global_uuid_list=global_uuid_list,
            global_uuid_to_ctype=global_uuid_to_ctype,
            metadata_nodes_dir=metadata_nodes_dir,
        )
        type_stats.append(st)
        new_total_bytes += st["binSize"] + st["uuidsSize"] + st["metaSize"]

    dict_stats = write_global_dict(global_values, dict_dir)
    new_total_bytes += (
        dict_stats["valuesSize"]
        + dict_stats["offsetsSize"]
        + dict_stats["metaSize"]
    )

    uuid_to_id, uuid_index_stats, uuid_types_stats = build_global_uuid_index(
        global_uuid_list=global_uuid_list,
        global_uuid_to_ctype=global_uuid_to_ctype,
        uuid_index_dir=uuid_index_dir,
        uuid_types_dir=uuid_types_dir,
    )
    new_total_bytes += uuid_index_stats["uuidsSize"] + uuid_index_stats["metaSize"]
    new_total_bytes += uuid_types_stats["typesSize"] + uuid_types_stats["metaSize"]

    # --- RELATION PACKING ---

    rel_stats = []
    if rel_files:
        rel_stats = pack_relations(
            root=root,
            rel_files=rel_files,
            uuid_to_id=uuid_to_id,
            rel_packed_dir=rel_packed_dir,
            global_uuid_to_ctype=global_uuid_to_ctype,
        )

        for st in rel_stats:
            new_total_bytes += st["srcTgtSize"] + st["tgtSrcSize"]

    write_packed_manifest(
        packed_dir=packed_root,
        dump_date_str=date_str,
        profile="full",              # or "web-lite" later
        filters={
            "onlyValid": False,
            "includedTypes": None,
        },
    )

    # --- SUMMARY ---

    print("\n=== Summary per node type ===")
    for st in type_stats:
        print(
            f"- {st['type']}: "
            f"{st['recordCount']} rec, "
            f"block={st['blockSize']}, "
            f"bin={st['binSize'] / (1024*1024):.2f} MiB, "
            f"uuids={st['uuidsSize'] / (1024*1024):.2f} MiB, "
            f"meta={st['metaSize'] / (1024*1024):.2f} MiB"
        )

    print("\n=== Overall size stats ===")
    print(f"[overall] Original total JSON: {orig_total_bytes / (1024*1024):.2f} MiB")
    print(f"[overall] New total packed   : {new_total_bytes / (1024*1024):.2f} MiB")
    if orig_total_bytes > 0:
        ratio = new_total_bytes / orig_total_bytes
        print(f"[overall] Ratio (new/orig)  : {ratio:.3f}")
    else:
        print("[overall] Original total size is 0, cannot compute ratio")

    print("\n=== Overall memory stats ===")
    if mem_stats["sampleCount"] > 0:
        avg_rss = mem_stats["rssSumBytes"] / mem_stats["sampleCount"]
        peak_rss = mem_stats["rssPeakBytes"]
        print(f"[mem] Average RSS during run: {avg_rss / (1024*1024):.2f} MiB")
        print(f"[mem] Peak RSS during run   : {peak_rss / (1024*1024):.2f} MiB")
    else:
        print("[mem] No memory samples collected (too few records?)")

    print("\n[pack] Done.")


if __name__ == "__main__":
    main()