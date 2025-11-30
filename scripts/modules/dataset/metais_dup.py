from collections import defaultdict, deque
from difflib import SequenceMatcher
from tqdm import tqdm

SIMILAR_NAME_RELTYPE   = "Has similar name"
SIMILAR_NAME_THRESHOLD = 0.9

# toggle whether similar-name search uses only valid entities
SIMILAR_NAME_ONLY_VALID = True

def run(ctx):
    entity   = ctx.entity
    relation = ctx.relation
    enum     = ctx.enums

    # Metadata is pre-attached in the context:
    # entity["citype_metadata"][citype_name] -> full metadata json for that type
    citype_meta = entity.get("citype_metadata", {})

    # Simple cache so we don't re-parse metadata for every entity
    attr_defs_cache = {}

    def get_attr_defs_for_citype(citype_name: str):
        """
        Return the 'attributes' list from citype metadata for this type,
        in the original order from MetaIS metadata.
        """
        if citype_name in attr_defs_cache:
            return attr_defs_cache[citype_name]

        meta = citype_meta.get(citype_name) or {}
        defs = meta.get("attributes") or []
        # keep original ordering from metadata; do not sort here
        attr_defs_cache[citype_name] = defs
        return defs

    def resolve_enum_value(val):
        """
        If val is:
          - a string starting with 'c_' and present in enum_map,
            return the human-readable value
          - a list, map recursively on elements
          - otherwise, return as-is
        """
        if isinstance(val, str):
            if val.startswith("c_"):
                return enum.get(val, val)
            return val
        if isinstance(val, list):
            return [resolve_enum_value(x) for x in val]
        return val

    def is_invalidated(rec: dict) -> bool:
        meta = rec.get("metaAttributes") or {}
        return meta.get("state") == "INVALIDATED"


    def transform_attributes(citype_name: str, attrs: dict):
        """
        Convert:
          { "Gen_Profil_nazov": "...", "Gen_Profil_zdroj": "c_zdroj.1", ... }

        into an ordered list:
          [
            {
              "attributeTechnicalName": "Gen_Profil_nazov",
              "attributeName": "Názov",
              "value": "..."
            },
            ...
          ]

        Ordering:
          1) attributes in the order from metadata/nodes/<citype>.json
          2) remaining attributes not present in metadata, appended
             sorted by technical name.
        """
        if not isinstance(attrs, dict):
            # Already transformed or unexpected shape; leave as is
            return attrs

        meta_defs = get_attr_defs_for_citype(citype_name)
        result = []
        used = set()

        # 1) follow metadata order
        for attr_def in meta_defs:
            tech = attr_def.get("technicalName")
            if not tech or tech not in attrs:
                continue

            # prefer Slovak label; fall back to English or technical name
            raw_label = attr_def.get("name") or attr_def.get("engName") or tech
            label = str(raw_label).strip()

            raw_value = attrs[tech]
            value = resolve_enum_value(raw_value)

            result.append({
                "attributeTechnicalName": tech,
                "attributeName": label,
                "value": value,
            })
            used.add(tech)

        # 2) any attributes not defined in metadata
        for tech in sorted(k for k in attrs.keys() if k not in used):
            raw_value = attrs[tech]
            value = resolve_enum_value(raw_value)

            result.append({
                "attributeTechnicalName": tech,
                "attributeName": tech,  # no better label available
                "value": value,
            })

        return result

    # Helper to pull an attribute value from *normalized* attributes list
    def get_attr_value(rec: dict, technical_name: str):
        attrs = rec.get("attributes")
        if not isinstance(attrs, list):
            return None
        for a in attrs:
            if a.get("attributeTechnicalName") == technical_name:
                return a.get("value")
        return None

    # Helper to get a human-readable identifier for an entity
    def get_entity_identifier(uuid: str, rec: dict):
        """
        Priority:
          1) Gen_Profil_nazov (name)
          2) Gen_Profil_kod_metais (code)
          3) UUID
        """
        name = get_attr_value(rec, "Gen_Profil_nazov")
        metais_code = get_attr_value(rec, "Gen_Profil_kod_metais")

        if name:
            return str(name)
        if metais_code:
            return str(metais_code)
        return uuid

    # metais_code -> list of (ctype, uuid)
    dup_records: dict[str, list[tuple[str, str]]] = {}

    # global pool ONLY for entities we actually touch
    # uuid -> {"type", "uuid", "attributes", "metaAttributes", ...}
    all_entities: dict[str, dict] = {}

    # --------- PHASE 0: precompute name candidates in the whole DB ----------

    # ctype -> list of (uuid, name_lower)
    candidates_by_citype: dict[str, list[tuple[str, str]]] = defaultdict(list)

    entity_by_uuid = entity.get("by_uuid", {})
    for uuid, rec in tqdm(
        entity_by_uuid.items(),
        desc="metais_dup: collecting name candidates",
        leave=False,
    ):
        ctype = rec.get("type")
        if not ctype:
            continue

        if SIMILAR_NAME_ONLY_VALID and is_invalidated(rec):
            continue

        attrs = rec.get("attributes") or {}
        name = attrs.get("Gen_Profil_nazov")
        if not name:
            continue
        name_str = str(name).strip()
        if not name_str:
            continue
        candidates_by_citype[ctype].append((uuid, name_str.lower()))

    # --------- PHASE 1: detect duplicates only ----------

    for citype_record in tqdm(
        entity.get("types", []),
        desc="metais_dup: scanning MetaIS codes",
        leave=False,
    ):
        citype_name = citype_record.get("technicalName")
        if not citype_name:
            continue

        for uuid in ctx.iter_uuids_of_type(citype_name):
            if uuid is None:
                continue

            # only grab the MetaIS code
            try:
                metais_code = ctx.get_entity_attr(
                    citype_name,
                    uuid,
                    "Gen_Profil_kod_metais",
                )
            except KeyError:
                # uuid not found in uuid_to_index
                print(
                    f"[WARNING] metais code not available for uuid {uuid}, "
                    f"entity type: {citype_name}"
                )
                continue

            if not metais_code:
                continue

            bucket = dup_records.get(metais_code)
            if bucket is None:
                bucket = []
                dup_records[metais_code] = bucket
            bucket.append((citype_name, uuid))

    groups: list[dict] = []

    # --------- PHASE 2: build entities (primaries + neighbors) ----------

    for metais_code, entries in dup_records.items():
        # Only real duplicates
        if len(entries) <= 1:
            continue

        primary_uuids: set[str] = set()

        for ctype, my_uuid in entries:
            primary_uuids.add(my_uuid)

            # materialize primary entity if not already present
            if my_uuid not in all_entities:
                all_entities[my_uuid] = ctx.get_entity_record(ctype, my_uuid)

            # relation info for this type
            node_rel_info = relation["by_node"].get(ctype)
            if not node_rel_info:
                # no relations for this type
                continue

            # entity_role: "src" or "tgt"
            for entity_role in ("src", "tgt"):
                rel_set = node_rel_info.get("by_" + entity_role, set())
                if not rel_set:
                    continue

                for reltype_name in rel_set:
                    rel_info = relation["by_rel"][reltype_name]

                    if entity_role == "src":
                        related_type  = rel_info["target_type"]
                        neighbors_map = rel_info["by_src"]  # my_uuid -> [target_uuids]
                    else:
                        related_type  = rel_info["source_type"]
                        neighbors_map = rel_info["by_tgt"]  # my_uuid -> [source_uuids]

                    neighbor_uuids = neighbors_map.get(my_uuid, [])
                    if not neighbor_uuids:
                        continue

                    for related_uuid in neighbor_uuids:
                        # ensure neighbor entity exists in global pool
                        if related_uuid not in all_entities:
                            try:
                                all_entities[related_uuid] = ctx.get_entity_record(
                                    related_type,
                                    related_uuid,
                                )
                            except KeyError:
                                print(
                                    f"[WARNING] uuid {related_uuid} "
                                    f"not found in entity dump"
                                )
                                continue

        # build deterministic per-group uuid list
        uuids_sorted = sorted(primary_uuids)

        groups.append({
            "metais_code":  metais_code,
            "count":        len(uuids_sorted),
            "entity_uuids": uuids_sorted,
        })

    # sort groups by size descending
    groups.sort(key=lambda g: g["count"], reverse=True)

    # --------- PHASE 2b: "similar name" external matches for primaries ----------

    similar_name_pairs: list[tuple[str, str]] = []  # (primary_uuid, similar_uuid)

    for metais_code, entries in tqdm(
        dup_records.items(),
        desc="metais_dup: similar-name search",
        leave=False,
    ):
        # Skip non-duplicate codes
        if len(entries) <= 1:
            continue

        # set of primary uuids for this group – we don't want to match inside this set
        group_primary_uuids = {uuid for (ctype, uuid) in entries}

        for ctype, my_uuid in entries:
            # require that this primary is actually in our all_entities pool
            if my_uuid not in all_entities:
                continue

            # get its name from the full DB record (attributes dict)
            db_rec = entity_by_uuid.get(my_uuid)
            if not db_rec:
                continue
            if SIMILAR_NAME_ONLY_VALID and is_invalidated(db_rec):
                continue
            db_attrs = db_rec.get("attributes") or {}
            name_val = db_attrs.get("Gen_Profil_nazov")
            if not name_val:
                continue
            name_str = str(name_val).strip().lower()
            if not name_str:
                continue

            candidates = candidates_by_citype.get(ctype, [])
            if not candidates:
                continue

            best_uuid = None
            best_ratio = 0.0

            for cand_uuid, cand_name_lower in candidates:
                if cand_uuid == my_uuid:
                    continue
                # don't match to another member of the same duplicate group
                if cand_uuid in group_primary_uuids:
                    continue

                ratio = SequenceMatcher(None, name_str, cand_name_lower).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_uuid = cand_uuid

            if best_uuid is not None and best_ratio >= SIMILAR_NAME_THRESHOLD:
                similar_name_pairs.append((my_uuid, best_uuid))

                # ensure the similar-name entity is materialized in all_entities
                if best_uuid not in all_entities:
                    try:
                        all_entities[best_uuid] = ctx.get_entity_record(ctype, best_uuid)
                    except KeyError:
                        print(
                            f"[WARNING] similar-name uuid {best_uuid} "
                            f"not found in entity dump"
                        )

    # --------- NORMALIZE ENTITY ATTRIBUTES ----------

    # Convert attributes dict -> ordered list with human-readable labels
    for uuid, rec in all_entities.items():
        ctype = rec.get("type")
        attrs = rec.get("attributes")
        if ctype and isinstance(attrs, dict):
            rec["attributes"] = transform_attributes(ctype, attrs)

    # --------- PHASE 3: build top-level relations (REAL relations only) ----------

    entity_uuid_set = set(all_entities.keys())

    relations: dict[str, dict] = {}
    for reltype_name, rel_info in relation["by_rel"].items():
        by_src = rel_info.get("by_src", {})
        if not by_src:
            continue

        pair_set: set[tuple[str, str]] = set()

        for src_uuid, tgt_list in by_src.items():
            if src_uuid not in entity_uuid_set:
                continue
            for tgt_uuid in tgt_list:
                if tgt_uuid not in entity_uuid_set:
                    continue
                pair_set.add((src_uuid, tgt_uuid))

        if not pair_set:
            continue

        relations[reltype_name] = {
            "source_type": rel_info["source_type"],
            "target_type": rel_info["target_type"],
            "pairs": [
                [src, tgt]
                for (src, tgt) in sorted(pair_set)
            ],
        }

    # --------- PHASE 3b: add synthetic "Has similar name" relations ----------

    if similar_name_pairs:
        # deduplicate in case we found the same pair multiple times
        pair_set = {(src, tgt) for (src, tgt) in similar_name_pairs}

        existing = relations.get(SIMILAR_NAME_RELTYPE)
        if existing is None:
            # We don't have a single canonical source/target type,
            # so leave them unknown – the front-end will show "? -> ?".
            relations[SIMILAR_NAME_RELTYPE] = {
                "source_type": None,
                "target_type": None,
                "pairs": [[src, tgt] for (src, tgt) in sorted(pair_set)],
            }
        else:
            # merge with any existing synthetic pairs
            old_pairs = {(p[0], p[1]) for p in existing.get("pairs", []) if len(p) >= 2}
            merged = old_pairs | pair_set
            existing["pairs"] = [[src, tgt] for (src, tgt) in sorted(merged)]

    # ------------------------------------------------------------------
    # PHASE 4 – adjacency graph (undirected)
    # ------------------------------------------------------------------
    adjacency: dict[str, set[str]] = defaultdict(set)

    for rel_info in relations.values():
        for pair in rel_info.get("pairs", []):
            if len(pair) < 2:
                continue
            src, tgt = pair[0], pair[1]
            adjacency[src].add(tgt)
            adjacency[tgt].add(src)

    # Make sure every entity appears in adjacency (even if isolated)
    for uuid in entity_uuid_set:
        adjacency.setdefault(uuid, set())

    # having the same metais code counts as a "relation" here
    for g in groups:
        uuids = g.get("entity_uuids", [])
        if len(uuids) < 2:
            continue

        # connect u0–u1, u1–u2, ... u(n-2)–u(n-1)
        for u, v in zip(uuids, uuids[1:]):
            adjacency[u].add(v)
            adjacency[v].add(u)

    # Helper sets for later
    duplicated_uuids: set[str] = set()
    for g in groups:
        duplicated_uuids.update(g.get("entity_uuids", []))

    # ------------------------------------------------------------------
    # PHASE 4b – distance from nearest duplicity primary (node level)
    # ------------------------------------------------------------------
    node_distance: dict[str, int] = {}
    q = deque()

    # multi-source BFS from all primaries
    for u in duplicated_uuids:
        if u in adjacency:          # just in case
            node_distance[u] = 0
            q.append(u)

    while q:
        u = q.popleft()
        du = node_distance[u]
        for v in adjacency.get(u, ()):
            if v not in node_distance:
                node_distance[v] = du + 1
                q.append(v)

    # ------------------------------------------------------------------
    # PHASE 3c – attach edge distance from nearest primary (edge level)
    # ------------------------------------------------------------------
    for reltype_name, rel_info in relations.items():
        old_pairs = rel_info.get("pairs", [])
        new_pairs = []

        for pair in old_pairs:
            # keep backward compatibility: allow [src, tgt] or [src, tgt, ...]
            if len(pair) >= 2:
                src, tgt = pair[0], pair[1]
            else:
                continue  # malformed, skip

            d_src = node_distance.get(src)
            d_tgt = node_distance.get(tgt)

            d_candidates = [d for d in (d_src, d_tgt) if d is not None]
            if d_candidates:
                edge_dist = min(d_candidates)
                new_pairs.append([src, tgt, edge_dist])
            else:
                # no path to any primary
                new_pairs.append([src, tgt, -1])

        rel_info["pairs"] = new_pairs

    # Precompute group->set(primaries)
    group_primaries: list[set[str]] = [
        set(g.get("entity_uuids", [])) for g in groups
    ]

    # ------------------------------------------------------------------
    # PHASE 5 – ORPHANS (groups with no external relations)
    # ------------------------------------------------------------------
    orphans: list[dict] = []

    for idx, g in enumerate(groups):
        primaries = group_primaries[idx]
        if not primaries:
            continue

        has_external = False
        for u in primaries:
            for v in adjacency.get(u, ()):
                if v not in primaries:
                    has_external = True
                    break
            if has_external:
                break

        if not has_external:
            orphans.append({
                "group_index": idx,
                "metais_code": g.get("metais_code"),
                "count": g.get("count", len(primaries)),
                "uuids": g.get("entity_uuids", []),
            })

    # ------------------------------------------------------------------
    # PHASE 6 – HUBS with layered neighborhoods
    # ------------------------------------------------------------------
    hubs: list[dict] = []
    hub_uuids: set[str] = set()

    # A hub is any node that:
    #   - is NOT a duplicity primary
    #   - has at least one neighbor that IS a duplicity primary
    for u in entity_uuid_set:
        if u in duplicated_uuids:
            continue
        neighs = adjacency.get(u, ())
        if any(v in duplicated_uuids for v in neighs):
            hub_uuids.add(u)

    # For each hub, BFS to get layers (distance rings)
    for hub_uuid in tqdm(
        sorted(hub_uuids),
        desc="metais_dup: hub layers",
        leave=False,
    ):
        # BFS distances from hub
        dist: dict[str, int] = {hub_uuid: 0}
        q = deque([hub_uuid])

        while q:
            cur = q.popleft()
            for nb in adjacency.get(cur, ()):
                if nb not in dist:
                    dist[nb] = dist[cur] + 1
                    q.append(nb)

        # Build layers by distance (1, 2, 3, ...)
        layers_by_dist: dict[int, list[str]] = defaultdict(list)
        for node_uuid, d in dist.items():
            if d == 0:
                continue  # skip the hub itself
            layers_by_dist[d].append(node_uuid)

        # Sort uuids in each layer for determinism
        layers = []
        max_d = max(layers_by_dist.keys(), default=0)
        for d in range(1, max_d + 1):
            layer_nodes = sorted(layers_by_dist.get(d, []))
            if not layer_nodes:
                continue
            layers.append({
                "count": len(layer_nodes),
                "uuids": layer_nodes,
            })

        # Immediate neighbors (first layer) count
        immediate_count = len(layers[0]["uuids"]) if layers else 0

        hubs.append({
            "hub_uuid": hub_uuid,
            "count": immediate_count,  # number of immediate neighbors (layer 1)
            "layers": layers,          # union of all layers = island around this hub
        })

    # Sort hubs by descending immediate neighbor count
    hubs.sort(key=lambda h: h["count"], reverse=True)

    # ------------------------------------------------------------------
    # PHASE 7 – ISLANDS (connected components, mapped to hubs)
    # ------------------------------------------------------------------
    islands: list[dict] = []
    visited: set[str] = set()

    for start in tqdm(
        entity_uuid_set,
        desc="metais_dup: islands BFS",
        leave=False,
    ):
        if start in visited:
            continue

        # BFS / DFS to get one connected component
        queue = deque([start])
        visited.add(start)
        component: list[str] = []

        while queue:
            u = queue.popleft()
            component.append(u)
            for v in adjacency.get(u, ()):
                if v not in visited:
                    visited.add(v)
                    queue.append(v)

        if not component:
            continue

        # Which hubs live in this island?
        hubs_in_island = sorted(u for u in component if u in hub_uuids)

        # We only record islands that actually contain hubs.
        if hubs_in_island:
            islands.append({
                "count": len(component),
                "uuids": hubs_in_island,   # only hub uuids for this island
            })

    # Sort islands by size descending
    islands.sort(key=lambda isl: isl["count"], reverse=True)

    # ------------------------------------------------------------------
    # FINAL OUTPUT
    # ------------------------------------------------------------------
    out = {
        "date":      ctx.date,
        "name":      "MetaIS code duplicity",
        "count":     len(groups),
        "groups":    groups,
        "orphans":   orphans,   # groups with no external neighbors
        "hubs":      hubs,      # hubs with layered neighborhoods
        "islands":   islands,   # islands described by which hubs they contain
        "entities":  all_entities,
        "relations": relations,
    }
    return out