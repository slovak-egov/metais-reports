#!/usr/bin/env bash
set -euo pipefail

APIURI_DEFAULT="https://metais.slovensko.sk/api/report/reports/run?lang=sk"

expand_tilde () {
  local p="${1:-}"
  if [[ "$p" == "~/"* ]]; then
    printf '%s\n' "${HOME}/${p#~/}"
  elif [[ "$p" == "~" ]]; then
    printf '%s\n' "${HOME}"
  else
    printf '%s\n' "$p"
  fi
}

resolve_groovy_path () {
  local arg="$1"
  local base_dir="$(dirname "$SCRIPT_DIR")/groovy"

  # Expand tilde first
  arg="$(expand_tilde "$arg")"

  # If it's an absolute or relative path containing a slash, respect it
  if [[ "$arg" == */* ]]; then
    echo "$(ensure_groovy_ext "$arg")"
    return
  fi

  # Otherwise, assume it's in groovy/ subdir
  local candidate="${base_dir}/$(ensure_groovy_ext "$arg")"
  echo "$candidate"
}

ensure_groovy_ext () {
  local p="$1"
  [[ "${p##*.}" == "groovy" ]] && { printf '%s\n' "$p"; return; }
  printf '%s\n' "${p}.groovy"
}

# Decide OUT_JSON/OUT_CSV from --output
compute_outputs () {
  local out_base="$1"
  if [[ "${out_base##*.}" == "json" ]]; then
    OUT_JSON="$out_base"
    OUT_BASE_NOEXT="${out_base%.*}"
  else
    OUT_BASE_NOEXT="$out_base"
    OUT_JSON="${OUT_BASE_NOEXT}.json"
  fi
  OUT_CSV="${OUT_BASE_NOEXT}.csv"
  export OUT_JSON OUT_CSV OUT_BASE_NOEXT
}

# Page/perPage → JSON or null
normalize_paging () {
  local page_set="${1:-0}" page_val="${2:-}"
  local per_set="${3:-0}" per_val="${4:-}"
  if (( page_set )); then PAGE_JSON="$page_val"; else PAGE_JSON="null"; fi
  if (( per_set ));  then PERPAGE_JSON="$per_val"; else PERPAGE_JSON="null"; fi
  export PAGE_JSON PERPAGE_JSON
}

print_help () {
  cat <<'EOF'
Usage: run.sh [options]

Options:
  -o, --output NAME       Output name/path for JSON (with or without .json).
  -d, --outdir DIR        Directory to write outputs (default: ../output).
  -p, --page N            Page number (omit to exclude from payload).
  -P, --per-page N        Page size (omit to exclude from payload).
  -s, --script PATH       Groovy script path (with or without .groovy).
                          (Optional if SCRIPT_CONTENT is provided.)
  -A, --api URL           Override API endpoint (default: metais-test URL).
      --params PATH       Parameters JSON file (default: params/params.json).
      --no-csv            Skip conversion from JSON to CSV.
  -k, --insecure          Allow insecure server connections.
  -h, -H, --help          Show this help.

Env:
  TOKEN                   Bearer token (required).
  SCRIPT_CONTENT          Inline Groovy script body. If set, it overrides -s
                          and the script file does not need to exist.

Notes:
- Run the script from the repo directory: $ run/run.sh
- If page/per-page are omitted, they are not sent in the payload.
- OUT_JSON is the raw API response; OUT_CSV is the convert target.
- CSV conversion is performed only for TABLE payloads (skipped for RAW).
EOF
}