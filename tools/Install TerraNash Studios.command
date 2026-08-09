#!/bin/zsh
set -u
set -o pipefail

SCRIPT_DIR="${0:A:h}"
PINOKIO_VERSION="8.0.40"
PINOKIO_SHA256="3c0f55f769efc2c02e5d0b8bc24e2ee7b0be54d42e6404663887e0cf8d3df3fd"
PINOKIO_DMG="$SCRIPT_DIR/installers/Pinokio-$PINOKIO_VERSION-arm64.dmg"
PINOKIO_APP="/Applications/Pinokio.app"
TOP_LEVEL_DRY_RUN="false"
for argument in "$@"; do
  [[ "$argument" == "--dry-run" ]] && TOP_LEVEL_DRY_RUN="true"
done
if [[ "$TOP_LEVEL_DRY_RUN" == "true" ]]; then
  LOG_FILE="/tmp/terranash-bootstrap-dry-run-$(/bin/date +%Y%m%d-%H%M%S).log"
else
  mkdir -p "$SCRIPT_DIR/logs"
  LOG_FILE="$SCRIPT_DIR/logs/bootstrap-$(/bin/date +%Y%m%d-%H%M%S).log"
fi

finish() {
  local exit_code=$1
  printf '\nLog: %s\n' "$LOG_FILE"
  printf '\nPress Return to close…'
  read -r
  exit "$exit_code"
}

stage_zero() {
  printf '\n== Stage 0: Pinokio runtime ==\n'
  local dry_run="false"
  local argument
  for argument in "$@"; do
    [[ "$argument" == "--dry-run" ]] && dry_run="true"
  done
  if [[ "$(/usr/bin/uname -s)" != "Darwin" || "$(/usr/bin/uname -m)" != "arm64" ]]; then
    printf 'FAILED: this fleet kit supports Apple-silicon Macs only.\n' >&2
    return 1
  fi

  local current_version=""
  if [[ -d "$PINOKIO_APP" ]]; then
    current_version=$(/usr/bin/plutil -extract CFBundleShortVersionString raw \
      "$PINOKIO_APP/Contents/Info.plist" 2>/dev/null || true)
  fi
  if [[ "$current_version" != "$PINOKIO_VERSION" ]]; then
    if [[ "$dry_run" == "true" ]]; then
      printf 'Would verify and install Pinokio %s from the SSD.\n' "$PINOKIO_VERSION"
    else
      if [[ ! -f "$PINOKIO_DMG" ]]; then
        printf 'FAILED: missing %s. Re-stage the SSD from Studio Hub.\n' "$PINOKIO_DMG" >&2
        return 1
      fi
      local actual_hash
      actual_hash=$(/usr/bin/shasum -a 256 "$PINOKIO_DMG" | /usr/bin/awk '{print $1}')
      if [[ "$actual_hash" != "$PINOKIO_SHA256" ]]; then
        printf 'FAILED: the Pinokio installer failed its SHA-256 check. Re-stage the SSD.\n' >&2
        return 1
      fi
      /usr/bin/osascript -e 'tell application "Pinokio" to quit' >/dev/null 2>&1 || true
      /bin/sleep 2
      local mount_dir
      mount_dir=$(/usr/bin/mktemp -d -t terranash-pinokio)
      if ! /usr/bin/hdiutil attach -readonly -nobrowse -mountpoint "$mount_dir" "$PINOKIO_DMG"; then
        /bin/rmdir "$mount_dir" 2>/dev/null || true
        return 1
      fi
      if [[ ! -d "$mount_dir/Pinokio.app" ]]; then
        printf 'FAILED: the verified installer contains no Pinokio.app.\n' >&2
        /usr/bin/hdiutil detach "$mount_dir" >/dev/null 2>&1 || true
        /bin/rmdir "$mount_dir" 2>/dev/null || true
        return 1
      fi
      printf 'Installing Pinokio %s (one administrator-password prompt)…\n' "$PINOKIO_VERSION"
      /usr/bin/sudo /usr/bin/ditto "$mount_dir/Pinokio.app" "$PINOKIO_APP" || {
        /usr/bin/hdiutil detach "$mount_dir" >/dev/null 2>&1 || true
        /bin/rmdir "$mount_dir" 2>/dev/null || true
        return 1
      }
      /usr/bin/hdiutil detach "$mount_dir" >/dev/null
      /bin/rmdir "$mount_dir" 2>/dev/null || true
      /usr/bin/codesign --verify --deep --strict "$PINOKIO_APP" || return 1
      /usr/sbin/spctl --assess --type execute "$PINOKIO_APP" || return 1
    fi
  else
    printf 'Pinokio %s is already installed.\n' "$current_version"
  fi

  if [[ "$dry_run" != "true" ]]; then
    /usr/bin/open "$PINOKIO_APP"
    printf 'Waiting for Pinokio first-run setup and bundled tools…\n'
  fi
  local pinokio_home="" pterm_path="" python_path=""
  local count=0
  local attempts=450
  [[ "$dry_run" == "true" ]] && attempts=1
  while (( count < attempts )); do
    if [[ -f "$HOME/.pinokio/config.json" ]]; then
      pinokio_home=$(/usr/bin/plutil -extract home raw -o - \
        "$HOME/.pinokio/config.json" 2>/dev/null || true)
    fi
    if [[ -n "$pinokio_home" ]]; then
      for candidate in "$pinokio_home/bin/npm/bin/pterm" "$pinokio_home/bin/pterm"; do
        if [[ -x "$candidate" ]]; then
          pterm_path="$candidate"
          break
        fi
      done
    fi
    if [[ -z "$pinokio_home" ]]; then
      pinokio_home=$(/usr/bin/curl -fsS --max-time 2 \
        http://127.0.0.1:42000/pinokio/home 2>/dev/null | \
        /usr/bin/plutil -extract path raw -o - - 2>/dev/null || true)
    fi
    if [[ -z "$pterm_path" ]]; then
      pterm_path=$(/usr/bin/curl -fsS --max-time 2 \
        http://127.0.0.1:42000/pinokio/path/pterm 2>/dev/null | \
        /usr/bin/plutil -extract path raw -o - - 2>/dev/null || true)
      [[ -x "$pterm_path" ]] || pterm_path=""
    fi
    if [[ -z "$pterm_path" ]]; then
      pterm_path=$(command -v pterm 2>/dev/null || true)
      [[ -x "$pterm_path" ]] || pterm_path=""
    fi
    if [[ -n "$pterm_path" ]]; then
      python_path=$("$pterm_path" which python3 --json 2>/dev/null | \
        /usr/bin/plutil -extract path raw -o - - 2>/dev/null || true)
      [[ -x "$python_path" ]] && break
      python_path=""
      local system_python
      system_python=$(command -v python3 2>/dev/null || true)
      if [[ -x "$system_python" ]] && \
          "$system_python" -c 'import sys; raise SystemExit(sys.version_info < (3, 9))' \
            >/dev/null 2>&1; then
        python_path="$system_python"
        printf 'Pinokio Python is still preparing; using existing Python 3.9+ for setup.\n'
        break
      fi
    fi
    if [[ "$dry_run" != "true" ]] && (( count > 0 && count % 15 == 0 )); then
      printf 'Still waiting for Pinokio tools; leave Pinokio open and finish any visible setup…\n'
    fi
    /bin/sleep 2
    (( count += 1 ))
  done
  if [[ -z "$pterm_path" || -z "$python_path" ]]; then
    if [[ "$dry_run" == "true" ]]; then
      printf 'Would initialize Pinokio, install Hub/Image/Voice and generation dependencies,\n'
      printf 'restore RAM-qualified models, configure ordered autolaunch, and optionally enroll.\n'
      return 0
    fi
    printf 'FAILED: Pinokio tools are not ready. Leave Pinokio open, finish any visible\n' >&2
    printf 'first-run setup, then run this same SSD installer again. Completed work is kept.\n' >&2
    return 1
  fi
  printf 'PINOKIO_HOME: %s\n' "$pinokio_home"
  "$python_path" "$SCRIPT_DIR/fleet_bootstrap.py" "$@"
}

stage_zero "$@" 2>&1 | /usr/bin/tee "$LOG_FILE"
finish ${pipestatus[1]}
