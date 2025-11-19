#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: $0 <TYPE> [--template <tpl.groovy>] [--limit N] [--offset N] [--no-csv] [--outdir DIR]" >&2
  exit 1
}

TYPE="${1:-}"; shift || true
[[ -z "${TYPE}" ]] && usage

TPL="groovy/templates/extract_raw_template.groovy"
LIMIT=""
OFFSET=""
OUTDIR="output/nodes"
EXTRA_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --template) TPL="${2:?}"; shift 2;;
    --limit)    LIMIT="${2:?}"; shift 2;;
    --offset)   OFFSET="${2:?}"; shift 2;;
    --outdir)   OUTDIR="${2:?}"; shift 2;;
    --no-csv)   EXTRA_ARGS+=("--no-csv"); shift;;
    *)          EXTRA_ARGS+=("$1"); shift;;
  esac
done

# Resolve this script's directory (…/scripts)
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# Prepare SCRIPT_CONTENT by substituting placeholders.
SCRIPT_CONTENT="$(cat "${TPL}")"
SCRIPT_CONTENT="${SCRIPT_CONTENT//__TYPE__/${TYPE}}"

if [[ -n "${LIMIT}" ]]; then
  SCRIPT_CONTENT="${SCRIPT_CONTENT//__LIMIT__/${LIMIT}}"
else
  SCRIPT_CONTENT="${SCRIPT_CONTENT//__LIMIT__/1000000000}"  # effectively no limit
fi

if [[ -n "${OFFSET}" ]]; then
  SCRIPT_CONTENT="${SCRIPT_CONTENT//__OFFSET__/${OFFSET}}"
else
  SCRIPT_CONTENT="${SCRIPT_CONTENT//__OFFSET__/0}"
fi

export SCRIPT_CONTENT

# Delegate to sibling run.sh (NOT run/run.sh)
"${SCRIPT_DIR}/run.sh" -o "${TYPE}" --outdir "${OUTDIR}" "${EXTRA_ARGS[@]}"