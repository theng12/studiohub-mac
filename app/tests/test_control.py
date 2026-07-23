import subprocess

from backend import control


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
