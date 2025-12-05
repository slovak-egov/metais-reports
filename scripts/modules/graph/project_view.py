from collections import defaultdict, deque
from tqdm import tqdm

# ----------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------

# MetaIS citype names for projects (adjust as needed)
PROJECT_CITYPE_NAMES = {"Projekt"}  # e.g. {"Projekt", "Projekt_EU"}

# Maximum number of “layers” around a project:
# layer 0 = immediate neighbors, layer 1 = neighbors-of-neighbors, etc.
MAX_LAYERS = 3

# ----------------------------------------------------------------------
# SCRIPT
# ----------------------------------------------------------------------


def run(ctx):
    entity   = ctx.entity
    relation = ctx.relation
    resolve_enum_value = ctx.resolve_enum_value

    entity_by_uuid = entity.get("by_uuid", {})

    # ------------------------------------------------------------------
    # PHASE 1 – Find all projects
    # ------------------------------------------------------------------

    project_uuids: set[str] = set()

    for uuid, rec in entity_by_uuid.items():
        ctype = rec.get("type")
        if ctype in PROJECT_CITYPE_NAMES:
            project_uuids.add(uuid)

    # ------------------------------------------------------------------
    # PHASE 2 – Build undirected adjacency + simplified relations
    # ------------------------------------------------------------------

    adjacency: dict[str, set[str]] = defaultdict(set)
    relations_out: dict[str, dict] = {}

    for reltype_name, rel_info in relation.get("by_rel", {}).items():
        by_src = rel_info.get("by_src", {})
        if not by_src:
            continue

        pair_set: set[tuple[str, str]] = set()

        for src_uuid, tgt_list in by_src.items():
            if not tgt_list:
                continue
            for tgt_uuid in tgt_list:
                if not tgt_uuid:
                    continue
                pair_set.add((src_uuid, tgt_uuid))
                # undirected adjacency for distance computation
                adjacency[src_uuid].add(tgt_uuid)
                adjacency[tgt_uuid].add(src_uuid)

        if not pair_set:
            continue

        meta = rel_info.get("metadata") or {}
        human_name = meta.get("name") or reltype_name
        eng_name   = meta.get("engName")

        relations_out[reltype_name] = {
            "technicalName": reltype_name,
            "name":          human_name,
            "engName":       eng_name,
            "source_type":   rel_info.get("source_type"),
            "target_type":   rel_info.get("target_type"),
            "pairs": [
                [src, tgt]
                for (src, tgt) in sorted(pair_set)
            ],
        }

    # ------------------------------------------------------------------
    # PHASE 3 – For each project, compute layered neighborhoods
    # ------------------------------------------------------------------

    def compute_layers_for_project(project_uuid: str, max_layers: int):
        """
        BFS from the project over the undirected adjacency graph.

        Convention:
          - Direct neighbors of the project are distance 0 (layer “0”).
          - Their neighbors (excluding the project) are distance 1 (layer “1”).
          - etc., up to max_layers - 1.

        Returns:
          layers: dict[str, list[str]]
            e.g. {"0": [...], "1": [...]} with sorted UUIDs
        """
        layers: dict[int, list[str]] = defaultdict(list)
        dist: dict[str, int] = {}

        queue = deque()

        # seed: direct neighbors at distance 0
        for nbr in adjacency.get(project_uuid, ()):
            dist[nbr] = 0
            queue.append(nbr)

        while queue:
            u = queue.popleft()
            d = dist[u]

            if d >= max_layers - 1:
                continue

            for v in adjacency.get(u, ()):
                if v == project_uuid:
                    continue  # do not assign a distance to the project itself
                if v not in dist:
                    dist[v] = d + 1
                    queue.append(v)

        # organize into integer layers
        for uuid, d in dist.items():
            if 0 <= d < max_layers:
                layers[d].append(uuid)

        # sort UUIDs in each layer and stringify keys
        return {
            str(layer): sorted(uuids)
            for layer, uuids in layers.items()
        }

    projects_out = []
    used_uuids: set[str] = set(project_uuids)  # all projects are “used” by definition

    for project_uuid in tqdm(
        sorted(project_uuids),
        desc="project_view: computing neighborhoods",
        leave=False,
    ):
        base_rec = entity_by_uuid.get(project_uuid, {})
        identifier = ctx.get_entity_identifier(project_uuid, base_rec)

        layers = compute_layers_for_project(project_uuid, MAX_LAYERS)

        # track all UUIDs we need to materialize
        used_uuids.update(project_uuid for project_uuid in [project_uuid])  # no-op but explicit
        for uuids in layers.values():
            used_uuids.update(uuids)

        projects_out.append({
            "project_uuid": project_uuid,
            "identifier":   identifier,
            "layers":       layers,  # {"0": [...], "1": [...], ...}
        })

    # ------------------------------------------------------------------
    # PHASE 4 – Materialize entities (normalized attributes)
    # ------------------------------------------------------------------

    all_entities: dict[str, dict] = {}

    for uuid in tqdm(
        sorted(used_uuids),
        desc="project_view: materializing entities",
        leave=False,
    ):
        rec = entity_by_uuid.get(uuid)
        if not rec:
            # entity not in the dump (shouldn't happen, but be defensive)
            all_entities[uuid] = {
                "uuid": uuid,
                "type": None,
                "attributes": [],
                "metaAttributes": {},
            }
            continue

        # shallow copy so we can modify attributes without touching original
        rec_copy = dict(rec)
        ctype = rec_copy.get("type")
        attrs = rec_copy.get("attributes")

        if ctype and isinstance(attrs, dict):
            rec_copy["attributes"] = ctx.normalize_attributes(ctype, attrs)

        all_entities[uuid] = rec_copy

    # ------------------------------------------------------------------
    # PHASE 5 – Filter relations to only edges fully inside used_uuids
    # ------------------------------------------------------------------

    relations_filtered: dict[str, dict] = {}

    for reltype_name, rel_info in relations_out.items():
        pairs = rel_info.get("pairs", [])
        # keep only edges where *both* endpoints are in used_uuids
        kept_pairs = [
            [src, tgt]
            for (src, tgt) in pairs
            if src in used_uuids and tgt in used_uuids
        ]
        if not kept_pairs:
            continue

        # shallow copy the metadata, swap in filtered pairs
        rel_copy = dict(rel_info)
        rel_copy["pairs"] = kept_pairs
        relations_filtered[reltype_name] = rel_copy

    # ------------------------------------------------------------------
    # FINAL OUTPUT
    # ------------------------------------------------------------------

    out = {
        "date":          ctx.date,
        "name":          "Project neighborhood view",
        "projectTypes":  sorted(PROJECT_CITYPE_NAMES),
        "projectCount":  len(projects_out),
        "maxLayer":      MAX_LAYERS - 1,
        "projects":      projects_out,
        "entities":      all_entities,
        "relations":     relations_filtered,
    }
    return out