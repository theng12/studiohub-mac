#!/usr/bin/env python3
"""Synchronize the tracked TerraNash bootstrap source to a mounted SSD."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import NamedTuple


KIT_DIRECTORY = "terranash-bootstrap"
INVENTORY = "RELEASE-INVENTORY.sha256"
OBSOLETE_PATHS = (
    "terranash-bootstrap/IMPLEMENTATION-REPORT.md",
    "terranash-bootstrap/docs/superpowers/plans/2026-08-19-three-stage-new-mac-setup.md",
)


class SyncError(RuntimeError):
    pass


class SyncResult(NamedTuple):
    changed: tuple[str, ...]
    removed: tuple[str, ...]
    drift: tuple[str, ...]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_files(source_root: Path) -> tuple[tuple[Path, str], ...]:
    files: list[tuple[Path, str]] = []
    for source in sorted((source_root / "kit").rglob("*")):
        if source.is_file() and "__pycache__" not in source.parts and source.suffix != ".pyc":
            relative = source.relative_to(source_root / "kit")
            files.append((source, str(Path(KIT_DIRECTORY) / relative)))
    for source in sorted((source_root / "root").rglob("*")):
        if source.is_file():
            files.append((source, str(source.relative_to(source_root / "root"))))
    if not files:
        raise SyncError(f"No canonical SSD files found under {source_root}.")
    return tuple(files)


def atomic_write(path: Path, content: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.sync-{os.getpid()}")
    try:
        temporary.write_bytes(content)
        temporary.chmod(mode & 0o777)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def obsolete_files(volume_root: Path) -> tuple[Path, ...]:
    paths = [volume_root / value for value in OBSOLETE_PATHS]
    paths.extend(sorted((volume_root / KIT_DIRECTORY / "installers").glob("Tailscale-*.pkg")))
    return tuple(path for path in paths if path.exists())


def manifest_installer_files(volume_root: Path) -> tuple[Path, ...]:
    manifest = volume_root / KIT_DIRECTORY / "installers/MANIFEST.json"
    try:
        payload = json.loads(manifest.read_text())
        names = [str(row["filename"]) for row in payload["apps"]]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise SyncError(f"Could not read synchronized installer manifest: {exc}") from exc
    result = []
    for name in names:
        if Path(name).name != name:
            raise SyncError(f"Unsafe installer filename in manifest: {name}")
        path = manifest.parent / name
        if not path.is_file():
            raise SyncError(f"Required SSD installer is missing: {path}")
        result.append(path)
    return tuple(result)


def inventory_text(source_root: Path, volume_root: Path) -> str:
    kit_root = volume_root / KIT_DIRECTORY
    paths = [volume_root / target for _source, target in source_files(source_root)]
    paths.extend(manifest_installer_files(volume_root))
    for optional in (
        volume_root / "AGENTS.md",
        volume_root / "studio-models/MANIFEST.json",
    ):
        if optional.is_file():
            paths.append(optional)
    unique = sorted(set(paths), key=lambda path: os.path.relpath(path, kit_root))
    lines = []
    for path in unique:
        if not path.is_file():
            raise SyncError(f"Inventory source is missing: {path}")
        relative = os.path.relpath(path, kit_root)
        if not relative.startswith("."):
            relative = f"./{relative}"
        lines.append(f"{file_sha256(path)}  {relative}")
    return "\n".join(lines) + "\n"


def sync(source_root: Path, volume_root: Path, *, check: bool) -> SyncResult:
    source_root = source_root.resolve()
    volume_root = volume_root.resolve()
    kit_root = volume_root / KIT_DIRECTORY
    if not volume_root.is_dir() or not kit_root.is_dir():
        raise SyncError(f"Expected a mounted TerraNash kit at {kit_root}.")
    if kit_root.is_symlink():
        raise SyncError(f"Refusing to synchronize through a symlinked kit root: {kit_root}")

    changed: list[str] = []
    drift: list[str] = []
    for source, target_name in source_files(source_root):
        target = volume_root / target_name
        content = source.read_bytes()
        source_mode = source.stat().st_mode & 0o777
        target_mode = target.stat().st_mode & 0o777 if target.is_file() else None
        differs = (
            target.is_symlink()
            or not target.is_file()
            or target.read_bytes() != content
            or target_mode != source_mode
        )
        if differs:
            if check:
                drift.append(target_name)
            else:
                atomic_write(target, content, source_mode)
                changed.append(target_name)

    retired = obsolete_files(volume_root)
    if check:
        drift.extend(str(path.relative_to(volume_root)) for path in retired)
        expected_inventory = inventory_text(source_root, volume_root)
        inventory = kit_root / INVENTORY
        if not inventory.is_file() or inventory.read_text() != expected_inventory:
            drift.append(str(Path(KIT_DIRECTORY) / INVENTORY))
        return SyncResult((), (), tuple(sorted(set(drift))))

    removed = []
    for path in retired:
        relative = str(path.relative_to(volume_root))
        path.unlink()
        removed.append(relative)

    inventory = kit_root / INVENTORY
    content = inventory_text(source_root, volume_root).encode()
    if not inventory.is_file() or inventory.read_bytes() != content:
        atomic_write(inventory, content)
        changed.append(str(Path(KIT_DIRECTORY) / INVENTORY))
    return SyncResult(tuple(changed), tuple(removed), ())


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--volume", required=True, type=Path, help="mounted SSD volume root")
    value.add_argument(
        "--source",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "ssd_bootstrap",
        help="canonical source root",
    )
    value.add_argument("--check", action="store_true", help="report drift without writing")
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    result = sync(args.source, args.volume, check=args.check)
    if args.check:
        if result.drift:
            print("SSD drift detected:")
            for path in result.drift:
                print(f"  {path}")
            return 1
        print("SSD matches the canonical Git source.")
        return 0
    for path in result.changed:
        print(f"Updated: {path}")
    for path in result.removed:
        print(f"Removed: {path}")
    print("SSD bootstrap synchronized and inventoried.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SyncError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
