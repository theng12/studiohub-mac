import subprocess
import json

from backend import control


def _repair_fixture(tmp_path, monkeypatch, *, suffix=""):
    home = tmp_path / "pinokio"
    launcher = home / "api" / "studiohub-mac"
    tool = launcher / "ssd_bootstrap" / "kit" / "runtime_state_migration.py"
    tool.parent.mkdir(parents=True)
    tool.write_text("#!/usr/bin/env python3\n")
    app = home / "api" / f"voicestudio-mac{suffix}"
    app.mkdir(parents=True)
    (app / "update.js").write_text("module.exports = {};\n")
    monkeypatch.setattr(control, "PINOKIO_HOME", home)
    monkeypatch.setattr(control, "LAUNCHER_ROOT", launcher)
    return home, launcher, tool, app


def test_bundled_pterm_uses_bundled_node():
    pterm = str(control.PINOKIO_HOME / "bin" / "npm" / "bin" / "pterm")
    command = control.pterm_command(pterm, "start", "update.js", "pinokio://test")
    assert command[-4:] == ["start", "update.js", "--ref", "pinokio://test"]
    node = control.PINOKIO_HOME / "bin" / "miniforge" / "bin" / "node"
    if node.exists():
        assert command[:2] == [str(node), pterm]


def test_app_folder_resolution_accepts_exact_git_suffix_variant(tmp_path, monkeypatch):
    monkeypatch.setattr(control, "PINOKIO_HOME", tmp_path)
    actual = tmp_path / "api" / "imagestudio-mac.git"
    actual.mkdir(parents=True)
    assert control.resolve_app_dir({"app": "imagestudio-mac"}) == actual
    assert control.resolve_app_dir({"app": "imagestudio-mac.git"}) == actual


def test_restart_hub_service_uses_fixed_launchd_helper(tmp_path, monkeypatch):
    helper = tmp_path / "restart_service.sh"
    helper.write_text("#!/bin/bash\n")
    monkeypatch.setattr(control, "LAUNCHER_ROOT", tmp_path)
    calls = {}

    monkeypatch.setattr(control.subprocess, "run", lambda args, **kwargs:
                        subprocess.CompletedProcess(args, 0, "", ""))

    def popen(args, **kwargs):
        calls["args"] = args
        calls["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(control.subprocess, "Popen", popen)
    result = control.restart_hub_service(delay_seconds=2)
    assert result["ok"] is True
    assert result["service"] == "com.kh.studiohub.server"
    assert calls["args"] == ["/bin/bash", str(helper), "2"]
    assert calls["kwargs"]["start_new_session"] is True
    assert calls["kwargs"]["stdin"] is subprocess.DEVNULL


def test_restart_hub_service_refuses_unloaded_service(tmp_path, monkeypatch):
    (tmp_path / "restart_service.sh").write_text("#!/bin/bash\n")
    monkeypatch.setattr(control, "LAUNCHER_ROOT", tmp_path)
    monkeypatch.setattr(control.subprocess, "run", lambda args, **kwargs:
                        subprocess.CompletedProcess(args, 1, "", ""))
    monkeypatch.setattr(
        control.subprocess, "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("restart must not be spawned")),
    )
    result = control.restart_hub_service()
    assert result["ok"] is False
    assert "not loaded" in result["error"]


def test_studio_update_repair_runs_only_fixed_tool_and_app(tmp_path, monkeypatch):
    _home, launcher, tool, _app = _repair_fixture(tmp_path, monkeypatch, suffix=".git")
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        payload = {"ok": True, "repositories": [{
            "name": "voicestudio-mac", "path": "/private/voice",
            "status": "migrated", "backup_path": "/private/backup",
            "refusal_reason": None,
        }]}
        return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")

    monkeypatch.setattr(control.subprocess, "run", run)
    result = control.run_studio_update_repair_sync({
        "id": "voice", "app": "voicestudio-mac", "modality": "voice",
        "machine": "local",
    })

    assert result == {
        "ok": True, "studio": "voice", "status": "migrated",
        "detail": "machine settings preserved; update and dependencies verified",
    }
    args, kwargs = calls[0]
    assert args == [
        "/usr/bin/python3", str(tool), "--app", "voicestudio-mac",
        "--preserve-machine-environment", "--json",
    ]
    assert kwargs["cwd"] == str(launcher)
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["timeout"] == 45 * 60
    assert "/private" not in json.dumps(result)


def test_studio_update_repair_refuses_remote_and_unsupported_studios(tmp_path, monkeypatch):
    _repair_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        control.subprocess, "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not execute")),
    )

    remote = control.run_studio_update_repair_sync({
        "id": "voice@mac-b", "app": "voicestudio-mac", "modality": "voice",
        "machine": "mac-b",
    })
    unsupported = control.run_studio_update_repair_sync({
        "id": "music", "app": "musicstudio-mac", "modality": "music",
        "machine": "local",
    })

    assert remote["ok"] is False and "local" in remote["error"]
    assert unsupported["ok"] is False and "Voice or Image" in unsupported["error"]


def test_studio_update_repair_sanitizes_failure_and_malformed_output(tmp_path, monkeypatch):
    home, _launcher, _tool, _app = _repair_fixture(tmp_path, monkeypatch)
    responses = iter([
        subprocess.CompletedProcess(
            [], 1,
            json.dumps({"ok": False, "repositories": [{
                "name": "voicestudio-mac", "path": str(home / "api/voicestudio-mac"),
                "status": "failed", "backup_path": str(home / "secret-backup"),
                "refusal_reason": f"dirty checkout at {home}/api/voicestudio-mac/ENVIRONMENT",
            }]}),
            "",
        ),
        subprocess.CompletedProcess([], 0, "not-json", ""),
    ])
    monkeypatch.setattr(control.subprocess, "run", lambda *_args, **_kwargs: next(responses))
    studio = {"id": "voice", "app": "voicestudio-mac", "modality": "voice", "machine": "local"}

    failed = control.run_studio_update_repair_sync(studio)
    malformed = control.run_studio_update_repair_sync(studio)

    assert failed["ok"] is False
    assert str(home) not in failed["error"]
    assert "PINOKIO_HOME" in failed["error"]
    assert malformed["ok"] is False and "machine-readable" in malformed["error"]
