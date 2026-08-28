from __future__ import annotations

import os
import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


KIT_ROOT = Path(__file__).resolve().parents[1]


class CommandWrapperTests(unittest.TestCase):
    def run_wrapper(self, name: str, *arguments: str, input_text: str = "") -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as value:
            env = os.environ.copy()
            env["HOME"] = value
            env["TERRANASH_NONINTERACTIVE"] = "1"
            config = Path(value) / ".pinokio/config.json"
            config.parent.mkdir(parents=True)
            config.write_text('{"home": "' + str(Path(value) / "pinokio") + '"}')
            return subprocess.run(
                [str(KIT_ROOT / name), *arguments],
                text=True,
                input=input_text,
                capture_output=True,
                env=env,
            )

    def test_stage_one_is_independent(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            apps = []
            for index in range(3):
                filename = f"Fixture-{index}.dmg"
                path = root / filename
                path.write_bytes(f"fixture {index}".encode())
                apps.append({
                    "id": f"fixture-{index}",
                    "title": f"Fixture {index}",
                    "version": "1.0",
                    "filename": filename,
                    "source_url": "https://example.invalid/fixture",
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "kind": "dmg",
                    "app_name": f"TerraNash Test Fixture {index}.app",
                    "bundle_id": f"com.terranash.fixture{index}",
                    "team_id": "ABCDEFGHIJ",
                })
            manifest = root / "MANIFEST.json"
            manifest.write_text(json.dumps({"schema_version": 1, "apps": apps}))
            result = self.run_wrapper(
                "1 Install Mac Apps.command", "--dry-run", "--manifest", str(manifest)
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        output = f"{result.stdout}\n{result.stderr}".lower()
        self.assertIn("install ordinary mac applications", output)
        self.assertNotIn("xcode-select", output)
        self.assertNotIn("image studio checkout", output)
        self.assertNotIn("voice studio checkout", output)
        self.assertNotIn("copy ram", output)

    def test_stage_two_dry_run_stops_quickly_when_pinokio_is_not_ready(self) -> None:
        result = self.run_wrapper("2 Install Studios.command", "--dry-run")
        output = f"{result.stdout}\n{result.stderr}"
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Install Tools", output)
        self.assertNotIn("Waiting for Pinokio", output)

    def test_model_actions_are_isolated(self) -> None:
        wrapper = KIT_ROOT / "3 Manage AI Models.command"
        self.assertTrue(wrapper.is_file())
        for action in ("stage", "restore", "restore-all"):
            result = self.run_wrapper(wrapper.name, "--dry-run", "--action", action)
            output = f"{result.stdout}\n{result.stderr}".lower()
            self.assertNotIn("install ordinary mac applications", output)
            self.assertNotIn("pterm download", output)
            self.assertNotIn("enrollment code", output)
            self.assertNotIn("--prune", output)

    def test_pruning_dry_run_executes_the_no_write_model_plan(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value) / "terranash-bootstrap"
            root.mkdir()
            for name in ("3 Manage AI Models.command", ".terranash-bootstrap.command"):
                shutil.copy2(KIT_ROOT / name, root / name)
                (root / name).chmod(0o755)
            pinokio_home = Path(value) / "pinokio"
            for app in ("imagestudio-mac", "voicestudio-mac", "studiohub-mac"):
                (pinokio_home / "api" / app / ".git").mkdir(parents=True)
            (root / "fleet_bootstrap.py").write_text(
                "import os\nfrom pathlib import Path\n"
                "def resolve_pinokio_home(): return Path(os.environ['PINOKIO_HOME'])\n"
            )
            (root / "studio_models.py").write_text(
                "import os, sys\n"
                "with open(os.environ['EVENTS'], 'w') as out: out.write(' '.join(sys.argv[1:]))\n"
            )
            events = Path(value) / "events"
            env = os.environ.copy()
            env["HOME"] = str(Path(value) / "home")
            env["PINOKIO_HOME"] = str(pinokio_home)
            env["EVENTS"] = str(events)
            env["TERRANASH_NONINTERACTIVE"] = "1"

            result = subprocess.run(
                [
                    str(root / "3 Manage AI Models.command"),
                    "--dry-run", "--action", "restore", "--prune",
                ],
                text=True,
                capture_output=True,
                env=env,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(events.is_file(), result.stdout)
            self.assertIn("restore --root", events.read_text())
            self.assertIn("--prune --plan", events.read_text())

    def test_stage_five_delegates_only_to_runtime_state_migration(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            source = KIT_ROOT / "5 Migrate Studio Updates.command"
            target = root / source.name
            shutil.copy2(source, target)
            target.chmod(0o755)
            dispatcher_marker = root / "dispatcher-ran"
            dispatcher = root / ".terranash-bootstrap.command"
            dispatcher.write_text(
                f"#!/bin/zsh\n/usr/bin/touch {str(dispatcher_marker)!r}\nexit 99\n"
            )
            dispatcher.chmod(0o755)
            (root / "runtime_state_migration.py").write_text(
                "import json, sys\nprint(json.dumps(sys.argv[1:]))\n"
            )
            env = os.environ.copy()
            env["HOME"] = str(root / "home")
            env["TERRANASH_NONINTERACTIVE"] = "1"
            result = subprocess.run(
                [str(root / "5 Migrate Studio Updates.command"), "--dry-run"],
                text=True,
                capture_output=True,
                env=env,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('["--dry-run"]', result.stdout)
        self.assertFalse(dispatcher_marker.exists())
        self.assertNotIn("fleet_bootstrap.py", result.stdout)
        self.assertNotIn("studio_models.py", result.stdout)
        self.assertNotIn("repair_startup.py", result.stdout)

    def test_stage_six_preflights_then_restores_suitable_models_and_refreshes(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            source = KIT_ROOT / "6 Inspect and Fix This Mac.command"
            target = root / source.name
            shutil.copy2(source, target)
            target.chmod(0o755)
            events = root / "events"
            (root / "runtime_state_migration.py").write_text(
                "import os, sys\n"
                "with open(os.environ['EVENTS'], 'a') as out: "
                "out.write('migration ' + ' '.join(sys.argv[1:]) + '\\n')\n"
            )
            (root / "repair_startup.py").write_text(
                "import os, sys\n"
                "with open(os.environ['EVENTS'], 'a') as out: "
                "out.write('startup ' + ' '.join(sys.argv[1:]) + '\\n')\n"
            )
            dispatcher = root / ".terranash-bootstrap.command"
            dispatcher.write_text(
                "#!/bin/zsh\nprint -r -- \"models $*\" >> \"$EVENTS\"\n"
            )
            dispatcher.chmod(0o755)
            env = os.environ.copy()
            env["EVENTS"] = str(events)
            env["TERRANASH_NONINTERACTIVE"] = "1"

            result = subprocess.run(
                [str(target)], text=True, capture_output=True, env=env,
            )
            observed = events.read_text()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(observed.splitlines(), [
            "migration --dry-run --update-current --preserve-machine-environment",
            "models --models --action restore --prune",
            "migration --update-current --preserve-machine-environment",
            "startup ",
        ])
        self.assertNotIn("restore-all", observed)
        self.assertIn("--prune", observed)

    def test_stage_six_dry_run_is_entirely_no_write(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            source = KIT_ROOT / "6 Inspect and Fix This Mac.command"
            target = root / source.name
            shutil.copy2(source, target)
            target.chmod(0o755)
            events = root / "events"
            for name, label in (
                ("runtime_state_migration.py", "migration"),
                ("repair_startup.py", "startup"),
            ):
                (root / name).write_text(
                    "import os, sys\n"
                    f"with open(os.environ['EVENTS'], 'a') as out: out.write('{label} ' + ' '.join(sys.argv[1:]) + '\\n')\n"
                )
            dispatcher = root / ".terranash-bootstrap.command"
            dispatcher.write_text(
                "#!/bin/zsh\nprint -r -- \"models $*\" >> \"$EVENTS\"\n"
            )
            dispatcher.chmod(0o755)
            env = os.environ.copy()
            env["EVENTS"] = str(events)
            env["TERRANASH_NONINTERACTIVE"] = "1"

            result = subprocess.run(
                [str(target), "--dry-run"], text=True, capture_output=True, env=env,
            )
            observed = events.read_text()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(observed.splitlines(), [
            "migration --dry-run --update-current --preserve-machine-environment",
            "models --models --dry-run --action restore --prune",
            "startup --dry-run",
        ])


if __name__ == "__main__":
    unittest.main()
