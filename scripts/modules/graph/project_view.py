from collections import defaultdict, deque
from tqdm import tqdm

# ----------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------

# MetaIS citype names for projects (adjust as needed)
PROJECT_CITYPE_NAME = "Projekt"  # e.g. {"Projekt", "Projekt_EU"}

# Maximum graph distance from any project node:
# dist = 0 for project itself, 1 for direct neighbors, ..., MAX_LAYERS for outer ring.
MAX_LAYERS = 3

# ----------------------------------------------------------------------
# SCRIPT
# ----------------------------------------------------------------------


def run(ctx, out_dir):
    """
    New-style project_view:

      - Find all project UUIDs in packed nodes
      - Build undirected adjacency from packed relations
      - Multi-source BFS out to MAX_LAYERS hops from any project
      - Request a repack of all UUIDs reached

    No large JSON of entities/relations is produced anymore.
    """

    store = ctx.store
    rel_store = store.relations
    uuid_index = store.uuid_index

    # ------------------------------------------------------------------
    # PHASE 1 – Find all projects (from packed nodes)
    # ------------------------------------------------------------------

    project_uuids: set[str] = set()
    all_types = set(store.list_types())

    ctype = PROJECT_CITYPE_NAME
    if ctype not in all_types:
        print(f"[project_view] Citype {ctype!r} not present in this packed snapshot; skipping.")
        return

    tv = store.open_type(ctype)
    for uuid, _attrs in tqdm(
        tv.iter_records(),
        desc=f"project_view: scanning {ctype}",
        leave=False,
    ):
        if uuid:
            project_uuids.add(uuid)

    if not project_uuids:
        print("[project_view] No projects found; skipping repack request")
        return

    print(f"[project_view] Found {len(project_uuids)} project nodes")

    # ------------------------------------------------------------------
    # PHASE 2 – Build undirected adjacency from packed relations
    # ------------------------------------------------------------------

    adjacency: dict[str, set[str]] = defaultdict(set)

    # We reach slightly into RelationStore internals (_relations) here.
    # For each reltype, we use the .src.tgt file and treat edges as undirected
    # for neighborhood computation.
    for reltype, files in rel_store._relations.items():
        rf_src = files.get("src.tgt")
        if not rf_src:
            continue

        for src_id, tgt_id in rf_src.iter_all_pairs():
            try:
                src_uuid = uuid_index.get_uuid(src_id)
                tgt_uuid = uuid_index.get_uuid(tgt_id)
            except Exception:
                continue

            if not src_uuid or not tgt_uuid:
                continue

            adjacency[src_uuid].add(tgt_uuid)
            adjacency[tgt_uuid].add(src_uuid)

    print(f"[project_view] Built adjacency for {len(adjacency)} nodes")

    # ------------------------------------------------------------------
    # PHASE 3 – Multi-source BFS from all projects (radius = MAX_LAYERS)
    # ------------------------------------------------------------------

    used_uuids: set[str] = set(project_uuids)
    dist: dict[str, int] = {}
    q: deque[str] = deque()

    # Seed BFS with all projects at distance 0
    for u in project_uuids:
        dist[u] = 0
        q.append(u)

    while q:
        u = q.popleft()
        d = dist[u]

        if d >= MAX_LAYERS:
            continue

        for v in adjacency.get(u, ()):
            if v not in dist:
                dist[v] = d + 1
                used_uuids.add(v)
                q.append(v)

    print(
        f"[project_view] Nodes within {MAX_LAYERS} hops of any project: "
        f"{len(used_uuids)}"
    )

    # ------------------------------------------------------------------
    # PHASE 4 – Request repack (no bulky JSON)
    # ------------------------------------------------------------------

    ctx.request_repack(
        profile="project_view",     # any profile name; first wins in master_loader
        entity_uuids=used_uuids,    # all reachable nodes
        relation_types=None,        # ALL relation types; run_repack will filter by endpoints
        only_valid=None,            # let repack decide; or True if you want only valid nodes
    )

    # Optional: tiny debug summary instead of full entity/edge dump
    try:
        summary = {
            "date": ctx.date,
            "name": "Project neighborhood repack request",
            "projectCount": len(project_uuids),
            "maxLayers": MAX_LAYERS,
            "usedUuidCount": len(used_uuids),
        }
        import json
        from json_writer import dump_json_smart

        summary_path = out_dir / "project_view_summary.json"
        with summary_path.open("w", encoding="utf-8") as f:
            dump_json_smart(summary, f)
        print(f"[project_view] Wrote summary to {summary_path}")
    except Exception as e:
        print(f"[project_view] WARNING: failed to write summary: {e}")