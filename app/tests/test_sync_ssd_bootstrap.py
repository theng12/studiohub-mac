from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def load_sync_tool():
    path = ROOT / "tools/sync_ssd_bootstrap.py"
    spec = importlib.util.spec_from_file_location("sync_ssd_bootstrap", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def source_fixture(root: Path) -> Path:
    source = root / "source"
    (source / "kit/installers").mkdir(parents=True)
    (source / "kit/tests").mkdir()
    (source / "root").mkdir()
    (source / "kit/mac_apps.py").write_text("print('canonical')\n")
    command = source / "kit/1 Install Mac Apps.command"
    command.write_text("#!/bin/zsh\nexit 0\n")
    command.chmod(0o755)
    migration_command = source / "kit/5 Migrate Studio Updates.command"
    migration_command.write_text("#!/bin/zsh\nexit 0\n")
    migration_command.chmod(0o755)
    (source / "kit/runtime_state_migration.py").write_text("print('migration')\n")
    (source / "kit/tests/test_mac_apps.py").write_text("# canonical test\n")
    (source / "kit/installers/MANIFEST.json").write_text(json.dumps({
        "schema_version": 1,
        "apps": [{"filename": "Keep.dmg"}],
    }))
    (source / "root/START HERE - TerraNash Mac Setup.md").write_text("App Store Tailscale\n")
    return source


def volume_fixture(root: Path) -> Path:
    volume = root / "volume"
    kit = volume / "terranash-bootstrap"
    (kit / "installers").mkdir(parents=True)
    (kit / "logs").mkdir()
    (volume / "studio-models").mkdir()
    (kit / "installers/Keep.dmg").write_bytes(b"keep installer")
    (kit / "installers/Tailscale-1.102.2-macos.pkg").write_bytes(b"retired")
    (kit / "logs/keep.log").write_text("preserve\n")
    (volume / "studio-models/MANIFEST.json").write_text("{}\n")
    return volume


def test_sync_copies_canonical_files_removes_tailscale_and_preserves_assets(tmp_path):
    tool = load_sync_tool()
    source = source_fixture(tmp_path)
    volume = volume_fixture(tmp_path)

    result = tool.sync(source, volume, check=False)

    kit = volume / "terranash-bootstrap"
    assert (kit / "mac_apps.py").read_text() == "print('canonical')\n"
    assert (kit / "runtime_state_migration.py").read_text() == "print('migration')\n"
    assert (kit / "5 Migrate Studio Updates.command").stat().st_mode & 0o777 == 0o755
    assert (volume / "START HERE - TerraNash Mac Setup.md").read_text() == "App Store Tailscale\n"
    assert not (kit / "installers/Tailscale-1.102.2-macos.pkg").exists()
    assert (kit / "installers/Keep.dmg").read_bytes() == b"keep installer"
    assert (kit / "logs/keep.log").read_text() == "preserve\n"
    inventory = (kit / "RELEASE-INVENTORY.sha256").read_text()
    assert "Tailscale" not in inventory
    assert "./mac_apps.py" in inventory
    assert "./installers/Keep.dmg" in inventory
    assert "../studio-models/MANIFEST.json" in inventory
    assert "terranash-bootstrap/installers/Tailscale-1.102.2-macos.pkg" in result.removed


def test_check_reports_drift_without_writing(tmp_path):
    tool = load_sync_tool()
    source = source_fixture(tmp_path)
    volume = volume_fixture(tmp_path)
    tool.sync(source, volume, check=False)
    target = volume / "terranash-bootstrap/mac_apps.py"
    target.write_text("locally changed\n")

    result = tool.sync(source, volume, check=True)

    assert "terranash-bootstrap/mac_apps.py" in result.drift
    assert target.read_text() == "locally changed\n"


def test_check_is_clean_immediately_after_sync(tmp_path):
    tool = load_sync_tool()
    source = source_fixture(tmp_path)
    volume = volume_fixture(tmp_path)
    tool.sync(source, volume, check=False)

    result = tool.sync(source, volume, check=True)

    assert result.drift == ()


def test_check_and_sync_detect_and_repair_executable_mode_drift(tmp_path):
    tool = load_sync_tool()
    source = source_fixture(tmp_path)
    volume = volume_fixture(tmp_path)
    tool.sync(source, volume, check=False)
    command = volume / "terranash-bootstrap/1 Install Mac Apps.command"
    command.chmod(0o644)

    assert "terranash-bootstrap/1 Install Mac Apps.command" in tool.sync(
        source, volume, check=True
    ).drift

    tool.sync(source, volume, check=False)
    assert command.stat().st_mode & 0o777 == 0o755
