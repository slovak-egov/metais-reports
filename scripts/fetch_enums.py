#!/usr/bin/env python3
import os
import sys
import json
import time
from pathlib import Path
from datetime import date

import requests

from config_env import load_env_file

# ----------------------------------------------------------------------
# ENV + BASIC PATHS
# ----------------------------------------------------------------------

load_env_file()


def find_project_root(start: Path) -> Path:
    current = start
    while current != current.parent:
        if (current / ".git").exists():
            return current
        current = current.parent
    return start


SCRIPT_DIR   = Path(__file__).resolve().parent
PROJECT_ROOT = find_project_root(SCRIPT_DIR)

METAIS_DATE = os.getenv("METAIS_DATE", date.today().strftime("%d-%m-%Y"))
RAW_ROOT    = PROJECT_ROOT / os.getenv("METAIS_RAW_ROOT", "output")
DATE_ROOT   = RAW_ROOT / METAIS_DATE

METADATA_ROOT = DATE_ROOT / "metadata"
ENUMS_DIR     = METADATA_ROOT / "enums"

METADATA_ROOT.mkdir(parents=True, exist_ok=True)
ENUMS_DIR.mkdir(parents=True, exist_ok=True)

ENUMS_LIST_URL = os.getenv(
    "METAIS_ENUMS_LIST_URL",
    "https://metais.slovensko.sk/api/enums-repo/enums/list",
)
ENUM_DETAIL_BASE = os.getenv(
    "METAIS_ENUM_DETAIL_URL",
    "https://metais.slovensko.sk/api/enums-repo/enums/enum/valid",
)

CONN_MAX_RETRIES = int(os.getenv("CONNECTION_MAX_RETRIES", "5"))
CONN_RETRY_DELAY = float(os.getenv("CONNECTION_RETRY_DELAY", "0.5"))
FETCH_TIMEOUT    = float(os.getenv("METAIS_FETCH_TIMEOUT", "60"))


# ----------------------------------------------------------------------
# HELPERS
# ----------------------------------------------------------------------

def fetch_json_with_retries(url: str):
    attempt = 1
    while CONN_MAX_RETRIES <= 0 or attempt <= CONN_MAX_RETRIES:
        try:
            r = requests.get(
                url,
                timeout=FETCH_TIMEOUT,
                headers={"Accept": "application/json"},
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if CONN_MAX_RETRIES > 0 and attempt >= CONN_MAX_RETRIES:
                raise
            print(
                f"[WARN] fetch_json_with_retries({url}) failed on attempt "
                f"{attempt}/{CONN_MAX_RETRIES}: {e}; "
                f"retrying in {CONN_RETRY_DELAY}s..."
            )
            time.sleep(CONN_RETRY_DELAY)
            attempt += 1


# ----------------------------------------------------------------------
# ENUMS FETCH
# ----------------------------------------------------------------------

def fetch_enums_list():
    print(f"[META] Fetching enums list from {ENUMS_LIST_URL}")
    data = fetch_json_with_retries(ENUMS_LIST_URL)

    list_path = METADATA_ROOT / "enums_list.json"
    with list_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"[META] Cached enums list -> {list_path}")

    if isinstance(data, dict) and "results" in data:
        return data["results"]
    return data


def fetch_one_enum(code: str) -> bool:
    url = f"{ENUM_DETAIL_BASE}/{code}"
    print(f"[ENUM] Fetching {code} from {url}")
    try:
        detail = fetch_json_with_retries(url)
    except Exception as e:
        print(f"[WARN] Could not fetch enum detail for {code}: {e}")
        return False

    out_path = ENUMS_DIR / f"{code}.json"
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(detail, f, ensure_ascii=False, indent=2)
    print(f"[ENUM] Cached enum {code} -> {out_path}")
    return True


def main():
    # Optional: allow specifying codes on CLI to limit what we fetch
    #   python fetch_enums.py KATEGORIA_OSOBA ZDROJ ...
    cli_codes = sys.argv[1:]

    enum_items = fetch_enums_list()
    if not enum_items:
        print("[ERROR] Enums list is empty or unavailable.", file=sys.stderr)
        sys.exit(1)

    # Build list of codes
    all_codes = [e.get("code") for e in enum_items if e.get("code")]
    if cli_codes:
        codes = [c for c in all_codes if c in cli_codes]
        missing = set(cli_codes) - set(codes)
        if missing:
            print(f"[WARN] Some requested codes not found in enums list: {', '.join(sorted(missing))}")
    else:
        codes = all_codes

    print(f"[INFO] Will fetch {len(codes)} enums.")
    ok = 0
    fail = 0
    for code in sorted(codes):
        if fetch_one_enum(code):
            ok += 1
        else:
            fail += 1

    print(f"[INFO] Completed enums: {ok} ok / {fail} failed.")

    # ------------------------------------------------------------------
    # MERGE ALL ENUMS INTO ONE BIG MAP: code -> human value
    # ------------------------------------------------------------------
    merged: dict[str, str] = {}
    collisions: list[dict] = []

    for code in sorted(codes):
        enum_path = ENUMS_DIR / f"{code}.json"
        if not enum_path.exists():
            print(f"[WARN] Enum file missing, skipping merge: {enum_path}")
            continue

        try:
            with enum_path.open("r", encoding="utf-8") as f:
                enum_data = json.load(f)
        except Exception as e:
            print(f"[WARN] Could not load enum {code} from {enum_path}: {e}")
            continue

        items = enum_data.get("enumItems") or []
        for item in items:
            item_code = item.get("code")
            if not item_code:
                continue

            # prefer Slovak label "value"; fall back to "engValue" or description
            val = (
                item.get("value") or
                item.get("engValue") or
                item.get("description") or
                item_code
            )

            if item_code in merged and merged[item_code] != val:
                # collision: same key, different text
                collisions.append({
                    "enum": code,
                    "item_code": item_code,
                    "old_value": merged[item_code],
                    "new_value": val,
                })
                print(
                    f"[WARN] Enum code collision for {item_code}: "
                    f"'{merged[item_code]}' vs '{val}'"
                )
                # keep the first value, ignore the new one
                continue

            merged[item_code] = val

    merged_path = METADATA_ROOT / "enums_merged.json"
    with merged_path.open("w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    print(f"[INFO] Wrote merged enums -> {merged_path} ({len(merged)} keys)")

    if collisions:
        collisions_path = METADATA_ROOT / "enums_collisions.json"
        with collisions_path.open("w", encoding="utf-8") as f:
            json.dump(collisions, f, ensure_ascii=False, indent=2)
        print(f"[WARN] Detected {len(collisions)} enum code collisions -> {collisions_path}")

if __name__ == "__main__":
    main()