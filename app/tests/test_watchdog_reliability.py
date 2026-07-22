from __future__ import annotations

import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).parents[2]
WATCHDOG = ROOT / "studiohub-watchdog.sh"


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def _watchdog_env(tmp_path: Path, *, healthy: bool) -> tuple[dict[str, str], Path, Path]:
    curl = tmp_path / "curl"
    launchctl = tmp_path / "launchctl"
    state = tmp_path / "watchdog-state"
    launches = tmp_path / "launches.log"
    _write_executable(curl, f"#!/bin/sh\nexit {0 if healthy else 1}\n")
    _write_executable(
        launchctl,
        "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$WATCHDOG_LAUNCH_LOG\"\n",
    )
    env = {
        **os.environ,
        "STUDIOHUB_WATCHDOG_CURL_BIN": str(curl),
        "STUDIOHUB_WATCHDOG_LAUNCHCTL_BIN": str(launchctl),
        "STUDIOHUB_WATCHDOG_STATE_FILE": str(state),
        "STUDIOHUB_WATCHDOG_FAILURES_REQUIRED": "3",
        "WATCHDOG_LAUNCH_LOG": str(launches),
    }
    return env, state, launches


def _run_watchdog(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(WATCHDOG)],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )


def test_watchdog_requires_three_consecutive_failures(tmp_path: Path) -> None:
    env, state, launches = _watchdog_env(tmp_path, healthy=False)

    first = _run_watchdog(env)
    second = _run_watchdog(env)
    assert "(1/3)" in first.stdout
    assert "(2/3)" in second.stdout
    assert state.read_text(encoding="utf-8").strip() == "2"
    assert not launches.exists()

    third = _run_watchdog(env)
    assert "failed 3 consecutive times" in third.stdout
    assert "kickstart -k" in launches.read_text(encoding="utf-8")


def test_watchdog_success_resets_failure_streak(tmp_path: Path) -> None:
    failing_env, state, launches = _watchdog_env(tmp_path, healthy=False)
    _run_watchdog(failing_env)
    assert state.read_text(encoding="utf-8").strip() == "1"

    healthy_env, _, _ = _watchdog_env(tmp_path, healthy=True)
    _run_watchdog(healthy_env)
    assert not state.exists()

    failing_env, _, _ = _watchdog_env(tmp_path, healthy=False)
    after_reset = _run_watchdog(failing_env)
    assert "(1/3)" in after_reset.stdout
    assert not launches.exists()
