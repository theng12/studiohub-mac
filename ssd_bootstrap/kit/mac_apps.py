#!/usr/bin/env python3
"""Install the checksum-pinned ordinary macOS applications from this SSD."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class InstallError(RuntimeError):
    """A verified installer cannot be used safely."""


@dataclass(frozen=True)
class InstallerAsset:
    id: str
    title: str
    version: str
    filename: str
    source_url: str
    sha256: str
    kind: str
    app_name: str
    bundle_id: str
    team_id: str


Runner = Callable[..., object]
LOGIN_AGENT_LABEL = "com.terranash.pinokio"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest(path: Path) -> list[InstallerAsset]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallError(f"Could not read installer manifest {path}: {exc}") from exc
    if payload.get("schema_version") != 1 or not isinstance(payload.get("apps"), list):
        raise InstallError("Installer manifest must use schema_version 1 and an apps list.")
    assets: list[InstallerAsset] = []
    required = {field.name for field in InstallerAsset.__dataclass_fields__.values()}
    for index, row in enumerate(payload["apps"], 1):
        if not isinstance(row, dict) or set(row) != required:
            raise InstallError(f"Installer manifest app {index} has unexpected or missing fields.")
        asset = InstallerAsset(**{key: str(row[key]) for key in required})
        if asset.kind != "dmg":
            raise InstallError(f"Unsupported installer kind for {asset.title}: {asset.kind}")
        if not re.fullmatch(r"[0-9a-f]{64}", asset.sha256):
            raise InstallError(f"{asset.title} has no valid lowercase SHA-256 digest.")
        if not re.fullmatch(r"[A-Z0-9]{10}", asset.team_id):
            raise InstallError(f"{asset.title} has no valid Apple team identifier.")
        if Path(asset.filename).name != asset.filename or not asset.filename:
            raise InstallError(f"{asset.title} has an unsafe installer filename.")
        if Path(asset.app_name).name != asset.app_name or not asset.app_name.endswith(".app"):
            raise InstallError(f"{asset.title} has an unsafe application name.")
        assets.append(asset)
    if not assets:
        raise InstallError("Installer manifest contains no applications.")
    return assets


def verify_asset(asset: InstallerAsset, installers_dir: Path) -> Path:
    path = installers_dir / asset.filename
    if not path.is_file():
        raise InstallError(f"Missing {path}. Restore the verified installer asset to the SSD.")
    actual = file_sha256(path)
    if actual != asset.sha256:
        raise InstallError(
            f"{asset.title} installer checksum mismatch: expected {asset.sha256}, got {actual}."
        )
    return path


def version_tuple(value: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", value)
    return tuple(int(part) for part in parts) if parts else ()


def needs_install(current: str, bundled: str) -> bool:
    current_tuple = version_tuple(current)
    return not current_tuple or current_tuple < version_tuple(bundled)


def bundle_info(app: Path) -> dict:
    try:
        with (app / "Contents/Info.plist").open("rb") as handle:
            value = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException) as exc:
        raise InstallError(f"Could not read {app}'s application identity: {exc}") from exc
    return value if isinstance(value, dict) else {}


def installed_version(asset: InstallerAsset, applications_dir: Path) -> str:
    app = applications_dir / asset.app_name
    if not app.is_dir():
        return ""
    info = bundle_info(app)
    if info.get("CFBundleIdentifier") != asset.bundle_id:
        raise InstallError(f"{app} has an unexpected bundle identifier; refusing to replace it.")
    return str(info.get("CFBundleShortVersionString") or "")


def _run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, text=True, **kwargs)


def verify_app(app: Path, asset: InstallerAsset, runner: Runner = _run) -> None:
    info = bundle_info(app)
    if info.get("CFBundleIdentifier") != asset.bundle_id:
        raise InstallError(f"{asset.title} installer contains the wrong application bundle.")
    runner(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)])
    result = subprocess.run(
        ["/usr/bin/codesign", "-dv", "--verbose=4", str(app)],
        check=True,
        text=True,
        capture_output=True,
    )
    signing = f"{result.stdout}\n{result.stderr}"
    if f"TeamIdentifier={asset.team_id}" not in signing:
        raise InstallError(f"{asset.title} has an unexpected Apple signing team.")
    runner(["/usr/sbin/spctl", "--assess", "--type", "execute", str(app)])


def _find_mounted_app(mount: Path, app_name: str) -> Path:
    direct = mount / app_name
    if direct.is_dir():
        return direct
    matches = [path for path in mount.iterdir() if path.is_dir() and path.name == app_name]
    if len(matches) == 1:
        return matches[0]
    raise InstallError(f"Verified disk image contains no unambiguous {app_name}.")


def install_dmg(
    asset: InstallerAsset,
    installer: Path,
    applications_dir: Path,
    runner: Runner = _run,
) -> None:
    mount = Path(tempfile.mkdtemp(prefix=f"terranash-{asset.id}-"))
    attached = False
    try:
        runner([
            "/usr/bin/hdiutil", "attach", "-readonly", "-nobrowse",
            "-mountpoint", str(mount), str(installer),
        ])
        attached = True
        source = _find_mounted_app(mount, asset.app_name)
        verify_app(source, asset, runner)
        runner(["/usr/bin/osascript", "-e", f'tell application id "{asset.bundle_id}" to quit'], check=False)
        runner(["/usr/bin/sudo", "/usr/bin/ditto", str(source), str(applications_dir / asset.app_name)])
    finally:
        if attached:
            runner(["/usr/bin/hdiutil", "detach", str(mount)], check=False)
        shutil.rmtree(mount, ignore_errors=True)
    verify_app(applications_dir / asset.app_name, asset, runner)


def install_assets(
    assets: list[InstallerAsset],
    installers_dir: Path,
    *,
    dry_run: bool,
    applications_dir: Path = Path("/Applications"),
    runner: Runner = _run,
) -> dict[str, int]:
    counts = {"planned": 0, "installed": 0, "skipped": 0}
    for asset in assets:
        installer = verify_asset(asset, installers_dir)
        current = installed_version(asset, applications_dir)
        if current and not needs_install(current, asset.version):
            print(f"  {asset.title} {current} is already installed.")
            counts["skipped"] += 1
            continue
        if dry_run:
            print(f"  Would verify and install {asset.title} {asset.version} from {installer.name}.")
            counts["planned"] += 1
            continue
        print(f"  Installing {asset.title} {asset.version}…")
        install_dmg(asset, installer, applications_dir, runner)
        counts["installed"] += 1
    return counts


def manual_next_steps() -> tuple[str, ...]:
    return (
        "Open Pinokio and finish its visible Install Tools / first-run setup.",
        "Install Tailscale from the Mac App Store, open it, and sign in.",
        "Open Yam Display and approve its requested permissions.",
        "Open Latest and review the applications it can maintain.",
    )


def login_agent_payload(pinokio: Path) -> dict:
    return {
        "Label": LOGIN_AGENT_LABEL,
        "ProgramArguments": ["/usr/bin/open", "-gj", str(pinokio)],
        "RunAtLoad": True,
        "ProcessType": "Interactive",
    }


def install_login_agent(*, dry_run: bool, runner: Runner = _run) -> None:
    pinokio = Path("/Applications/Pinokio.app")
    agent = Path.home() / "Library/LaunchAgents" / f"{LOGIN_AGENT_LABEL}.plist"
    print(f"  Pinokio login item: {agent}")
    if dry_run:
        return
    if not pinokio.is_dir():
        raise InstallError("Pinokio is not installed; cannot configure its login item.")
    agent.parent.mkdir(parents=True, exist_ok=True)
    temporary = agent.with_name(f".{agent.name}.{os.getpid()}.tmp")
    with temporary.open("wb") as handle:
        plistlib.dump(login_agent_payload(pinokio), handle)
    temporary.chmod(0o600)
    temporary.replace(agent)
    domain = f"gui/{os.getuid()}"
    runner(["/bin/launchctl", "bootout", domain, str(agent)], check=False)
    runner(["/bin/launchctl", "bootstrap", domain, str(agent)])


def validate_host() -> None:
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise InstallError("This fleet SSD supports Apple-silicon Macs only.")


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--dry-run", action="store_true", help="verify assets and print actions only")
    value.add_argument("--manifest", type=Path, help="installer manifest override")
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    validate_host()
    kit_root = Path(__file__).resolve().parent
    manifest = args.manifest or kit_root / "installers/MANIFEST.json"
    assets = load_manifest(manifest)
    print("\n== Install ordinary Mac applications ==")
    counts = install_assets(assets, manifest.parent, dry_run=args.dry_run)
    install_login_agent(dry_run=args.dry_run)
    print(
        f"\nComplete: {counts['installed']} installed, {counts['skipped']} already current, "
        f"{counts['planned']} planned."
    )
    print("\nManual next steps:")
    for index, step in enumerate(manual_next_steps(), 1):
        print(f"  {index}. {step}")
    print("Then run 2 Install Studios.command from this SSD.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (InstallError, subprocess.CalledProcessError) as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
