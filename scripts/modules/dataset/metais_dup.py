def run(ctx):
    entity   = ctx.entity
    relation = ctx.relation

    # metais_code -> list of (ctype, uuid)
    dup_records: dict[str, list[tuple[str, str]]] = {}

    # global pool ONLY for entities we actually touch
    # uuid -> {"type", "uuid", "attributes", "metaAttributes", "relations"?}
    all_entities: dict[str, dict] = {}

    # --------- PASS 1: detect duplicates (no full records yet) ----------
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

            # Only grab the MetaIS code, nothing else.
            try:
                metais_code = ctx.get_entity_attr(
                    citype_name,
                    uuid,
                    "Gen_Profil_kod_metais",
                )
            except KeyError:
                # attribute doesn't exist on this type or uuid not found
                print(f"[WARNING] metais code not available for uuid {uuid}, entity type: {citype_name}")
                continue

            if not metais_code:
                continue

            bucket = dup_records.get(metais_code)
            if bucket is None:
                bucket = []
                dup_records[metais_code] = bucket
            bucket.append((citype_name, uuid))

    groups: list[dict] = []

    # --------- PASS 2: for each code with duplicates, build entities & relations ----------
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

            record = all_entities[my_uuid]

            # reuse existing relations if we’ve enriched this entity already
            entity_relations = record.get("relations")
            if entity_relations is None:
                entity_relations = {}
                record["relations"] = entity_relations

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

                    # ensure bucket for this reltype on the entity
                    rel_entry = entity_relations.get(reltype_name)
                    if rel_entry is None:
                        rel_entry = {
                            "entity_role":   entity_role,
                            "related_type":  related_type,
                            "related_uuids": [],
                        }
                        entity_relations[reltype_name] = rel_entry

                    rel_uuids = rel_entry["related_uuids"]

                    for related_uuid in neighbor_uuids:
                        # keep list unique per reltype
                        if related_uuid not in rel_uuids:
                            rel_uuids.append(related_uuid)

                        # ensure neighbor entity exists in global pool
                        if related_uuid not in all_entities:
                            try:
                                all_entities[related_uuid] = ctx.get_entity_record(
                                    related_type,
                                    related_uuid,
                                )
                            except KeyError:
                                # neighbor not present in entity dump; skip
                                print(f"[WARKING] uuid {related_uuid} not found in entity dump")
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

    out = {
        "date":     ctx.date,
        "entities": all_entities,
        "count":    len(groups),
        "groups":   groups,
    }
    return out