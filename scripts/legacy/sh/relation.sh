#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: $0 <TARGET> <SOURCE> <RELATION_TYPE> [--template tpl.groovy] [--outdir DIR] [--no-csv] [--limit N] [--offset N] [extra args...]" >&2
  exit 1
}

TARGET="${1:-}"
SOURCE="${2:-}"
TYPE_REL="${3:-}"
shift 3 || true

if [[ -z "$TARGET" || -z "$SOURCE" || -z "$TYPE_REL" ]]; then
  usage
fi

# Defaults
TPL="groovy/templates/relation_template.groovy"
OUTDIR="output/relations"
EXTRA_ARGS=()
LIMIT=""
OFFSET="0"

# Parse optional flags
while [[ $# -gt 0 ]]; do
  case "$1" in
    --template) TPL="${2:?}"; shift 2 ;;
    --outdir)   OUTDIR="${2:?}"; shift 2 ;;
    --no-csv)   EXTRA_ARGS+=("--no-csv"); shift ;;
    --limit)    LIMIT="${2:?}"; shift 2 ;;
    --offset)   OFFSET="${2:?}"; shift 2 ;;
    *)          EXTRA_ARGS+=("$1"); shift ;;
  esac
done

# Defaults if not given
[[ -z "$LIMIT"  ]] && LIMIT="1000000000"
[[ -z "$OFFSET" ]] && OFFSET="0"

# Resolve this script's directory (…/scripts)
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# Substitute placeholders in the Groovy template
SCRIPT_CONTENT="$(
  sed -e "s|__TARGET__|${TARGET}|g" \
      -e "s|__SOURCE__|${SOURCE}|g" \
      -e "s|__RELATION__|${TYPE_REL}|g" \
      -e "s|__LIMIT__|${LIMIT}|g" \
      -e "s|__OFFSET__|${OFFSET}|g" \
      "$TPL"
)"

export SCRIPT_CONTENT

# Delegate to run.sh
"${SCRIPT_DIR}/run.sh" -o "${TYPE_REL}" --outdir "${OUTDIR}" "${EXTRA_ARGS[@]}"