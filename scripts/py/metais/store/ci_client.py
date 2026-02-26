#!/usr/bin/env python3
# metais_ci_client.py
from __future__ import annotations

import getpass
import json
import os
import re
import sys
import time
import traceback
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Iterable
from urllib.parse import urlsplit

import requests

from metais.auth.metais_auth import DEFAULT_CLIENT_ID, bearer_from_user_pass_plain

# ----------------------------
# constants
# ----------------------------

BASE_TEST = "https://metais-test.slovensko.sk"
BASE_PROD = "https://metais.slovensko.sk"
ENV_BASES = {
    "metais": BASE_PROD,
    "metais-prod": BASE_PROD,
    "prod": BASE_PROD,
    "test": BASE_TEST,
    "metais-test": BASE_TEST,
}

CACHE_TTL_SECONDS = 24 * 3600

AUTO_KEYS = {"Gen_Profil_kod_metais", "Gen_Profil_ref_id"}


# ----------- #
# dataclasses #
# ----------- #

@dataclass(frozen=True)
class CodeReservation:
    cicode: str
    path: Path  # *.inflight.json

@dataclass
class StoreResult:
    status: str  # "success" | "fail" | "skipped" | "dry-run"
    citype: str
    entity_uuid: str | None = None
    entity_url: str | None = None
    request_id: str | None = None
    payload: Dict[str, Any] | None = None
    reservation: Dict[str, Any] | None = None
    error: Dict[str, Any] | None = None
    log_path: str | None = None

@dataclass
class RelationResult:
    status: str  # "success" | "fail" | "existing" | "dry-run"
    reltype: str
    relation_uuid: str | None = None
    request_id: str | None = None
    payload: Dict[str, Any] | None = None
    error: Dict[str, Any] | None = None
    log_path: str | None = None

@dataclass
class _ExistingIndex:
    fetched_at_unix: float
    # attribute_name -> normalized_value -> list[ci_uuid]
    by_attr: Dict[str, Dict[str, list[str]]]
    # ci_uuid -> raw attrs dict (for prompts)
    ci_attrs: Dict[str, Dict[str, Any]]

@dataclass
class _ExistingRelIndex:
    fetched_at_unix: float
    # (startUuid, endUuid) -> relation_uuid
    by_pair: Dict[tuple[str, str], str]

# ------------------------- #
# module-level tiny helpers #
# ------------------------- #

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _safe_key(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "_", s)

def _redact_token(tok: str, keep: int = 6) -> str:
    if not tok:
        return "<empty>"
    if len(tok) <= keep * 2:
        return "<redacted>"
    return tok[:keep] + "…" + tok[-keep:]

def _nodia_casefold(s: str) -> str:
    s = s.strip()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return s.casefold()

def _norm_val(v: Any) -> str:
    """
    Normalization used for duplicate checks.
    - strings: strip, accentless+casefold
    - others: stable stringification via json if possible else str()
    """
    if v is None:
        return "null"
    if isinstance(v, str):
        return _nodia_casefold(v)
    try:
        return _nodia_casefold(json.dumps(v, ensure_ascii=False, sort_keys=True))
    except Exception:
        return _nodia_casefold(str(v))

def _is_auth_http_error(e: BaseException) -> bool:
    if isinstance(e, requests.exceptions.HTTPError):
        resp = getattr(e, "response", None)
        if resp is not None and getattr(resp, "status_code", None) in (401, 403):
            return True
    return False

def _ask_yes_no(prompt: str, *, default: bool = False) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    while True:
        ans = input(prompt + suffix).strip().casefold()
        if ans == "":
            return default
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        print("Please answer y or n.", file=sys.stderr)

def resolve_base(base: str) -> str:
    b = (base or "").strip()
    if not b:
        b = "test"
    b = ENV_BASES.get(b, b)
    if not re.match(r"^https?://", b):
        b = "https://" + b
    return b.rstrip("/")

def _default_cache_dir() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "metais-ci"

def resolve_cache_dir(path_str: str) -> Path:
    p = Path(path_str)
    if p.is_absolute():
        return p
    try:
        from metais.common.project_root import find_project_root
        root = find_project_root(Path(__file__))
        return (root / p).resolve()
    except Exception:
        return p.resolve()

# --------------------------- #
# terminal formatting helpers #
# --------------------------- #

_ANSI_RESET = "\033[0m"
_ANSI_RED = "\033[31m"
_ANSI_ORANGE = "\033[38;5;208m"
_ANSI_ORANGE_DIM = "\033[2m\033[38;5;208m"
_ANSI_BOLD = "\033[1m"
_ANSI_DIM = "\033[2m"

def _supports_color(stream) -> bool:
    # Good enough for WSL / Linux terminals.
    try:
        return hasattr(stream, "isatty") and stream.isatty()
    except Exception:
        return False

def _c(s: str, code: str, *, enabled: bool) -> str:
    if not enabled:
        return s
    return f"{code}{s}{_ANSI_RESET}"

def _fmt_val(v: Any, *, max_len: int = 48) -> str:
    if v is None:
        s = "null"
    elif isinstance(v, str):
        s = v
    else:
        try:
            s = json.dumps(v, ensure_ascii=False, sort_keys=True)
        except Exception:
            s = str(v)

    s = s.replace("\r", "").replace("\n", "⏎")
    if len(s) > max_len:
        s = s[: max_len - 1] + "…"
    return s

# ----------------- #
# main client class #
# ----------------- #

class MetaISCIClient:
    """
    Stateful MetaIS CI creation client.

    Public API:
      - set_role(by_name=..., by_uuid=...)
      - set_owner(by_name=..., by_ico=..., by_uuid=...)
      - store_ci(citype, attrs, ...)

    Notes:
      - bearer is stored in-memory only
      - schema is cached on disk + in-memory
      - duplicate check (report-based) is fetched fresh at the start of each store_ci call
      - code reservation is persisted to disk (pending/inflight/used)
    """

    def __init__(
        self,
        *,
        base: str = "test",
        cache_dir: str | Path | None = None,
        client_id: str | None = None,
        redirect_uri: str | None = None,
        verify_tls: bool = True,
        verbose: bool = False,
        lang: str = "sk",
        report_code: str = "",
        timeout_roles: float = 30.0,
        timeout_schema: float = 30.0,
        timeout_report: float = 120.0,
        timeout_store: float = 60.0,
    ) -> None:
        self.base = resolve_base(base)
        self.cache_dir = resolve_cache_dir(str(cache_dir)) if cache_dir else self._pick_default_cache_dir()
        self.client_id = (client_id or os.environ.get("METAIS_CLIENT_ID") or DEFAULT_CLIENT_ID).strip()
        self.redirect_uri = redirect_uri if redirect_uri is not None else os.environ.get("METAIS_REDIRECT_URI")
        self.verify_tls = bool(verify_tls)
        self.verbose = bool(verbose)
        self.lang = lang

        # report code used for:
        # - resolving owners via PO report
        # - duplicate checks via report (type=citype/reltype)
        self.report_code = report_code.strip()

        self.timeout_roles = float(timeout_roles)
        self.timeout_schema = float(timeout_schema)
        self.timeout_report = float(timeout_report)
        self.timeout_store = float(timeout_store)

        self.session = requests.Session()
        self.session.verify = self.verify_tls

        # auth + identity state
        self._bearer: Optional[str] = None
        self._last_auth_user: Optional[str] = None

        # required state before storing
        self.role_uuid: Optional[str] = None
        self.owner_uuid: Optional[str] = None

        # in-memory caches (per run)
        self._role_map: Optional[Dict[str, str]] = None
        self._schema_cache: Dict[str, Dict[str, Any]] = {} # citype -> schema
        self._existing_cache: Dict[str, _ExistingIndex] = {} # citype -> index
        self._rel_cache: Dict[tuple[str, str, str], _ExistingRelIndex] = {} # keyed by (reltype, startType, endType)

    # --------------------- #
    # public config setters #
    # --------------------- #

    def set_role(self, *, by_name: str | None = None, by_uuid: str | None = None, force_refresh: bool = False) -> str:
        self._require_exactly_one("set_role", by_name=by_name, by_uuid=by_uuid)
        if by_uuid is not None:
            self.role_uuid = by_uuid.strip()
            if not self.role_uuid:
                raise ValueError("set_role(by_uuid=...) cannot be empty.")
            return self.role_uuid

        assert by_name is not None
        role_map = self._get_role_map(force_refresh=force_refresh)
        self.role_uuid = self._resolve_role_uuid(role_map, by_name)
        return self.role_uuid

    def set_owner(
        self,
        *,
        by_name: str | None = None,
        by_ico: str | None = None,
        by_uuid: str | None = None,
        report_code: str | None = None,
        force_refresh: bool = False,
    ) -> str:
        self._require_exactly_one("set_owner", by_name=by_name, by_ico=by_ico, by_uuid=by_uuid)

        if by_uuid is not None:
            self.owner_uuid = by_uuid.strip()
            if not self.owner_uuid:
                raise ValueError("set_owner(by_uuid=...) cannot be empty.")
            return self.owner_uuid

        rep = (report_code or self.report_code or self._env_report_code()).strip()
        if not rep:
            raise RuntimeError(
                "Missing report_code for owner resolution. "
                "Set it in client(report_code=...), or env METAIS_REPORT_NUM_TEST/PROD."
            )

        indexes = self._get_po_indexes(report_code=rep, force_refresh=force_refresh)

        if by_ico is not None:
            self.owner_uuid = self._resolve_po_uuid_by_ico(indexes, by_ico)
        else:
            assert by_name is not None
            self.owner_uuid = self._resolve_po_uuid_by_name(indexes, by_name)

        return self.owner_uuid

    # ------------------ #
    # public main action #
    # ------------------ #

    def store_ci(
        self,
        citype: str,
        attrs: Dict[str, Any] | list[Dict[str, Any]] | list[list[Dict[str, Any]]] | list[Dict[str, Any]],
        *,
        dry_run: bool = False,
        check_duplicates: bool = True,
        skip_duplicate_prompt: bool = False,
        report_code: str | None = None,
        force_refresh_schema: bool = False,
        continue_on_error: bool = False,
    ) -> StoreResult | list[StoreResult]:
        """
        Store one or many CIs.

        attrs:
          - dict: single CI attributes {name: value}
          - list[dict]: batch of {name:value}
          - also accepts "MetaIS style" list of {"name":..., "value":...} by auto-normalization

        Duplicate checking:
          - if enabled: fetch current list of CIs of that citype via report execute, index by attributes
          - if any attribute matches (excluding AUTO_KEYS), prompt y/n unless skip_duplicate_prompt=True

        Returns:
          - StoreResult for single input
          - list[StoreResult] for batch
        """
        citype = (citype or "").strip()
        if not citype:
            raise ValueError("citype cannot be empty.")

        self._require_ready_to_store()

        items = self._normalize_batch_attrs(attrs)
        is_batch = isinstance(attrs, list) and not self._is_metais_attr_list(attrs)

        # Fetch schema (cached on disk + mem)
        schema = self._get_citype_schema_cached(citype, force_refresh=force_refresh_schema)

        # Fresh duplicate snapshot at start of job (per call)
        existing: Optional[_ExistingIndex] = None
        if check_duplicates:
            rep = (report_code or self.report_code or self._env_report_code()).strip()
            if not rep:
                raise RuntimeError(
                    "Duplicate check enabled but no report_code is set. "
                    "Provide report_code=..., or set client(report_code=...), or disable check_duplicates."
                )
            existing = self._fetch_existing_ci_index(citype=citype, report_code=rep)
            # keep it for the duration of this run (and update it as we add)
            self._existing_cache[citype] = existing

        results: list[StoreResult] = []

        for i, item_attrs in enumerate(items):
            try:
                res = self._store_one(
                    citype=citype,
                    schema=schema,
                    attrs=item_attrs,
                    dry_run=dry_run,
                    existing=existing,
                    skip_duplicate_prompt=skip_duplicate_prompt,
                )
                results.append(res)

                # if we successfully added, update in-memory duplicate index so later items see it
                if existing is not None and res.status == "success" and res.entity_uuid and res.payload:
                    self._existing_index_add(existing, ci_uuid=res.entity_uuid, attrs=item_attrs)

            except Exception as e:
                if continue_on_error:
                    results.append(
                        StoreResult(
                            status="fail",
                            citype=citype,
                            error={
                                "type": type(e).__name__,
                                "message": str(e),
                                "traceback": traceback.format_exc(),
                            },
                        )
                    )
                    continue
                raise

        if is_batch:
            return results
        return results[0]

    def _resolve_ci_uuid_by_name(
        self,
        *,
        citype: str,
        query: str,
        report_code: str,
        max_examples: int = 3,
        force_refresh: bool = False,
        pick_if_ambiguous: bool = False,
        stream=None,
    ) -> str:
        if stream is None:
            stream = sys.stderr

        q = _nodia_casefold(query)

        # Special-case PO: use existing PO index logic (and cached file)
        if citype == "PO":
            indexes = self._get_po_indexes(report_code=report_code, force_refresh=force_refresh)
            # tweak your _resolve_po_uuid_by_name to accept max_examples if you want;
            # otherwise reuse it and it will show more than 3.
            return self._resolve_po_uuid_by_name(indexes, query)

        # Otherwise: reuse existing cache if already loaded, else fetch & cache once
        idx = self._existing_cache.get(citype)
        if idx is None or force_refresh:
            idx = self._fetch_existing_ci_index(citype=citype, report_code=report_code)
            self._existing_cache[citype] = idx

        # Build candidates for "exact normalized" matches:
        exact_norm: list[tuple[str, str]] = []  # (raw_name, uuid)
        hits: list[tuple[str, str]] = []        # substring matches

        for uu, ad in idx.ci_attrs.items():
            nm = ad.get("Gen_Profil_nazov")
            if not isinstance(nm, str) or not nm.strip():
                continue
            nn = _nodia_casefold(nm)
            if nn == q:
                exact_norm.append((nm, uu))
            elif q in nn:
                hits.append((nm, uu))

        if len(exact_norm) == 1:
            return exact_norm[0][1]

        if len(exact_norm) > 1:
            # show URLs
            lines = []
            for nm, uu in exact_norm[:max(10, max_examples)]:
                url = f"{self.base}/ci/{citype}/{uu}"
                lines.append(f"- {nm} ({uu}) {url}")

            if pick_if_ambiguous and hasattr(sys.stdin, "isatty") and sys.stdin.isatty():
                # interactive picker: show up to 10
                cand = exact_norm[:10]
                print(f"Ambiguous {citype} name {query!r}: {len(exact_norm)} exact normalized matches.", file=stream)
                for i, (nm, uu) in enumerate(cand, 1):
                    url = f"{self.base}/ci/{citype}/{uu}"
                    print(f"  [{i}] {nm} ({uu}) {url}", file=stream)

                ans = input(f"Pick 1-{len(cand)} (Enter to abort): ").strip()
                if ans == "":
                    raise RuntimeError("Aborted by user (ambiguous match).")
                try:
                    k = int(ans)
                    if not (1 <= k <= len(cand)):
                        raise ValueError
                except ValueError:
                    raise RuntimeError(f"Invalid selection {ans!r}.")
                return cand[k - 1][1]

            raise RuntimeError(
                f"Ambiguous {citype} name {query!r}: {len(exact_norm)} exact normalized matches.\n"
                + "\n".join(lines[:max_examples])
                + ("\n..." if len(exact_norm) > max_examples else "")
            )

        # Substring matches: must be unambiguous
        hits.sort(key=lambda t: (len(_nodia_casefold(t[0])), _nodia_casefold(t[0])))

        if len(hits) == 1:
            return hits[0][1]

        examples = [f"- {nm} ({uu})" for nm, uu in hits[:max_examples]]
        raise RuntimeError(
            f"Ambiguous {citype} name {query!r}: {len(hits)} substring matches.\n" + "\n".join(examples)
        )

    def _resolve_ci_uuid_by_ico(
        self,
        *,
        citype: str,
        ico: str,
        report_code: str,
        force_refresh: bool = False,
    ) -> str:
        citype = (citype or "").strip()
        ico = str(ico).strip()

        if citype != "PO":
            raise ValueError(f"by_ico is only supported for citype='PO' (got {citype!r}).")
        if not ico:
            raise ValueError("ico cannot be empty.")

        indexes = self._get_po_indexes(report_code=report_code, force_refresh=force_refresh)
        return self._resolve_po_uuid_by_ico(indexes, ico)

    def find_ci_uuid(
        self,
        citype: str,
        *,
        by_uuid: str | None = None,
        by_name: str | None = None,
        by_ico: str | None = None,
        report_code: str | None = None,
        pick_if_ambiguous: bool = False,
        force_refresh: bool = False,
    ) -> str:
        self._require_exactly_one("find_ci_uuid", by_uuid=by_uuid, by_name=by_name, by_ico=by_ico)

        citype = (citype or "").strip()
        if not citype:
            raise ValueError("citype cannot be empty.")

        if by_uuid is not None:
            u = by_uuid.strip()
            if not u:
                raise ValueError("by_uuid cannot be empty.")
            return u

        rep = (report_code or self.report_code or self._env_report_code()).strip()
        if not rep:
            raise RuntimeError("find_ci_uuid(by_name=.../by_ico=...) requires report_code.")

        if by_ico is not None:
            return self._resolve_ci_uuid_by_ico(
                citype=citype,
                ico=by_ico,
                report_code=rep,
                force_refresh=force_refresh,
            )

        assert by_name is not None
        return self._resolve_ci_uuid_by_name(
            citype=citype,
            query=by_name,
            report_code=rep,
            pick_if_ambiguous=pick_if_ambiguous,
            force_refresh=force_refresh,
        )

    def store_rel(
        self,
        *,
        reltype: str,
        start_type: str,
        end_type: str,
        start_uuid: str | None = None,
        start_name: str | None = None,
        end_uuid: str | None = None,
        end_name: str | None = None,
        rel_attrs: Dict[str, Any] | list[Dict[str, Any]] | None = None,
        dry_run: bool = False,
        check_duplicates: bool = True,
        pick_if_ambiguous: bool = False,
        skip_duplicate_prompt: bool = False,
        report_code: str | None = None,
    ) -> RelationResult:
        """
        Create a relation via /api/cmdb/store/relation.

        Start/end are currently supported ONLY by UUID.
        We still fill start/end names + kodMetaIS + typeName by reading CIs + schemas.
        """
        self._require_ready_to_store()

        self._require_exactly_one("store_rel(start)", start_uuid=start_uuid, start_name=start_name)
        self._require_exactly_one("store_rel(end)", end_uuid=end_uuid, end_name=end_name)

        reltype = reltype.strip()
        start_type = start_type.strip()
        end_type = end_type.strip()

        rep = (report_code or self.report_code or self._env_report_code()).strip()
        if (start_name or end_name) and not rep:
            raise RuntimeError("Name-based relation endpoints require report_code (for lookup).")

        if start_name is not None:
            start_uuid = self._resolve_ci_uuid_by_name(citype=start_type, query=start_name, report_code=rep, pick_if_ambiguous=pick_if_ambiguous)
        if end_name is not None:
            end_uuid = self._resolve_ci_uuid_by_name(citype=end_type, query=end_name, report_code=rep, pick_if_ambiguous=pick_if_ambiguous)

        assert start_uuid is not None and end_uuid is not None

        if not (reltype and start_uuid and end_uuid and start_type and end_type):
            raise ValueError("reltype/start_uuid/start_type/end_uuid/end_type must all be provided (non-empty).")

        rep = (report_code or self.report_code or self._env_report_code()).strip()
        if check_duplicates and not rep:
            raise RuntimeError("Duplicate check enabled but no report_code is set.")

        # relation attrs normalization
        attrs_list: list[Dict[str, Any]] = []
        if rel_attrs:
            if isinstance(rel_attrs, dict):
                attrs_list = [{"name": k, "value": v} for k, v in rel_attrs.items()]
            elif isinstance(rel_attrs, list):
                # assume it's already [{"name":..., "value":...}, ...] or dicts
                if all(isinstance(x, dict) and "name" in x and "value" in x for x in rel_attrs):
                    attrs_list = rel_attrs
                else:
                    raise TypeError("rel_attrs list must contain dicts of form {'name':..., 'value':...}")
            else:
                raise TypeError("rel_attrs must be dict or list of {'name','value'} dicts")

        # Fetch start/end type display names from schema
        start_schema = self._get_citype_schema_cached(start_type)
        end_schema = self._get_citype_schema_cached(end_type)
        start_type_name = str(start_schema.get("name") or start_type)
        end_type_name = str(end_schema.get("name") or end_type)

        # Read start/end CIs (may require auth)
        start_read = self._read_ci(start_uuid)
        end_read = self._read_ci(end_uuid)

        start_attrs = self._ci_attrs_dict_from_read(start_read)
        end_attrs = self._ci_attrs_dict_from_read(end_read)

        start_name = self._ci_display_name(start_attrs)
        end_name = self._ci_display_name(end_attrs)

        start_kod = self._ci_kod_metais(start_type, start_attrs)
        end_kod = self._ci_kod_metais(end_type, end_attrs)

        # Duplicate check: exact reltype + (startUuid,endUuid)
        existing_uuid: str | None = None
        if check_duplicates:
            idx_key = (reltype, start_type, end_type)
            rel_index = self._rel_cache.get(idx_key)
            if rel_index is None or (time.time() - rel_index.fetched_at_unix) > CACHE_TTL_SECONDS:
                rel_index = self._fetch_existing_rel_index(report_code=rep, reltype=reltype, start_type=start_type, end_type=end_type)
                self._rel_cache[idx_key] = rel_index
            existing_uuid = rel_index.by_pair.get((start_uuid, end_uuid))

            if existing_uuid:
                print(f"[rel-dupe] Relation already exists: type={reltype} start={start_uuid} end={end_uuid} uuid={existing_uuid}", file=sys.stderr)

                proceed = True
                if not skip_duplicate_prompt:
                    proceed = _ask_yes_no("Create another identical relation anyway?", default=False)

                if not proceed:
                    rec = self._build_log_record(
                        status="existing",
                        citype=reltype,
                        entity_uuid=existing_uuid,
                        entity_url=None,
                        request_id=None,
                        payload=None,
                        reservation=None,
                        error=None,
                        extra={
                            "reason": "relation-duplicate-user-chose-existing",
                            "reltype": reltype,
                            "startUuid": start_uuid,
                            "endUuid": end_uuid,
                        },
                    )
                    log_path = self._log_added_record(citype=reltype, entity_uuid=f"existing-{existing_uuid}", status="existing", record=rec)
                    return RelationResult(status="existing", reltype=reltype, relation_uuid=existing_uuid, log_path=str(log_path))

        # Build payload (no metaAttributes)
        rel_uuid = str(uuid.uuid4())
        owner = f"{self.role_uuid}-{self.owner_uuid}"
        payload = {
            "type": reltype,
            "uuid": rel_uuid,
            "owner": owner,
            "startUuid": start_uuid,
            "startType": start_type,
            "startTypeName": start_type_name,
            "startName": start_name,
            "startKodMetaIS": start_kod,
            "endUuid": end_uuid,
            "endType": end_type,
            "endTypeName": end_type_name,
            "endName": end_name,
            "endKodMetaIS": end_kod,
            "attributes": attrs_list,
        }

        if dry_run:
            print("DRY RUN (no POST) store/relation", file=sys.stderr)
            print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
            rec = self._build_log_record(
                status="dry-run",
                citype=reltype,
                entity_uuid=rel_uuid,
                entity_url=None,
                request_id=None,
                payload=payload,
                reservation=None,
                error=None,
            )
            log_path = self._log_added_record(citype=reltype, entity_uuid=rel_uuid, status="dryrun", record=rec)
            return RelationResult(status="dry-run", reltype=reltype, relation_uuid=rel_uuid, payload=payload, log_path=str(log_path))

        # Real POST
        self._ensure_bearer(reason=f"store relation {reltype}", force=False)
        assert self._bearer is not None

        try:
            req_id = self._store_relation_api(payload=payload, lang=self.lang)
        except Exception as e:
            rec = self._build_log_record(
                status="fail",
                citype=reltype,
                entity_uuid=rel_uuid,
                entity_url=None,
                request_id=None,
                payload=payload,
                reservation=None,
                error={"type": type(e).__name__, "message": str(e), "traceback": traceback.format_exc()},
                http=self._extract_http_debug(e),
            )
            log_path = self._log_added_record(citype=reltype, entity_uuid=rel_uuid, status="fail", record=rec)
            return RelationResult(status="fail", reltype=reltype, relation_uuid=rel_uuid, payload=payload, error=rec["error"], log_path=str(log_path))

        rec = self._build_log_record(
            status="success",
            citype=reltype,
            entity_uuid=rel_uuid,
            entity_url=None,
            request_id=req_id,
            payload=payload,
            reservation=None,
            error=None,
        )
        log_path = self._log_added_record(citype=reltype, entity_uuid=rel_uuid, status="success", record=rec)

        # Update in-memory index if we have it
        if check_duplicates and rep:
            idx_key = (reltype, start_type, end_type)
            idx = self._rel_cache.get(idx_key)
            if idx:
                idx.by_pair[(start_uuid, end_uuid)] = rel_uuid

        return RelationResult(status="success", reltype=reltype, relation_uuid=rel_uuid, request_id=req_id, payload=payload, log_path=str(log_path))

    # ----------------------------
    # internal store logic
    # ----------------------------

    def _store_one(
        self,
        *,
        citype: str,
        schema: Dict[str, Any],
        attrs: Dict[str, Any],
        dry_run: bool,
        existing: Optional[_ExistingIndex],
        skip_duplicate_prompt: bool,
    ) -> StoreResult:
        attrs = dict(attrs)  # local copy

        required = self._extract_critical_required(schema)
        self._apply_defaults_from_schema(attrs, schema)

        # mandatory=critical validation BEFORE generating cicode
        self._validate_required_attrs(attrs, required, ignore=AUTO_KEYS)

        # duplicate detection BEFORE generating cicode
        if existing is not None:
            enum_keys = self._enum_keys_from_schema(schema)

            dup_map = self._find_duplicates_detailed(existing, attrs)

            # Ignore candidates that ONLY match on enums (noise)
            dup_map = {
                uu: km
                for uu, km in dup_map.items()
                if any((k not in enum_keys) for k in km.keys())
            }

            if dup_map:
                self._print_dupe_table(
                    citype=citype,
                    existing=existing,
                    new_attrs=attrs,
                    dup_map=dup_map,
                    enum_keys=enum_keys,
                    max_candidates=3,
                    term_width=70,
                    col_width=35,
                    max_val_len=35,
                )

                proceed = True
                if not skip_duplicate_prompt:
                    proceed = _ask_yes_no("Proceed anyway?", default=False)

                if not proceed:
                    rec = self._build_log_record(
                        status="skipped",
                        citype=citype,
                        entity_uuid=None,
                        entity_url=None,
                        request_id=None,
                        payload=None,
                        reservation=None,
                        error=None,
                        extra={
                            "reason": "duplicate-check",
                            "dup_map": {uu: sorted(list(keys)) for uu, keys in dup_map.items()},
                            "attrs": attrs,
                        },
                    )
                    log_path = self._log_added_record(
                        citype=citype,
                        entity_uuid=f"skipped-{uuid.uuid4().hex[:12]}",
                        status="skipped",
                        record=rec,
                    )

                    best_uuid = self._pick_best_existing_ci(dup_map=dup_map, existing=existing, enum_keys=enum_keys)
                    best_url = f"{self.base}/ci/{citype}/{best_uuid}"

                    rec = self._build_log_record(
                        status="existing",
                        citype=citype,
                        entity_uuid=best_uuid,
                        entity_url=best_url,
                        request_id=None,
                        payload=None,
                        reservation=None,
                        error=None,
                        extra={
                            "reason": "duplicate-check-user-chose-existing",
                            "best_uuid": best_uuid,
                            "dup_map": {uu: km for uu, km in dup_map.items()},
                            "attrs": attrs,
                        },
                    )
                    log_path = self._log_added_record(
                        citype=citype,
                        entity_uuid=f"existing-{best_uuid}",
                        status="existing",
                        record=rec,
                    )

                    return StoreResult(
                        status="existing",
                        citype=citype,
                        entity_uuid=best_uuid,
                        entity_url=best_url,
                        log_path=str(log_path),
                    )

        # compute ref_id requires uriPrefix
        if not schema.get("uriPrefix"):
            raise RuntimeError(f"Schema for {citype!r} has no uriPrefix; cannot compute Gen_Profil_ref_id reliably.")

        # build code (fake if dry-run)
        reservation: Optional[CodeReservation] = None

        if dry_run:
            gen_id = self._fake_metais_code_from_schema(schema, fallback_prefix=f"{citype}_")
        else:
            # Ensure bearer before any POST or code-generation
            self._ensure_bearer(reason=f"generate cicode and store CI for citype={citype}")
            assert self._bearer is not None

            reservation = self._acquire_or_generate_cicode(
                base=self.base,
                citype=citype,
                bearer=self._bearer,
                cache_dir=self.cache_dir,
                lang=self.lang,
            )
            gen_id = reservation.cicode

        attrs["Gen_Profil_kod_metais"] = gen_id
        attrs["Gen_Profil_ref_id"] = self._compute_ref_id_from_schema(schema, gen_id)

        payload = self._build_payload(
            citype=citype,
            role_uuid=self.role_uuid or "",
            owner_po_uuid=self.owner_uuid or "",
            attrs=attrs,
        )
        entity_uuid = str(payload.get("uuid") or "")
        entity_url = f"{self.base}/ci/{citype}/{entity_uuid}" if entity_uuid else None

        if dry_run:
            self._print_dry_run(citype=citype, payload=payload)
            rec = self._build_log_record(
                status="dry-run",
                citype=citype,
                entity_uuid=entity_uuid,
                entity_url=entity_url,
                request_id=None,
                payload=payload,
                reservation={"cicode": gen_id, "note": "dry-run fabricated"},
                error=None,
            )
            log_path = self._log_added_record(citype=citype, entity_uuid=entity_uuid or f"dryrun-{uuid.uuid4().hex[:12]}", status="dryrun", record=rec)
            return StoreResult(status="dry-run", citype=citype, entity_uuid=entity_uuid, entity_url=entity_url, payload=payload, log_path=str(log_path))

        assert self._bearer is not None
        assert reservation is not None

        try:
            req_id = self._store_ci_api(base=self.base, bearer=self._bearer, payload=payload, lang=self.lang)
        except Exception as e:
            # keep the code on disk for reuse
            self._mark_reservation_failed(reservation, note=str(e))

            rec = self._build_log_record(
                status="fail",
                citype=citype,
                entity_uuid=entity_uuid,
                entity_url=entity_url,
                request_id=None,
                payload=payload,
                reservation={"cicode": reservation.cicode, "path": str(reservation.path)},
                error={
                    "type": type(e).__name__,
                    "message": str(e),
                    "traceback": traceback.format_exc(),
                },
                http=self._extract_http_debug(e),
            )
            log_path = self._log_added_record(citype=citype, entity_uuid=entity_uuid or f"fail-{uuid.uuid4().hex[:12]}", status="fail", record=rec)
            return StoreResult(
                status="fail",
                citype=citype,
                entity_uuid=entity_uuid,
                entity_url=entity_url,
                payload=payload,
                reservation={"cicode": reservation.cicode, "path": str(reservation.path)},
                error=rec.get("error"),
                log_path=str(log_path),
            )

        # success
        self._mark_reservation_success(reservation, request_id=req_id)

        rec = self._build_log_record(
            status="success",
            citype=citype,
            entity_uuid=entity_uuid,
            entity_url=entity_url,
            request_id=req_id,
            payload=payload,
            reservation={"cicode": reservation.cicode, "path": str(reservation.path)},
            error=None,
        )
        log_path = self._log_added_record(citype=citype, entity_uuid=entity_uuid or f"ok-{uuid.uuid4().hex[:12]}", status="success", record=rec)

        return StoreResult(
            status="success",
            citype=citype,
            entity_uuid=entity_uuid,
            entity_url=entity_url,
            request_id=req_id,
            payload=payload,
            reservation={"cicode": reservation.cicode, "path": str(reservation.path)},
            log_path=str(log_path),
        )

    # ----------------------------
    # readiness + invariants
    # ----------------------------

    def _require_ready_to_store(self) -> None:
        if not self.role_uuid:
            raise RuntimeError("Role is not set. Call set_role(by_name=...) or set_role(by_uuid=...) first.")
        if not self.owner_uuid:
            raise RuntimeError("Owner is not set. Call set_owner(by_ico=...) / set_owner(by_name=...) / set_owner(by_uuid=...) first.")

    def _require_exactly_one(self, fn: str, **kwargs: Any) -> None:
        provided = [k for k, v in kwargs.items() if v is not None and str(v).strip() != ""]
        if len(provided) != 1:
            keys = ", ".join(kwargs.keys())
            raise ValueError(f"{fn} requires exactly one of: {keys}. Provided: {provided!r}")

    # ----------------------------
    # bearer management (max 3 interactive attempts)
    # ----------------------------

    def _ensure_bearer(self, *, reason: str, force: bool = False) -> str:
        if self._bearer and not force:
            return self._bearer

        def _try_login(username: str, password: str) -> str:
            return bearer_from_user_pass_plain(
                username,
                password,
                base=self.base,
                client_id=self.client_id,
                redirect_uri=self.redirect_uri,
                verify_tls=self.verify_tls,
                verbose=self.verbose,
            )

        # 1) env creds first (no prompting)
        env_user = (os.environ.get("METAIS_USER") or "").strip()
        env_pass = os.environ.get("METAIS_PASS") or ""
        if env_user and env_pass:
            try:
                tok = _try_login(env_user, env_pass)
                self._bearer = tok
                self._last_auth_user = env_user
                return tok
            except Exception as e:
                print(f"[auth] METAIS_USER/METAIS_PASS login failed: {e}", file=sys.stderr)

        # 2) interactive (max 3)
        print(f"[auth] Authentication required: {reason}", file=sys.stderr)
        print(f"[auth] Base: {self.base}", file=sys.stderr)

        if not _ask_yes_no("Provide credentials now?", default=False):
            raise SystemExit(
                "Cannot proceed without authentication. "
                "Set METAIS_USER/METAIS_PASS or rerun and choose 'y' when prompted."
            )

        # allow re-tries
        username_seed = env_user
        for attempt in range(1, 4):
            username = (username_seed or input("MetaIS username / email: ")).strip()
            password = getpass.getpass("MetaIS password: ")
            try:
                tok = _try_login(username, password)
                self._bearer = tok
                self._last_auth_user = username
                return tok
            except KeyboardInterrupt:
                print("\nCancelled.", file=sys.stderr)
                raise SystemExit(130)
            except Exception as e:
                # if clearly auth-related, allow retry, else fail immediately
                if _is_auth_http_error(e) or "401" in str(e) or "403" in str(e):
                    print(f"[auth] Login failed ({attempt}/3).", file=sys.stderr)
                    if attempt >= 3:
                        raise SystemExit("Authentication failed 3 times; aborting.")
                    username_seed = username  # keep
                    continue
                raise SystemExit(f"Authentication failed: {e}")

        raise SystemExit("Authentication failed; aborting.")


    _AUTH_CODES = (401, 403)

    def _raise_http_error(self, r: requests.Response) -> None:
        ct = (r.headers.get("Content-Type") or "").lower()
        body = r.text
        try:
            if "application/json" in ct:
                body = json.dumps(r.json(), ensure_ascii=False, indent=2)
        except Exception:
            pass
        raise requests.exceptions.HTTPError(
            f"{r.status_code} {r.reason} for {r.url}\nResponse body:\n{body}",
            response=r,
        )

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        reason: str,
        params: Optional[Dict[str, Any]] = None,
        json_data: Any = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
        require_auth: bool = True,
    ) -> Any:
        """
        One place to handle:
        - ensure bearer (force=False) before call
        - attach Authorization header if bearer exists
        - on 401/403: refresh bearer (force=True) and retry once
        - raise rich HTTPError on >=400
        - parse JSON response
        """
        timeout = float(timeout) if timeout is not None else self.timeout_store

        def _make_headers() -> Dict[str, str]:
            h: Dict[str, str] = {}
            if headers:
                h.update(headers)
            h.setdefault("Accept", "application/json")
            if json_data is not None:
                h.setdefault("Content-Type", "application/json")
            if self._bearer:
                h["Authorization"] = f"Bearer {self._bearer}"
            return h

        # Ensure we *have* a bearer before the first attempt if requested
        if require_auth:
            self._ensure_bearer(reason=reason, force=False)

        last_resp: Optional[requests.Response] = None

        for attempt in range(2):  # initial + one refresh retry
            r = self.session.request(
                method,
                url,
                params=params,
                json=json_data,
                headers=_make_headers(),
                timeout=timeout,
            )
            last_resp = r

            if r.status_code in self._AUTH_CODES and attempt == 0:
                # Token missing/expired/etc -> reauth and retry once.
                self._ensure_bearer(reason=f"{reason} (401/403 -> reauth)", force=True)
                continue

            if r.status_code >= 400:
                self._raise_http_error(r)

            try:
                return r.json()
            except Exception as e:
                snippet = (r.text or "")[:2000]
                raise RuntimeError(
                    f"Expected JSON from {method} {url} but failed to decode it. "
                    f"Status={r.status_code}. Body starts:\n{snippet}"
                ) from e

        assert last_resp is not None
        self._raise_http_error(last_resp)

    # ----------------------------
    # caching / filesystem layout
    # ----------------------------

    def _pick_default_cache_dir(self) -> Path:
        try:
            from metais.common.project_root import find_project_root
            root = find_project_root(Path(__file__))
            return (root / "scratch" / "metais-ci").resolve()
        except Exception:
            return _default_cache_dir()

    def _host_key(self) -> str:
        host = urlsplit(self.base).netloc or self.base
        return _safe_key(host)

    def _cache_root(self) -> Path:
        d = self.cache_dir / "cache" / self._host_key()
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _cache_path(self, name: str) -> Path:
        return self._cache_root() / f"{_safe_key(name)}.json"

    def _schema_cache_path(self, citype: str) -> Path:
        d = self._cache_root() / "schemas"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{_safe_key(citype)}.json"

    def _added_dir(self, citype: str) -> Path:
        d = self.cache_dir / "added" / self._host_key() / _safe_key(citype)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _codes_dir(self, citype: str) -> Path:
        d = self.cache_dir / "reserved_codes" / self._host_key() / _safe_key(citype)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _load_cache(self, path: Path) -> Optional[Dict[str, Any]]:
        try:
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return None
        except Exception:
            return None

    def _save_cache(self, path: Path, payload: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        tmp.replace(path)

    def _atomic_write_json(self, path: Path, obj: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        tmp.replace(path)

    def _is_fresh(self, cache_obj: Dict[str, Any], ttl_seconds: int = CACHE_TTL_SECONDS) -> bool:
        ts = cache_obj.get("fetched_at_unix")
        if not isinstance(ts, (int, float)):
            return False
        return (time.time() - float(ts)) < ttl_seconds

    # ----------------------------
    # roles (cached)
    # ----------------------------

    def _fetch_roles(self, *, bearer: str) -> list[Dict[str, Any]]:
        url = f"{self.base}/api/iam/roles"
        j = self._request_json(
            "GET",
            url,
            reason="fetch roles list (/api/iam/roles)",
            timeout=self.timeout_roles,
            require_auth=True,
        )
        if not isinstance(j, list):
            raise RuntimeError(f"Unexpected roles payload (expected list), got: {type(j)}")
        return j

    def _get_role_map(self, *, force_refresh: bool = False) -> Dict[str, str]:
        if self._role_map is not None and not force_refresh:
            return dict(self._role_map)

        p = self._cache_path("roles")
        cached = self._load_cache(p)
        if cached and not force_refresh and self._is_fresh(cached):
            mp = cached.get("role_map")
            if isinstance(mp, dict):
                self._role_map = {str(k): str(v) for k, v in mp.items()}
                return dict(self._role_map)

        url = f"{self.base}/api/iam/roles"
        roles = self._request_json(
            "GET",
            url,
            reason="fetch roles list (/api/iam/roles)",
            timeout=self.timeout_roles,
            require_auth=True,
        )
        if not isinstance(roles, list):
            raise RuntimeError(f"Unexpected roles payload (expected list), got: {type(roles)}")

        mp: Dict[str, str] = {}
        for r in roles:
            name = (r or {}).get("name")
            ruuid = (r or {}).get("uuid")
            if name and ruuid:
                mp[str(name)] = str(ruuid)

        self._save_cache(p, {"fetched_at_unix": time.time(), "role_map": mp, "count": len(mp)})
        self._role_map = dict(mp)
        return dict(mp)

    def _resolve_role_uuid(self, role_map: Dict[str, str], role_name: str) -> str:
        if role_name in role_map:
            return role_map[role_name]
        want = role_name.casefold()
        for k, v in role_map.items():
            if k.casefold() == want:
                return v
        sample = ", ".join(sorted(list(role_map.keys()))[:10])
        raise RuntimeError(f"Role {role_name!r} not found. Example roles: {sample} ...")

    # ----------------------------
    # schemas (cached on disk + mem)
    # ----------------------------

    def _fetch_citype_schema(self, *, citype: str, bearer: Optional[str]) -> Dict[str, Any]:
        url = f"{self.base}/api/types-repo/citypes/citype/{citype}"
        j = self._request_json(
            "GET",
            url,
            reason=f"fetch schema for citype={citype}",
            timeout=self.timeout_schema,
            # require_auth=True is fine; if you *really* want "try without auth first",
            # set require_auth=False (the wrapper still reauths on 401/403).
            require_auth=True,
        )
        if not isinstance(j, dict):
            raise RuntimeError(f"Unexpected schema payload (expected dict), got: {type(j)}")
        return j

    def _get_citype_schema_cached(self, citype: str, *, force_refresh: bool = False) -> Dict[str, Any]:
        if citype in self._schema_cache and not force_refresh:
            return self._schema_cache[citype]

        p = self._schema_cache_path(citype)
        cached = self._load_cache(p)

        # Fresh disk cache
        if cached and not force_refresh and self._is_fresh(cached):
            sch = cached.get("schema")
            if isinstance(sch, dict):
                self._schema_cache[citype] = sch
                return sch

        # Stale schema we can fall back to if fetch fails
        stale_schema: Dict[str, Any] | None = None
        if cached:
            sch = cached.get("schema")
            if isinstance(sch, dict):
                stale_schema = sch

        # Fetch (auth+401/403 handled inside _request_json via _fetch_citype_schema)
        try:
            sch = self._fetch_citype_schema(citype=citype, bearer=self._bearer)  # ideally remove bearer arg later
        except Exception as e:
            if stale_schema is not None:
                if self.verbose:
                    print(f"[schema] warning: fetch failed for {citype}, using stale cache: {e}", file=sys.stderr)
                self._schema_cache[citype] = stale_schema
                return stale_schema
            raise

        if not isinstance(sch, dict):
            raise RuntimeError(f"Schema fetch returned non-dict for citype={citype}: {type(sch)}")

        self._save_cache(p, {"fetched_at_unix": time.time(), "citype": citype, "schema": sch})
        self._schema_cache[citype] = sch
        return sch

    # -----------------------------
    # schema interpretation helpers
    # -----------------------------

    def _iter_valid_attributes_from_schema(self, schema: Dict[str, Any]):
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

    def _extract_critical_required(self, schema: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        required: Dict[str, Dict[str, Any]] = {}
        for a, src in self._iter_valid_attributes_from_schema(schema):
            mand = (a.get("mandatory") or {})
            if mand.get("type") == "critical":
                tech = a.get("technicalName")
                if tech:
                    required[tech] = {
                        "source": src,
                        "name": a.get("name"),
                        "description": a.get("description"),
                    }
        return required

    def _apply_defaults_from_schema(self, attrs: Dict[str, Any], schema: Dict[str, Any]) -> None:
        for a, _src in self._iter_valid_attributes_from_schema(schema):
            tech = a.get("technicalName")
            if not tech or tech in attrs:
                continue
            if a.get("defaultValue", None) is not None:
                attrs[tech] = a["defaultValue"]

    def _validate_required_attrs(self, attrs: Dict[str, Any], required: Dict[str, Dict[str, Any]], *, ignore: Optional[set[str]] = None) -> None:
        ignore = ignore or set()
        missing = [k for k in required.keys() if (k not in attrs) and (k not in ignore)]
        if not missing:
            return
        lines = ["Missing required (mandatory=critical) attributes:"]
        for k in sorted(missing):
            info = required.get(k, {})
            src = info.get("source", "?")
            nm = info.get("name", "")
            lines.append(f"  - {k}  ({src}){(' — ' + nm) if nm else ''}")
        raise RuntimeError("\n".join(lines))

    def _compute_ref_id_from_schema(self, schema: Dict[str, Any], cicode: str) -> str:
        uri_prefix = schema.get("uriPrefix")
        if not uri_prefix:
            raise RuntimeError("Schema missing uriPrefix")

        code_prefix = schema.get("codePrefix") or ""

        if code_prefix and not cicode.startswith(code_prefix):
            tail = cicode
            if not tail:
                raise RuntimeError("Empty cicode; cannot compute ref_id.")
            return f"{uri_prefix.rstrip('/')}/{tail}"

        if code_prefix and code_prefix.endswith("_"):
            tail = cicode[len(code_prefix):]
        else:
            tail = cicode

        if not tail:
            raise RuntimeError(f"Computed empty tail from cicode={cicode!r} and codePrefix={code_prefix!r}")

        return f"{uri_prefix.rstrip('/')}/{tail}"

    def _enum_keys_from_schema(self, schema: Dict[str, Any]) -> set[str]:
        """
        Return set of technicalName values whose constraints include type == "enum".
        Includes valid profile attributes because we use _iter_valid_attributes_from_schema().
        """
        out: set[str] = set()
        for a, _src in self._iter_valid_attributes_from_schema(schema):
            tech = a.get("technicalName")
            if not tech:
                continue
            for c in (a.get("constraints") or []):
                if isinstance(c, dict) and c.get("type") == "enum":
                    out.add(str(tech))
                    break
        return out

    def _fake_metais_code_from_schema(self, schema: Dict[str, Any], *, fallback_prefix: str = "ci_") -> str:
        code_prefix = (schema.get("codePrefix") or "").strip()
        if not code_prefix:
            code_prefix = fallback_prefix
        return f"{code_prefix}12345"

    # ----------------------------
    # report-based PO resolving + generic CI list fetch (duplicate checks)
    # ----------------------------

    def _env_report_code(self) -> str:
        # choose based on host containing '-test'
        host = urlsplit(self.base).netloc
        if "metais-test" in host or host.endswith("-test.slovensko.sk"):
            return (os.environ.get("METAIS_REPORT_NUM_TEST") or "").strip()
        return (os.environ.get("METAIS_REPORT_NUM_PROD") or "").strip()

    def _fetch_report_page(
        self,
        *,
        report_code: str,
        parameters: Dict[str, Any],
        bearer: Optional[str],
    ) -> Dict[str, Any]:
        if not report_code:
            raise RuntimeError("Missing report_code (set env METAIS_REPORT_NUM_PROD/TEST or pass report_code).")

        url = f"{self.base}/api/report/reports/execute/{report_code}/type/typ"
        j = self._request_json(
            "POST",
            url,
            reason=f"execute report {report_code}",
            params={"lang": self.lang},
            json_data={"parameters": parameters},
            headers={"User-Agent": "metais-ci/2.0"},
            timeout=self.timeout_report,
            require_auth=True,
        )
        if not isinstance(j, dict) or "result" not in j:
            raise RuntimeError("Unexpected report response shape (missing 'result').")
        return j


    def _fetch_report_all(
        self,
        *,
        report_code: str,
        parameters: Dict[str, Any],
        bearer: Optional[str],
        page_limit: int = 1000,
    ) -> Dict[str, Any]:
        """
        Try once without paging (fast if small). If it times out, fall back to paging:
        limit=page_limit, offset=0, page_limit, 2*page_limit, ... until empty result.
        """
        try:
            return self._fetch_report_page(report_code=report_code, parameters=parameters, bearer=bearer)
        except requests.exceptions.Timeout:
            pass  # fall back to paging

        all_rows: list[Any] = []
        offset = 0

        while True:
            p = dict(parameters)
            p["limit"] = page_limit
            p["offset"] = offset

            try:
                j = self._fetch_report_page(report_code=report_code, parameters=p, bearer=bearer)
            except requests.exceptions.Timeout as e:
                # If paging still times out, you can optionally reduce the limit here.
                raise RuntimeError(f"Report paging timed out at offset={offset} limit={page_limit}: {e}")

            page = j.get("result") or []
            if not isinstance(page, list):
                raise RuntimeError("Report 'result' is not a list.")
            if not page:
                break

            all_rows.extend(page)
            offset += page_limit

        # Return a report-like object
        return {"result": all_rows}

    def _attrs_to_dict(self, ci: Dict[str, Any]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for a in (ci.get("attributes") or []):
            if not isinstance(a, dict):
                continue
            n = a.get("name")
            if n:
                out[str(n)] = a.get("value")
        return out

    def _get_po_indexes(self, *, report_code: str, force_refresh: bool = False) -> Dict[str, Dict[str, str]]:
        p = self._cache_path("po_list")
        cached = self._load_cache(p)

        if cached and not force_refresh and self._is_fresh(cached):
            idx = cached.get("indexes")
            if isinstance(idx, dict):
                return idx

        stale_indexes: Dict[str, Dict[str, str]] | None = None
        if cached and isinstance(cached.get("indexes"), dict):
            stale_indexes = cached["indexes"]

        try:
            data = self._fetch_report_all(
                report_code=report_code,
                parameters={"target": "nodes", "type": "PO", "validOnly": "true"},
                bearer=self._bearer,  # ideally remove bearer arg later
            )
        except Exception as e:
            if stale_indexes is not None:
                if self.verbose:
                    print(f"[po] warning: fetch failed, using stale cache: {e}", file=sys.stderr)
                return stale_indexes
            raise

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

            ad = self._attrs_to_dict(ci)
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
        self._save_cache(p, {"fetched_at_unix": time.time(), "indexes": indexes, "count": len(result)})
        return indexes

    def _resolve_po_uuid_by_ico(self, indexes: Dict[str, Dict[str, str]], ico: str) -> str:
        mp = indexes["ico_to_uuid"]
        ico = str(ico).strip()
        if ico in mp:
            return mp[ico]
        raise RuntimeError(f"PO with ICO={ico!r} not found in PO list.")

    def _resolve_po_uuid_by_name(self, indexes: Dict[str, Dict[str, str]], query: str) -> str:
        q = _nodia_casefold(query)
        name_to_uuid = indexes["name_to_uuid"]
        name_raw = indexes["name_raw"]

        if q in name_to_uuid:
            return name_to_uuid[q]

        hits = []
        for norm_name, puuid in name_to_uuid.items():
            if q in norm_name:
                hits.append((norm_name, puuid))

        if not hits:
            raise RuntimeError(f"No PO name match for {query!r}.")

        hits.sort(key=lambda t: (len(t[0]), t[0]))
        best = hits[0]

        if len(hits) > 1:
            examples = []
            for norm_name, puuid in hits[:10]:
                raw = name_raw.get(puuid, norm_name)
                examples.append(f"- {raw}  ({puuid})")
            raise RuntimeError(
                f"Owner name {query!r} is ambiguous ({len(hits)} matches). "
                f"Use by_ico/by_uuid or be more specific.\n" + "\n".join(examples)
            )

        return best[1]

    def _fetch_existing_ci_index(self, *, citype: str, report_code: str) -> _ExistingIndex:
        data = self._fetch_report_all(
            report_code=report_code,
            parameters={"target": "nodes", "type": citype, "validOnly": "true"},
            bearer=self._bearer,  # ideally remove bearer arg later
        )

        result = data.get("result") or []
        if not isinstance(result, list):
            raise RuntimeError(f"{citype} report 'result' is not a list.")

        by_attr: Dict[str, Dict[str, list[str]]] = {}
        ci_attrs: Dict[str, Dict[str, Any]] = {}

        for ci in result:
            if not isinstance(ci, dict):
                continue
            cuuid = ci.get("uuid")
            if not cuuid:
                continue
            cuuid = str(cuuid)

            ad = self._attrs_to_dict(ci)
            ci_attrs[cuuid] = ad

            for k, v in ad.items():
                if k in AUTO_KEYS:
                    continue
                nv = _norm_val(v)
                by_attr.setdefault(k, {}).setdefault(nv, []).append(cuuid)

        return _ExistingIndex(fetched_at_unix=time.time(), by_attr=by_attr, ci_attrs=ci_attrs)

    def _is_exact_value_match(self, a: Any, b: Any) -> bool:
        # “exact” means raw equality (for strings: after strip only)
        if isinstance(a, str) and isinstance(b, str):
            return a.strip() == b.strip()
        return a == b


    def _find_duplicates_detailed(self, existing: _ExistingIndex, attrs: Dict[str, Any]) -> Dict[str, Dict[str, str]]:
        """
        Return: {existing_uuid -> {attr_key -> "exact" | "norm"}}

        We still *find* candidates by normalized match (fast),
        but we classify each matched key as:
        - "exact" if raw values equal
        - "norm"  if only normalized values match (case/diacritics/type differences)
        """
        hit: Dict[str, Dict[str, str]] = {}

        for k, v_new in attrs.items():
            if k in AUTO_KEYS:
                continue

            nv = _norm_val(v_new)
            uuids = existing.by_attr.get(k, {}).get(nv, [])

            for uu in uuids:
                ex = existing.ci_attrs.get(uu, {}) or {}
                v_old = ex.get(k, None)

                kind = "exact" if self._is_exact_value_match(v_old, v_new) else "norm"

                # if we see the same key multiple times, keep the strongest classification
                prev = hit.get(uu, {}).get(k)
                if prev == "exact":
                    continue
                hit.setdefault(uu, {})[k] = kind

        return hit

    def _pick_best_existing_ci(self, *, dup_map: Dict[str, Dict[str, str]], existing: _ExistingIndex, enum_keys: set[str]) -> str:
        """
        Pick the 'reddest' existing CI (most non-enum exact matches, then non-enum norm, then name).
        dup_map is uuid -> {key -> kind}
        """
        def score(uu: str) -> tuple[int, int, int, str]:
            km = dup_map.get(uu, {})
            nonenum_exact = sum(1 for k, kind in km.items() if k not in enum_keys and kind == "exact")
            nonenum_norm  = sum(1 for k, kind in km.items() if k not in enum_keys and kind == "norm")
            enum_cnt      = sum(1 for k in km.keys() if k in enum_keys)
            exnm = (existing.ci_attrs.get(uu, {}) or {}).get("Gen_Profil_nazov") or ""
            # sort: higher exact, higher norm, lower enum (irrelevant), then name
            return (nonenum_exact, nonenum_norm, -enum_cnt, _nodia_casefold(str(exnm)))

        best = max(dup_map.keys(), key=score)
        return best

    def _print_dupe_table(
        self,
        *,
        citype: str,
        existing: _ExistingIndex,
        new_attrs: Dict[str, Any],
        dup_map: Dict[str, Dict[str, str]],  # uuid -> {key: kind}
        enum_keys: set[str],
        max_candidates: int = 3,
        term_width: int = 70,
        col_width: int = 35,
        max_val_len: int = 35,
        stream=None,
    ) -> None:
        if stream is None:
            stream = sys.stderr
        use_color = _supports_color(stream)

        def hr(ch: str = "-") -> str:
            return ch * max(10, term_width)

        def style_for(key: str, kind: str) -> str:
            # enum matches are always "less scary"
            if key in enum_keys:
                return _ANSI_ORANGE_DIM
            # non-enum:
            if kind == "exact":
                return _ANSI_RED
            return _ANSI_ORANGE  # normalized-only match (case/diacritics/etc.)

        def _rank_item(item: tuple[str, Dict[str, str]]):
            uu, km = item
            nonenum_exact = sum(1 for k, kind in km.items() if k not in enum_keys and kind == "exact")
            nonenum_norm  = sum(1 for k, kind in km.items() if k not in enum_keys and kind == "norm")
            enum_cnt      = sum(1 for k in km.keys() if k in enum_keys)
            exnm = (existing.ci_attrs.get(uu, {}) or {}).get("Gen_Profil_nazov") or ""
            # prioritize real duplicates
            return (-nonenum_exact, -nonenum_norm, -enum_cnt, _nodia_casefold(str(exnm)), uu)

        items = sorted(dup_map.items(), key=_rank_item)
        shown = items[:max_candidates]
        hidden = len(items) - len(shown)

        nm = new_attrs.get("Gen_Profil_nazov")
        title = f"[dupe-check] {citype}"
        if nm is not None:
            title += f"  Gen_Profil_nazov={nm!r}"
        print(_c(title, _ANSI_BOLD, enabled=use_color), file=stream)
        if hidden > 0:
            print(_c(f"[dupe-check] ({hidden} more candidates not shown)", _ANSI_DIM, enabled=use_color), file=stream)
        print(file=stream)

        # Header: New entity starts at col_width
        print(f"{'Existing entity':<{col_width}}{'New entity'}", file=stream)
        print(hr("-"), file=stream)

        new_keys = [k for k in new_attrs.keys() if k not in AUTO_KEYS]

        for cand_uuid, key_kinds in shown:
            ex = existing.ci_attrs.get(cand_uuid, {}) or {}

            nonenum_exact = sorted([k for k, kind in key_kinds.items() if k not in enum_keys and kind == "exact"])
            nonenum_norm  = sorted([k for k, kind in key_kinds.items() if k not in enum_keys and kind == "norm"])
            enum_match    = sorted([k for k in key_kinds.keys() if k in enum_keys])

            # candidate header
            print(_c(f"uuid: {cand_uuid}", _ANSI_BOLD, enabled=use_color), file=stream)
            if nonenum_exact:
                print(_c("non-enum exact: " + ", ".join(nonenum_exact), _ANSI_DIM, enabled=use_color), file=stream)
            if nonenum_norm:
                print(_c("non-enum casefold + accent-less: " + ", ".join(nonenum_norm), _ANSI_DIM, enabled=use_color), file=stream)
            if enum_match:
                print(_c("enum matches: " + ", ".join(enum_match), _ANSI_DIM, enabled=use_color), file=stream)

            print(hr("-"), file=stream)

            ordered = nonenum_exact + nonenum_norm + enum_match + [k for k in new_keys if k not in key_kinds]

            for k in ordered:
                if k in AUTO_KEYS:
                    continue
                if not (k in ex or k in new_attrs):
                    continue

                # key name line (colored only if it matched)
                key_line = f"{k}:"
                if k in key_kinds:
                    key_line = _c(key_line, style_for(k, key_kinds[k]), enabled=use_color)
                print(key_line, file=stream)

                ex_s = _fmt_val(ex.get(k, None), max_len=max_val_len) if k in ex else ""
                new_s = _fmt_val(new_attrs.get(k, None), max_len=max_val_len) if k in new_attrs else ""

                left = ex_s.ljust(col_width)
                right = new_s

                if k in key_kinds:
                    st = style_for(k, key_kinds[k])
                    left = _c(left, st, enabled=use_color)
                    right = _c(right, st, enabled=use_color)

                print(f"{left}{right}", file=stream)
                print(file=stream)

            print(hr("-"), file=stream)
            print(file=stream)

    def _existing_index_add(self, existing: _ExistingIndex, *, ci_uuid: str, attrs: Dict[str, Any]) -> None:
        existing.ci_attrs[ci_uuid] = dict(attrs)
        for k, v in attrs.items():
            if k in AUTO_KEYS:
                continue
            nv = _norm_val(v)
            existing.by_attr.setdefault(k, {}).setdefault(nv, []).append(ci_uuid)

    # ----------------------------------
    # relation fetching and dup checking
    # ----------------------------------

    def _fetch_existing_rel_index(self, *, report_code: str, reltype: str, start_type: str, end_type: str) -> _ExistingRelIndex:
        data = self._fetch_report_all(
            report_code=report_code,
            parameters={
                "target": "relations",
                "type": reltype,
                "src": start_type,
                "tgt": end_type,
                "validOnly": "true",
            },
            bearer=self._bearer,  # ideally remove bearer arg later
        )

        res = data.get("result") or []
        if not isinstance(res, list):
            raise RuntimeError("Relation report 'result' is not a list.")

        by_pair: Dict[tuple[str, str], str] = {}
        for rel in res:
            if not isinstance(rel, dict):
                continue
            uu = rel.get("uuid")
            su = rel.get("startUuid")
            eu = rel.get("endUuid")
            if not (uu and su and eu):
                continue

            key = (str(su), str(eu))
            uu_s = str(uu)

            if key not in by_pair:
                by_pair[key] = uu_s
            elif by_pair[key] != uu_s and self.verbose:
                print(f"[rel-dupe-index] multiple rels for {reltype} {key[0]}->{key[1]}: {by_pair[key]}, {uu_s}", file=sys.stderr)

        return _ExistingRelIndex(fetched_at_unix=time.time(), by_pair=by_pair)

    # ----------------------------
    # cicode reservation tracking
    # ----------------------------

    def _list_reservation_files(self, d: Path) -> list[Path]:
        inflight = sorted(d.glob("*.inflight.json"), key=lambda p: p.stat().st_mtime)
        pending = sorted(d.glob("*.pending.json"), key=lambda p: p.stat().st_mtime)
        return inflight + pending

    def _generate_metais_code(self, *, base: str, bearer: str, citype: str, lang: str) -> str:
        url = f"{base}/api/types-repo/citypes/generate/{citype}"
        j = self._request_json(
            "GET",
            url,
            reason=f"generate cicode for citype={citype}",
            params={"lang": lang},
            timeout=self.timeout_schema,
            require_auth=True,
        )
        if not isinstance(j, dict):
            raise RuntimeError(f"Unexpected generate/{citype} payload type: {type(j)}")
        cicode = j.get("cicode")
        if not cicode:
            raise RuntimeError(f"Missing cicode in response keys={list(j.keys())}")
        return str(cicode)

    def _acquire_or_generate_cicode(
        self,
        *,
        base: str,
        citype: str,
        bearer: str,
        cache_dir: Path,
        lang: str,
    ) -> CodeReservation:
        d = self._codes_dir(citype)

        # 1) reuse existing
        for p in self._list_reservation_files(d):
            if p.name.endswith(".pending.json"):
                inflight = p.with_name(p.name.replace(".pending.json", ".inflight.json"))
                try:
                    p.replace(inflight)
                    p = inflight
                except Exception:
                    continue

            obj = self._load_cache(p)
            if isinstance(obj, dict) and isinstance(obj.get("cicode"), str) and obj["cicode"].strip():
                return CodeReservation(cicode=obj["cicode"].strip(), path=p)

        # 2) generate new
        cicode = self._generate_metais_code(base=base, bearer=bearer, citype=citype, lang=lang)

        ts = int(time.time())
        rid = uuid.uuid4().hex[:12]
        inflight = d / f"{ts}.{rid}.inflight.json"

        self._atomic_write_json(
            inflight,
            {
                "fetched_at_unix": time.time(),
                "base": base,
                "host": urlsplit(base).netloc,
                "citype": citype,
                "cicode": cicode,
                "status": "inflight",
            },
        )
        return CodeReservation(cicode=cicode, path=inflight)

    def _mark_reservation_success(self, res: CodeReservation, *, request_id: str | None = None) -> None:
        used = res.path.with_name(res.path.name.replace(".inflight.json", ".used.json"))
        obj = self._load_cache(res.path) or {}
        if isinstance(obj, dict):
            obj["status"] = "used"
            obj["used_at_unix"] = time.time()
            if request_id:
                obj["requestId"] = request_id
            self._atomic_write_json(used, obj)

        try:
            res.path.unlink(missing_ok=True)
        except TypeError:
            if res.path.exists():
                res.path.unlink()

    def _mark_reservation_failed(self, res: CodeReservation, *, note: str | None = None) -> None:
        if not note:
            return
        obj = self._load_cache(res.path)
        if isinstance(obj, dict):
            obj["last_error"] = note[:2000]
            obj["last_error_at_unix"] = time.time()
            self._atomic_write_json(res.path, obj)

    # ------------------
    # reading ci by uuid
    # ------------------

    def _read_ci(self, ci_uuid: str) -> Dict[str, Any]:
        url = f"{self.base}/api/cmdb/read/ci/{ci_uuid}"
        j = self._request_json(
            "GET",
            url,
            reason=f"read CI {ci_uuid}",
            timeout=self.timeout_store,
            require_auth=True,
        )
        if not isinstance(j, dict):
            raise RuntimeError(f"Unexpected read/ci payload type: {type(j)}")
        return j


    def _ci_attrs_dict_from_read(self, read_ci: Dict[str, Any]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for a in (read_ci.get("attributes") or []):
            if isinstance(a, dict) and a.get("name") is not None:
                out[str(a["name"])] = a.get("value")
        return out


    def _ci_display_name(self, attrs: Dict[str, Any]) -> str:
        v = attrs.get("Gen_Profil_nazov")
        return str(v) if v is not None else ""


    def _ci_kod_metais(self, ci_type: str, attrs: Dict[str, Any]) -> str:
        """
        Return the code that belongs into *KodMetaIS fields* of relation payloads.

        Rules:
        - For PO: prefer ICO (EA_Profil_PO_ico / Profil_PO_ico). If Gen_Profil_kod_metais exists too, it must match.
        - For non-PO: require Gen_Profil_kod_metais.
        - Missing => raise (weird entity).
        """
        ci_type = (ci_type or "").strip()

        if ci_type == "PO":
            ico = attrs.get("EA_Profil_PO_ico")
            if ico is None or str(ico).strip() == "":
                ico = attrs.get("Profil_PO_ico")

            kod = attrs.get("Gen_Profil_kod_metais")

            ico_s = str(ico).strip() if ico is not None else ""
            kod_s = str(kod).strip() if kod is not None else ""

            if ico_s and kod_s and ico_s != kod_s:
                raise RuntimeError(f"PO has mismatched ICO vs Gen_Profil_kod_metais: {ico_s!r} vs {kod_s!r}")

            if ico_s:
                return ico_s
            if kod_s:
                return kod_s

            raise RuntimeError("PO is missing both EA_Profil_PO_ico and Gen_Profil_kod_metais (unexpected).")

        # non-PO
        kod = attrs.get("Gen_Profil_kod_metais")
        kod_s = str(kod).strip() if kod is not None else ""
        if not kod_s:
            raise RuntimeError(f"{ci_type} is missing Gen_Profil_kod_metais (unexpected).")
        return kod_s

    def _normalize_state_param(self, states: str | Iterable[str] | None) -> str:
        """
        MetaIS expects comma-separated states, e.g. "DRAFT,INVALIDATED".
        Accepts either already-joined string or an iterable.
        """
        if states is None:
            return ""
        if isinstance(states, str):
            return states.strip()
        parts = [str(s).strip() for s in states if str(s).strip()]
        return ",".join(parts)

    def _neighbors_page_api(
        self,
        *,
        ci_uuid: str,
        page: int,
        per_page: int,
        states: str | Iterable[str] = ("DRAFT", "INVALIDATED"),
        lang: str | None = None,
    ) -> Dict[str, Any]:
        ci_uuid = str(ci_uuid).strip()
        if not ci_uuid:
            raise ValueError("ci_uuid cannot be empty.")

        url = f"{self.base}/api/cmdb/read/relations/neighbourswithallrels/{ci_uuid}"
        st = self._normalize_state_param(states)
        params: Dict[str, Any] = {
            "page": int(page),
            "perPage": int(per_page),
            "lang": (lang or self.lang),
        }
        if st:
            params["state"] = st

        j = self._request_json(
            "GET",
            url,
            reason=f"fetch neighbourswithallrels for CI {ci_uuid}",
            params=params,
            timeout=self.timeout_store,
            require_auth=True,
        )
        if not isinstance(j, dict):
            raise RuntimeError(f"Unexpected neighbours payload type: {type(j)}")
        return j

    def fetch_neighbors_with_rels(
        self,
        ci_uuid: str,
        *,
        states: str | Iterable[str] = ("DRAFT", "INVALIDATED"),
        per_page: int = 50,
        max_pages: int | None = None,
        lang: str | None = None,
    ) -> list[Dict[str, Any]]:
        """
        Returns the concatenated `ciWithRels` list from:
          /api/cmdb/read/relations/neighbourswithallrels/{ci_uuid}

        `states` is passed to the endpoint as ?state=DRAFT,INVALIDATED (etc).
        """
        out: list[Dict[str, Any]] = []

        page = 1
        while True:
            j = self._neighbors_page_api(
                ci_uuid=ci_uuid,
                page=page,
                per_page=per_page,
                states=states,
                lang=lang,
            )

            rows = j.get("ciWithRels") or []
            if not isinstance(rows, list):
                raise RuntimeError("neighbourswithallrels: ciWithRels is not a list.")
            out.extend([x for x in rows if isinstance(x, dict)])

            pag = j.get("pagination") or {}
            total_pages = pag.get("totalPages")
            try:
                total_pages_i = int(total_pages) if total_pages is not None else page
            except Exception:
                total_pages_i = page

            if max_pages is not None and page >= max_pages:
                break
            if page >= total_pages_i:
                break
            page += 1

        return out

    def _extract_relation_uuids_from_neighbors_payload(
        self,
        neighbors: list[Dict[str, Any]],
        *,
        only_states: str | Iterable[str] | None = ("INVALIDATED",),
    ) -> list[str]:
        """
        Pull relation UUIDs from neighbourswithallrels response.
        If only_states is provided, filter by rel.metaAttributes.state.
        """
        want = None
        if only_states is not None:
            s = self._normalize_state_param(only_states)
            want = {x.strip() for x in s.split(",") if x.strip()}

        uu: set[str] = set()
        for item in neighbors:
            rels = item.get("rels") or []
            if not isinstance(rels, list):
                continue
            for rel in rels:
                if not isinstance(rel, dict):
                    continue
                ruid = rel.get("uuid")
                if not ruid:
                    continue
                if want is not None:
                    st = ((rel.get("metaAttributes") or {}).get("state") or "").strip()
                    if want is not None:
                        if st and st not in want:
                            continue
                uu.add(str(ruid).strip())

        return sorted(u for u in uu if u)

    # ----------------------------
    # CMDB store API
    # ----------------------------

    def _store_ci_api(self, *, base: str, bearer: str, payload: Dict[str, Any], lang: str) -> str:
        url = f"{base}/api/cmdb/store/ci"
        j = self._request_json(
            "POST",
            url,
            reason="store CI",
            params={"lang": lang},
            json_data=payload,
            timeout=self.timeout_store,
            require_auth=True,
        )
        if not isinstance(j, dict):
            raise RuntimeError(f"Unexpected store/ci response type: {type(j)}")
        req_id = j.get("requestId")
        if not req_id:
            raise RuntimeError(f"Missing requestId in response keys={list(j.keys())}")
        return str(req_id)

    def _store_relation_api(self, *, payload: Dict[str, Any], lang: str) -> str:
        url = f"{self.base}/api/cmdb/store/relation"
        j = self._request_json(
            "POST",
            url,
            reason="store relation",
            params={"lang": lang},
            json_data=payload,
            timeout=self.timeout_store,
            require_auth=True,
        )
        if not isinstance(j, dict):
            raise RuntimeError(f"Unexpected store/relation response type: {type(j)}")
        req_id = j.get("requestId")
        if not req_id:
            raise RuntimeError(f"Missing requestId in response keys={list(j.keys())}")
        return str(req_id)

    # ----------------------- #
    # payload + normalization #
    # ----------------------- #

    def _build_payload(self, *, citype: str, role_uuid: str, owner_po_uuid: str, attrs: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "uuid": str(uuid.uuid4()),
            "type": citype,
            "attributes": [{"name": k, "value": v} for k, v in attrs.items()],
            "owner": f"{role_uuid}-{owner_po_uuid}",
        }

    def _is_metais_attr_list(self, obj: Any) -> bool:
        # A single CI expressed as [{"name":..., "value":...}, ...]
        return (
            isinstance(obj, list)
            and len(obj) > 0
            and all(isinstance(x, dict) for x in obj)
            and all(("name" in x and "value" in x) for x in obj)
            and all(set(x.keys()).issubset({"name", "value"}) for x in obj)  # avoid ambiguity
        )

    def _normalize_one_attrs(self, obj: Any) -> Dict[str, Any]:
        # already dict => OK
        if isinstance(obj, dict):
            return dict(obj)

        # list of {"name":..., "value":...}
        if isinstance(obj, list) and all(isinstance(x, dict) for x in obj):
            # if it looks like list-of-name/value dicts
            if all(("name" in x and "value" in x) for x in obj):
                out: Dict[str, Any] = {}
                for x in obj:
                    n = x.get("name")
                    if n is not None:
                        out[str(n)] = x.get("value")
                return out

        raise TypeError("attrs must be dict{name:value} or list of {'name','value'} dicts")

    def _normalize_batch_attrs(self, attrs: Any) -> list[Dict[str, Any]]:
        # Single CI as dict
        if isinstance(attrs, dict):
            return [self._normalize_one_attrs(attrs)]

        if isinstance(attrs, list):
            # Single CI in MetaIS style: [{"name":..,"value":..}, ...]
            if self._is_metais_attr_list(attrs):
                return [self._normalize_one_attrs(attrs)]  # note: pass the whole list

            # Otherwise it's a batch: each item is either:
            #  - dict{name:value}
            #  - list[{"name":..,"value":..}, ...]
            out: list[Dict[str, Any]] = []
            for it in attrs:
                out.append(self._normalize_one_attrs(it))
            return out

        raise TypeError("attrs must be dict or list")

    # ------------------------------ #
    # logging (thorough, no secrets) #
    # ------------------------------ #

    def _extract_http_debug(self, e: BaseException) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        resp = getattr(e, "response", None)
        if resp is None:
            return out
        try:
            out["http_status"] = getattr(resp, "status_code", None)
            out["http_reason"] = getattr(resp, "reason", None)
            out["url"] = getattr(resp, "url", None)
            ct = None
            try:
                ct = resp.headers.get("Content-Type")
            except Exception:
                pass
            if ct:
                out["content_type"] = ct
            body = None
            try:
                body = resp.text
            except Exception:
                body = None
            if isinstance(body, str) and body:
                out["response_text"] = body[:200000]
            try:
                out["response_json"] = resp.json()
            except Exception:
                pass
        except Exception:
            return out
        return out

    def _build_log_record(
        self,
        *,
        status: str,
        citype: str,
        entity_uuid: str | None,
        entity_url: str | None,
        request_id: str | None,
        payload: Dict[str, Any] | None,
        reservation: Dict[str, Any] | None,
        error: Dict[str, Any] | None,
        http: Dict[str, Any] | None = None,
        extra: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        rec: Dict[str, Any] = {
            "logged_at_utc": _utc_now_iso(),
            "status": status,
            "base": self.base,
            "citype": citype,
            "entity_uuid": entity_uuid,
            "entity_url": entity_url,
            "who": self._last_auth_user or (os.environ.get("METAIS_USER") or "").strip() or None,
            "argv": sys.argv,
        }
        if request_id is not None:
            rec["requestId"] = request_id
        if payload is not None:
            rec["payload"] = payload
        if reservation is not None:
            rec["reservation"] = reservation
        if error is not None:
            rec["error"] = error
        if http:
            rec["http"] = http
        if extra:
            rec["extra"] = extra
        return rec

    def _log_added_record(self, *, citype: str, entity_uuid: str, status: str, record: Dict[str, Any]) -> Path:
        d = self._added_dir(citype)
        path = d / f"{entity_uuid}.{status}.json"
        self._atomic_write_json(path, record)
        return path

    # -------------- #
    # (re)validation #
    # -------------- #

    def _normalize_uuid_list(self, uuids: str | Iterable[str]) -> list[str]:
        # Accept a single uuid string or any iterable of strings
        if isinstance(uuids, str):
            uu = [uuids]
        else:
            uu = list(uuids)

        out: list[str] = []
        for x in uu:
            s = str(x).strip()
            if s:
                out.append(s)
        return out

    def _invalidate_ci_api(self, *, payload: Dict[str, Any], lang: str) -> Dict[str, Any]:
        url = f"{self.base}/api/cmdb/invalidate/list"
        j = self._request_json(
            "POST",
            url,
            reason="invalidate CI list",
            params={"lang": lang},
            json_data=payload,
            timeout=self.timeout_store,
            require_auth=True,
        )
        if not isinstance(j, dict):
            raise RuntimeError(f"Unexpected invalidate/list response type: {type(j)}")
        return j

    def _recycle_cis_api(self, *, domain: str, payload: Dict[str, Any], lang: str) -> Dict[str, Any]:
        url = f"{self.base}/api/cmdb/recycle/cis/{domain}"
        j = self._request_json(
            "POST",
            url,
            reason=f"recycle CIs domain={domain}",
            params={"lang": lang},
            json_data=payload,
            timeout=self.timeout_store,
            require_auth=True,
        )
        if not isinstance(j, dict):
            raise RuntimeError(f"Unexpected recycle/cis response type: {type(j)}")
        return j

    def _recycle_rels_api(self, *, payload: Dict[str, Any], lang: str) -> Dict[str, Any]:
        url = f"{self.base}/api/cmdb/recycle/rels"
        j = self._request_json(
            "POST",
            url,
            reason="recycle relations",
            params={"lang": lang},
            json_data=payload,
            timeout=self.timeout_store,
            require_auth=True,
        )
        if not isinstance(j, dict):
            raise RuntimeError(f"Unexpected recycle/rels response type: {type(j)}")
        return j

    def invalidate_cis(
        self,
        uuids: str | Iterable[str],
        *,
        comment: str,
        dry_run: bool = False,
        audit_history: bool = False,
        skip_if_invalidated: bool = True,
    ) -> Dict[str, Any]:
        uu = self._normalize_uuid_list(uuids)
        if not uu:
            raise ValueError("uuids cannot be empty.")
        if not comment.strip():
            raise ValueError("comment cannot be empty.")

        configuration_items: list[Dict[str, Any]] = []
        skipped: list[Dict[str, Any]] = []

        for ci_uuid in uu:
            try:
                read = self._read_ci(ci_uuid)
            except requests.exceptions.HTTPError as e:
                resp = getattr(e, "response", None)
                code = getattr(resp, "status_code", None)

                if code == 404:
                    print(f"[invalidate] warning: CI uuid not found: {ci_uuid}", file=sys.stderr)
                    skipped.append({"uuid": ci_uuid, "reason": "not-found"})
                    continue

                msg = str(e).strip()
                print(f"[invalidate] warning: failed to read CI {ci_uuid}: {msg}", file=sys.stderr)
                skipped.append({"uuid": ci_uuid, "reason": f"http-{code}"})
                continue

            except Exception as e:
                print(f"[invalidate] warning: failed to read CI {ci_uuid}: {e}", file=sys.stderr)
                skipped.append({"uuid": ci_uuid, "reason": type(e).__name__})
                continue

            # Keep only the fields we know invalidate/list accepts (matches frontend)
            citype = read.get("type")
            attrs = read.get("attributes")
            meta = read.get("metaAttributes")
            state = (meta or {}).get("state")
            if skip_if_invalidated and state == "INVALIDATED":
                skipped.append({"uuid": ci_uuid, "reason": "already-invalidated", "state": state})
                continue

            if not citype or not isinstance(attrs, list):
                print(f"[invalidate] warning: read/ci returned unexpected shape for {ci_uuid}", file=sys.stderr)
                skipped.append({"uuid": ci_uuid, "reason": "bad-shape"})
                continue

            ci_obj: Dict[str, Any] = {
                "type": citype,
                "uuid": ci_uuid,
                "attributes": attrs,
            }
            if isinstance(meta, dict):
                ci_obj["metaAttributes"] = meta

            configuration_items.append(ci_obj)

        if not configuration_items:
            return {
                "requestId": None,
                "noop": True,
                "reason": "all-already-invalidated-or-skipped",
                "skipped": skipped,
                "history_before": history_before_if_you_collected_it,
            }

        payload = {
            "configurationItemSet": configuration_items,
            "invalidateReason": {"comment": comment},
        }

        # --------------------
        # audit BEFORE (truthfully "before")
        # --------------------
        history_before: Dict[str, Any] | None = None
        before_vid: Dict[str, str | None] = {}

        if audit_history or self.verbose:
            ids = [ci["uuid"] for ci in configuration_items]
            history_before = self._audit_ci_latest_map(ids)
            for u, summ in history_before.items():
                if isinstance(summ, dict):
                    before_vid[u] = summ.get("versionId")
                else:
                    before_vid[u] = None

        if dry_run:
            print("DRY RUN (no POST) invalidate/list", file=sys.stderr)
            print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
            out: Dict[str, Any] = {"dryRun": True, "payload": payload, "skipped": skipped}
            if history_before is not None:
                out["history_before"] = history_before
            return out

        # --------------------
        # Real POST
        # --------------------
        res = self._invalidate_ci_api(payload=payload, lang=self.lang)

        # --------------------
        # audit AFTER (poll until versionId changes, best-effort)
        # --------------------
        history_after: Dict[str, Any] | None = None
        if audit_history or self.verbose:
            history_after = {}
            for u in [ci["uuid"] for ci in configuration_items]:
                try:
                    hv = self._wait_ci_history_version_change(
                        u,
                        prev_version_id=before_vid.get(u),
                        timeout_s=30.0,
                        poll_s=1.0,
                    )
                    history_after[u] = self.summarize_history_entry(hv) if hv else None
                except Exception as e:
                    history_after[u] = {"error": str(e)}

        # attach audits + skipped
        out_res = dict(res) if isinstance(res, dict) else {"result": res}
        if history_before is not None:
            out_res["history_before"] = history_before
        if history_after is not None:
            out_res["history_after"] = history_after
        if skipped:
            out_res["skipped"] = skipped
        return out_res


    def recycle_cis(
        self,
        ci_uuids: str | Iterable[str],
        *,
        domain: str = "biznis",
        dry_run: bool = False,
        audit_history: bool = False,
    ) -> Dict[str, Any]:
        uu = self._normalize_uuid_list(ci_uuids)
        if not uu:
            raise ValueError("ci_uuids cannot be empty.")

        payload = {"ciIdList": uu}

        # --------------------
        # audit BEFORE
        # --------------------
        history_before: Dict[str, Any] | None = None
        before_vid: Dict[str, str | None] = {}

        if audit_history or self.verbose:
            history_before = self._audit_ci_latest_map(uu)
            for u, summ in history_before.items():
                if isinstance(summ, dict):
                    before_vid[u] = summ.get("versionId")
                else:
                    before_vid[u] = None

        if dry_run:
            print(f"DRY RUN (no POST) recycle/cis/{domain}", file=sys.stderr)
            print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
            out: Dict[str, Any] = {"dryRun": True, "payload": payload}
            if history_before is not None:
                out["history_before"] = history_before
            return out

        # --------------------
        # Real POST
        # --------------------
        res = self._recycle_cis_api(domain=domain, payload=payload, lang=self.lang)

        # --------------------
        # audit AFTER
        # --------------------
        history_after: Dict[str, Any] | None = None
        if audit_history or self.verbose:
            history_after = {}
            for u in uu:
                try:
                    hv = self._wait_ci_history_version_change(
                        u,
                        prev_version_id=before_vid.get(u),
                        timeout_s=30.0,
                        poll_s=1.0,
                    )
                    history_after[u] = self.summarize_history_entry(hv) if hv else None
                except Exception as e:
                    history_after[u] = {"error": str(e)}

        out_res = dict(res) if isinstance(res, dict) else {"result": res}
        if history_before is not None:
            out_res["history_before"] = history_before
        if history_after is not None:
            out_res["history_after"] = history_after
        return out_res


    def recycle_rels(
        self,
        rel_uuids: str | Iterable[str],
        *,
        dry_run: bool = False,
        audit_history: bool = False,
    ) -> Dict[str, Any]:
        uu = self._normalize_uuid_list(rel_uuids)
        if not uu:
            raise ValueError("rel_uuids cannot be empty.")

        payload = {"relIdList": uu}

        # --------------------
        # audit BEFORE
        # --------------------
        history_before: Dict[str, Any] | None = None
        before_vid: Dict[str, str | None] = {}

        if audit_history or self.verbose:
            history_before = self._audit_rel_latest_map(uu)
            for u, summ in history_before.items():
                if isinstance(summ, dict):
                    before_vid[u] = summ.get("versionId")
                else:
                    before_vid[u] = None

        if dry_run:
            print("DRY RUN (no POST) recycle/rels", file=sys.stderr)
            print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
            out: Dict[str, Any] = {"dryRun": True, "payload": payload}
            if history_before is not None:
                out["history_before"] = history_before
            return out

        # --------------------
        # Real POST
        # --------------------
        res = self._recycle_rels_api(payload=payload, lang=self.lang)

        # --------------------
        # audit AFTER
        # --------------------
        history_after: Dict[str, Any] | None = None
        if audit_history or self.verbose:
            history_after = {}
            for u in uu:
                try:
                    hv = self._wait_rel_history_version_change(
                        u,
                        prev_version_id=before_vid.get(u),
                        timeout_s=30.0,
                        poll_s=1.0,
                    )
                    history_after[u] = self.summarize_rel_history_entry(hv) if hv else None
                except Exception as e:
                    history_after[u] = {"error": str(e)}

        out_res = dict(res) if isinstance(res, dict) else {"result": res}
        if history_before is not None:
            out_res["history_before"] = history_before
        if history_after is not None:
            out_res["history_after"] = history_after
        return out_res

    # ---------------- #
    # history fetching #
    # ---------------- #

    def _audit_ci_latest_map(self, uuids: list[str]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for u in uuids:
            try:
                hv = self.fetch_ci_history_latest(u)
                out[u] = self.summarize_history_entry(hv) if hv else None
            except Exception as e:
                out[u] = {"error": str(e)}
        return out

    def _audit_rel_latest_map(self, uuids: list[str]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for u in uuids:
            try:
                hv = self.fetch_rel_history_latest(u)
                out[u] = self.summarize_rel_history_entry(hv) if hv else None
            except Exception as e:
                out[u] = {"error": str(e)}
        return out

    def _wait_ci_history_version_change(
        self,
        ci_uuid: str,
        *,
        prev_version_id: str | None,
        timeout_s: float = 20.0,
        poll_s: float = 1.0,
    ) -> Dict[str, Any] | None:
        t0 = time.time()
        last = None
        while time.time() - t0 < timeout_s:
            hv = self.fetch_ci_history_latest(ci_uuid)
            last = hv
            if hv is None:
                time.sleep(poll_s)
                continue
            vid = hv.get("versionId")
            if prev_version_id is None or (vid and vid != prev_version_id):
                return hv
            time.sleep(poll_s)
        return last

    def _wait_rel_history_version_change(
        self,
        rel_uuid: str,
        *,
        prev_version_id: str | None,
        timeout_s: float = 20.0,
        poll_s: float = 1.0,
    ) -> Dict[str, Any] | None:
        t0 = time.time()
        last = None
        while time.time() - t0 < timeout_s:
            hv = self.fetch_rel_history_latest(rel_uuid)
            last = hv
            if hv is None:
                time.sleep(poll_s)
                continue
            vid = hv.get("versionId")
            if prev_version_id is None or (vid and vid != prev_version_id):
                return hv
            time.sleep(poll_s)
        return last

    def _ci_history_page_api(self, *, ci_uuid: str, page: int, per_page: int, lang: str) -> Dict[str, Any]:
        url = f"{self.base}/api/cmdb/history/read/ci/{ci_uuid}/list"
        j = self._request_json(
            "GET",
            url,
            reason=f"read CI history {ci_uuid}",
            params={"page": page, "perPage": per_page, "lang": lang},
            timeout=self.timeout_store,
            require_auth=True,
        )
        if not isinstance(j, dict):
            raise RuntimeError(f"Unexpected history payload type: {type(j)}")
        return j


    def fetch_ci_history(
        self,
        ci_uuid: str,
        *,
        per_page: int = 50,
        max_pages: int | None = None,
    ) -> list[Dict[str, Any]]:
        """
        Fetch CI history versions (paged). Returns list of historyVersions.
        NOTE: backend may cap perPage (your response showed perPage=10 even when requesting 1000).
        """
        ci_uuid = str(ci_uuid).strip()
        if not ci_uuid:
            raise ValueError("ci_uuid cannot be empty.")

        out: list[Dict[str, Any]] = []
        page = 1
        while True:
            j = self._ci_history_page_api(ci_uuid=ci_uuid, page=page, per_page=per_page, lang=self.lang)
            versions = j.get("historyVersions") or []
            if not isinstance(versions, list):
                raise RuntimeError("historyVersions is not a list.")
            out.extend([v for v in versions if isinstance(v, dict)])

            pag = j.get("pagination") or {}
            total_pages = pag.get("totalPages")
            try:
                total_pages_i = int(total_pages) if total_pages is not None else page
            except Exception:
                total_pages_i = page

            if max_pages is not None and page >= max_pages:
                break
            if page >= total_pages_i:
                break
            page += 1

        return out


    def fetch_ci_history_latest(self, ci_uuid: str) -> Dict[str, Any] | None:
        """
        Fetch only the newest history entry (fast path).
        """
        versions = self.fetch_ci_history(ci_uuid, per_page=20, max_pages=1)
        if not versions:
            return None
        # API seems to return newest-first; if that changes, sort by actionTime.
        return versions[0]


    def summarize_history_entry(self, hv: Dict[str, Any]) -> Dict[str, Any]:
        """
        Compact summary for logs / printing.
        """
        actions = hv.get("actions") or []
        if not isinstance(actions, list):
            actions = [str(actions)]
        actions_s = [str(x) for x in actions]

        item = hv.get("item") or {}
        meta = (item.get("metaAttributes") or {}) if isinstance(item, dict) else {}
        state = meta.get("state")

        return {
            "actionTime": hv.get("actionTime"),
            "actionBy": hv.get("actionBy"),
            "actions": actions_s,
            "versionId": hv.get("versionId"),
            "state": state,
        }

    def _rel_history_page_api(self, *, rel_uuid: str, page: int, per_page: int, lang: str) -> Dict[str, Any]:
        rel_uuid = str(rel_uuid).strip()
        if not rel_uuid:
            raise ValueError("rel_uuid cannot be empty.")

        url = f"{self.base}/api/cmdb/history/read/rel/{rel_uuid}/list"
        j = self._request_json(
            "GET",
            url,
            reason=f"read relation history {rel_uuid}",
            params={"page": page, "perPage": per_page, "lang": lang},
            timeout=self.timeout_store,
            require_auth=True,
        )
        if not isinstance(j, dict):
            raise RuntimeError(f"Unexpected relation history payload type: {type(j)}")
        return j


    def fetch_rel_history(
        self,
        rel_uuid: str,
        *,
        per_page: int = 50,
        max_pages: int | None = None,
    ) -> list[Dict[str, Any]]:
        rel_uuid = str(rel_uuid).strip()
        if not rel_uuid:
            raise ValueError("rel_uuid cannot be empty.")

        out: list[Dict[str, Any]] = []
        page = 1
        while True:
            j = self._rel_history_page_api(rel_uuid=rel_uuid, page=page, per_page=per_page, lang=self.lang)

            versions = j.get("historyVersions") or []
            if not isinstance(versions, list):
                raise RuntimeError("historyVersions is not a list.")
            out.extend([v for v in versions if isinstance(v, dict)])

            pag = j.get("pagination") or {}
            total_pages = pag.get("totalPages")
            try:
                total_pages_i = int(total_pages) if total_pages is not None else page
            except Exception:
                total_pages_i = page

            if max_pages is not None and page >= max_pages:
                break
            if page >= total_pages_i:
                break
            page += 1

        return out


    def fetch_rel_history_latest(self, rel_uuid: str) -> Dict[str, Any] | None:
        versions = self.fetch_rel_history(rel_uuid, per_page=20, max_pages=1)
        if not versions:
            return None
        return versions[0]


    def summarize_rel_history_entry(self, hv: Dict[str, Any]) -> Dict[str, Any]:
        actions = hv.get("actions") or []
        if not isinstance(actions, list):
            actions = [str(actions)]
        actions_s = [str(x) for x in actions]

        item = hv.get("item") or {}
        meta = (item.get("metaAttributes") or {}) if isinstance(item, dict) else {}
        state = meta.get("state")

        return {
            "actionTime": hv.get("actionTime"),
            "actionBy": hv.get("actionBy"),
            "actions": actions_s,
            "versionId": hv.get("versionId"),
            "state": state,
        }

    # ----------------------------
    # dry-run output
    # ----------------------------

    def _print_dry_run(self, *, citype: str, payload: Dict[str, Any]) -> None:
        print("DRY RUN (no POST)")
        print("base:", self.base)
        print("would GET schema:", f"{self.base}/api/types-repo/citypes/citype/{citype}")
        print("would GET cicode:", "(SKIPPED due to dry_run; using fabricated cicode)")
        print("would POST:", f"{self.base}/api/cmdb/store/ci?lang={self.lang}")
        if self.verbose:
            print("client_id:", self.client_id)
            print("redirect_uri:", self.redirect_uri or f"{self.base}/auth-success")
            env_user = (os.environ.get("METAIS_USER") or "").strip()
            print("user:", env_user or "<unset METAIS_USER>")
            print("bearer:", _redact_token(self._bearer or ""))
        print("\nPayload:")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
