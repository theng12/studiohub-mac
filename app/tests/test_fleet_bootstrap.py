import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def load_tool(name: str):
    path = ROOT / "tools" / name
    spec = importlib.util.spec_from_file_location(name.removesuffix(".py"), path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_bootstrap_autolaunch_is_one_dependency_graph(tmp_path):
    bootstrap = load_tool("fleet_bootstrap.py")
    targets = {}
    for name in bootstrap.AUTOLAUNCH:
        target = tmp_path / name
        target.mkdir()
        (target / "ENVIRONMENT").write_text("HF_HOME=./cache/HF_HOME\n")
        targets[name] = target

    bootstrap.configure_autolaunch(targets, dry_run=False)

    image = (targets["imagestudio-mac"] / "ENVIRONMENT").read_text()
    voice = (targets["voicestudio-mac"] / "ENVIRONMENT").read_text()
    hub = (targets["studiohub-mac"] / "ENVIRONMENT").read_text()
    assert "PINOKIO_SCRIPT_AUTOLAUNCH_ENABLED=false" in image
    assert "PINOKIO_SCRIPT_AUTOLAUNCH_ENABLED=false" in voice
    assert "PINOKIO_SCRIPT_AUTOLAUNCH_ENABLED=true" in hub
    assert "PINOKIO_SCRIPT_REQUIRES=imagestudio-mac,voicestudio-mac" in hub
    assert "HF_HOME=./cache/HF_HOME" in image


def test_environment_update_replaces_values_without_duplicates(tmp_path):
    bootstrap = load_tool("fleet_bootstrap.py")
    environment = tmp_path / "ENVIRONMENT"
    environment.write_text(
        "PINOKIO_SCRIPT_AUTOLAUNCH=old.js\n"
        "PINOKIO_SCRIPT_AUTOLAUNCH_ENABLED=true\n"
    )

    bootstrap.update_environment(environment, {
        "PINOKIO_SCRIPT_AUTOLAUNCH": "start.js",
        "PINOKIO_SCRIPT_AUTOLAUNCH_ENABLED": "false",
    })

    value = environment.read_text()
    assert value.count("PINOKIO_SCRIPT_AUTOLAUNCH=") == 1
    assert value.count("PINOKIO_SCRIPT_AUTOLAUNCH_ENABLED=") == 1
    assert "PINOKIO_SCRIPT_AUTOLAUNCH=start.js" in value
    assert "PINOKIO_SCRIPT_AUTOLAUNCH_ENABLED=false" in value


def test_model_root_prefers_current_label_and_can_find_renamed_manifest(tmp_path):
    models = load_tool("studio_models.py")
    current = tmp_path / "ugreen-terranash"
    current.mkdir()
    assert models.find_default_root(tmp_path) == current / "studio-models"

    current.rmdir()
    renamed = tmp_path / "ugreen-terranash 1" / "studio-models"
    renamed.mkdir(parents=True)
    (renamed / "MANIFEST.json").write_text("{}")
    assert models.find_default_root(tmp_path) == renamed


def test_stage_prune_is_limited_to_obsolete_hf_packages(tmp_path):
    models = load_tool("studio_models.py")
    keep = tmp_path / "voice" / "family" / "models--owner--keep"
    stale = tmp_path / "voice" / "family" / "models--owner--stale"
    unrelated = tmp_path / "voice" / "family" / "notes"
    image = tmp_path / "image" / "family" / "models--owner--image"
    for path in (keep, stale, unrelated, image):
        path.mkdir(parents=True)

    found = models.stale_staged_packages(
        tmp_path,
        {keep.relative_to(tmp_path), image.relative_to(tmp_path)},
    )

    assert found == [stale]


def test_bootstrap_has_no_hard_coded_volume_path():
    source = (ROOT / "tools/fleet_bootstrap.py").read_text()
    wrapper = (ROOT / "tools/Install TerraNash Studios.command").read_text()
    assert "/Volumes/UGREEN-1TB" not in source
    assert "/Volumes/ugreen-terranash" not in source
    assert "${0:A:h}" in wrapper
    assert "/usr/bin/python3" not in wrapper
    assert 'pterm_path\" which python3 --json' in wrapper
    assert '[[ -x "$python_path" ]] && break' in wrapper
    assert '[[ -n "$pterm_path" ]] && break' not in wrapper
    assert "/pinokio/path/pterm" in wrapper
    assert "/pinokio/path/node" in wrapper
    assert 'export PATH="${node_path:h}:$PATH"' in wrapper
    assert "sys.version_info < (3, 9)" in wrapper
    assert 'USER_HOME="${HOME:-}"' in wrapper
    assert '$USER_HOME/Library/Logs/TerraNash' in wrapper
    assert '$SCRIPT_DIR/logs' not in wrapper
    assert '$HOME/.pinokio' not in wrapper


def test_pinokio_home_accepts_spaces_apostrophes_and_unicode(tmp_path, monkeypatch):
    bootstrap = load_tool("fleet_bootstrap.py")
    user_home = tmp_path / "Ana O'Connor 測試"
    pinokio_home = user_home / "My Pinokio 安裝"
    config = user_home / ".pinokio/config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"home": str(pinokio_home)}))
    monkeypatch.setenv("HOME", str(user_home))

    assert bootstrap.resolve_pinokio_home() == pinokio_home.resolve()


def test_staged_payload_permissions_are_portable_across_user_ids(tmp_path):
    models = load_tool("studio_models.py")
    package = tmp_path / "models--owner--private"
    blob = package / "blobs" / "model.bin"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"model")
    package.chmod(0o700)
    blob.parent.chmod(0o700)
    blob.chmod(0o600)
    link = package / "current"
    link.symlink_to(blob)

    models.make_portable_readable(package)

    assert package.stat().st_mode & 0o555 == 0o555
    assert blob.parent.stat().st_mode & 0o555 == 0o555
    assert blob.stat().st_mode & 0o444 == 0o444
    assert link.is_symlink()


def test_restaging_removes_legacy_ssd_logs(tmp_path, monkeypatch):
    models = load_tool("studio_models.py")
    kit = tmp_path / "terranash-bootstrap"
    legacy_log = kit / "logs/bootstrap-old.log"
    legacy_log.parent.mkdir(parents=True)
    legacy_log.write_text("PINOKIO_HOME=/Users/old-account/pinokio")
    installer = kit / "installers" / models.PINOKIO_DMG
    installer.parent.mkdir()
    installer.write_bytes(b"test installer")
    monkeypatch.setattr(models, "file_sha256", lambda _path: models.PINOKIO_DMG_SHA256)

    models.stage_bootstrap_kit(tmp_path, plan_only=False)

    assert not legacy_log.parent.exists()
    assert (kit / "Install TerraNash Studios.command").stat().st_mode & 0o555 == 0o555
    assert (tmp_path / "READ-ME-FIRST.md").stat().st_mode & 0o444 == 0o444


def test_ssd_guide_separates_machine_paths_and_stays_short():
    guide = (ROOT / "SSD-COPY-README.md").read_text()
    for heading in (
        "## NEW MACHINE — install everything",
        "## EXISTING MACHINE — models are missing",
        "## JOIN CONTROLLER — do this later",
        "## REPAIR — an earlier run failed",
        "## MODELS DID NOT COPY — find the correct log",
        "## SSD MAINTAINER — main Mac only",
    ):
        assert heading in guide
    assert "~/Library/Logs/TerraNash" in guide
    assert "terranash-bootstrap/logs" not in guide
    assert ".shutdownStall" in guide
    assert len(guide.splitlines()) < 90


def test_git_url_comparison_ignores_git_suffix():
    bootstrap = load_tool("fleet_bootstrap.py")
    assert bootstrap.normalize_git_url("https://github.com/theng12/studiohub-mac.git") == (
        bootstrap.normalize_git_url("https://github.com/theng12/studiohub-mac/")
    )
