#!/bin/zsh
set -u
set -o pipefail

SCRIPT_DIR="${0:A:h}"
MODE="${1:-}"
[[ -n "$MODE" ]] && shift
DRY_RUN="false"
ACTION=""
PASSTHROUGH=()

while (( $# > 0 )); do
  case "$1" in
    --dry-run)
      DRY_RUN="true"
      PASSTHROUGH+=("$1")
      shift
      ;;
    --action)
      if (( $# < 2 )); then
        print -u2 "FAILED: --action requires stage, restore, or restore-all."
        exit 2
      fi
      ACTION="$2"
      shift 2
      ;;
    *)
      PASSTHROUGH+=("$1")
      shift
      ;;
  esac
done

if [[ "$DRY_RUN" == "true" ]]; then
  LOG_FILE="/tmp/terranash-bootstrap-dry-run-$(/bin/date +%Y%m%d-%H%M%S)-$$.log"
  SSD_LOG_FILE=""
else
  LOG_DIR="${HOME:-}/Library/Logs/TerraNash"
  if [[ -z "${HOME:-}" ]] || ! /bin/mkdir -p "$LOG_DIR" 2>/dev/null || [[ ! -w "$LOG_DIR" ]]; then
    LOG_DIR="/tmp"
  fi
  LOG_FILE="$LOG_DIR/terranash-bootstrap-$(/bin/date +%Y%m%d-%H%M%S)-$$.log"
  SSD_LOG_FILE=""
  if [[ -d "$SCRIPT_DIR/logs" && -w "$SCRIPT_DIR/logs" ]]; then
    SSD_LOG_FILE="$SCRIPT_DIR/logs/terranash-bootstrap-$(/bin/date +%Y%m%d-%H%M%S)-$$.log"
  fi
fi

finish() {
  local code="$1"
  printf '\nLog: %s\n' "$LOG_FILE"
  [[ -n "$SSD_LOG_FILE" ]] && printf 'SSD log copy: %s\n' "$SSD_LOG_FILE"
  if [[ -t 0 && "${TERRANASH_NONINTERACTIVE:-0}" != "1" ]]; then
    printf '\nPress Return to close…'
    read -r
  fi
  return "$code"
}

python_bin() {
  local candidate
  for candidate in /usr/bin/python3 "$(command -v python3 2>/dev/null || true)"; do
    if [[ -x "$candidate" ]] && "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 9))' >/dev/null 2>&1; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  print -u2 "FAILED: Python 3.9 or newer is required. Finish Pinokio Install Tools, then rerun."
  return 1
}

run_stage() {
  local python
  python=$(python_bin) || return 1
  case "$MODE" in
    --mac-apps)
      "$python" "$SCRIPT_DIR/mac_apps.py" "${PASSTHROUGH[@]}"
      ;;
    --studios)
      "$python" "$SCRIPT_DIR/fleet_bootstrap.py" --studios-only "${PASSTHROUGH[@]}"
      ;;
    --models)
      run_models "$python"
      ;;
    *)
      print -u2 "FAILED: unknown bootstrap mode."
      return 2
      ;;
  esac
}

run_models() {
  local python="$1" model_root="$SCRIPT_DIR/../studio-models" home="" command=()
  if [[ -z "$ACTION" ]]; then
    printf '\n== Manage AI Models ==\n'
    printf '1) Update the SSD from models and fleet voices already downloaded on this Mac\n'
    printf '2) Copy approved SSD models and fleet voices (8 GB Voice: Qwen 0.6B Base only)\n'
    printf '3) Advanced: copy every SSD model regardless of this Mac\x27s memory\n'
    printf 'q) Cancel\n\nChoice: '
    read -r ACTION
    case "$ACTION" in
      1) ACTION="stage" ;;
      2) ACTION="restore" ;;
      3) ACTION="restore-all" ;;
      q|Q) printf 'Cancelled. Nothing changed.\n'; return 0 ;;
      *) print -u2 "FAILED: choose 1, 2, 3, or q."; return 2 ;;
    esac
  fi

  case "$ACTION" in
    stage)
      command=("$python" "$SCRIPT_DIR/studio_models.py" stage --root "$model_root")
      ;;
    restore|restore-all)
      home=$(PYTHONPATH="$SCRIPT_DIR" "$python" -c 'import fleet_bootstrap; value=fleet_bootstrap.resolve_pinokio_home(); print(value or "")')
      if [[ -z "$home" ]]; then
        print -u2 "FAILED: Stage 2 is incomplete. Finish Pinokio tools and run 2 Install Studios.command first."
        return 1
      fi
      local app canonical legacy
      for app in imagestudio-mac voicestudio-mac studiohub-mac; do
        canonical="$home/api/$app"
        legacy="$home/api/$app.git"
        if [[ ! -d "$canonical/.git" && ! -d "$legacy/.git" ]]; then
          print -u2 "FAILED: Stage 2 is incomplete; $app is not installed."
          return 1
        fi
      done
      command=("$python" "$SCRIPT_DIR/studio_models.py" restore --root "$model_root" --pinokio-home "$home")
      [[ "$ACTION" == "restore-all" ]] && command+=(--all)
      ;;
    *)
      print -u2 "FAILED: --action must be stage, restore, or restore-all."
      return 2
      ;;
  esac

  local option prune_requested="false"
  for option in "${PASSTHROUGH[@]}"; do
    [[ "$option" == "--dry-run" ]] || command+=("$option")
    [[ "$option" == "--prune" ]] && prune_requested="true"
  done

  if [[ "$DRY_RUN" == "true" ]]; then
    if [[ "$prune_requested" == "true" && "$ACTION" != "stage" ]]; then
      "${command[@]}" --plan
      return $?
    fi
    printf 'Would run:'
    printf ' %q' "${command[@]}"
    printf ' --plan\n'
    printf 'No applications, Studios, enrollment, or model files were changed.\n'
    return 0
  fi
  "${command[@]}"
}

if [[ -n "$SSD_LOG_FILE" ]]; then
  run_stage 2>&1 | /usr/bin/tee "$LOG_FILE" "$SSD_LOG_FILE"
else
  run_stage 2>&1 | /usr/bin/tee "$LOG_FILE"
fi
exit_code=${pipestatus[1]}
finish "$exit_code"
exit "$exit_code"
