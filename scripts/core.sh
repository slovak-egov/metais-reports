#!/usr/bin/env bash
set -euo pipefail

# Expects:
#   TOKEN          – Bearer token (required)
#   APIURI         – API endpoint (required)
#   SCRIPT_PATH    – path to .groovy file (optional if SCRIPT_CONTENT is set)
#   SCRIPT_CONTENT – inline Groovy body (optional if SCRIPT_PATH is set)
#   PARAMS_PATH    – path to params.json (required)
#   OUT_JSON       – output file path for JSON (required)
#   PAGE_JSON      – page number or 'null' (string, required)
#   PERPAGE_JSON   – perPage number or 'null' (string, required)

: "${TOKEN:?TOKEN env var is required}"
: "${APIURI:?APIURI env var is required}"
: "${PARAMS_PATH:?PARAMS_PATH env var is required}"
: "${OUT_JSON:?OUT_JSON env var is required}"
: "${PAGE_JSON:?PAGE_JSON env var is required}"
: "${PERPAGE_JSON:?PERPAGE_JSON env var is required}"

# Must have either inline content or a path to read
if [[ -z "${SCRIPT_CONTENT:-}" ]]; then
  : "${SCRIPT_PATH:?SCRIPT_PATH is required when SCRIPT_CONTENT is empty}"
  # shellcheck disable=SC2155
  export SCRIPT_CONTENT="$(cat "$SCRIPT_PATH")"
fi

_payload="$(mktemp)"
_resp_tmp="$(mktemp)"
trap 'rm -f "$_payload" "$_resp_tmp"' EXIT

jq -n \
  --arg body "$SCRIPT_CONTENT" \
  --rawfile params_raw "$PARAMS_PATH" \
  --argjson page "$PAGE_JSON" \
  --argjson perPage "$PERPAGE_JSON" '
    ($params_raw | try fromjson catch (error("params.json is not valid JSON"))) as $params
    |
    {
      body: $body,
      parameters: $params,
      page: $page,
      perPage: $perPage
    }
    | del(.page   | select(.==null))
    | del(.perPage| select(.==null))
  ' > "$_payload"

curl_flags=(
  -sS
  -X POST "$APIURI"
  -H "Authorization: Bearer $TOKEN"
  -H "Content-Type: application/json"
  --data @"$_payload"
  -o "$_resp_tmp"
  -w '%{http_code}'
)

if [[ "${RUN_INSECURE:-0}" -eq 1 ]]; then
  curl_flags+=(-k)
fi

http_code="$(curl "${curl_flags[@]}")"

if [[ "$http_code" == "200" ]]; then
  if jq -e . "$_resp_tmp" > /dev/null 2>&1; then
    mv -f "$_resp_tmp" "$OUT_JSON"
  else
    echo "ERROR: API returned non-JSON despite 200 OK. Refusing to write $OUT_JSON." >&2
    echo "------ Response (first 200 chars) ------" >&2
    head -c 200 "$_resp_tmp" >&2; echo >&2
    exit 1
  fi
else
  echo "HTTP ERROR: $http_code from $APIURI" >&2
  sz=$(wc -c <"$_resp_tmp")
  if (( sz > 0 )); then
    echo "------ Server response ------" >&2
    if jq -e . "$_resp_tmp" >/dev/null 2>&1; then
      jq . <"$_resp_tmp" >&2
    else
      cat "$_resp_tmp" >&2
    fi
  else
    echo "(empty response body)" >&2
  fi
  case "$http_code" in
    401|403) echo "Hint: your TOKEN may be missing/expired/wrong audience." >&2 ;;
    400)     echo "Hint: payload likely malformed (check script.groovy and params.json)." >&2 ;;
    404)     echo "Hint: endpoint may differ between test/prod or lang param." >&2 ;;
  esac
  exit 1
fi