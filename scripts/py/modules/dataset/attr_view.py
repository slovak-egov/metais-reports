from collections import Counter
from tqdm import tqdm
from packed_reader import interpret_meta_state

TOP_VALUES_LIMIT = 20  # how many most-frequent values to keep per attribute / metaAttr

def classify_validity(raw):
    """
      - None / anything not INVALIDATED -> valid
      - INVALIDATED                     -> invalid
    """
    return "valid" if interpret_meta_state(raw) else "invalid"

TOGGLE_CONFIG = {
    "*": [  # all citypes
        {
            "id": "validity",
            "kind": "meta",
            "technicalName": "state",
            "label": "Platnosť záznamu",
            "classifier": classify_validity, # custom classifier because state can be either "INVALIDATED" or "DRAFT" (or perhaps something else)
            "include_undefined": False,  # undefined treated as 'valid' (DRAFT)
        },
    ],
    "Projekt": [
        {
            "id": "phase",
            "kind": "attr",
            "technicalName": "EA_Profil_Projekt_faza_projektu",
            "label": "Životný cyklus projektu",
            "classifier": None, # no classifier needed - the value itself is the bucket (careful here, if there's many values, there'll be many buckets)
            "include_undefined": True, # undefined phase is a separate category
        },
        {
            "id": "status",
            "kind": "attr",
            "technicalName": "EA_Profil_Projekt_status",
            "label": "Stav evidencie",
            "classifier": None,
            "include_undefined": True,
        },
    ],
}


def get_toggle_specs_for_citype(citype_name: str):
    """
    Combine global (*) toggles with citype-specific ones, in a stable order.
    """
    specs = []
    specs.extend(TOGGLE_CONFIG.get("*", []))
    specs.extend(TOGGLE_CONFIG.get(citype_name, []))
    return specs

def run(ctx, out_dir):
    store = ctx.store # (PackedStore)
    resolve_enum_value = ctx.resolve_enum_value

    # ---------- helpers ----------

    def iter_entity_attributes(rec):
        """
        Yield (technicalName, value) pairs from rec["attributes"].

        Supports:
          - attributes as dict: {technicalName: value, ...}
          - attributes as list of objects with 'attributeTechnicalName'/'technicalName' keys
        """
        attrs = rec.get("attributes") or {}
        if isinstance(attrs, dict):
            for tech, val in attrs.items():
                yield tech, val
        elif isinstance(attrs, list):
            for a in attrs:
                tech = (
                    a.get("attributeTechnicalName")
                    or a.get("technicalName")
                    or a.get("name")
                )
                if not tech:
                    continue
                yield tech, a.get("value")

    def iter_entity_meta(rec):
        """
        Yield (metaName, value) pairs from rec["metaAttributes"] assumed to be a dict.
        """
        meta = rec.get("metaAttributes") or {}
        if isinstance(meta, dict):
            for key, val in meta.items():
                yield key, val

    def get_attr_value_from_raw(rec, technical_name: str):
        """
        Get attribute value from raw/normalized attributes.
        """
        attrs = rec.get("attributes") or {}
        if isinstance(attrs, dict):
            return attrs.get(technical_name)
        if isinstance(attrs, list):
            for a in attrs:
                tech = (
                    a.get("attributeTechnicalName")
                    or a.get("technicalName")
                    or a.get("name")
                )
                if tech == technical_name:
                    return a.get("value")
        return None

    def get_meta_value(rec, meta_name: str):
        meta = rec.get("metaAttributes") or {}
        if isinstance(meta, dict):
            return meta.get(meta_name)
        return None

    def is_value_defined(v):
        """
        Decide if a value counts as "populated".
        """
        if v is None:
            return False
        if isinstance(v, str) and not v.strip():
            return False
        if isinstance(v, (list, dict)) and not v:
            return False
        return True

    def make_hashable(x):
        """
        Turn a value into something safe to use as a dict key.
        """
        if isinstance(x, (str, int, float, bool)) or x is None:
            return x
        # For lists, dicts, etc., just stringify.
        return str(x)

    def update_stat_value(stat, resolved_val):
        """
        Update a stat entry for one entity's value.
        stat has:
          - "count"
          - "value_counts" (a Counter)
        We call this at most once per entity for a given attribute/metaAttr.
        """
        # Count this attribute/metaAttr as defined for this entity
        stat["count"] += 1

        vc = stat["value_counts"]

        if isinstance(resolved_val, list):
            # Multi-valued: count each distinct element once per entity
            seen_in_entity = set()
            for item in resolved_val:
                key = make_hashable(item)
                if key in seen_in_entity:
                    continue
                seen_in_entity.add(key)
                vc[key] += 1
        else:
            key = make_hashable(resolved_val)
            vc[key] += 1

    def make_empty_bucket(base_attr_template):
        """
        Create a fresh stats bucket:
          - entity_count
          - type_counter, uuid_counter
          - attr_stats cloned from base_attr_template (but zeroed)
          - meta_stats (empty, built dynamically)
        """
        attr_stats = {}
        for tech, stat0 in base_attr_template.items():
            attr_stats[tech] = {
                "technicalName": stat0["technicalName"],
                "name":          stat0["name"],
                "description":   stat0["description"],
                "valid":         stat0["valid"],
                "count":         0,
                "value_counts":  Counter(),
            }

        return {
            "entity_count": 0,
            "type_counter": Counter(),
            "uuid_counter": Counter(),
            "attr_stats":   attr_stats,
            "meta_stats":   {},
        }

    def materialize_bucket(bucket):
        """
        Convert a stats bucket into the JSON-ready pieces:
          - systemAttributes
          - attributes
          - metaAttributes
        (Very close to your original end-of-run code.)
        """
        entity_count = bucket["entity_count"]
        type_counter = bucket["type_counter"]
        uuid_counter = bucket["uuid_counter"]
        attr_stats   = bucket["attr_stats"]
        meta_stats   = bucket["meta_stats"]

        # ---------- system attributes ----------
        system_attrs_out = []

        # type
        if entity_count > 0:
            type_count      = sum(type_counter.values())
            type_unique     = len(type_counter)
            type_top_values = [
                {"value": val, "count": cnt}
                for val, cnt in type_counter.most_common(TOP_VALUES_LIMIT)
            ]
        else:
            type_count = type_unique = 0
            type_top_values = []

        system_attrs_out.append({
            "technicalName": "type",
            "name":          "Typ entity",
            "valid":         True,
            "count":         type_count,
            "uniqueCount":   type_unique,
            "topValues":     type_top_values,
        })

        # uuid
        if entity_count > 0:
            uuid_count      = sum(uuid_counter.values())
            uuid_unique     = len(uuid_counter)
            uuid_top_values = []  # usually all unique, not that interesting
        else:
            uuid_count = uuid_unique = 0
            uuid_top_values = []

        system_attrs_out.append({
            "technicalName": "uuid",
            "name":          "Unikátne ID",
            "valid":         True,
            "count":         uuid_count,
            "uniqueCount":   uuid_unique,
            "topValues":     uuid_top_values,
        })

        # ---------- materialize attribute stats ----------
        attrs_out = []
        for tech, stat in attr_stats.items():
            value_counts = stat.pop("value_counts")  # Counter

            count       = stat["count"]
            uniqueCount = len(value_counts) if count > 0 else 0

            # only keep top values if there are duplicates (uniqueCount < count)
            if count > 0 and uniqueCount < count:
                top_vals = [
                    {"value": val, "count": cnt}
                    for val, cnt in value_counts.most_common(TOP_VALUES_LIMIT)
                ]
            else:
                top_vals = []

            stat["uniqueCount"] = uniqueCount
            stat["topValues"]   = top_vals
            attrs_out.append(stat)

        # sort attributes by frequency (descending), then by technicalName
        attrs_out.sort(key=lambda a: (-a["count"], a["technicalName"]))

        # ---------- materialize metaAttribute stats ----------
        meta_out = []
        for meta_name, stat in meta_stats.items():
            value_counts = stat.pop("value_counts")

            count       = stat["count"]
            uniqueCount = len(value_counts) if count > 0 else 0

            if count > 0 and uniqueCount < count:
                top_vals = [
                    {"value": val, "count": cnt}
                    for val, cnt in value_counts.most_common(TOP_VALUES_LIMIT)
                ]
            else:
                top_vals = []

            stat["uniqueCount"] = uniqueCount
            stat["topValues"]   = top_vals
            meta_out.append(stat)

        meta_out.sort(key=lambda a: (-a["count"], a["technicalName"]))

        return system_attrs_out, attrs_out, meta_out

    def get_filter_values_for_rec(rec, toggle_specs):
        """
        For a single entity record, compute:
          - filter_key: tuple of (id, value) pairs (Python-only key)
          - values_by_id: {id -> value} for dimension value collection

        Values are:
          - classifier(resolved_raw) if classifier provided
          - resolved_raw (enum-resolved string) or "__undefined__" if missing (and include_undefined=True)
        """
        if not toggle_specs:
            return None, {}

        key_parts = []
        values_by_id = {}

        for spec in toggle_specs:
            spec_id   = spec["id"]
            kind      = spec["kind"]
            tech_name = spec["technicalName"]
            classifier = spec.get("classifier")
            include_undef = spec.get("include_undefined", False)

            if kind == "attr":
                raw = get_attr_value_from_raw(rec, tech_name)
            else:  # "meta"
                raw = get_meta_value(rec, tech_name)

            resolved = resolve_enum_value(raw) if raw is not None else None

            if classifier is not None:
                val = classifier(resolved)
            else:
                if resolved is None:
                    if include_undef:
                        val = "__undefined__"
                    else:
                        # skip this dimension from the key if we don't want undefined as a bucket
                        val = "__undefined__" if include_undef else "__unspecified__"
                else:
                    val = resolved

            values_by_id[spec_id] = val
            key_parts.append((spec_id, val))

        return tuple(key_parts), values_by_id

    def clone_bucket_for_materialization(bucket):
        """
        Make a deep-ish copy of a stats bucket so materialize_bucket()
        can pop value_counts without destroying the original.
        """
        attr_stats_copy = {}
        for tech, stat in bucket["attr_stats"].items():
            attr_stats_copy[tech] = {
                "technicalName": stat["technicalName"],
                "name":          stat["name"],
                "description":   stat["description"],
                "valid":         stat["valid"],
                "count":         stat["count"],
                "value_counts":  stat["value_counts"].copy(),
            }

        meta_stats_copy = {}
        for meta_name, stat in bucket["meta_stats"].items():
            meta_stats_copy[meta_name] = {
                "technicalName": stat["technicalName"],
                "name":          stat["name"],
                "description":   stat["description"],
                "valid":         stat["valid"],
                "count":         stat["count"],
                "value_counts":  stat["value_counts"].copy(),
            }

        return {
            "entity_count": bucket["entity_count"],
            "type_counter": bucket["type_counter"].copy(),
            "uuid_counter": bucket["uuid_counter"].copy(),
            "attr_stats":   attr_stats_copy,
            "meta_stats":   meta_stats_copy,
        }

    citypes_out = []

    # ---------- main loop over types (from packed store) ----------
    for citype_name in store.list_types():
        if not citype_name:
            continue

        # try to get nice label if ctx still has citype metadata; else fallback
        try:
            meta_info = ctx.get_citype_metadata(citype_name)
        except AttributeError:
            meta_info = {}
        citype_label = meta_info.get("name") or citype_name

        # which toggles apply to this citype?
        toggle_specs = get_toggle_specs_for_citype(citype_name)

        # per-dimension set of actually seen values (for UI)
        dim_values = {spec["id"]: set() for spec in toggle_specs}

        # open packed type view
        tv = store.open_type(citype_name)

        # base attribute stats template from TypeView metadata
        base_attr_template = {}
        for attr_tech in tv.attributes:
            meta = tv.attr_meta.get(attr_tech, {})
            attrName = meta.get("name") or attr_tech
            attrDesc = meta.get("description")
            attrValid = True   # we don't have a 'valid' flag in packed meta; assume True

            base_attr_template[attr_tech] = {
                "technicalName": attr_tech,
                "name":          attrName,
                "description":   attrDesc,
                "valid":         attrValid,
            }

        # stats buckets:
        #   None -> global stats (no filters)
        #   (("validity","valid"),("phase","Iniciačná fáza"),...) -> filtered stats
        stats_by_filter = {}
        stats_by_filter[None] = make_empty_bucket(base_attr_template)

        # iterate over entities of this type using packed_reader
        for uuid, attrs in tqdm(
            tv.iter_records(),
            desc=f"attr_view: {citype_name}",
            leave=False,
        ):
            if uuid is None:
                continue

            # Split packed attributes into regular + meta
            regular_attrs = {}
            meta_attrs = {}

            for key, value in attrs.items():
                if key.startswith("__meta__"):
                    meta_key = key[len("__meta__"):]    # "__meta__state" -> "state"
                    meta_attrs[meta_key] = value
                else:
                    regular_attrs[key] = value

            rec = {
                "type": citype_name,
                "uuid": uuid,
                "attributes": regular_attrs,
                "metaAttributes": meta_attrs,
            }

            # --- compute filter key for this record (once) ---
            filter_key, values_by_id = get_filter_values_for_rec(rec, toggle_specs)
            # collect dimension values for UI
            for dim_id, val in values_by_id.items():
                dim_values[dim_id].add(val)

            # ensure bucket for this filter key exists
            if filter_key is not None and filter_key not in stats_by_filter:
                stats_by_filter[filter_key] = make_empty_bucket(base_attr_template)

            # we always update the global bucket
            bucket_keys = [None]
            if filter_key is not None:
                bucket_keys.append(filter_key)

            for bkey in bucket_keys:
                bucket = stats_by_filter[bkey]
                bucket["entity_count"] += 1

                # --- system attributes ---
                e_type = rec.get("type")
                if is_value_defined(e_type):
                    bucket["type_counter"][e_type] += 1

                e_uuid = rec.get("uuid")
                if is_value_defined(e_uuid):
                    bucket["uuid_counter"][e_uuid] += 1

                # --- normal attributes ---
                for tech, raw_val in iter_entity_attributes(rec):
                    if not tech:
                        continue
                    if not is_value_defined(raw_val):
                        continue

                    resolved_val = resolve_enum_value(raw_val)

                    attr_stats = bucket["attr_stats"]
                    stat = attr_stats.get(tech)
                    if stat is None:
                        # attribute not present in metadata – create a generic entry
                        stat = {
                            "technicalName": tech,
                            "name":          ctx.get_attribute_label(tech, None),
                            "description":   None,
                            "valid":         True,
                            "count":         0,
                            "value_counts":  Counter(),
                        }
                        attr_stats[tech] = stat

                    update_stat_value(stat, resolved_val)

                # --- metaAttributes, fully agnostic ---
                meta_stats = bucket["meta_stats"]
                for meta_name, raw_val in iter_entity_meta(rec):
                    if not meta_name:
                        continue
                    if not is_value_defined(raw_val):
                        continue

                    resolved_val = resolve_enum_value(raw_val)

                    stat = meta_stats.get(meta_name)
                    if stat is None:
                        # no metadata for metaAttrs, so be generic
                        stat = {
                            "technicalName": meta_name,
                            "name":          meta_name,
                            "description":   None,
                            "valid":         True,
                            "count":         0,
                            "value_counts":  Counter(),
                        }
                        meta_stats[meta_name] = stat

                    update_stat_value(stat, resolved_val)

        # ---------- materialize global (unfiltered) bucket ----------
        # ---------- materialize global (unfiltered) bucket ----------
        global_bucket = stats_by_filter[None]

        # use a copy so we don't lose value_counts in the original
        global_bucket_copy = clone_bucket_for_materialization(global_bucket)
        system_attrs_out, attrs_out, meta_out = materialize_bucket(global_bucket_copy)

        # ---------- filters metadata (axes & their possible values) ----------
        filters_meta = []
        axis_ids = []

        for spec in toggle_specs:
            dim_id  = spec["id"]
            label   = spec.get("label") or dim_id
            kind    = spec["kind"]
            tech    = spec["technicalName"]

            values = sorted(dim_values.get(dim_id, []), key=lambda x: str(x))
            filters_meta.append({
                "id":            dim_id,
                "source":        kind,  # "attr" | "meta"
                "technicalName": tech,
                "label":         label,
                "values":        values,
            })
            axis_ids.append(dim_id)

        # mapping: axis_id -> {value -> index}
        axis_value_index = {}
        for fmeta in filters_meta:
            dim_id = fmeta["id"]
            mapping = {val: idx for idx, val in enumerate(fmeta["values"])}
            axis_value_index[dim_id] = mapping

        # ---------- attribute index (shared across all filterViews) ----------
        # use the global (unfiltered) attribute list order
        attribute_index = [a["technicalName"] for a in attrs_out]
        attr_to_idx = {tech: i for i, tech in enumerate(attribute_index)}

        # ---------- build compact value dictionary ----------
        value_dict = []
        value_to_idx = {}

        def get_value_index(v):
            if v not in value_to_idx:
                value_to_idx[v] = len(value_dict)
                value_dict.append(v)
            return value_to_idx[v]

        # ---------- build compact filterViews ----------
        filter_views_compact = []

        filter_views_compact = []
        filter_index = {}

        if toggle_specs:
            # build sparse list of filterViews
            for fkey, bucket in stats_by_filter.items():
                if fkey is None:
                    continue  # skip global bucket
                if bucket["entity_count"] == 0:
                    continue

                # fkey = (("validity","valid"), ("phase","Iniciačná fáza"), ...)
                key_dict = dict(fkey)

                # build axs = [stateIdx_for_dim0, stateIdx_for_dim1, ...]
                axs = []
                for dim_id in axis_ids:
                    val = key_dict.get(dim_id)
                    idx = axis_value_index[dim_id].get(val)
                    if idx is None:
                        axs = None
                        break
                    axs.append(idx)

                if axs is None:
                    continue

                entCt = bucket["entity_count"]

                # compact attributes: only those with count > 0
                atrs = []
                for tech, stat in bucket["attr_stats"].items():
                    count = stat["count"]
                    if count <= 0:
                        continue

                    # ensure attribute has an index
                    attr_idx = attr_to_idx.get(tech)
                    if attr_idx is None:
                        attr_idx = len(attribute_index)
                        attribute_index.append(tech)
                        attr_to_idx[tech] = attr_idx

                    value_counts = stat["value_counts"]
                    uniqueCount = len(value_counts) if count > 0 else 0

                    top_vals_compact = []
                    if count > 0 and uniqueCount > 0 and uniqueCount < count:
                        for val, cnt in value_counts.most_common(TOP_VALUES_LIMIT):
                            vidx = get_value_index(val)
                            top_vals_compact.append([vidx, cnt])

                    atrs.append([attr_idx, count, uniqueCount, top_vals_compact])

                if not atrs:
                    continue

                filter_views_compact.append({
                    "axs":   axs,
                    "entCt": entCt,
                    "atrs":  atrs,
                })

            # sort by entCt descending, then by axs lexicographically
            filter_views_compact.sort(
                key=lambda fv: (-fv["entCt"], fv["axs"])
            )

            # build index: "0|5|7" -> position in filter_views_compact
            for i, fv in enumerate(filter_views_compact):
                key = "|".join(str(x) for x in fv["axs"])
                filter_index[key] = i
        else:
            # no toggles configured for this citype
            attribute_index = []
            value_dict = []
            filter_views_compact = []
            filter_index = {}

        citypes_out.append({
            "technicalName":    citype_name,
            "name":             citype_label,
            "entityCount":      global_bucket["entity_count"],
            "systemAttributes": system_attrs_out,
            "attributes":       attrs_out,
            "metaAttributes":   meta_out,

            # toggle metadata
            "filters":          filters_meta,

            # compact indices
            "attributeIndex":   attribute_index,
            "valueDict":        value_dict,

            # sparse filtered views + index
            "filterViews":      filter_views_compact,
            "filterIndex":      filter_index,
        })

    out = {
        "date":    ctx.date,
        "name":    "Attribute & metaAttribute statistics",
        "citypes": citypes_out,
    }
    
    # write to meta-viz/data/<DATE>/dataset/attr_view.json (or similar)
    from json_writer import dump_json_smart
    out_path = out_dir / "attr_view.json"
    with out_path.open("w", encoding="utf-8") as f:
        dump_json_smart(out, f)

    return out  # optional, for debugging/tests