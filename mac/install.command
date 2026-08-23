#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
/bin/bash "$SCRIPT_DIR/install.sh"

printf '\nインストールが完了しました。Enterキーで終了します。\n'
read -r
