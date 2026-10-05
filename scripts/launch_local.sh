#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PYTHON=${JEV_PYTHON:-"$ROOT/../jev-test-env/bin/python"}
if [ ! -x "$PYTHON" ]; then
    printf '%s\n' 'BLOCKED: PYTHON_RUNTIME_MISSING; set JEV_PYTHON to an existing configured virtualenv Python.' >&2
    exit 78
fi
cd "$ROOT"
exec "$PYTHON" "$ROOT/scripts/launch_local.py" "$@"
