from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


KIT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT_ROOT))

import mac_apps


class MacAppManifestTests(unittest.TestCase):
    def write_manifest(self, root: Path, *, digest: str, kind: str = "dmg") -> Path:
        payload = {
            "schema_version": 1,
            "apps": [{
                "id": "example",
                "title": "Example",
                "version": "1.2.3",
                "filename": "Example.dmg",
                "source_url": "https://example.invalid/Example.dmg",
                "sha256": digest,
                "kind": kind,
                "app_name": "Example.app",
                "bundle_id": "com.example.app",
                "team_id": "ABCDEFGHIJ",
            }],
        }
        path = root / "MANIFEST.json"
        path.write_text(json.dumps(payload))
        return path

    def test_load_manifest_rejects_non_sha256_digest(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            path = self.write_manifest(Path(value), digest="not-a-digest")
            with self.assertRaisesRegex(mac_apps.InstallError, "SHA-256"):
                mac_apps.load_manifest(path)

    def test_load_manifest_rejects_unknown_installer_kind(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            path = self.write_manifest(Path(value), digest="a" * 64, kind="zip")
            with self.assertRaisesRegex(mac_apps.InstallError, "kind"):
                mac_apps.load_manifest(path)

    def test_shipped_manifest_contains_only_the_three_owner_approved_dmgs(self) -> None:
        assets = mac_apps.load_manifest(KIT_ROOT / "installers/MANIFEST.json")
        self.assertEqual(
            [asset.id for asset in assets],
            ["pinokio", "yam-display", "latest"],
        )
        self.assertTrue(all(asset.kind == "dmg" for asset in assets))

    def test_stage_one_has_no_package_installer_path(self) -> None:
        source = (KIT_ROOT / "mac_apps.py").read_text()
        self.assertNotIn("def install_pkg", source)
        self.assertNotIn("/usr/sbin/installer", source)
        self.assertNotIn("pkgutil", source)

    def test_manual_steps_delegate_tailscale_to_the_mac_app_store(self) -> None:
        steps = mac_apps.manual_next_steps()
        self.assertIn("Install Tailscale from the Mac App Store", steps[1])
        self.assertIn("Open Yam Display", steps[2])
        self.assertIn("Open Latest", steps[3])

    def test_verify_asset_uses_real_bytes_and_rejects_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            asset_path = root / "Example.dmg"
            asset_path.write_bytes(b"verified installer bytes")
            digest = hashlib.sha256(asset_path.read_bytes()).hexdigest()
            asset = mac_apps.load_manifest(self.write_manifest(root, digest=digest))[0]
            self.assertEqual(mac_apps.verify_asset(asset, root), asset_path)
            asset_path.write_bytes(b"tampered")
            with self.assertRaisesRegex(mac_apps.InstallError, "checksum"):
                mac_apps.verify_asset(asset, root)

    def test_verify_asset_rejects_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            asset = mac_apps.load_manifest(self.write_manifest(root, digest="a" * 64))[0]
            with self.assertRaisesRegex(mac_apps.InstallError, "Missing"):
                mac_apps.verify_asset(asset, root)

    def test_version_comparison_skips_equal_or_newer_app(self) -> None:
        self.assertFalse(mac_apps.needs_install("1.2.3", "1.2.3"))
        self.assertFalse(mac_apps.needs_install("1.2.4", "1.2.3"))
        self.assertTrue(mac_apps.needs_install("1.2.2", "1.2.3"))
        self.assertTrue(mac_apps.needs_install("", "1.2.3"))

    def test_dry_run_plans_apps_without_external_commands(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            asset_path = root / "Example.dmg"
            asset_path.write_bytes(b"verified installer bytes")
            digest = hashlib.sha256(asset_path.read_bytes()).hexdigest()
            asset = mac_apps.load_manifest(self.write_manifest(root, digest=digest))[0]

            calls: list[list[str]] = []
            result = mac_apps.install_assets(
                [asset],
                root,
                dry_run=True,
                applications_dir=root / "Applications",
                runner=lambda command, **_kwargs: calls.append(command),
            )

            self.assertEqual(result, {"planned": 1, "installed": 0, "skipped": 0})
            self.assertEqual(calls, [])

    def test_installer_commands_never_include_xcode_or_studios(self) -> None:
        source = (KIT_ROOT / "mac_apps.py").read_text()
        forbidden = ("xcode-select", "imagestudio", "voicestudio", "studiohub")
        self.assertTrue(all(term not in source.lower() for term in forbidden))

    def test_pinokio_login_agent_uses_one_stable_launch_graph(self) -> None:
        payload = mac_apps.login_agent_payload(Path("/Applications/Pinokio.app"))
        self.assertEqual(payload["Label"], "com.terranash.pinokio")
        self.assertEqual(payload["ProgramArguments"], [
            "/usr/bin/open", "-gj", "/Applications/Pinokio.app"
        ])
        self.assertTrue(payload["RunAtLoad"])


if __name__ == "__main__":
    unittest.main()
