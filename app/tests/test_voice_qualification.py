import asyncio
import time

import httpx
import pytest

from backend import control_plane, main, model_exposure, peers, voice_qualification


CUSTOM = "mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-8bit"
BASE = "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-8bit"
CHATTERBOX = "mlx-community/chatterbox-4bit"
VOXCPM2 = "mlx-community/VoxCPM2-4bit"
VIBEVOICE = "mlx-community/VibeVoice-Realtime-0.5B-4bit"
OMNIVOICE = "mlx-community/OmniVoice-bfloat16"
REVISION = "a" * 64
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
            for repo in (CUSTOM, BASE, CHATTERBOX, VOXCPM2, VIBEVOICE, OMNIVOICE):
                entry = {
                    "repo": repo, "hub_studio": studio_id, "hub_cached": self._cached,
                    "hub_catalog_stale": False, "runtime_compatible": self._ready,
                    "hub_ready": self._ready, "runtime_revision": REVISION,
                    "min_unified_memory_gb": 8, "min_free_memory_gb": 2,
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
    assert len(client.calls) == 3


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


def test_omnivoice_long_form_waits_for_short_form_adapter_evidence(reset):
    _controller(); _remote_memory()
    with pytest.raises(voice_qualification.QualificationError) as error:
        asyncio.run(voice_qualification.submit(
            FakeMonitor(),
            _request(model=OMNIVOICE, operation="voice_design", case_type="long_form",
                     text="x" * 40_000,
                     params={"voice_design_prompt": "female, warm, clear"}),
            FakeClient(),
        ))
    assert error.value.code == "LONG_FORM_ADAPTER_NOT_READY"


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
                                 "site_name": "Site A", "controller_id": "terranash-0200",
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
    assert result["target"]["machine_id"] == "terranash-0200"
    assert result["target"]["registry_machine_id"] == "local"
    assert result["target"]["execution_path"] == "controller_local"
    assert client.calls[2][1].startswith("http://127.0.0.1:47864/")


def test_excluded_physical_machine_blocks_controller_local_before_worker_calls(reset, monkeypatch):
    _controller()
    control_plane.save_settings({"role": "controller", "site_id": "site-a",
                                 "site_name": "Site A", "controller_id": "terranash-0205",
                                 "database_mode": "off"})
    monkeypatch.setattr(voice_qualification, "host_stats", lambda: {
        "total_gb": 25.8, "available_gb": 20.0,
    })
    client = FakeClient()
    with pytest.raises(voice_qualification.QualificationError) as error:
        asyncio.run(voice_qualification.submit(
            FakeMonitor(), _request(target_studio_id="voice", machine_tier_gb=24,
                                    allow_controller_local=True,
                                    excluded_machine_ids=["terranash-0205"]), client,
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
