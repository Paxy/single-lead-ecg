#!/bin/bash
# Command-line analysis — the same engine the web service uses.
#   ./report.sh [csv_file] [nominal_fs_Hz]
# With no arguments it analyses ./data/data.csv at fs=192.
# Python: $ECG_PYTHON if set, else the service venv, else python3.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INPUT="${1:-$DIR/data/data.csv}"
FS="${2:-192}"
PY="${ECG_PYTHON:-}"
if [ -z "$PY" ]; then
  if [ -x /opt/ecg-venv/bin/python3 ]; then PY=/opt/ecg-venv/bin/python3; else PY=python3; fi
fi
exec "$PY" "$DIR/ecg_analyze.py" \
    --input "$INPUT" \
    --outdir "$DIR/data" \
    --prefix data \
    --fs "$FS" \
    --archive-dir "$DIR/data/history" \
    --source cli \
    --job-json "$DIR/data/job.json"
