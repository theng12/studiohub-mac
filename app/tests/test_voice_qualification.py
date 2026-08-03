import asyncio
import time

import httpx
import pytest

from backend import control_plane, model_exposure, peers, voice_qualification


CUSTOM = "mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-8bit"
BASE = "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-8bit"
CHATTERBOX = "mlx-community/chatterbox-4bit"
REVISION = "a" * 64


class FakeMonitor:
    def __init__(self, *, version="1.27.0", cached=True, ready=True, machine="worker-8"):
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
        return {"models": [{
            "repo": CUSTOM, "hub_studio": "voice@worker-8", "hub_cached": self._cached,
            "hub_catalog_stale": False, "runtime_compatible": self._ready,
            "hub_ready": self._ready, "runtime_revision": REVISION,
            "min_unified_memory_gb": 8, "min_free_memory_gb": 2,
        }, {
            "repo": BASE, "hub_studio": "voice@worker-8", "hub_cached": self._cached,
            "hub_catalog_stale": False, "runtime_compatible": self._ready,
            "hub_ready": self._ready, "runtime_revision": REVISION,
            "min_unified_memory_gb": 8, "min_free_memory_gb": 2,
        }, {
            "repo": CHATTERBOX, "hub_studio": "voice@worker-8", "hub_cached": self._cached,
            "hub_catalog_stale": False, "runtime_compatible": self._ready,
            "hub_ready": self._ready, "runtime_revision": REVISION,
            "min_unified_memory_gb": 8, "min_free_memory_gb": 2,
        }]}


class Response:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {}

    def json(self):
        return self._body


class FakeClient:
    def __init__(self, *, post=None, get=None, delete=None):
        self.post_result = post or Response(200, {"job": {"id": "voice-job-1"}})
        self.get_result = get or Response(200, {"jobs": []})
        self.delete_result = delete or Response(200, {"ok": True})
        self.calls = []

    async def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        if isinstance(self.post_result, Exception):
            raise self.post_result
        return self.post_result

    async def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        if isinstance(self.get_result, Exception):
            raise self.get_result
        return self.get_result

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
    assert [call[0] for call in client.calls] == ["get", "post"]
    assert client.calls[1][1].startswith("http://100.64.0.8:47873/studio/voice/")
    assert "/api/generate/txt2speech" in client.calls[1][1]


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
    with pytest.raises(voice_qualification.QualificationError, match="remote worker") as error:
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
    assert len(client.calls) == 2


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
    assert [call[0] for call in client.calls] == ["get"]


def test_long_form_requires_exact_40000_character_case(reset):
    _controller(); _remote_memory()
    with pytest.raises(voice_qualification.QualificationError) as error:
        asyncio.run(voice_qualification.submit(
            FakeMonitor(), _request(case_type="long_form", text="too short"), FakeClient(),
        ))
    assert error.value.code == "LONG_FORM_TEXT_REQUIRED"


def test_poll_sanitizes_terminal_evidence_and_never_leaks_worker_endpoint_or_error(reset):
    _controller(); _remote_memory()
    submit_client = FakeClient()
    started = asyncio.run(voice_qualification.submit(FakeMonitor(), _request(), submit_client))
    poll_client = FakeClient(get=Response(200, {"job": {
        "id": "voice-job-1", "state": "done", "model_revision": REVISION,
        "audio_duration_ms": 1234, "audio_sha256": "b" * 64,
        "output_url": "http://private-worker/audio", "output_path": "/private/path.wav",
        "error": "raw worker error", "resource_usage": {
            "schema": "voicestudio.resource-telemetry", "schema_version": 1,
            "host": {"minimum_available_gb": 2.5, "secret": "no"},
        },
    }}))
    result = asyncio.run(voice_qualification.poll(FakeMonitor(), started["id"], poll_client))
    rendered = str(result)

    assert result["state"] == "succeeded"
    assert result["terminal_evidence"]["artifact"]["audio_duration_ms"] == 1234
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
