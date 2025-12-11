#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<EOF
usage: $0 --kind node|rel [--template tpl.groovy] [--limit N] [--offset N] [--outname NAME] [--no-csv] [--outdir DIR]

Env:
  GROOVY_DIR   Directory with Groovy templates (optional, defaults to ../groovy/template
               relative to this script if not set, unless --template is given).
EOF
  exit 1
}

KIND=""
TPL=""
LIMIT=""
OFFSET=""
OUTDIR="output/global"
OUTNAME=""
EXTRA_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --kind)
      KIND="${2:-}"; shift 2;;
    --template)
      TPL="${2:-}"; shift 2;;
    --limit)
      LIMIT="${2:-}"; shift 2;;
    --offset)
      OFFSET="${2:-}"; shift 2;;
    --outdir)
      OUTDIR="${2:-}"; shift 2;;
    --outname)
      OUTNAME="${2:-}"; shift 2;;
    --no-csv)
      EXTRA_ARGS+=("--no-csv"); shift;;
    *)
      EXTRA_ARGS+=("$1"); shift;;
  esac
done

if [[ -z "${KIND}" ]]; then
  echo "[ERROR] --kind node|rel is required" >&2
  usage
fi

if [[ "${KIND}" != "node" && "${KIND}" != "rel" && "${KIND}" != "relnotype" ]]; then
  echo "[ERROR] --kind must be 'node', 'rel', or 'relnotype', got '${KIND}'" >&2
  usage
fi

# Resolve this script's directory once
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# Provide a default GROOVY_DIR if not set: ../groovy/template relative to this script
: "${GROOVY_DIR:=${SCRIPT_DIR}/../groovy/template}"

# Resolve default template from GROOVY_DIR if not explicitly given
if [[ -z "${TPL}" ]]; then
  if [[ "${KIND}" == "node" ]]; then
    TPL="${GROOVY_DIR}/entity_template_agnostic_all.groovy"
  elif [[ "${KIND}" == "relnotype" ]]; then
    TPL="${GROOVY_DIR}/relation_template_agnostic_notype.groovy"
  else
    TPL="${GROOVY_DIR}/relation_template_agnostic_all.groovy"
  fi
fi

if [[ -z "${OUTNAME}" ]]; then
  if [[ "${KIND}" == "node" ]]; then
    OUTNAME="nodes_all"
  else
    OUTNAME="relations_all"
  fi
fi

SCRIPT_CONTENT="$(cat "${TPL}")"

# Plug paging into __LIMIT__ / __OFFSET__
if [[ -n "${LIMIT}" ]]; then
  SCRIPT_CONTENT="${SCRIPT_CONTENT//__LIMIT__/${LIMIT}}"
else
  SCRIPT_CONTENT="${SCRIPT_CONTENT//__LIMIT__/1000000000}"
fi

if [[ -n "${OFFSET}" ]]; then
  SCRIPT_CONTENT="${SCRIPT_CONTENT//__OFFSET__/${OFFSET}}"
else
  SCRIPT_CONTENT="${SCRIPT_CONTENT//__OFFSET__/0}"
fi

export SCRIPT_CONTENT

# Delegate to run.sh; -o just names the output file
"${SCRIPT_DIR}/run.sh" -o "${OUTNAME}" --outdir "${OUTDIR}" "${EXTRA_ARGS[@]}"