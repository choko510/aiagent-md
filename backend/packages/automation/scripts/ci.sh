#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
python -m pytest -q -p no:cacheprovider "$ROOT/tests"
if command -v ruff >/dev/null 2>&1; then
  ruff check "$ROOT/src" "$ROOT/tests"
  ruff format --check "$ROOT/src" "$ROOT/tests"
fi
