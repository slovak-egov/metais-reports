#!/usr/bin/env python3
from __future__ import annotations

import sys
import json
from pathlib import Path
from typing import Any, Dict, Tuple, Set

from tqdm import tqdm
from packed_reader import PackedStore


def load_raw_nodes(root: Path) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """
    Load all RAW node JSON files under root/nodes.

    Returns:
      {
        "KS": {
          "<uuid>": { "Gen_Profil_nazov": ..., "__meta__created": ... },
          ...
        },
        "AS": {...},
        ...
      }

    We flatten both:
      - attributes[*].(name, value)
      - metaAttributes.{key: value} -> "__meta__<key>"
    """
    nodes_dir = root / "nodes"
    if not nodes_dir.is_dir():
        print(f"[verify] ERROR: missing nodes/ under {root}", file=sys.stderr)
        return {}

    by_type: Dict[str, Dict[str, Dict[str, Any]]] = {}
    errors = 0

    paths = sorted(nodes_dir.glob("*.json"))
    for path in tqdm(paths, desc="[raw-nodes] loading", unit="file"):
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)

        if raw.get("type") != "RAW":
            print(f"[verify:nodes] ERROR {path}: unexpected top-level type={raw.get('type')}", file=sys.stderr)
            errors += 1
            continue

        for rec in raw.get("result", []):
            tname = rec.get("type", "UNKNOWN")
            uuid_str = rec.get("uuid")
            if not uuid_str:
                print(f"[verify:nodes] ERROR {path}: record without uuid", file=sys.stderr)
                errors += 1
                continue

            type_map = by_type.setdefault(tname, {})
            if uuid_str in type_map:
                print(f"[verify:nodes] ERROR duplicate UUID in raw nodes type={tname} uuid={uuid_str}", file=sys.stderr)
                errors += 1
                # keep first, ignore duplicate
                continue

            attrs: Dict[str, Any] = {}

            for attr in rec.get("attributes", []):
                name = attr.get("name")
                if name is None:
                    continue
                if name in attrs:
                    print(
                        f"[verify:nodes] WARN {path}: duplicate attribute name {name} "
                        f"for uuid={uuid_str}, overriding previous",
                        file=sys.stderr,
                    )
                attrs[name] = attr.get("value")

            for mname, mvalue in rec.get("metaAttributes", {}).items():
                full_name = f"__meta__{mname}"
                if full_name in attrs:
                    print(
                        f"[verify:nodes] WARN {path}: duplicate metaAttribute {full_name} "
                        f"for uuid={uuid_str}, overriding previous",
                        file=sys.stderr,
                    )
                attrs[full_name] = mvalue

            type_map[uuid_str] = attrs

    if errors > 0:
        print(f"[verify:nodes] Finished loading raw nodes with {errors} error(s)", file=sys.stderr)
    else:
        print("[verify:nodes] Loaded raw nodes OK")

    return by_type


def verify_nodes(root: Path, store: PackedStore, raw_nodes: Dict[str, Dict[str, Dict[str, Any]]]) -> int:
    """
    Verify:
      - type sets match (raw vs packed)
      - for each type: UUID sets match
      - per-UUID attribute dicts match
      - uuid_index / uuid_types are consistent with node types

    Returns number of errors.
    """
    errors = 0

    raw_types = set(raw_nodes.keys())
    manifest_path = store.base_dir / "manifest.json"
    if manifest_path.is_file():
        with manifest_path.open("r", encoding="utf-8") as f:
            manifest = json.load(f)
        packed_types = set(manifest.get("nodeTypes", []))
    else:
        packed_types = set(store.list_types())  # fallback

    for t in sorted(raw_types - packed_types):
        print(f"[verify:nodes] ERROR: raw type '{t}' has no packed representation", file=sys.stderr)
        errors += 1

    for t in sorted(packed_types - raw_types):
        print(f"[verify:nodes] ERROR: packed type '{t}' has no raw nodes", file=sys.stderr)
        errors += 1

    common_types = sorted(raw_types & packed_types)
    print(f"[verify:nodes] Checking {len(common_types)} common types")

    # For uuid_index / uuid_types cross-check
    all_raw_uuids: Dict[str, str] = {}  # uuid -> type

    # Progress over citypes
    for t in tqdm(common_types, desc="[nodes] citypes", unit="ctype"):
        raw_entities = raw_nodes[t]
        tv = store.open_type(t)

        # Collect packed entities for this type (with a tqdm over records)
        packed_entities: Dict[str, Dict[str, Any]] = {}
        for uuid_str, attrs in tqdm(
            tv.iter_records(),
            total=tv.record_count,
            desc=f"  [nodes:{t}] records",
            unit="rec",
            leave=False,
        ):
            if uuid_str in packed_entities:
                print(f"[verify:nodes] ERROR: duplicate UUID in packed type={t} uuid={uuid_str}", file=sys.stderr)
                errors += 1
            packed_entities[uuid_str] = attrs

        # UUID set compare
        raw_uuids = set(raw_entities.keys())
        packed_uuids = set(packed_entities.keys())

        for u in sorted(raw_uuids - packed_uuids):
            print(f"[verify:nodes] ERROR: type={t} uuid={u} present in RAW but missing in PACKED", file=sys.stderr)
            errors += 1
        for u in sorted(packed_uuids - raw_uuids):
            print(f"[verify:nodes] ERROR: type={t} uuid={u} present in PACKED but missing in RAW", file=sys.stderr)
            errors += 1

        # Attribute compare for common UUIDs (also with tqdm)
        common_uuids = raw_uuids & packed_uuids
        for u in tqdm(
            sorted(common_uuids),
            desc=f"  [nodes:{t}] attrs",
            unit="uuid",
            leave=False,
        ):
            raw_attrs = raw_entities[u]
            packed_attrs = packed_entities[u]

            raw_keys = set(raw_attrs.keys())
            packed_keys = set(packed_attrs.keys())

            for name in sorted(raw_keys - packed_keys):
                print(
                    f"[verify:nodes] ERROR: type={t} uuid={u} missing attribute in PACKED: {name}",
                    file=sys.stderr,
                )
                errors += 1

            for name in sorted(packed_keys - raw_keys):
                print(
                    f"[verify:nodes] ERROR: type={t} uuid={u} extra attribute in PACKED: {name}",
                    file=sys.stderr,
                )
                errors += 1

            for name in raw_keys & packed_keys:
                rv = raw_attrs[name]
                pv = packed_attrs[name]
                if rv != pv:
                    print(
                        f"[verify:nodes] ERROR: type={t} uuid={u} attribute '{name}' "
                        f"value mismatch RAW={rv!r} PACKED={pv!r}",
                        file=sys.stderr,
                    )
                    errors += 1

            # Gather for global uuid_index check
            all_raw_uuids[u] = t

    # ---- Global uuid_index / uuid_types vs nodes ----
    print("[verify:nodes] Cross-checking uuid_index + uuid_types vs nodes")

    uuid_index_count = store.uuid_index.record_count
    raw_unique_count = len(all_raw_uuids)
    if uuid_index_count != raw_unique_count:
        print(
            f"[verify:nodes] ERROR: uuid_index.recordCount={uuid_index_count} "
            f"!= raw unique node uuids={raw_unique_count}",
            file=sys.stderr,
        )
        errors += 1

    # tqdm over all UUIDs for global cross-check
    for u, t in tqdm(all_raw_uuids.items(), desc="[nodes] uuid_index cross-check", unit="uuid"):
        idx = store.uuid_index.get_id(u)
        if idx is None:
            print(f"[verify:nodes] ERROR: uuid_index missing UUID {u} (type={t})", file=sys.stderr)
            errors += 1
            continue

        ctype = store.get_ctype_for_uuid(u)
        if ctype != t:
            print(
                f"[verify:nodes] ERROR: uuid_types mismatch for uuid={u}: "
                f"expected type={t}, got={ctype}",
                file=sys.stderr,
            )
            errors += 1

    print(f"[verify:nodes] Node verification done with {errors} error(s)")
    return errors


def load_raw_relations(root: Path, valid_uuids: Set[str]) -> Dict[str, Set[Tuple[str, str]]]:
    """
    Load all RAW_REL JSON files under root/relations.

    Returns:
      { "PO_je_gestor_KS": { (src_uuid, tgt_uuid), ... }, ... }

    We filter out edges whose source or target uuid is not in valid_uuids,
    because the packer also dropped those.
    """
    rel_dir = root / "relations"
    rels: Dict[str, Set[Tuple[str, str]]] = {}

    if not rel_dir.is_dir():
        print(f"[verify:rels] WARNING: missing relations/ under {root}", file=sys.stderr)
        return rels

    paths = sorted(rel_dir.glob("*.json"))
    for path in tqdm(paths, desc="[raw-rels] loading", unit="file"):
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)

        if raw.get("type") != "RAW_REL":
            print(f"[verify:rels] ERROR {path}: unexpected top-level type={raw.get('type')}", file=sys.stderr)
            continue

        name = path.stem  # e.g. "PO_je_gestor_KS"
        edges: Set[Tuple[str, str]] = set()

        for rec in raw.get("result", []):
            src = rec.get("source")
            tgt = rec.get("target")
            if not src or not tgt:
                continue
            # mimic packer behavior: skip if node missing
            if src not in valid_uuids or tgt not in valid_uuids:
                continue
            edges.add((src, tgt))

        rels[name] = edges

    print(f"[verify:rels] Loaded raw relations: {len(rels)} reltype(s)")
    return rels


def verify_relations(root: Path, store: PackedStore, raw_nodes: Dict[str, Dict[str, Dict[str, Any]]]) -> int:
    """
    Verify relations:

      - raw vs packed reltype name set
      - for each reltype: edge set (src_uuid, tgt_uuid) matches
        (after filtering out edges whose nodes don't exist)
      - src.tgt and tgt.src orientations are consistent

    Returns number of errors.
    """
    errors = 0

    # Gather all valid uuids from nodes
    valid_uuids: Set[str] = set()
    for t, entities in raw_nodes.items():
        valid_uuids.update(entities.keys())

    # Load raw relations
    raw_relations = load_raw_relations(root, valid_uuids)

    packed_reltypes = set(store.relations.list_relation_types())
    raw_reltypes = set(raw_relations.keys())

    for name in sorted(raw_reltypes - packed_reltypes):
        # If no edges survived the node-filtering step, it's fine that the
        # packer skipped writing an empty file.
        if not raw_relations.get(name):
            continue
        print(f"[verify:rels] ERROR: raw relation '{name}' has no packed representation", file=sys.stderr)
        errors += 1

    for name in sorted(packed_reltypes - raw_reltypes):
        print(f"[verify:rels] ERROR: packed relation '{name}' has no raw JSON file", file=sys.stderr)
        errors += 1

    common_reltypes = sorted(raw_reltypes & packed_reltypes)
    print(f"[verify:rels] Checking {len(common_reltypes)} common relation types")

    uuid_index = store.uuid_index
    rel_map = store.relations._relations  # internal, but fine for a verifier

    # Progress over relation types
    for reltype in tqdm(common_reltypes, desc="[rels] reltypes", unit="rel"):
        raw_edges = raw_relations[reltype]

        rel_files = rel_map.get(reltype, {})
        rf_src = rel_files.get("src.tgt")
        rf_tgt = rel_files.get("tgt.src")

        if rf_src is None:
            print(f"[verify:rels] ERROR: relation '{reltype}' missing src.tgt file in packed", file=sys.stderr)
            errors += 1
            continue

        # Build packed edge set from src.tgt (UUIDs) with tqdm over ID pairs
        packed_edges: Set[Tuple[str, str]] = set()
        from packed_reader import RelationFile  # type hint only, avoids circular issues

        # we know rf_src has .record_count
        from tqdm import tqdm as _tqdm_pairs
        for src_id, tgt_id in _tqdm_pairs(
            rf_src.iter_all_pairs(),
            total=rf_src.record_count,
            desc=f"  [rels:{reltype}] pairs",
            unit="pair",
            leave=False,
        ):
            try:
                src_uuid = uuid_index.get_uuid(src_id)
                tgt_uuid = uuid_index.get_uuid(tgt_id)
            except Exception as e:
                print(
                    f"[verify:rels] ERROR: reltype={reltype} bad ID pair ({src_id}, {tgt_id}): {e}",
                    file=sys.stderr,
                )
                errors += 1
                continue
            packed_edges.add((src_uuid, tgt_uuid))

        # Compare raw vs packed edge sets
        missing_in_packed = raw_edges - packed_edges
        extra_in_packed = packed_edges - raw_edges

        for (s, t) in missing_in_packed:
            print(
                f"[verify:rels] ERROR reltype={reltype}: edge RAW-only {s} -> {t}",
                file=sys.stderr,
            )
            errors += 1
        for (s, t) in extra_in_packed:
            print(
                f"[verify:rels] ERROR reltype={reltype}: edge PACKED-only {s} -> {t}",
                file=sys.stderr,
            )
            errors += 1

        # Orientation check src.tgt vs tgt.src
        if rf_tgt is not None:
            # src.tgt gives (src_id, tgt_id)
            src_pairs = set(rf_src.iter_all_pairs())

            # tgt.src gives (tgt_id, src_id) – flip to (src_id, tgt_id)
            tgt_pairs_flipped = {(src_id, tgt_id) for (tgt_id, src_id) in rf_tgt.iter_all_pairs()}

            if src_pairs != tgt_pairs_flipped:
                print(
                    f"[verify:rels] ERROR reltype={reltype}: src.tgt and tgt.src "
                    f"do not represent the same pair set at ID level",
                    file=sys.stderr,
                )
                errors += 1

    print(f"[verify:rels] Relation verification done with {errors} error(s)")
    return errors


def main():
    if len(sys.argv) != 2:
        print(
            "Usage: verify_packed.py OUTPUT_DATE_DIR\n"
            "Example: verify_packed.py output/05-12-2025",
            file=sys.stderr,
        )
        sys.exit(1)

    root = Path(sys.argv[1]).resolve()
    if not root.is_dir():
        print(f"[verify] ERROR: root directory not found: {root}", file=sys.stderr)
        sys.exit(1)

    packed_root = root / "packed"
    if not packed_root.is_dir():
        print(f"[verify] ERROR: packed directory not found: {packed_root}", file=sys.stderr)
        sys.exit(1)

    print(f"[verify] Root:   {root}")
    print(f"[verify] Packed: {packed_root}")

    # Load packed store
    store = PackedStore(packed_root)

    # Load raw nodes
    raw_nodes = load_raw_nodes(root)

    total_errors = 0
    total_errors += verify_nodes(root, store, raw_nodes)
    total_errors += verify_relations(root, store, raw_nodes)

    store.close()

    print("\n[verify] =======================")
    print(f"[verify] TOTAL ERRORS: {total_errors}")
    print("[verify] =======================")

    if total_errors > 0:
        sys.exit(2)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()