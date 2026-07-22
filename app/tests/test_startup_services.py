from pathlib import Path
from types import SimpleNamespace

import pytest

from backend import control, fleet_ops, peers, startup_services
from backend import main


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


def test_dashboard_exposes_fleet_startup_controls():
    dashboard = (Path(__file__).parents[1] / "frontend" / "index.html").read_text()
    assert 'id="startup-refresh"' in dashboard
    assert 'id="startup-install-all"' in dashboard
    assert 'id="startup-body"' in dashboard
    assert "function loadStartupServices()" in dashboard
    assert "function installStartupService(" in dashboard
    assert "function installMissingStartupServices()" in dashboard
