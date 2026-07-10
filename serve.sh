#!/bin/bash
#
# Studio Hub KH — headless server entrypoint used by the macOS launchd startup
# service (installed via the "Install as Startup Service" menu button, see
# install_service.sh). Mirrors the sibling studios' serve.sh.
#
# Self-locating: every path is derived from THIS file's own folder, so the same
# file works on any Mac / username with no edits. launchd runs it with
# KeepAlive, so if the Hub ever exits it is relaunched automatically.
#
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"   # launcher root (this file's folder)
export PYTHONUNBUFFERED=1

# The Hub runs no local models, so there is no HF_HOME to pin (unlike the
# studios). The fleet token, registry and DB all live as files in this folder
# and are found relative to the app automatically.
cd "$HERE/app"
exec "$HERE/conda_env/bin/python" -m uvicorn backend.main:app \
  --host 0.0.0.0 --port 47873
