#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
PYTHON="python3"
if [ -x .venv/bin/python ]; then PYTHON=.venv/bin/python; elif [ -x venv/bin/python ]; then PYTHON=venv/bin/python; fi
exec "$PYTHON" start_live.py
