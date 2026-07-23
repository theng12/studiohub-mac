#!/bin/bash
#
# Force-restart the Studio Hub KH always-on service (launchd kickstart -k).
# Useful if it's wedged or after you change something. KeepAlive normally
# handles crashes on its own; this is the manual "kick".
#
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
UID_NUM="$(id -u)"
SRV="com.kh.studiohub.server"
PORT=47873
DELAY_SECONDS="${1:-0}"

if ! [[ "$DELAY_SECONDS" =~ ^([0-9]+|[0-9]+\.[0-9]+)$ ]]; then
  echo "❌ Invalid restart delay."
  exit 2
fi
if [[ "$DELAY_SECONDS" != "0" ]]; then
  sleep "$DELAY_SECONDS"
fi

if launchctl kickstart -k "gui/$UID_NUM/$SRV" 2>/dev/null; then
  echo "🔄 Restart signal sent to $SRV."
  for _ in $(seq 1 45); do
    if curl -fsS --max-time 3 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
      echo "✅ Studio Hub is healthy again on port $PORT."
      exit 0
    fi
    sleep 1
  done
  echo "❌ Restart was sent, but Studio Hub did not become healthy within 45 seconds."
  echo "   Check $ROOT/logs/service/server.err.log."
  exit 1
else
  echo "⚠️  Couldn't kick the service — is it installed?"
  echo "   Use 'Install as Startup Service' first."
fi
