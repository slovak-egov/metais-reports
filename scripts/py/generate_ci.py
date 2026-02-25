#!/usr/bin/env python3
from __future__ import annotations

import time
import argparse
import json
import os
import re
import uuid
import unicodedata
from urllib.parse import urlsplit
from pathlib import Path
from datetime import date
from typing import Any, Dict, Optional

import requests

from metais.auth.metais_auth import bearer_from_user_pass_plain, DEFAULT_CLIENT_ID

# ----------------------------
# env / base resolution
# ----------------------------

ENV_BASES = {
    "metais": "https://metais.slovensko.sk",
    "metais-prod": "https://metais.slovensko.sk",
    "prod": "https://metais.slovensko.sk",
    "test": "https://metais-test.slovensko.sk",
    "metais-test": "https://metais-test.slovensko.sk",
}

CACHE_TTL_SECONDS = 24 * 3600

def resolve_base(base: str) -> str:
    b = (base or "").strip()
    if not b:
        b = "test"
    b = ENV_BASES.get(b, b)
    if not re.match(r"^https?://", b):
        b = "https://" + b
    return b.rstrip("/")

def _redact_token(tok: str, keep: int = 6) -> str:
    if not tok:
        return "<empty>"
    if len(tok) <= keep * 2:
        return "<redacted>"
    return tok[:keep] + "…" + tok[-keep:]

def _default_cache_dir() -> Path:
    # ~/.cache/metais-ci (Linux-y; works fine under WSL too)
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "metais-ci"

def _safe_key(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "_", s)

def _cache_path(cache_dir: Path, base: str, name: str) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    host = urlsplit(base).netloc or _safe_key(base)
    return cache_dir / f"{name}.{_safe_key(host)}.json"

def _load_cache(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception:
        return None

def _save_cache(path: Path, payload: Dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    tmp.replace(path)

def _is_fresh(cache_obj: Dict[str, Any], ttl_seconds: int = CACHE_TTL_SECONDS) -> bool:
    ts = cache_obj.get("fetched_at_unix")
    if not isinstance(ts, (int, float)):
        return False
    return (time.time() - float(ts)) < ttl_seconds

def _nodia_casefold(s: str) -> str:
    # normalize + strip diacritics + casefold
    s = s.strip()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return s.casefold()

# ----------------------------
# fetchers
# ----------------------------

def fetch_roles(
    session: requests.Session,
    *,
    base: str,
    bearer: str,
    timeout: float = 30.0,
) -> list[Dict[str, Any]]:
    url = f"{base}/api/iam/roles"
    r = session.get(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {bearer}",
        },
        timeout=timeout,
    )
    r.raise_for_status()
    j = r.json()
    if not isinstance(j, list):
        raise RuntimeError(f"Unexpected roles payload (expected list), got: {type(j)}")
    return j

def get_role_map(
    session: requests.Session,
    *,
    base: str,
    cache_dir: Path,
    bearer: Optional[str],
    force_refresh: bool = False,
) -> Dict[str, str]:
    """
    Returns mapping: role_name -> role_uuid
    Cached for 1 day (or until --refresh-cache).
    """
    p = _cache_path(cache_dir, base, "roles")
    cached = _load_cache(p)
    if cached and not force_refresh and _is_fresh(cached):
        mp = cached.get("role_map")
        if isinstance(mp, dict):
            return {str(k): str(v) for k, v in mp.items()}

    if not bearer:
        # no creds; can only use cache
        if cached and isinstance(cached.get("role_map"), dict):
            mp = cached["role_map"]
            return {str(k): str(v) for k, v in mp.items()}
        raise RuntimeError("No bearer available to fetch roles and no cached roles found.")

    roles = fetch_roles(session, base=base, bearer=bearer)
    mp: Dict[str, str] = {}
    for r in roles:
        name = (r or {}).get("name")
        ruuid = (r or {}).get("uuid")
        if name and ruuid:
            mp[str(name)] = str(ruuid)

    _save_cache(p, {"fetched_at_unix": time.time(), "role_map": mp, "count": len(mp)})
    return mp

def resolve_role_uuid(
    role_map: Dict[str, str],
    role_name: str,
) -> str:
    if role_name in role_map:
        return role_map[role_name]
    # try casefold match
    want = role_name.casefold()
    for k, v in role_map.items():
        if k.casefold() == want:
            return v
    # not found
    sample = ", ".join(sorted(list(role_map.keys()))[:10])
    raise RuntimeError(f"Role {role_name!r} not found. Example roles: {sample} ...")

def fetch_citype_schema(
    session: requests.Session,
    *,
    base: str,
    citype: str,
    bearer: Optional[str] = None,
    timeout: float = 30.0,
) -> Dict[str, Any]:
    url = f"{base}/api/types-repo/citypes/citype/{citype}"
    headers = {}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    r = session.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.json()

def _iter_valid_attributes_from_schema(schema: Dict[str, Any]):
    """
    Yield tuples: (attribute_dict, source_label)
      - top-level schema["attributes"] if attribute.valid==true
      - schema["attributeProfiles"] only if profile.valid==true,
        then profile.attributes where attribute.valid==true
    """
    for a in schema.get("attributes", []) or []:
        if a and a.get("valid") is True:
            yield a, "base"

    for prof in schema.get("attributeProfiles", []) or []:
        if not prof or prof.get("valid") is not True:
            continue
        prof_label = prof.get("technicalName") or prof.get("name") or "profile"
        for a in prof.get("attributes", []) or []:
            if a and a.get("valid") is True:
                yield a, f"profile:{prof_label}"

def extract_critical_required(schema: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """
    Returns mapping:
      technicalName -> info dict (source, name, description)
    for attributes whose mandatory.type == "critical"
    respecting:
      - attribute must be valid
      - for profile attributes: profile must be valid
    """
    required: Dict[str, Dict[str, Any]] = {}
    for a, src in _iter_valid_attributes_from_schema(schema):
        mand = (a.get("mandatory") or {})
        if (mand.get("type") == "critical"):
            tech = a.get("technicalName")
            if tech:
                required[tech] = {
                    "source": src,
                    "name": a.get("name"),
                    "description": a.get("description"),
                }
    return required

def apply_defaults_from_schema(attrs: Dict[str, Any], schema: Dict[str, Any]) -> None:
    """
    For any valid attribute (including valid profiles), if attrs doesn't contain it
    and schema provides a non-null defaultValue, set it.
    """
    for a, _src in _iter_valid_attributes_from_schema(schema):
        tech = a.get("technicalName")
        if not tech or tech in attrs:
            continue
        if a.get("defaultValue", None) is not None:
            attrs[tech] = a["defaultValue"]

def validate_required_attrs(attrs: Dict[str, Any], required: Dict[str, Dict[str, Any]]) -> None:
    missing = [k for k in required.keys() if k not in attrs]
    if not missing:
        return

    lines = ["Missing required (mandatory=critical) attributes:"]
    for k in sorted(missing):
        info = required.get(k, {})
        src = info.get("source", "?")
        nm = info.get("name", "")
        lines.append(f"  - {k}  ({src}){(' — ' + nm) if nm else ''}")
    raise RuntimeError("\n".join(lines))


# resolve PO

def fetch_po_list(
    session: requests.Session,
    *,
    base: str,
    report_code: str,
    bearer: Optional[str] = None,
    lang: str = "sk",
    timeout: float = 120.0,
) -> Dict[str, Any]:
    """
    POST /api/report/reports/execute/{code}/type/typ?lang=sk
    payload parameters target=nodes type=PO validOnly=true
    """
    if not report_code:
        raise RuntimeError("Missing report_code (set METAIS_REPORT_NUM_PROD/TEST or pass --report-code).")

    url = f"{base}/api/report/reports/execute/{report_code}/type/typ"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "metais-ci/1.0",
    }
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"

    payload = {"parameters": {"target": "nodes", "type": "PO", "validOnly": "true"}}
    r = session.post(url, params={"lang": lang}, headers=headers, json=payload, timeout=timeout)
    r.raise_for_status()
    j = r.json()
    if not isinstance(j, dict) or "result" not in j:
        raise RuntimeError("Unexpected PO report response shape (missing 'result').")
    return j

def _attrs_to_dict(ci: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for a in (ci.get("attributes") or []):
        if not isinstance(a, dict):
            continue
        n = a.get("name")
        if n:
            out[str(n)] = a.get("value")
    return out

def get_po_indexes(
    session: requests.Session,
    *,
    base: str,
    cache_dir: Path,
    bearer: Optional[str],
    report_code: str,
    lang: str = "sk",
    force_refresh: bool = False,
) -> Dict[str, Dict[str, str]]:
    """
    Returns dict with:
      - ico_to_uuid: str->uuid
      - name_to_uuid: normalized_name->uuid
      - name_raw: uuid->raw_name (for debugging)
    """
    p = _cache_path(cache_dir, base, "po_list")
    cached = _load_cache(p)
    if cached and not force_refresh and _is_fresh(cached):
        if isinstance(cached.get("indexes"), dict):
            return cached["indexes"]

    # fetch (bearer optional; your quick_test worked without auth, but keep bearer if you have it)
    data = fetch_po_list(session, base=base, report_code=report_code, bearer=bearer, lang=lang)
    result = data.get("result") or []
    if not isinstance(result, list):
        raise RuntimeError("PO report 'result' is not a list.")

    ico_to_uuid: Dict[str, str] = {}
    name_to_uuid: Dict[str, str] = {}
    name_raw: Dict[str, str] = {}

    for ci in result:
        if not isinstance(ci, dict):
            continue
        puuid = ci.get("uuid")
        if not puuid:
            continue
        puuid = str(puuid)

        ad = _attrs_to_dict(ci)
        ico = ad.get("EA_Profil_PO_ico")
        nm = ad.get("Gen_Profil_nazov")

        if isinstance(nm, str) and nm.strip():
            name_raw[puuid] = nm
            name_to_uuid[_nodia_casefold(nm)] = puuid

        if ico is not None:
            ico_s = str(ico).strip()
            if ico_s:
                ico_to_uuid[ico_s] = puuid

    indexes = {"ico_to_uuid": ico_to_uuid, "name_to_uuid": name_to_uuid, "name_raw": name_raw}
    _save_cache(p, {"fetched_at_unix": time.time(), "indexes": indexes, "count": len(result)})
    return indexes

def resolve_po_uuid_by_ico(indexes: Dict[str, Dict[str, str]], ico: str) -> str:
    mp = indexes["ico_to_uuid"]
    ico = str(ico).strip()
    if ico in mp:
        return mp[ico]
    raise RuntimeError(f"PO with ICO={ico!r} not found in PO list.")

def resolve_po_uuid_by_name(indexes: Dict[str, Dict[str, str]], query: str) -> str:
    q = _nodia_casefold(query)
    name_to_uuid = indexes["name_to_uuid"]
    name_raw = indexes["name_raw"]

    # exact normalized match
    if q in name_to_uuid:
        return name_to_uuid[q]

    # substring matches
    hits = []
    for norm_name, puuid in name_to_uuid.items():
        if q in norm_name:
            hits.append((norm_name, puuid))

    if not hits:
        raise RuntimeError(f"No PO name match for {query!r}.")

    # If multiple, prefer shortest name (usually most specific exact-ish match)
    hits.sort(key=lambda t: (len(t[0]), t[0]))
    best = hits[0]
    # If ambiguous (many), fail loudly with examples (no guessing silently)
    if len(hits) > 1:
        examples = []
        for norm_name, puuid in hits[:10]:
            raw = name_raw.get(puuid, norm_name)
            examples.append(f"- {raw}  ({puuid})")
        raise RuntimeError(
            f"Owner name {query!r} is ambiguous ({len(hits)} matches). "
            f"Use --owner-ico or be more specific.\n" + "\n".join(examples)
        )
    return best[1]

# ----------------------------
# API calls (generate code, store)
# ----------------------------

def generate_metais_code(
    session: requests.Session,
    *,
    base: str,
    bearer: str,
    citype: str,
    lang: str = "sk",
    timeout: float = 30.0,
) -> str:
    url = f"{base}/api/types-repo/citypes/generate/{citype}"
    r = session.get(
        url,
        params={"lang": lang},
        headers={"Authorization": f"Bearer {bearer}"},
        timeout=timeout,
    )
    r.raise_for_status()
    j = r.json()
    cicode = j.get("cicode")
    if not cicode:
        raise RuntimeError(f"Missing cicode in response keys={list(j.keys())}")
    return str(cicode)

def store_ci(
    session: requests.Session,
    *,
    base: str,
    bearer: str,
    payload: Dict[str, Any],
    lang: str = "sk",
    timeout: float = 60.0,
) -> str:
    url = f"{base}/api/cmdb/store/ci"
    r = session.post(
        url,
        params={"lang": lang},
        headers={
            "Authorization": f"Bearer {bearer}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=timeout,
    )
    r.raise_for_status()
    j = r.json()
    req_id = j.get("requestId")
    if not req_id:
        raise RuntimeError(f"Missing requestId in response keys={list(j.keys())}")
    return str(req_id)


# ----------------------------
# build + run
# ----------------------------

def build_payload(
    *,
    citype: str,
    role_uuid: str,
    owner_po_uuid: str,
    attrs: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "uuid": str(uuid.uuid4()),
        "type": citype,
        "attributes": [{"name": k, "value": v} for k, v in attrs.items()],
        "owner": f"{role_uuid}-{owner_po_uuid}",
    }

def parse_attr_kv(s: str) -> tuple[str, Any]:
    if "=" not in s:
        raise ValueError(f"--attr must be NAME=VALUE, got: {s!r}")
    k, vraw = s.split("=", 1)
    k = k.strip()
    if not k:
        raise ValueError(f"Empty attribute name in: {s!r}")
    vraw = vraw.strip()

    # Allow JSON literals for values (null/true/false/number/object/string)
    # If it doesn't parse as JSON, treat as plain string.
    try:
        v = json.loads(vraw)
    except json.JSONDecodeError:
        v = vraw
    return k, v

def load_attrs(args: argparse.Namespace) -> Dict[str, Any]:
    out: Dict[str, Any] = {}

    if args.attrs_json:
        if args.attrs_json == "-":
            txt = os.sys.stdin.read()
        else:
            with open(args.attrs_json, "r", encoding="utf-8") as f:
                txt = f.read()
        j = json.loads(txt)
        if not isinstance(j, dict):
            raise ValueError("--attrs-json must be a JSON object mapping name->value")
        out.update(j)

    for item in args.attr or []:
        k, v = parse_attr_kv(item)
        out[k] = v

    return out

def main() -> int:
    ap = argparse.ArgumentParser(description="Create a MetaIS CI via CMDB API (with schema-based mandatory checks).")

    ap.add_argument("--citype", required=True, help="CI type technicalName (e.g. InfraSluzba, AS, ISVS, ...)")
    env_group = ap.add_mutually_exclusive_group()
    env_group.add_argument("--test", action="store_true", help="Use MetaIS test (default).")
    env_group.add_argument("--prod", action="store_true", help="Use MetaIS prod.")

    ap.add_argument("--dry-run", action="store_true", help="No POST; print payload + what would be called.")
    ap.add_argument("--lang", default="sk")

    ap.add_argument("--attrs-json", default=None, help="JSON file with {attributeName: value}. Use '-' for stdin.")
    ap.add_argument("--attr", action="append", default=[], help="Add/override attribute NAME=VALUE. VALUE can be JSON.")

    ap.add_argument("--role", default="EA_GARPO", help="Role name (e.g. EA_GARPO). Default: EA_GARPO")

    owner_group = ap.add_mutually_exclusive_group()
    owner_group.add_argument("--owner-uuid", default="", help="PO UUID directly (fast path).")
    owner_group.add_argument("--owner-ico", default="", help="Owner ICO (will resolve to PO UUID via cached PO list).")
    owner_group.add_argument("--owner", default="", help="Owner name substring (case-insensitive, diacritics-insensitive).")

    ap.add_argument("--report-code", default="", help="Override report code used to fetch PO list.")
    ap.add_argument("--cache-dir", default=str(_default_cache_dir()), help="Cache directory for roles/PO list.")
    ap.add_argument("--refresh-cache", action="store_true", help="Force re-download of cached roles/PO list.")

    ap.add_argument("--verify-tls", action="store_true", default=True)
    ap.add_argument("--no-verify-tls", action="store_false", dest="verify_tls")

    ap.add_argument("--verbose", action="store_true", help="More prints (no secrets).")
    args = ap.parse_args()

    base_label = "prod" if args.prod else "test"
    base = resolve_base(os.environ.get("METAIS_BASE", base_label))

    if not (args.owner_uuid or args.owner_ico or args.owner):
        raise SystemExit("Provide one of --owner-uuid / --owner-ico / --owner.")

    attrs = load_attrs(args)
    if not attrs:
        raise SystemExit("No attributes provided. Use --attrs-json and/or --attr NAME=VALUE.")

    # auth (optional for dry-run if creds missing)
    user = os.environ.get("METAIS_USER", "").strip()
    pw = os.environ.get("METAIS_PASS", "")
    client_id = os.environ.get("METAIS_CLIENT_ID", DEFAULT_CLIENT_ID)
    redirect_uri = os.environ.get("METAIS_REDIRECT_URI")

    bearer: Optional[str] = None
    if user and pw:
        bearer = bearer_from_user_pass_plain(
            user, pw,
            base=base,
            client_id=client_id,
            redirect_uri=redirect_uri,  # may be None -> derived in your auth helper
            verify_tls=args.verify_tls,
            verbose=False,
        )
    elif not args.dry_run:
        raise SystemExit("METAIS_USER/METAIS_PASS not set (required for real run).")
    # else dry-run can proceed with placeholders

    s = requests.Session()

    # 1) Fetch schema and enforce mandatory=critical
    schema = fetch_citype_schema(s, base=base, citype=args.citype, bearer=bearer if bearer else None)
    required = extract_critical_required(schema)

    # 2) Apply schema defaults (e.g. Gen_Profil_zdroj defaultValue)
    apply_defaults_from_schema(attrs, schema)

    # 3) Generate code + ref_id + ref_uriPrefix (schema uriPrefix is best)
    uri_prefix = schema.get("uriPrefix")  # e.g. https://data.gov.sk/id/ikt/infrastructure-service
    if not uri_prefix:
        # you can decide to hard-fail for unknown citypes
        raise SystemExit(f"Schema for {args.citype!r} has no uriPrefix; cannot compute Gen_Profil_ref_id reliably.")

    if bearer:
        gen_id = generate_metais_code(s, base=base, bearer=bearer, citype=args.citype, lang=args.lang)
        ref_id = gen_id.split("_")[-1]
    else:
        gen_id = "<GENERATED_CICODE>"
        ref_id = "<REF_ID_FROM_CICODE>"

    cache_dir = Path(args.cache_dir)

    # role uuid (cached; requires bearer unless already cached)
    role_map = get_role_map(
        s, base=base, cache_dir=cache_dir, bearer=bearer, force_refresh=args.refresh_cache
    )
    role_uuid = resolve_role_uuid(role_map, args.role)

    # owner PO uuid
    if args.owner_uuid:
        po_uuid = args.owner_uuid.strip()
    else:
        # need PO list indexes (cached)
        report_code = args.report_code.strip()
        if not report_code:
            report_code = os.environ.get("METAIS_REPORT_NUM_PROD" if args.prod else "METAIS_REPORT_NUM_TEST", "").strip()

        indexes = get_po_indexes(
            s,
            base=base,
            cache_dir=cache_dir,
            bearer=bearer,  # optional; kept if needed
            report_code=report_code,
            lang=args.lang,
            force_refresh=args.refresh_cache,
        )

        if args.owner_ico:
            po_uuid = resolve_po_uuid_by_ico(indexes, args.owner_ico)
        elif args.owner:
            po_uuid = resolve_po_uuid_by_name(indexes, args.owner)
        else:
            raise SystemExit("Provide one of --owner-uuid / --owner-ico / --owner.")

    # mandatory list you said you want (we still validate via schema too)
    # but we *set* the auto ones here:
    attrs["Gen_Profil_kod_metais"] = gen_id
    attrs["Gen_Profil_ref_id"] = f"{uri_prefix}/{ref_id}"

    # optional convenience: if name missing, default to citype_YYYY-MM-DD (but schema will still enforce)
    attrs.setdefault("Gen_Profil_nazov", f"{args.citype}_{date.today().isoformat()}")

    # 4) Validate required attributes (base + valid profiles only)
    validate_required_attrs(attrs, required)

    # 5) Build payload
    payload = build_payload(
        citype=args.citype,
        role_uuid=role_uuid,
        owner_po_uuid=po_uuid,
        attrs=attrs
    )

    if args.dry_run:
        print("DRY RUN (no POST)")
        print("base:", base)
        print("would GET schema:", f"{base}/api/types-repo/citypes/citype/{args.citype}")
        if bearer:
            print("would GET cicode:", f"{base}/api/types-repo/citypes/generate/{args.citype}?lang={args.lang}")
        else:
            print("would GET cicode: (skipped; no creds in env)")
        print("would POST:", f"{base}/api/cmdb/store/ci?lang={args.lang}")
        if args.verbose:
            print("client_id:", client_id)
            print("redirect_uri:", redirect_uri or f"{base}/auth-success")
            print("user:", user or "<unset METAIS_USER>")
            print("bearer:", _redact_token(bearer or ""))
        print("\nPayload:")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    # real call
    assert bearer is not None
    req_id = store_ci(s, base=base, bearer=bearer, payload=payload, lang=args.lang)
    print(req_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
