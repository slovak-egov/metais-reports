#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: $0 <CENTRAL> <OUTER> <RELATION_TYPE> [--template tpl.groovy] [--outdir DIR] [--no-csv] [--limit N] [--offset N] [extra args...]" >&2
  exit 1
}

CENTRAL="${1:-}"
OUTER="${2:-}"
TYPE_REL="${3:-}"
shift 3 || true

if [[ -z "$CENTRAL" || -z "$OUTER" || -z "$TYPE_REL" ]]; then
  usage
fi

# Defaults
TPL="groovy/templates/relation_template.groovy"
OUTDIR="output/relations"
EXTRA_ARGS=()
LIMIT=""
OFFSET="0"

# Parse optional flags (mirroring raw.sh style)
while [[ $# -gt 0 ]]; do
  case "$1" in
    --template) TPL="${2:?}"; shift 2 ;;
    --outdir)   OUTDIR="${2:?}"; shift 2 ;;
    --no-csv)   EXTRA_ARGS+=("--no-csv"); shift ;;
    --limit)    LIMIT="${2:?}"; shift 2 ;;      # NEW
    --offset)   OFFSET="${2:?}"; shift 2 ;;     # NEW
    *)          EXTRA_ARGS+=("$1"); shift ;;
  esac
done

# Sensible default if no limit was passed (effectively "no limit")
if [[ -z "$LIMIT" ]]; then
  LIMIT="1000000000"
fi

# Resolve this script's directory (…/scripts)
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# Build SCRIPT_CONTENT from the template with placeholders
SCRIPT_CONTENT="$(
  sed -e "s|__CENTRAL__|${CENTRAL}|g" \
      -e "s|__OUTER__|${OUTER}|g" \
      -e "s|__RELATION__|${TYPE_REL}|g" \
      -e "s|__LIMIT__|${LIMIT}|g" \
      -e "s|__OFFSET__|${OFFSET}|g" \
    "$TPL"
)"

export SCRIPT_CONTENT

# Delegate to sibling run.sh, using the OUTDIR we just parsed
"${SCRIPT_DIR}/run.sh" -o "${TYPE_REL}" --outdir "${OUTDIR}" "${EXTRA_ARGS[@]}"