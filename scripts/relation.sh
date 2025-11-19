#!/usr/bin/env bash
set -euo pipefail

# usage: run/relation.sh <CENTRAL> <OUTER> <RELATION_TYPE> [extra args...]
# example: run/relation.sh KS PO PO_je_gestor_KS --no-csv

if [[ $# -lt 3 ]]; then
  echo "usage: $0 <CENTRAL> <OUTER> <RELATION_TYPE> [extra run args]" >&2
  exit 1
fi

CENTRAL="$1"; OUTER="$2"; TYPE_REL="$3"; shift 3 || true

# File base name = exact technical relation name, so metadata lookups match
report_base="$TYPE_REL"

TEMPLATE="groovy/templates/extract_relation_template.groovy"

# Prepare script from template
SCRIPT_CONTENT="$(
  sed -e "s|__CENTRAL__|${CENTRAL}|g" \
      -e "s|__OUTER__|${OUTER}|g" \
      -e "s|__RELATION__|${TYPE_REL}|g" \
    "$TEMPLATE"
)"

export SCRIPT_CONTENT
run/run.sh -o "$report_base" --outdir output/relations "$@"