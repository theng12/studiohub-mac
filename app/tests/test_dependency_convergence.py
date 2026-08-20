import json
import sys

import pytest

from backend import dependency_convergence as convergence


class FakeRunner:
    def __init__(self) -> None:
        self.argv: list[list[str]] = []
        self.calls: list[dict] = []

    def __call__(self, argv: list[str], **kwargs) -> None:
        self.argv.append(argv)
        self.calls.append(kwargs)


@pytest.fixture
def fake_runner(monkeypatch: pytest.MonkeyPatch) -> FakeRunner:
    monkeypatch.setattr(convergence, "uv_executable", lambda: "/fixed/pinokio/bin/miniforge/bin/uv")
    return FakeRunner()


@pytest.mark.parametrize("mode", ["base", "all-installed"])
def test_hub_modes_install_only_locked_base(mode: str, fake_runner: FakeRunner) -> None:
    convergence.converge(mode, runner=fake_runner)

    assert fake_runner.argv == [[
        "/fixed/pinokio/bin/miniforge/bin/uv", "pip", "install", "--python", sys.executable,
        "-r", str(convergence.APP / "requirements.lock"),
    ]]
    assert fake_runner.calls == [{"cwd": convergence.APP, "check": True, "timeout": 1200}]


def test_hub_rejects_generation_without_commands(fake_runner: FakeRunner) -> None:
    with pytest.raises(ValueError, match="Hub has no generation stack"):
        convergence.converge("generation", runner=fake_runner)

    assert fake_runner.argv == []


@pytest.mark.parametrize("mode", ["", "install", "base; echo no", "all"])
def test_hub_rejects_unrecognized_modes_without_commands(mode: str, fake_runner: FakeRunner) -> None:
    with pytest.raises(ValueError, match="mode must be base or all-installed"):
        convergence.converge(mode, runner=fake_runner)

    assert fake_runner.argv == []


def test_uv_executable_uses_only_fixed_configured_pinokio_path(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "pinokio"
    uv = home / "bin" / "miniforge" / "bin" / "uv"
    uv.parent.mkdir(parents=True)
    uv.touch()
    config = tmp_path / ".pinokio" / "config.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"home": str(home)}), encoding="utf-8")
    monkeypatch.setattr(convergence.Path, "home", classmethod(lambda _cls: tmp_path))

    assert convergence.uv_executable() == str(uv)


@pytest.mark.parametrize(
    "payload",
    [None, "{not json", {}, {"home": 1}, {"home": "relative/pinokio"}, {"home": "/missing/pinokio"}],
)
def test_uv_executable_fails_closed_for_invalid_or_missing_config(
    tmp_path, monkeypatch: pytest.MonkeyPatch, payload: object,
) -> None:
    config = tmp_path / ".pinokio" / "config.json"
    if payload is not None:
        config.parent.mkdir()
        config.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(convergence.Path, "home", classmethod(lambda _cls: tmp_path))

    with pytest.raises(RuntimeError, match="Configured Pinokio"):
        convergence.uv_executable()


@pytest.mark.parametrize("mode", ["base", "all-installed"])
def test_cli_accepts_only_fixed_modes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], mode: str,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(convergence, "converge", lambda selected: calls.append(selected))

    assert convergence.main([mode]) == 0

    assert capsys.readouterr().err == ""
    assert calls == [mode]


@pytest.mark.parametrize("invalid", ["generation", "secret-token=abc", "x" * 10_000])
def test_cli_rejects_invalid_argv_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], invalid: str,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(convergence, "converge", lambda mode: calls.append(mode))

    assert convergence.main([invalid]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "dependency convergence: invalid mode\n"
    assert invalid not in captured.err
    assert calls == []


@pytest.mark.parametrize(("launcher", "mode"), [("install.js", "base"), ("update.js", "all-installed")])
def test_launchers_use_only_the_convergence_module(launcher: str, mode: str) -> None:
    source = (convergence.APP.parent / launcher).read_text(encoding="utf-8")
    invocation = f"python -m backend.dependency_convergence {mode}"

    assert source.count("python -m backend.dependency_convergence") == 1
    assert source.count(invocation) == 1
    assert "uv pip install" not in source
    assert "pip install" not in source


def test_update_launcher_keeps_owned_service_and_start_branches() -> None:
    source = (convergence.APP.parent / "update.js").read_text(encoding="utf-8")

    assert "{{exists('service/.installed')}}" in source
    assert "bash install_service.sh" in source
    assert "{{!exists('service/.installed')}}" in source
    assert 'uri: "start.js"' in source
    assert "com.kh.studiohub.server" in source
