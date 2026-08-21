#!/usr/bin/env bash
# End-to-end RCC-AID -> DICOM LABELMAP SEG conversion.
#
#   scripts/run_all.sh                       # full run, all 129 cases
#   scripts/run_all.sh --cases A B C         # restrict the per-case steps to a pilot subset
#   RCC_JOBS=8 scripts/run_all.sh            # override parallelism
#
# Expects the uv venv to exist: `uv sync`. dcmqi comes from the pinned PyPI package, so nothing
# needs to be on PATH.
set -euo pipefail

cd "$(dirname "$0")/.."
PY=.venv/bin/python
JOBS="${RCC_JOBS:-}"
JOBS_ARG=()
[[ -n "$JOBS" ]] && JOBS_ARG=(--jobs "$JOBS")

if [[ ! -x "$PY" ]]; then
  echo "no venv at $PY -- run: uv sync" >&2
  exit 1
fi
export PYTHONPATH=src

step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

step "verify terminology against DICOM PS3.16"
$PY scripts/verify_terminology.py

step "fetch RCC-AID annotations from Zenodo"
$PY -m rcc_aid.fetch_inputs

step "build the case index"
$PY -m rcc_aid.build_case_index

step "download source DICOM from IDC (8.27 GB)"
$PY -m rcc_aid.fetch_dicom --dry-run
$PY -m rcc_aid.fetch_dicom

step "resolve reference instances"
$PY -m rcc_aid.resolve_references "$@"

step "normalize label maps (integer cast, z-flip, DICOM geometry)"
$PY -m rcc_aid.normalize_labels "$@"

step "generate dcmqi metadata"
$PY -m rcc_aid.make_metadata "$@"

step "convert to DICOM SEG (labelmap + binary)"
$PY -m rcc_aid.convert "${JOBS_ARG[@]}" "$@"

step "quality control"
$PY -m rcc_aid.qc "${JOBS_ARG[@]}" "$@"

printf '\n\033[1mdone.\033[0m reports in qc/:\n'
ls -1 qc/*.csv
