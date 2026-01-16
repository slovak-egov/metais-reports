# metais_dup.py
from __future__ import annotations

from collections import defaultdict, deque
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, List, Set, Tuple

from tqdm import tqdm

from json_writer import dump_json_smart

import packed_reader

# technical relation names for synthetic relations
SIMILAR_NAME_RELTYPE_KEY = "has_similar_name"
SAME_CODE_RELTYPE_KEY    = "share_same_metaid"

SIMILAR_NAME_LABEL = "Has similar name"
SAME_CODE_LABEL    = "Share a common MetaIS code"

SIMILAR_NAME_THRESHOLD   = 0.9
SIMILAR_NAME_ONLY_VALID  = True

PO_CITYPE_NAME = "PO"


def run(ctx, out_dir: Path):
    """
    New-style module entrypoint: run(ctx, out_dir).

    Responsibilities (new world):
      1) Scan packed nodes to find groups of UUIDs that share Gen_Profil_kod_metais.
      2) Build synthetic relations:
           - share_same_metaid (clique within each group),
           - has_similar_name (best fuzzy match outside the group, same citype).
      3) Compute a UUID set for repacking:
           - all primaries (duplicate nodes)
           - all 1-hop neighbors over REAL relations
      4) Request repack via ctx.request_repack(...)
      5) Write a JSON summary to out_dir/metais_dup.json
         (groups + basic synthetic relations; no heavy islands/PO view here).
    """

    store = ctx.store

    # ------------------------------------------------------------
    # STEP 0 – helpers
    # ------------------------------------------------------------
    
    def entity_valid(state_val):
        return packed_reader.interpret_meta_state(state_val)

    # ------------------------------------------------------------
    # STEP 1 – first pass: collect metais_code -> [uuid]
    #          and name candidates for fuzzy search
    # ------------------------------------------------------------
    dup_records: Dict[str, List[str]] = defaultdict(list)  # metais_code -> [uuid]
    name_candidates_by_citype: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
    all_citypes = list(store.list_types())  # or list_citypes(), adapt if needed

    for ctype in tqdm(all_citypes, desc="metais_dup: scanning nodes", leave=True):
        tv = store.open_type(ctype)  # adjust if API differs

        for uuid, attrs in tv.iter_records():  # (uuid_str, attrs_dict)
            # validity for "similar name" purposes
            state_val = attrs.get("__meta__state")
            record_is_valid = entity_valid(state_val)

            # 1a) MetaIS code
            metais_code = attrs.get("Gen_Profil_kod_metais")
            if metais_code:
                dup_records[str(metais_code)].append(uuid)

            # 1b) name candidates per citype
            if SIMILAR_NAME_ONLY_VALID and not record_is_valid:
                continue

            name_val = attrs.get("Gen_Profil_nazov")
            if not name_val:
                continue
            name_str = str(name_val).strip().lower()
            if not name_str:
                continue

            name_candidates_by_citype[ctype].append((uuid, name_str))

    # ------------------------------------------------------------
    # STEP 2 – build groups of primaries (real duplicates)
    # ------------------------------------------------------------
    groups: List[Dict] = []
    primary_uuids: Set[str] = set()

    for metais_code, uuids in dup_records.items():
        # only keep real duplicates (>= 2 entities with the same code)
        uniq = sorted(set(uuids))
        if len(uniq) <= 1:
            continue

        primary_uuids.update(uniq)

        groups.append({
            "metais_code": metais_code,
            "count":       len(uniq),
            "entity_uuids": uniq,
        })

    # sort groups by size descending
    groups.sort(key=lambda g: g["count"], reverse=True)

    # ------------------------------------------------------------
    # STEP 3 – similar-name synthetic relations (has_similar_name)
    #          For each primary, find best external match with same citype.
    # ------------------------------------------------------------
    similar_name_pairs: Set[Tuple[str, str]] = set()

    # citype for each UUID – read lazily
    uuid_to_ctype: Dict[str, str] = {}

    for ctype in all_citypes:
        tv = store.open_type(ctype)
        for uuid, _attrs in tv.iter_records():
            uuid_to_ctype[uuid] = ctype

    # precompute groups per code for quick membership checks
    code_groups_by_uuid: Dict[str, Set[str]] = defaultdict(set)
    for g in groups:
        uuids = g["entity_uuids"]
        for u in uuids:
            code_groups_by_uuid[u].add(g["metais_code"])

    # now search similar names
    for g in tqdm(groups, desc="metais_dup: similar-name search", leave=True):
        metais_code = g["metais_code"]
        group_uuids = set(g["entity_uuids"])

        for u in g["entity_uuids"]:
            ctype = uuid_to_ctype.get(u)
            if not ctype:
                continue

            # we need the name for this primary
            tv = store.open_type(ctype)
            # For efficiency, you might add a get_record(uuid) helper to PackedStore;
            # here we just scan once and cache names.
            # To keep this example simple, we build a small per-ctype cache of names.

    # For practicality, build a cache: (ctype, uuid) -> name_str
    name_cache: Dict[Tuple[str, str], str] = {}
    for ctype in all_citypes:
        tv = store.open_type(ctype)
        for uuid, attrs in tv.iter_records():
            name_val = attrs.get("Gen_Profil_nazov")
            if not name_val:
                continue
            name_str = str(name_val).strip().lower()
            if name_str:
                name_cache[(ctype, uuid)] = name_str

    # Now do the real similar-name search
    for g in tqdm(groups, desc="metais_dup: similar-name search", leave=True):
        metais_code = g["metais_code"]
        group_uuids = set(g["entity_uuids"])

        # group citypes may differ, so we just look up for each primary separately
        for u in g["entity_uuids"]:
            ctype = uuid_to_ctype.get(u)
            if not ctype:
                continue

            name_str = name_cache.get((ctype, u))
            if not name_str:
                continue

            candidates = name_candidates_by_citype.get(ctype, [])
            if not candidates:
                continue

            best_uuid = None
            best_ratio = 0.0

            for cand_uuid, cand_name in candidates:
                if cand_uuid == u:
                    continue
                # do not match with another entity from the *same* metais_code group
                if cand_uuid in group_uuids:
                    continue

                r = SequenceMatcher(None, name_str, cand_name).ratio()
                if r > best_ratio:
                    best_ratio = r
                    best_uuid = cand_uuid

            if best_uuid is not None and best_ratio >= SIMILAR_NAME_THRESHOLD:
                # canonicalize ordering for dedupe
                pair = (u, best_uuid)
                similar_name_pairs.add(pair)

    # ------------------------------------------------------------
    # STEP 4 – synthetic relations data structures
    # ------------------------------------------------------------
    synthetic_relations: Dict[str, Dict] = {}

    # 4a) share_same_metaid – full clique inside each group
    same_code_pairs: Set[Tuple[str, str]] = set()
    for g in groups:
        uuids = g["entity_uuids"]
        n = len(uuids)
        if n < 2:
            continue
        for i in range(n):
            u = uuids[i]
            for j in range(i + 1, n):
                v = uuids[j]
                if not u or not v:
                    continue
                pair = (u, v) if u < v else (v, u)
                same_code_pairs.add(pair)

    if same_code_pairs:
        synthetic_relations[SAME_CODE_RELTYPE_KEY] = {
            "technicalName": SAME_CODE_RELTYPE_KEY,
            "name":          SAME_CODE_LABEL,
            "engName":       SAME_CODE_LABEL,
            "source_type": None,
            "target_type": None,
            "pairs": [[u, v] for (u, v) in sorted(same_code_pairs)],
        }

    # 4b) has_similar_name – from similar_name_pairs
    if similar_name_pairs:
        synthetic_relations[SIMILAR_NAME_RELTYPE_KEY] = {
            "technicalName": SIMILAR_NAME_RELTYPE_KEY,
            "name":          SIMILAR_NAME_LABEL,
            "engName":       SIMILAR_NAME_LABEL,
            "source_type": None,
            "target_type": None,
            "pairs": [[u, v] for (u, v) in sorted(similar_name_pairs)],
        }

    # ------------------------------------------------------------
    # STEP 5 – figure out which UUIDs to repack
    #          (primaries + 1-hop neighbors via REAL relations)
    # ------------------------------------------------------------
    uuids_to_repack: Set[str] = set(primary_uuids)

    # Build 1-hop adjacency for *all* relations in the packed store
    rel_store = store.relations
    all_reltypes = list(rel_store.list_relation_types())

    for reltype in tqdm(all_reltypes, desc="metais_dup: building repack neighborhood", leave=True):
        rv = rel_store.open(reltype)
        for src_uuid, tgt_uuid in rv.iter_pairs():
            # if either endpoint is a primary, include both endpoints in repack set
            if src_uuid in primary_uuids or tgt_uuid in primary_uuids:
                uuids_to_repack.add(src_uuid)
                uuids_to_repack.add(tgt_uuid)

    # Note:
    #  If you want multi-hop closure instead of just 1-hop, you can:
    #    - repeat passes until no new uuids are added, or
    #    - do a BFS over adjacency.
    #  For now, we keep it to 1-hop, which is usually a good compromise.

    # ------------------------------------------------------------
    # STEP 6 – ask master_loader for a repack
    # ------------------------------------------------------------
    #   - entity_uuids: all uuids_to_repack
    #   - relation_types: None (keep all relation types)
    #   - only_valid: None (let repacker decide; or True if you want only-valid)
    ctx.request_repack(
        profile="metais_dup",
        entity_uuids=uuids_to_repack,
        relation_types=None,      # ALL relations, filtered by endpoints
        only_valid=None,          # None -> "all" in run_repack
    )

    # ------------------------------------------------------------
    # STEP 7 – write JSON summary for meta-viz
    # ------------------------------------------------------------
    out = {
        "date":     ctx.date,
        "name":     "MetaIS code duplicity",
        "count":    len(groups),
        "groups":   groups,
        "synthetic_relations": synthetic_relations,
    }

    out_path = out_dir / "metais_dup.json"
    dump_json_smart(out_path, out)

    # return value is ignored by master_loader, but we keep it for debugging
    return out