import os
import json
from metais.store.ci_client import MetaISCIClient

uuid_to_invalidate = "5aea2e0f-07c3-47c9-aac7-528520c41e93"

c = MetaISCIClient(
    base="test",
    verbose=True,
    report_code=os.environ.get("METAIS_REPORT_NUM_TEST", ""),
)

c.set_role(by_name="REFID_URI_DEF")
c.set_owner(by_name="ministerstvo investicii")

res = c.invalidate_cis(uuid_to_invalidate, comment="Test invalidácie prvku cez cmdb/invalidate/ci", dry_run=False, audit_history=True)

print(json.dumps(res, ensure_ascii=False, indent=2))
