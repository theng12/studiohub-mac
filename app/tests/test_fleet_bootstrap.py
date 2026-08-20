import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def load_tool(name: str):
    path = ROOT / "tools" / name
    spec = importlib.util.spec_from_file_location(name.removesuffix(".py"), path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_bootstrap_autolaunch_is_independent_for_every_studio(tmp_path):
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
    assert "PINOKIO_SCRIPT_AUTOLAUNCH_ENABLED=true" in image
    assert "PINOKIO_SCRIPT_AUTOLAUNCH_ENABLED=true" in voice
    assert "PINOKIO_SCRIPT_AUTOLAUNCH_ENABLED=true" in hub
    assert "PINOKIO_SCRIPT_REQUIRES=" in hub
    assert "imagestudio-mac,voicestudio-mac" not in hub
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


def test_8gb_offline_restore_copies_only_qwen_base_and_required_whisper(
    tmp_path, monkeypatch
):
    models = load_tool("studio_models.py")
    root = tmp_path / "studio-models"
    packages = [
        ("mlx-community/Qwen3-TTS-12Hz-0.6B-Base-8bit", 16),
        ("mlx-community/whisper-large-v3-turbo", None),
        ("mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-8bit", 8),
        ("mlx-community/OmniVoice-bfloat16", 8),
    ]
    entries = []
    for repo, floor in packages:
        dirname = "models--" + repo.replace("/", "--")
        source = root / "voice" / "Voice" / dirname
        source.mkdir(parents=True)
        (source / "weights.bin").write_bytes(repo.encode())
        entries.append({
            "repo": repo,
            "dir": dirname,
            "family": "Voice",
            "floor_gb": floor,
            "bytes": models.dir_bytes(source),
        })
    (root / "MANIFEST.json").write_text(json.dumps({
        "studios": {
            "voice": {"packages": entries},
            "image": {"packages": []},
        },
        "voices": [],
    }))
    home = tmp_path / "pinokio"
    studio = home / "api/voicestudio-mac"
    studio.mkdir(parents=True)
    (studio / "ENVIRONMENT").write_text("HF_HOME=./cache/HF_HOME\n")
    monkeypatch.setattr(models, "machine_memory_gb", lambda: 8.6)

    models.do_restore(
        root, plan_only=False, prune=False, restore_all=False, force=False,
        include_unqualified=False, keep_non_cloning=False, pinokio_home=home,
    )

    hub = studio / "cache/HF_HOME/hub"
    assert (hub / "models--mlx-community--Qwen3-TTS-12Hz-0.6B-Base-8bit").is_dir()
    assert (hub / "models--mlx-community--whisper-large-v3-turbo").is_dir()
    assert not (hub / "models--mlx-community--Qwen3-TTS-12Hz-0.6B-CustomVoice-8bit").exists()
    assert not (hub / "models--mlx-community--OmniVoice-bfloat16").exists()


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


def test_restaging_preserves_legacy_ssd_logs(tmp_path, monkeypatch):
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
    assert legacy_log.read_text() == "PINOKIO_HOME=/Users/old-account/pinokio"
    assert legacy_log.parent.stat().st_mode & 0o7777 == 0o1777
    assert not (kit / "Install TerraNash Studios.command").exists()
    assert (kit / ".terranash-bootstrap.command").stat().st_mode & 0o555 == 0o555
    assert (kit / "1 Install Pinokio and Studios.command").is_file()
    assert (kit / "2 Copy Models to This Mac.command").is_file()
    assert (kit / "4 Repair Studio Startup.command").is_file()
    assert (kit / "repair_startup.py").is_file()
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


def test_bootstrap_updates_matching_legacy_git_checkout(tmp_path, monkeypatch):
    bootstrap = load_tool("fleet_bootstrap.py")
    home = tmp_path / "pinokio"
    legacy = home / "api/voicestudio-mac.git"
    (legacy / ".git").mkdir(parents=True)
    (legacy / ".git/config").write_text(
        '[remote "origin"]\n\turl = https://github.com/theng12/voicestudio-mac.git\n'
    )
    commands = []
    monkeypatch.setattr(bootstrap, "run", lambda command, **kwargs: commands.append(command))
    monkeypatch.setattr(
        bootstrap,
        "git_checkout_state",
        lambda _target: ("main", "origin/main", ""),
        raising=False,
    )

    target = bootstrap.ensure_repo(
        Path("/pinokio/pterm"), home, bootstrap.APPS[1], dry_run=False
    )

    assert target == legacy
    assert commands == [["git", "-C", str(legacy), "pull", "--ff-only"]]


@pytest.mark.parametrize(
    ("checkout_state", "message"),
    [
        (("feature/experiment", "origin/feature/experiment", ""), "must be on main"),
        (("main", "fork/main", ""), "must track origin/main"),
        (("main", "origin/main", " M README.md"), "has local changes"),
    ],
)
def test_bootstrap_refuses_to_update_an_unsafe_matching_checkout(
    tmp_path, monkeypatch, checkout_state, message
):
    bootstrap = load_tool("fleet_bootstrap.py")
    home = tmp_path / "pinokio"
    target = home / "api/studiohub-mac"
    (target / ".git").mkdir(parents=True)
    (target / ".git/config").write_text(
        '[remote "origin"]\n\turl = https://github.com/theng12/studiohub-mac.git\n'
    )
    commands = []
    monkeypatch.setattr(bootstrap, "run", lambda command, **kwargs: commands.append(command))
    monkeypatch.setattr(
        bootstrap,
        "git_checkout_state",
        lambda _target: checkout_state,
        raising=False,
    )

    with pytest.raises(bootstrap.BootstrapError, match=message):
        bootstrap.ensure_repo(
            Path("/pinokio/pterm"), home, bootstrap.APPS[2], dry_run=False,
        )

    assert commands == []


def test_legacy_checkout_name_is_used_for_scripts_and_startup_graph(
    tmp_path, monkeypatch
):
    bootstrap = load_tool("fleet_bootstrap.py")
    target = tmp_path / "voicestudio-mac.git"
    target.mkdir()
    calls = []
    monkeypatch.setattr(
        bootstrap,
        "install_script",
        lambda _pterm, name, script, **kwargs: calls.append((name, script)),
    )
    monkeypatch.setattr(bootstrap, "python_imports", lambda *_args: False)

    bootstrap.ensure_dependencies(
        Path("/pinokio/pterm"), target, bootstrap.APPS[1], dry_run=True
    )

    assert calls == [
        ("voicestudio-mac.git", "install.js"),
        ("voicestudio-mac.git", "install_generation.js"),
    ]

    targets = {
        "imagestudio-mac": tmp_path / "imagestudio-mac.git",
        "voicestudio-mac": target,
        "studiohub-mac": tmp_path / "studiohub-mac",
    }
    for path in targets.values():
        path.mkdir(exist_ok=True)
    bootstrap.configure_autolaunch(targets, dry_run=False)

    hub_environment = (targets["studiohub-mac"] / "ENVIRONMENT").read_text()
    assert "PINOKIO_SCRIPT_REQUIRES=" in hub_environment
    assert "imagestudio-mac.git,voicestudio-mac.git" not in hub_environment


def test_stage_includes_every_cached_local_catalog_model(tmp_path, monkeypatch, capsys):
    models = load_tool("studio_models.py")
    hub = tmp_path / "hub"
    package = hub / "models--owner--internal-candidate"
    package.mkdir(parents=True)
    (package / "weights.bin").write_bytes(b"candidate")
    catalog = [{
        "repo": "owner/internal-candidate",
        "family": "future-family",
        "family_label": "Future family",
        "provider": "local",
        "min_unified_memory_gb": 16,
        "cache": {"state": "cached"},
    }]
    monkeypatch.setattr(
        models,
        "discover",
        lambda port: (hub, catalog) if port == models.STUDIOS["voice"]["port"] else (None, []),
    )
    monkeypatch.setattr(models, "discover_fleet_voices", lambda: [])
    monkeypatch.setattr(models, "stage_bootstrap_kit", lambda *_args, **_kwargs: None)

    models.do_stage(tmp_path / "ssd", plan_only=True, keep_non_cloning=False)

    assert "Voice Studio: 1 packages" in capsys.readouterr().out


def test_stage_excludes_a_catalog_package_that_is_still_partial(
    tmp_path, monkeypatch, capsys
):
    models = load_tool("studio_models.py")
    hub = tmp_path / "hub"
    package = hub / "models--owner--partial"
    package.mkdir(parents=True)
    (package / "weights.bin.incomplete").write_bytes(b"unfinished")
    catalog = [{
        "repo": "owner/partial",
        "family": "future-family",
        "family_label": "Future family",
        "provider": "local",
        "cache": {"state": "partial"},
    }]
    monkeypatch.setattr(
        models,
        "discover",
        lambda port: (hub, catalog)
        if port == models.STUDIOS["voice"]["port"]
        else (None, []),
    )
    monkeypatch.setattr(models, "discover_fleet_voices", lambda: [])
    monkeypatch.setattr(models, "stage_bootstrap_kit", lambda *_args, **_kwargs: None)

    models.do_stage(tmp_path / "ssd", plan_only=True, keep_non_cloning=False)

    assert "Voice Studio: 0 packages" in capsys.readouterr().out


def test_incremental_package_copy_skips_identical_and_replaces_changed(tmp_path):
    models = load_tool("studio_models.py")
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    (source / "blobs").mkdir(parents=True)
    (source / "blobs/model.bin").write_bytes(b"first")

    assert models.copy_package_if_changed(source, destination) == "copy"
    original_inode = (destination / "blobs/model.bin").stat().st_ino
    assert models.copy_package_if_changed(source, destination) == "intact"
    assert (destination / "blobs/model.bin").stat().st_ino == original_inode

    (source / "blobs/model.bin").write_bytes(b"second payload")
    assert models.copy_package_if_changed(source, destination) == "replace"
    assert (destination / "blobs/model.bin").read_bytes() == b"second payload"


def test_package_copy_omits_stale_zero_byte_incomplete_placeholders(tmp_path):
    models = load_tool("studio_models.py")
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    (source / "blobs").mkdir(parents=True)
    (source / "blobs/model.bin").write_bytes(b"complete")
    (source / "blobs/model.bin.old.incomplete").write_bytes(b"")

    assert models.copy_package_if_changed(source, destination) == "copy"
    assert not list(destination.rglob("*.incomplete"))
    assert models.copy_package_if_changed(source, destination) == "intact"


def test_voice_restore_is_idempotent_and_refuses_same_id_with_other_audio(tmp_path):
    models = load_tool("studio_models.py")
    staged = tmp_path / "staged"
    destination = tmp_path / "voices"
    voice_id = "stable-voice"
    audio = b"owner voice bytes"
    digest = models.bytes_sha256(audio)
    source = staged / voice_id
    source.mkdir(parents=True)
    (source / "reference.wav").write_bytes(audio)
    (source / "metadata.json").write_text(json.dumps({
        "id": voice_id,
        "fleet_managed": True,
        "audio_extension": ".wav",
        "audio_sha256": digest,
    }))
    entry = {"id": voice_id, "dir": voice_id, "audio_sha256": digest}

    assert models.restore_voice(source, destination, entry) == "copy"
    assert models.restore_voice(source, destination, entry) == "intact"

    (destination / voice_id / "reference.wav").write_bytes(b"local replacement")
    assert models.restore_voice(source, destination, entry) == "conflict"
    assert (destination / voice_id / "reference.wav").read_bytes() == b"local replacement"


def test_voice_restore_rejects_unsafe_manifest_identity(tmp_path):
    models = load_tool("studio_models.py")
    source = tmp_path / "source"
    source.mkdir()
    (source / "metadata.json").write_text("{}")

    try:
        models.restore_voice(
            source,
            tmp_path / "voices",
            {"id": "../../escape", "dir": "../../escape", "audio_sha256": "a" * 64},
        )
    except ValueError as exc:
        assert "safe" in str(exc)
    else:
        raise AssertionError("unsafe voice ID was accepted")

    assert not (tmp_path / "escape").exists()
