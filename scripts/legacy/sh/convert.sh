#!/usr/bin/env bash
# convert.sh — KS.json -> KS.csv using jq
# Usage: ./convert.sh input.json output.csv
set -euo pipefail

if [[ $# -lt 2 || $# -gt 3 ]]; then
  echo "Usage: $0 INPUT.json OUTPUT.csv [DELIM]" >&2
  echo "Default delimiter is ';'." >&2
  exit 1
fi

infile=$1
outfile=$2
DELIM=${3:-";"}

if ! command -v jq >/dev/null 2>&1; then
  echo "Error: jq is required but not found in PATH." >&2
  exit 1
fi

# Convert JSON -> CSV with quoting for fields containing delimiter, quotes, or newlines.
# Adds UTF-8 BOM at the top (for Excel compatibility).
jq -r --arg d "$DELIM" '
  # quote for CSV with custom delimiter
  def q($d):
    tostring
    | gsub("\"";"\"\"")
    | if test("[" + $d + "\n\r\"]") then "\""+.+"\"" else . end;

  .result as $r
  | [$r.headers[].name] as $h
  | ($h | map(q($d)) | join($d)),
    ($r.rows[] | [.values[]] | map(q($d)) | join($d))
' "$infile" \
| sed "1s/^/\xef\xbb\xbf/" > "$outfile"
