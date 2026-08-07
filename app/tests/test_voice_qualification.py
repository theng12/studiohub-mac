import asyncio
import time

import httpx
import pytest

from backend import (
    control_plane,
    execution_assets,
    main,
    model_exposure,
    peers,
    voice_qualification,
)


CUSTOM = "mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-8bit"
BASE = "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-8bit"
CHATTERBOX = "mlx-community/chatterbox-4bit"
VOXCPM2 = "mlx-community/VoxCPM2-4bit"
VIBEVOICE = "mlx-community/VibeVoice-Realtime-0.5B-4bit"
OMNIVOICE = "mlx-community/OmniVoice-bfloat16"
FISH = "mlx-community/fish-audio-s2-pro-8bit"
REVISION = "a" * 64
AIDEN_SOURCE_SHA256 = voice_qualification.FISH_AIDEN_SOURCE_SHA256
AIDEN_TRANSCRIPT = (
    "But at his feet lay a small, injured fox. Its hind leg was bent and it could not move. "
    "Taking pity on the poor creature,"
)
QWEN_ROSTER = [
    {"id": value} for value in (
        "Ryan", "Aiden", "Serena", "Vivian", "Uncle_Fu", "Dylan", "Eric",
        "Ono_Anna", "Sohee",
    )
]
CHATTERBOX_LANGUAGES = [
    "ar", "da", "de", "el", "en", "es", "fi", "fr", "he", "hi",
    "it", "ja", "ko", "ms", "nl", "no", "pl", "pt", "ru", "sv",
    "sw", "tr", "zh",
]
VIBEVOICE_ROSTER = [{"id": value} for value in sorted(voice_qualification.VIBEVOICE_VOICE_IDS)]


class FakeMonitor:
    def __init__(self, *, version="1.27.9", cached=True, ready=True, machine="worker-8"):
        self.registry = [
            {"id": "voice", "modality": "voice", "machine": "local", "host": "127.0.0.1", "port": 47864},
            {"id": "voice@worker-8", "modality": "voice", "machine": machine,
             "host": "100.64.0.8", "hub_port": 47873, "port": 47864},
        ]
        self.status = {
            "voice": {"status": "up", "app_version": version},
            "voice@worker-8": {"status": "up", "app_version": version},
        }
        self._cached, self._ready = cached, ready

    def cached_aggregate_catalog(self):
        models = []
        for studio_id in ("voice", "voice@worker-8"):
            for repo in (CUSTOM, BASE, CHATTERBOX, VOXCPM2, VIBEVOICE, OMNIVOICE, FISH):
                entry = {
                    "repo": repo, "hub_studio": studio_id, "hub_cached": self._cached,
                    "hub_catalog_stale": False, "runtime_compatible": self._ready,
                    "hub_ready": self._ready, "runtime_revision": REVISION,
                    "min_unified_memory_gb": 24 if repo == FISH else 8,
                    "min_free_memory_gb": 2,
                }
                if repo == CHATTERBOX:
                    entry["language_support"] = {
                        "input_selection": "required", "enumeration_status": "exact",
                        "codes": CHATTERBOX_LANGUAGES, "runtime_enforced": True,
                    }
                models.append(entry)
        return {"models": models}


class Response:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {}

    def json(self):
        return self._body


class FakeClient:
    def __init__(self, *, post=None, get=None, delete=None, availability=None):
        self.post_result = post or Response(200, {"job": {"id": "voice-job-1"}})
        self.get_result = get or Response(200, {"jobs": []})
        self.delete_result = delete or Response(200, {"ok": True})
        self.availability_result = availability or Response(200, {
            "qwen3_preset_speakers": QWEN_ROSTER,
            "vibevoice_voices": VIBEVOICE_ROSTER,
        })
        self.calls = []

    async def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        if isinstance(self.post_result, Exception):
            raise self.post_result
        return self.post_result

    async def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        result = self.availability_result if url.endswith("/api/generate/availability") else self.get_result
        if isinstance(result, Exception):
            raise result
        return result

    async def delete(self, url, **kwargs):
        self.calls.append(("delete", url, kwargs))
        if isinstance(self.delete_result, Exception):
            raise self.delete_result
        return self.delete_result


def _controller():
    control_plane.save_settings({"role": "controller", "site_id": "site-a",
                                 "site_name": "Site A", "controller_id": "controller-a",
                                 "database_mode": "off"})


def _remote_memory(machine="worker-8", tier=8, available=6):
    peers._cache[machine] = (time.time(), {
        "reachable": True, "auth": True, "status": "connected",
        "host": {"total_gb": tier, "available_gb": available},
    })


def _request(**overrides):
    request = {
        "client_request_id": "qualification.test-0001", "target_studio_id": "voice@worker-8",
        "machine_tier_gb": 8, "model": CUSTOM, "case_type": "short",
        "text": "A short, deterministic qualification sample.",
        "params": {"preset_speaker": "Ryan"},
    }
    request.update(overrides)
    return request


def _save_fish_gate(case_type: str, *, tier: int = 24, machine: str = "worker-8") -> None:
    now = time.time()
    voice_qualification._save({
        "id": f"vq-gate-{tier}-{case_type}",
        "client_request_id": f"qualification.gate.{tier}.{case_type}",
        "request_fingerprint": "9" * 64, "created_at": now,
        "state": "succeeded", "case_type": case_type, "model": FISH,
        "operation": "voice_clone", "target": {
            "studio_id": "voice@worker-8", "machine_id": machine,
            "machine_tier_gb": tier, "runtime_revision": REVISION,
        },
        "worker_job_id": f"job-gate-{tier}-{case_type}", "progress": 1.0,
        "terminal_evidence": {"artifact": {
            "reference_source_sha256": AIDEN_SOURCE_SHA256,
        }},
        "review_reason": None, "stop_reason": None,
    })


def test_submit_persists_before_remote_call_and_replays_without_duplicate_submit(reset):
    _controller(); _remote_memory()
    client = FakeClient()
    first = asyncio.run(voice_qualification.submit(FakeMonitor(), _request(), client))
    restored = voice_qualification.get(first["id"])
    replay = asyncio.run(voice_qualification.submit(FakeMonitor(), _request(), client))

    assert first["state"] == "running"
    assert restored["client_request_id"] == "qualification.test-0001"
    assert replay["replayed"] is True and replay["id"] == first["id"]
    assert [call[0] for call in client.calls] == ["get", "get", "post"]
    assert client.calls[2][1].startswith("http://100.64.0.8:47873/studio/voice/")
    assert "/api/generate/txt2speech" in client.calls[2][1]


def test_submit_ledger_exists_before_remote_worker_can_accept(reset):
    _controller(); _remote_memory()

    class InspectingClient(FakeClient):
        async def post(self, url, **kwargs):
            persisted = voice_qualification.get_by_client_request_id("qualification.test-0001")
            assert persisted is not None and persisted["state"] == "submitting"
            return await super().post(url, **kwargs)

    result = asyncio.run(voice_qualification.submit(FakeMonitor(), _request(), InspectingClient()))
    assert result["state"] == "running"


def test_rejects_controller_local_target_without_any_remote_call(reset):
    _controller(); _remote_memory()
    client = FakeClient()
    with pytest.raises(voice_qualification.QualificationError, match="allow_controller_local") as error:
        asyncio.run(voice_qualification.submit(
            FakeMonitor(), _request(target_studio_id="voice"), client,
        ))
    assert error.value.code == "LOCAL_TARGET_FORBIDDEN"
    assert client.calls == []


def test_lost_submit_response_is_uncertain_and_never_resubmitted(reset):
    _controller(); _remote_memory()
    client = FakeClient(post=httpx.ReadTimeout("lost response"))
    first = asyncio.run(voice_qualification.submit(FakeMonitor(), _request(), client))
    replay = asyncio.run(voice_qualification.submit(FakeMonitor(), _request(), client))

    assert first["state"] == "uncertain"
    assert first["review_reason"] == "SUBMIT_RESPONSE_UNKNOWN"
    assert replay["replayed"] is True and replay["state"] == "uncertain"
    assert replay["review_reason"] == "SUBMIT_RECONCILIATION_NOT_FOUND"
    assert [call[0] for call in client.calls].count("post") == 1


def test_lost_submit_response_reconciles_by_stable_request_id_without_repost(reset):
    _controller(); _remote_memory()

    class ReconcileClient(FakeClient):
        def __init__(self):
            super().__init__(post=httpx.ReadTimeout("lost response"))
            self.job_list_calls = 0

        async def get(self, url, **kwargs):
            if url.endswith("/api/generate/availability"):
                return await super().get(url, **kwargs)
            self.calls.append(("get", url, kwargs))
            self.job_list_calls += 1
            if self.job_list_calls == 1:
                return Response(200, {"jobs": []})
            return Response(200, {"jobs": [{
                "id": "voice-job-recovered", "state": "running",
                "started_at": time.time() - 2,
                "params": {"client_request_id": "qualification.test-0001"},
            }]})

    client = ReconcileClient()
    first = asyncio.run(voice_qualification.submit(FakeMonitor(), _request(), client))
    replay = asyncio.run(voice_qualification.submit(FakeMonitor(), _request(), client))

    assert first["state"] == "uncertain"
    assert replay["state"] == "running"
    assert replay["worker_job_id"] == "voice-job-recovered"
    assert replay["worker_started_at"] is not None
    assert [call[0] for call in client.calls].count("post") == 1


def test_unmatched_uncertain_submit_releases_machine_after_safe_reconciliation_grace(reset):
    _controller(); _remote_memory()
    client = FakeClient(post=httpx.ReadTimeout("lost response"))
    first = asyncio.run(voice_qualification.submit(FakeMonitor(), _request(), client))
    stored = voice_qualification.get(first["id"])
    stored["created_at"] = time.time() - voice_qualification.SUBMIT_RECONCILIATION_GRACE_S - 1
    voice_qualification._save(stored)

    resolved = asyncio.run(voice_qualification.poll(FakeMonitor(), first["id"], FakeClient()))

    assert resolved["state"] == "failed"
    assert resolved["review_reason"] == "SUBMIT_NOT_ACCEPTED_AFTER_RECONCILIATION"
    assert voice_qualification._active_attempt_on_machine("worker-8") is False


@pytest.mark.parametrize("monitor,available,code", [
    (FakeMonitor(version="1.26.9"), 6, "VOICE_STUDIO_VERSION_TOO_OLD"),
    (FakeMonitor(cached=False), 6, "MODEL_NOT_CACHED"),
    (FakeMonitor(ready=False), 6, "MODEL_RUNTIME_NOT_READY"),
    (FakeMonitor(), 1, "INSUFFICIENT_LIVE_MEMORY"),
])
def test_preflight_rejects_unready_worker_without_submit(reset, monitor, available, code):
    _controller(); _remote_memory(available=available)
    client = FakeClient()
    with pytest.raises(voice_qualification.QualificationError) as error:
        asyncio.run(voice_qualification.submit(FakeMonitor() if monitor is None else monitor,
                                                _request(), client))
    assert error.value.code == code
    assert client.calls == []


def test_preflight_rejects_remote_worker_with_active_generation(reset):
    _controller(); _remote_memory()
    client = FakeClient(get=Response(200, {"jobs": [{"id": "other", "state": "running"}]}))
    with pytest.raises(voice_qualification.QualificationError) as error:
        asyncio.run(voice_qualification.submit(FakeMonitor(), _request(), client))
    assert error.value.code == "WORKER_NOT_IDLE"
    assert [call[0] for call in client.calls] == ["get", "get"]


def test_long_form_requires_exact_40000_character_case(reset):
    _controller(); _remote_memory()
    with pytest.raises(voice_qualification.QualificationError) as error:
        asyncio.run(voice_qualification.submit(
            FakeMonitor(), _request(case_type="long_form", text="too short"), FakeClient(),
        ))
    assert error.value.code == "LONG_FORM_TEXT_REQUIRED"


def test_wave2_requires_current_voice_studio_release(reset):
    _controller(); _remote_memory()
    with pytest.raises(voice_qualification.QualificationError) as error:
        asyncio.run(voice_qualification.submit(
            FakeMonitor(version="1.27.8"),
            _request(model=VIBEVOICE, operation="preset_tts", params={"voice": "en-Emma_woman"}),
            FakeClient(),
        ))
    assert error.value.code == "VOICE_STUDIO_VERSION_TOO_OLD"


def test_vibevoice_requires_exact_worker_roster_and_known_preset(reset):
    _controller(); _remote_memory()
    client = FakeClient(availability=Response(200, {"vibevoice_voices": VIBEVOICE_ROSTER[:-1]}))
    with pytest.raises(voice_qualification.QualificationError) as error:
        asyncio.run(voice_qualification.submit(
            FakeMonitor(),
            _request(model=VIBEVOICE, operation="preset_tts", params={"voice": "en-Emma_woman"}),
            client,
        ))
    assert error.value.code == "VIBEVOICE_ROSTER_MISMATCH"

    with pytest.raises(voice_qualification.QualificationError) as error:
        asyncio.run(voice_qualification.submit(
            FakeMonitor(),
            _request(client_request_id="qualification.test-0002", model=VIBEVOICE,
                     operation="preset_tts", params={"voice": "invented"}),
            FakeClient(),
        ))
    assert error.value.code == "VIBEVOICE_PRESET_REQUIRED"


def test_voxcpm_design_is_reference_free_but_requires_design_prompt(reset):
    _controller(); _remote_memory()
    with pytest.raises(voice_qualification.QualificationError) as error:
        asyncio.run(voice_qualification.submit(
            FakeMonitor(),
            _request(model=VOXCPM2, operation="voice_design", params={}),
            FakeClient(),
        ))
    assert error.value.code == "VOICE_DESIGN_PROMPT_REQUIRED"

    result = asyncio.run(voice_qualification.submit(
        FakeMonitor(),
        _request(client_request_id="qualification.test-0002", model=VOXCPM2,
                 operation="voice_design",
                 params={"voice_design_prompt": "calm mature documentary narrator"}),
        FakeClient(),
    ))
    assert result["state"] == "running"
    assert result["operation"] == "voice_design"


def test_omnivoice_long_form_clone_is_enabled_after_short_form_evidence(
    reset, monkeypatch, tmp_path,
):
    _controller(); _remote_memory()
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"RIFF-safe-test-reference")
    monkeypatch.setattr(
        execution_assets,
        "resolve_voice_reference",
        lambda _asset_id: ({
            "id": "a" * 24,
            "sha256": "b" * 64,
            "audio_extension": ".wav",
            "media_type": "audio/wav",
            "expires_at": time.time() + 3600,
            "transcript": "A stable, exact reference transcript.",
            "transcript_segments": [],
        }, reference),
    )
    result = asyncio.run(voice_qualification.submit(
        FakeMonitor(),
        _request(
            model=OMNIVOICE,
            operation="voice_clone",
            case_type="long_form",
            text="x" * 40_000,
            params={"omnivoice_num_steps": 32, "omnivoice_guidance_scale": 2.0},
            voice_reference_asset_id="a" * 24,
        ),
        FakeClient(),
    ))
    assert result["state"] == "running"
    assert result["operation"] == "voice_clone"


def test_fish_refuses_the_sixteen_gigabyte_tier(reset):
    """Fish was admitted on 16 and 24 GB until it was measured on real hardware:
    13.234 GB peak on a 17.2 GB machine leaves under 4 GB for macOS and whatever
    else the worker is doing, at 3.75x realtime — the slowest model measured on
    the fleet. 24 GB is the only admitted tier now, and a 16 GB worker has to be
    refused outright rather than merely discouraged."""
    _controller(); _remote_memory(tier=17.18, available=12)
    client = FakeClient()
    with pytest.raises(voice_qualification.QualificationError) as error:
        asyncio.run(voice_qualification.submit(
            FakeMonitor(version="1.27.15"),
            _request(
                model=FISH, operation="voice_clone", machine_tier_gb=16,
                params={}, voice_reference_asset_id="f" * 24,
            ),
            client,
        ))
    assert error.value.code == "MACHINE_TIER_MISMATCH"
    assert "24 GB" in str(error.value)
    assert client.calls == []


@pytest.mark.parametrize(("marketed_tier", "reported_total"), [(24, 25.77)])
def test_fish_clone_qualification_uses_evidence_tier_not_catalog_claim(
    reset, monkeypatch, tmp_path, marketed_tier, reported_total,
):
    _controller(); _remote_memory(tier=reported_total, available=12)
    _save_fish_gate("short", tier=marketed_tier)
    reference = tmp_path / "aiden-reference.mp3"
    reference.write_bytes(b"safe-aiden-reference")
    monkeypatch.setattr(
        execution_assets,
        "resolve_voice_reference",
        lambda _asset_id: ({
            "id": "f" * 24,
            "sha256": AIDEN_SOURCE_SHA256,
            "audio_extension": ".mp3",
            "media_type": "audio/mpeg",
            "expires_at": time.time() + 3600,
            "transcript": AIDEN_TRANSCRIPT,
            "transcript_segments": [],
        }, reference),
    )

    result = asyncio.run(voice_qualification.submit(
        FakeMonitor(version="1.27.15"),
        _request(
            model=FISH,
            operation="voice_clone",
            case_type="medium",
            machine_tier_gb=marketed_tier,
            text="A sentence-complete Fish qualification passage.",
            params={"fish_temperature": 0.7, "fish_top_p": 0.7, "fish_top_k": 30},
            voice_reference_asset_id="f" * 24,
        ),
        FakeClient(),
    ))

    assert result["state"] == "running"
    assert result["operation"] == "voice_clone"
    assert result["target_audio_duration_seconds"] == 300
    assert result["target"]["machine_tier_gb"] == marketed_tier
    assert result["target"]["reported_catalog_minimum_total_memory_gb"] == 24
    assert result["target"]["minimum_total_memory_gb"] is None
    assert result["target"]["minimum_free_memory_gb"] == 8.0
    assert result["target"]["reference_source_sha256"] == AIDEN_SOURCE_SHA256


def test_fish_rejects_8gb_tier_before_reference_or_worker_calls(reset):
    _controller(); _remote_memory(tier=8, available=7)
    client = FakeClient()
    with pytest.raises(voice_qualification.QualificationError) as error:
        asyncio.run(voice_qualification.submit(
            FakeMonitor(version="1.27.15"),
            _request(
                model=FISH, operation="voice_clone", machine_tier_gb=8,
                params={}, voice_reference_asset_id="f" * 24,
            ),
            client,
        ))
    assert error.value.code == "MACHINE_TIER_MISMATCH"
    assert client.calls == []


def test_fish_requires_checksum_bound_aiden_reference(reset, monkeypatch, tmp_path):
    _controller(); _remote_memory(tier=25.77, available=12)
    reference = tmp_path / "not-aiden.wav"
    reference.write_bytes(b"RIFF-not-aiden")
    monkeypatch.setattr(
        execution_assets,
        "resolve_voice_reference",
        lambda _asset_id: ({
            "id": "f" * 24, "sha256": "b" * 64, "audio_extension": ".wav",
            "media_type": "audio/wav", "expires_at": time.time() + 3600,
            "transcript": AIDEN_TRANSCRIPT, "transcript_segments": [],
        }, reference),
    )
    client = FakeClient()
    with pytest.raises(voice_qualification.QualificationError) as error:
        asyncio.run(voice_qualification.submit(
            FakeMonitor(version="1.27.15"),
            _request(
                model=FISH, operation="voice_clone", machine_tier_gb=24,
                params={}, voice_reference_asset_id="f" * 24,
            ),
            client,
        ))
    assert error.value.code == "FISH_AIDEN_REFERENCE_REQUIRED"
    assert client.calls == []


def test_fish_medium_and_long_form_require_ordered_same_tier_successes(reset):
    _controller(); _remote_memory(tier=25.77, available=12)
    with pytest.raises(voice_qualification.QualificationError) as error:
        asyncio.run(voice_qualification.submit(
            FakeMonitor(version="1.27.15"),
            _request(
                model=FISH, operation="voice_clone", case_type="medium",
                machine_tier_gb=24, params={}, voice_reference_asset_id="f" * 24,
            ),
            FakeClient(),
        ))
    assert error.value.code == "FISH_QUALIFICATION_GATE_REQUIRED"

    _save_fish_gate("short", tier=24)
    with pytest.raises(voice_qualification.QualificationError) as error:
        asyncio.run(voice_qualification.submit(
            FakeMonitor(version="1.27.15"),
            _request(
                client_request_id="qualification.test-0002",
                model=FISH, operation="voice_clone", case_type="long_form",
                machine_tier_gb=24, params={}, voice_reference_asset_id="f" * 24,
            ),
            FakeClient(),
        ))
    assert error.value.code == "FISH_QUALIFICATION_GATE_REQUIRED"


def test_fish_long_form_targets_fifteen_minutes_without_legacy_40k_requirement(
    reset, monkeypatch, tmp_path,
):
    _controller(); _remote_memory(tier=25.77, available=12)
    _save_fish_gate("short", tier=24)
    _save_fish_gate("medium", tier=24)
    reference = tmp_path / "aiden.wav"
    reference.write_bytes(b"RIFF-safe-aiden")
    monkeypatch.setattr(
        execution_assets,
        "resolve_voice_reference",
        lambda _asset_id: ({
            "id": "f" * 24, "sha256": AIDEN_SOURCE_SHA256, "audio_extension": ".wav",
            "media_type": "audio/wav", "expires_at": time.time() + 3600,
            "transcript": AIDEN_TRANSCRIPT,
            "transcript_segments": [],
        }, reference),
    )
    result = asyncio.run(voice_qualification.submit(
        FakeMonitor(version="1.27.15"),
        _request(
            model=FISH, operation="voice_clone", case_type="long_form",
            machine_tier_gb=24, text="A complete sentence, well below forty thousand characters.",
            params={}, voice_reference_asset_id="f" * 24,
        ),
        FakeClient(),
    ))

    assert result["state"] == "running"
    assert result["target_audio_duration_seconds"] == 900


def test_qwen_requires_the_exact_worker_reported_nine_speaker_roster(reset):
    _controller(); _remote_memory()
    client = FakeClient(availability=Response(200, {"qwen3_preset_speakers": QWEN_ROSTER[:-1]}))
    with pytest.raises(voice_qualification.QualificationError) as error:
        asyncio.run(voice_qualification.submit(FakeMonitor(), _request(), client))
    assert error.value.code == "QWEN_CUSTOMVOICE_ROSTER_MISMATCH"
    assert client.calls and all(call[0] == "get" for call in client.calls)
    assert voice_qualification.list_attempts() == []


def test_chatterbox_requires_exact_23_language_contract_before_asset_or_submit(reset):
    _controller(); _remote_memory()
    monitor = FakeMonitor()
    for entry in monitor.cached_aggregate_catalog()["models"]:
        if entry["repo"] == CHATTERBOX and entry["hub_studio"] == "voice@worker-8":
            entry["language_support"]["codes"] = ["en"]
    # Keep the modified aggregate alive for this test rather than changing the
    # production fixture's complete roster.
    monitor.cached_aggregate_catalog = lambda: {"models": [
        {
            **entry,
            "language_support": ({**entry["language_support"], "codes": ["en"]}
                if entry["repo"] == CHATTERBOX and entry["hub_studio"] == "voice@worker-8"
                else entry.get("language_support")),
        }
        for entry in FakeMonitor().cached_aggregate_catalog()["models"]
    ]}
    client = FakeClient()
    with pytest.raises(voice_qualification.QualificationError) as error:
        asyncio.run(voice_qualification.submit(
            monitor, _request(model=CHATTERBOX, params={"language": "en"},
                              voice_reference_asset_id="0" * 24), client,
        ))
    assert error.value.code == "CHATTERBOX_LANGUAGE_ROSTER_MISMATCH"
    assert client.calls == []
    assert voice_qualification.list_attempts() == []


def test_missing_clone_reference_is_a_preflight_failure_before_worker_submit(reset):
    _controller(); _remote_memory()
    client = FakeClient()
    with pytest.raises(voice_qualification.QualificationError) as error:
        asyncio.run(voice_qualification.submit(
            FakeMonitor(), _request(model=BASE, params={}, voice_reference_asset_id="0" * 24), client,
        ))
    assert error.value.code == "REFERENCE_ASSET_UNAVAILABLE"
    assert client.calls == []
    assert voice_qualification.list_attempts() == []


def test_controller_local_requires_explicit_opt_in_and_stores_physical_controller_id(reset, monkeypatch):
    _controller()
    control_plane.save_settings({"role": "controller", "site_id": "site-a",
                                 "site_name": "Site A", "controller_id": "site-a-0200",
                                 "database_mode": "off"})
    monkeypatch.setattr(voice_qualification, "host_stats", lambda: {
        "total_gb": 25.8, "available_gb": 20.0,
    })
    client = FakeClient()
    result = asyncio.run(voice_qualification.submit(
        FakeMonitor(), _request(target_studio_id="voice", machine_tier_gb=24,
                                allow_controller_local=True), client,
    ))
    assert result["state"] == "running"
    assert result["target"]["machine_id"] == "site-a-0200"
    assert result["target"]["registry_machine_id"] == "local"
    assert result["target"]["execution_path"] == "controller_local"
    assert client.calls[2][1].startswith("http://127.0.0.1:47864/")


def test_excluded_physical_machine_blocks_controller_local_before_worker_calls(reset, monkeypatch):
    _controller()
    control_plane.save_settings({"role": "controller", "site_id": "site-a",
                                 "site_name": "Site A", "controller_id": "site-a-0205",
                                 "database_mode": "off"})
    monkeypatch.setattr(voice_qualification, "host_stats", lambda: {
        "total_gb": 25.8, "available_gb": 20.0,
    })
    client = FakeClient()
    with pytest.raises(voice_qualification.QualificationError) as error:
        asyncio.run(voice_qualification.submit(
            FakeMonitor(), _request(target_studio_id="voice", machine_tier_gb=24,
                                    allow_controller_local=True,
                                    excluded_machine_ids=["site-a-0205"]), client,
        ))
    assert error.value.code == "TARGET_MACHINE_EXCLUDED"
    assert client.calls == []
    assert voice_qualification.list_attempts() == []


def test_poll_relays_real_voicestudio_v11_terminal_evidence_without_worker_secrets(reset):
    _controller(); _remote_memory()
    submit_client = FakeClient()
    started = asyncio.run(voice_qualification.submit(FakeMonitor(), _request(), submit_client))
    poll_client = FakeClient(get=Response(200, {"job": {
        "id": "voice-job-1", "state": "done", "model_revision": REVISION,
        "integration_name": "voicestudio_genstudio_integration", "integration_version": "1.1",
        "internal_model_id": CUSTOM, "runtime_revision": REVISION,
        "bytes": 4567, "sha256": "b" * 64, "audio_duration_ms": 1234,
        "reference_source_sha256": "c" * 64, "reference_audio_sha256": "d" * 64,
        "reference_preparation_revision": "voice-reference-v1",
        "output_url": "http://private-worker/audio", "output_path": "/private/path.wav",
        "error": "raw worker error", "resource_usage": {
            "schema": "voicestudio.resource-telemetry", "schema_version": 1,
            "host": {"minimum_available_gb": 2.5, "secret": "no"},
        },
    }}))
    result = asyncio.run(voice_qualification.poll(FakeMonitor(), started["id"], poll_client))
    rendered = str(result)

    assert result["state"] == "succeeded"
    assert result["terminal_evidence"]["artifact"]["bytes"] == 4567
    assert result["terminal_evidence"]["artifact"]["sha256"] == "b" * 64
    assert result["terminal_evidence"]["artifact"]["integration_version"] == "1.1"
    assert result["terminal_evidence"]["artifact"]["reference_preparation_revision"] == "voice-reference-v1"
    assert result["terminal_evidence"]["resource_usage"]["host"] == {"minimum_available_gb": 2.5}
    assert "private-worker" not in rendered and "private/path" not in rendered and "raw worker" not in rendered


def test_cancel_is_durable_and_uses_remote_hub_worker_path(reset):
    _controller(); _remote_memory()
    started = asyncio.run(voice_qualification.submit(FakeMonitor(), _request(), FakeClient()))
    client = FakeClient(delete=Response(200, {"ok": True}))
    result = asyncio.run(voice_qualification.cancel(FakeMonitor(), started["id"], client))

    assert result["state"] == "cancel_requested"
    assert result["cancel_requested_at"] is not None
    assert client.calls[0][0] == "delete"
    assert client.calls[0][1].startswith("http://100.64.0.8:47873/studio/voice/api/generate/jobs/")


def test_fish_poll_cancels_exact_job_after_ten_x_speed_stop(reset, monkeypatch, tmp_path):
    _controller(); _remote_memory(tier=25.77, available=12)
    reference = tmp_path / "aiden.wav"
    reference.write_bytes(b"RIFF-safe-aiden")
    monkeypatch.setattr(
        execution_assets,
        "resolve_voice_reference",
        lambda _asset_id: ({
            "id": "f" * 24, "sha256": AIDEN_SOURCE_SHA256, "audio_extension": ".wav",
            "media_type": "audio/wav", "expires_at": time.time() + 3600,
            "transcript": AIDEN_TRANSCRIPT,
            "transcript_segments": [],
        }, reference),
    )
    started = asyncio.run(voice_qualification.submit(
        FakeMonitor(version="1.27.15"),
        _request(
            model=FISH, operation="voice_clone", case_type="short",
            machine_tier_gb=24, params={}, voice_reference_asset_id="f" * 24,
        ),
        FakeClient(),
    ))
    stored = voice_qualification.get(started["id"])
    stored["accepted_at"] = time.time() - 301
    voice_qualification._save(stored)
    client = FakeClient(
        get=Response(200, {"job": {
            "id": "voice-job-1", "state": "running", "progress": 0.1,
        }}),
        delete=Response(200, {"ok": True}),
    )

    stopped = asyncio.run(voice_qualification.poll(FakeMonitor(), started["id"], client))

    assert stopped["state"] == "cancel_requested"
    assert stopped["review_reason"] == "SLOWDOWN_RATIO_STOP"
    assert stopped["stop_reason"] == "SLOWDOWN_RATIO_STOP"
    assert stopped["cancel_requested_at"] is not None
    assert [call[0] for call in client.calls] == ["get", "delete"]
    assert client.calls[1][1].endswith("/api/generate/jobs/voice-job-1")


def test_fish_success_requires_and_derives_exact_duration_artifact_evidence(
    reset, monkeypatch, tmp_path,
):
    _controller(); _remote_memory(tier=25.77, available=12)
    reference = tmp_path / "aiden.wav"
    reference.write_bytes(b"RIFF-safe-aiden")
    monkeypatch.setattr(
        execution_assets,
        "resolve_voice_reference",
        lambda _asset_id: ({
            "id": "f" * 24, "sha256": AIDEN_SOURCE_SHA256, "audio_extension": ".wav",
            "media_type": "audio/wav", "expires_at": time.time() + 3600,
            "transcript": AIDEN_TRANSCRIPT, "transcript_segments": [],
        }, reference),
    )
    started = asyncio.run(voice_qualification.submit(
        FakeMonitor(version="1.27.15"),
        _request(
            model=FISH, operation="voice_clone", case_type="short",
            machine_tier_gb=24, params={}, voice_reference_asset_id="f" * 24,
        ),
        FakeClient(),
    ))
    terminal = {
        "id": "voice-job-1", "state": "done", "model_revision": REVISION,
        "integration_name": "voicestudio_genstudio_integration",
        "integration_version": "1.1", "internal_model_id": FISH,
        "runtime_revision": REVISION, "runtime_s": 60.0,
        "media_type": "audio/wav", "format": "wav", "bytes": 960_000,
        "sha256": "c" * 64, "audio_duration_s": 30.0, "audio_duration_ms": 30_000,
        "sample_rate_hz": 44_100, "channels": 1,
        "reference_source_sha256": AIDEN_SOURCE_SHA256,
        "reference_audio_sha256": "d" * 64,
        "reference_preparation_revision": "voice-reference-v1",
        "reference_duration_s": 10.0,
        "resource_usage": {
            "schema": "voicestudio.resource-telemetry", "schema_version": 1,
            "host": {"minimum_available_gb": 8.25},
        },
    }
    result = asyncio.run(voice_qualification.poll(
        FakeMonitor(version="1.27.15"), started["id"],
        FakeClient(get=Response(200, {"job": terminal})),
    ))

    artifact = result["terminal_evidence"]["artifact"]
    assert result["state"] == "succeeded"
    assert artifact["slowdown_ratio"] == 2.0
    assert artifact["inverse_realtime_throughput"] == 0.5
    assert artifact["target_audio_duration_seconds"] == 30.0


def test_fish_output_outside_duration_tolerance_is_not_a_pass(
    reset, monkeypatch, tmp_path,
):
    _controller(); _remote_memory(tier=25.77, available=12)
    reference = tmp_path / "aiden.wav"
    reference.write_bytes(b"RIFF-safe-aiden")
    monkeypatch.setattr(
        execution_assets,
        "resolve_voice_reference",
        lambda _asset_id: ({
            "id": "f" * 24, "sha256": AIDEN_SOURCE_SHA256, "audio_extension": ".wav",
            "media_type": "audio/wav", "expires_at": time.time() + 3600,
            "transcript": AIDEN_TRANSCRIPT, "transcript_segments": [],
        }, reference),
    )
    started = asyncio.run(voice_qualification.submit(
        FakeMonitor(version="1.27.15"),
        _request(
            model=FISH, operation="voice_clone", case_type="short",
            machine_tier_gb=24, params={}, voice_reference_asset_id="f" * 24,
        ),
        FakeClient(),
    ))
    terminal = {
        "id": "voice-job-1", "state": "done", "model_revision": REVISION,
        "integration_name": "voicestudio_genstudio_integration",
        "integration_version": "1.1", "internal_model_id": FISH,
        "runtime_revision": REVISION, "runtime_s": 40.0,
        "media_type": "audio/wav", "format": "wav", "bytes": 640_000,
        "sha256": "c" * 64, "audio_duration_s": 20.0, "audio_duration_ms": 20_000,
        "sample_rate_hz": 44_100, "channels": 1,
        "reference_source_sha256": AIDEN_SOURCE_SHA256,
        "reference_audio_sha256": "d" * 64,
        "reference_preparation_revision": "voice-reference-v1",
        "reference_duration_s": 10.0,
    }
    result = asyncio.run(voice_qualification.poll(
        FakeMonitor(version="1.27.15"), started["id"],
        FakeClient(get=Response(200, {"job": terminal})),
    ))

    assert result["state"] == "failed"
    assert result["review_reason"] == "FISH_AUDIO_DURATION_TARGET_MISSED"


def test_fish_done_response_cannot_erase_prior_ten_x_stop_decision(reset):
    now = time.time()
    voice_qualification._save({
        "id": "vq-stop-race", "client_request_id": "qualification.stop-race",
        "request_fingerprint": "e" * 64, "created_at": now,
        "state": "cancel_requested", "case_type": "short", "model": FISH,
        "operation": "voice_clone", "target": {
            "studio_id": "voice@worker-8", "machine_id": "worker-8",
            "runtime_revision": REVISION,
        },
        "worker_job_id": "voice-job-1", "progress": 0.5,
        "cancel_requested_at": now, "terminal_evidence": None,
        "review_reason": "SLOWDOWN_RATIO_STOP", "stop_reason": "SLOWDOWN_RATIO_STOP",
        "target_audio_duration_seconds": 30, "accepted_at": now - 310,
        "worker_started_at": now - 305,
    })
    terminal = {
        "id": "voice-job-1", "state": "done", "model_revision": REVISION,
        "integration_name": "voicestudio_genstudio_integration",
        "integration_version": "1.1", "internal_model_id": FISH,
        "runtime_revision": REVISION, "runtime_s": 305.0,
        "media_type": "audio/wav", "format": "wav", "bytes": 960_000,
        "sha256": "c" * 64, "audio_duration_s": 30.0, "audio_duration_ms": 30_000,
        "sample_rate_hz": 44_100, "channels": 1,
        "reference_source_sha256": AIDEN_SOURCE_SHA256,
        "reference_audio_sha256": "d" * 64,
        "reference_preparation_revision": "voice-reference-v1",
        "reference_duration_s": 10.0,
    }

    result = asyncio.run(voice_qualification.poll(
        FakeMonitor(version="1.27.15"), "vq-stop-race",
        FakeClient(get=Response(200, {"job": terminal})),
    ))

    assert result["state"] == "cancelled"
    assert result["stop_reason"] == "SLOWDOWN_RATIO_STOP"
    assert result["review_reason"] == "SLOWDOWN_RATIO_STOP"


def test_fish_medium_requires_strategy_and_expected_300_character_chunk_count(reset):
    now = time.time()
    voice_qualification._save({
        "id": "vq-medium-chunks", "client_request_id": "qualification.medium-chunks",
        "request_fingerprint": "8" * 64, "created_at": now,
        "state": "running", "case_type": "medium", "model": FISH,
        "operation": "voice_clone", "target": {
            "studio_id": "voice@worker-8", "machine_id": "worker-8",
            "runtime_revision": REVISION,
        },
        "worker_job_id": "voice-job-1", "progress": 1.0,
        "terminal_evidence": None, "review_reason": None, "stop_reason": None,
        "target_audio_duration_seconds": 300, "input_text_characters": 901,
        "accepted_at": now - 600, "worker_started_at": now - 600,
    })
    terminal = {
        "id": "voice-job-1", "state": "done", "model_revision": REVISION,
        "integration_name": "voicestudio_genstudio_integration",
        "integration_version": "1.1", "internal_model_id": FISH,
        "runtime_revision": REVISION, "runtime_s": 600.0,
        "media_type": "audio/wav", "format": "wav", "bytes": 9_600_000,
        "sha256": "c" * 64, "audio_duration_s": 300.0,
        "audio_duration_ms": 300_000, "sample_rate_hz": 44_100, "channels": 1,
        "reference_source_sha256": AIDEN_SOURCE_SHA256,
        "reference_audio_sha256": "d" * 64,
        "reference_preparation_revision": "voice-reference-v1",
        "reference_duration_s": 10.0, "long_form_strategy": "",
        "chunk_total": 3,
    }
    incomplete = asyncio.run(voice_qualification.poll(
        FakeMonitor(version="1.27.15"), "vq-medium-chunks",
        FakeClient(get=Response(200, {"job": terminal})),
    ))
    assert incomplete["state"] == "uncertain"
    assert incomplete["review_reason"] == "FISH_CHUNK_EVIDENCE_MISSING"

    terminal.update(long_form_strategy="sentence_safe_300", chunk_total=4)
    complete = asyncio.run(voice_qualification.poll(
        FakeMonitor(version="1.27.15"), "vq-medium-chunks",
        FakeClient(get=Response(200, {"job": terminal})),
    ))
    assert complete["state"] == "succeeded"


def test_resource_telemetry_rejects_strings_nonfinite_and_unknown_enums():
    clean = voice_qualification._sanitize_resource_usage({
        "schema": "voicestudio.resource-telemetry", "schema_version": 1,
        "host": {
            "minimum_available_gb": "8.0", "maximum_used_gb": float("inf"),
            "peak_pressure_level": "invented", "available_gb_end": 8.0,
        },
        "mlx": {"supported": "yes", "peak_active_gb": 4.5},
        "outcome": {"state": "done", "memory_failure": False},
    })

    assert clean["host"] == {"available_gb_end": 8.0}
    assert clean["mlx"] == {"peak_active_gb": 4.5}
    assert clean["outcome"] == {"state": "done", "memory_failure": False}
    assert voice_qualification._sanitize_resource_usage({
        "schema": "voicestudio.resource-telemetry", "schema_version": True,
    }) is None


def test_qualification_never_mutates_model_approval_or_catalog(reset):
    _controller(); _remote_memory()
    before = model_exposure.records()
    asyncio.run(voice_qualification.submit(FakeMonitor(), _request(), FakeClient()))
    after = model_exposure.records()

    assert before == after == []


def test_machine_token_can_collect_qualification_evidence_without_model_approval(authed):
    listing = authed.get("/api/hub/admin/voice-qualifications")
    exposure = authed.post(
        "/api/hub/model-exposures/approve",
        json={"candidate_key": "a" * 64},
    )

    assert listing.status_code == 200
    assert listing.json() == {"attempts": []}
    assert exposure.status_code == 403


def test_qualification_artifact_endpoint_never_exposes_unready_worker_location(authed, monkeypatch):
    monkeypatch.setattr(main.voice_qualification, "get", lambda _attempt_id: {
        "id": "vq-test", "state": "running", "worker_job_id": "private-worker-job",
        "target": {"studio_id": "voice@private-machine"},
    })
    response = authed.get("/api/hub/admin/voice-qualifications/vq-test/artifact")

    assert response.status_code == 425
    assert "private-machine" not in response.text
    assert "private-worker-job" not in response.text
