#!/usr/bin/env bash
set -euo pipefail

mkdir -p flat
shopt -s nullglob

for infile in json/*.json; do
  base="$(basename "$infile" .json)"
  outfile="flat/${base}.json"

  jq '
    .result as $r
    | ($r.headers | map(.name)) as $cols
    | $r.rows
    | map(
        .values as $vals
        | reduce range(0; $cols | length) as $i (
            {};
            . + { ($cols[$i]): ($vals[$i] // "") }   # eplaces null with ""
          )
      )
  ' "$infile" > "$outfile"
done
