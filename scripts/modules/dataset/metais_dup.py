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

    '''
    print("[DEBUG] total uuids:", len(entity.get("by_uuid", {})))
    print("[DEBUG] first few types:",
        [t.get("technicalName") for t in entity.get("types", [])][:10])
    '''
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

    '''
    # --- DEBUG: summarize what we actually saw ---
    num_codes = len(dup_records)
    num_real_duplicates = sum(1 for v in dup_records.values() if len(v) > 1)

    print("[DEBUG] metais codes seen (distinct):", num_codes)
    print("[DEBUG] real duplicate codes (len>1):", num_real_duplicates)
    '''
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

    # --------- NORMALIZE ENTITY ATTRIBUTES ----------
    # Convert attributes dict -> ordered list with human-readable labels
    for uuid, rec in all_entities.items():
        ctype = rec.get("type")
        attrs = rec.get("attributes")
        if ctype and isinstance(attrs, dict):
            rec["attributes"] = transform_attributes(ctype, attrs)

    # --------- PHASE 3: build top-level relations ----------
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

    out = {
        "date":      ctx.date,
        "name":      "MetaIS code duplicity",
        "count":     len(groups),
        "groups":    groups,
        "entities":  all_entities,
        "relations": relations,
    }
    return out