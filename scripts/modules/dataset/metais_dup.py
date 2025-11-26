def run(ctx):
    entity   = ctx.entity
    relation = ctx.relation

    # metais_code -> list of (ctype, uuid)
    dup_records: dict[str, list[tuple[str, str]]] = {}

    # global pool ONLY for entities we actually touch
    # uuid -> {"type", "uuid", "attributes", "metaAttributes", ...}
    all_entities: dict[str, dict] = {}

    # --------- PHASE 1: detect duplicates only ----------
    for citype_record in entity.get("types", []):
        citype_name = citype_record.get("technicalName")
        if not citype_name:
            continue
        if citype_name not in entity:
            continue

        citype_data = entity[citype_name]

        for uuid in citype_data["uuids"]:
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
                # attribute doesn't exist on this type or uuid not found
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
                                # neighbor not present in entity dump; skip
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

    # --------- PHASE 3: build top-level relations ----------
    # Only relations between entities that actually appear in all_entities
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

        # store direction explicitly: src -> tgt
        relations[reltype_name] = {
            "source_type": rel_info["source_type"],
            "target_type": rel_info["target_type"],
            "pairs": [
                [src, tgt]
                for (src, tgt) in sorted(pair_set)
            ],
        }

    out = {
        "date":      ctx.date,
        "name":      "MetaIS code duplicity",
        "count":     len(groups),
        "groups":    groups,       # [{metais_code, count, entity_uuids}]
        "entities":  all_entities, # {uuid: {type, uuid, attributes, metaAttributes}}
        "relations": relations,    # {reltype: {source_type, target_type, pairs:[[src,tgt],...]}}
    }
    return out