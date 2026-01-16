import json
from pathlib import Path

metadir = Path("output/08-12-2025/metadata/relations/")

all_attrs = set()
date_attrs = set()

for reltype in metadir.glob("*.json"):
    with reltype.open("r", encoding="utf-8") as f:
        data = json.load(f)
        for attr in data["attributes"]:
            name = attr["technicalName"]
            is_date = attr["type"] == "DATE"
            all_attrs.add(name)
            if is_date:
                date_attrs.add(name)
        
        for profile in data.get("attributeProfiles", []):
            for attr in profile.get("attributes", []):
                name = attr["technicalName"]
                is_date = attr["type"] == "DATE"
                all_attrs.add(name)
                if is_date:
                    date_attrs.add(name)


for name in all_attrs:
    print("\"" + name + "\",")

print("\n")

for name in date_attrs:
    print("\"" + name + "\",")