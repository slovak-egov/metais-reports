#!/usr/bin/env python3
from pathlib import Path

from packed_reader import PackedStore
from repack import RepackSpec, repack  # <-- the repacker module we wrote

FULL_BASE = Path("output/07-12-2025/packed")
REPACK_BASE = Path("output/07-12-2025/repack_projects_demo/packed")


def uuid_str_to_bytes(u: str) -> bytes:
    """Convert 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx' to 16-byte value."""
    return bytes.fromhex(u.replace("-", ""))


def collect_project_neighborhood(store: PackedStore) -> set[str]:
    """
    Walk the *full* dataset and return a set of UUID strings that should go
    into the repack:

      - all Projekt
      - all PO that associate those Projekts
      - all KS / AS / ISVS realized by those Projekts
      - all ISVS that are parents / children of those ISVS
    """
    uuid_set: set[str] = set()

    # 1) All projects
    proj_tv = store.open_type("Projekt")
    project_uuids: list[str] = []

    # We don't actually need attributes here → empty attr list is fine.
    for uuid_str, _attrs in proj_tv.iter_records([]):
        project_uuids.append(uuid_str)
        uuid_set.add(uuid_str)

    print(f"[collect] Projects: {len(project_uuids)}")

    # 2) Add PO that associate those projects (PO -> Projekt)
    rel = store.relations
    po_uuids: set[str] = set()
    for pu in project_uuids:
        for po in rel.neighbors_to("PO_asociuje_Projekt", pu):
            po_uuids.add(po)
            uuid_set.add(po)

    print(f"[collect] PO associated: {len(po_uuids)}")

    # 3) Add KS, AS, ISVS realized by these projects
    ks_uuids: set[str] = set()
    as_uuids: set[str] = set()
    isvs_uuids: set[str] = set()

    for pu in project_uuids:
        for ks in rel.neighbors_from("Projekt_realizuje_KS", pu):
            ks_uuids.add(ks)
            uuid_set.add(ks)
        for a in rel.neighbors_from("Projekt_realizuje_AS", pu):
            as_uuids.add(a)
            uuid_set.add(a)
        for isvs in rel.neighbors_from("Projekt_realizuje_ISVS", pu):
            isvs_uuids.add(isvs)
            uuid_set.add(isvs)

    print(f"[collect] KS:   {len(ks_uuids)}")
    print(f"[collect] AS:   {len(as_uuids)}")
    print(f"[collect] ISVS: {len(isvs_uuids)}")

    # 4) For each ISVS, add ISVS parents/children via ISVS_patri_pod_ISVS
    related_isvs: set[str] = set()

    for u in isvs_uuids:
        # children (this ISVS is parent)
        for child in rel.neighbors_from("ISVS_patri_pod_ISVS", u):
            related_isvs.add(child)
            uuid_set.add(child)
        # parents (this ISVS is child)
        for parent in rel.neighbors_to("ISVS_patri_pod_ISVS", u):
            related_isvs.add(parent)
            uuid_set.add(parent)

    print(f"[collect] ISVS parents/children: {len(related_isvs)}")
    print(f"[collect] TOTAL UUIDs in neighborhood: {len(uuid_set)}")

    return uuid_set


def main():
    store = PackedStore(FULL_BASE)
    try:
        # ------------------------------------------------------------------
        # 1) Collect the “interesting neighborhood” from the FULL dataset.
        # ------------------------------------------------------------------
        uuid_strings = collect_project_neighborhood(store)

    finally:
        store.close()

    # ----------------------------------------------------------------------
    # 2) Turn UUID strings into 16-byte values for the repacker.
    # ----------------------------------------------------------------------
    uuid_allowlist_bytes = {uuid_str_to_bytes(u) for u in uuid_strings}

    # ----------------------------------------------------------------------
    # 3) Build a RepackSpec:
    #
    #    - entity_types: just the types we care about
    #    - entity_validity: "valid" or "all" depending on your taste
    #    - relation_types: None  → include ALL relation types from source
    #    - include_relations_if_both_endpoints_in_allowlist = True
    #      → keep only edges whose both endpoints are in uuid_allowlist
    # ----------------------------------------------------------------------
    spec = RepackSpec(
        source_packed=FULL_BASE,
        dest_packed=REPACK_BASE,
        entity_types={"Projekt", "PO", "KS", "AS", "ISVS"},
        entity_validity="valid",          # or "all" if you want invalid too
        relation_types=None,              # None → all relation types in source
        relation_validity="all",
        uuid_allowlist=uuid_allowlist_bytes,
        include_relations_if_both_endpoints_in_allowlist=True,
        profile_name="repack-projects-demo",
        source_dump_date="05-12-2025",    # just to be explicit in manifest
    )

    # ----------------------------------------------------------------------
    # 4) Run the repack.
    # ----------------------------------------------------------------------
    print("[demo-repack] Starting repack …")
    repack(spec)
    print("[demo-repack] Repack finished.")

    # ----------------------------------------------------------------------
    # 5) (Optional) Open the repacked store and do a quick sanity check.
    # ----------------------------------------------------------------------
    repacked_store = PackedStore(REPACK_BASE)
    try:
        proj = repacked_store.open_type("Projekt")
        count_repacked = sum(1 for _ in proj.iter_records([]))
        print(f"[demo-repack] Repacked Projekt count: {count_repacked}")

        # You can also re-run a subset of your original demo here, now against
        # the tiny repack instead of the giant full dataset.
    finally:
        repacked_store.close()


if __name__ == "__main__":
    main()