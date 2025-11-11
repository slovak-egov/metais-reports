import json, csv, os, sys

in_dir = "data/json"
out_dir = "data/csv"
os.makedirs(out_dir, exist_ok=True)

for filename in os.listdir(in_dir):
    if not filename.endswith(".json"):
        continue
    infile = os.path.join(in_dir, filename)
    outfile = os.path.join(out_dir, filename.replace(".json", ".csv"))

    with open(infile, "r", encoding="utf-8") as f:
        data = json.load(f)

    headers = [h["name"] for h in data["result"]["headers"]]
    rows = [r["values"] for r in data["result"]["rows"]]

    # Write CSV (semicolon delimiter, UTF-8 BOM for Excel)
    with open(outfile, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, delimiter=";", quoting=csv.QUOTE_ALL)
        writer.writerow(headers)
        for r in rows:
            # Replace None with empty string
            writer.writerow(["" if v is None else v for v in r])
