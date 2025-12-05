#!/usr/bin/env python3
import sys
import json
from pathlib import Path
import argparse
import struct
import os
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

    meta = {
        "recordCount": total_records,
        "blockSize": block_size,
        "intBytes": int_bytes,
        "endianness": "LE",
        "missingSentinel": MISSING_SENTINEL,
        "attributes": attr_order,
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
            # For global index we just append; we'll sort later
            global_uuid_list.append(u_bytes)

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

def build_global_uuid_index(global_uuid_list: list[bytes], uuid_index_dir: Path):
    """
    global_uuid_list: raw UUID bytes seen across ALL node types (possibly with duplicates)
    We:
      - deduplicate
      - sort
      - assign global int32 IDs
      - write uuids.bin (16 bytes * N) and meta.json
      - return a dict uuid_bytes -> id (for relation packing)
    """
    print("\n[uuid-index] Building global UUID index ...")

    # Deduplicate
    unique = sorted(set(global_uuid_list))
    count = len(unique)
    print(f"[uuid-index] Unique UUIDs: {count}")

    uuid_index_dir.mkdir(parents=True, exist_ok=True)
    uuids_path = uuid_index_dir / "uuids.bin"
    meta_path  = uuid_index_dir / "meta.json"

    with uuids_path.open("wb") as f:
        for b in unique:
            f.write(b)

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
    uuid_to_id: dict[bytes, int] = {}
    for i, b in enumerate(unique):
        uuid_to_id[b] = i

    return uuid_to_id, {
        "count": count,
        "uuidsSize": uuids_path.stat().st_size,
        "metaSize": meta_path.stat().st_size,
    }


# ---------- Relation packing (using global UUID -> ID) ----------

def pack_relations(
    root: Path,
    rel_files: list[Path],
    uuid_to_id: dict[bytes, int],
    rel_packed_dir: Path,
):
    rel_packed_dir.mkdir(parents=True, exist_ok=True)
    stats_list = []
    pack_i32 = struct.Struct("<i").pack

    for path in rel_files:
        raw = load_raw_rel_file(path)
        result = raw["result"]

        pairs_src_tgt: list[tuple[int, int]] = []
        for rec in result:
            src_b = uuid_to_bytes(rec.get("source", ""))
            tgt_b = uuid_to_bytes(rec.get("target", ""))

            try:
                src_id = uuid_to_id[src_b]
                tgt_id = uuid_to_id[tgt_b]
            except KeyError:
                # Node not present among packed nodes; skip this edge.
                # You can also log if you want.
                continue

            pairs_src_tgt.append((src_id, tgt_id))

        record_count = len(pairs_src_tgt)
        if record_count == 0:
            print(f"[rel-pack] {path.name}: 0 edges after filtering; skipping")
            continue

        print(f"[rel-pack] {path.name}: {record_count} edges")

        # --- src.tgt ---
        pairs_src_tgt.sort(key=lambda st: (st[0], st[1]))
        stem = path.stem  # e.g. "PO_je_poskytovatelom_KS"

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
                    "sourceFile": str(path),
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
                    "sourceFile": str(path),
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

    return stats_list


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

    for type_name, files_for_type in grouped_nodes.items():
        st = process_node_type(
            type_name=type_name,
            files_for_type=files_for_type,
            nodes_packed_dir=nodes_packed_dir,
            global_key_to_index=global_key_to_index,
            global_values=global_values,
            mem_stats=mem_stats,
            global_uuid_list=global_uuid_list,
        )
        type_stats.append(st)
        new_total_bytes += st["binSize"] + st["uuidsSize"] + st["metaSize"]

    dict_stats = write_global_dict(global_values, dict_dir)
    new_total_bytes += (
        dict_stats["valuesSize"]
        + dict_stats["offsetsSize"]
        + dict_stats["metaSize"]
    )

    uuid_to_id, uuid_index_stats = build_global_uuid_index(global_uuid_list, uuid_index_dir)
    new_total_bytes += uuid_index_stats["uuidsSize"] + uuid_index_stats["metaSize"]

    # --- RELATION PACKING ---

    rel_stats = []
    if rel_files:
        rel_stats = pack_relations(
            root=root,
            rel_files=rel_files,
            uuid_to_id=uuid_to_id,
            rel_packed_dir=rel_packed_dir,
        )

        for st in rel_stats:
            new_total_bytes += st["srcTgtSize"] + st["tgtSrcSize"]

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