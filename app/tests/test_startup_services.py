import asyncio
import contextlib
import fcntl
import os
import plistlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from backend import broker, control, control_plane, fleet_ops, peers, registry, startup_services
from backend import main


@pytest.fixture
def owner(app):
    return TestClient(app, client=("127.0.0.1", 50000))


@pytest.fixture(autouse=True)
def isolated_retirement_lock(monkeypatch, request):
    if request.node.name in {
        "test_startup_retirement_lock_refuses_a_running_updater",
        "test_removal_lock_fences_legacy_suffix_duplicate_updater",
        "test_removal_lock_refuses_symlinked_checkout_before_writing_state",
    }:
        yield
        return

    @contextlib.contextmanager
    def unlocked(modality):
        yield

    monkeypatch.setattr(startup_services, "retirement_lock", unlocked, raising=False)
    yield


def _seed_app(tmp_path: Path, monkeypatch, modality: str = "image") -> tuple[Path, Path]:
    monkeypatch.setattr(control, "PINOKIO_HOME", tmp_path)
    launch_agents = tmp_path / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    monkeypatch.setattr(startup_services, "_launch_agents_dir", lambda: launch_agents)
    spec = startup_services.SERVICE_SPECS[modality]
    app_dir = tmp_path / "api" / spec["app"]
    app_dir.mkdir(parents=True)
    installer = app_dir / "install_service.sh"
    installer.write_text("#!/bin/bash\n")
    uninstaller = app_dir / "uninstall_service.sh"
    uninstaller.write_text("#!/bin/bash\n")
    runtime = app_dir / "conda_env" / "bin"
    runtime.mkdir(parents=True)
    (runtime / "python").touch()
    return app_dir, launch_agents


def _mark_installed(app_dir: Path, launch_agents: Path, modality: str = "image") -> None:
    spec = startup_services.SERVICE_SPECS[modality]
    (app_dir / "service").mkdir(exist_ok=True)
    (app_dir / "service" / ".installed").touch()
    (launch_agents / f"{spec['server_label']}.plist").touch()
    (launch_agents / f"{spec['watchdog_label']}.plist").touch()


def _add_legacy_compat_target(modality: str) -> None:
    """Seed a retirable (legacy) Studio row that `load_registry()` no longer emits.

    `registry.TRACKED_MODALITIES` is ("image", "voice") while
    `startup_services.RETIRABLE_MODALITIES` is the four legacy families, so the
    two sets are disjoint: production never registers a legacy row any more.

    The *retire* endpoints still look their target up in `monitor.registry` and
    answer 404 without one, so those cases must seed the row themselves. The
    *full-remove* endpoint no longer does: a still-installed legacy checkout is
    removed on the strength of the folder on disk, keying its removal flags off
    the modality when no row exists. Removal tests should therefore leave the
    registry alone and exercise the real production shape.
    """
    main.monitor.registry.append({
        "id": modality, "modality": modality, "machine": "local",
        "host": "127.0.0.1", "port": startup_services.SERVICE_SPECS[modality]["port"],
    })


def test_startup_audit_distinguishes_missing_repair_and_installed(tmp_path, monkeypatch):
    app_dir, launch_agents = _seed_app(tmp_path, monkeypatch)
    loaded = set()
    monkeypatch.setattr(startup_services, "_launchd_loaded", lambda label: label in loaded)

    missing = startup_services.inspect_service("image")
    assert missing["status"] == "not_installed" and missing["can_install"] is True

    (app_dir / "service").mkdir()
    (app_dir / "service" / ".installed").touch()
    repair = startup_services.inspect_service("image")
    assert repair["status"] == "repair_needed" and repair["installed"] is False

    _mark_installed(app_dir, launch_agents)
    spec = startup_services.SERVICE_SPECS["image"]
    loaded.update({spec["server_label"], spec["watchdog_label"]})
    installed = startup_services.inspect_service("image")
    assert installed["status"] == "installed" and installed["installed"] is True
    assert installed["can_install"] is False


def test_startup_installer_refuses_symlink(tmp_path, monkeypatch):
    app_dir, _ = _seed_app(tmp_path, monkeypatch)
    installer = app_dir / "install_service.sh"
    installer.unlink()
    target = tmp_path / "unsafe.sh"
    target.write_text("#!/bin/bash\n")
    installer.symlink_to(target)

    row = startup_services.inspect_service("image")
    assert row["supported"] is False and row["can_install"] is False


def test_startup_install_runs_trusted_script_and_verifies_launchd(tmp_path, monkeypatch):
    app_dir, launch_agents = _seed_app(tmp_path, monkeypatch)
    loaded = set()
    spec = startup_services.SERVICE_SPECS["image"]

    def fake_run(command, **kwargs):
        if command[0] == "/bin/launchctl":
            label = command[-1].rsplit("/", 1)[-1]
            return SimpleNamespace(returncode=0 if label in loaded else 1)
        assert command[0] == "/bin/bash"
        assert Path(command[1]) == (app_dir / "install_service.sh").resolve()
        _mark_installed(app_dir, launch_agents)
        loaded.update({spec["server_label"], spec["watchdog_label"]})
        return SimpleNamespace(returncode=0, stdout="installed", stderr="")

    monkeypatch.setattr(startup_services.subprocess, "run", fake_run)
    result = startup_services.install_service("image")
    assert result["ok"] is True and result["changed"] is True
    assert result["service"]["installed"] is True


def test_voice_restart_runs_only_the_trusted_installed_service(tmp_path, monkeypatch):
    app_dir, launch_agents = _seed_app(tmp_path, monkeypatch, "voice")
    _mark_installed(app_dir, launch_agents, "voice")
    restart = app_dir / "restart_service.sh"
    restart.write_text("#!/bin/bash\n")
    spec = startup_services.SERVICE_SPECS["voice"]
    loaded = {spec["server_label"], spec["watchdog_label"]}

    def fake_run(command, **kwargs):
        if command[0] == "/bin/launchctl":
            return SimpleNamespace(returncode=0 if command[-1].rsplit("/", 1)[-1] in loaded else 1)
        assert command == ["/bin/bash", str(restart.resolve())]
        assert kwargs["cwd"] == app_dir
        return SimpleNamespace(returncode=0, stdout="restart requested", stderr="")

    monkeypatch.setattr(startup_services.subprocess, "run", fake_run)
    monkeypatch.setattr(startup_services, "verified_voice_service", lambda: {"installed": True})

    result = startup_services.restart_voice_service()

    assert result["ok"] is True and result["changed"] is True
    assert result["service"]["installed"] is True


@pytest.mark.asyncio
async def test_voice_recovery_drains_exact_job_without_restarting_service(reset, monkeypatch):
    submitted = broker.submit_batch({
        "modality": "voice", "model": "local/voice", "items": [{"text": "recover"}],
    })
    batch = broker.batches[submitted["batch_id"]]
    item = batch["items"][0]
    item.update(state="uncertain", studio="voice", studio_job_id="voice-job-1")
    main.monitor.registry = [{
        "id": "voice", "modality": "voice", "machine": "local",
        "host": "127.0.0.1", "port": 47870,
    }]
    events = []

    async def reconciliation(*_args, **_kwargs):
        events.append("reconcile")
        return "active"

    async def signal(_client, target):
        events.append(("cancel", target["studio_job_id"]))
        return True

    async def no_delay(_seconds):
        return None

    monkeypatch.setattr(main, "VOICE_RECOVERY_GRACE_S", 0)
    monkeypatch.setattr(main, "_reconcile_voice_recovery_job", reconciliation)
    monkeypatch.setattr(broker, "_signal_worker_cancel", signal)
    monkeypatch.setattr(main.asyncio, "sleep", no_delay)
    monkeypatch.setattr(startup_services, "restart_voice_service", lambda: events.append("restart"))

    result = await main._recover_voice_item(batch, item, main.monitor.registry[0], False, object())

    assert result["forced"] is False
    assert events == ["reconcile", ("cancel", "voice-job-1"), "reconcile"]
    assert item["state"] == "uncertain"
    assert item["recovery"]["phase"] == "manual_action_required"
    assert broker.in_maintenance("voice") is False


@pytest.mark.asyncio
async def test_forced_voice_recovery_restarts_then_adopts_only_original_final_job(reset, monkeypatch):
    submitted = broker.submit_batch({
        "modality": "voice", "model": "local/voice", "items": [{"text": "recover"}],
    })
    batch = broker.batches[submitted["batch_id"]]
    item = batch["items"][0]
    item.update(state="uncertain", studio="voice", studio_job_id="voice-job-1")
    studio = {"id": "voice", "modality": "voice", "machine": "local",
              "host": "127.0.0.1", "port": 47870}
    events = []
    states = iter(["active", "active", "done"])

    async def reconciliation(*_args, **_kwargs):
        value = next(states)
        events.append(("reconcile", value))
        return value

    async def signal(_client, _target):
        events.append("cancel")
        return True

    async def no_other(_client, _studio, job_id):
        events.append(("other-active", job_id))
        return False

    async def healthy(_client, _studio):
        events.append("health")
        return True

    async def run_here(fn):
        return fn()

    monkeypatch.setattr(main, "VOICE_RECOVERY_GRACE_S", 0)
    monkeypatch.setattr(main, "_managed_local_voice_service", lambda _studio: {"installed": True})
    monkeypatch.setattr(main, "_reconcile_voice_recovery_job", reconciliation)
    monkeypatch.setattr(broker, "_signal_worker_cancel", signal)
    monkeypatch.setattr(main, "_other_active_voice_jobs", no_other)
    monkeypatch.setattr(main, "_wait_voice_recovery_health", healthy)
    monkeypatch.setattr(main.asyncio, "to_thread", run_here)
    monkeypatch.setattr(startup_services, "restart_voice_service", lambda: events.append("restart"))

    result = await main._recover_voice_item(batch, item, studio, True, object())

    assert result["ok"] is True and result["forced"] is True and result["state"] == "done"
    assert events == [
        ("reconcile", "active"), "cancel", ("reconcile", "active"),
        ("other-active", "voice-job-1"), "restart", "health", ("reconcile", "done"),
    ]
    assert broker.in_maintenance("voice") is False


@pytest.mark.asyncio
async def test_forced_voice_recovery_keeps_uncertain_when_health_times_out(reset, monkeypatch):
    submitted = broker.submit_batch({
        "modality": "voice", "model": "local/voice", "items": [{"text": "recover"}],
    })
    batch = broker.batches[submitted["batch_id"]]
    item = batch["items"][0]
    item.update(state="uncertain", studio="voice", studio_job_id="voice-job-1")
    studio = {"id": "voice", "modality": "voice", "machine": "local",
              "host": "127.0.0.1", "port": 47870}

    async def active(*_args, **_kwargs):
        return "active"

    async def signal(*_args, **_kwargs):
        return True

    async def no_other(*_args, **_kwargs):
        return False

    async def unhealthy(*_args, **_kwargs):
        return False

    async def run_here(fn):
        return fn()

    monkeypatch.setattr(main, "VOICE_RECOVERY_GRACE_S", 0)
    monkeypatch.setattr(main, "_managed_local_voice_service", lambda _studio: {"installed": True})
    monkeypatch.setattr(main, "_reconcile_voice_recovery_job", active)
    monkeypatch.setattr(broker, "_signal_worker_cancel", signal)
    monkeypatch.setattr(main, "_other_active_voice_jobs", no_other)
    monkeypatch.setattr(main, "_wait_voice_recovery_health", unhealthy)
    monkeypatch.setattr(main.asyncio, "to_thread", run_here)
    monkeypatch.setattr(startup_services, "restart_voice_service", lambda: None)

    result = await main._recover_voice_item(batch, item, studio, True, object())

    assert result["ok"] is False and result["forced"] is True
    assert item["state"] == "uncertain"
    assert item["recovery"]["phase"] == "manual_action_required"
    assert "did not become healthy" in item["recovery"]["reason"]
    assert broker.in_maintenance("voice") is True
    broker.set_maintenance("voice", False)


@pytest.mark.asyncio
async def test_forced_voice_recovery_refuses_to_interrupt_another_active_voice_job(reset, monkeypatch):
    submitted = broker.submit_batch({
        "modality": "voice", "model": "local/voice", "items": [{"text": "recover"}],
    })
    batch = broker.batches[submitted["batch_id"]]
    item = batch["items"][0]
    item.update(state="uncertain", studio="voice", studio_job_id="voice-job-1")
    studio = {"id": "voice", "modality": "voice", "machine": "local",
              "host": "127.0.0.1", "port": 47870}
    restarted = []

    async def active(*_args, **_kwargs):
        return "active"

    async def signal(*_args, **_kwargs):
        return True

    async def another_job(*_args, **_kwargs):
        return True

    monkeypatch.setattr(main, "VOICE_RECOVERY_GRACE_S", 0)
    monkeypatch.setattr(main, "_managed_local_voice_service", lambda _studio: {"installed": True})
    monkeypatch.setattr(main, "_reconcile_voice_recovery_job", active)
    monkeypatch.setattr(broker, "_signal_worker_cancel", signal)
    monkeypatch.setattr(main, "_other_active_voice_jobs", another_job)
    monkeypatch.setattr(startup_services, "restart_voice_service", lambda: restarted.append(True))

    result = await main._recover_voice_item(batch, item, studio, True, object())

    assert result["ok"] is False and result["forced"] is True
    assert restarted == []
    assert item["recovery"]["phase"] == "manual_action_required"
    assert "another active job" in item["recovery"]["reason"]


def test_startup_uninstall_runs_trusted_script_and_verifies_service_is_gone(
    tmp_path, monkeypatch,
):
    app_dir, launch_agents = _seed_app(tmp_path, monkeypatch, "music")
    _mark_installed(app_dir, launch_agents, "music")
    spec = startup_services.SERVICE_SPECS["music"]
    loaded = {spec["server_label"], spec["watchdog_label"]}
    preserved = {}
    for relative in ("models/model.bin", "cache/item.json", "outputs/result.txt",
                     "settings.json"):
        path = app_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative)
        preserved[path] = path.read_bytes()

    def fake_run(command, **kwargs):
        assert command == ["/bin/bash", str((app_dir / "uninstall_service.sh").resolve())]
        loaded.clear()
        (app_dir / "service" / ".installed").unlink()
        (launch_agents / f"{spec['server_label']}.plist").unlink()
        (launch_agents / f"{spec['watchdog_label']}.plist").unlink()
        return SimpleNamespace(returncode=0, stdout="removed", stderr="")

    monkeypatch.setattr(startup_services, "_launchd_loaded", lambda label: label in loaded)
    monkeypatch.setattr(startup_services.subprocess, "run", fake_run)

    result = startup_services.uninstall_service("music")

    assert result["ok"] is True and result["changed"] is True
    assert result["service"]["status"] == "not_installed"
    assert result["service"]["installed"] is False
    assert app_dir.is_dir()
    assert all(path.read_bytes() == content for path, content in preserved.items())


def test_startup_uninstall_refuses_symlinked_script(tmp_path, monkeypatch):
    app_dir, _ = _seed_app(tmp_path, monkeypatch, "music")
    (app_dir / "uninstall_service.sh").unlink()
    unsafe = tmp_path / "unsafe-uninstall.sh"
    unsafe.write_text("#!/bin/bash\n")
    (app_dir / "uninstall_service.sh").symlink_to(unsafe)

    with pytest.raises(ValueError, match="trusted startup uninstaller"):
        startup_services.uninstall_service("music")


def test_huggingface_hub_cache_override_is_the_hub_directory(tmp_path):
    app_dir = tmp_path / "musicstudio-mac"
    app_dir.mkdir()
    (app_dir / "ENVIRONMENT").write_text(
        "HUGGINGFACE_HUB_CACHE=./cache/huggingface-hub\n"
    )

    assert startup_services._hf_hub_dir(app_dir) == (
        app_dir / "cache" / "huggingface-hub"
    )


def test_huggingface_hub_cache_override_wins_when_hf_home_is_also_set(tmp_path):
    app_dir = tmp_path / "musicstudio-mac"
    app_dir.mkdir()
    (app_dir / "ENVIRONMENT").write_text(
        "HF_HOME=./cache/HF_HOME\n"
        "HUGGINGFACE_HUB_CACHE=./cache/explicit-hub\n"
    )

    assert startup_services._hf_hub_dir(app_dir) == (
        app_dir / "cache" / "explicit-hub"
    )


def test_full_remove_cleans_every_launchagent_and_trashes_suffix_duplicates(
    tmp_path, monkeypatch,
):
    app_dir, launch_agents = _seed_app(tmp_path, monkeypatch, "music")
    duplicate = app_dir.with_name(f"{app_dir.name}.git")
    duplicate.mkdir()
    for target in (app_dir, duplicate):
        (target / "ENVIRONMENT").write_text(
            "PINOKIO_SCRIPT_AUTOLAUNCH_ENABLED=true\n"
            "PINOKIO_SCRIPT_REQUIRES=imagestudio-mac,voicestudio-mac\n"
            "HF_HOME=./cache/HF_HOME\n"
        )
        (target / "models").mkdir()
        (target / "models" / "weight.bin").write_bytes(b"model")
    music_catalog = app_dir / "app" / "backend" / "catalog.py"
    music_catalog.parent.mkdir(parents=True)
    music_catalog.write_text(
        'ModelEntry(repo="owner/music-exclusive")\n'
        'ModelEntry(repo="owner/shared-safe")\n'
    )
    protected_catalog = (
        tmp_path / "api" / "imagestudio-mac" / "app" / "backend" / "catalog.py"
    )
    protected_catalog.parent.mkdir(parents=True)
    protected_catalog.write_text('ModelEntry(repo="owner/shared-safe")\n')
    hf_hub = app_dir / "cache" / "HF_HOME" / "hub"
    exclusive_cache = hf_hub / "models--owner--music-exclusive"
    shared_cache = hf_hub / "models--owner--shared-safe"
    exclusive_cache.mkdir(parents=True)
    shared_cache.mkdir()
    (exclusive_cache / "weights.bin").write_bytes(b"exclusive")
    (shared_cache / "weights.bin").write_bytes(b"shared")
    _mark_installed(app_dir, launch_agents, "music")
    spec = startup_services.SERVICE_SPECS["music"]
    updater_plist = launch_agents / f"{spec['updater_label']}.plist"
    updater_plist.touch()
    loaded = {
        spec["server_label"], spec["watchdog_label"], spec["updater_label"],
    }
    stopped = []

    def fake_run(command, **kwargs):
        assert command[0] == "/bin/launchctl"
        if command[1] == "print":
            label = command[-1].rsplit("/", 1)[-1]
            return SimpleNamespace(returncode=0 if label in loaded else 1)
        assert command[1] == "bootout"
        loaded.discard(command[-1].rsplit("/", 1)[-1])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(startup_services.subprocess, "run", fake_run)
    monkeypatch.setattr(
        control, "stop_studio_sync",
        lambda studio: stopped.append(studio["app"]) or {"ok": True},
        raising=False,
    )
    trash = tmp_path / "Trash"
    monkeypatch.setattr(startup_services, "_trash_dir", lambda: trash, raising=False)

    with startup_services.retirement_lock("music"):
        result = startup_services.fully_remove_studio("music")

    assert result["ok"] is True
    assert result["removed"] is True
    assert {Path(value).name.split("-removed-", 1)[0]
            for value in result["trashed_checkouts"]} == {
        "musicstudio-mac", "musicstudio-mac.git",
    }
    assert stopped == ["musicstudio-mac", "musicstudio-mac.git"]
    assert not app_dir.exists() and not duplicate.exists()
    assert not exclusive_cache.exists()
    retired_primary = next(
        Path(value) for value in result["trashed_checkouts"]
        if Path(value).name.split("-removed-", 1)[0] == "musicstudio-mac"
    )
    assert (
        retired_primary / "cache" / "HF_HOME" / "hub"
        / "models--owner--shared-safe"
    ).is_dir()
    assert any("models--owner--music-exclusive" in Path(value).name
               for value in result["trashed"])
    assert trash.stat().st_mode & 0o777 == 0o700
    assert loaded == set()
    for label in (spec["server_label"], spec["watchdog_label"], spec["updater_label"]):
        assert not (launch_agents / f"{label}.plist").exists()
    for value in result["trashed_checkouts"]:
        retired = Path(value)
        assert (retired / "models" / "weight.bin").read_bytes() == b"model"
        environment = (retired / "ENVIRONMENT").read_text()
        assert "PINOKIO_SCRIPT_AUTOLAUNCH_ENABLED=false" in environment
        assert "PINOKIO_SCRIPT_REQUIRES=" in environment
        assert "imagestudio-mac,voicestudio-mac" not in environment


def test_full_remove_succeeds_when_pinokio_kernel_is_absent_and_no_listener_remains(
    tmp_path, monkeypatch,
):
    app_dir, _ = _seed_app(tmp_path, monkeypatch, "music")
    (app_dir / "ENVIRONMENT").write_text("")
    removed_agents = []
    monkeypatch.setattr(
        control, "stop_studio_sync",
        lambda studio: {"ok": False, "error": "connect ECONNREFUSED 127.0.0.1:42000"},
    )
    monkeypatch.setattr(
        startup_services, "_remove_launch_agent", removed_agents.append,
    )
    monkeypatch.setattr(startup_services, "_wait_port_closed", lambda _port: True)
    trash = tmp_path / "Trash"
    monkeypatch.setattr(startup_services, "_trash_dir", lambda: trash)

    result = startup_services.fully_remove_studio("music")

    assert result["removed"] is True
    assert not app_dir.exists()
    assert len(removed_agents) == 3


def test_full_remove_refuses_unmanaged_listener_after_managed_services_are_unloaded(
    tmp_path, monkeypatch,
):
    app_dir, _ = _seed_app(tmp_path, monkeypatch, "music")
    (app_dir / "ENVIRONMENT").write_text("")
    monkeypatch.setattr(
        control, "stop_studio_sync",
        lambda studio: {"ok": False, "error": "connect ECONNREFUSED 127.0.0.1:42000"},
    )
    monkeypatch.setattr(startup_services, "_remove_launch_agent", lambda _label: None)
    monkeypatch.setattr(startup_services, "_wait_port_closed", lambda _port: False)

    with pytest.raises(ValueError, match="still running outside managed services"):
        startup_services.fully_remove_studio("music")

    assert app_dir.is_dir()


def test_absent_checkout_retry_removes_residual_agents_before_completing(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(control, "PINOKIO_HOME", tmp_path)
    launch_agents = tmp_path / "LaunchAgents"
    launch_agents.mkdir()
    monkeypatch.setattr(startup_services, "_launch_agents_dir", lambda: launch_agents)
    spec = startup_services.SERVICE_SPECS["music"]
    loaded = {spec["updater_label"], spec["server_label"]}
    for label in loaded:
        (launch_agents / f"{label}.plist").touch()

    def fake_run(command, **_kwargs):
        if command[1] == "bootout":
            loaded.discard(command[-1].rsplit("/", 1)[-1])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(startup_services.subprocess, "run", fake_run)
    monkeypatch.setattr(startup_services, "_launchd_loaded", lambda label: label in loaded)
    monkeypatch.setattr(startup_services, "_wait_port_closed", lambda _port: True)

    result = startup_services.finalize_absent_studio_removal("music")

    assert result["already_removed"] is True
    assert loaded == set()
    assert list(launch_agents.iterdir()) == []


def test_absent_checkout_retry_refuses_residual_unmanaged_listener(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(control, "PINOKIO_HOME", tmp_path)
    launch_agents = tmp_path / "LaunchAgents"
    launch_agents.mkdir()
    monkeypatch.setattr(startup_services, "_launch_agents_dir", lambda: launch_agents)
    monkeypatch.setattr(startup_services, "_remove_launch_agent", lambda _label: None)
    monkeypatch.setattr(startup_services, "_wait_port_closed", lambda _port: False)

    with pytest.raises(ValueError, match="still running outside managed services"):
        startup_services.finalize_absent_studio_removal("music")


def test_hub_startup_resumes_verified_cleanup_for_absent_intent(monkeypatch):
    events = []
    monkeypatch.setattr(
        startup_services, "has_removal_intent", lambda modality: modality == "music",
    )
    monkeypatch.setattr(startup_services, "is_fully_removed", lambda _modality: False)
    monkeypatch.setattr(
        startup_services, "finalize_absent_studio_removal",
        lambda modality: events.append(("finalize", modality)) or {"ok": True},
    )
    monkeypatch.setattr(
        registry, "set_studio_removal_complete",
        lambda machine, studio_id, complete: events.append(
            ("complete", machine, studio_id, complete)
        ),
    )

    results = startup_services.reconcile_removal_intents()

    assert results == [{"modality": "music", "ok": True}]
    assert events == [
        ("finalize", "music"),
        ("complete", "local", "music", True),
    ]


def test_disable_autolaunch_never_follows_a_precreated_temporary_symlink(tmp_path):
    app_dir = tmp_path / "musicstudio-mac"
    app_dir.mkdir()
    environment = app_dir / "ENVIRONMENT"
    environment.write_text("PINOKIO_SCRIPT_AUTOLAUNCH_ENABLED=true\n")
    outside = tmp_path / "outside"
    outside.write_text("protected")
    temporary = environment.with_name(f".{environment.name}.{os.getpid()}.tmp")
    temporary.symlink_to(outside)

    with pytest.raises(FileExistsError):
        startup_services._disable_pinokio_autolaunch(app_dir)

    assert outside.read_text() == "protected"


@pytest.mark.parametrize("modality", ["image", "voice"])
def test_full_remove_never_targets_protected_studios(tmp_path, monkeypatch, modality):
    _seed_app(tmp_path, monkeypatch, modality)

    with pytest.raises(ValueError, match="Only Music, Chat, Video, and Render"):
        startup_services.fully_remove_studio(modality)


def test_startup_retirement_lock_refuses_a_running_updater(tmp_path, monkeypatch):
    app_dir, _ = _seed_app(tmp_path, monkeypatch, "music")
    lock_path = app_dir / "auto_update" / "update.lock"
    lock_path.parent.mkdir()
    with open(lock_path, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="update is already running"):
            with startup_services.retirement_lock("music"):
                pytest.fail("retirement acquired a live updater lock")


def test_removal_lock_fences_legacy_suffix_duplicate_updater(tmp_path, monkeypatch):
    app_dir, _ = _seed_app(tmp_path, monkeypatch, "music")
    duplicate = app_dir.with_name(f"{app_dir.name}.git")
    lock_path = duplicate / "auto_update" / "update.lock"
    lock_path.parent.mkdir(parents=True)
    with open(lock_path, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="update is already running"):
            with startup_services.retirement_lock("music"):
                pytest.fail("removal ignored the duplicate updater lock")


def test_removal_lock_refuses_symlinked_checkout_before_writing_state(
    tmp_path, monkeypatch,
):
    api_root = tmp_path / "api"
    api_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (api_root / "musicstudio-mac").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(control, "PINOKIO_HOME", tmp_path)

    with pytest.raises(ValueError, match="unsafe Studio checkout"):
        with startup_services.retirement_lock("music"):
            pass

    assert not (outside / "auto_update").exists()


def test_startup_audit_api_aggregates_peer_hubs(authed, monkeypatch):
    local = {"schema_version": 1, "machine": "local", "reachable": True,
             "supported": True, "services": [{"modality": "image", "installed": True}]}
    monkeypatch.setattr(startup_services, "local_snapshot", lambda: local)

    async def remote_status(registry, client):
        return {"mac-b": {"machine": "mac-b", "reachable": False,
                           "supported": False, "services": [], "detail": "offline"}}

    monkeypatch.setattr(peers, "startup_services_status", remote_status)
    response = authed.get("/api/hub/startup-services")
    assert response.status_code == 200
    assert set(response.json()["machines"]) == {"local", "mac-b"}
    assert authed.get("/api/hub/startup-services?local_only=true").json() == local


def test_startup_snapshot_ignores_installed_untracked_studio(tmp_path, monkeypatch):
    _seed_app(tmp_path, monkeypatch, "music")
    monkeypatch.setattr(startup_services, "_launchd_loaded", lambda label: False)
    monkeypatch.setattr(
        registry, "load_registry",
        lambda: [{"id": "music", "modality": "music", "machine": "local"}],
    )
    snapshot = startup_services.local_snapshot()

    assert {row["modality"] for row in snapshot["services"]} == {"image", "voice"}


def test_leftover_studios_lists_installed_legacy_checkouts_only(tmp_path, monkeypatch):
    """The dashboard's remove list: retired families still on disk, nothing else.

    Image and Voice must never appear — they cannot be removed by that button,
    and the endpoint behind it refuses them with a 400.
    """
    monkeypatch.setattr(control, "PINOKIO_HOME", tmp_path)
    monkeypatch.setattr(registry, "load_registry", lambda: [])
    monkeypatch.setattr(startup_services, "_launchd_loaded", lambda _label: False)
    for name in ("musicstudio-mac", "musicstudio-mac.git", "videostudio-mac",
                 "imagestudio-mac", "voicestudio-mac.git"):
        (tmp_path / "api" / name).mkdir(parents=True)

    leftovers = startup_services.leftover_studios()

    assert [row["modality"] for row in leftovers] == ["music", "video"]
    assert leftovers[0]["title"] == "Music Studio KH"
    assert sorted(leftovers[0]["folders"]) == ["musicstudio-mac", "musicstudio-mac.git"]
    assert leftovers[1]["folders"] == ["videostudio-mac"]
    assert startup_services.local_snapshot()["leftover_studios"] == leftovers


def test_leftover_studios_is_empty_when_no_legacy_checkout_remains(
    tmp_path, monkeypatch,
):
    """A clean Mac reports nothing, so the dashboard renders no section at all."""
    monkeypatch.setattr(control, "PINOKIO_HOME", tmp_path)
    monkeypatch.setattr(registry, "load_registry", lambda: [])
    monkeypatch.setattr(startup_services, "_launchd_loaded", lambda _label: False)
    (tmp_path / "api" / "voicestudio-mac.git").mkdir(parents=True)

    assert startup_services.leftover_studios() == []
    assert startup_services.local_snapshot()["leftover_studios"] == []


def test_startup_snapshot_omits_fully_removed_optional_studios(tmp_path, monkeypatch):
    monkeypatch.setattr(control, "PINOKIO_HOME", tmp_path)
    monkeypatch.setattr(registry, "load_registry", lambda: [])
    monkeypatch.setattr(startup_services, "_launchd_loaded", lambda label: False)

    snapshot = startup_services.local_snapshot()

    assert {row["modality"] for row in snapshot["services"]} == {"image", "voice"}


def test_startup_install_api_protects_busy_work(authed, monkeypatch):
    called = []
    monkeypatch.setattr(startup_services, "install_service",
                        lambda modality: called.append(modality) or {"ok": True})
    monkeypatch.setattr(fleet_ops, "studio_has_active_work", lambda studio_id: True)

    response = authed.post("/api/hub/startup-services/local/image/install")
    assert response.status_code == 409
    assert called == []
    assert "image" not in main.broker._maintenance


def test_startup_install_api_runs_locally_or_through_peer(authed, monkeypatch):
    monkeypatch.setattr(fleet_ops, "studio_has_active_work", lambda studio_id: False)
    monkeypatch.setattr(startup_services, "install_service",
                        lambda modality: {"ok": True, "changed": True, "modality": modality})
    local = authed.post("/api/hub/startup-services/local/image/install")
    assert local.status_code == 200 and local.json()["modality"] == "image"

    main.monitor.registry.append({
        "id": "voice@mac-b", "modality": "voice", "machine": "mac-b",
        "host": "100.70.0.9", "port": 47870,
    })

    async def remote_install(client, studio, modality):
        assert studio["machine"] == "mac-b" and modality == "voice"
        return {"ok": True, "changed": True}

    monkeypatch.setattr(peers, "install_remote_startup_service", remote_install)
    remote = authed.post("/api/hub/startup-services/mac-b/voice/install")
    assert remote.status_code == 200 and remote.json()["changed"] is True
    assert "voice@mac-b" not in main.broker._maintenance


def test_startup_retire_api_orders_updater_service_and_routing_changes(
    owner, monkeypatch,
):
    _add_legacy_compat_target("music")
    events = []
    monkeypatch.setattr(fleet_ops, "studio_has_active_work", lambda studio_id: False)

    async def set_mode(target_id, mode):
        events.append(("updater", target_id, mode))
        return {"settings": {"mode": mode}}

    monkeypatch.setattr(main.fleet_auto_updates, "set_mode", set_mode)
    async def retirement_status(target_id):
        return {"state": "idle"}

    monkeypatch.setattr(main.fleet_auto_updates, "retirement_status", retirement_status)
    monkeypatch.setattr(
        startup_services, "uninstall_service",
        lambda modality: events.append(("unservice", modality)) or {
            "ok": True, "changed": True, "service": {"installed": False},
        },
    )
    monkeypatch.setattr(
        registry, "set_studio_enabled",
        lambda machine, studio_id, enabled: events.append(
            ("routing", machine, studio_id, enabled)
        ),
    )

    response = owner.post("/api/hub/startup-services/local/music/retire")

    assert response.status_code == 200
    assert response.json()["preserved"] == [
        "launcher", "models", "caches", "outputs", "settings",
    ]
    assert events == [
        ("updater", "music", "off"),
        ("routing", "local", "music", False),
        ("unservice", "music"),
    ]


def test_startup_retire_owner_route_rejects_fleet_only_credential(client):
    peers.set_fleet_token("shared-secret")

    response = client.post(
        "/api/hub/startup-services/local/music/retire",
        headers={"X-Hub-Token": "shared-secret"},
    )

    assert response.status_code == 403


def test_startup_retire_owner_route_rejects_hub_machine_token(client):
    response = client.post(
        "/api/hub/startup-services/local/music/retire",
        headers={"X-Hub-Token": main.HUB_TOKEN},
    )

    assert response.status_code == 403


@pytest.mark.parametrize("modality", ["image", "voice"])
def test_startup_retire_api_never_targets_active_production_families(
    owner, monkeypatch, modality,
):
    called = []
    monkeypatch.setattr(
        startup_services, "uninstall_service", lambda value: called.append(value),
    )

    response = owner.post(f"/api/hub/startup-services/local/{modality}/retire")

    assert response.status_code == 400
    assert called == []


def test_startup_retire_api_preserves_service_and_routing_when_work_is_active(
    owner, monkeypatch,
):
    _add_legacy_compat_target("chat")
    events = []
    monkeypatch.setattr(fleet_ops, "studio_has_active_work", lambda studio_id: True)
    monkeypatch.setattr(
        startup_services, "uninstall_service", lambda value: events.append("unservice"),
    )
    monkeypatch.setattr(
        registry, "set_studio_enabled", lambda *args: events.append("routing"),
    )

    response = owner.post("/api/hub/startup-services/local/chat/retire")

    assert response.status_code == 409
    assert events == []


def test_startup_retire_api_refuses_an_active_managed_update(owner, monkeypatch):
    _add_legacy_compat_target("music")
    events = []
    monkeypatch.setattr(fleet_ops, "studio_has_active_work", lambda studio_id: False)
    monkeypatch.setattr(
        main.fleet_auto_updates, "jobs",
        lambda: [{"status": "running", "items": [
            {"target": "music", "status": "updating"},
        ]}],
    )
    monkeypatch.setattr(
        startup_services, "uninstall_service", lambda value: events.append("unservice"),
    )
    monkeypatch.setattr(
        registry, "set_studio_enabled", lambda *args: events.append("routing"),
    )

    response = owner.post("/api/hub/startup-services/local/music/retire")

    assert response.status_code == 409
    assert "update" in response.json()["detail"].lower()
    assert events == []


def test_startup_retire_refuses_direct_sibling_update(owner, monkeypatch):
    _add_legacy_compat_target("music")
    events = []
    monkeypatch.setattr(fleet_ops, "studio_has_active_work", lambda studio_id: False)
    monkeypatch.setattr(main.fleet_auto_updates, "jobs", lambda: [])

    async def retirement_status(target_id):
        assert target_id == "music"
        return {"state": "restarting"}

    monkeypatch.setattr(main.fleet_auto_updates, "retirement_status", retirement_status)
    monkeypatch.setattr(
        startup_services, "uninstall_service", lambda value: events.append("unservice"),
    )
    monkeypatch.setattr(
        registry, "set_studio_enabled", lambda *args: events.append("routing"),
    )

    response = owner.post("/api/hub/startup-services/local/music/retire")

    assert response.status_code == 409
    assert "update" in response.json()["detail"].lower()
    assert events == []


def test_startup_retire_partial_failure_keeps_routing_disabled(owner, monkeypatch):
    _add_legacy_compat_target("music")
    events = []
    monkeypatch.setattr(fleet_ops, "studio_has_active_work", lambda studio_id: False)
    monkeypatch.setattr(main.fleet_auto_updates, "jobs", lambda: [])
    async def retirement_status(target_id):
        return {"state": "idle"}
    monkeypatch.setattr(main.fleet_auto_updates, "retirement_status", retirement_status)

    async def set_mode(target_id, mode):
        events.append(("updater", target_id, mode))
        return {"settings": {"mode": mode}}

    monkeypatch.setattr(main.fleet_auto_updates, "set_mode", set_mode)
    monkeypatch.setattr(
        registry, "set_studio_enabled",
        lambda machine, studio_id, enabled: events.append(
            ("routing", machine, studio_id, enabled)
        ),
    )
    monkeypatch.setattr(
        startup_services, "uninstall_service",
        lambda value: (_ for _ in ()).throw(ValueError("launchd refused")),
    )

    response = owner.post("/api/hub/startup-services/local/music/retire")

    assert response.status_code == 409
    assert events == [
        ("updater", "music", "off"),
        ("routing", "local", "music", False),
    ]


def test_startup_retire_api_disables_remote_registration_before_peer_mutation(
    owner, monkeypatch,
):
    main.monitor.registry.append({
        "id": "video@mac-b", "modality": "video", "machine": "mac-b",
        "host": "100.70.0.9", "port": 47872,
    })
    events = []
    monkeypatch.setattr(fleet_ops, "studio_has_active_work", lambda studio_id: False)

    async def remote_retire(client, studio, modality):
        events.append(("peer", studio["id"], modality))
        return {"ok": True, "changed": True}

    monkeypatch.setattr(peers, "retire_remote_startup_service", remote_retire)
    monkeypatch.setattr(
        registry, "set_studio_enabled",
        lambda machine, studio_id, enabled: events.append(
            ("routing", machine, studio_id, enabled)
        ),
    )

    response = owner.post("/api/hub/startup-services/mac-b/video/retire")

    assert response.status_code == 200
    assert events == [
        ("routing", "mac-b", "video@mac-b", False),
        ("peer", "video@mac-b", "video"),
    ]


def test_startup_retire_api_keeps_routing_disabled_after_peer_failure(
    owner, monkeypatch,
):
    main.monitor.registry.append({
        "id": "render@mac-b", "modality": "render", "machine": "mac-b",
        "host": "100.70.0.9", "port": 47874,
    })
    changed = []
    monkeypatch.setattr(fleet_ops, "studio_has_active_work", lambda studio_id: False)

    async def remote_retire(client, studio, modality):
        return {"ok": False, "error": "peer is offline"}

    monkeypatch.setattr(peers, "retire_remote_startup_service", remote_retire)
    monkeypatch.setattr(
        registry, "set_studio_enabled", lambda *args: changed.append(args),
    )

    response = owner.post("/api/hub/startup-services/mac-b/render/retire")

    assert response.status_code == 409
    assert changed == [("mac-b", "render@mac-b", False)]


def test_full_remove_api_disables_routing_then_removes_local_checkout(
    owner, monkeypatch,
):
    # No registry row on purpose: that is the real production shape for a
    # legacy family, and full-remove must not depend on one.
    # This case covers the full-remove path for an *installed* Studio, so pin
    # that precondition rather than inheriting it from the machine's real
    # Pinokio layout. Without this, any checkout where `PINOKIO_HOME` does not
    # resolve to a real install silently runs the ghost-cleanup branch instead
    # and the test fails while production is correct.
    monkeypatch.setattr(
        startup_services, "inspect_service",
        lambda modality: {"modality": modality, "app_installed": True},
    )
    events = []
    monkeypatch.setattr(fleet_ops, "studio_has_active_work", lambda studio_id: False)
    monkeypatch.setattr(main.fleet_auto_updates, "jobs", lambda: [])
    monkeypatch.setattr(
        registry, "set_studio_enabled",
        lambda machine, studio_id, enabled: events.append(
            ("routing", machine, studio_id, enabled)
        ),
    )
    monkeypatch.setattr(
        registry, "set_studio_removed",
        lambda machine, studio_id, removed: events.append(
            ("removed", machine, studio_id, removed)
        ),
        raising=False,
    )
    monkeypatch.setattr(
        registry, "set_studio_removal_complete",
        lambda machine, studio_id, complete: events.append(
            ("complete", machine, studio_id, complete)
        ),
        raising=False,
    )
    monkeypatch.setattr(
        startup_services, "fully_remove_studio",
        lambda modality: events.append(("remove", modality)) or {
            "ok": True, "removed": True, "trashed": ["/tmp/musicstudio-mac-removed-now"],
        },
        raising=False,
    )
    monkeypatch.setattr(main.monitor, "reload_registry", lambda: events.append(("reload",)))

    response = owner.post("/api/hub/startup-services/local/music/remove")

    assert response.status_code == 200
    assert response.json()["removed"] is True
    assert events == [
        ("removed", "local", "music", True),
        ("complete", "local", "music", False),
        ("routing", "local", "music", False),
        ("remove", "music"),
        ("complete", "local", "music", True),
        ("reload",),
    ]


def test_full_remove_deletes_an_installed_legacy_checkout_without_a_registry_row(
    owner, tmp_path, monkeypatch,
):
    """The owner-visible promise: "remove" deletes the folder, registration or not.

    Legacy families are no longer tracked in `studios.json`, so an installed
    Music/Chat/Video/Render checkout normally has no `monitor.registry` row at
    all. Removal is about the checkout on disk, so the missing row must not
    turn a full-remove into a 404 that leaves the Studio installed.
    """
    app_dir, launch_agents = _seed_app(tmp_path, monkeypatch, "music")
    (app_dir / "ENVIRONMENT").write_text("PINOKIO_SCRIPT_AUTOLAUNCH_ENABLED=true\n")
    (app_dir / "outputs").mkdir()
    _mark_installed(app_dir, launch_agents, "music")
    assert not any(row.get("modality") == "music" for row in main.monitor.registry)

    monkeypatch.setattr(fleet_ops, "studio_has_active_work", lambda _studio_id: False)
    monkeypatch.setattr(main.fleet_auto_updates, "jobs", lambda: [])
    monkeypatch.setattr(control, "stop_studio_sync", lambda studio: {"ok": True})
    monkeypatch.setattr(startup_services, "_remove_launch_agent", lambda _label: None)
    monkeypatch.setattr(startup_services, "_wait_port_closed", lambda _port: True)
    trash = tmp_path / "Trash"
    monkeypatch.setattr(startup_services, "_trash_dir", lambda: trash)

    response = owner.post("/api/hub/startup-services/local/music/remove")

    assert response.status_code == 200
    body = response.json()
    assert body["removed"] is True and body["routing_enabled"] is False
    assert not app_dir.exists()
    assert [Path(value).name.split("-removed-", 1)[0]
            for value in body["trashed_checkouts"]] == ["musicstudio-mac"]
    assert registry.studio_enabled("local", "music") is False
    assert registry.studio_removed("local", "music") is True
    assert registry.studio_removal_complete("local", "music") is True


def test_full_remove_of_an_absent_legacy_studio_deletes_nothing(
    owner, tmp_path, monkeypatch,
):
    """A Studio that is not installed is an honest no-op, never a blind delete."""
    monkeypatch.setattr(control, "PINOKIO_HOME", tmp_path)
    (tmp_path / "api").mkdir()
    launch_agents = tmp_path / "LaunchAgents"
    launch_agents.mkdir()
    monkeypatch.setattr(startup_services, "_launch_agents_dir", lambda: launch_agents)
    monkeypatch.setattr(startup_services, "_remove_launch_agent", lambda _label: None)
    monkeypatch.setattr(startup_services, "_launchd_loaded", lambda _label: False)
    monkeypatch.setattr(startup_services, "_wait_port_closed", lambda _port: True)
    monkeypatch.setattr(fleet_ops, "studio_has_active_work", lambda _studio_id: False)
    monkeypatch.setattr(main.fleet_auto_updates, "jobs", lambda: [])
    removed = []
    monkeypatch.setattr(
        startup_services, "fully_remove_studio", removed.append, raising=False,
    )
    monkeypatch.setattr(
        startup_services.shutil, "move",
        lambda *args: pytest.fail("an absent Studio must never move a path"),
    )

    response = owner.post("/api/hub/startup-services/local/chat/remove")

    assert response.status_code == 200
    assert response.json()["already_removed"] is True
    assert removed == []


def test_full_remove_refuses_a_symlinked_legacy_checkout_instead_of_deleting(
    owner, tmp_path, monkeypatch,
):
    """Only a real folder inside PINOKIO_HOME/api may be deleted — never a link out."""
    api_root = tmp_path / "api"
    api_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("protected")
    (api_root / "musicstudio-mac").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(control, "PINOKIO_HOME", tmp_path)
    launch_agents = tmp_path / "LaunchAgents"
    launch_agents.mkdir()
    monkeypatch.setattr(startup_services, "_launch_agents_dir", lambda: launch_agents)
    monkeypatch.setattr(startup_services, "_launchd_loaded", lambda _label: False)
    monkeypatch.setattr(fleet_ops, "studio_has_active_work", lambda _studio_id: False)
    monkeypatch.setattr(main.fleet_auto_updates, "jobs", lambda: [])
    monkeypatch.setattr(
        startup_services.shutil, "move",
        lambda *args: pytest.fail("an unsafe checkout path must never be moved"),
    )

    response = owner.post("/api/hub/startup-services/local/music/remove")

    assert response.status_code == 409
    assert "unsafe Studio checkout" in response.json()["detail"]
    assert (api_root / "musicstudio-mac").is_symlink()
    assert (outside / "keep.txt").read_text() == "protected"


@pytest.mark.parametrize("modality", ["image", "voice"])
def test_full_remove_leaves_an_installed_production_checkout_untouched(
    owner, tmp_path, monkeypatch, modality,
):
    """The 400 refusal for Image/Voice still precedes every removal decision."""
    app_dir, launch_agents = _seed_app(tmp_path, monkeypatch, modality)
    _mark_installed(app_dir, launch_agents, modality)
    monkeypatch.setattr(
        startup_services.shutil, "move",
        lambda *args: pytest.fail("a production Studio must never be moved"),
    )

    response = owner.post(f"/api/hub/startup-services/local/{modality}/remove")

    assert response.status_code == 400
    assert app_dir.is_dir()


def test_remote_full_remove_reaches_a_mac_through_any_row_it_still_has(
    owner, monkeypatch,
):
    """A fleet Mac's leftover Studio has no row either — reach it via a tracked one.

    The dashboard lists remote leftovers, so this must not 404 the way the
    local path used to.
    """
    main.monitor.registry.append({
        "id": "image@mac-b", "modality": "image", "machine": "mac-b",
        "host": "100.70.0.9", "port": 47868,
    })
    events = []
    monkeypatch.setattr(fleet_ops, "studio_has_active_work", lambda _studio_id: False)

    async def remote_remove(client, studio, modality):
        events.append(("peer", studio["id"], modality))
        return {"ok": True, "changed": True, "removed": True}

    monkeypatch.setattr(peers, "remove_remote_studio", remote_remove)
    monkeypatch.setattr(
        registry, "set_studio_enabled",
        lambda machine, studio_id, enabled: events.append(
            ("routing", machine, studio_id, enabled)
        ),
    )
    monkeypatch.setattr(
        registry, "remove_studio",
        lambda studio_id: events.append(("prune", studio_id)) or 0,
    )
    monkeypatch.setattr(
        main.monitor, "forget_studios",
        lambda studio_ids: events.append(("forget", sorted(studio_ids))),
    )

    response = owner.post("/api/hub/startup-services/mac-b/video/remove")

    assert response.status_code == 200
    assert response.json()["removed"] is True
    # Reached through the Image row, but only the removed Studio is purged —
    # the Mac keeps every Studio it really has registered.
    assert events == [
        ("routing", "mac-b", "video@mac-b", False),
        ("peer", "image@mac-b", "video"),
        ("prune", "video@mac-b"),
        ("forget", ["video@mac-b"]),
    ]


def test_remote_full_remove_still_refuses_a_machine_the_hub_does_not_know(
    owner, monkeypatch,
):
    called = []
    monkeypatch.setattr(
        peers, "remove_remote_studio",
        lambda *args: called.append(args), raising=False,
    )

    response = owner.post("/api/hub/startup-services/mac-nowhere/music/remove")

    assert response.status_code == 404
    assert "unknown machine" in response.json()["detail"]
    assert called == []


def test_remote_full_remove_lost_response_retry_prunes_controller_registration(
    owner, monkeypatch,
):
    main.monitor.registry.append({
        "id": "music@mac-b", "modality": "music", "machine": "mac-b",
        "host": "100.70.0.9", "port": 47869,
    })
    events = []
    monkeypatch.setattr(fleet_ops, "studio_has_active_work", lambda _studio_id: False)
    monkeypatch.setattr(
        registry, "set_studio_enabled",
        lambda machine, studio_id, enabled: events.append(
            ("routing", machine, studio_id, enabled)
        ),
    )
    monkeypatch.setattr(
        registry, "remove_studio",
        lambda studio_id: events.append(("registry", studio_id)) or 1,
    )

    async def already_removed(_client, _studio, _modality):
        return {
            "ok": True, "changed": False, "removed": True,
            "already_removed": True,
        }

    monkeypatch.setattr(peers, "remove_remote_studio", already_removed)
    monkeypatch.setattr(main.monitor, "reload_registry", lambda: events.append(("reload",)))
    monkeypatch.setattr(main.monitor, "forget_studios", lambda ids: events.append(("monitor", ids)))
    monkeypatch.setattr(fleet_ops, "forget_studios", lambda ids: events.append(("fleet", ids)))
    monkeypatch.setattr(peers, "forget_machine", lambda machine: events.append(("peer", machine)))

    response = owner.post("/api/hub/startup-services/mac-b/music/remove")

    assert response.status_code == 200
    assert response.json()["already_removed"] is True
    assert ("registry", "music@mac-b") in events


def test_full_remove_peer_retry_is_idempotent_after_lost_success_response(
    app, monkeypatch,
):
    peers.set_fleet_token("shared-secret")
    monkeypatch.setattr(
        control_plane, "load_settings",
        lambda: {
            "role": "agent",
            "parent_controller_url": "http://100.64.0.10:47873",
        },
    )
    monkeypatch.setattr(
        main, "resolve_private_origin",
        lambda _url: SimpleNamespace(address="100.64.0.10"),
    )
    monkeypatch.setattr(startup_services, "is_fully_removed", lambda modality: True)
    monkeypatch.setattr(main.monitor, "registry", [])

    response = TestClient(
        app,
        client=("100.64.0.10", 50000),
        headers={"X-Hub-Token": "shared-secret"},
    ).post("/api/hub/service/startup-services/local/music/remove")

    assert response.status_code == 200
    assert response.json()["already_removed"] is True
    assert response.json()["changed"] is False


def test_full_remove_peer_retry_finishes_incomplete_cleanup_before_success(
    app, monkeypatch,
):
    peers.set_fleet_token("shared-secret")
    monkeypatch.setattr(
        control_plane, "load_settings",
        lambda: {
            "role": "agent",
            "parent_controller_url": "http://100.64.0.10:47873",
        },
    )
    monkeypatch.setattr(
        main, "resolve_private_origin",
        lambda _url: SimpleNamespace(address="100.64.0.10"),
    )
    monkeypatch.setattr(startup_services, "is_fully_removed", lambda _modality: False)
    monkeypatch.setattr(startup_services, "has_removal_intent", lambda _modality: True)
    events = []
    monkeypatch.setattr(
        startup_services, "finalize_absent_studio_removal",
        lambda modality: events.append(("finalize", modality)) or {
            "ok": True, "changed": True, "removed": True,
            "already_removed": True, "modality": modality,
        },
    )
    monkeypatch.setattr(
        registry, "set_studio_removal_complete",
        lambda machine, studio_id, complete: events.append(
            ("complete", machine, studio_id, complete)
        ),
        raising=False,
    )
    monkeypatch.setattr(main.monitor, "registry", [])

    response = TestClient(
        app,
        client=("100.64.0.10", 50000),
        headers={"X-Hub-Token": "shared-secret"},
    ).post("/api/hub/service/startup-services/local/music/remove")

    assert response.status_code == 200
    assert events == [
        ("finalize", "music"),
        ("complete", "local", "music", True),
    ]


def test_full_remove_peer_first_request_finalizes_an_absent_unregistered_studio(
    app, monkeypatch,
):
    peers.set_fleet_token("shared-secret")
    monkeypatch.setattr(
        control_plane, "load_settings",
        lambda: {
            "role": "agent",
            "parent_controller_url": "http://100.64.0.10:47873",
        },
    )
    monkeypatch.setattr(
        main, "resolve_private_origin",
        lambda _url: SimpleNamespace(address="100.64.0.10"),
    )
    monkeypatch.setattr(startup_services, "is_fully_removed", lambda _modality: False)
    monkeypatch.setattr(startup_services, "has_removal_intent", lambda _modality: False)
    monkeypatch.setattr(
        startup_services, "inspect_service",
        lambda modality: {"modality": modality, "app_installed": False},
    )
    events = []
    monkeypatch.setattr(
        startup_services, "finalize_absent_studio_removal",
        lambda modality: events.append(("finalize", modality)) or {
            "ok": True, "changed": True, "removed": True,
            "already_removed": True, "modality": modality,
        },
    )
    monkeypatch.setattr(
        registry, "set_studio_removed",
        lambda machine, studio_id, removed: events.append(
            ("removed", machine, studio_id, removed)
        ),
    )
    monkeypatch.setattr(
        registry, "set_studio_removal_complete",
        lambda machine, studio_id, complete: events.append(
            ("complete", machine, studio_id, complete)
        ),
    )
    monkeypatch.setattr(
        registry, "set_studio_enabled",
        lambda machine, studio_id, enabled: events.append(
            ("routing", machine, studio_id, enabled)
        ),
    )
    monkeypatch.setattr(main.monitor, "registry", [])
    monkeypatch.setattr(main.monitor, "reload_registry", lambda: events.append(("reload",)))

    response = TestClient(
        app,
        client=("100.64.0.10", 50000),
        headers={"X-Hub-Token": "shared-secret"},
    ).post("/api/hub/service/startup-services/local/render/remove")

    assert response.status_code == 200
    assert response.json()["already_removed"] is True
    assert events == [
        ("removed", "local", "render", True),
        ("complete", "local", "render", False),
        ("routing", "local", "render", False),
        ("finalize", "render"),
        ("complete", "local", "render", True),
        ("reload",),
    ]


def test_full_remove_peer_rejects_shared_token_from_non_controller_agent(
    app, monkeypatch,
):
    peers.set_fleet_token("shared-secret")
    monkeypatch.setattr(
        control_plane, "load_settings",
        lambda: {
            "role": "agent",
            "parent_controller_url": "http://100.64.0.10:47873",
        },
    )
    monkeypatch.setattr(
        main, "resolve_private_origin",
        lambda _url: SimpleNamespace(address="100.64.0.10"),
    )

    response = TestClient(
        app,
        client=("100.64.0.11", 50000),
        headers={"X-Hub-Token": "shared-secret"},
    ).post("/api/hub/service/startup-services/local/music/remove")

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "source_host_mismatch"


def test_startup_retire_peer_rejects_shared_token_from_non_controller_agent(
    app, monkeypatch,
):
    peers.set_fleet_token("shared-secret")
    monkeypatch.setattr(
        control_plane, "load_settings",
        lambda: {
            "role": "agent",
            "parent_controller_url": "http://100.64.0.10:47873",
        },
    )
    monkeypatch.setattr(
        main, "resolve_private_origin",
        lambda _url: SimpleNamespace(address="100.64.0.10"),
    )

    response = TestClient(
        app,
        client=("100.64.0.11", 50000),
        headers={"X-Hub-Token": "shared-secret"},
    ).post("/api/hub/service/startup-services/local/render/retire")

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "source_host_mismatch"


@pytest.mark.parametrize("modality", ["image", "voice"])
def test_full_remove_api_protects_production_studios(owner, monkeypatch, modality):
    called = []
    monkeypatch.setattr(
        startup_services, "fully_remove_studio",
        lambda value: called.append(value),
        raising=False,
    )

    response = owner.post(f"/api/hub/startup-services/local/{modality}/remove")

    assert response.status_code == 400
    assert called == []


def test_dashboard_tracks_startup_for_image_and_voice_only():
    dashboard = (Path(__file__).parents[1] / "frontend" / "index.html").read_text()
    assert 'id="startup-refresh"' in dashboard
    assert 'id="startup-install-all"' in dashboard
    assert 'id="startup-body"' in dashboard
    assert "function loadStartupServices()" in dashboard
    assert "function installStartupService(" in dashboard
    assert "function installMissingStartupServices()" in dashboard
    assert 'const TRACKED_STUDIO_MODALITIES = ["image", "voice"]' in dashboard
    assert 'id="startup-remove-unused"' not in dashboard
    assert "function removeStartupStudio(" not in dashboard
    assert "function removeUnusedStudios()" not in dashboard


def test_generation_install_api_is_a_separate_explicit_fleet_job(authed, monkeypatch):
    called = []

    def start_generation(monitor, studio_ids, *, local_only=False):
        called.append((studio_ids, local_only))
        return {"id": "generation-1", "status": "queued", "items": []}

    monkeypatch.setattr(fleet_ops, "start_generation_installs", start_generation)
    response = authed.post("/api/hub/maintenance/generation-installs", json={})
    assert response.status_code == 200
    assert called == [(None, False)]

    dashboard = (Path(__file__).parents[1] / "frontend" / "index.html").read_text()
    assert 'id="generation-install-all"' in dashboard
    assert "startGenerationInstall()" in dashboard


def test_studio_update_repair_api_is_capability_gated_and_supports_retry(authed, monkeypatch):
    started = []
    retried = []

    def start_repair(monitor, studio_ids, *, local_only=False):
        started.append((studio_ids, local_only))
        return {"id": "repair-1", "status": "queued", "items": []}

    def retry_repair(monitor, job_id):
        retried.append(job_id)
        return {"id": "repair-2", "status": "queued", "items": []}

    monkeypatch.setattr(fleet_ops, "start_studio_update_repairs", start_repair)
    monkeypatch.setattr(fleet_ops, "retry_studio_update_repairs", retry_repair)

    version = authed.get("/api/version")
    started_response = authed.post(
        "/api/hub/maintenance/studio-update-repairs",
        json={"studio_ids": ["voice"], "local_only": True},
    )
    retried_response = authed.post(
        "/api/hub/maintenance/studio-update-repairs/repair-1/retry"
    )

    assert version.status_code == 200
    assert version.json()["studio_update_repair_schema"] == 1
    assert started_response.status_code == 200 and started == [(["voice"], True)]
    assert retried_response.status_code == 200 and retried == ["repair-1"]


def test_studio_update_repair_retry_runs_on_event_loop(authed, monkeypatch):
    def retry_repair(_monitor, _job_id):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            loop_running = False
        else:
            loop_running = True
        return {"id": "repair-2", "loop_running": loop_running}

    monkeypatch.setattr(fleet_ops, "retry_studio_update_repairs", retry_repair)

    response = authed.post(
        "/api/hub/maintenance/studio-update-repairs/repair-1/retry"
    )

    assert response.status_code == 200
    assert response.json()["loop_running"] is True


def test_dashboard_exposes_one_time_remote_studio_update_repair():
    dashboard = (Path(__file__).parents[1] / "frontend" / "index.html").read_text()

    assert 'id="studio-update-repair-card"' in dashboard
    assert 'id="studio-update-repair-run"' in dashboard
    assert 'id="studio-update-repair-status"' in dashboard
    assert "function startStudioUpdateRepair()" in dashboard
    assert "function retryStudioUpdateRepair(" in dashboard
    assert '"/api/hub/maintenance/studio-update-repairs"' in dashboard
    assert "/api/hub/maintenance/studio-update-repairs/${encodeURIComponent(jobId)}/retry" in dashboard
    assert "complete machine-local ENVIRONMENT" in dashboard
    assert "Models, enrollment, voices, and jobs are not changed" in dashboard
    assert "update the Agent Hub first" in dashboard

@pytest.mark.asyncio
async def test_voice_recovery_rejects_malformed_or_unknown_job_listing(reset):
    studio = {"id": "voice", "modality": "voice", "machine": "local",
              "host": "127.0.0.1", "port": 47870, "app": "voicestudio-mac.git"}

    class Response:
        status_code = 200
        def __init__(self, payload): self.payload = payload
        def json(self): return self.payload
    class Client:
        def __init__(self, payload): self.payload = payload
        async def get(self, *_args, **_kwargs): return Response(self.payload)

    assert await main._other_active_voice_jobs(Client({"jobs": [{"id": "other", "state": "mystery"}]}), studio, "original") is None
    assert await main._other_active_voice_jobs(Client({"jobs": [{"state": "running"}]}), studio, "original") is None
    assert await main._other_active_voice_jobs(Client({"jobs": [{"id": "other", "state": "running"}]}), studio, "original") is True
    assert await main._other_active_voice_jobs(Client({"jobs": [{"id": "other", "state": "uncertain"}]}), studio, "original") is None


def test_voice_recovery_requires_exact_registered_launchd_target(monkeypatch):
    expected = {"installed": True, "server_loaded": True, "watchdog_loaded": True,
                "app": "voicestudio-mac.git"}
    monkeypatch.setattr(startup_services, "verified_voice_service", lambda: expected)
    exact = {"id": "voice", "modality": "voice", "machine": "local",
             "host": "127.0.0.1", "port": 47870, "app": "voicestudio-mac.git"}
    assert main._managed_local_voice_service(exact) == expected
    assert main._managed_local_voice_service({**exact, "port": 47871}) is None
    assert main._managed_local_voice_service({**exact, "id": "voice@other"}) is None


@pytest.mark.asyncio
async def test_reconcile_rejects_different_worker_job_identity(reset):
    submitted = broker.submit_batch({"modality": "voice", "model": "local/voice", "items": [{"text": "recover"}]})
    batch = broker.batches[submitted["batch_id"]]
    item = batch["items"][0]
    item.update(state="uncertain", studio="voice", studio_job_id="original")
    studio = {"id": "voice", "modality": "voice", "machine": "local", "host": "127.0.0.1", "port": 47870}

    class Response:
        status_code = 200
        def json(self): return {"job": {"id": "other", "state": "done"}}
    class Client:
        async def get(self, *_args, **_kwargs): return Response()

    assert await main._reconcile_voice_recovery_job(Client(), batch, item, studio) == "unknown"
    assert item["state"] == "uncertain"

@pytest.mark.asyncio
async def test_voice_recovery_cancels_before_bounded_reconcile_grace(reset, monkeypatch):
    submitted = broker.submit_batch({"modality": "voice", "model": "local/voice", "items": [{"text": "recover"}]})
    batch = broker.batches[submitted["batch_id"]]
    item = batch["items"][0]
    item.update(state="uncertain", studio="voice", studio_job_id="voice-job-1")
    studio = {"id": "voice", "modality": "voice", "machine": "local", "host": "127.0.0.1", "port": 47870}
    events = []
    async def reconcile(*_args): events.append("reconcile"); return "active"
    async def signal(*_args): events.append("cancel"); return True
    async def sleep(_seconds): events.append("grace")
    monkeypatch.setattr(main, "VOICE_RECOVERY_GRACE_S", 1)
    monkeypatch.setattr(main, "_reconcile_voice_recovery_job", reconcile)
    monkeypatch.setattr(broker, "_signal_worker_cancel", signal)
    monkeypatch.setattr(main.asyncio, "sleep", sleep)

    result = await main._recover_voice_item(batch, item, studio, False, object())

    assert result["ok"] is False
    assert events == ["reconcile", "cancel", "grace", "reconcile"]


@pytest.mark.asyncio
async def test_voice_recovery_preserves_an_operator_maintenance_drain(reset, monkeypatch):
    submitted = broker.submit_batch({"modality": "voice", "model": "local/voice", "items": [{"text": "recover"}]})
    batch = broker.batches[submitted["batch_id"]]
    item = batch["items"][0]
    item.update(state="uncertain", studio="voice", studio_job_id="voice-job-1")
    studio = {"id": "voice", "modality": "voice", "machine": "local", "host": "127.0.0.1", "port": 47870}
    broker.set_maintenance("voice", True)
    async def terminal(*_args): return "cancelled"
    monkeypatch.setattr(main, "_reconcile_voice_recovery_job", terminal)

    result = await main._recover_voice_item(batch, item, studio, False, object())

    assert result["state"] == "cancelled"
    assert broker.in_maintenance("voice") is True
    broker.set_maintenance("voice", False)


@pytest.mark.asyncio
async def test_voice_recovery_route_rejects_a_concurrent_same_item(reset):
    submitted = broker.submit_batch({"modality": "voice", "model": "local/voice", "items": [{"text": "recover"}]})
    batch_id = submitted["batch_id"]
    broker.batches[batch_id]["items"][0].update(
        state="uncertain", studio="voice", studio_job_id="voice-job-1",
    )
    item_key = ("item", f"{batch_id}:0")
    service_key = ("service", "voice")
    second = broker.submit_batch({"modality": "voice", "model": "local/voice", "items": [{"text": "second"}]})
    broker.batches[second["batch_id"]]["items"][0].update(
        state="uncertain", studio="voice", studio_job_id="voice-job-2",
    )
    with main._voice_recovery_guard:
        main._voice_recovery_inflight.update({item_key, service_key})
    try:
        with pytest.raises(main.HTTPException, match="already in progress"):
            await main.hub_recover_voice_job(batch_id, 0, main.VoiceRecoveryBody())
        with pytest.raises(main.HTTPException, match="already in progress"):
            await main.hub_recover_voice_job(second["batch_id"], 0, main.VoiceRecoveryBody())
    finally:
        with main._voice_recovery_guard:
            main._voice_recovery_inflight.difference_update({item_key, service_key})

def test_verified_voice_service_rejects_lookalike_launchd_plist(tmp_path, monkeypatch):
    app_dir, launch_agents = _seed_app(tmp_path, monkeypatch, "voice")
    _mark_installed(app_dir, launch_agents, "voice")
    spec = startup_services.SERVICE_SPECS["voice"]
    for name, body in (("voicestudio-serve.sh", "--port 47870\n"),
                       ("voicestudio-watchdog.sh", "PORT=47870\n")):
        (app_dir / name).write_text(body)
    def plist(label, script):
        (launch_agents / f"{label}.plist").write_bytes(plistlib.dumps({
            "Label": label, "ProgramArguments": [str(app_dir / script)],
        }))
    plist(spec["server_label"], "voicestudio-serve.sh")
    plist(spec["watchdog_label"], "voicestudio-watchdog.sh")
    monkeypatch.setattr(startup_services, "_launchd_loaded", lambda _label: True)

    assert startup_services.verified_voice_service()["installed"] is True
    plist(spec["server_label"], "other-script.sh")
    assert startup_services.verified_voice_service() is None


@pytest.mark.asyncio
async def test_voice_recovery_keeps_drain_if_cancelled_during_restart(reset, monkeypatch):
    submitted = broker.submit_batch({"modality": "voice", "model": "local/voice", "items": [{"text": "recover"}]})
    batch = broker.batches[submitted["batch_id"]]
    item = batch["items"][0]
    item.update(state="uncertain", studio="voice", studio_job_id="voice-job-1")
    studio = {"id": "voice", "modality": "voice", "machine": "local", "host": "127.0.0.1", "port": 47870, "app": "voicestudio-mac.git"}
    entered, release = asyncio.Event(), asyncio.Event()
    async def active(*_args): return "active"
    async def signal(*_args): return True
    async def none_other(*_args): return False
    async def blocked(_fn):
        entered.set()
        await release.wait()
    monkeypatch.setattr(main, "VOICE_RECOVERY_GRACE_S", 0)
    monkeypatch.setattr(main, "_reconcile_voice_recovery_job", active)
    monkeypatch.setattr(main, "_managed_local_voice_service", lambda _studio: {"installed": True})
    monkeypatch.setattr(broker, "_signal_worker_cancel", signal)
    monkeypatch.setattr(main, "_other_active_voice_jobs", none_other)
    monkeypatch.setattr(main.asyncio, "to_thread", blocked)
    task = asyncio.create_task(main._recover_voice_item(batch, item, studio, True, object()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert broker.in_maintenance("voice") is True
    broker.set_maintenance("voice", False)
    release.set()

def test_verified_voice_service_treats_symlinked_or_malformed_plist_as_unavailable(tmp_path, monkeypatch):
    app_dir, launch_agents = _seed_app(tmp_path, monkeypatch, "voice")
    _mark_installed(app_dir, launch_agents, "voice")
    spec = startup_services.SERVICE_SPECS["voice"]
    for name, body in (("voicestudio-serve.sh", "--port 47870\n"),
                       ("voicestudio-watchdog.sh", "PORT=47870\n")):
        (app_dir / name).write_text(body)
    for label, script in ((spec["server_label"], "voicestudio-serve.sh"),
                          (spec["watchdog_label"], "voicestudio-watchdog.sh")):
        (launch_agents / f"{label}.plist").write_bytes(plistlib.dumps({"Label": label, "ProgramArguments": [str(app_dir / script)]}))
    monkeypatch.setattr(startup_services, "_launchd_loaded", lambda _label: True)
    server = launch_agents / f"{spec['server_label']}.plist"
    outside = tmp_path / "outside.plist"
    outside.write_bytes(server.read_bytes())
    server.unlink(); server.symlink_to(outside)
    assert startup_services.verified_voice_service() is None
    server.unlink(); server.write_bytes(b'<?xml version="1.0"?><plist><dict><key>Label</key>')
    assert startup_services.verified_voice_service() is None
