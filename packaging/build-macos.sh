#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PYTHON="${PYTHON312:-python3.12}"
"$PYTHON" -c 'import sys; assert sys.version_info[:2] == (3, 12), "CPython 3.12 required"'
mkdir -p packaging/.work/tool-tmp
export TMPDIR="$ROOT/packaging/.work/tool-tmp"
export PIP_CACHE_DIR="$ROOT/packaging/.work/pip-cache"
export PIP_DISABLE_PIP_VERSION_CHECK=1
unset PYTHONHOME PYTHONPATH
rm -rf packaging/.venv-build
"$PYTHON" -m venv packaging/.venv-build
PYTHON="$ROOT/packaging/.venv-build/bin/python"
TARGET="$("$PYTHON" -c 'import platform; print("macos-arm64" if platform.machine() == "arm64" else "macos-x64")')"
"$PYTHON" -m pip install --only-binary=:all: --require-hashes -r packaging/locks/bootstrap.txt
"$PYTHON" -m pip install --only-binary=:all: --no-binary=sacremoses --no-build-isolation --require-hashes -r "packaging/locks/$TARGET.txt"
"$PYTHON" -m pip check
"$PYTHON" packaging/build.py build --target "$TARGET" "$@"
