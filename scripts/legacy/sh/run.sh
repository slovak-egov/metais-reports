#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
. "${SCRIPT_DIR}/lib.sh"

# Defaults
RUN_STDOUT=0
OUTPUT_BASE="output"
SCRIPT_PATH="groovy/script.groovy"
PARAMS_PATH="params/params.json"
OUTDIR="$(dirname "$SCRIPT_DIR")/output"
RUN_CONVERT=1
APIURI="$APIURI_DEFAULT"
RUN_INSECURE=0
PAGE_SET=0; PERPAGE_SET=0
PAGE_VAL=""; PERPAGE_VAL=""

# --- parse args ---
while (( "$#" )); do
  case "$1" in
    -o|--output)   OUTPUT_BASE="${2:-}"; shift 2 ;;
    -d|--outdir)   OUTDIR="${2:-}"; shift 2 ;;
    -p|--page)     PAGE_VAL="${2:-}"; PAGE_SET=1; shift 2 ;;
    -P|--per-page) PERPAGE_VAL="${2:-}"; PERPAGE_SET=1; shift 2 ;;
    -s|--script)   SCRIPT_PATH="$(resolve_groovy_path "${2:-}")"; shift 2 ;;
    -A|--api)      APIURI="${2:-}"; shift 2 ;;
    -k|--insecure) RUN_INSECURE=1; shift ;;
    --params)      PARAMS_PATH="${2:-}"; shift 2 ;;
    --no-csv)      RUN_CONVERT=0; shift ;;
    --stdout)      RUN_STDOUT=1; shift ;;
    -h|-H|--help)  print_help; exit 0 ;;
    *) echo "Unknown option: $1"; echo "Try --help"; exit 1 ;;
  esac
done

# --- normalize paths & names ---
: "${TOKEN:?TOKEN env var is required}"

SCRIPT_PATH="$(expand_tilde "$SCRIPT_PATH")"
PARAMS_PATH="$(expand_tilde "$PARAMS_PATH")"
OUTPUT_BASE="$(expand_tilde "$OUTPUT_BASE")"
OUTDIR="$(expand_tilde "$OUTDIR")"

SCRIPT_PATH="$(ensure_groovy_ext "$SCRIPT_PATH")"

# Only require the file if we don't have inline content
if [[ -z "${SCRIPT_CONTENT:-}" ]]; then
  [[ -f "$SCRIPT_PATH" ]] || { echo "Script not found: $SCRIPT_PATH"; exit 1; }
fi
[[ -e "$PARAMS_PATH" ]] || { echo "Params not found: $PARAMS_PATH"; exit 1; }

mkdir -p "$OUTDIR"

# Derive default output base from script path only if none provided and we have a path
if [[ -z "${OUTPUT_BASE:-}" || "$OUTPUT_BASE" == "output" ]]; then
  if [[ -n "${SCRIPT_CONTENT:-}" && ! -f "$SCRIPT_PATH" ]]; then
    # Inline-only mode without a script file: give a generic base
    OUTPUT_BASE="inline_report"
  else
    script_basename="$(basename "$SCRIPT_PATH")"
    script_name="${script_basename%.groovy}"
    OUTPUT_BASE="$script_name"
  fi
fi

# Decide how we handle outputs
if [[ "$RUN_STDOUT" -eq 1 ]]; then
  # Stream JSON to stdout, no CSV
  OUT_JSON="-"
  RUN_CONVERT=0
else
  # Classic file-based mode
  compute_outputs "$OUTPUT_BASE"
  OUT_JSON="${OUTDIR}/$(basename "$OUT_JSON")"
  OUT_CSV="${OUTDIR}/$(basename "$OUT_CSV")"
fi

normalize_paging "$PAGE_SET" "$PAGE_VAL" "$PERPAGE_SET" "$PERPAGE_VAL"

# --- export contract for core.sh ---
export APIURI SCRIPT_PATH SCRIPT_CONTENT PARAMS_PATH OUT_JSON PAGE_JSON PERPAGE_JSON TOKEN RUN_INSECURE

# --- run core ---
"${SCRIPT_DIR}/core.sh"

# Only log file writes if we actually wrote a file
if [[ "$OUT_JSON" != "-" ]]; then
  echo "Wrote: $OUT_JSON"
fi

# --- optional convert (only in file mode) ---
if (( RUN_CONVERT )) && [[ "$OUT_JSON" != "-" ]]; then
  # Only convert if it's a TABLE payload
  if jq -e '.type? == "TABLE"' "$OUT_JSON" > /dev/null 2>&1; then
    if [[ -x "${SCRIPT_DIR}/convert.sh" ]]; then
      "${SCRIPT_DIR}/convert.sh" "$OUT_JSON" "$OUT_CSV"
      echo "Wrote: $OUT_CSV"
    elif [[ -x "./convert.sh" ]]; then
      ./convert.sh "$OUT_JSON" "$OUT_CSV"
      echo "Wrote: $OUT_CSV"
    else
      echo "NOTE: ${SCRIPT_DIR}/convert.sh not found; skipping CSV." >&2
    fi
  else
    echo "NOTE: Output is not TABLE; skipping CSV for $OUT_JSON." >&2
  fi
fi