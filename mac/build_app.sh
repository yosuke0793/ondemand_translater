#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$SCRIPT_DIR/TranslateService.app"
SOURCE_FILE="$SCRIPT_DIR/TranslateServiceLauncher.m"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
WORKER_DIR="$PROJECT_DIR/source/worker"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "ERROR: TranslateService.app is macOS-only." >&2
  exit 1
fi
ARCH="$(uname -m)"
if [[ "$ARCH" != "arm64" && "$ARCH" != "x86_64" ]]; then
  echo "ERROR: Unsupported macOS architecture: $ARCH" >&2
  echo "Supported architectures: arm64 (Apple Silicon), x86_64 (Intel)." >&2
  exit 1
fi
if ! command -v clang >/dev/null 2>&1; then
  echo "ERROR: clang was not found. Install Xcode Command Line Tools." >&2
  exit 1
fi
if [[ ! -f "$SOURCE_FILE" || ! -f "$WORKER_DIR/run_standalone.sh" ]]; then
  echo "ERROR: project files are incomplete." >&2
  exit 1
fi

mkdir -p "$APP_DIR/Contents/MacOS"
clang -framework Foundation "$SOURCE_FILE" -o "$APP_DIR/Contents/MacOS/TranslateService"
chmod +x "$APP_DIR/Contents/MacOS/TranslateService"

/usr/libexec/PlistBuddy -c "Delete :CFBundleExecutable" "$APP_DIR/Contents/Info.plist" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :CFBundleExecutable string TranslateService" "$APP_DIR/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Delete :CFBundlePackageType" "$APP_DIR/Contents/Info.plist" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :CFBundlePackageType string APPL" "$APP_DIR/Contents/Info.plist"

plutil -lint "$APP_DIR/Contents/Info.plist"
echo "Built: $APP_DIR"
echo "Architecture: $ARCH"
