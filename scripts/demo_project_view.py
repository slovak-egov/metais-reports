#!/usr/bin/env python3
from pathlib import Path
from packed_reader import PackedStore

BASE_DIR = Path("output/05-12-2025/packed")


def pick_project(store: PackedStore, index: int = 13):
    """
    Pick the N-th Projekt (0-based) and return (uuid, attrs).
    We resolve just kod + nazov to keep it cheap.
    """
    proj = store.open_type("Projekt")
    for i, (uuid_str, attrs) in enumerate(
        proj.iter_records(["Gen_Profil_kod_metais", "Gen_Profil_nazov"])
    ):
        if i == index:
            return uuid_str, attrs
    raise RuntimeError(f"Projekt index {index} out of range")


def get_code_for_uuid(store: PackedStore, type_name: str, uuid_str: str):
    """
    Resolve Gen_Profil_kod_metais for a given UUID of a given type.
    Uses TypeView.find_record_index_by_uuid + get_attr_value.
    """
    tv = store.open_type(type_name)
    idx = tv.find_record_index_by_uuid(uuid_str)
    if idx is None:
        return None
    return tv.get_attr_value(idx, "Gen_Profil_kod_metais")


def main():
    store = PackedStore(BASE_DIR)

    # --------------------------------------------------------
    # 1) Pick a project (15th in sorted view)
    # --------------------------------------------------------
    proj_uuid, proj_attrs = pick_project(store, index=13)
    proj_code = proj_attrs.get("Gen_Profil_kod_metais")
    proj_name = proj_attrs.get("Gen_Profil_nazov")

    print("=== PROJECT ===")
    print("UUID :", proj_uuid)
    print("Code :", proj_code)
    print("Name :", proj_name)
    print()

    # --------------------------------------------------------
    # 2) Get its PO via PO_asociuje_Projekt
    #    Relation is PO -> Projekt, so we query neighbors_to()
    # --------------------------------------------------------
    print("=== PO_asociuje_Projekt (PO -> Projekt) ===")
    po_uuids = store.relations.neighbors_to("PO_asociuje_Projekt", proj_uuid)
    print(f"PO count: {len(po_uuids)}")

    for u in po_uuids[:10]:
        code = get_code_for_uuid(store, "PO", u)
        print(f"  PO UUID={u}  code={code}")
    print()

    # --------------------------------------------------------
    # 3) Get KS, AS, ISVS realized by this Projekt
    #    Projekt_realizuje_*: Projekt -> {KS, AS, ISVS}
    # --------------------------------------------------------
    print("=== Projekt_realizuje_KS (Projekt -> KS) ===")
    ks_uuids = store.relations.neighbors_from("Projekt_realizuje_KS", proj_uuid)
    print(f"KS count: {len(ks_uuids)}")
    for u in ks_uuids[:10]:
        code = get_code_for_uuid(store, "KS", u)
        print(f"  KS UUID={u}  code={code}")
    print()

    print("=== Projekt_realizuje_AS (Projekt -> AS) ===")
    as_uuids = store.relations.neighbors_from("Projekt_realizuje_AS", proj_uuid)
    print(f"AS count: {len(as_uuids)}")
    for u in as_uuids[:10]:
        code = get_code_for_uuid(store, "AS", u)
        print(f"  AS UUID={u}  code={code}")
    print()

    print("=== Projekt_realizuje_ISVS (Projekt -> ISVS) ===")
    isvs_uuids = store.relations.neighbors_from("Projekt_realizuje_ISVS", proj_uuid)
    print(f"ISVS count: {len(isvs_uuids)}")
    for u in isvs_uuids[:10]:
        code = get_code_for_uuid(store, "ISVS", u)
        print(f"  ISVS UUID={u}  code={code}")
    print()

    # --------------------------------------------------------
    # 4) For all those ISVS, find related ISVS via ISVS_patri_pod_ISVS
    #    We look both directions:
    #      - neighbors_from:  this ISVS -> children
    #      - neighbors_to:    parents -> this ISVS
    # --------------------------------------------------------
    print("=== ISVS_patri_pod_ISVS (ISVS hierarchy) ===")

    related_isvs = set()

    for u in isvs_uuids:
        # children (this ISVS is parent)
        for child in store.relations.neighbors_from("ISVS_patri_pod_ISVS", u):
            related_isvs.add(child)
        # parents (this ISVS is child)
        for parent in store.relations.neighbors_to("ISVS_patri_pod_ISVS", u):
            related_isvs.add(parent)

    print(f"Related ISVS (parents+children) count: {len(related_isvs)}")

    for u in list(related_isvs)[:20]:
        code = get_code_for_uuid(store, "ISVS", u)
        print(f"  ISVS UUID={u}  code={code}")

    store.close()


if __name__ == "__main__":
    main()