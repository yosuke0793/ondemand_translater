#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
WORKER_DIR="$PROJECT_DIR/source/worker"
VENV_DIR="$WORKER_DIR/.venv"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "ERROR: This installer supports macOS only." >&2
  exit 1
fi
ARCH="$(uname -m)"
if [[ "$ARCH" != "arm64" && "$ARCH" != "x86_64" ]]; then
  echo "ERROR: Unsupported macOS architecture: $ARCH" >&2
  echo "Supported architectures: arm64 (Apple Silicon), x86_64 (Intel)." >&2
  exit 1
fi
if [[ ! -d "$WORKER_DIR" ]]; then
  echo "ERROR: source/worker was not found: $WORKER_DIR" >&2
  exit 1
fi

if ! command -v brew >/dev/null 2>&1; then
  if [[ -x /opt/homebrew/bin/brew ]]; then
    export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:$PATH"
  elif [[ -x /usr/local/bin/brew ]]; then
    export PATH="/usr/local/bin:/usr/local/sbin:$PATH"
  fi
fi

if ! command -v brew >/dev/null 2>&1; then
  cat >&2 <<'EOF'
ERROR: Homebrew is required but was not found.
Install Homebrew from https://brew.sh/ and run install.command again.
EOF
  exit 1
fi

BREW_PREFIX="$(brew --prefix)"
export PATH="$BREW_PREFIX/bin:$BREW_PREFIX/sbin:$BREW_PREFIX/opt/ffmpeg-full/bin:/opt/homebrew/opt/ffmpeg-full/bin:/usr/local/opt/ffmpeg-full/bin:$PATH"

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "[setup] installing FFmpeg"
  brew install ffmpeg-full
fi

PYTHON="${TRANSLATE_PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
  for candidate in python3.10 python3.11 python3.12 python3.13; do
    if command -v "$candidate" >/dev/null 2>&1; then
      if version="$($candidate -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null)"; then
        case "$version" in
          3.10|3.11|3.12|3.13) PYTHON="$(command -v "$candidate")"; break ;;
        esac
      fi
    fi
  done
fi
if [[ -z "$PYTHON" ]]; then
  for fallback_version in 3.12 3.10; do
    echo "[setup] installing Python $fallback_version"
    if brew install "python@$fallback_version"; then
      fallback_python="$BREW_PREFIX/opt/python@$fallback_version/bin/python$fallback_version"
      if [[ -x "$fallback_python" ]]; then
        PYTHON="$fallback_python"
        break
      fi
    fi
  done
fi
if [[ -z "$PYTHON" ]]; then
  echo "ERROR: Python 3.12 or 3.10 could not be installed." >&2
  exit 1
fi
if ! "$PYTHON" -c 'import tkinter' >/dev/null 2>&1; then
  python_minor="$($PYTHON -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  echo "[setup] installing Tk support for Python $python_minor"
  brew install "python-tk@$python_minor"
  if ! "$PYTHON" -c 'import tkinter' >/dev/null 2>&1; then
    echo "ERROR: $PYTHON does not include tkinter." >&2
    echo "Install a Python distribution with Tk support, then rerun install.command." >&2
    exit 1
  fi
fi

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  echo "[setup] creating virtual environment"
  "$PYTHON" -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install -r "$WORKER_DIR/requirements.txt"

"$SCRIPT_DIR/build_app.sh"
echo "Installation complete: $SCRIPT_DIR/TranslateService.app"
echo "Double-click TranslateService.app in Finder to launch."
