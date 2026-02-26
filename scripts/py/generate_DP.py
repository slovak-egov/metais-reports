import os
from metais.store.ci_client import MetaISCIClient

c = MetaISCIClient(
    base="test",
    verbose=True,
    report_code=os.environ.get("METAIS_REPORT_NUM_TEST", ""),
)

c.set_role(by_name="REFID_URI_DEF")
c.set_owner(by_name="ministerstvo investicii")

batch_attrs = [{
    "Gen_Profil_nazov": "Dátum zaúčtovania",
    "Gen_Profil_anglicky_nazov": "Posting date",
    "Gen_Profil_zdroj": "c_zdroj.9",
    "Gen_Profil_RefID_stav_registracie": "c_stav_registracie.1",
    "Profil_DatovyPrvok_typ_datoveho_prvku": "c_typ_dp.1",
    "Profil_DatovyPrvok_kod_datoveho_prvku": "https://data.gov.sk/def/ontology/finance/accounted",
}]

mirri_uuid = c.find_ci_uuid("PO", by_name="ministerstvo investicii", pick_if_ambiguous=True)

results = c.store_ci("DatovyPrvok", batch_attrs, dry_run=False, check_duplicates=True, continue_on_error=True)

for r in results:
    if r.status not in ("success", "existing") or not r.entity_uuid:
        continue

    c.store_rel(
        reltype="PO_je_gestor_DatovyPrvok",
        start_type="PO",
        end_type="DatovyPrvok",
        start_uuid=mirri_uuid,
        end_uuid=r.entity_uuid, # either the newly added entity or best match found entity.
        dry_run=True,
        check_duplicates=True,
    )
