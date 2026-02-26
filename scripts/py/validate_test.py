import os
import json
from metais.store.ci_client import MetaISCIClient

uuid_to_validate = "5aea2e0f-07c3-47c9-aac7-528520c41e93"

c = MetaISCIClient(
    base="test",
    verbose=True,
    report_code=os.environ.get("METAIS_REPORT_NUM_TEST", ""),
)

c.set_role(by_name="REFID_URI_DEF")
c.set_owner(by_name="ministerstvo investicii")

res_ci = c.recycle_cis(uuid_to_validate, dry_run=False, audit_history=True)

neighbors = c.fetch_neighbors_with_rels(uuid_to_validate, states=("DRAFT", "INVALIDATED"))

rel_uuids = c._extract_relation_uuids_from_neighbors_payload(
    neighbors,
    only_states=("INVALIDATED",),
)

res_rels = {"skipped": "no invalidated relations found"} if not rel_uuids else c.recycle_rels(
    rel_uuids,
    dry_run=False,
    audit_history=True,
)

print(json.dumps({
    "validate_ci": res_ci,
    "neighbor_relations_invalidated": rel_uuids,
    "validate_neighbor_relations": res_rels,
}, ensure_ascii=False, indent=2))
