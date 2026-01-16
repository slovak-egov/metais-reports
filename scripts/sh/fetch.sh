#!/usr/bin/env bash
set -euo pipefail

URL="${METAIS_REPORT_EXEC_URL:-}"

usage() {
  cat <<'EOF'
Usage:
  fetch.sh --target nodes|rels [--safe] [--valid] [--limit N] [--offset N]
           [--type NAME] [--src NAME] [--tgt NAME] [--save FILE]
           [--url URL]

Notes:
- --safe adds {"mode":"safe"}.
- --valid adds {"validOnly": true} (filters out INVALIDATED).
- --limit/--offset are included only if provided.
- --type sets "type" (node citype or relation reltype depending on Groovy logic).
- --src sets "sourceType", --tgt sets "targetType".
- If TOKEN is exported, adds Authorization: Bearer $TOKEN.
EOF
}

TARGET=""
SAFE=0
VALID=0
LIMIT=""
OFFSET=""
TYPE=""
SRC=""
TGT=""
SAVE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target) TARGET="${2:-}"; shift 2;;
    --safe) SAFE=1; shift;;
    --valid) VALID=1; shift;;
    --limit) LIMIT="${2:-}"; shift 2;;
    --offset) OFFSET="${2:-}"; shift 2;;
    --type) TYPE="${2:-}"; shift 2;;
    --src) SRC="${2:-}"; shift 2;;
    --tgt) TGT="${2:-}"; shift 2;;
    --save) SAVE="${2:-}"; shift 2;;
    --url) URL="${2:-}"; shift 2;;
    -h|--help) usage; exit 0;;
    *) echo "Unknown arg: $1" >&2; usage; exit 2;;
  esac
done

if [[ -z "$URL" ]]; then
  echo "Missing report execute URL. Set METAIS_REPORT_EXEC_URL or pass --url." >&2
  exit 2
fi

if [[ -z "$TARGET" ]]; then
  echo "Missing --target" >&2
  usage
  exit 2
fi

# normalize target to what your Groovy expects
case "$TARGET" in
  node|nodes|entity|entities) TARGET="nodes";;
  rel|rels|relation|relations) TARGET="relations";;
  *) echo "Bad --target: $TARGET (use nodes|rels)" >&2; exit 2;;
esac

# validate numeric args if provided
if [[ -n "$LIMIT" ]] && ! [[ "$LIMIT" =~ ^[0-9]+$ ]]; then
  echo "Bad --limit: $LIMIT (must be integer >= 0)" >&2
  exit 2
fi
if [[ -n "$OFFSET" ]] && ! [[ "$OFFSET" =~ ^[0-9]+$ ]]; then
  echo "Bad --offset: $OFFSET (must be integer >= 0)" >&2
  exit 2
fi

command -v jq >/dev/null || { echo "jq not found. Install: sudo apt-get install -y jq" >&2; exit 2; }

LIMIT_JSON="null";  [[ -n "$LIMIT"  ]] && LIMIT_JSON="$LIMIT"
OFFSET_JSON="null"; [[ -n "$OFFSET" ]] && OFFSET_JSON="$OFFSET"
SAFE_JSON="false";  [[ "$SAFE" -eq 1 ]] && SAFE_JSON="true"
VALID_JSON="false"; [[ "$VALID" -eq 1 ]] && VALID_JSON="true"

PAYLOAD="$(
  jq -n \
    --arg target "$TARGET" \
    --arg type "$TYPE" \
    --arg src "$SRC" \
    --arg tgt "$TGT" \
    --argjson limit "$LIMIT_JSON" \
    --argjson offset "$OFFSET_JSON" \
    --argjson safe "$SAFE_JSON" \
    --argjson valid "$VALID_JSON" \
    '{
       parameters: { target: $target }
     }
     | if $safe  then .parameters.mode="safe" else . end
     | if $valid then .parameters.validOnly=true else . end
     | if $limit  != null then .parameters.limit=$limit else . end
     | if $offset != null then .parameters.offset=$offset else . end
     | if ($type|length) > 0 then .parameters.type=$type else . end
     | if ($src|length)  > 0 then .parameters.sourceType=$src else . end
     | if ($tgt|length)  > 0 then .parameters.targetType=$tgt else . end'
)"

CURL_ARGS=(
  -sS -X POST "$URL"
  -H "Content-Type: application/json"
  -H "Accept: application/json"
  -d "$PAYLOAD"
)

if [[ -n "${TOKEN:-}" ]]; then
  CURL_ARGS+=( -H "Authorization: Bearer $TOKEN" )
fi

if [[ -n "$SAVE" ]]; then
  curl "${CURL_ARGS[@]}" | tee "$SAVE"
  echo "" >&2
  echo "Saved to: $SAVE" >&2
else
  curl "${CURL_ARGS[@]}"
fi