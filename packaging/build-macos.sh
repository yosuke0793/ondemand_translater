#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PYTHON="${PYTHON312:-python3.12}"
"$PYTHON" -c 'import sys; assert sys.version_info[:2] == (3, 12), "CPython 3.12 required"'
if [[ "$(uname -m)" == x86_64 ]] && ! command -v nasm >/dev/null 2>&1; then
    echo "NASM is required for Intel FFmpeg builds; install it (brew install nasm) and retry." >&2
    exit 1
fi
mkdir -p packaging/.work/tool-tmp
export TMPDIR="$ROOT/packaging/.work/tool-tmp"
export PIP_CACHE_DIR="$ROOT/packaging/.work/pip-cache"
export PIP_DISABLE_PIP_VERSION_CHECK=1
unset PYTHONHOME PYTHONPATH
if ! command -v cmake >/dev/null 2>&1; then
    if ! command -v brew >/dev/null 2>&1; then
        echo "CMake is required to build pinned HarfBuzz sources." >&2
        exit 1
    fi
    brew install cmake
fi
rm -rf packaging/.venv-build
"$PYTHON" -m venv packaging/.venv-build
PYTHON="$ROOT/packaging/.venv-build/bin/python"
TARGET="$("$PYTHON" -c 'import platform; print("macos-arm64" if platform.machine() == "arm64" else "macos-x64")')"
"$PYTHON" -m pip install --only-binary=:all: --require-hashes -r packaging/locks/bootstrap.txt
"$PYTHON" -m pip install --only-binary=:all: --no-binary=sacremoses --no-build-isolation --require-hashes -r "packaging/locks/$TARGET.txt"
"$PYTHON" -m pip check
"$PYTHON" packaging/build.py build --target "$TARGET" "$@"
