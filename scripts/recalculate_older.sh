#!/usr/bin/env bash

export METAIS_SNAPSHOT_DATE=19-11-2025

python3 scripts/calculate_reports.py \
  --nodes-dir      "output/19-11-2025/nodes" \
  --relations-dir  "output/19-11-2025/relations" \
  --out-attrs      "meta-viz/data/stats/19-11-2025/nodes" \
  --out-rel-attrs  "meta-viz/data/stats/19-11-2025/relations"