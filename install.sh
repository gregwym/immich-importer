#!/bin/sh
# Run from any working directory; no pip or administrative access required.
set -eu
installer_python=${PYTHON_BIN:-python3}
if ! command -v "$installer_python" >/dev/null 2>&1; then
    echo 'Python 3.8+ is required. Install the runtime yourself or set PYTHON_BIN.' >&2
    exit 1
fi
installer_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "$installer_python" -u -B "$installer_dir/tools/install.py" "$@"
