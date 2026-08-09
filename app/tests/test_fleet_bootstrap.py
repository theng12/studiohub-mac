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


def test_existing_startup_service_is_converted_automatically(tmp_path, monkeypatch):
    bootstrap = load_tool("fleet_bootstrap.py")
    target = tmp_path / "imagestudio-mac"
    marker = target / "service/.installed"
    marker.parent.mkdir(parents=True)
    marker.write_text("installed")
    (target / "unservice.js").write_text("module.exports = {}")
    calls = []

    def run_script(_pterm, name, script, *, dry_run):
        calls.append((name, script, dry_run))
        marker.unlink()

    monkeypatch.setattr(bootstrap, "install_script", run_script)
    monkeypatch.setattr(bootstrap, "python_imports", lambda *_args: True)
    bootstrap.ensure_dependencies(
        Path("/pinokio/pterm"), target, bootstrap.APPS[0], dry_run=False,
    )

    assert calls == [("imagestudio-mac", "unservice.js", False)]
    assert not marker.exists()


def test_restore_uses_ssd_bundled_model_tool(tmp_path, monkeypatch):
    bootstrap = load_tool("fleet_bootstrap.py")
    commands = []
    monkeypatch.setattr(
        bootstrap, "run",
        lambda command, **_kwargs: commands.append(command),
    )
    home = tmp_path / "pinokio"
    bootstrap.restore_models(home, tmp_path / "studio-models", dry_run=True)

    expected = Path(bootstrap.__file__).resolve().with_name("studio_models.py")
    assert commands[0][1] == str(expected)
    assert commands[0][-2:] == ["--pinokio-home", str(home)]


def test_offline_restore_copies_without_contacting_studio(tmp_path, monkeypatch):
    models = load_tool("studio_models.py")
    root = tmp_path / "studio-models"
    source = root / "voice" / "Family" / "models--owner--voice"
    (source / "blobs").mkdir(parents=True)
    (source / "blobs/model.bin").write_bytes(b"model weights")
    (root / "MANIFEST.json").write_text(json.dumps({
        "studios": {
            "voice": {"packages": [{
                "repo": "owner/voice", "dir": source.name, "family": "Family",
                "floor_gb": 8, "bytes": models.dir_bytes(source),
            }]},
            "image": {"packages": []},
        },
    }))
    home = tmp_path / "Pinokio Home"
    studio = home / "api/voicestudio-mac"
    studio.mkdir(parents=True)
    (studio / "ENVIRONMENT").write_text("HF_HOME=./cache/HF_HOME\n")
    monkeypatch.setattr(models, "machine_memory_gb", lambda: 16.0)
    monkeypatch.setattr(
        models, "discover",
        lambda _port: (_ for _ in ()).throw(AssertionError("server discovery used")),
    )

    models.do_restore(
        root, plan_only=False, prune=False, restore_all=False, force=False,
        include_unqualified=False, keep_non_cloning=False, pinokio_home=home,
    )

    copied = studio / "cache/HF_HOME/hub" / source.name / "blobs/model.bin"
    assert copied.read_bytes() == b"model weights"


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
    assert '$SCRIPT_DIR/logs' in wrapper
    assert '$HOME/.pinokio' not in wrapper
    assert '[[ "$argument" == "--models-only" ]]' in wrapper


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

    assert legacy_log.parent.is_dir()
    assert not legacy_log.exists()
    assert legacy_log.parent.stat().st_mode & 0o7777 == 0o1777
    assert not (kit / "Install TerraNash Studios.command").exists()
    assert (kit / ".terranash-bootstrap.command").stat().st_mode & 0o555 == 0o555
    assert (kit / "1 Install Pinokio and Studios.command").is_file()
    assert (kit / "2 Copy Models to This Mac.command").is_file()
    assert (kit / "studio_models.py").is_file()
    assert (tmp_path / "READ-ME-FIRST.md").stat().st_mode & 0o444 == 0o444


def test_ssd_guide_separates_machine_paths_and_stays_short():
    guide = (ROOT / "SSD-COPY-README.md").read_text()
    for heading in (
        "## NEW MACHINE — two clicks",
        "## EXISTING MACHINE — models are missing",
        "## JOIN CONTROLLER — later",
        "## REPAIR",
        "## SSD MAINTAINER — main Mac only",
    ):
        assert heading in guide
    assert "terranash-bootstrap/logs" in guide
    assert ".shutdownStall" in guide
    assert len(guide.splitlines()) < 90


def test_bootstrap_requires_one_split_mode():
    bootstrap = load_tool("fleet_bootstrap.py")
    assert bootstrap.parser().parse_args(["--apps-only"]).apps_only
    assert bootstrap.parser().parse_args(["--models-only"]).models_only


def test_git_url_comparison_ignores_git_suffix():
    bootstrap = load_tool("fleet_bootstrap.py")
    assert bootstrap.normalize_git_url("https://github.com/theng12/studiohub-mac.git") == (
        bootstrap.normalize_git_url("https://github.com/theng12/studiohub-mac/")
    )
