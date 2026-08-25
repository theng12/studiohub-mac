"""Audit and install sibling Studio launchd startup services on this Mac.

Every command is local to the Hub process.  A location controller reaches a
remote machine by asking that machine's authenticated peer Hub to run the same
local operation; the controller never writes another Mac's filesystem.
"""

from __future__ import annotations

import ast
import contextlib
import datetime as dt
import fcntl
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

from . import control


SERVICE_SPECS = {
    "image": {
        "title": "Image Studio KH", "app": "imagestudio-mac",
        "port": 47868,
        "server_label": "com.kh.imagestudio.server",
        "watchdog_label": "com.kh.imagestudio.watchdog",
        "updater_label": "com.kh.imagestudio.updater",
    },
    "music": {
        "title": "Music Studio KH", "app": "musicstudio-mac",
        "port": 47869,
        "server_label": "com.kh.musicstudio.server",
        "watchdog_label": "com.kh.musicstudio.watchdog",
        "updater_label": "com.kh.musicstudio.updater",
    },
    "voice": {
        "title": "Voice Studio KH", "app": "voicestudio-mac.git",
        "port": 47870,
        "server_label": "com.kh.voicestudio.server",
        "watchdog_label": "com.kh.voicestudio.watchdog",
        "updater_label": "com.kh.voicestudio.updater",
    },
    "chat": {
        "title": "Chat Studio KH", "app": "chatstudio-mac.git",
        "port": 47871,
        "server_label": "com.kh.chatstudio.server",
        "watchdog_label": "com.kh.chatstudio.watchdog",
        "updater_label": "com.kh.chatstudio.updater",
    },
    "video": {
        "title": "Video Studio KH", "app": "videostudio-mac",
        "port": 47872,
        "server_label": "com.kh.videostudio.server",
        "watchdog_label": "com.kh.videostudio.watchdog",
        "updater_label": "com.kh.videostudio.updater",
    },
    "render": {
        "title": "Render Studio KH", "app": "renderstudio-mac",
        "port": 47874,
        "server_label": "com.kh.renderstudio.server",
        "watchdog_label": "com.kh.renderstudio.watchdog",
        "updater_label": "com.kh.renderstudio.updater",
    },
}

RETIRABLE_MODALITIES = frozenset({"music", "chat", "video", "render"})


def _app_dir(modality: str) -> Path | None:
    spec = SERVICE_SPECS.get(modality)
    if spec is None:
        return None
    return control.resolve_app_dir({"app": spec["app"], "machine": "local"})


def _app_dirs(modality: str) -> list[Path]:
    spec = SERVICE_SPECS.get(modality)
    if spec is None:
        return []
    app = spec["app"]
    names = [app, app[:-4] if app.endswith(".git") else f"{app}.git"]
    return [control.PINOKIO_HOME / "api" / name for name in dict.fromkeys(names)
            if (control.PINOKIO_HOME / "api" / name).is_dir()]


def _validated_app_dirs(modality: str) -> list[Path]:
    app_dirs = _app_dirs(modality)
    if not app_dirs:
        raise ValueError("Studio app is not installed on this Mac.")
    api_root = (control.PINOKIO_HOME / "api").resolve()
    for app_dir in app_dirs:
        if app_dir.is_symlink() or app_dir.resolve(strict=True).parent != api_root:
            raise ValueError("Refusing an unsafe Studio checkout path.")
    return app_dirs


def has_removal_intent(modality: str) -> bool:
    """Recognize an interrupted removal only from its marker and absent checkout."""
    if modality not in RETIRABLE_MODALITIES or _app_dirs(modality):
        return False
    from . import registry
    return registry.studio_removed("local", modality)


def is_fully_removed(modality: str) -> bool:
    """Return true only after durable completion and live residue verification."""
    if not has_removal_intent(modality):
        return False
    from . import registry
    if not registry.studio_removal_complete("local", modality):
        return False
    spec = SERVICE_SPECS[modality]
    labels = (spec["updater_label"], spec["server_label"], spec["watchdog_label"])
    return (
        all(not _launchd_loaded(label)
            and not (_launch_agents_dir() / f"{label}.plist").exists()
            and not (_launch_agents_dir() / f"{label}.plist").is_symlink()
            for label in labels)
        and not _port_open(spec["port"])
    )


def _launch_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _trash_dir() -> Path:
    return Path.home() / ".Trash"


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
    return _safe_service_script(app_dir, "install_service.sh")


def _safe_service_script(app_dir: Path, name: str) -> Path | None:
    installer = app_dir / name
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


@contextlib.contextmanager
def retirement_lock(modality: str):
    """Fence retirement against the sibling updater's own exclusive lock."""
    if modality not in RETIRABLE_MODALITIES:
        raise ValueError("Only Music, Chat, Video, and Render may be retired.")
    app_dirs = _validated_app_dirs(modality)

    @contextlib.contextmanager
    def one(app_dir: Path):
        state_dir = app_dir / "auto_update"
        state_dir.mkdir(parents=True, exist_ok=True)
        lock_path = state_dir / "update.lock"
        handle = open(lock_path, "a+", encoding="utf-8")
        os.chmod(lock_path, 0o600)
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError(
                    "A Studio update is already running; wait before retiring it."
                ) from exc
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    with contextlib.ExitStack() as stack:
        for app_dir in sorted(app_dirs):
            stack.enter_context(one(app_dir))
        yield


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
    from . import registry

    registered = list(registry.load_registry())
    services = []
    for modality in SERVICE_SPECS:
        service = inspect_service(modality)
        if modality in RETIRABLE_MODALITIES and not service["app_installed"]:
            continue
        studio = next((row for row in registered
                       if row.get("machine", "local") == "local"
                       and row.get("modality") == modality), None)
        enabled = (registry.studio_enabled("local", studio["id"])
                   if studio is not None else True)
        service.update(
            routing_enabled=enabled,
            retired=modality in RETIRABLE_MODALITIES and not enabled,
        )
        services.append(service)
    return {
        "schema_version": 1,
        "observed_at": time.time(),
        "machine": "local",
        "reachable": True,
        "supported": True,
        "services": services,
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


def uninstall_service(modality: str) -> dict:
    """Unload only one legacy Studio's launchd server and watchdog.

    The sibling-owned script removes its launchd marker and plists. It does
    not delete the launcher checkout, models, caches, outputs, or settings.
    """
    if modality not in RETIRABLE_MODALITIES:
        raise ValueError("Only Music, Chat, Video, and Render may be retired.")
    before = inspect_service(modality)
    app_dir = _app_dir(modality)
    uninstaller = (_safe_service_script(app_dir, "uninstall_service.sh")
                   if app_dir is not None else None)
    if app_dir is None or uninstaller is None:
        raise ValueError("A trusted startup uninstaller is unavailable.")
    if before["status"] == "not_installed":
        return {"ok": True, "changed": False, "service": before,
                "detail": "Automatic startup is already retired"}
    try:
        result = subprocess.run(
            ["/bin/bash", str(uninstaller)], cwd=app_dir,
            capture_output=True, text=True, timeout=240, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Startup retirement timed out; check the Studio service logs.") from exc
    except OSError as exc:
        raise ValueError(f"Startup uninstaller could not run: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "startup uninstaller failed").strip()
        raise ValueError(detail[-500:])
    after = inspect_service(modality)
    if after["status"] != "not_installed":
        raise ValueError("Uninstaller finished, but a launchd service or startup file remains.")
    return {"ok": True, "changed": True, "service": after,
            "detail": "Automatic startup retired and verified"}


def _disable_pinokio_autolaunch(app_dir: Path) -> None:
    environment = app_dir / "ENVIRONMENT"
    if environment.is_symlink():
        raise ValueError("Refusing a symlinked Studio ENVIRONMENT file.")
    values = {
        "PINOKIO_SCRIPT_AUTOLAUNCH": "start.js",
        "PINOKIO_SCRIPT_AUTOLAUNCH_ENABLED": "false",
        "PINOKIO_SCRIPT_REQUIRES": "",
    }
    lines = environment.read_text(encoding="utf-8").splitlines() if environment.exists() else []
    remaining = dict(values)
    output = []
    for line in lines:
        key = (line.split("=", 1)[0].strip()
               if "=" in line and not line.lstrip().startswith("#") else "")
        output.append(f"{key}={remaining.pop(key)}" if key in remaining else line)
    if remaining:
        if output and output[-1]:
            output.append("")
        output.extend(f"{key}={value}" for key, value in remaining.items())
    temporary = environment.with_name(f".{environment.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(output).rstrip() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, environment)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def _remove_launch_agent(label: str) -> None:
    domain = f"gui/{os.getuid()}"
    subprocess.run(
        ["/bin/launchctl", "bootout", f"{domain}/{label}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=30, check=False,
    )
    path = _launch_agents_dir() / f"{label}.plist"
    if path.exists() or path.is_symlink():
        if path.is_dir() and not path.is_symlink():
            raise ValueError(f"Refusing unexpected LaunchAgent directory: {path.name}")
        path.unlink()
    if _launchd_loaded(label) or path.exists() or path.is_symlink():
        raise ValueError(f"LaunchAgent {label} could not be removed.")


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            return True
    except OSError:
        return False


def _wait_port_closed(port: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while _port_open(port):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)
    return True


def _catalog_repos(app_dir: Path) -> set[str]:
    """Read literal Hugging Face repo ids without importing sibling code."""
    catalog = app_dir / "app" / "backend" / "catalog.py"
    try:
        tree = ast.parse(catalog.read_text(encoding="utf-8"), filename=str(catalog))
    except (OSError, SyntaxError, UnicodeError):
        return set()
    repos: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if (keyword.arg == "repo" and isinstance(keyword.value, ast.Constant)
                        and isinstance(keyword.value.value, str)):
                    repos.add(keyword.value.value)
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if (isinstance(key, ast.Constant) and key.value == "repo"
                        and isinstance(value, ast.Constant)
                        and isinstance(value.value, str)):
                    repos.add(value.value)
    return {repo for repo in repos if repo.count("/") == 1}


def _hf_hub_dir(app_dir: Path) -> Path:
    environment = app_dir / "ENVIRONMENT"
    values: dict[str, str] = {}
    try:
        for line in environment.read_text(encoding="utf-8").splitlines():
            if "=" not in line or line.lstrip().startswith("#"):
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("\"'")
    except (OSError, UnicodeError):
        pass
    raw_hf_home = values.get("HF_HOME")
    raw_hub_cache = values.get("HUGGINGFACE_HUB_CACHE")
    if raw_hub_cache:
        candidate = Path(raw_hub_cache).expanduser()
        if not candidate.is_absolute():
            candidate = app_dir / candidate
        return candidate
    if raw_hf_home:
        candidate = Path(raw_hf_home).expanduser()
        if not candidate.is_absolute():
            candidate = app_dir / candidate
        return candidate / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def _exclusive_cached_models(modality: str, app_dirs: list[Path]) -> list[Path]:
    owned = set().union(*(_catalog_repos(app_dir) for app_dir in app_dirs))
    used_elsewhere: set[str] = set()
    for other in SERVICE_SPECS:
        if other == modality:
            continue
        for app_dir in _app_dirs(other):
            used_elsewhere.update(_catalog_repos(app_dir))
    exclusive = owned - used_elsewhere
    targets: set[Path] = set()
    for app_dir in app_dirs:
        hub = _hf_hub_dir(app_dir)
        for repo in exclusive:
            package = hub / f"models--{repo.replace('/', '--')}"
            if package.exists() or package.is_symlink():
                targets.add(package)
    return sorted(targets)


def finalize_absent_studio_removal(modality: str) -> dict:
    """Finish an interrupted removal whose checkout has already disappeared."""
    if modality not in RETIRABLE_MODALITIES:
        raise ValueError("Only Music, Chat, Video, and Render may be fully removed.")
    if _app_dirs(modality):
        raise ValueError("Studio checkout is still present; run the normal removal path.")
    spec = SERVICE_SPECS[modality]
    labels = (spec["updater_label"], spec["server_label"], spec["watchdog_label"])
    changed = any(
        _launchd_loaded(label)
        or (_launch_agents_dir() / f"{label}.plist").exists()
        or (_launch_agents_dir() / f"{label}.plist").is_symlink()
        for label in labels
    )
    for label in labels:
        _remove_launch_agent(label)
    if not _wait_port_closed(spec["port"]):
        raise ValueError(
            f"{spec['title']} is still running outside managed services; "
            "stop it in Pinokio, then retry."
        )
    return {
        "ok": True, "changed": changed, "removed": True,
        "already_removed": True, "modality": modality,
        "detail": "Interrupted removal cleanup completed and verified",
    }


def reconcile_removal_intents() -> list[dict]:
    """Resume only owner-confirmed removals whose checkout is already absent."""
    from . import registry

    results = []
    for modality in sorted(RETIRABLE_MODALITIES):
        if not has_removal_intent(modality) or is_fully_removed(modality):
            continue
        try:
            result = finalize_absent_studio_removal(modality)
            registry.set_studio_removal_complete("local", modality, True)
            results.append({"modality": modality, "ok": True, **result})
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            results.append({"modality": modality, "ok": False, "error": str(exc)})
    return results


def _trash_destination(app_dir: Path, *, display_name: str | None = None) -> Path:
    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    base = _trash_dir() / f"{display_name or app_dir.name}-removed-{timestamp}"
    candidate = base
    counter = 2
    while candidate.exists():
        candidate = base.with_name(f"{base.name}-{counter}")
        counter += 1
    return candidate


def fully_remove_studio(modality: str) -> dict:
    """Remove one unused Studio checkout after the Hub has fenced active work."""
    if modality not in RETIRABLE_MODALITIES:
        raise ValueError("Only Music, Chat, Video, and Render may be fully removed.")
    app_dirs = _validated_app_dirs(modality)
    cached_models = _exclusive_cached_models(modality, app_dirs)
    spec = SERVICE_SPECS[modality]
    for app_dir in app_dirs:
        # Pinokio may be closed on a headless fleet Mac. Its stop request is
        # best-effort; the authoritative service unload and listener check
        # below decide whether removal is safe.
        control.stop_studio_sync({
            "id": modality, "machine": "local", "app": app_dir.name,
        })
        _disable_pinokio_autolaunch(app_dir)
    for label in (spec["updater_label"], spec["server_label"], spec["watchdog_label"]):
        _remove_launch_agent(label)
    if not _wait_port_closed(spec["port"]):
        raise ValueError(
            f"{spec['title']} is still running outside managed services; "
            "stop it in Pinokio, then retry."
        )
    for app_dir in app_dirs:
        with contextlib.suppress(FileNotFoundError):
            (app_dir / "service" / ".installed").unlink()
    trash = _trash_dir()
    trash.mkdir(parents=True, exist_ok=True)
    os.chmod(trash, 0o700)
    trashed = []
    trashed_models = []
    for package in cached_models:
        destination = _trash_destination(
            package,
            display_name=f"{modality}studio-cache-{package.name}",
        )
        shutil.move(str(package), str(destination))
        if (package.exists() or package.is_symlink()
                or not (destination.exists() or destination.is_symlink())):
            raise ValueError(f"Model cache {package.name} was not moved to Trash.")
        trashed.append(str(destination))
        trashed_models.append(str(destination))
    trashed_checkouts = []
    for app_dir in app_dirs:
        destination = _trash_destination(app_dir)
        shutil.move(str(app_dir), str(destination))
        if app_dir.exists() or not destination.is_dir():
            raise ValueError(f"Studio checkout {app_dir.name} was not moved to Trash.")
        trashed.append(str(destination))
        trashed_checkouts.append(str(destination))
    return {
        "ok": True, "changed": True, "removed": True,
        "modality": modality, "trashed": trashed,
        "trashed_checkouts": trashed_checkouts,
        "trashed_model_caches": trashed_models,
        "detail": "Updater, startup services, routing, and checkout removed; data moved to Trash",
    }
