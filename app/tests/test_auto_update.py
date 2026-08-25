from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from backend.auto_update import AutoUpdater, UpdateDeferred, UpdateError, _redact


@pytest.fixture
def updater(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AutoUpdater:
    root = tmp_path / "studiohub-mac"
    (root / ".git").mkdir(parents=True)
    (root / "app").mkdir()
    (root / "conda_env" / "bin").mkdir(parents=True)
    (root / "VERSION").write_text("1.0.0\n")
    (root / "app" / "requirements.txt").write_text("fastapi\n")
    python = root / "conda_env" / "bin" / "python"
    python.symlink_to(sys.executable)
    spec = {
        "root": str(root), "title": "Studio Hub KH", "slug": "studiohub-test",
        "expected_remote": "https://github.com/theng12/studiohub-mac.git",
        "branch": "main", "port": 47873, "default_hour": 1,
        "server_label": "com.kh.studiohub.server",
        "watchdog_label": "com.kh.studiohub.watchdog",
    }
    item = AutoUpdater(spec)
    monkeypatch.setattr(item, "scheduler_status", lambda: {
        "installed": item.settings()["mode"] != "off", "supported": True,
        "label": item.agent_label,
    })
    monkeypatch.setattr(item, "apply_scheduler", lambda force_pending=False: {
        "installed": item.settings()["mode"] != "off" or force_pending,
        "supported": True, "label": item.agent_label,
    })
    monkeypatch.setattr(item, "_notify", lambda *args: None)
    # AutoUpdater is a macOS component, but its state-machine tests also run on
    # Linux CI. Tests that need a specific launch mode override this explicitly;
    # the shared fixture must not probe /bin/launchctl on a non-Mac runner.
    monkeypatch.setattr(item, "active_mode", lambda: "stopped")
    return item


def _spec(root: Path) -> dict:
    return {
        "root": str(root), "title": "Studio Hub KH", "slug": "studiohub-test",
        "expected_remote": "https://github.com/theng12/studiohub-mac.git",
        "branch": "main", "port": 47873, "default_hour": 1,
        "server_label": "com.kh.studiohub.server",
        "watchdog_label": "com.kh.studiohub.watchdog",
    }


def test_linked_worktree_gitfile_is_accepted(tmp_path: Path):
    root = tmp_path / "linked-worktree"
    root.mkdir()
    gitdir = tmp_path / "main-checkout" / ".git" / "worktrees" / "linked-worktree"
    gitdir.mkdir(parents=True)
    (root / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")

    updater = AutoUpdater(_spec(root))

    assert updater.root == root.resolve()


@pytest.mark.parametrize("gitfile", [
    "not a gitfile\n",
    "gitdir:\n",
    "gitdir: target\nextra\n",
    "gitdir: \x00invalid\n",
    "gitdir: missing-target\n",
])
def test_malformed_or_missing_linked_worktree_gitfile_is_rejected(tmp_path: Path, gitfile: str):
    root = tmp_path / "linked-worktree"
    root.mkdir()
    (root / ".git").write_text(gitfile, encoding="utf-8")

    with pytest.raises(UpdateError, match="real Git checkout"):
        AutoUpdater(_spec(root))


def test_symlinked_root_and_gitfile_are_rejected(tmp_path: Path):
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(checkout, target_is_directory=True)

    with pytest.raises(UpdateError, match="real Git checkout"):
        AutoUpdater(_spec(linked_root))

    linked_gitdir = tmp_path / "linked-gitdir"
    linked_gitdir.symlink_to(checkout / ".git", target_is_directory=True)
    root_with_symlinked_gitdir = tmp_path / "symlinked-gitdir"
    root_with_symlinked_gitdir.mkdir()
    (root_with_symlinked_gitdir / ".git").symlink_to(linked_gitdir, target_is_directory=True)

    with pytest.raises(UpdateError, match="real Git checkout"):
        AutoUpdater(_spec(root_with_symlinked_gitdir))

    root = tmp_path / "symlinked-gitfile"
    root.mkdir()
    gitfile = tmp_path / "gitfile"
    gitfile.write_text(f"gitdir: {checkout / '.git'}\n", encoding="utf-8")
    (root / ".git").symlink_to(gitfile)

    with pytest.raises(UpdateError, match="real Git checkout"):
        AutoUpdater(_spec(root))


def _save(updater: AutoUpdater, mode: str) -> dict:
    return updater.save_settings({
        "mode": mode, "frequency": "daily", "maintenance_hour": 1,
        "idle_only": True,
    })


def test_default_is_off_and_idle_only(updater: AutoUpdater):
    assert updater.settings() == {
        "mode": "off", "frequency": "daily", "maintenance_hour": 1,
        "idle_only": True, "weekday": 6,
    }
    assert updater.public_status()["scheduler"]["installed"] is False


@pytest.mark.parametrize(("latest", "expected"), [
    ("0.9.9", False),
    ("1.0.0", False),
    ("1.0.1", True),
    ("v1.2.0", True),
    ("invalid", False),
])
def test_update_availability_never_offers_a_downgrade(
    updater: AutoUpdater, latest: str, expected: bool,
):
    updater._write_status(latest_version=latest)
    assert updater.public_status()["update_available"] is expected


def test_public_status_advertises_exact_dependency_convergence(updater: AutoUpdater):
    assert updater.public_status()["capabilities"] == {
        "managed_exact_commit": True,
        "dependency_convergence": 1,
    }


def test_settings_modes_install_and_remove_schedule(updater: AutoUpdater):
    assert _save(updater, "notify")["scheduler"]["installed"] is True
    assert _save(updater, "auto")["scheduler"]["installed"] is True
    status = _save(updater, "off")
    assert status["scheduler"]["installed"] is False
    assert status["next_check"] is None


def test_launchagent_enable_update_disable_and_removal(updater: AutoUpdater, monkeypatch, tmp_path):
    updater.agent_path = tmp_path / "com.kh.studiohub-test.autoupdate.plist"
    loaded = False
    calls = []

    def launchctl(*args):
        nonlocal loaded
        calls.append(args)
        if args[0] == "bootstrap":
            loaded = True
        elif args[0] == "bootout":
            loaded = False
        return subprocess.CompletedProcess(args, 0 if (args[0] != "print" or loaded) else 1, "", "")

    monkeypatch.setattr(updater, "_launchctl", launchctl)
    monkeypatch.setattr(updater, "_pinokio_home", lambda: tmp_path)
    monkeypatch.setattr(updater, "scheduler_status", lambda: {
        "installed": loaded, "supported": True, "label": updater.agent_label,
    })

    _save(updater, "notify")
    first = AutoUpdater.apply_scheduler(updater)
    assert first["installed"] is True and updater.agent_path.is_file()
    assert updater.wrapper_path.is_file()
    payload = plistlib.loads(updater.agent_path.read_bytes())
    assert payload["ProgramArguments"] == [str(updater.wrapper_path)]
    assert updater.wrapper_path.name == "studiohub-test-updater.sh"
    assert str(updater.root / "conda_env" / "bin" / "python") in updater.wrapper_path.read_text()
    first_contents = updater.agent_path.read_bytes()

    updater.save_settings({"mode": "auto", "frequency": "weekly",
                           "maintenance_hour": 23, "idle_only": True})
    second = AutoUpdater.apply_scheduler(updater)
    assert second["installed"] is True
    assert updater.agent_path.read_bytes() == first_contents
    assert sum(1 for call in calls if call[0] == "bootstrap") == 2

    _save(updater, "off")
    final = AutoUpdater.apply_scheduler(updater)
    assert final["installed"] is False
    assert not updater.agent_path.exists()
    assert not updater.wrapper_path.exists()


def test_named_wrapper_never_follows_a_precreated_temporary_symlink(
    updater, tmp_path,
):
    outside = tmp_path / "outside"
    outside.write_text("protected")
    updater.state_dir.mkdir()
    temporary = updater.wrapper_path.with_name(
        f".{updater.wrapper_path.name}.{os.getpid()}.tmp"
    )
    temporary.symlink_to(outside)

    with pytest.raises(FileExistsError):
        updater._write_wrapper()

    assert outside.read_text() == "protected"


def test_scheduler_reconciliation_does_not_run_while_update_lock_is_held(
    updater: AutoUpdater, monkeypatch: pytest.MonkeyPatch,
):
    calls = []
    monkeypatch.setattr(updater, "apply_scheduler", lambda: calls.append("bootout"))

    with updater._exclusive_lock():
        assert updater.apply_scheduler_if_idle() is False

    assert calls == []


def test_invalid_settings_are_rejected(updater: AutoUpdater):
    with pytest.raises(UpdateError):
        updater.save_settings({"mode": "always", "frequency": "daily",
                               "maintenance_hour": 2, "idle_only": True})
    with pytest.raises(UpdateError):
        updater.save_settings({"mode": "auto", "frequency": "daily",
                               "maintenance_hour": 24, "idle_only": True})


def test_notify_only_checks_but_does_not_install(updater: AutoUpdater, monkeypatch):
    _save(updater, "notify")
    updater._write_status(next_check="2000-01-01T00:00:00Z")
    monkeypatch.setattr(updater, "check", lambda: {"update_available": True, "latest_version": "2.0.0"})
    called = []
    monkeypatch.setattr(updater, "update", lambda **kwargs: called.append(kwargs))
    monkeypatch.setattr(updater, "_notify", lambda *args: called.append("notify"))
    updater.scheduled()
    assert called == ["notify"]


def test_auto_mode_installs_available_update(updater: AutoUpdater, monkeypatch):
    _save(updater, "auto")
    updater._write_status(next_check="2000-01-01T00:00:00Z")
    monkeypatch.setattr(updater, "check", lambda: {"update_available": True, "latest_version": "2.0.0"})
    called = []
    monkeypatch.setattr(updater, "update", lambda **kwargs: called.append(kwargs) or {"state": "succeeded"})
    updater.scheduled()
    assert called == [{"automatic": True}]


def test_active_work_defers_and_records_reason(updater: AutoUpdater, monkeypatch):
    monkeypatch.setattr(updater, "readiness_reasons", lambda: ["voice generation is running"])
    with pytest.raises(UpdateDeferred):
        updater.update(automatic=True)
    status = updater.public_status()
    assert status["state"] == "deferred"
    assert "voice generation" in status["defer_reason"]
    assert status["next_retry"]


def test_restart_safety_requires_loaded_service_and_clean_checkout(
    updater: AutoUpdater, monkeypatch,
):
    monkeypatch.setattr(updater, "_service_loaded", lambda: False)
    with pytest.raises(UpdateError, match="startup service"):
        updater.restart_safety()

    monkeypatch.setattr(updater, "_service_loaded", lambda: True)
    monkeypatch.setattr(updater, "_git_preflight", lambda **kwargs: {
        "local": "a" * 40, "remote": "a" * 40,
        "latest": "1.0.0", "available": False,
    })
    assert updater.restart_safety() == {
        "ready": True,
        "mode": "service",
        "expected_version": "1.0.0",
        "commit": "a" * 40,
    }

    def unsafe(**_kwargs):
        raise UpdateError("Working tree has local changes")

    monkeypatch.setattr(updater, "_git_preflight", unsafe)
    with pytest.raises(UpdateError, match="local changes"):
        updater.restart_safety()


def test_update_after_work_creates_pending_retry(updater: AutoUpdater, monkeypatch):
    monkeypatch.setattr(updater, "readiness_reasons", lambda: ["download active"])
    status = updater.trigger_update(after_current=True)
    assert status["pending_manual"] is True
    assert status["state"] == "deferred"


def test_managed_target_requires_all_three_fields(updater: AutoUpdater):
    """A partial managed tuple must never degrade into an ordinary update."""
    with pytest.raises(UpdateError, match="all be provided"):
        updater.trigger_update(target_commit="a" * 40)


@pytest.mark.parametrize("version", ["1.2", "01.2.3", "1.2.3.4", "1.2.3+build.1"])
def test_managed_target_requires_strict_release_semver(
    updater: AutoUpdater, version: str,
):
    with pytest.raises(UpdateError, match="target_version is invalid"):
        updater.trigger_update(
            target_commit="a" * 40, target_version=version, operation_id="hub-op-1",
        )


def test_same_operation_adopts_but_different_active_target_conflicts(
    updater: AutoUpdater, monkeypatch: pytest.MonkeyPatch,
):
    """Changing an in-flight operation's target must be rejected, never replaced."""
    spawned = []
    monkeypatch.setattr(
        updater, "_spawn",
        lambda *args: spawned.append(args) or SimpleNamespace(pid=os.getpid()),
    )
    request = {
        "target_commit": "a" * 40,
        "target_version": "2.8.0",
        "operation_id": "hub-op-1",
    }

    first = updater.trigger_update(**request)
    adopted = updater.trigger_update(**request)
    with pytest.raises(UpdateError, match="managed update operation is already active"):
        updater.trigger_update(
            target_commit="b" * 40, target_version="2.8.1", operation_id="hub-op-2",
        )

    assert first["managed_update"] == request
    assert adopted["managed_update"] == request
    assert len(spawned) == 1


def test_threaded_identical_requests_admit_one_managed_helper(
    updater: AutoUpdater, monkeypatch: pytest.MonkeyPatch,
):
    """The admission lock serializes same-process callers before helper spawn."""
    request = {
        "target_commit": "a" * 40,
        "target_version": "2.8.0",
        "operation_id": "hub-op-1",
    }
    spawned = []
    lock = threading.Lock()

    def spawn(*args):
        with lock:
            spawned.append(args)
        return SimpleNamespace(pid=os.getpid())

    monkeypatch.setattr(
        updater, "_spawn",
        spawn,
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _unused: updater.trigger_update(**request), range(2)))

    assert len(spawned) == 1
    assert all(result["managed_update"] == request for result in results)


def test_completed_managed_operation_is_idempotent_without_history_eviction(
    updater: AutoUpdater, monkeypatch: pytest.MonkeyPatch,
):
    """A completion ledger must preserve idempotency beyond a UI-sized history."""
    history = [
        {
            "target_commit": f"{index:x}" * 40,
            "target_version": "2.8.0",
            "operation_id": f"hub-op-{index}",
            "result": "succeeded",
        }
        for index in range(9)
    ]
    updater._write_status(managed_operation_history=history)
    monkeypatch.setattr(updater, "_spawn", lambda *_args: pytest.fail("completed operation must be adopted"))

    result = updater.trigger_update(
        target_commit="0" * 40, target_version="2.8.0", operation_id="hub-op-0",
    )

    assert result["managed_update"] is None


def test_dead_managed_operation_reinstalls_durable_recovery(
    updater: AutoUpdater, monkeypatch: pytest.MonkeyPatch,
):
    request = {
        "target_commit": "a" * 40,
        "target_version": "2.8.0",
        "operation_id": "hub-op-1",
    }
    updater._write_status(active_managed_update=request, pending_manual=True, managed_helper_pid=999_999)
    scheduled, spawned = [], []
    monkeypatch.setattr(updater, "apply_scheduler", lambda **kwargs: scheduled.append(kwargs) or {"installed": True})
    monkeypatch.setattr(
        updater, "_spawn",
        lambda *args: spawned.append(args) or SimpleNamespace(pid=os.getpid()),
    )

    updater.trigger_update(**request)

    assert scheduled == [{"force_pending": True}]
    assert len(spawned) == 1


def test_restart_recovery_replays_dependencies_and_starts_original_owner(
    updater: AutoUpdater, monkeypatch: pytest.MonkeyPatch,
):
    """After process death, a target checkout must recover using persisted mode/phase."""
    request = {
        "target_commit": "b" * 40,
        "target_version": "2.8.0",
        "operation_id": "hub-op-1",
    }
    updater._write_status(
        active_managed_update={
            **request, "run_mode": "service", "phase": "merged",
            "rollback_commit": "a" * 40, "rollback_version": "1.0.0",
        },
        pending_manual=True,
    )
    monkeypatch.setattr(updater, "active_mode", lambda: "stopped")
    monkeypatch.setattr(updater, "readiness_reasons", lambda **_kwargs: [])
    monkeypatch.setattr(updater, "_git_preflight", lambda **_kwargs: {
        "local": request["target_commit"], "remote": request["target_commit"],
        "latest": request["target_version"], "available": False,
    })
    calls = []
    monkeypatch.setattr(updater, "_install_dependencies", lambda: calls.append("install"))
    monkeypatch.setattr(updater, "_verify_import", lambda _version: calls.append("import"))
    monkeypatch.setattr(updater, "_start_mode", lambda mode: calls.append(f"start:{mode}"))
    monkeypatch.setattr(updater, "_verify_health", lambda *_args: calls.append("health") or True)

    updater.update(**request)

    assert calls == ["install", "import", "start:service", "health"]


@pytest.mark.parametrize("outcome", ["no-op", "failure", "success"])
def test_managed_terminal_paths_clear_retry_without_rescheduling_regular_check(
    updater: AutoUpdater, monkeypatch: pytest.MonkeyPatch, outcome: str,
):
    request = {
        "target_commit": "b" * 40,
        "target_version": "2.8.0",
        "operation_id": "hub-op-1",
    }
    regular_check = "2030-01-01T00:00:00Z"
    _save(updater, "auto")
    updater._write_status(
        active_managed_update={**request, "run_mode": "stopped", "phase": "prepared"},
        pending_manual=True, next_retry="2026-08-15T10:00:00Z", next_check=regular_check,
    )
    monkeypatch.setattr(updater, "active_mode", lambda: "stopped")
    monkeypatch.setattr(updater, "readiness_reasons", lambda **_kwargs: [])
    if outcome == "success":
        monkeypatch.setattr(updater, "_git_preflight", lambda **_kwargs: {
            "local": "a" * 40, "remote": request["target_commit"],
            "latest": request["target_version"], "available": True,
        })
        monkeypatch.setattr(updater, "_git", lambda *_args, **_kwargs: "")
        monkeypatch.setattr(updater, "_stop_mode", lambda _mode: None)
        monkeypatch.setattr(updater, "_install_dependencies", lambda: None)
        monkeypatch.setattr(updater, "_verify_import", lambda _version: None)
        monkeypatch.setattr(updater, "_start_mode", lambda _mode: None)
        monkeypatch.setattr(updater, "_verify_health", lambda *_args: True)
    else:
        monkeypatch.setattr(updater, "_git_preflight", lambda **_kwargs: {
            "local": request["target_commit"], "remote": request["target_commit"],
            "latest": request["target_version"], "available": False,
        })
        monkeypatch.setattr(updater, "_verify_health", lambda *_args: outcome == "no-op")

    if outcome == "failure":
        with pytest.raises(UpdateError):
            updater.update(**request)
    else:
        updater.update(**request)

    status = updater.public_status()
    assert status["next_retry"] is None
    assert status["next_check"] == regular_check


def test_managed_target_merges_requested_sha_not_origin_main(
    updater: AutoUpdater, monkeypatch: pytest.MonkeyPatch,
):
    """The checked target, not a later-moving main tip, is the merge argument."""
    target, main_tip, calls = "b" * 40, "c" * 40, []

    def fake_git(*args, **_kwargs):
        command = tuple(args)
        calls.append(command)
        if command == ("remote", "get-url", "origin"):
            return updater.spec["expected_remote"]
        if command[:3] == ("symbolic-ref", "--quiet", "--short"):
            return "main"
        if command[:2] == ("status", "--porcelain") or command[:1] == ("fetch",):
            return ""
        if command == ("rev-parse", "HEAD"):
            return "a" * 40
        if command == ("rev-parse", "origin/main"):
            return main_tip
        if command == ("rev-parse", "--verify", f"{target}^{{commit}}"):
            return target
        if command == ("show", f"{target}:VERSION"):
            return "2.8.0"
        if command[:1] == ("merge",):
            return ""
        raise AssertionError(command)

    monkeypatch.setattr(updater, "_git", fake_git)
    monkeypatch.setattr(updater, "_run", lambda args, **_kwargs: subprocess.CompletedProcess(args, 0, "", ""))
    monkeypatch.setattr(updater, "readiness_reasons", lambda **_kwargs: [])
    monkeypatch.setattr(updater, "active_mode", lambda: "stopped")
    monkeypatch.setattr(updater, "_stop_mode", lambda _mode: None)
    monkeypatch.setattr(updater, "_install_dependencies", lambda: None)
    monkeypatch.setattr(updater, "_verify_import", lambda _version: None)
    monkeypatch.setattr(updater, "_start_mode", lambda _mode: None)
    monkeypatch.setattr(updater, "_verify_health", lambda *_args: True)

    updater.update(target_commit=target, target_version="2.8.0", operation_id="hub-op-1")

    assert ("merge", "--ff-only", target) in calls
    assert ("merge", "--ff-only", "origin/main") not in calls


@pytest.mark.parametrize(("case", "message"), [
    ("unknown", "did not resolve"),
    ("not_on_main", "not an ancestor of origin/main"),
    ("local_diverged", "Local and remote history diverged"),
    ("version_mismatch", "VERSION does not match"),
    ("rewritten", "Remote history was rewritten"),
])
def test_managed_preflight_refuses_unsafe_exact_target(
    updater: AutoUpdater, monkeypatch: pytest.MonkeyPatch, case: str, message: str,
):
    """A managed target must pass every exact-target safety boundary before stop."""
    target, main_tip, local, previous = "b" * 40, "c" * 40, "a" * 40, "d" * 40
    if case == "rewritten":
        updater._write_status(last_remote_commit=previous)

    def fake_git(*args, **_kwargs):
        command = tuple(args)
        if command == ("remote", "get-url", "origin"):
            return updater.spec["expected_remote"]
        if command[:3] == ("symbolic-ref", "--quiet", "--short"):
            return "main"
        if command[:2] == ("status", "--porcelain") or command[:1] == ("fetch",):
            return ""
        if command == ("rev-parse", "HEAD"):
            return local
        if command == ("rev-parse", "origin/main"):
            return main_tip
        if command == ("rev-parse", "--verify", f"{target}^{{commit}}"):
            return "e" * 40 if case == "unknown" else target
        if command == ("show", f"{target}:VERSION"):
            return "2.8.1" if case == "version_mismatch" else "2.8.0"
        raise AssertionError(command)

    def fake_run(args, **_kwargs):
        if "merge-base" not in args:
            return subprocess.CompletedProcess(args, 0, "", "")
        ancestor, descendant = args[-2:]
        rejected = (
            (case == "not_on_main" and (ancestor, descendant) == (target, main_tip))
            or (case == "local_diverged" and (ancestor, descendant) == (local, target))
            or (case == "rewritten" and (ancestor, descendant) == (previous, main_tip))
        )
        return subprocess.CompletedProcess(args, int(rejected), "", "")

    monkeypatch.setattr(updater, "_git", fake_git)
    monkeypatch.setattr(updater, "_run", fake_run)

    with pytest.raises(UpdateError, match=message):
        updater._git_preflight(target_commit=target, target_version="2.8.0")


@pytest.mark.parametrize("response", ["unavailable", "malformed"])
def test_managed_readiness_fails_closed_while_ordinary_readiness_stays_legacy(
    updater: AutoUpdater, monkeypatch: pytest.MonkeyPatch, response: str,
):
    """No live-readiness failure mode is evidence that a managed Hub is idle."""
    if response == "unavailable":
        monkeypatch.setattr(
            "backend.auto_update.urlopen",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("readiness unavailable")),
        )
    else:
        class MalformedResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            @staticmethod
            def read():
                return b'{"reasons":"not-a-list"}'

        monkeypatch.setattr("backend.auto_update.urlopen", lambda *_args, **_kwargs: MalformedResponse())
    monkeypatch.setattr(updater, "_service_loaded", lambda: True)

    assert updater.readiness_reasons() == []
    assert updater.readiness_reasons(managed=True) == [
        "the update safety check is unavailable and Studio Hub is not confirmed stopped"
    ]


def test_busy_managed_update_persists_full_tuple_for_retry(
    updater: AutoUpdater, monkeypatch: pytest.MonkeyPatch,
):
    request = {"target_commit": "a" * 40, "target_version": "2.8.0", "operation_id": "hub-op-1"}
    monkeypatch.setattr(updater, "readiness_reasons", lambda **_kwargs: ["fleet work active"])

    with pytest.raises(UpdateDeferred):
        updater.update(**request)

    status = updater.public_status()
    assert status["pending_manual"] is True
    assert status["managed_update"] == request


def test_settings_off_keeps_active_managed_recovery_armed(
    updater: AutoUpdater, monkeypatch: pytest.MonkeyPatch,
):
    request = {"target_commit": "a" * 40, "target_version": "2.8.0", "operation_id": "hub-op-1"}
    updater._write_status(active_managed_update={**request, "requested_commit": request["target_commit"]},
                          pending_manual=True, next_retry="2026-08-15T10:00:00Z")
    schedules = []
    monkeypatch.setattr(
        updater,
        "apply_scheduler",
        lambda **kwargs: schedules.append(kwargs) or {"installed": True, "forced_by_managed": True},
    )

    status = _save(updater, "off")

    assert schedules == [{}]
    assert status["pending_manual"] is True
    assert status["managed_update"] == request
    assert status["next_retry"] == "2026-08-15T10:00:00Z"


def test_concurrent_off_settings_cannot_unload_accepted_managed_recovery(
    updater: AutoUpdater, monkeypatch: pytest.MonkeyPatch,
):
    """Off-mode scheduler removal must serialize with managed admission."""
    request = {"target_commit": "a" * 40, "target_version": "2.8.0", "operation_id": "hub-op-1"}
    ordinary_apply_started, release_ordinary_apply = threading.Event(), threading.Event()
    updater.agent_path = updater.root / "managed-recovery.plist"
    monkeypatch.setattr(updater, "_pinokio_home", lambda: updater.root.parent)
    monkeypatch.setattr(
        updater, "_launchctl",
        lambda *_args: subprocess.CompletedProcess(_args, 0, "", ""),
    )
    monkeypatch.setattr(updater, "scheduler_status", lambda: {
        "installed": updater.agent_path.exists(), "supported": True, "label": updater.agent_label,
    })
    original_apply = AutoUpdater.apply_scheduler

    def apply_scheduler(*, force_pending=False):
        if not force_pending:
            ordinary_apply_started.set()
            assert release_ordinary_apply.wait(timeout=2)
        return original_apply(updater, force_pending=force_pending)

    monkeypatch.setattr(updater, "apply_scheduler", apply_scheduler)
    monkeypatch.setattr(updater, "_spawn", lambda *_args: SimpleNamespace(pid=os.getpid()))
    saved, admitted = [], []
    save_thread = threading.Thread(target=lambda: saved.append(_save(updater, "off")))
    save_thread.start()
    assert ordinary_apply_started.wait(timeout=2)
    admission_thread = threading.Thread(target=lambda: admitted.append(updater.trigger_update(**request)))
    admission_thread.start()
    for _ in range(20):
        if admitted:
            break
        threading.Event().wait(0.05)
    assert admitted, "managed admission must complete before stale Off scheduler application resumes"
    release_ordinary_apply.set()
    save_thread.join(timeout=2)
    admission_thread.join(timeout=2)

    assert not save_thread.is_alive()
    assert not admission_thread.is_alive()
    assert saved and admitted
    assert updater.agent_path.exists()
    assert updater.public_status()["managed_update"] == request


def test_off_settings_succeeds_when_managed_completion_follows_forced_scheduler_apply(
    updater: AutoUpdater, monkeypatch: pytest.MonkeyPatch,
):
    """Settings must validate the scheduler decision, not a later active snapshot."""
    request = {"target_commit": "a" * 40, "target_version": "2.8.0", "operation_id": "hub-op-1"}
    updater._write_status(active_managed_update={**request, "requested_commit": request["target_commit"]},
                          pending_manual=True)
    updater.agent_path = updater.root / "managed-recovery.plist"
    monkeypatch.setattr(updater, "_pinokio_home", lambda: updater.root.parent)
    monkeypatch.setattr(
        updater, "_launchctl",
        lambda *_args: subprocess.CompletedProcess(_args, 0, "", ""),
    )
    monkeypatch.setattr(updater, "scheduler_status", lambda: {
        "installed": updater.agent_path.exists(), "supported": True, "label": updater.agent_label,
    })
    original_apply = AutoUpdater.apply_scheduler

    def complete_after_forced_apply(*, force_pending=False):
        result = original_apply(updater, force_pending=force_pending)
        if result["forced_by_managed"]:
            updater._write_status(active_managed_update=None, pending_manual=False, next_retry=None)
            # Mirror terminal Off-mode cleanup before save_settings validates its result.
            original_apply(updater)
        return result

    monkeypatch.setattr(updater, "apply_scheduler", complete_after_forced_apply)

    status = _save(updater, "off")

    assert status["managed_update"] is None
    assert not updater.agent_path.exists()


def test_scheduler_adopts_active_managed_tuple_even_when_ordinary_mode_is_off(
    updater: AutoUpdater, monkeypatch: pytest.MonkeyPatch,
):
    request = {"target_commit": "a" * 40, "target_version": "2.8.0", "operation_id": "hub-op-1"}
    updater._write_status(active_managed_update={**request, "requested_commit": request["target_commit"]},
                          pending_manual=False, next_retry="2000-01-01T00:00:00Z")
    calls = []
    monkeypatch.setattr(updater, "update", lambda **kwargs: calls.append(kwargs) or {"state": "succeeded"})

    updater.scheduled()

    assert calls == [{"automatic": False, **request}]


def test_concurrent_check_completion_cannot_erase_admitted_managed_tuple(
    updater: AutoUpdater, monkeypatch: pytest.MonkeyPatch,
):
    """A check's stale read/replace cannot discard a request admitted mid-check."""
    request = {"target_commit": "a" * 40, "target_version": "2.8.0", "operation_id": "hub-op-1"}
    monkeypatch.setattr(updater, "_git_preflight", lambda **_kwargs: {
        "local": "a" * 40, "remote": "a" * 40, "latest": "2.8.0", "available": False,
    })
    monkeypatch.setattr(updater, "_spawn", lambda *_args: SimpleNamespace(pid=os.getpid()))
    import backend.auto_update as auto_update
    original_atomic = auto_update._atomic_json
    check_has_read, finish_check = threading.Event(), threading.Event()

    def pause_check_write(path, payload):
        if payload.get("last_update_result") == "Already up to date":
            check_has_read.set()
            assert finish_check.wait(timeout=2)
        original_atomic(path, payload)

    monkeypatch.setattr(auto_update, "_atomic_json", pause_check_write)
    worker = threading.Thread(target=updater.check)
    worker.start()
    assert check_has_read.wait(timeout=2)
    admitted = []
    admission = threading.Thread(target=lambda: admitted.append(updater.trigger_update(**request)))
    admission.start()
    finish_check.set()
    worker.join(timeout=2)
    admission.join(timeout=2)

    assert not worker.is_alive()
    assert not admission.is_alive()
    assert admitted
    assert updater.public_status()["managed_update"] == request


def test_managed_history_records_commit_lifecycle(
    updater: AutoUpdater, monkeypatch: pytest.MonkeyPatch,
):
    """The terminal idempotency ledger retains auditable requested/start/end commits."""
    request = {"target_commit": "b" * 40, "target_version": "2.8.0", "operation_id": "hub-op-1"}
    monkeypatch.setattr(updater, "readiness_reasons", lambda **_kwargs: [])
    monkeypatch.setattr(updater, "active_mode", lambda: "stopped")
    monkeypatch.setattr(updater, "_git_preflight", lambda **_kwargs: {
        "local": "a" * 40, "remote": request["target_commit"],
        "latest": request["target_version"], "available": True,
    })
    monkeypatch.setattr(updater, "_stop_mode", lambda _mode: None)
    monkeypatch.setattr(updater, "_git", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(updater, "_install_dependencies", lambda: None)
    monkeypatch.setattr(updater, "_verify_import", lambda _version: None)
    monkeypatch.setattr(updater, "_start_mode", lambda _mode: None)
    monkeypatch.setattr(updater, "_verify_health", lambda *_args: True)

    updater.update(**request)

    history = updater._read_status()["managed_operation_history"]
    assert len(history) == 1
    assert {key: history[0][key] for key in (*request, "requested_commit", "started_commit",
                                              "completed_commit", "rollback_commit", "result")} == {
        **request,
        "requested_commit": "b" * 40,
        "started_commit": "a" * 40,
        "completed_commit": "b" * 40,
        "rollback_commit": "a" * 40,
        "result": "succeeded",
    }


def test_concurrent_update_lock_is_refused(updater: AutoUpdater):
    with updater._exclusive_lock():
        with pytest.raises(UpdateError, match="already running"):
            with updater._exclusive_lock():
                pass


@pytest.mark.parametrize("case, message", [
    ("remote", "Unexpected Git remote"),
    ("branch", "configured main branch"),
    ("dirty", "local changes"),
    ("diverged", "diverged"),
])
def test_git_safety_refusals(updater: AutoUpdater, monkeypatch, case, message):
    def fake_git(*args, **kwargs):
        command = tuple(args)
        if command == ("remote", "get-url", "origin"):
            return "https://github.com/attacker/wrong.git" if case == "remote" else updater.spec["expected_remote"]
        if command[:3] == ("symbolic-ref", "--quiet", "--short"):
            return "feature" if case == "branch" else "main"
        if command[:2] == ("status", "--porcelain"):
            return " M local.txt" if case == "dirty" else ""
        if command[:1] == ("fetch",):
            return ""
        if command == ("rev-parse", "HEAD"):
            return "a" * 40
        if command == ("rev-parse", "origin/main"):
            return "b" * 40
        if command[:1] == ("show",):
            return "2.0.0"
        raise AssertionError(command)
    monkeypatch.setattr(updater, "_git", fake_git)
    def fake_run(args, **kwargs):
        rc = 1 if case == "diverged" and "merge-base" in args else 0
        return subprocess.CompletedProcess(args, rc, "", "")
    monkeypatch.setattr(updater, "_run", fake_run)
    with pytest.raises(UpdateError, match=message):
        updater._git_preflight()


def test_dirty_checkout_message_preserves_porcelain_columns_and_filenames(
    updater: AutoUpdater,
):
    porcelain = " M worktree.txt\nM  index.txt\n?? untracked.txt\nR  old.txt -> new.txt\n"

    def runner(args, **kwargs):
        command = tuple(args[1:])
        stdout = {
            ("remote", "get-url", "origin"): updater.spec["expected_remote"] + "\n",
            ("symbolic-ref", "--quiet", "--short", "HEAD"): "main\n",
            ("status", "--porcelain", "--untracked-files=normal"): porcelain,
        }.get(command)
        if stdout is None:
            raise AssertionError(command)
        return subprocess.CompletedProcess(args, 0, stdout, "")

    updater.runner = runner

    assert updater._git("status", "--porcelain", "--untracked-files=normal") == porcelain.rstrip()
    with pytest.raises(UpdateError) as error:
        updater._git_preflight()
    message = str(error.value)
    assert "worktree.txt" in message
    assert "index.txt" in message
    assert "untracked.txt" in message
    assert "old.txt -> new.txt" in message
    assert "orktree.txt" not in message.replace("worktree.txt", "")


def test_disk_space_failure_happens_before_files_change(updater: AutoUpdater, monkeypatch):
    monkeypatch.setattr(updater, "readiness_reasons", lambda: [])
    monkeypatch.setattr("backend.auto_update.shutil.disk_usage", lambda _p: type("D", (), {"free": 1})())
    monkeypatch.setattr(updater, "_git_preflight", lambda **kwargs: pytest.fail("Git update must not start"))
    with pytest.raises(UpdateError, match="disk space"):
        updater.update()


@pytest.mark.parametrize("failure", ["dependencies", "health"])
def test_install_or_health_failure_attempts_rollback(updater: AutoUpdater, monkeypatch, failure):
    monkeypatch.setattr(updater, "readiness_reasons", lambda: [])
    monkeypatch.setattr(updater, "active_mode", lambda: "stopped")
    monkeypatch.setattr(updater, "_git_preflight", lambda **kwargs: {
        "local": "a" * 40, "remote": "b" * 40, "latest": "2.0.0", "available": True,
    })
    monkeypatch.setattr(updater, "_stop_mode", lambda mode: None)
    monkeypatch.setattr(updater, "_git", lambda *args, **kwargs: "")
    monkeypatch.setattr(updater, "_verify_import", lambda expected: None)
    monkeypatch.setattr(updater, "_start_mode", lambda mode: None)
    monkeypatch.setattr(updater, "_verify_health", lambda mode, version: failure != "health")
    if failure == "dependencies":
        monkeypatch.setattr(updater, "_install_dependencies", lambda: (_ for _ in ()).throw(UpdateError("dependency install failed")))
    else:
        monkeypatch.setattr(updater, "_install_dependencies", lambda: None)
    rollbacks = []
    monkeypatch.setattr(updater, "_rollback", lambda *args: rollbacks.append(args) or True)
    with pytest.raises(UpdateError):
        updater.update()
    assert len(rollbacks) == 1
    assert updater.public_status()["rollback"] == "succeeded"


def test_install_dependencies_uses_only_the_convergence_module(
    updater: AutoUpdater, monkeypatch: pytest.MonkeyPatch,
):
    module = updater.root / "app" / "backend" / "dependency_convergence.py"
    module.parent.mkdir()
    module.touch()
    calls = []
    monkeypatch.setattr(
        updater, "_run",
        lambda args, **kwargs: calls.append((args, kwargs))
        or subprocess.CompletedProcess(args, 0, "", ""),
    )

    updater._install_dependencies()

    assert calls == [(
        [str(updater._python()), "-m", "backend.dependency_convergence", "all-installed"],
        {"cwd": updater.root / "app", "timeout": 1200},
    )]


def test_install_dependencies_requires_convergence_module(
    updater: AutoUpdater, monkeypatch: pytest.MonkeyPatch,
):
    calls = []
    monkeypatch.setattr(updater, "_run", lambda *args, **kwargs: calls.append((args, kwargs)))

    with pytest.raises(UpdateError, match="Dependency convergence command is unavailable"):
        updater._install_dependencies()

    assert calls == []


def test_rollback_restores_old_tree_with_fixed_compatibility_command_only(
    updater: AutoUpdater, monkeypatch: pytest.MonkeyPatch,
):
    uv = updater.root / "pinokio" / "bin" / "miniforge" / "bin" / "uv"
    uv.parent.mkdir(parents=True)
    uv.touch()
    (updater.root / "app" / "requirements.lock").write_text("fastapi\n")
    calls = []

    def fake_git(*args, **_kwargs):
        if args == ("rev-parse", "HEAD"):
            return "b" * 40
        if args[:2] == ("status", "--porcelain"):
            return ""
        return ""

    monkeypatch.setattr(updater, "_git", fake_git)
    monkeypatch.setattr(updater, "_pinokio_home", lambda: updater.root / "pinokio")
    monkeypatch.setattr(updater, "_stop_mode", lambda _mode: None)
    monkeypatch.setattr(updater, "_start_mode", lambda _mode: None)
    monkeypatch.setattr(updater, "_verify_import", lambda _version: None)
    monkeypatch.setattr(updater, "_verify_health", lambda *_args: True)
    monkeypatch.setattr(
        updater, "_run",
        lambda args, **kwargs: calls.append((args, kwargs))
        or subprocess.CompletedProcess(args, 0, "", ""),
    )

    assert updater._rollback("a" * 40, "b" * 40, "stopped", "1.0.0") is True
    assert calls == [(
        [str(uv), "pip", "install", "--python", str(updater._python()),
         "-r", str(updater.root / "app" / "requirements.lock")],
        {"cwd": updater.root / "app", "timeout": 1200},
    )]


def test_rollback_failure_is_reported(updater: AutoUpdater, monkeypatch):
    monkeypatch.setattr(updater, "readiness_reasons", lambda: [])
    monkeypatch.setattr(updater, "active_mode", lambda: "stopped")
    monkeypatch.setattr(updater, "_git_preflight", lambda **kwargs: {
        "local": "a" * 40, "remote": "b" * 40, "latest": "2.0.0", "available": True,
    })
    monkeypatch.setattr(updater, "_stop_mode", lambda mode: None)
    monkeypatch.setattr(updater, "_git", lambda *args, **kwargs: "")
    monkeypatch.setattr(updater, "_install_dependencies", lambda: (_ for _ in ()).throw(UpdateError("boom")))
    monkeypatch.setattr(updater, "_rollback", lambda *args: False)
    with pytest.raises(UpdateError):
        updater.update()
    assert updater.public_status()["rollback"] == "failed"


def test_service_and_pinokio_modes_restart_only_their_owner(updater: AutoUpdater, monkeypatch):
    calls = []
    monkeypatch.setattr(updater, "_run", lambda args, **kwargs: calls.append(tuple(args)) or subprocess.CompletedProcess(args, 0, "", ""))
    monkeypatch.setattr(updater, "_pterm", lambda action: calls.append(("pterm", action)))
    updater._start_mode("service")
    updater._start_mode("pinokio")
    assert calls == [("/bin/bash", "install_service.sh"), ("pterm", "start")]


def test_secrets_are_redacted():
    value = _redact({"hf_token": "hf_secret", "details": "Authorization: Bearer-abc"})
    assert value["hf_token"] == "[redacted]"
    assert "Bearer-abc" not in value["details"]


def test_next_daily_and_weekly_checks_are_future(updater: AutoUpdater):
    now = dt.datetime(2026, 7, 15, 10, tzinfo=dt.timezone.utc)
    updater.now = lambda: now
    daily = updater._next_regular({**updater.defaults, "frequency": "daily", "maintenance_hour": 2})
    weekly = updater._next_regular({**updater.defaults, "frequency": "weekly", "maintenance_hour": 2})
    assert daily > now
    assert weekly > daily


def test_build_suffix_version_matching(updater: AutoUpdater):
    updater.spec["allow_build_suffix"] = True
    assert updater._version_matches("1.22.0.abcdef0", "1.22.0")
    assert not updater._version_matches("1.21.9.abcdef0", "1.22.0")
