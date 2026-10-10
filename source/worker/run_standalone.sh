#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="$SCRIPT_DIR/.venv"
PYTHON="${TRANSLATE_PYTHON:-$VENV_DIR/bin/python}"

if [[ -z "${TRANSLATE_PYTHON:-}" && ! -x "$PYTHON" ]]; then
  BOOTSTRAP_PYTHON=""
  for candidate in python3.10 python3.11 python3.12 python3.13 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      candidate_path="$(command -v "$candidate")"
      if candidate_version="$($candidate_path -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null)"; then
        case "$candidate_version" in
          3.10|3.11|3.12|3.13)
            BOOTSTRAP_PYTHON="$candidate_path"
            break
            ;;
        esac
      fi
    fi
  done

  if [[ -z "$BOOTSTRAP_PYTHON" ]]; then
    echo "ERROR: Python 3.10-3.13 was not found." >&2
    echo "Install Python 3.10-3.13 or set TRANSLATE_PYTHON." >&2
    exit 1
  fi

  echo "[setup] creating virtual environment: $VENV_DIR"
  "$BOOTSTRAP_PYTHON" -m venv "$VENV_DIR"
  PYTHON="$VENV_DIR/bin/python"
fi

if [[ ! -x "$PYTHON" ]]; then
  echo "ERROR: Python executable not found: $PYTHON" >&2
  exit 1
fi

if ! "$PYTHON" -c 'import tkinter' >/dev/null 2>&1; then
  echo "ERROR: tkinter is not available in $PYTHON." >&2
  echo "macOS: install a Python distribution with Tk support." >&2
  echo "Ubuntu: sudo apt install -y python3-tk" >&2
  exit 1
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ERROR: ffmpeg was not found in PATH." >&2
  echo "macOS: brew install ffmpeg-full" >&2
  echo "Ubuntu: sudo apt install -y ffmpeg" >&2
  exit 1
fi

missing_modules="$($PYTHON - <<'PY'
import importlib.util

modules = (
    "faster_whisper",
    "argostranslate",
    "edge_tts",
    "requests",
    "cv2",
    "pytesseract",
    "keyring",
)
print(" ".join(module for module in modules if importlib.util.find_spec(module) is None))
PY
)"

if [[ -n "$missing_modules" ]]; then
  echo "[setup] installing Python dependencies (missing: $missing_modules)"
  "$PYTHON" -m pip install -r requirements.txt
fi

exec "$PYTHON" standalone_app.py
