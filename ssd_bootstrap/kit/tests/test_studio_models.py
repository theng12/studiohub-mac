from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


KIT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT_ROOT))

import studio_models


class ModelStageIsolationTests(unittest.TestCase):
    def test_manifest_is_replaced_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            path = Path(value) / "MANIFEST.json"
            path.write_text('{"old": true}\n')
            studio_models.write_json_atomic(path, {"schema_version": 2, "ok": True})
            self.assertEqual(json.loads(path.read_text()), {"schema_version": 2, "ok": True})
            self.assertEqual(list(path.parent.glob(".MANIFEST.json.*.tmp")), [])

    def test_staging_models_does_not_rewrite_the_bootstrap_kit(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            volume = Path(value)
            root = volume / "studio-models"
            hub = volume / "source-hub"
            package = hub / "models--example--small"
            package.mkdir(parents=True)
            (package / "weights.bin").write_bytes(b"weights")
            catalog = [{
                "repo": "example/small",
                "family": "Example",
                "provider": "local",
                "min_unified_memory_gb": 8,
                "cache": {"state": "cached", "path": str(package)},
            }]

            def discover(port: int):
                if port == studio_models.STUDIOS["voice"]["port"]:
                    return hub, catalog
                return None, []

            with mock.patch.object(studio_models, "discover", side_effect=discover), \
                    mock.patch.object(studio_models, "discover_fleet_voices", return_value=[]):
                result = studio_models.do_stage(root, plan_only=False, keep_non_cloning=False)

            self.assertEqual(result, 0)
            manifest = json.loads((root / "MANIFEST.json").read_text())
            self.assertEqual(manifest["studios"]["voice"]["packages"][0]["repo"], "example/small")
            self.assertFalse((volume / "terranash-bootstrap").exists())

    def test_8gb_voice_allowlist_is_base_whisper_and_kokoro(self) -> None:
        self.assertTrue(studio_models.restore_allowed(
            "voice", "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-8bit", 8.6, False
        ))
        self.assertTrue(studio_models.restore_allowed(
            "voice", "mlx-community/whisper-large-v3-turbo", 8.6, False
        ))
        self.assertTrue(studio_models.restore_allowed(
            "voice", "mlx-community/Kokoro-82M-bf16", 8.6, False
        ))
        self.assertFalse(studio_models.restore_allowed(
            "voice", "mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-8bit", 8.6, False
        ))
        self.assertFalse(studio_models.restore_allowed(
            "voice", "mlx-community/OmniVoice-bfloat16", 8.6, False
        ))

    def test_shared_companion_uses_the_lowest_known_memory_floor(self) -> None:
        models = [
            {
                "repo": "example/high",
                "min_unified_memory_gb": 16,
                "cache": {"companions": [{"repo": "example/shared-codec"}]},
            },
            {
                "repo": "example/low",
                "min_unified_memory_gb": 8,
                "cache": {"companions": [{"repo": "example/shared-codec"}]},
            },
        ]

        _families, floors, companions = studio_models.catalog_index(models)

        self.assertEqual(floors["example/shared-codec"], 8)
        self.assertIn("example/shared-codec", companions)

    def test_prune_removes_recognized_unselected_voice_models_only(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value) / "studio-models"
            pinokio_home = Path(value) / "pinokio"
            voice_studio = pinokio_home / "api/voicestudio-mac"
            hub = voice_studio / "cache/HF_HOME/hub"
            hub.mkdir(parents=True)
            packages = (
                (studio_models.QWEN_06B_BASE, 8),
                (studio_models.QWEN_QUALITY_WHISPER, None),
                (studio_models.KOKORO_82M_BF16, 8),
                ("mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-8bit", 8),
            )
            entries = []
            for repo, floor in packages:
                dirname = "models--" + repo.replace("/", "--")
                package = hub / dirname
                package.mkdir()
                (package / "weights.bin").write_bytes(b"weights")
                entries.append({
                    "repo": repo,
                    "dir": dirname,
                    "family": "Voice",
                    "floor_gb": floor,
                    "bytes": 7,
                })
            unknown = hub / "models--local--owner-model"
            unknown.mkdir()
            (unknown / "weights.bin").write_bytes(b"private")
            root.mkdir()
            (root / "MANIFEST.json").write_text(json.dumps({
                "schema_version": 2,
                "studios": {
                    "voice": {"packages": entries},
                    "image": {"packages": []},
                },
                "voices": [],
            }))

            output = io.StringIO()
            with mock.patch.object(studio_models, "machine_memory_gb", return_value=8.6), \
                    contextlib.redirect_stdout(output):
                result = studio_models.do_restore(
                    root,
                    plan_only=False,
                    prune=True,
                    restore_all=False,
                    force=False,
                    include_unqualified=False,
                    keep_non_cloning=False,
                    pinokio_home=pinokio_home,
                )

            custom = hub / "models--mlx-community--Qwen3-TTS-12Hz-0.6B-CustomVoice-8bit"
            self.assertEqual(result, 0)
            self.assertFalse(custom.exists())
            self.assertTrue((hub / ("models--" + studio_models.QWEN_06B_BASE.replace("/", "--"))).is_dir())
            self.assertTrue((hub / ("models--" + studio_models.QWEN_QUALITY_WHISPER.replace("/", "--"))).is_dir())
            self.assertTrue((hub / ("models--" + studio_models.KOKORO_82M_BF16.replace("/", "--"))).is_dir())
            self.assertTrue(unknown.is_dir())
            self.assertIn("not selected for this Mac's tier", output.getvalue())
            self.assertNotIn("needing more memory than this Mac has", output.getvalue())


if __name__ == "__main__":
    unittest.main()
