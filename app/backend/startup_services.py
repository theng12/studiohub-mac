"""Audit and install sibling Studio launchd startup services on this Mac.

Every command is local to the Hub process.  A location controller reaches a
remote machine by asking that machine's authenticated peer Hub to run the same
local operation; the controller never writes another Mac's filesystem.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from . import control


SERVICE_SPECS = {
    "image": {
        "title": "Image Studio KH", "app": "imagestudio-mac",
        "server_label": "com.kh.imagestudio.server",
        "watchdog_label": "com.kh.imagestudio.watchdog",
    },
    "music": {
        "title": "Music Studio KH", "app": "musicstudio-mac",
        "server_label": "com.kh.musicstudio.server",
        "watchdog_label": "com.kh.musicstudio.watchdog",
    },
    "voice": {
        "title": "Voice Studio KH", "app": "voicestudio-mac.git",
        "server_label": "com.kh.voicestudio.server",
        "watchdog_label": "com.kh.voicestudio.watchdog",
    },
    "chat": {
        "title": "Chat Studio KH", "app": "chatstudio-mac.git",
        "server_label": "com.kh.chatstudio.server",
        "watchdog_label": "com.kh.chatstudio.watchdog",
    },
    "video": {
        "title": "Video Studio KH", "app": "videostudio-mac",
        "server_label": "com.kh.videostudio.server",
        "watchdog_label": "com.kh.videostudio.watchdog",
    },
    "render": {
        "title": "Render Studio KH", "app": "renderstudio-mac",
        "server_label": "com.kh.renderstudio.server",
        "watchdog_label": "com.kh.renderstudio.watchdog",
    },
}


def _app_dir(modality: str) -> Path | None:
    spec = SERVICE_SPECS.get(modality)
    if spec is None:
        return None
    return control.resolve_app_dir({"app": spec["app"], "machine": "local"})


def _launch_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _launchd_loaded(label: str) -> bool:
    try:
        result = subprocess.run(
            ["/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _safe_installer(app_dir: Path) -> Path | None:
    installer = app_dir / "install_service.sh"
    try:
        resolved_root = app_dir.resolve(strict=True)
        resolved = installer.resolve(strict=True)
    except OSError:
        return None
    if installer.is_symlink() or not installer.is_file():
        return None
    if resolved.parent != resolved_root:
        return None
    return resolved


def inspect_service(modality: str) -> dict:
    spec = SERVICE_SPECS.get(modality)
    if spec is None:
        raise ValueError(f"unknown Studio type: {modality}")
    app_dir = _app_dir(modality)
    base = {
        "modality": modality,
        "title": spec["title"],
        "app": spec["app"],
        "app_installed": app_dir is not None,
        "supported": False,
        "installed": False,
        "server_loaded": False,
        "watchdog_loaded": False,
        "can_install": False,
    }
    if app_dir is None:
        return {**base, "status": "app_missing",
                "detail": "Studio app is not installed on this Mac"}
    installer = _safe_installer(app_dir)
    if installer is None:
        return {**base, "status": "unsupported",
                "detail": "This Studio version has no trusted startup installer"}
    if not (app_dir / "conda_env" / "bin" / "python").is_file():
        return {**base, "app": app_dir.name, "supported": True,
                "status": "runtime_missing",
                "detail": "Run this Studio's Install first, then enable automatic startup"}
    launch_agents = _launch_agents_dir()
    marker = app_dir / "service" / ".installed"
    server_plist = launch_agents / f"{spec['server_label']}.plist"
    watchdog_plist = launch_agents / f"{spec['watchdog_label']}.plist"
    server_loaded = _launchd_loaded(spec["server_label"])
    watchdog_loaded = _launchd_loaded(spec["watchdog_label"])
    files_ready = marker.is_file() and server_plist.is_file() and watchdog_plist.is_file()
    installed = files_ready and server_loaded and watchdog_loaded
    if installed:
        status, detail = "installed", "Starts automatically and watchdog is loaded"
    elif marker.exists() or server_plist.exists() or watchdog_plist.exists() \
            or server_loaded or watchdog_loaded:
        status, detail = "repair_needed", "Startup service is incomplete; reinstall to repair it"
    else:
        status, detail = "not_installed", "Automatic startup is not installed"
    return {
        **base,
        "app": app_dir.name,
        "supported": True,
        "installed": installed,
        "server_loaded": server_loaded,
        "watchdog_loaded": watchdog_loaded,
        "can_install": not installed,
        "status": status,
        "detail": detail,
    }


def local_snapshot() -> dict:
    return {
        "schema_version": 1,
        "observed_at": time.time(),
        "machine": "local",
        "reachable": True,
        "supported": True,
        "services": [inspect_service(modality) for modality in SERVICE_SPECS],
    }


def install_service(modality: str) -> dict:
    before = inspect_service(modality)
    if before["installed"]:
        return {"ok": True, "changed": False, "service": before,
                "detail": "Startup service is already installed"}
    if not before["can_install"]:
        raise ValueError(before["detail"])
    app_dir = _app_dir(modality)
    installer = _safe_installer(app_dir) if app_dir is not None else None
    if app_dir is None or installer is None:
        raise ValueError("Trusted startup installer is unavailable.")
    try:
        result = subprocess.run(
            ["/bin/bash", str(installer)], cwd=app_dir,
            capture_output=True, text=True, timeout=240, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Startup installation timed out; check the Studio service logs.") from exc
    except OSError as exc:
        raise ValueError(f"Startup installer could not run: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "startup installer failed").strip()
        raise ValueError(detail[-500:])
    after = inspect_service(modality)
    if not after["installed"]:
        raise ValueError("Installer finished, but launchd did not load both services.")
    return {"ok": True, "changed": True, "service": after,
            "detail": "Automatic startup installed and verified"}
