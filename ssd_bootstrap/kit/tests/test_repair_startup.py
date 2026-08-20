from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import repair_startup


class StartupRepairTests(unittest.TestCase):
    def test_service_owner_and_duplicate_are_not_pinokio_autolaunched(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            home = Path(value)
            apps = home / "api"
            image = apps / "imagestudio-mac"
            voice = apps / "voicestudio-mac"
            duplicate = apps / "voicestudio-mac.git"
            hub = apps / "studiohub-mac"
            for target in (image, voice, duplicate, hub):
                target.mkdir(parents=True)
                (target / "ENVIRONMENT").write_text(
                    "PINOKIO_SCRIPT_REQUIRES=imagestudio-mac,voicestudio-mac\n"
                )
            marker = image / "service/.installed"
            marker.parent.mkdir()
            marker.write_text("installed\n")

            self.assertEqual(repair_startup.repair_startup(home, dry_run=False), 0)
            self.assertIn("AUTOLAUNCH_ENABLED=false", (image / "ENVIRONMENT").read_text())
            self.assertIn("AUTOLAUNCH_ENABLED=true", (voice / "ENVIRONMENT").read_text())
            self.assertIn("AUTOLAUNCH_ENABLED=false", (duplicate / "ENVIRONMENT").read_text())
            self.assertIn("AUTOLAUNCH_ENABLED=true", (hub / "ENVIRONMENT").read_text())
            for target in (image, voice, duplicate, hub):
                self.assertNotIn(
                    "imagestudio-mac,voicestudio-mac", (target / "ENVIRONMENT").read_text()
                )

    def test_legacy_service_owner_disables_canonical_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            home = Path(value)
            canonical = home / "api/voicestudio-mac"
            legacy = home / "api/voicestudio-mac.git"
            for target in (canonical, legacy):
                target.mkdir(parents=True)
                (target / "ENVIRONMENT").write_text("")
            marker = legacy / "service/.installed"
            marker.parent.mkdir()
            marker.write_text("installed\n")

            self.assertEqual(repair_startup.repair_startup(home, dry_run=False), 0)
            self.assertIn(
                "AUTOLAUNCH_ENABLED=false", (canonical / "ENVIRONMENT").read_text()
            )
            self.assertIn(
                "AUTOLAUNCH_ENABLED=false", (legacy / "ENVIRONMENT").read_text()
            )


if __name__ == "__main__":
    unittest.main()
