#!/usr/bin/env python3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import ijson
import matplotlib.pyplot as plt
import matplotlib.dates as mdates


def parse_iso_datetime(s: str | None):
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def get_state(node: dict) -> str:
    meta = node.get("metaAttributes") or {}
    return meta.get("state", "<missing>")


def get_created_at(node: dict):
    meta = node.get("metaAttributes") or {}
    return parse_iso_datetime(meta.get("createdAt"))


def get_modified_at(node: dict):
    meta = node.get("metaAttributes") or {}
    return parse_iso_datetime(meta.get("lastModifiedAt"))


def main():
    in_path = Path("/home/rabatinb/metais-reports/output/20-11-2025/nodes/KS.json")

    print(f"[INFO] Streaming INVALIDATED KS from {in_path}")

    created_counts = Counter()   # date -> count
    modified_counts = Counter()  # date -> count
    total = 0
    invalidated = 0

    with in_path.open("rb") as f:
        for node in ijson.items(f, "result.item"):
            total += 1
            if get_state(node) != "INVALIDATED":
                continue
            invalidated += 1

            c = get_created_at(node)
            if c is not None:
                created_counts[c.date()] += 1

            m = get_modified_at(node)
            if m is not None:
                modified_counts[m.date()] += 1

    print(f"Total KS records     : {total}")
    print(f"INVALIDATED records  : {invalidated}")
    print(f"Distinct created days: {len(created_counts)}")
    print(f"Distinct mod days    : {len(modified_counts)}")

    if not created_counts:
        print("[ERROR] No createdAt dates found for INVALIDATED records.")
        sys.exit(1)

    # Build sorted date axis covering full range
    all_dates = sorted(set(created_counts.keys()) | set(modified_counts.keys()))
    created_series = [created_counts[d] for d in all_dates]
    modified_series = [modified_counts[d] for d in all_dates]

    fig, ax = plt.subplots(figsize=(14, 6))

    ax.plot(all_dates, created_series, label="createdAt (per day)")
    ax.plot(all_dates, modified_series, label="lastModifiedAt (per day)")

    ax.set_xlabel("Date")
    ax.set_ylabel("Count of INVALIDATED KS")
    ax.set_title("KS INVALIDATED: daily counts of createdAt vs lastModifiedAt")

    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    plt.xticks(rotation=45, ha="right")
    ax.legend()
    plt.tight_layout()

    out_png = in_path.parent / "KS_invalidated_hist_daily.png"
    plt.savefig(out_png, dpi=150)
    print(f"[INFO] Saved plot to {out_png}")

    # Optional if you want the window:
    # plt.show()


if __name__ == "__main__":
    main()