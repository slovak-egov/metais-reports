from pathlib import Path
from packed_reader import PackedStore
from tqdm import tqdm
import time
from contextlib import contextmanager

@contextmanager
def timed(label: str):
    start = time.perf_counter()
    yield
    end = time.perf_counter()
    print(f"[time] {label}: {end - start:.3f} s")

base = Path("output/05-12-2025/packed")
store = PackedStore(base)

print("Node types:", store.list_types())
print("Relation types:", store.relations.list_relation_types())

ks = store.open_type("KS")

uuids = [
    "68323400-b5a3-4c0e-ae00-028ea82b034c",
    "78dab3c0-56ac-40f6-9cff-ad3f1e12cdab",
    "7f3e3bd3-d38a-476b-9f24-2503c119ad19",
    "1fc18229-961b-43a3-8b1e-48a05563e676",
    "387b51da-d396-49ec-86a2-eb46fb420e98",
    "320ba66e-49a0-41ec-ad8c-c72e73b61e1a",
    "f2ae0432-f907-4bde-aa3b-760f9d3be37b",
    "b26af04e-10dd-440e-b43a-0e51bf09f061",
    "7b5e14d4-b03a-412d-b9ec-c04cd040a80c",
    "2773eb3a-21eb-4d32-88db-d874e350b91e"
]

with timed("UUID -> record lookup block"):
    for i in range(1000):
        for uuid in uuids:
            rec_idx = ks.find_record_index_by_uuid(uuid)
            if rec_idx is None:
                print("UUID not found")
            else:
                name = ks.get_attr_value(rec_idx, "Gen_Profil_nazov")
                code = ks.get_attr_value(rec_idx, "Gen_Profil_kod_metais")
                #print(f"KS[{rec_idx}] {uuid}")
                #print("  Gen_Profil_kod_metais:", code)
                #print("  Gen_Profil_nazov     :", name)

with timed("iterate over a couple KS entities"):
    for i, (uuid_str, attrs) in enumerate(ks.iter_records(["Gen_Profil_kod_metais"])):
        print(uuid_str, "->", attrs.get("Gen_Profil_kod_metais"))
        if i >= 4:
            break

wanted = ["Gen_Profil_kod_metais", "Gen_Profil_nazov"]

with timed("Sequential iter_records"):
    for uuid_str, attrs in tqdm(
        ks.iter_records(attr_names=wanted),
        total=ks.record_count,
        desc="Scanning KS"
    ):
        code = attrs.get("Gen_Profil_kod_metais")
        name = attrs.get("Gen_Profil_nazov")
        # do something with uuid, code, name...
        #print(uuid_str, code, name)

po = store.open_type("PO")

with timed("Some PO + relations"):
    # grab first PO uuid
    first_po_uuid, _ = next(po.iter_records())
    print("Sample PO:", first_po_uuid)

    # find KS for which this PO is gestors
    ks_neighbors = store.relations.neighbors_from("PO_je_gestor_KS", first_po_uuid)
    print("KS where PO is gestor (count):", len(ks_neighbors))
    for u in ks_neighbors[:10]:
        print("  KS:", u)

store.close()