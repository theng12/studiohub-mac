from __future__ import annotations

import configparser
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


KIT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT_ROOT))

import fleet_bootstrap


def make_checkout(root: Path, name: str, origin: str) -> Path:
    target = root / "api" / name
    (target / ".git").mkdir(parents=True)
    config = configparser.ConfigParser()
    config['remote "origin"'] = {"url": origin}
    with (target / ".git/config").open("w") as handle:
        config.write(handle)
    return target


class CheckoutResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = fleet_bootstrap.APPS[1]

    def test_existing_legacy_checkout_is_adopted(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            home = Path(value)
            legacy = make_checkout(home, "voicestudio-mac.git", self.spec["url"])
            target = fleet_bootstrap.ensure_repo(Path("/fake/pterm"), home, self.spec, dry_run=True)
            self.assertEqual(target, legacy)

    def test_canonical_wins_when_both_forms_exist(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            home = Path(value)
            canonical = make_checkout(home, "voicestudio-mac", self.spec["url"])
            make_checkout(home, "voicestudio-mac.git", self.spec["url"])
            target = fleet_bootstrap.ensure_repo(Path("/fake/pterm"), home, self.spec, dry_run=True)
            self.assertEqual(target, canonical)

    def test_wrong_origin_is_rejected_before_use(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            home = Path(value)
            make_checkout(home, "voicestudio-mac", "https://github.com/example/wrong.git")
            with self.assertRaisesRegex(fleet_bootstrap.BootstrapError, "different repository"):
                fleet_bootstrap.ensure_repo(Path("/fake/pterm"), home, self.spec, dry_run=False)

    def test_existing_safe_checkout_fast_forwards_main(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            home = Path(value)
            target = make_checkout(home, "voicestudio-mac", self.spec["url"])
            with mock.patch.object(
                fleet_bootstrap, "git_checkout_state", return_value=("main", "origin/main", "")
            ), mock.patch.object(fleet_bootstrap, "run") as run:
                self.assertEqual(
                    fleet_bootstrap.ensure_repo(
                        Path("/fake/pterm"), home, self.spec, dry_run=False
                    ),
                    target,
                )
            run.assert_called_once_with(
                ["git", "-C", str(target), "pull", "--ff-only"], dry_run=False
            )

    def test_unsafe_existing_checkout_is_not_updated(self) -> None:
        states = (
            (("feature/work", "origin/feature/work", ""), "must be on main"),
            (("main", "fork/main", ""), "must track origin/main"),
            (("main", "origin/main", " M README.md"), "has local changes"),
        )
        for state, message in states:
            with self.subTest(state=state), tempfile.TemporaryDirectory() as value:
                home = Path(value)
                make_checkout(home, "voicestudio-mac", self.spec["url"])
                with mock.patch.object(
                    fleet_bootstrap, "git_checkout_state", return_value=state
                ), mock.patch.object(fleet_bootstrap, "run") as run:
                    with self.assertRaisesRegex(fleet_bootstrap.BootstrapError, message):
                        fleet_bootstrap.ensure_repo(
                            Path("/fake/pterm"), home, self.spec, dry_run=False
                        )
                run.assert_not_called()


class ReadinessTests(unittest.TestCase):
    def test_missing_pinokio_tools_fail_immediately_with_owner_instruction(self) -> None:
        with mock.patch.object(fleet_bootstrap, "resolve_pinokio_home", return_value=None):
            with self.assertRaisesRegex(fleet_bootstrap.BootstrapError, "Install Tools"):
                fleet_bootstrap.check_pinokio_ready(dry_run=False)

    def test_ready_pinokio_returns_existing_home_and_pterm(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            home = Path(value)
            pterm = home / "bin/npm/bin/pterm"
            pterm.parent.mkdir(parents=True)
            pterm.write_text("#!/bin/sh\n")
            pterm.chmod(0o755)
            node = home / "bin/miniforge/bin/node"
            node.parent.mkdir(parents=True)
            node.write_text("#!/bin/sh\n")
            node.chmod(0o755)
            with mock.patch.object(fleet_bootstrap, "resolve_pinokio_home", return_value=home), \
                    mock.patch.object(fleet_bootstrap, "control_plane_ready", return_value=True):
                self.assertEqual(fleet_bootstrap.check_pinokio_ready(dry_run=False), (home, pterm))

    def test_studio_main_has_no_model_or_enrollment_side_effect(self) -> None:
        targets = {spec["name"]: Path("/tmp") / spec["name"] for spec in fleet_bootstrap.APPS}
        with mock.patch.object(fleet_bootstrap, "validate_host"), \
                mock.patch.object(
                    fleet_bootstrap,
                    "check_pinokio_ready",
                    return_value=(Path("/fake/home"), Path("/fake/pterm")),
                ), \
                mock.patch.object(
                    fleet_bootstrap,
                    "ensure_repo",
                    side_effect=lambda _pterm, _home, spec, **_kwargs: targets[spec["name"]],
                ) as ensure_repo, \
                mock.patch.object(fleet_bootstrap, "ensure_dependencies") as dependencies, \
                mock.patch.object(fleet_bootstrap, "configure_autolaunch") as autolaunch:
            result = fleet_bootstrap.main(["--studios-only", "--dry-run"])
        self.assertEqual(result, 0)
        self.assertEqual(ensure_repo.call_count, 3)
        self.assertEqual(dependencies.call_count, 3)
        autolaunch.assert_called_once_with(targets, dry_run=True)

    def test_studios_autolaunch_independently_without_false_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            targets = {}
            for app in fleet_bootstrap.APPS:
                target = root / app["name"]
                target.mkdir()
                targets[app["name"]] = target
            fleet_bootstrap.configure_autolaunch(targets, dry_run=False)
            for target in targets.values():
                environment = (target / "ENVIRONMENT").read_text()
                self.assertIn("PINOKIO_SCRIPT_AUTOLAUNCH_ENABLED=true", environment)
                self.assertIn("PINOKIO_SCRIPT_REQUIRES=", environment)
                self.assertNotIn("imagestudio-mac,voicestudio-mac", environment)


if __name__ == "__main__":
    unittest.main()
