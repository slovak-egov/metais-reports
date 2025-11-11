#!/usr/bin/env bash

IDS=(43101 43102 43103 43104 43105 43106)
NAMES=(KS AS ISVS Projekt InfraSluzba KRIS)

BASE_URL="https://metais.slovensko.sk/api/report/reports/execute"
LANG="sk"

mkdir -p data/json

for i in "${!IDS[@]}"; do
  id="${IDS[$i]}"
  filename="${NAMES[$i]}"
  out="json/${filename}.json"
  curl --location "${BASE_URL}/${id}/type/typ?lang=${LANG}" \
  --header 'Content-Type: application/json' \
  --data '{
      "parameters": {
          "inclApplication": "true"
      }
  }' \
  -o "${out}"
done
