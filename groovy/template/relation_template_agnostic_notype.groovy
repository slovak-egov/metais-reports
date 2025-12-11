def secPerDay = 24L * 60L * 60L

def toIso8601 = { long ms ->
    // Split into seconds + milliseconds
    long totalSec = ms / 1000
    long milli    = ms % 1000

    // Days since epoch
    long days = totalSec / secPerDay
    long secOfDay = totalSec % secPerDay

    // Compute hour/min/sec
    int hour = (int)(secOfDay / 3600)
    int min  = (int)((secOfDay % 3600) / 60)
    int sec  = (int)(secOfDay % 60)

    // Compute date: year-month-day from days since 1970-01-01
    int year = 1970
    long d = days

    // helper closures (no implicit imports)
    def isLeap = { int y -> (y % 4 == 0 && y % 100 != 0) || (y % 400 == 0) }
    def daysInYear = { int y -> isLeap(y) ? 366 : 365 }
    def daysInMonth = { int y, int m ->
        switch (m) {
            case 1: return 31
            case 2: return isLeap(y) ? 29 : 28
            case 3: return 31
            case 4: return 30
            case 5: return 31
            case 6: return 30
            case 7: return 31
            case 8: return 31
            case 9: return 30
            case 10: return 31
            case 11: return 30
            case 12: return 31
        }
    }

    // subtract whole years
    while (true) {
        int dy = daysInYear(year)
        if (d >= dy) {
            d -= dy
            year += 1
        } else {
            break
        }
    }

    // now compute month
    int month = 1
    while (true) {
        int dm = daysInMonth(year, month)
        if (d >= dm) {
            d -= dm
            month += 1
        } else {
            break
        }
    }

    // remaining days → day of month
    int day = (int)d + 1

    // zero-pad helper
    def zp = { v, n ->
        String s = v.toString()
        while (s.length() < n) s = "0" + s
        return s
    }

    return zp(year,4) + "-" +
           zp(month,2) + "-" +
           zp(day,2) + "T" +
           zp(hour,2) + ":" +
           zp(min,2) + ":" +
           zp(sec,2) + "." +
           zp(milli,3)
}

def qi_rel = qi("rel")
def qi_src = qi("src")
def qi_tgt = qi("tgt")

possiblePropNames = [
    "Profil_Rel_FormularKS_vyzaduje_zep",
    "Profil_Rel_FormularKS_typ_formulara_upvs",
    "KS_profil_UPVS2_partner_type",
    "KS_profil_UPVS2_supported_os",
    "CMDB_HISTORY_REL_PROFIL_ACTIONS",
    "Gen_Profil_Rel_poznamka",
    "KS_Profil_PO_UPVS_spatvzatie",
    "KS_profil_PO_UPVS_kontakt_na_adresata",
    "ReferenceRegister_usedBy_AS_Profile_note",
    "KS_profil_PO_alt_popis",
    "KS_Profil_PO_UPVS_do",
    "KS_profil_UPVS2_responsive_application",
    "KS_Profil_PO_UPVS_typy_priloh",
    "ReferenceRegister_accessedFor_PO_Profile_note",
    "CMDB_HISTORY_REL_PROFIL_VALID_TO",
    "KS_profil_PO_UPVS_Formular_ID",
    "KS_profil_PO_pristupove_miesto",
    "Profil_Platnost_platnost_do",
    "KS_Profil_PO_UPVS_kod_sluzby_eKolok",
    "Profil_OEOpravnenie_typ_opravnenia",
    "KS_Profil_PO_UPVS_sign_types",
    "KS_profil_PO_hromadny_import_sluzieb",
    "KS_Profil_PO_UPVS_platba",
    "Profil_OEOpravnenie_pravny_zaklad_kod",
    "Profil_Rel_FazaZivotnehoCyklu_datum_zacatia",
    "KS_Profil_PO_UPVS_responsive_application",
    "KS_profil_PO_UPVS_listinne_tlacivo",
    "Profil_Rel_FormularKS_Profil_Rel_FormularKS_typ_spoplatnenia",
    "KS_profil_PO_dostupnost_sluzby",
    "Profil_Rel_FormularKS_Profil_Rel_FormularKS_poziadavka_na_kontrolu_casovej_peciatky",
    "KS_Profil_PO_UPVS_KS_Profil_PO_UPVS_sign_types_new",
    "KS_profil_UPVS2_sign_types",
    "CMDB_HISTORY_REL_PROFIL_ACTION_BY",
    "Profil_Rel_Princip_sposob_plnenia",
    "KS_profil_PO_specificka_evidencia",
    "KS_Profil_PO_UPVS_KS_Profil_PO_UPVS_nefunkcnost_do",
    "KS_Profil_PO_UPVS_KS_Profil_PO_UPVS_url_info_eng",
    "KS_Profil_PO_UPVS_KS_Profiil_UPVS_neakceptovatelny_typ_osoby",
    "KS_profil_UPVS2_kod_spoplatnenia",
    "KS_profil_PO_iban",
    "Profil_UPVS_POKS_alt_popis",
    "KS_Profil_PO_UPVS_stav_publikovania",
    "KS_profil_PO_UPVS_Koncova_sluzba_ID",
    "Profil_OEOpravnenie_pravny_zaklad",
    "Profil_Rel_FormularKS_typy_priloh",
    "KS_profil_UPVS2_ks_pristupove_miesto",
    "KS_profil_UPVS2_native_mobile_application",
    "KS_Profil_PO_UPVS_KS_Profil_PO_UPVS_nefunkcnost_od",
    "CMDB_HISTORY_REL_PROFIL_VALID_FROM",
    "KS_Profil_PO_UPVS_identity_type_detail",
    "Profil_UPVS_POKS_url_info",
    "KS_Profil_PO_UPVS_supported_os",
    "Profil_Rel_FormularKS_vyzaduje_autentifikaciu",
    "Profil_Rel_FormularKS_Profil_Rel_FormularKS_skupina_pouzivatelov_new",
    "Profil_Rel_FazaZivotnehoCyklu_datum_ukoncenia",
    "Clarity_Profil_Projekt_clarity_id",
    "Profil_Rel_FormularKS_skupina_pouzivatelov",
    "EA_Profil_Rel_typ_vazby",
    "KS_profil_PO_platba",
    "KS_Profil_PO_UPVS_ks_pristupove_miesto",
    "KS_Profil_PO_UPVS_KS_Profil_PO_UPVS_KS_profil_PO_UPVS_referencne_cislo_podania_new",
    "Profil_UPVS_POKS_od",
    "Profil_OEOpravnenie_atribut_objektu_evidencie",
    "Profil_Rel_FormularKS_Profil_Rel_FormularKS_sposob_autorizacie",
    "KS_profil_UPVS2_identity_type",
    "KS_Profil_PO_UPVS_od",
    "KS_profil_PO_allowed_send_end_org",
    "KS_profil_PO_UPVS_hromadny_import_sluzieb",
    "KS_profil_PO_kod_sluzby_eKolok",
    "KS_profil_UPVS2_spatvzatie",
    "KS_profil_PO_UPVS_allowed_send_end_org",
    "KS_Profil_PO_UPVS_KS_profil_UPVS_odkazy_na_dalsie_informacie",
    "KS_profil_PO_kontakt_na_adresata",
    "KS_Profil_PO_UPVS_identity_type",
    "KS_Profil_PO_UPVS_native_mobile_application",
    "KS_profil_PO_listinne_tlacivo",
    "Profil_Rel_ISVS_vyuziva_sluzbu_Profil_Rel_ISVS_vyuziva_sluzbu",
    "Profil_Rel_ISVS_vyuziva_sluzbu_CSP_kod_projektu",
    "Profil_Platnost_platnost_od",
    "KS_Profil_PO_UPVS_partner_type",
    "Profil_OEOpravnenie_pravny_ucel",
    "KS_Profil_PO_UPVS_KS_Profil_PO_UPVS_KS_Profil_PO_UPVS_KS_profil_PO_UPVS_referencne_cislo_podania_v2",
    "KS_Profil_PO_UPVS_iban",
    "KS_Profil_PO_UPVS_url",
    "KS_profil_UPVS2_identity_type_detail",
    "KS_Profil_PO_UPVS_KS_Profil_PO_UPVS_typ_sluzby_UPVS",
    "Profil_Rel_FormularKS_Profil_Rel_FormularKS_povolit_prilohy",
    "KS_profil_UPVS2_ks_schranka_ovm",
    "Profil_Rel_FormularKS_autentifikacia",
    "KS_Profil_PO_UPVS_url_info",
    "KS_Profil_PO_UPVS_KS_Profil_PO_UPVS_KS_profil_UPVS_opis_dalsich_krokov",
    "Profil_UPVS_POKS_typ_spoplatnenia",
    "KS_Profil_PO_UPVS_KS_profil_PO_UPVS_referencne_cislo_podania",
    "Profil_UPVS_POKS_do",
    "Gen_Profil_Rel_kod_metais",
    "KS_profil_UPVS2_url",
    "KS_Profil_PO_UPVS_ks_schranka_ovm",
    "KS_profil_PO_alt_nazov",
    "KS_Profil_PO_UPVS_kod_spoplatnenia",
    "Profil_Rel_FormularKS_Profil_Rel_FormularKS_kod_spoplatnenia",
    "KS_Profil_PO_UPVS_typ_spoplatnenia",
]

def timestampPropNames = [
    "KS_Profil_PO_UPVS_KS_Profil_PO_UPVS_nefunkcnost_do",
    "KS_Profil_PO_UPVS_od",
    "KS_Profil_PO_UPVS_KS_Profil_PO_UPVS_nefunkcnost_od",
    "Profil_UPVS_POKS_do",
    "Profil_Rel_FazaZivotnehoCyklu_datum_ukoncenia",
    "Profil_UPVS_POKS_od",
    "Profil_Platnost_platnost_od",
    "Profil_Platnost_platnost_do",
    "KS_Profil_PO_UPVS_do",
    "Profil_Rel_FazaZivotnehoCyklu_datum_zacatia",
] as Set

// these are known for sho
def baseReturnProps = [
    prop("source",            qi_src.prop("\$cmdb_id")),
    prop("target",            qi_tgt.prop("\$cmdb_id")),
    prop("relUuid",           qi_rel.prop("\$cmdb_id")),
    prop("sourceType",        qi_src.prop("\$cmdb_typeName")),
    prop("targetType",        qi_tgt.prop("\$cmdb_typeName")),
    prop("relCreatedAt",      qi_rel.prop("\$cmdb_createdAt")),
    prop("relLastModifiedAt", qi_rel.prop("\$cmdb_lastModifiedAt")),
    prop("relCreatedBy",      qi_rel.prop("\$cmdb_createdBy")),
    prop("relLastModifiedBy", qi_rel.prop("\$cmdb_lastModifiedBy")),
    prop("relState",          qi_rel.prop("\$cmdb_state")),
    prop("relOwner",          qi_rel.prop("\$cmdb_owner")),
]

// try all possible prop names
def attrReturnProps = possiblePropNames.collect { pn ->
    prop(pn, qi_rel.prop(pn))
}

// combine
def allReturnProps = baseReturnProps + attrReturnProps

// return res
def q = match(
    path().node(qi_src).rel(qi_rel).node(qi_tgt)
)
.returns(*allReturnProps)
.orderBy(qi_rel.prop("\$cmdb_createdAt"), OrderDirection.ASC)
.limit(__LIMIT__).offset(__OFFSET__)

def res = Neo4j.execute(q)

def rows = res.data.collect { row ->
    // build attributes list from only non-null returned columns
    def attributes = []
    possiblePropNames.each { pn ->
        def v = row[pn]
        if (v != null) {
            def outVal = v
            if (pn in timestampPropNames) {
                // convert only if numeric, just in case
                try {
                    outVal = toIso8601(v as long)
                } catch (ignored) {
                    // if it's not numeric, leave it as is
                }
            }

            attributes << [
                name : pn,
                value: outVal
            ]
        }
    }
    [
        type: "undefined",
        uuid: row.relUuid,
        startUuid: row.source,
        endUuid: row.target,
        startType: row.sourceType,
        endType: row.targetType,
        attributes: attributes,
        metaAttributes: [
            owner:          row.relOwner,
            state:          row.relState,
            createdBy:      row.relCreatedBy,
            createdAt:      toIso8601(row.relCreatedAt),
            lastModifiedBy: row.relLastModifiedBy,
            lastModifiedAt: toIso8601(row.relLastModifiedAt),
        ]
    ]
}

def total = rows.size()
def result = new ReportResult("RAW", rows, total)

def perPage = (__LIMIT__ as int)
def page    = (__OFFSET__ / __LIMIT__) as int

result.perPage = perPage
result.page    = page

return result