from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock


KIT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT_ROOT))

import runtime_state_migration
from runtime_state_migration import (
    APPROVED_ENVIRONMENT_LINES,
    APP_SPECS,
    LEGACY_HUB_EXCLUDES,
    MigrationEngine,
)


def git(*arguments: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=cwd, check=True, text=True, capture_output=True
    )
    return result.stdout


def configure_checkout(path: Path) -> None:
    git("config", "user.email", "fixture@example.invalid", cwd=path)
    git("config", "user.name", "Fixture", cwd=path)


def make_remote(root: Path, name: str, *, old_environment: bool = False) -> tuple[Path, Path]:
    remote = root / f"{name}.remote.git"
    seed = root / f"{name}.seed"
    git("init", "--bare", str(remote), cwd=root)
    git("init", "--initial-branch=main", str(seed), cwd=root)
    configure_checkout(seed)
    (seed / "update.js").write_text("module.exports = {};\n", encoding="utf-8")
    (seed / "README.md").write_text("fixture\n", encoding="utf-8")
    if old_environment:
        (seed / "ENVIRONMENT").write_text("SETTING=base\n", encoding="utf-8")
    else:
        (seed / ".gitignore").write_text("", encoding="utf-8")
    git("add", ".", cwd=seed)
    git("commit", "-m", "old state", cwd=seed)
    git("remote", "add", "origin", str(remote), cwd=seed)
    git("push", "-u", "origin", "main", cwd=seed)
    target = root / "home" / "api" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    git("clone", str(remote), str(target), cwd=root)
    return remote, target


def publish_durable_update(root: Path, name: str, remote: Path, *, environment: bool) -> None:
    updater = root / f"{name}.updater"
    git("clone", str(remote), str(updater), cwd=root)
    configure_checkout(updater)
    if environment:
        git("mv", "ENVIRONMENT", "ENVIRONMENT.example", cwd=updater)
        (updater / ".gitignore").write_text("/ENVIRONMENT\n", encoding="utf-8")
    else:
        (updater / ".gitignore").write_text(
            "/.enrollment_repair_journal.json\n", encoding="utf-8"
        )
    git("add", ".", cwd=updater)
    git("commit", "-m", "durable runtime state", cwd=updater)
    git("push", cwd=updater)


class RuntimeStateMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.backup_root = self.root / "backups"
        self.pterm = self.root / "pterm"
        self.pterm.write_text("#!/bin/sh\n", encoding="utf-8")
        self.pterm.chmod(0o755)
        self.calls: list[Path] = []
        self.remotes: dict[str, Path] = {}

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_engine(self, **overrides: object) -> MigrationEngine:
        def update_runner(target: Path, pterm: Path) -> str:
            self.assertEqual(pterm, self.pterm)
            self.calls.append(target)
            return "dependency convergence: complete\n"

        arguments = {
            "home": self.home,
            "backup_root": self.backup_root,
            "pterm_resolver": lambda _home: self.pterm,
            "control_plane_ready": lambda: True,
            "update_runner": update_runner,
            "activity_probe": lambda _app: None,
            "post_update_probe": lambda _app: None,
            "app_specs": tuple(
                (name, title, str(self.remotes[name]), port)
                for name, title, _url, port in (
                    ("imagestudio-mac", "Image Studio", "", 47868),
                    ("voicestudio-mac", "Voice Studio", "", 47870),
                    ("studiohub-mac", "Studio Hub", "", 47873),
                )
            ),
        }
        arguments.update(overrides)
        return MigrationEngine(**arguments)

    def test_default_update_runner_uses_pinokio_node_under_launchd_path(self) -> None:
        target = self.home / "api" / "voicestudio-mac"
        target.mkdir(parents=True)
        node = self.home / "bin/miniforge/bin/node"
        node.parent.mkdir(parents=True)
        node.write_text("#!/bin/sh\n", encoding="utf-8")
        node.chmod(0o755)
        calls: list[tuple[tuple[str, ...], Path | None]] = []

        def runner(command: object, cwd: Path | None = None) -> subprocess.CompletedProcess[bytes]:
            calls.append((tuple(str(part) for part in command), cwd))
            return subprocess.CompletedProcess(command, 0, b"updated\n", b"")

        output = MigrationEngine(home=self.home, runner=runner)._run_update(target, self.pterm)

        self.assertEqual(output, "updated\n")
        self.assertEqual(calls, [((
            str(node), str(self.pterm), "start", "update.js", "--ref",
            "pinokio://127.0.0.1:42000/api/voicestudio-mac",
        ), None)])

    def prepare_old_fleet(self, *, legacy_voice: bool = False) -> dict[str, Path]:
        targets: dict[str, Path] = {}
        for name, environment in (
            ("imagestudio-mac", True),
            ("voicestudio-mac.git" if legacy_voice else "voicestudio-mac", True),
            ("studiohub-mac", False),
        ):
            canonical = name.removesuffix(".git")
            remote, target = make_remote(self.root, canonical, old_environment=environment)
            self.remotes[canonical] = remote
            if name.endswith(".git"):
                legacy = target.with_name(name)
                target.rename(legacy)
                target = legacy
            publish_durable_update(self.root, canonical, remote, environment=environment)
            targets[canonical] = target
        for name in ("imagestudio-mac", "voicestudio-mac"):
            (targets[name] / "ENVIRONMENT").write_text(
                "SETTING=base\n" + "\n".join(APPROVED_ENVIRONMENT_LINES) + "\n",
                encoding="utf-8",
            )
        hub = targets["studiohub-mac"]
        (hub / ".enrollment_repair_journal.json.lock").write_text("", encoding="utf-8")
        (hub / "controller_settings.json.repair.lock").write_text("", encoding="utf-8")
        return targets

    def test_migrates_exact_old_runtime_state_and_preserves_backup_before_update(self) -> None:
        targets = self.prepare_old_fleet()
        original = (targets["imagestudio-mac"] / "ENVIRONMENT").read_bytes()

        report = self.make_engine().run()

        self.assertTrue(report.ok, report)
        self.assertEqual(self.calls, [
            targets["imagestudio-mac"], targets["voicestudio-mac"], targets["studiohub-mac"],
        ])
        for name in ("imagestudio-mac", "voicestudio-mac"):
            target = targets[name]
            self.assertEqual((target / "ENVIRONMENT").read_bytes(), original)
            self.assertEqual(git("status", "--porcelain=v1", "-z", cwd=target), "")
            self.assertEqual(git("check-ignore", "-q", "ENVIRONMENT", cwd=target), "")
            result = report.for_name(name)
            self.assertEqual(result.status, "migrated")
            self.assertIsNotNone(result.backup_path)
            self.assertEqual(stat.S_IMODE(result.backup_path.stat().st_mode), 0o600)
            claim_directory = Path(
                git("rev-parse", "--git-path", "runtime-state-migration", cwd=target).strip()
            )
            if not claim_directory.is_absolute():
                claim_directory = target / claim_directory
            self.assertEqual(list(claim_directory.iterdir()), [])
        excludes = (targets["studiohub-mac"] / ".git/info/exclude").read_text(encoding="utf-8")
        self.assertTrue(all(pattern in excludes for pattern in LEGACY_HUB_EXCLUDES))
        self.assertTrue((targets["studiohub-mac"] / ".enrollment_repair_journal.json.lock").exists())

    def test_migrates_a_clean_old_environment_to_ignored_machine_state(self) -> None:
        targets = self.prepare_old_fleet()
        for name in ("imagestudio-mac", "voicestudio-mac"):
            (targets[name] / "ENVIRONMENT").write_text("SETTING=base\n", encoding="utf-8")

        report = self.make_engine().run()

        self.assertTrue(report.ok, report)
        for name in ("imagestudio-mac", "voicestudio-mac"):
            self.assertEqual((targets[name] / "ENVIRONMENT").read_text(encoding="utf-8"), "SETTING=base\n")
            self.assertEqual(report.for_name(name).status, "migrated")

    def test_refuses_unknown_environment_bytes_without_mutating_any_repository(self) -> None:
        targets = self.prepare_old_fleet()
        environment = targets["voicestudio-mac"] / "ENVIRONMENT"
        environment.write_text("SETTING=operator-value\n", encoding="utf-8")

        report = self.make_engine().plan()

        refusal = report.for_name("voicestudio-mac")
        self.assertEqual(refusal.status, "refused")
        self.assertIn("ENVIRONMENT", refusal.refusal_reason or "")
        self.assertEqual(self.calls, [])
        self.assertEqual(environment.read_text(encoding="utf-8"), "SETTING=operator-value\n")
        self.assertFalse(self.backup_root.exists())

    def test_refuses_unknown_dirty_path_using_nul_safe_status(self) -> None:
        targets = self.prepare_old_fleet()
        unknown = targets["imagestudio-mac"] / "operator\nnotes.txt"
        unknown.write_text("keep\n", encoding="utf-8")

        report = self.make_engine().plan()

        refusal = report.for_name("imagestudio-mac")
        self.assertEqual(refusal.status, "refused")
        self.assertIn("operator", refusal.refusal_reason or "")
        self.assertTrue(unknown.exists())
        self.assertEqual(self.calls, [])

    def test_refuses_renames_with_both_nul_delimited_paths(self) -> None:
        targets = self.prepare_old_fleet()
        git("mv", "README.md", "renamed.md", cwd=targets["imagestudio-mac"])

        report = self.make_engine().plan()

        refusal = report.for_name("imagestudio-mac")
        self.assertEqual(refusal.status, "refused")
        self.assertIn("renamed.md", refusal.refusal_reason or "")
        self.assertIn("README.md", refusal.refusal_reason or "")

    def test_uses_legacy_checkout_name_and_already_migrated_state_is_idempotent(self) -> None:
        targets = self.prepare_old_fleet(legacy_voice=True)
        engine = self.make_engine()

        first = engine.run()
        self.assertTrue(first.ok, first)
        calls_after_first = list(self.calls)
        excludes = (targets["studiohub-mac"] / ".git/info/exclude").read_bytes()
        for name in ("imagestudio-mac", "voicestudio-mac"):
            (targets[name] / "ENVIRONMENT").unlink()
        second = engine.run()

        self.assertTrue(second.ok, second)
        self.assertEqual([result.status for result in second.repositories], [
            "already_ready", "already_ready", "already_ready",
        ])
        self.assertEqual(self.calls, calls_after_first)
        self.assertEqual((targets["studiohub-mac"] / ".git/info/exclude").read_bytes(), excludes)

    def test_interrupted_update_restores_backup_and_stops_before_hub(self) -> None:
        targets = self.prepare_old_fleet()
        original = (targets["imagestudio-mac"] / "ENVIRONMENT").read_bytes()

        def failing_update(target: Path, _pterm: Path) -> str:
            self.calls.append(target)
            raise RuntimeError("update interrupted")

        report = self.make_engine(update_runner=failing_update).run()

        self.assertFalse(report.ok)
        self.assertEqual(self.calls, [targets["imagestudio-mac"]])
        self.assertEqual((targets["imagestudio-mac"] / "ENVIRONMENT").read_bytes(), original)
        self.assertTrue(report.for_name("imagestudio-mac").backup_path.exists())
        self.assertEqual(report.for_name("voicestudio-mac").status, "not_run")
        self.assertEqual(report.for_name("studiohub-mac").status, "not_run")

    def test_refuses_an_update_that_changes_the_restored_machine_environment(self) -> None:
        targets = self.prepare_old_fleet()
        environment = targets["imagestudio-mac"] / "ENVIRONMENT"
        environment.chmod(0o640)
        original = environment.read_bytes()

        def damaging_update(target: Path, _pterm: Path) -> str:
            self.calls.append(target)
            (target / "ENVIRONMENT").write_text("lost=true\n", encoding="utf-8")
            return "dependency convergence: complete\n"

        report = self.make_engine(update_runner=damaging_update).run()

        result = report.for_name("imagestudio-mac")
        self.assertEqual(result.status, "failed")
        self.assertIn("did not preserve", result.refusal_reason or "")
        self.assertEqual(
            result.backup_path.read_text(encoding="utf-8"),
            "SETTING=base\n" + "\n".join(APPROVED_ENVIRONMENT_LINES) + "\n",
        )
        self.assertEqual(environment.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(environment.stat().st_mode), 0o640)
        self.assertEqual(self.calls, [targets["imagestudio-mac"]])
        claim_directory = Path(
            git("rev-parse", "--git-path", "runtime-state-migration", cwd=targets["imagestudio-mac"]).strip()
        )
        if not claim_directory.is_absolute():
            claim_directory = targets["imagestudio-mac"] / claim_directory
        self.assertTrue(any(path.read_bytes() == b"lost=true\n" for path in claim_directory.iterdir()))

    def test_post_update_concurrent_environment_replacement_fails_closed(self) -> None:
        targets = self.prepare_old_fleet()
        environment = targets["imagestudio-mac"] / "ENVIRONMENT"

        def replacing_update(target: Path, _pterm: Path) -> str:
            self.calls.append(target)
            replacement = target / ".ENVIRONMENT.concurrent"
            replacement.write_text("external=true\n", encoding="utf-8")
            os.replace(replacement, target / "ENVIRONMENT")
            return "done\n"

        report = self.make_engine(update_runner=replacing_update).run()

        result = report.for_name("imagestudio-mac")
        self.assertEqual(result.status, "failed")
        self.assertIn("concurrently replaced", result.refusal_reason or "")
        self.assertEqual(environment.read_text(encoding="utf-8"), "external=true\n")

    def test_post_update_recovery_never_overwrites_replacement_after_identity_check(self) -> None:
        targets = self.prepare_old_fleet()
        environment = targets["imagestudio-mac"] / "ENVIRONMENT"
        replacement = b"operator replacement after recovery identity check\n"
        update_failed = False

        def failing_update(target: Path, _pterm: Path) -> str:
            nonlocal update_failed
            self.calls.append(target)
            update_failed = True
            raise RuntimeError("simulated update failure")

        engine = self.make_engine(update_runner=failing_update)
        original_capture = engine._capture_environment

        def replace_before_post_update_claim(repository: object, path: Path, purpose: str):
            if update_failed and path == environment and purpose == "post-update":
                temporary = environment.with_name(".ENVIRONMENT.after-recovery-check")
                temporary.write_bytes(replacement)
                temporary.chmod(0o640)
                os.replace(temporary, environment)
            return original_capture(repository, path, purpose)

        with mock.patch.object(engine, "_capture_environment", side_effect=replace_before_post_update_claim):
            report = engine.run()

        result = report.for_name("imagestudio-mac")
        self.assertEqual(result.status, "failed")
        self.assertEqual(environment.read_bytes(), replacement)
        self.assertEqual(stat.S_IMODE(environment.stat().st_mode), 0o640)
        self.assertEqual(self.calls, [targets["imagestudio-mac"]])
        self.assertTrue(result.backup_path.exists())

    def test_post_update_health_failure_restores_environment_bytes_and_mode(self) -> None:
        targets = self.prepare_old_fleet()
        environment = targets["imagestudio-mac"] / "ENVIRONMENT"
        environment.chmod(0o640)
        original = environment.read_bytes()

        report = self.make_engine(post_update_probe=lambda _app: "health unavailable").run()

        result = report.for_name("imagestudio-mac")
        self.assertEqual(result.status, "failed")
        self.assertIn("health unavailable", result.refusal_reason or "")
        self.assertEqual(environment.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(environment.stat().st_mode), 0o640)

    def test_preserves_the_original_machine_environment_mode(self) -> None:
        targets = self.prepare_old_fleet()
        environment = targets["imagestudio-mac"] / "ENVIRONMENT"
        environment.chmod(0o640)

        report = self.make_engine().run()

        self.assertTrue(report.ok, report)
        self.assertEqual(stat.S_IMODE(environment.stat().st_mode), 0o640)

    def test_uses_post_update_status_probe_not_output_words_and_correct_voice_port(self) -> None:
        self.assertEqual(APP_SPECS[1][3], 47870)
        targets = self.prepare_old_fleet()
        observed: list[str] = []

        def completed_update(target: Path, _pterm: Path) -> str:
            self.calls.append(target)
            return "pterm completed\n"

        report = self.make_engine(
            update_runner=completed_update,
            post_update_probe=lambda repository: observed.append(repository.name) or None,
        ).run()

        self.assertTrue(report.ok, report)
        self.assertEqual(observed, ["imagestudio-mac", "voicestudio-mac", "studiohub-mac"])
        self.assertEqual(self.calls, [
            targets["imagestudio-mac"], targets["voicestudio-mac"], targets["studiohub-mac"],
        ])

    def test_post_update_probe_requires_healthy_exact_dependency_convergence_capability(self) -> None:
        repository = runtime_state_migration._Repository(
            "imagestudio-mac", "Image Studio", "fixture", 47868, self.root
        )

        class Reply:
            def __init__(self, payload: object) -> None:
                self.payload = json.dumps(payload).encode("utf-8")

            def read(self) -> bytes:
                return self.payload

            def __enter__(self) -> "Reply":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

        cases = (
            ({"ok": False}, {"capabilities": {"dependency_convergence": 1}}),
            ({"ok": True}, {}),
            ({"ok": True}, {"capabilities": {"dependency_convergence": True}}),
            ({"ok": True}, {"capabilities": {"dependency_convergence": "1"}}),
            ({"ok": True}, {"capabilities": []}),
            ({"ok": True}, {"state": "restarting", "capabilities": {"dependency_convergence": 1}}),
        )
        for health, update in cases:
            with self.subTest(health=health, update=update), mock.patch.object(
                runtime_state_migration.urllib.request,
                "urlopen",
                side_effect=(Reply(health), Reply(update)),
            ):
                self.assertIsNotNone(MigrationEngine._post_update_state(repository))
        with mock.patch.object(
            runtime_state_migration.urllib.request,
            "urlopen",
            side_effect=(Reply({"ok": True}), Reply({"capabilities": {"dependency_convergence": 1}})),
        ):
            self.assertIsNone(MigrationEngine._post_update_state(repository))

    def test_activity_probe_refuses_real_voice_and_image_customer_work_shapes(self) -> None:
        class Reply:
            def __init__(self, payload: object) -> None:
                self.payload = json.dumps(payload).encode("utf-8")

            def read(self) -> bytes:
                return self.payload

            def __enter__(self) -> "Reply":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

        cases = (
            ("voicestudio-mac", 47870, {"busy": True}),
            ("imagestudio-mac", 47868, {"generation": {"busy": False, "queued": 1, "running": 0}}),
        )
        for name, port, health in cases:
            repository = runtime_state_migration._Repository(name, name, "fixture", port, self.root)
            with self.subTest(name=name), mock.patch.object(
                runtime_state_migration.urllib.request,
                "urlopen",
                side_effect=(Reply(health), Reply({"state": "idle"})),
            ):
                self.assertEqual(MigrationEngine._active_work(repository), "customer work")
        voice = runtime_state_migration._Repository(
            "voicestudio-mac", "Voice Studio", "fixture", 47870, self.root
        )
        with mock.patch.object(
            runtime_state_migration.urllib.request,
            "urlopen",
            side_effect=(Reply({"busy": True}), OSError("status unavailable")),
        ):
            self.assertEqual(MigrationEngine._active_work(voice), "customer work")
        hub = runtime_state_migration._Repository(
            "studiohub-mac", "Studio Hub", "fixture", 47873, self.root
        )
        with mock.patch.object(
            runtime_state_migration.urllib.request,
            "urlopen",
            side_effect=(OSError("health unavailable"), Reply({"state": "updating"})),
        ):
            self.assertEqual(
                MigrationEngine._active_work(hub), "an update or maintenance operation"
            )
        with mock.patch.object(
            runtime_state_migration.urllib.request,
            "urlopen",
            side_effect=(Reply({"ok": True}), Reply({"state": "restarting"})),
        ):
            self.assertEqual(
                MigrationEngine._active_work(hub), "an update or maintenance operation"
            )

    def test_accepts_a_real_linked_git_worktree_and_included_origin_config(self) -> None:
        remote, primary = make_remote(self.root, "linked-image-source", old_environment=True)
        linked = self.home / "api" / "linked-image"
        git("worktree", "add", "--force", str(linked), "main", cwd=primary)
        include = self.root / "origin.include"
        include.write_text(f'[remote "origin"]\n\turl = {remote}\n', encoding="utf-8")
        git("config", "--unset", "remote.origin.url", cwd=primary)
        git("config", "include.path", str(include), cwd=primary)
        self.remotes["linked-image"] = remote

        engine = MigrationEngine(
            home=self.home,
            backup_root=self.backup_root,
            pterm_resolver=lambda _home: self.pterm,
            control_plane_ready=lambda: True,
            activity_probe=lambda _app: None,
            post_update_probe=lambda _app: None,
            app_specs=(("linked-image", "Image Studio", str(remote), 47868),),
        )
        report = engine.plan()

        self.assertTrue((linked / ".git").is_file())
        self.assertEqual(report.for_name("linked-image").status, "requires_migration")

    def test_migrates_a_real_linked_studio_worktree_without_claim_leaks(self) -> None:
        remote, primary = make_remote(self.root, "linked-image-source", old_environment=True)
        publish_durable_update(self.root, "linked-image-source", remote, environment=True)
        linked = self.home / "api" / "linked-image"
        git("worktree", "add", "--force", str(linked), "main", cwd=primary)
        linked_environment = linked / "ENVIRONMENT"
        linked_environment.write_text(
            "SETTING=base\n" + "\n".join(APPROVED_ENVIRONMENT_LINES) + "\n",
            encoding="utf-8",
        )
        calls: list[Path] = []
        engine = MigrationEngine(
            home=self.home,
            backup_root=self.backup_root,
            pterm_resolver=lambda _home: self.pterm,
            control_plane_ready=lambda: True,
            update_runner=lambda target, _pterm: calls.append(target) or "done\n",
            activity_probe=lambda _app: None,
            post_update_probe=lambda _app: None,
            app_specs=(("linked-image", "Image Studio", str(remote), 47868),),
        )

        report = engine.run()

        claim_directory = Path(
            git("rev-parse", "--git-path", "runtime-state-migration", cwd=linked).strip()
        )
        if not claim_directory.is_absolute():
            claim_directory = linked / claim_directory
        self.assertTrue((linked / ".git").is_file())
        self.assertTrue(report.ok, report)
        self.assertEqual(calls, [linked])
        self.assertEqual(list(claim_directory.iterdir()), [])

    def test_runs_linked_studio_hub_with_worktree_local_excludes_idempotently(self) -> None:
        remote, primary = make_remote(self.root, "hub-source", old_environment=False)
        publish_durable_update(self.root, "hub-source", remote, environment=False)
        linked = self.home / "api" / "studiohub-mac"
        git("worktree", "add", "--force", str(linked), "main", cwd=primary)
        for name in (
            ".enrollment_repair_journal.json.lock",
            "controller_settings.json.repair.lock",
        ):
            (linked / name).write_text("", encoding="utf-8")
        calls: list[Path] = []
        engine = MigrationEngine(
            home=self.home,
            backup_root=self.backup_root,
            pterm_resolver=lambda _home: self.pterm,
            control_plane_ready=lambda: True,
            update_runner=lambda target, _pterm: calls.append(target) or "done\n",
            activity_probe=lambda _app: None,
            post_update_probe=lambda _app: None,
            app_specs=(("studiohub-mac", "Studio Hub", str(remote), 47873),),
        )

        first = engine.run()
        exclude_path = Path(git("rev-parse", "--git-path", "info/exclude", cwd=linked).strip())
        if not exclude_path.is_absolute():
            exclude_path = linked / exclude_path
        excludes = exclude_path.read_bytes()
        second = engine.run()

        self.assertTrue((linked / ".git").is_file())
        self.assertTrue(first.ok, first)
        self.assertEqual(calls, [linked])
        self.assertTrue(all(pattern.encode() in excludes for pattern in LEGACY_HUB_EXCLUDES))
        self.assertTrue(second.ok, second)
        self.assertEqual(second.for_name("studiohub-mac").status, "already_ready")
        self.assertEqual(exclude_path.read_bytes(), excludes)

    def test_hub_exclude_filesystem_error_is_a_failed_report_not_a_crash(self) -> None:
        remote, primary = make_remote(self.root, "hub-source", old_environment=False)
        linked = self.home / "api" / "studiohub-mac"
        git("worktree", "add", "--force", str(linked), "main", cwd=primary)
        (linked / ".enrollment_repair_journal.json.lock").write_text("", encoding="utf-8")
        blocker = self.root / "not-a-directory"
        blocker.write_text("fixture\n", encoding="utf-8")
        engine = MigrationEngine(
            home=self.home,
            backup_root=self.backup_root,
            pterm_resolver=lambda _home: self.pterm,
            control_plane_ready=lambda: True,
            activity_probe=lambda _app: None,
            post_update_probe=lambda _app: None,
            app_specs=(("studiohub-mac", "Studio Hub", str(remote), 47873),),
        )

        with mock.patch.object(engine, "_git_path", return_value=blocker / "exclude"):
            report = engine.run()

        result = report.for_name("studiohub-mac")
        self.assertEqual(result.status, "failed")
        self.assertIn("could not update Git excludes", result.refusal_reason or "")

    def test_dry_run_never_writes_or_invokes_updates(self) -> None:
        targets = self.prepare_old_fleet()
        for name in ("imagestudio-mac", "voicestudio-mac"):
            (targets[name] / "ENVIRONMENT").write_text("SETTING=base\n", encoding="utf-8")
        before = (targets["imagestudio-mac"] / "ENVIRONMENT").read_bytes()

        report = self.make_engine(dry_run=True).run()

        self.assertTrue(report.ok, report)
        self.assertEqual(self.calls, [])
        self.assertEqual((targets["imagestudio-mac"] / "ENVIRONMENT").read_bytes(), before)
        self.assertFalse(self.backup_root.exists())

    def test_hub_mode_preserves_arbitrary_machine_environment_bytes_and_mode(self) -> None:
        targets = self.prepare_old_fleet()
        environment = targets["voicestudio-mac"] / "ENVIRONMENT"
        original = (
            b"SETTING=base\r\n"
            b"CPLUS_INCLUDE_PATH=/Library/Developer/CommandLineTools/SDKs/"
            b"MacOSX.sdk/usr/include/c++/v1\r\n"
            b"OWNER_NOTE=0310-\xff\r\n"
        )
        environment.write_bytes(original)
        environment.chmod(0o640)

        strict = self.make_engine().plan().for_name("voicestudio-mac")
        report = self.make_engine(preserve_machine_environment=True).run()

        self.assertEqual(strict.status, "refused")
        self.assertTrue(report.ok, report)
        self.assertEqual(environment.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(environment.stat().st_mode), 0o640)
        self.assertEqual(report.for_name("voicestudio-mac").status, "migrated")

    def test_hub_mode_still_refuses_every_other_dirty_path(self) -> None:
        targets = self.prepare_old_fleet()
        environment = targets["voicestudio-mac"] / "ENVIRONMENT"
        environment.write_text("LOCAL_SETTING=allowed\n", encoding="utf-8")
        (targets["voicestudio-mac"] / "README.md").write_text(
            "operator edit\n", encoding="utf-8"
        )

        result = self.make_engine(
            preserve_machine_environment=True
        ).plan().for_name("voicestudio-mac")

        self.assertEqual(result.status, "refused")
        self.assertIn("README.md", result.refusal_reason or "")
        self.assertEqual(environment.read_text(encoding="utf-8"), "LOCAL_SETTING=allowed\n")

    def test_hub_mode_refuses_symlinked_machine_environment(self) -> None:
        targets = self.prepare_old_fleet()
        environment = targets["voicestudio-mac"] / "ENVIRONMENT"
        outside = self.root / "operator-environment"
        outside.write_text("LOCAL_SETTING=outside\n", encoding="utf-8")
        environment.unlink()
        environment.symlink_to(outside)

        result = self.make_engine(
            preserve_machine_environment=True
        ).plan().for_name("voicestudio-mac")

        self.assertEqual(result.status, "refused")
        self.assertEqual(outside.read_text(encoding="utf-8"), "LOCAL_SETTING=outside\n")

    def test_hub_mode_refuses_symlinked_checkout_root(self) -> None:
        targets = self.prepare_old_fleet()
        checkout = targets["voicestudio-mac"]
        real_checkout = self.root / "real-voice-checkout"
        checkout.rename(real_checkout)
        checkout.symlink_to(real_checkout, target_is_directory=True)

        result = self.make_engine(
            preserve_machine_environment=True
        ).plan().for_name("voicestudio-mac")

        self.assertEqual(result.status, "refused")
        self.assertIn("symlink", result.refusal_reason or "")

    def test_json_cli_filters_to_a_fixed_app_and_emits_machine_readable_result(self) -> None:
        report = runtime_state_migration.MigrationReport((
            runtime_state_migration.RepositoryResult(
                "voicestudio-mac", Path("/fixed/voice"), "migrated",
                Path("/fixed/backup"), None,
            ),
        ))
        instance = mock.Mock()
        instance.run.return_value = report
        output = StringIO()

        with mock.patch.object(runtime_state_migration, "MigrationEngine", return_value=instance) as engine:
            with redirect_stdout(output):
                code = runtime_state_migration.main([
                    "--app", "voicestudio-mac",
                    "--preserve-machine-environment",
                    "--json",
                ])

        self.assertEqual(code, 0)
        arguments = engine.call_args.kwargs
        self.assertTrue(arguments["preserve_machine_environment"])
        self.assertEqual(arguments["app_specs"], (APP_SPECS[1],))
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["repositories"][0]["path"], "/fixed/voice")
        self.assertEqual(payload["repositories"][0]["backup_path"], "/fixed/backup")

    def test_whole_file_mode_requires_an_explicit_fixed_app(self) -> None:
        with self.assertRaises(SystemExit):
            runtime_state_migration.main(["--preserve-machine-environment", "--json"])
        with self.assertRaises(SystemExit):
            runtime_state_migration.main([
                "--app", "studiohub-mac", "--preserve-machine-environment", "--json",
            ])

    def test_failed_fast_forward_restores_the_verified_original_environment(self) -> None:
        targets = self.prepare_old_fleet()
        original = (targets["imagestudio-mac"] / "ENVIRONMENT").read_bytes()
        remote = self.remotes["imagestudio-mac"]
        remote.rename(remote.with_name("unavailable.remote.git"))

        report = self.make_engine().run()

        result = report.for_name("imagestudio-mac")
        self.assertEqual(result.status, "failed")
        self.assertEqual((targets["imagestudio-mac"] / "ENVIRONMENT").read_bytes(), original)
        self.assertEqual(self.calls, [])

    def test_replacement_after_exact_validation_is_refused_before_backup(self) -> None:
        targets = self.prepare_old_fleet()
        environment = targets["imagestudio-mac"] / "ENVIRONMENT"
        replacement = b"operator-owned replacement\n"
        engine = self.make_engine()
        original_plan = engine._plan_repository
        image_plans = 0

        def plan_then_replace(repository: object):
            nonlocal image_plans
            result = original_plan(repository)
            if repository.name == "imagestudio-mac":
                image_plans += 1
                if image_plans == 2:
                    temporary = environment.with_name(".ENVIRONMENT.race")
                    temporary.write_bytes(replacement)
                    os.replace(temporary, environment)
            return result

        with mock.patch.object(engine, "_plan_repository", side_effect=plan_then_replace):
            report = engine.run()

        result = report.for_name("imagestudio-mac")
        self.assertEqual(result.status, "failed")
        self.assertIn("unapproved", result.refusal_reason or "")
        self.assertEqual(environment.read_bytes(), replacement)
        self.assertFalse(self.backup_root.exists())
        self.assertEqual(self.calls, [])

    def test_same_inode_edit_after_backup_guard_is_refused_without_overwrite(self) -> None:
        targets = self.prepare_old_fleet()
        environment = targets["imagestudio-mac"] / "ENVIRONMENT"
        replacement = b"operator in-place edit\n"
        engine = self.make_engine()
        original_guard = engine._require_snapshot_current
        image_guards = 0

        def guard_then_edit(repository: object, path: Path, snapshot: object) -> None:
            nonlocal image_guards
            original_guard(repository, path, snapshot)
            if repository.name == "imagestudio-mac":
                image_guards += 1
                if image_guards == 2:
                    path.write_bytes(replacement)
                    path.chmod(0o640)

        with mock.patch.object(engine, "_require_snapshot_current", side_effect=guard_then_edit):
            report = engine.run()

        result = report.for_name("imagestudio-mac")
        backups = list(self.backup_root.rglob("ENVIRONMENT"))
        self.assertEqual(result.status, "failed")
        self.assertIn("changed after validation", result.refusal_reason or "")
        self.assertEqual(environment.read_bytes(), replacement)
        self.assertEqual(stat.S_IMODE(environment.stat().st_mode), 0o640)
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(),
                         b"SETTING=base\n" + b"\n".join(line.encode() for line in APPROVED_ENVIRONMENT_LINES) + b"\n")
        self.assertEqual(self.calls, [])

    def test_atomic_replacement_after_backup_guard_is_never_recovered_over(self) -> None:
        targets = self.prepare_old_fleet()
        environment = targets["imagestudio-mac"] / "ENVIRONMENT"
        replacement = b"operator atomic replacement\n"
        engine = self.make_engine()
        original_guard = engine._require_snapshot_current
        image_guards = 0

        def guard_then_replace(repository: object, path: Path, snapshot: object) -> None:
            nonlocal image_guards
            original_guard(repository, path, snapshot)
            if repository.name == "imagestudio-mac":
                image_guards += 1
                if image_guards == 2:
                    temporary = path.with_name(".ENVIRONMENT.after-backup")
                    temporary.write_bytes(replacement)
                    temporary.chmod(0o640)
                    os.replace(temporary, path)

        with mock.patch.object(engine, "_require_snapshot_current", side_effect=guard_then_replace):
            report = engine.run()

        result = report.for_name("imagestudio-mac")
        backups = list(self.backup_root.rglob("ENVIRONMENT"))
        self.assertEqual(result.status, "failed")
        self.assertIn("concurrent", result.refusal_reason or "")
        self.assertEqual(environment.read_bytes(), replacement)
        self.assertEqual(stat.S_IMODE(environment.stat().st_mode), 0o640)
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(),
                         b"SETTING=base\n" + b"\n".join(line.encode() for line in APPROVED_ENVIRONMENT_LINES) + b"\n")
        self.assertEqual(self.calls, [])

    def test_replacement_immediately_before_head_restore_is_never_overwritten(self) -> None:
        targets = self.prepare_old_fleet()
        environment = targets["imagestudio-mac"] / "ENVIRONMENT"
        replacement = b"operator replacement inside head restore\n"
        engine = self.make_engine()
        original_restore = engine._restore_head_environment

        def replace_before_restore(repository: object):
            if repository.name == "imagestudio-mac":
                temporary = environment.with_name(".ENVIRONMENT.before-head-restore")
                temporary.write_bytes(replacement)
                temporary.chmod(0o640)
                os.replace(temporary, environment)
            return original_restore(repository)

        with mock.patch.object(engine, "_restore_head_environment", side_effect=replace_before_restore):
            report = engine.run()

        result = report.for_name("imagestudio-mac")
        backups = list(self.backup_root.rglob("ENVIRONMENT"))
        self.assertEqual(result.status, "failed")
        self.assertIn("concurrent", result.refusal_reason or "")
        self.assertEqual(environment.read_bytes(), replacement)
        self.assertEqual(stat.S_IMODE(environment.stat().st_mode), 0o640)
        self.assertEqual(len(backups), 1)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
