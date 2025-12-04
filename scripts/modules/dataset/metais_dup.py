from collections import defaultdict, deque
from difflib import SequenceMatcher
from tqdm import tqdm

# technical relation names for synthetic relations (not in the database, we made these up)
SIMILAR_NAME_RELTYPE_KEY = "has_similar_name"
SAME_CODE_RELTYPE_KEY    = "share_same_metaid"

# human-friendly labels for those synthetic relations
SIMILAR_NAME_LABEL = "Has similar name"
SAME_CODE_LABEL    = "Share a common MetaIS code"

# fuzzy string match
SIMILAR_NAME_THRESHOLD = 0.9

# toggle whether similar-name search uses only valid entities
SIMILAR_NAME_ONLY_VALID = True

# MetaIS citype name for public organizations
PO_CITYPE_NAME = "PO"


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

    # --------- PHASE 3: build top-level relations (REAL + synthetic) ----------

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

        # pull human-readable labels from metadata if available
        meta = (rel_info.get("metadata") or {})
        human_name = meta.get("name") or reltype_name
        eng_name   = meta.get("engName")

        relations[reltype_name] = {
            # meta about the relation type itself
            "technicalName": reltype_name,
            "name":          human_name,
            "engName":       eng_name,

            # type pair (as before)
            "source_type": rel_info["source_type"],
            "target_type": rel_info["target_type"],

            # edge list
            "pairs": [
                [src, tgt]
                for (src, tgt) in sorted(pair_set)
            ],
        }

    # --------- PHASE 3b: add synthetic "has_similar_name" relations ----------

    if similar_name_pairs:
        # deduplicate in case we found the same pair multiple times
        pair_set = {(src, tgt) for (src, tgt) in similar_name_pairs}

        existing = relations.get(SIMILAR_NAME_RELTYPE_KEY)
        if existing is None:
            relations[SIMILAR_NAME_RELTYPE_KEY] = {
                "technicalName": SIMILAR_NAME_RELTYPE_KEY,
                "name":          SIMILAR_NAME_LABEL,
                "engName":       SIMILAR_NAME_LABEL,  # we don't have a separate engName, reuse

                # no single canonical type pair – front-end may show "? → ?"
                "source_type": None,
                "target_type": None,
                "pairs": [[src, tgt] for (src, tgt) in sorted(pair_set)],
            }
        else:
            old_pairs = {(p[0], p[1]) for p in existing.get("pairs", []) if len(p) >= 2}
            merged = old_pairs | pair_set
            existing["pairs"] = [[src, tgt] for (src, tgt) in sorted(merged)]

    # --------- PHASE 3c: synthetic "share_same_metaid" relations ----------

    # Precompute group->set(primaries) and primary->group index
    group_primaries: list[set[str]] = []
    primary_to_group_idx: dict[str, int] = {}
    for idx, g in enumerate(groups):
        primaries = set(g.get("entity_uuids", []))
        group_primaries.append(primaries)
        for u in primaries:
            primary_to_group_idx[u] = idx

    same_code_pairs: set[tuple[str, str]] = set()

    for g in groups:
        uuids = g.get("entity_uuids", [])
        if len(uuids) < 2:
            continue

        # full clique: connect every pair u_i – u_j, i < j
        n = len(uuids)
        for i in range(n):
            u = uuids[i]
            if not u:
                continue
            for j in range(i + 1, n):
                v = uuids[j]
                if not v:
                    continue
                # keep canonical ordering so (u,v) and (v,u) don't both appear
                pair = (u, v) if u < v else (v, u)
                same_code_pairs.add(pair)

    if same_code_pairs:
        relations[SAME_CODE_RELTYPE_KEY] = {
            "technicalName": SAME_CODE_RELTYPE_KEY,
            "name":          SAME_CODE_LABEL,
            "engName":       SAME_CODE_LABEL,

            # symmetric relation, no fixed type pair
            "source_type": None,
            "target_type": None,
            "pairs": [[src, tgt] for (src, tgt) in sorted(same_code_pairs)],
        }

    # ------------------------------------------------------------------
    # PHASE 4 – adjacency graph + distances
    # ------------------------------------------------------------------

    # Helper sets for later
    duplicated_uuids: set[str] = set()
    for g in groups:
        duplicated_uuids.update(g.get("entity_uuids", []))

    # full adjacency (all edges), undirected
    adjacency: dict[str, set[str]] = defaultdict(set)
    # adjacency using only distance-0 edges (elementary islands)
    adjacency0: dict[str, set[str]] = defaultdict(set)

    # Make sure every entity appears in adjacency (even if isolated)
    for uuid in entity_uuid_set:
        adjacency.setdefault(uuid, set())
        adjacency0.setdefault(uuid, set())

    # 4a) Build adjacency FROM raw relations (before distances)
    for rel_info in relations.values():
        for pair in rel_info.get("pairs", []):
            if len(pair) < 2:
                continue
            src, tgt = pair[0], pair[1]
            adjacency[src].add(tgt)
            adjacency[tgt].add(src)

    # 4b) distances from nearest duplicity primary (node level)
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

    # how far does the “duplicity influence” reach?
    max_node_dist = max(node_distance.values()) if node_distance else 0

    # 4c) attach edge distance from nearest primary (edge level)
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
            else:
                # no path to any primary
                edge_dist = -1

            new_pairs.append([src, tgt, edge_dist])

            # distance-0 edges: primary↔primary or primary↔neighbor
            if edge_dist == 0:
                adjacency0[src].add(tgt)
                adjacency0[tgt].add(src)

        rel_info["pairs"] = new_pairs

    # 4d) determine maximum edge distance
    edge_dists: set[int] = set()
    for rel_info in relations.values():
        for pair in rel_info.get("pairs", []):
            if len(pair) < 3:
                continue
            d = pair[2]
            if isinstance(d, int) and d >= 0:
                edge_dists.add(d)

    max_edge_dist = max(edge_dists) if edge_dists else 0

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
    # PHASE 6 – ISLANDS FOR MULTIPLE DISTANCES 0..max_edge_dist
    # ------------------------------------------------------------------

    def compute_islands_for_maxdist(max_dist: int) -> list[dict]:
        """
        Build islands using all relation edges whose edge_dist satisfies
          0 <= edge_dist <= max_dist.

        Returns a list of dicts
          {
            "count":  <size of connected component (nodes)>,
            "groups": [list of duplicity group indices],
          }

        Only components that contain at least one duplicity group are kept.
        """
        adjacency_d: dict[str, set[str]] = defaultdict(set)

        # Build adjacency from relation pairs
        for rel_info in relations.values():
            for pair in rel_info.get("pairs", []):
                if len(pair) < 3:
                    continue
                src, tgt, edge_dist = pair

                if edge_dist is None or edge_dist < 0 or edge_dist > max_dist:
                    continue

                adjacency_d[src].add(tgt)
                adjacency_d[tgt].add(src)

        # Ensure every entity is present so isolated primaries form size-1 components
        for uuid in entity_uuid_set:
            adjacency_d.setdefault(uuid, set())

        islands_level: list[dict] = []
        visited: set[str] = set()

        for start in tqdm(
            entity_uuid_set,
            desc=f"metais_dup: islands (max_dist={max_dist})",
            leave=False,
        ):
            if start in visited:
                continue

            queue = deque([start])
            visited.add(start)
            component: set[str] = set()

            while queue:
                u = queue.popleft()
                component.add(u)
                for v in adjacency_d.get(u, ()):
                    if v not in visited:
                        visited.add(v)
                        queue.append(v)

            if not component:
                continue

            # Which duplicity groups live in this island?
            comp_groups: list[int] = []
            for idx, primaries in enumerate(group_primaries):
                if primaries & component:
                    comp_groups.append(idx)

            if comp_groups:
                islands_level.append({
                    "count":  len(component),
                    "groups": sorted(comp_groups),
                })

        # largest islands first
        islands_level.sort(key=lambda isl: isl["count"], reverse=True)
        return islands_level

    # Compute islands for all distances 0..max_edge_dist
    islands_by_dist: dict[str, list[dict]] = {}
    for d in range(0, max_edge_dist + 1):
        islands_by_dist[str(d)] = compute_islands_for_maxdist(d)

    # can't sort a dict
    # islands_by_dist.sort(key=lambda isl: isl["count"], reverse=True)

    # ------------------------------------------------------------------
    # PHASE 7 – PO view: which non-primary POs touch which groups?
    # ------------------------------------------------------------------
    po_view: list[dict] = []

    # find all POs that are not duplicity primaries
    po_uuids: set[str] = set()
    for uuid, rec in all_entities.items():
        ctype = rec.get("type")
        if ctype == PO_CITYPE_NAME and uuid not in duplicated_uuids:
            po_uuids.add(uuid)

    # po_uuid -> set(group_idx)
    po_to_groups: dict[str, set[int]] = defaultdict(set)

    # scan all relations for distance-0 edges primary<->PO
    for rel_info in relations.values():
        for pair in rel_info.get("pairs", []):
            if len(pair) < 3:
                continue
            src, tgt, edge_dist = pair
            if edge_dist != 0:
                continue   # we only care about direct neighborhood of primaries

            # primary -> PO
            if src in duplicated_uuids and tgt in po_uuids:
                gidx = primary_to_group_idx.get(src)
                if gidx is not None:
                    po_to_groups[tgt].add(gidx)

            # PO -> primary
            if tgt in duplicated_uuids and src in po_uuids:
                gidx = primary_to_group_idx.get(tgt)
                if gidx is not None:
                    po_to_groups[src].add(gidx)

    # materialize PO entries – we keep ALL that touch at least one duplicity group
    for po_uuid, group_indices in po_to_groups.items():
        rec = all_entities.get(po_uuid, {"type": None, "attributes": [], "metaAttributes": {}})
        po_view.append({
            "po_uuid": po_uuid,
            "identifier": get_entity_identifier(po_uuid, rec),
            "group_indices": sorted(group_indices),
            "group_count": len(group_indices),
        })

    # sort POs by how many groups they touch (descending)
    po_view.sort(key=lambda h: h["group_count"], reverse=True)

    # ------------------------------------------------------------------
    # FINAL OUTPUT
    # ------------------------------------------------------------------
    out = {
        "date":      ctx.date,
        "name":      "MetaIS code duplicity",
        "count":     len(groups),
        "groups":    groups,
        "orphans":   orphans,
        "islands":   islands_by_dist,
        "po_view":   po_view,
        "entities":  all_entities,
        "relations": relations,
    }
    return out