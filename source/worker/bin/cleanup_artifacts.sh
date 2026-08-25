#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

MODE="dry-run"
INCLUDE_QUEUE=0

usage() {
  cat <<'EOF'
Safe bulk cleanup for translate_service artifacts.

Default is dry-run (no deletion).

Usage:
  ./bin/cleanup_artifacts.sh [--apply] [--include-queue] [--yes]

Options:
  --apply          Actually delete files (without this: dry-run only)
  --include-queue  Also delete queued/processing/done/error JSON files
  --yes            Skip confirmation prompt in --apply mode
  -h, --help       Show this help

Deleted targets (default):
  - logs/*.log, logs/*.out, logs/*.err
  - jobs/*/status.json
  - jobs/*/{audio.wav,jp.srt,jp_clean.srt,en.srt,en.txt,en.mp3,final.mp4,final_sub.mp4,output.mp4}
  - jobs/*/.tts_segments, jobs/*/.asr_chunks

NOT deleted by default:
  - queue/*.json, processing/*.json, done/*.json, error/*.json
  - jobs/*/input.mp4
EOF
}

AUTO_YES=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply)
      MODE="apply"
      shift
      ;;
    --include-queue)
      INCLUDE_QUEUE=1
      shift
      ;;
    --yes)
      AUTO_YES=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      exit 2
      ;;
  esac
done

must_be_within_base() {
  local p="$1"
  [[ "$p" == "$BASE_DIR"/* ]]
}

collect_targets() {
  local p

  for p in "$BASE_DIR/logs"/*.log "$BASE_DIR/logs"/*.out "$BASE_DIR/logs"/*.err; do
    [[ -e "$p" ]] && printf '%s\0' "$p"
  done

  while IFS= read -r -d '' p; do
    printf '%s\0' "$p"
  done < <(find "$BASE_DIR/jobs" -type f \
    \( -name 'status.json' -o -name 'audio.wav' -o -name 'jp.srt' -o -name 'jp_clean.srt' \
       -o -name 'en.srt' -o -name 'en.txt' -o -name 'en.mp3' -o -name 'final.mp4' \
       -o -name 'final_sub.mp4' -o -name 'output.mp4' \) -print0 2>/dev/null)

  while IFS= read -r -d '' p; do
    printf '%s\0' "$p"
  done < <(find "$BASE_DIR/jobs" -type d \( -name '.tts_segments' -o -name '.asr_chunks' \) -print0 2>/dev/null)

  if [[ $INCLUDE_QUEUE -eq 1 ]]; then
    for p in "$BASE_DIR/queue"/*.json "$BASE_DIR/processing"/*.json "$BASE_DIR/done"/*.json "$BASE_DIR/error"/*.json; do
      [[ -e "$p" ]] && printf '%s\0' "$p"
    done
  fi
}

mapfile -d '' TARGETS < <(collect_targets)

if [[ ${#TARGETS[@]} -eq 0 ]]; then
  echo "No cleanup targets found."
  exit 0
fi

echo "Base: $BASE_DIR"
echo "Mode: $MODE"
echo "Include queue-state json: $INCLUDE_QUEUE"
echo "Targets: ${#TARGETS[@]}"
for t in "${TARGETS[@]}"; do
  echo "  $t"
done

if [[ "$MODE" != "apply" ]]; then
  echo
  echo "Dry-run only. Use --apply to actually delete."
  exit 0
fi

if [[ $AUTO_YES -ne 1 ]]; then
  echo
  read -r -p "Delete the targets above? [y/N] " answer
  if [[ "$answer" != "y" && "$answer" != "Y" ]]; then
    echo "Cancelled."
    exit 0
  fi
fi

for t in "${TARGETS[@]}"; do
  if ! must_be_within_base "$t"; then
    echo "Refusing to delete path outside base: $t" >&2
    exit 1
  fi
done

for t in "${TARGETS[@]}"; do
  if [[ -d "$t" ]]; then
    rm -rf -- "$t"
  else
    rm -f -- "$t"
  fi
done

echo "Cleanup completed: ${#TARGETS[@]} targets deleted."
