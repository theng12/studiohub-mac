import contextlib
import fcntl
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from backend import control, fleet_ops, peers, registry, startup_services
from backend import main


@pytest.fixture
def owner(app):
    return TestClient(app, client=("127.0.0.1", 50000))


@pytest.fixture(autouse=True)
def isolated_retirement_lock(monkeypatch, request):
    if request.node.name == "test_startup_retirement_lock_refuses_a_running_updater":
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


def test_startup_retirement_lock_refuses_a_running_updater(tmp_path, monkeypatch):
    app_dir, _ = _seed_app(tmp_path, monkeypatch, "music")
    lock_path = app_dir / "auto_update" / "update.lock"
    lock_path.parent.mkdir()
    with open(lock_path, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="update is already running"):
            with startup_services.retirement_lock("music"):
                pytest.fail("retirement acquired a live updater lock")


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


def test_startup_snapshot_marks_disabled_legacy_service_retired(tmp_path, monkeypatch):
    _seed_app(tmp_path, monkeypatch, "music")
    monkeypatch.setattr(startup_services, "_launchd_loaded", lambda label: False)
    monkeypatch.setattr(
        registry, "load_registry",
        lambda: [{"id": "music", "modality": "music", "machine": "local"}],
    )
    monkeypatch.setattr(
        registry, "studio_enabled",
        lambda machine, studio_id: not (machine == "local" and studio_id == "music"),
    )

    snapshot = startup_services.local_snapshot()
    music = next(row for row in snapshot["services"] if row["modality"] == "music")

    assert music["retired"] is True
    assert music["routing_enabled"] is False


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


def test_dashboard_exposes_fleet_startup_controls():
    dashboard = (Path(__file__).parents[1] / "frontend" / "index.html").read_text()
    assert 'id="startup-refresh"' in dashboard
    assert 'id="startup-install-all"' in dashboard
    assert 'id="startup-retire-unused"' in dashboard
    assert 'id="startup-body"' in dashboard
    assert "function loadStartupServices()" in dashboard
    assert "function installStartupService(" in dashboard
    assert "function installMissingStartupServices()" in dashboard
    assert 'const RETIRABLE_STARTUP_MODALITIES = ["music", "chat", "video", "render"]' in dashboard
    assert "function retireStartupService(" in dashboard
    assert "function retireUnusedStartupServices()" in dashboard
    assert "startupActionBusy ||" in dashboard
    assert "startupOfflineMachines" in dashboard
    assert "offline or unsupported" in dashboard
    assert "Nothing is deleted" in dashboard
    assert "Image" not in dashboard.split("const RETIRABLE_STARTUP_MODALITIES =", 1)[1].split(";", 1)[0]
    assert "Voice" not in dashboard.split("const RETIRABLE_STARTUP_MODALITIES =", 1)[1].split(";", 1)[0]


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
