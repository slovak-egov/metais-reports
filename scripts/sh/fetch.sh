#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  fetch.sh --target nodes|rels [--safe] [--valid] [--limit N] [--offset N]
           [--type NAME] [--src NAME] [--tgt NAME] [--save FILE]
           [--url URL] [--prod | --test]

Environment / URL resolution (PROD default):
- Default behavior is PROD (metais.slovensko.sk).
- Use --test to switch to TEST (metais-test.slovensko.sk).
- If --url is provided, it is used directly and env-based resolution is skipped.

When resolving URL from env (no --url):
- PROD: requires METAIS_REPORT_NUM_PROD (numeric report ID), builds:
  https://metais.slovensko.sk/api/report/reports/execute/{NUM}/type/typ?lang=sk
- TEST: requires METAIS_REPORT_NUM_TEST (numeric report ID), builds:
  https://metais-test.slovensko.sk/api/report/reports/execute/{NUM}/type/typ?lang=sk

Notes:
- --safe adds {"mode":"safe"}.
- --valid adds {"validOnly": true} (filters out INVALIDATED).
- --limit/--offset are included only if provided.
- --type sets "type" (node citype or relation reltype depending on Groovy logic).
- --src sets "sourceType", --tgt sets "targetType".
- If TOKEN is exported, adds Authorization: Bearer $TOKEN.
EOF
}

die() { echo "Error: $*" >&2; exit 2; }

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "$1 not found. Install it (e.g. sudo apt-get install -y $1)."
}

is_uint() { [[ "${1:-}" =~ ^[0-9]+$ ]]; }

# Inputs
URL=""

# Environment-provided report IDs
REPORT_NUM_PROD="${METAIS_REPORT_NUM_PROD:-}"
REPORT_NUM_TEST="${METAIS_REPORT_NUM_TEST:-}"

TARGET=""
SAFE=0
VALID=0
LIMIT=""
OFFSET=""
TYPE=""
SRC=""
TGT=""
SAVE=""

# Env selection (default PROD)
USE_PROD=1
USE_TEST=0
SEEN_PROD=0
SEEN_TEST=0

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
    --prod) SEEN_PROD=1; USE_PROD=1; USE_TEST=0; shift;;
    --test) SEEN_TEST=1; USE_TEST=1; USE_PROD=0; shift;;
    -h|--help) usage; exit 0;;
    *) die "Unknown arg: $1 (use --help)";;
  esac
done

if [[ "$SEEN_PROD" -eq 1 && "$SEEN_TEST" -eq 1 ]]; then
  die "Cannot use both --prod and --test."
fi

[[ -n "$TARGET" ]] || { usage; die "Missing --target"; }

# normalize target to what your Groovy expects
case "$TARGET" in
  node|nodes|entity|entities) TARGET="nodes";;
  rel|rels|relation|relations) TARGET="relations";;
  *) die "Bad --target: $TARGET (use nodes|rels)";;
esac

# validate numeric args if provided
if [[ -n "$LIMIT" ]] && ! is_uint "$LIMIT"; then
  die "Bad --limit: $LIMIT (must be integer >= 0)"
fi
if [[ -n "$OFFSET" ]] && ! is_uint "$OFFSET"; then
  die "Bad --offset: $OFFSET (must be integer >= 0)"
fi

require_cmd jq
require_cmd curl

# Resolve URL: either explicit --url, or build from env + (--prod/--test)
if [[ -z "$URL" ]]; then
  if [[ "$USE_TEST" -eq 1 ]]; then
    [[ -n "$REPORT_NUM_TEST" ]] || die "Missing METAIS_REPORT_NUM_TEST (numeric report ID) or pass --url."
    is_uint "$REPORT_NUM_TEST" || die "Bad METAIS_REPORT_NUM_TEST: $REPORT_NUM_TEST (must be integer >= 0)"
    URL="https://metais-test.slovensko.sk/api/report/reports/execute/${REPORT_NUM_TEST}/type/typ?lang=sk"
  else
    [[ -n "$REPORT_NUM_PROD" ]] || die "Missing METAIS_REPORT_NUM_PROD (numeric report ID) or pass --url."
    is_uint "$REPORT_NUM_PROD" || die "Bad METAIS_REPORT_NUM_PROD: $REPORT_NUM_PROD (must be integer >= 0)"
    URL="https://metais.slovensko.sk/api/report/reports/execute/${REPORT_NUM_PROD}/type/typ?lang=sk"
  fi
fi

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
