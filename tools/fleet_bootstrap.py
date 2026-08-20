#!/usr/bin/env python3
"""Provision a clean Apple-silicon Mac from the TerraNash fleet SSD.

The script deliberately copies only portable assets from the SSD: the signed
Pinokio installer and Hugging Face model caches. Python/Conda environments are
rebuilt by each Studio's checked-in Pinokio installer on the target Mac.
"""

from __future__ import annotations

import argparse
import configparser
import getpass
import hashlib
import json
import os
import platform
import plistlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


PINOKIO_VERSION = "8.0.40"
PINOKIO_DMG = f"Pinokio-{PINOKIO_VERSION}-arm64.dmg"
PINOKIO_DMG_SHA256 = "3c0f55f769efc2c02e5d0b8bc24e2ee7b0be54d42e6404663887e0cf8d3df3fd"
CONTROL_PLANE = "http://127.0.0.1:42000"
LOGIN_AGENT_LABEL = "com.terranash.pinokio"

APPS = (
    {
        "name": "imagestudio-mac",
        "title": "Image Studio",
        "url": "https://github.com/theng12/imagestudio-mac.git",
        "port": 47868,
        "generation_marker": "conda_env/lib/python3.12/site-packages/mflux",
    },
    {
        "name": "voicestudio-mac",
        "title": "Voice Studio",
        "url": "https://github.com/theng12/voicestudio-mac.git",
        "port": 47870,
        "generation_marker": "conda_env/lib/python3.12/site-packages/mlx_audio",
    },
    {
        "name": "studiohub-mac",
        "title": "Studio Hub",
        "url": "https://github.com/theng12/studiohub-mac.git",
        "port": 47873,
        "generation_marker": None,
    },
)

AUTOLAUNCH = {
    "imagestudio-mac": {
        "PINOKIO_SCRIPT_AUTOLAUNCH": "start.js",
        "PINOKIO_SCRIPT_AUTOLAUNCH_ENABLED": "true",
        "PINOKIO_SCRIPT_REQUIRES": "",
    },
    "voicestudio-mac": {
        "PINOKIO_SCRIPT_AUTOLAUNCH": "start.js",
        "PINOKIO_SCRIPT_AUTOLAUNCH_ENABLED": "true",
        "PINOKIO_SCRIPT_REQUIRES": "",
    },
    "studiohub-mac": {
        "PINOKIO_SCRIPT_AUTOLAUNCH": "start.js",
        "PINOKIO_SCRIPT_AUTOLAUNCH_ENABLED": "true",
        "PINOKIO_SCRIPT_REQUIRES": "",
    },
}


class BootstrapError(RuntimeError):
    pass


def heading(value: str) -> None:
    print(f"\n== {value} ==", flush=True)


def run(command: list[str], *, dry_run: bool = False,
        check: bool = True) -> subprocess.CompletedProcess[str]:
    printable = " ".join(command)
    print(f"  $ {printable}", flush=True)
    if dry_run:
        return subprocess.CompletedProcess(command, 0, "", "")
    return subprocess.run(command, check=check, text=True)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pinokio_app() -> Path | None:
    candidates = (
        Path("/Applications/Pinokio.app"),
        Path.home() / "Applications/Pinokio.app",
    )
    return next((path for path in candidates if path.is_dir()), None)


def version_tuple(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in value.split("."))
    except ValueError:
        return ()


def installed_pinokio_version(app: Path) -> str:
    try:
        with (app / "Contents/Info.plist").open("rb") as handle:
            return str(plistlib.load(handle).get("CFBundleShortVersionString") or "")
    except (OSError, plistlib.InvalidFileException):
        return ""


def install_pinokio(kit_root: Path, *, dry_run: bool) -> Path:
    installed = pinokio_app()
    if installed:
        current = installed_pinokio_version(installed)
        if version_tuple(current) >= version_tuple(PINOKIO_VERSION):
            print(f"  Pinokio {current} already installed: {installed}")
            return installed
        print(f"  Pinokio {current or 'unknown'} is older than required {PINOKIO_VERSION}; upgrading.")

    dmg = kit_root / "installers" / PINOKIO_DMG
    if not dmg.is_file():
        raise BootstrapError(
            f"Missing {dmg}. Re-stage the SSD from Studio Hub before using it."
        )
    if not dry_run and sha256(dmg) != PINOKIO_DMG_SHA256:
        raise BootstrapError(f"{dmg.name} failed its SHA-256 check; re-stage the SSD.")
    if dry_run:
        print(f"  Would verify and install {dmg} into /Applications")
        return Path("/Applications/Pinokio.app")

    mount = Path(tempfile.mkdtemp(prefix="terranash-pinokio-"))
    attached = False
    try:
        run(["/usr/bin/hdiutil", "attach", "-readonly", "-nobrowse",
             "-mountpoint", str(mount), str(dmg)])
        attached = True
        source = mount / "Pinokio.app"
        if not source.is_dir():
            raise BootstrapError("The verified Pinokio installer contains no Pinokio.app.")
        run(["/usr/bin/sudo", "/usr/bin/ditto", str(source),
             "/Applications/Pinokio.app"])
    finally:
        if attached:
            run(["/usr/bin/hdiutil", "detach", str(mount)], check=False)
        shutil.rmtree(mount, ignore_errors=True)

    installed = Path("/Applications/Pinokio.app")
    run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(installed)])
    run(["/usr/sbin/spctl", "--assess", "--type", "execute", str(installed)])
    return installed


def install_login_agent(app: Path, *, dry_run: bool) -> None:
    agent = Path.home() / "Library/LaunchAgents" / f"{LOGIN_AGENT_LABEL}.plist"
    payload = {
        "Label": LOGIN_AGENT_LABEL,
        "ProgramArguments": ["/usr/bin/open", "-gj", str(app)],
        "RunAtLoad": True,
        "ProcessType": "Interactive",
    }
    print(f"  Pinokio login item: {agent}")
    if dry_run:
        return
    agent.parent.mkdir(parents=True, exist_ok=True)
    temporary = agent.with_name(f".{agent.name}.{os.getpid()}.tmp")
    with temporary.open("wb") as handle:
        plistlib.dump(payload, handle)
    temporary.chmod(0o600)
    temporary.replace(agent)
    domain = f"gui/{os.getuid()}"
    run(["/bin/launchctl", "bootout", domain, str(agent)], check=False)
    run(["/bin/launchctl", "bootstrap", domain, str(agent)])


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def resolve_pinokio_home() -> Path | None:
    configured = read_json(Path.home() / ".pinokio/config.json").get("home")
    if configured:
        return Path(str(configured)).expanduser().resolve()
    try:
        with urllib.request.urlopen(f"{CONTROL_PLANE}/pinokio/home", timeout=2) as reply:
            value = json.loads(reply.read()).get("path")
            return Path(value).expanduser().resolve() if value else None
    except (OSError, ValueError, urllib.error.URLError):
        return None


def resolve_pterm(home: Path) -> Path | None:
    candidates = (home / "bin/npm/bin/pterm", home / "bin/pterm")
    found = next((path for path in candidates if path.is_file() and os.access(path, os.X_OK)), None)
    if found:
        return found
    executable = shutil.which("pterm")
    return Path(executable).resolve() if executable else None


def wait_for_pinokio(app: Path, timeout: int, *, dry_run: bool) -> tuple[Path, Path]:
    run(["/usr/bin/open", str(app)], dry_run=dry_run)
    if dry_run:
        home = Path.home() / "pinokio"
        return home, home / "bin/npm/bin/pterm"
    print("  Waiting for Pinokio's first-run setup and control plane…")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        home = resolve_pinokio_home()
        pterm = resolve_pterm(home) if home else None
        if home and pterm:
            try:
                with urllib.request.urlopen(f"{CONTROL_PLANE}/pinokio/home", timeout=2):
                    return home, pterm
            except (OSError, urllib.error.URLError):
                pass
        time.sleep(2)
    raise BootstrapError(
        "Pinokio did not finish first-run setup. Complete the visible Pinokio window, "
        "then run this installer again; completed steps will be skipped."
    )


def normalize_git_url(value: str) -> str:
    return value.strip().removesuffix("/").removesuffix(".git").lower()


def git_origin(target: Path) -> str:
    config = configparser.ConfigParser()
    try:
        config.read(target / ".git/config")
        return config.get('remote "origin"', "url")
    except (configparser.Error, KeyError):
        return ""


def git_checkout_state(target: Path) -> tuple[str, str, str]:
    def output(*arguments: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(target), *arguments],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    return (
        output("symbolic-ref", "--quiet", "--short", "HEAD"),
        output("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"),
        output("status", "--porcelain"),
    )


def app_ref(name: str) -> str:
    return f"pinokio://127.0.0.1:42000/api/{name}"


def ensure_repo(pterm: Path, home: Path, app: dict, *, dry_run: bool) -> Path:
    canonical = home / "api" / app["name"]
    legacy = home / "api" / f'{app["name"]}.git'
    if canonical.exists() and legacy.exists():
        print(
            f"  {app['title']} has both {canonical.name} and {legacy.name}; "
            "using the canonical checkout and leaving the older folder untouched."
        )
    target = canonical if canonical.exists() else legacy if legacy.exists() else canonical
    if target.exists():
        if not (target / ".git").is_dir():
            raise BootstrapError(f"{target} exists but is not a Git checkout.")
        origin = git_origin(target)
        if not dry_run and normalize_git_url(origin) != normalize_git_url(app["url"]):
            raise BootstrapError(f"{target} belongs to a different repository; refusing to overwrite it.")
        if not dry_run:
            try:
                branch, upstream, changes = git_checkout_state(target)
            except subprocess.CalledProcessError as exc:
                raise BootstrapError(
                    f"Could not verify the Git state for {target}; refusing to update it."
                ) from exc
            if branch != "main":
                raise BootstrapError(f"{target} must be on main before it can be updated.")
            if upstream != "origin/main":
                raise BootstrapError(f"{target} must track origin/main before it can be updated.")
            if changes:
                raise BootstrapError(f"{target} has local changes; refusing to update it.")
        print(f"  {app['title']} checkout already exists; updating it.")
        run(["git", "-C", str(target), "pull", "--ff-only"], dry_run=dry_run)
        return target
    run([str(pterm), "download", app["url"], app["name"]], dry_run=dry_run)
    if not dry_run and not (target / ".git").is_dir():
        raise BootstrapError(f"Pinokio did not create {target}.")
    return target


def install_script(pterm: Path, name: str, script: str, *, dry_run: bool) -> None:
    run([str(pterm), "start", script, "--ref", app_ref(name)], dry_run=dry_run)


def python_imports(target: Path, imports: str) -> bool:
    python = target / "conda_env/bin/python"
    if not python.is_file():
        return False
    result = subprocess.run([str(python), "-c", imports], capture_output=True, text=True)
    return result.returncode == 0


def ensure_dependencies(pterm: Path, target: Path, app: dict, *, dry_run: bool) -> None:
    installed_name = target.name
    service_marker = target / "service/.installed"
    if service_marker.exists():
        if not (target / "unservice.js").is_file() and not dry_run:
            raise BootstrapError(
                f"{app['title']} has a startup service but no unservice.js repair action."
            )
        print(f"  Converting {app['title']} from its old startup service to Pinokio startup…")
        install_script(pterm, installed_name, "unservice.js", dry_run=dry_run)
        if not dry_run and service_marker.exists():
            raise BootstrapError(f"{app['title']} startup-service removal did not complete.")
    base_import = (
        "import fastapi, httpx, psutil, uvicorn"
        if app["name"] == "studiohub-mac"
        else "import fastapi, huggingface_hub, uvicorn"
    )
    base_ok = python_imports(target, base_import)
    if not base_ok:
        print(f"  Installing {app['title']} base environment…")
        install_script(pterm, installed_name, "install.js", dry_run=dry_run)
        if not dry_run and not python_imports(target, base_import):
            raise BootstrapError(f"{app['title']} base dependency verification failed.")
    else:
        print(f"  {app['title']} base environment is verified.")
    marker = app["generation_marker"]
    generation_import = "import mflux" if app["name"] == "imagestudio-mac" else "import mlx_audio, torch, transformers"
    generation_ok = not marker or python_imports(target, generation_import)
    if marker and not generation_ok:
        print(f"  Installing {app['title']} generation environment…")
        install_script(pterm, installed_name, "install_generation.js", dry_run=dry_run)
        if not dry_run and not python_imports(target, generation_import):
            raise BootstrapError(f"{app['title']} generation dependency verification failed.")
    elif marker:
        print(f"  {app['title']} generation environment is verified.")


def request_json(url: str, *, method: str = "GET", body: dict | None = None,
                 timeout: int = 10) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            value = json.loads(response.read())
            return value if isinstance(value, dict) else {}
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read()).get("detail")
        except (ValueError, AttributeError):
            detail = None
        raise BootstrapError(str(detail or f"{url} returned HTTP {exc.code}")) from exc
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise BootstrapError(f"Could not reach {url}: {exc}") from exc


def wait_health(port: int, *, timeout: int = 180, dry_run: bool = False) -> None:
    if dry_run:
        print(f"  Would wait for http://127.0.0.1:{port}/api/health")
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2):
                return
        except (OSError, urllib.error.URLError):
            time.sleep(2)
    raise BootstrapError(f"Nothing became healthy on port {port} within {timeout} seconds.")


def wait_stopped(port: int, *, timeout: int = 30, dry_run: bool = False) -> None:
    if dry_run:
        print(f"  Would wait for port {port} to stop answering")
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1):
                time.sleep(1)
        except (OSError, urllib.error.URLError):
            return
    raise BootstrapError(f"{port} did not stop within {timeout} seconds.")


def ensure_started(pterm: Path, app: dict, *, dry_run: bool) -> None:
    try:
        if not dry_run:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{app['port']}/api/health", timeout=2):
                print(f"  {app['title']} is already healthy.")
                return
    except (OSError, urllib.error.URLError):
        pass
    install_script(pterm, app["name"], "start.js", dry_run=dry_run)
    wait_health(app["port"], dry_run=dry_run)


def update_environment(path: Path, values: dict[str, str]) -> None:
    lines = path.read_text().splitlines() if path.exists() else []
    remaining = dict(values)
    output: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else ""
        if key in remaining:
            output.append(f"{key}={remaining.pop(key)}")
        else:
            output.append(line)
    if remaining:
        if output and output[-1]:
            output.append("")
        output.append("# TerraNash fleet bootstrap: one Pinokio-owned startup graph.")
        output.extend(f"{key}={value}" for key, value in remaining.items())
    path.write_text("\n".join(output).rstrip() + "\n")


def configure_autolaunch(targets: dict[str, Path], *, dry_run: bool) -> None:
    for name, values in AUTOLAUNCH.items():
        values = dict(values)
        path = targets[name] / "ENVIRONMENT"
        print(f"  {name}: {', '.join(f'{k}={v}' for k, v in values.items())}")
        if not dry_run:
            update_environment(path, values)


def restore_models(home: Path, model_root: Path, *, dry_run: bool) -> None:
    if not model_root.joinpath("MANIFEST.json").is_file() and not dry_run:
        raise BootstrapError(f"No model manifest found at {model_root}.")
    tool = Path(__file__).resolve().with_name("studio_models.py")
    command = [sys.executable, str(tool), "restore", "--root", str(model_root),
               "--pinokio-home", str(home)]
    run(command, dry_run=dry_run)


def local_profile() -> dict:
    data = request_json("http://127.0.0.1:47873/api/hub/registry/hardware-profiles")
    hardware = data.get("local_hardware")
    return hardware if isinstance(hardware, dict) else {}


def enroll_agent(controller: str, machine_name: str | None, *, dry_run: bool) -> dict:
    if dry_run:
        print(f"  Would securely prompt for the enrollment code and join {controller}")
        return {"mode": "agent"}
    hardware = local_profile()
    profile_id = hardware.get("profile_id")
    if not profile_id:
        raise BootstrapError(
            "Hub detected this Mac as "
            f"{hardware.get('machine_type') or 'unknown model'} / "
            f"{hardware.get('chip') or 'unknown chip'} / "
            f"{hardware.get('memory_gb') or hardware.get('total_gb') or 'unknown'} GB, "
            "but the Controller has no matching reusable profile yet. Add that hardware "
            "profile on the Controller, update this Hub, then rerun enrollment."
        )
    code = getpass.getpass("Controller registration code (hidden): ").strip()
    if not code:
        raise BootstrapError("No registration code entered; fleet enrollment was not changed.")
    return request_json(
        "http://127.0.0.1:47873/api/hub/setup/join", method="POST",
        body={
            "controller_url": controller,
            "enrollment_code": code,
            "hardware_profile_id": profile_id,
            "machine_name": machine_name or socket.gethostname().split(".", 1)[0],
        },
        timeout=30,
    )


def validate_host() -> None:
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise BootstrapError("This fleet kit supports Apple-silicon Macs only.")


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--apps-only", action="store_true",
                      help="install Pinokio, Studios, dependencies, and startup settings")
    mode.add_argument("--models-only", action="store_true",
                      help="copy RAM-qualified models without starting any Studio")
    ap.add_argument("--dry-run", action="store_true", help="show every step without changing this Mac")
    ap.add_argument("--pinokio-timeout", type=int, default=900,
                    help="seconds to wait for Pinokio first-run setup (default: 900)")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    validate_host()
    kit_root = Path(__file__).resolve().parent
    model_root = kit_root.parent / "studio-models"

    heading("TerraNash Mac identity")
    print(f"  hostname: {socket.gethostname().split('.', 1)[0]}")
    print(f"  chip architecture: {platform.machine()}")
    mem = subprocess.run(["/usr/sbin/sysctl", "-n", "hw.memsize"],
                         capture_output=True, text=True)
    try:
        print(f"  unified memory: {round(int(mem.stdout.strip()) / 1024 ** 3)} GB")
    except ValueError:
        print("  unified memory: unknown")
    print(f"  SSD model source: {model_root}")

    if args.models_only:
        home = resolve_pinokio_home()
        if home is None:
            raise BootstrapError("Pinokio setup is not complete. Run step 1 first.")
        missing = [
            spec["title"] for spec in APPS
            if not (home / "api" / spec["name"]).is_dir()
            and not (home / "api" / f"{spec['name']}.git").is_dir()
        ]
        if missing:
            raise BootstrapError(
                f"Missing {', '.join(missing)}. Run step 1 before copying models."
            )
        heading("Copy RAM-matched models")
        print(f"  PINOKIO_HOME: {home}")
        restore_models(home, model_root, dry_run=args.dry_run)
        heading("Complete")
        print("  Model caches are ready. Start the Studios normally in Pinokio.")
        print("  Safe to run again: complete model packages are skipped.")
        return 0

    heading("Pinokio")
    app = install_pinokio(kit_root, dry_run=args.dry_run)
    install_login_agent(app, dry_run=args.dry_run)
    home, pterm = wait_for_pinokio(app, args.pinokio_timeout, dry_run=args.dry_run)
    print(f"  PINOKIO_HOME: {home}")

    heading("Install Hub, Image, and Voice")
    targets = {spec["name"]: ensure_repo(pterm, home, spec, dry_run=args.dry_run)
               for spec in APPS}
    for spec in APPS:
        ensure_dependencies(pterm, targets[spec["name"]], spec, dry_run=args.dry_run)

    heading("Configure one Pinokio startup graph")
    configure_autolaunch(targets, dry_run=args.dry_run)

    heading("Complete")
    print("  Pinokio starts at login.")
    print("  Image Studio, Voice Studio, and Studio Hub are installed.")
    print("  Next: run step 2 on the SSD to copy this Mac's model caches.")
    print("  Safe to run again: completed installation steps are detected and skipped.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (BootstrapError, subprocess.CalledProcessError) as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        print("Fix the reported step and run this installer again; completed steps are retained.",
              file=sys.stderr)
        raise SystemExit(1)
