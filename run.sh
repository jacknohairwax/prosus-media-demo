#!/usr/bin/env bash
# collect -> classify -> build_page; stops at the first failing step.
set -euo pipefail
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
"$PY" collect.py
"$PY" classify.py
"$PY" build_page.py
