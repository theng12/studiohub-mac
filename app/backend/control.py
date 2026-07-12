"""Lifecycle control — start/stop studios through Pinokio's pterm CLI.

Verified mechanism (PTERM.md §start/§stop):
    pterm start start.js --ref pinokio://127.0.0.1:42000/api/<app>
    pterm stop  start.js --ref pinokio://127.0.0.1:42000/api/<app>

The pterm client streams the script's early output and exits on its own once
the script settles. It must NOT be killed mid-stream — cutting the client
during the handshake aborts the shell before the command runs (verified the
hard way). So we spawn it fully detached and never wait on it.

Only studios on THIS machine can be controlled: pterm talks to the local
Pinokio kernel. Remote studios will be controlled by their own machine's Hub
once federation lands.
"""

import shutil
import subprocess
from pathlib import Path

from .registry import LAUNCHER_ROOT

# PINOKIO_HOME/api/studiohub-mac -> PINOKIO_HOME
PINOKIO_HOME = LAUNCHER_ROOT.parents[1]
KERNEL = "pinokio://127.0.0.1:42000"


def find_pterm() -> str | None:
    """PATH first (Pinokio-managed shells have it), then the bundled location."""
    found = shutil.which("pterm")
    if found:
        return found
    bundled = PINOKIO_HOME / "bin" / "npm" / "bin" / "pterm"
    return str(bundled) if bundled.exists() else None


def pterm_command(pterm: str, action: str, script: str, ref: str) -> list[str]:
    """Build a command that also works under launchd's minimal PATH."""
    bundled_node = PINOKIO_HOME / "bin" / "miniforge" / "bin" / "node"
    prefix = [str(bundled_node), pterm] if bundled_node.exists() else [pterm]
    return prefix + [action, script, "--ref", ref]


def _is_controllable(studio: dict) -> str | None:
    """Return an error string, or None if the studio can be controlled."""
    if studio.get("machine", "local") != "local":
        return "remote studios must be controlled by their own machine's Hub"
    if not studio.get("app"):
        return "studio has no 'app' (Pinokio folder name) in the registry"
    app_dir = PINOKIO_HOME / "api" / studio["app"]
    if not app_dir.exists():
        return f"Pinokio app folder not found: api/{studio['app']}"
    return None


def control_studio(studio: dict, action: str) -> dict:
    """Fire pterm start/stop for a studio's start.js. Returns immediately —
    poll /api/hub/studios to watch the status change."""
    error = _is_controllable(studio)
    if error:
        return {"ok": False, "error": error}
    pterm = find_pterm()
    if pterm is None:
        return {"ok": False, "error": "pterm CLI not found (PATH or PINOKIO_HOME/bin/npm/bin)"}

    ref = f"{KERNEL}/api/{studio['app']}"
    cmd = pterm_command(pterm, action, "start.js", ref)
    try:
        # Detached: new session, output discarded, never waited on. The client
        # exits by itself; killing it early aborts the script launch.
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as e:
        return {"ok": False, "error": f"failed to spawn pterm: {e}"}
    return {"ok": True, "action": action, "studio": studio["id"], "ref": ref}


def run_hub_script(script: str) -> dict:
    """Run THIS Hub's own maintenance script (update.js) via its Pinokio app.
    Used for remote-triggered self-update: the primary Hub tells a peer to pull
    latest + restart itself (its startup service brings it back). Detached, like
    the studio scripts — the update kills this server, so we must not wait."""
    if script not in {"update.js"}:
        return {"ok": False, "error": "unsupported maintenance script"}
    app = LAUNCHER_ROOT.name  # the Hub's own Pinokio folder, e.g. "studiohub-mac"
    if not (LAUNCHER_ROOT / script).exists():
        return {"ok": False, "error": f"{script} not found for the Hub"}
    pterm = find_pterm()
    if pterm is None:
        return {"ok": False, "error": "pterm CLI not found"}
    ref = f"{KERNEL}/api/{app}"
    try:
        subprocess.Popen(
            pterm_command(pterm, "start", script, ref),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
    except OSError as e:
        return {"ok": False, "error": f"failed to spawn pterm: {e}"}
    return {"ok": True, "script": script, "app": app, "ref": ref}


def run_studio_script(studio: dict, script: str) -> dict:
    """Launch an allowed maintenance script through the Studio's Pinokio app."""
    if script not in {"update.js"}:
        return {"ok": False, "error": "unsupported maintenance script"}
    error = _is_controllable(studio)
    if error:
        return {"ok": False, "error": error}
    script_path = PINOKIO_HOME / "api" / studio["app"] / script
    if not script_path.exists():
        return {"ok": False, "error": f"{script} not found for {studio['id']}"}
    pterm = find_pterm()
    if pterm is None:
        return {"ok": False, "error": "pterm CLI not found"}
    ref = f"{KERNEL}/api/{studio['app']}"
    try:
        subprocess.Popen(
            pterm_command(pterm, "start", script, ref),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
    except OSError as e:
        return {"ok": False, "error": f"failed to spawn pterm: {e}"}
    return {"ok": True, "script": script, "studio": studio["id"], "ref": ref}
