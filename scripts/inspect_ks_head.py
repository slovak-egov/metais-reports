#!/usr/bin/env python3
import sys
from collections import Counter, defaultdict
import json
import ijson

# Adjust if needed
path = "/home/rabatinb/metais-reports/output/20-11-2025/nodes/KS.json"


def get_code(node: dict) -> str | None:
    """
    Extract MetaIS code from a KS node.
    attributes[].name == "Gen_Profil_kod_metais"
    """
    for attr in node.get("attributes", []):
        if attr.get("name") == "Gen_Profil_kod_metais":
            return attr.get("value")
    return None


def get_name(node: dict) -> str | None:
    """
    Extract human-readable name from a KS node.
    attributes[].name == "Gen_Profil_nazov"
    """
    for attr in node.get("attributes", []):
        if attr.get("name") == "Gen_Profil_nazov":
            return attr.get("value")
    return None


def get_state(node: dict) -> str:
    meta = node.get("metaAttributes") or {}
    return meta.get("state", "<missing>")


total = 0

# codes
missing_code = 0
code_counts = Counter()
codes_per_state = defaultdict(Counter)

# names
missing_name = 0
name_counts = Counter()
names_per_state = defaultdict(Counter)

state_counts = Counter()

preview = []

print(f"[INFO] Streaming KS from {path}")

with open(path, "rb") as f:
    for node in ijson.items(f, "result.item"):
        total += 1

        code = get_code(node)
        name = get_name(node)
        state = get_state(node)

        state_counts[state] += 1

        # codes
        if code is None or code == "":
            missing_code += 1
        else:
            code_counts[code] += 1
            codes_per_state[state][code] += 1

        # names
        if name is None or (isinstance(name, str) and not name.strip()):
            missing_name += 1
        else:
            # normalize a bit if you want:
            norm_name = name.strip()
            name_counts[norm_name] += 1
            names_per_state[state][norm_name] += 1

        if len(preview) < 5:
            preview.append({
                "uuid": node.get("uuid"),
                "code": code,
                "name": name,
                "state": state,
            })

print("\n=== KS summary ===")
print(f"Total KS records           : {total}")

# codes
print(f"\n--- Codes ---")
print(f"Records with no code       : {missing_code}")
print(f"Distinct MetaIS codes      : {len(code_counts)}")
code_dupes = [c for c, n in code_counts.items() if n > 1]
print(f"Codes with duplicates      : {len(code_dupes)}")

# names
print(f"\n--- Names (Gen_Profil_nazov) ---")
print(f"Records with no name       : {missing_name}")
print(f"Distinct names             : {len(name_counts)}")
name_dupes = [nm for nm, n in name_counts.items() if n > 1]
print(f"Names with duplicates      : {len(name_dupes)}")

print("\nState distribution:")
for st, n in state_counts.most_common():
    print(f"  {st:12s} : {n}")

print("\nPer-state distinct codes (top 5 states):")
for st, n in state_counts.most_common(5):
    print(f"  {st:12s} : {len(codes_per_state[st])} distinct codes")

print("\nPer-state distinct names (top 5 states):")
for st, n in state_counts.most_common(5):
    print(f"  {st:12s} : {len(names_per_state[st])} distinct names")

if code_dupes:
    print("\nTop 10 most duplicated codes:")
    for code, n in code_counts.most_common(10):
        print(f"  {repr(code)} : {n} records")

if name_dupes:
    print("\nTop 10 most duplicated names:")
    for nm, n in name_counts.most_common(10):
        print(f"  {repr(nm)} : {n} records")

print("\nSample records (uuid, code, name, state):")
print(json.dumps(preview, ensure_ascii=False, indent=2))