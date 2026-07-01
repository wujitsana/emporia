#!/usr/bin/env bash
# Thin wrapper — same as: installer/install.py --local-demo
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY="${ROOT}/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "Missing .venv — run: uv sync  (or pip install -e .)" >&2
  exit 1
fi
exec "$PY" installer/install.py --local-demo "$@"