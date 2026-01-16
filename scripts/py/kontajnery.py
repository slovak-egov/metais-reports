import pandas as pd
from tqdm import tqdm
from pathlib import Path
from typing import Any, Mapping, Union

from metais.packed_reader.packed_reader import PackedReader

Pathish = Union[str, Path]

def verify_obj_for_excel(content: Any) -> bool:
    # Expect: { "col_name": [values...], ... }
    if not isinstance(content, Mapping):
        print(f"Warning: content must be a dict-like mapping, got {type(content).__name__}")
        return False

    lengths: dict[str, int] = {}
    ok = True

    for k, v in content.items():
        if not isinstance(k, str):
            print(f"Warning: column name must be str, got {type(k).__name__}: {k!r}")
            ok = False
            continue

        if not isinstance(v, list):
            print(f"Warning: column '{k}' is not a list (got {type(v).__name__})")
            ok = False
            continue

        lengths[k] = len(v)

    if not lengths:
        print("Warning: no valid columns found (empty table).")
        return False

    # Check all lengths equal
    cols = list(lengths.keys())
    ref_len = lengths[cols[0]]

    mismatched = {k: n for k, n in lengths.items() if n != ref_len}
    if mismatched:
        print("Warning: columns have different lengths:")
        print(f"  reference: '{cols[0]}' -> {ref_len}")
        for k, n in mismatched.items():
            print(f"  mismatch:  '{k}' -> {n}")
        ok = False

    return ok


def save_excel(content: Any, path: Pathish, sort_by: str | None = None, order: str = "asc") -> bool:
    if not verify_obj_for_excel(content):
        print("Not saving Excel: verification failed.")
        return False

    path = path if isinstance(path, Path) else Path(str(path))
    if path.suffix.lower() != ".xlsx":
        path = path.with_suffix(".xlsx")

    path.parent.mkdir(parents=True, exist_ok=True)

    df = pd.DataFrame(content)
    if sort_by is not None:
        if order in ["asc", "ascending"]:
            df = df.sort_values(sort_by, ascending=True, kind="stable")
        else:
            df = df.sort_values(sort_by, ascending=False, kind="stable")
    df.to_excel(path, index=False)
    return True


def main():
    with PackedReader(
        date="14-01-2026",
        dict_cache_size=16384,
        attr_cache_size=1024,
        resolver_cache_size=1024,
        open_relation_partitions_max=None
    ) as pr:
        po_names = []
        po_ks_rel_counts = []
        ks_counts = []
        ks_as_rel_counts = []
        as_counts = []
        isvs_as_rel_counts = []
        isvs_counts = []
        node_counts = []
        rel_counts = []
        obj_counts = []
        total = pr._get_local_resolver("PO").local_count
        for PO in tqdm(pr.iterate_citype(
            "PO",
            include_attrs=True,
            include_meta=False,
            valid_only=True
        ), total=total):
            po_name = pr.get_attr_value_typed(PO, "Gen_Profil_nazov")

            ks_uuids: set[str] = set()
            as_uuids: set[str] = set()
            isvs_uuids: set[str] = set()
            all_ent_uuids: set[str] = set()

            po_ks_rel_uuids: set[str] = set()
            as_ks_rel_uuids: set[str] = set()
            isvs_as_rel_uuids: set[str] = set()
            all_rel_uuids: set[str] = set()

            all_ent_uuids.add(PO.uuid_str())

            for KS, po_ks_rel_uuid in pr.iterate_neighbors(
                PO,
                reltype="PO_je_gestor_KS", # here KS is target, PO is source
                role="source",
                include_attrs=False,
                as_nodes=True,
                valid_only=True,
                include_rel_uuid=True,
            ):
                ks_uuid = KS.uuid_str()

                ks_uuids.add(ks_uuid)
                all_ent_uuids.add(ks_uuid)

                po_ks_rel_uuids.add(po_ks_rel_uuid)
                all_rel_uuids.add(po_ks_rel_uuid)
                
                for AS, as_ks_rel_uuid in pr.iterate_neighbors(
                    KS,
                    role="either", # we want KS -anything-> AS and KS <-anything- AS
                    neighbor_citype="AS",
                    include_attrs=False,
                    as_nodes=True,
                    valid_only=True,
                    include_rel_uuid=True,
                ):
                    as_uuid = AS.uuid_str()

                    as_uuids.add(as_uuid)
                    all_ent_uuids.add(as_uuid)

                    as_ks_rel_uuids.add(as_ks_rel_uuid)
                    all_rel_uuids.add(as_ks_rel_uuid)
                    
                    for ISVS, isvs_as_rel_uuid in pr.iterate_neighbors(
                        AS,
                        reltype="ISVS_realizuje_AS", # here AS is target
                        role="target",
                        include_attrs=False,
                        as_nodes=True,
                        valid_only=True,
                        include_rel_uuid=True,
                    ):
                        isvs_uuid = ISVS.uuid_str()

                        isvs_uuids.add(isvs_uuid)
                        all_ent_uuids.add(isvs_uuid)

                        isvs_as_rel_uuids.add(isvs_as_rel_uuid)
                        all_rel_uuids.add(isvs_as_rel_uuid)

            po_names.append(po_name)
            po_ks_rel_counts.append(len(po_ks_rel_uuids))
            ks_counts.append(len(ks_uuids))
            ks_as_rel_counts.append(len(as_ks_rel_uuids))
            as_counts.append(len(as_uuids))
            isvs_as_rel_counts.append(len(isvs_as_rel_uuids))
            isvs_counts.append(len(isvs_uuids))
            node_counts.append(len(all_ent_uuids))
            rel_counts.append(len(all_rel_uuids))
            obj_counts.append(len(all_ent_uuids) + len(all_rel_uuids))

        data = {
            "PO": po_names,
            "Počet relácii PO -je_gestor-> KS": po_ks_rel_counts,
            "Počet gestorovaných KS": ks_counts,
            "Počet KS <-vzťah-> AS": ks_as_rel_counts,
            "Počet súvisiacich AS": as_counts,
            "Počet AS <-realizuje- ISVS": isvs_as_rel_counts,
            "Počet súvisiacich ISVS": isvs_counts,
            "Celkový počet entít v skupine": node_counts,
            "Celkový počet vzťahov v skupine": rel_counts,
            "Celkový počet objektov v skupine": obj_counts,
        }

        save_excel(data, "kontajnery.xlsx", sort_by="Celkový počet objektov v skupine", order="descending")


if __name__ == "__main__":
    main()