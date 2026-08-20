#!/usr/bin/env python3
"""Install TerraNash Studios after Pinokio's visible first-run tools are ready."""

from __future__ import annotations

import argparse
import configparser
import json
import os
import platform
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


CONTROL_PLANE = "http://127.0.0.1:42000"

APPS = (
    {
        "name": "imagestudio-mac",
        "title": "Image Studio",
        "url": "https://github.com/theng12/imagestudio-mac.git",
        "generation_import": "import mflux",
    },
    {
        "name": "voicestudio-mac",
        "title": "Voice Studio",
        "url": "https://github.com/theng12/voicestudio-mac.git",
        "generation_import": "import mlx_audio, torch, transformers",
    },
    {
        "name": "studiohub-mac",
        "title": "Studio Hub",
        "url": "https://github.com/theng12/studiohub-mac.git",
        "generation_import": None,
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


def run(
    command: list[str],
    *,
    dry_run: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    print(f"  $ {' '.join(command)}", flush=True)
    if dry_run:
        return subprocess.CompletedProcess(command, 0, "", "")
    return subprocess.run(command, check=check, text=True)


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _control_plane_home() -> Path | None:
    try:
        with urllib.request.urlopen(f"{CONTROL_PLANE}/pinokio/home", timeout=2) as reply:
            value = json.loads(reply.read()).get("path")
    except (OSError, ValueError, urllib.error.URLError):
        return None
    return Path(str(value)).expanduser().resolve() if value else None


def resolve_pinokio_home() -> Path | None:
    configured = read_json(Path.home() / ".pinokio/config.json").get("home")
    if configured:
        return Path(str(configured)).expanduser().resolve()
    live = _control_plane_home()
    if live:
        return live
    fallback = os.environ.get("PINOKIO_HOME", "").strip()
    return Path(fallback).expanduser().resolve() if fallback else None


def resolve_pterm(home: Path) -> Path | None:
    candidates = (home / "bin/npm/bin/pterm", home / "bin/pterm")
    found = next((path for path in candidates if path.is_file() and os.access(path, os.X_OK)), None)
    if found:
        return found
    executable = shutil.which("pterm")
    return Path(executable).resolve() if executable else None


def resolve_node(home: Path) -> Path | None:
    candidates = (
        home / "bin/miniforge/bin/node",
        home / "bin/miniconda/bin/node",
        home / "bin/node",
    )
    found = next((path for path in candidates if path.is_file() and os.access(path, os.X_OK)), None)
    if found:
        return found
    executable = shutil.which("node")
    return Path(executable).resolve() if executable else None


def control_plane_ready() -> bool:
    try:
        with urllib.request.urlopen(f"{CONTROL_PLANE}/pinokio/home", timeout=2) as reply:
            return 200 <= getattr(reply, "status", 200) < 300
    except (OSError, urllib.error.URLError):
        return False


def check_pinokio_ready(*, dry_run: bool) -> tuple[Path, Path]:
    home = resolve_pinokio_home()
    pterm = resolve_pterm(home) if home else None
    node = resolve_node(home) if home else None
    if home and pterm and node and (dry_run or control_plane_ready()):
        os.environ["PATH"] = f"{node.parent}:{os.environ.get('PATH', '')}"
        return home, pterm
    raise BootstrapError(
        "Pinokio tools are not ready. Open Pinokio, finish its visible Install Tools / "
        "first-run setup, close any setup prompt, then rerun 2 Install Studios.command."
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
            "using the canonical checkout and leaving the legacy folder untouched."
        )
    target = canonical if canonical.exists() else legacy if legacy.exists() else canonical
    if target.exists():
        if not (target / ".git").is_dir():
            raise BootstrapError(f"{target} exists but is not a Git checkout.")
        origin = git_origin(target)
        if normalize_git_url(origin) != normalize_git_url(app["url"]):
            raise BootstrapError(f"{target} belongs to a different repository; refusing to use it.")
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
        print(f"  {app['title']} checkout already exists; updating {target.name}.")
        run(["git", "-C", str(target), "pull", "--ff-only"], dry_run=dry_run)
        return target
    run([str(pterm), "download", app["url"], app["name"]], dry_run=dry_run)
    if not dry_run and not (canonical / ".git").is_dir():
        raise BootstrapError(f"Pinokio did not create {canonical}.")
    return canonical


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
            raise BootstrapError(f"{app['title']} has a legacy service but no unservice.js.")
        print(f"  Converting {app['title']} to Pinokio-owned startup…")
        install_script(pterm, installed_name, "unservice.js", dry_run=dry_run)
        if not dry_run and service_marker.exists():
            raise BootstrapError(f"{app['title']} legacy service removal did not complete.")
    base_import = (
        "import fastapi, httpx, psutil, uvicorn"
        if app["name"] == "studiohub-mac"
        else "import fastapi, huggingface_hub, uvicorn"
    )
    if not python_imports(target, base_import):
        print(f"  Installing {app['title']} base environment…")
        install_script(pterm, installed_name, "install.js", dry_run=dry_run)
        if not dry_run and not python_imports(target, base_import):
            raise BootstrapError(f"{app['title']} base dependency verification failed.")
    else:
        print(f"  {app['title']} base environment is verified.")
    generation_import = app["generation_import"]
    if generation_import and not python_imports(target, generation_import):
        print(f"  Installing {app['title']} generation environment…")
        install_script(pterm, installed_name, "install_generation.js", dry_run=dry_run)
        if not dry_run and not python_imports(target, generation_import):
            raise BootstrapError(f"{app['title']} generation dependency verification failed.")
    elif generation_import:
        print(f"  {app['title']} generation environment is verified.")


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
        output.append("# TerraNash bootstrap: one Pinokio-owned startup graph.")
        output.extend(f"{key}={value}" for key, value in remaining.items())
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text("\n".join(output).rstrip() + "\n")
    temporary.replace(path)


def configure_autolaunch(targets: dict[str, Path], *, dry_run: bool) -> None:
    for name, configured in AUTOLAUNCH.items():
        values = dict(configured)
        print(f"  {targets[name].name}: {', '.join(f'{key}={value}' for key, value in values.items())}")
        if not dry_run:
            update_environment(targets[name] / "ENVIRONMENT", values)


def validate_host() -> None:
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise BootstrapError("This fleet SSD supports Apple-silicon Macs only.")


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--studios-only", action="store_true", required=True)
    value.add_argument("--dry-run", action="store_true", help="print every Studio action without writing")
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    validate_host()
    home, pterm = check_pinokio_ready(dry_run=args.dry_run)
    heading("Install Image Studio, Voice Studio, and Studio Hub")
    print(f"  PINOKIO_HOME: {home}")
    targets = {
        spec["name"]: ensure_repo(pterm, home, spec, dry_run=args.dry_run)
        for spec in APPS
    }
    for spec in APPS:
        ensure_dependencies(pterm, targets[spec["name"]], spec, dry_run=args.dry_run)
    heading("Configure independent Studio startup")
    configure_autolaunch(targets, dry_run=args.dry_run)
    heading("Complete")
    print("  Studios and generation dependencies are ready.")
    print("  No models were copied and this Mac was not enrolled.")
    print("  Next: run 3 Manage AI Models.command, then enroll from Studio Hub when ready.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (BootstrapError, subprocess.CalledProcessError) as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        print("Fix the reported step and rerun this command; completed work is retained.", file=sys.stderr)
        raise SystemExit(1)
