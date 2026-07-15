#!/bin/bash
#
# Install Studio Hub KH as an always-on macOS service (launchd LaunchAgent).
# Mirrors the sibling studios' install_service.sh.
#
# Idempotent — safe to run repeatedly; it re-bootstraps cleanly. No sudo needed
# (LaunchAgents are per-user). The one-time system settings for full power-cut
# recovery are admin-level and explained at the end; they are NOT done here.
#
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
UID_NUM="$(id -u)"
LA="$HOME/Library/LaunchAgents"
SRV="com.kh.studiohub.server"
WD="com.kh.studiohub.watchdog"
PORT=47873
APPNAME="Studio Hub KH"

mkdir -p "$LA" "$ROOT/logs/service" "$ROOT/service"
chmod +x "$ROOT/studiohub-serve.sh" "$ROOT/studiohub-watchdog.sh"

_hub_owned_pid() {
  local pid="$1" command cwd
  command="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1)"
  [[ "$command" == *"$ROOT"* || "$cwd" == "$ROOT" || "$cwd" == "$ROOT/"* ]]
}

# Never disrupt an unrelated service that happens to use the configured port.
# Check before unloading launchd so a failed repair leaves the current Hub intact.
PORT_PIDS="$(lsof -ti tcp:$PORT -sTCP:LISTEN 2>/dev/null || true)"
for p in $PORT_PIDS; do
  if ! _hub_owned_pid "$p"; then
    echo "❌ Port $PORT is owned by an unrelated process (pid $p)."
    echo "   Studio Hub repair stopped without terminating it. Free the port or change that service first."
    exit 1
  fi
done

# ── server agent: boot-start + auto-restart on crash ──
cat > "$LA/$SRV.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$SRV</string>
  <key>ProgramArguments</key>
  <array><string>$ROOT/studiohub-serve.sh</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Interactive</string>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$ROOT/logs/service/server.log</string>
  <key>StandardErrorPath</key><string>$ROOT/logs/service/server.err.log</string>
</dict>
</plist>
PLIST

# ── watchdog agent: every 60s, restart the Hub if /api/health is dead ──
cat > "$LA/$WD.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$WD</string>
  <key>ProgramArguments</key>
  <array><string>$ROOT/studiohub-watchdog.sh</string></array>
  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>60</integer>
  <key>StandardOutPath</key><string>$ROOT/logs/service/watchdog.log</string>
  <key>StandardErrorPath</key><string>$ROOT/logs/service/watchdog.err.log</string>
</dict>
</plist>
PLIST

# (re)load both agents — bootout first so re-running picks up any changes
launchctl bootout  "gui/$UID_NUM/$SRV" 2>/dev/null || true
launchctl bootout  "gui/$UID_NUM/$WD"  2>/dev/null || true

# bootout is asynchronous — wait for each label to fully unload before we
# bootstrap again, or launchd returns "Bootstrap failed: 5: Input/output error".
_wait_gone() { for _ in $(seq 1 25); do launchctl print "gui/$UID_NUM/$1" >/dev/null 2>&1 || return 0; sleep 0.2; done; }
_wait_gone "$SRV"; _wait_gone "$WD"

# Take over the port: if you started the Hub via Pinokio's "Start", that
# instance is still holding port $PORT. The whole point of converting to a
# service is for the service to own it, so stop the old listener now (graceful
# TERM, then KILL any straggler).
PORT_PIDS="$(lsof -ti tcp:$PORT -sTCP:LISTEN 2>/dev/null || true)"
if [ -n "$PORT_PIDS" ]; then
  echo "Taking over port $PORT — stopping the current instance (Pinokio 'Start'):"
  for p in $PORT_PIDS; do echo "   • stopping pid $p"; kill "$p" 2>/dev/null || true; done
  sleep 2
  STRAGGLERS="$(lsof -ti tcp:$PORT -sTCP:LISTEN 2>/dev/null || true)"
  if [ -n "$STRAGGLERS" ]; then
    for p in $STRAGGLERS; do
      if _hub_owned_pid "$p"; then kill -9 "$p" 2>/dev/null || true; fi
    done
    sleep 1
  fi
  echo ""
fi

# retry once — bootstrap can still transiently fail right after a bootout.
_bootstrap() { launchctl bootstrap "gui/$UID_NUM" "$1" 2>/dev/null || { sleep 1; launchctl bootstrap "gui/$UID_NUM" "$1"; }; }
_bootstrap "$LA/$SRV.plist"
_bootstrap "$LA/$WD.plist"
launchctl kickstart "gui/$UID_NUM/$SRV" 2>/dev/null || true

# A successful bootstrap is not the same as a healthy update. Wait until the
# new process owns the port and reports the VERSION currently on disk.
EXPECTED_VERSION="$(tr -d '[:space:]' < "$ROOT/VERSION")"
LOADED_VERSION=""
for _ in $(seq 1 60); do
  HEALTH="$(curl -fsS --max-time 3 "http://127.0.0.1:$PORT/api/health" 2>/dev/null || true)"
  LIVE_PID="$(lsof -ti tcp:$PORT -sTCP:LISTEN 2>/dev/null | head -n 1 || true)"
  if [ -n "$HEALTH" ] && [ -n "$LIVE_PID" ] && _hub_owned_pid "$LIVE_PID"; then
    LOADED_VERSION="$(printf '%s' "$HEALTH" | "$ROOT/conda_env/bin/python" -c 'import json,sys; print(json.load(sys.stdin).get("app_version", ""))' 2>/dev/null || true)"
    if [ "$LOADED_VERSION" = "$EXPECTED_VERSION" ]; then break; fi
  fi
  sleep 1
done
if [ "$LOADED_VERSION" != "$EXPECTED_VERSION" ]; then
  echo "❌ Studio Hub did not become healthy on v$EXPECTED_VERSION within 60 seconds."
  echo "   Loaded version: ${LOADED_VERSION:-not responding}. Check logs/service/server.err.log."
  exit 1
fi
touch "$ROOT/service/.installed"

echo ""
echo "✅ $APPNAME v$LOADED_VERSION is healthy as an always-on service on port $PORT."
echo "   • Starts automatically at login, restarts itself if it crashes, and a"
echo "     watchdog re-launches it if it ever stops responding to /api/health."
echo "   • Logs: $ROOT/logs/service/"
echo "   • Reach it over Tailscale/LAN at  http://<this-mac>:$PORT"
echo ""
echo "──────────────────────────────────────────────────────────────────────────"
echo "ONE-TIME Mac settings for full hands-off recovery after a POWER CUT"
echo "(admin-level — do these once per machine; NOT done by this button):"
echo ""
echo "  1. Power back on automatically when electricity returns:"
echo "         sudo pmset -a autorestart 1"
echo ""
echo "  2. Enable Automatic login"
echo "         System Settings ▸ Users & Groups ▸ Automatically log in as …"
echo "     WHY: a LaunchAgent runs inside your logged-in session. Without"
echo "     auto-login the Mac boots to the login screen and the Hub never starts."
echo ""
echo "  3. Turn FileVault OFF"
echo "         System Settings ▸ Privacy & Security ▸ FileVault"
echo "     WHY: with FileVault on, a reboot stops at the encrypted-disk password"
echo "     screen and never reaches auto-login — so the Hub never comes back."
echo ""
echo "  Use the service OR Pinokio's Start button — not both (they share port $PORT)."
echo "──────────────────────────────────────────────────────────────────────────"
