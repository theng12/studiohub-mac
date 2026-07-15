from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest

from backend import main, shared_voices


def _create(**overrides):
    values = {
        "audio_bytes": b"small-reference-audio",
        "filename": "aiden.wav",
        "name": "Aiden",
        "language": "en",
        "gender": "m",
        "license": "self-owned",
        "notes": "Fresh shared reference",
        "source_url": None,
        "transcript": "A reviewed transcript.",
        "permission_acknowledged": True,
    }
    values.update(overrides)
    return shared_voices.create(**values)


def test_canonical_voice_uses_stable_id_hash_and_reviewed_transcript(reset, monitor):
    voice = _create()
    stored = shared_voices.list_voices(monitor)

    assert len(stored) == 1
    assert stored[0]["id"] == voice["id"]
    assert len(voice["id"]) == 12
    assert voice["audio_sha256"] == hashlib.sha256(b"small-reference-audio").hexdigest()
    assert stored[0]["transcript"] == "A reviewed transcript."
    assert stored[0]["sync"]["total"] == 1
    assert stored[0]["sync"]["pending"] == 1
    assert shared_voices.audio_path(voice["id"]).read_bytes() == b"small-reference-audio"


def test_shared_voice_validation_rejects_unsafe_or_unapproved_uploads(reset):
    with pytest.raises(ValueError, match="permission"):
        _create(permission_acknowledged=False)
    with pytest.raises(ValueError, match="filename"):
        _create(filename="../aiden.wav")
    with pytest.raises(ValueError, match="unsupported audio"):
        _create(filename="aiden.exe")
    with pytest.raises(ValueError, match="source URL"):
        _create(source_url="file:///tmp/private")


class _Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


@pytest.mark.asyncio
async def test_connection_drop_stays_pending_then_self_heals_on_retry(reset, monitor, monkeypatch):
    voice = _create()
    studio = next(s for s in monitor.registry if s["id"] == "voice")
    monitor.status[studio["id"]] = {"status": "up"}
    calls = 0

    async def put(url, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("restart dropped the connection")
        return _Response(200, {
            "voice": {"id": voice["id"]},
            "sync": {"status": "created", "sha256": voice["audio_sha256"]},
        })

    monkeypatch.setattr(monitor._client, "put", put)
    first = await shared_voices.sync_voice(monitor, voice["id"])
    assert first["targets"][0]["status"] == "pending"
    assert "automatic retry" in first["targets"][0]["message"]

    await shared_voices.reconcile_once(monitor)
    healed = shared_voices.list_voices(monitor)[0]
    assert healed["sync"]["synced"] == 1
    assert healed["targets"][0]["remote_action"] == "created"
    assert calls == 2


@pytest.mark.asyncio
async def test_remote_voice_routes_through_peer_hub_and_reports_old_worker(reset, monitor, monkeypatch):
    voice = _create()
    local = next(s for s in monitor.registry if s["id"] == "voice")
    remote = {**local, "id": "voice@mac-b", "machine": "mac-b", "host": "10.0.0.2"}
    monitor.registry = [remote]
    monitor.status[remote["id"]] = {"status": "up"}
    captured = {}

    async def put(url, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs["headers"]
        return _Response(404, {"detail": "Not Found"})

    monkeypatch.setattr(monitor._client, "put", put)
    result = await shared_voices.sync_voice(monitor, voice["id"])

    assert captured["url"].startswith("http://10.0.0.2:47873/studio/voice/")
    assert "X-Hub-Token" in captured["headers"]
    assert result["targets"][0]["status"] == "unsupported"
    assert "v1.19.0" in result["targets"][0]["message"]


def test_authenticated_create_route_starts_sync(authed, client, monkeypatch):
    monkeypatch.setattr(shared_voices, "start_sync", lambda monitor, voice_id: True)
    data = {
        "name": "Aiden", "language": "en", "gender": "m",
        "license": "self-owned", "transcript": "Reviewed words.",
        "permission_acknowledged": "true",
    }
    files = {"audio": ("aiden.wav", b"audio", "audio/wav")}

    assert client.post("/api/hub/shared-voices", data=data, files=files).status_code == 401
    response = authed.post("/api/hub/shared-voices", data=data, files=files)
    assert response.status_code == 200
    assert response.json()["sync_started"] is True
    assert response.json()["voice"]["transcript"] == "Reviewed words."


def test_in_hub_transcription_returns_editable_plain_text(authed, monkeypatch):
    async def transcribe(*args, **kwargs):
        return {
            "srt": "1\n00:00:00,000 --> 00:00:01,000\nHello there.\n\n"
                   "2\n00:00:01,000 --> 00:00:02,000\nFresh voice.\n",
            "language": "en", "elapsed_seconds": 1.2,
        }

    monkeypatch.setattr(main, "_run_single_transcription", transcribe)
    response = authed.post(
        "/api/hub/shared-voices/transcribe",
        data={"model": "mlx-community/whisper-large-v3", "language": "en"},
        files={"audio": ("aiden.wav", b"audio", "audio/wav")},
    )
    assert response.status_code == 200
    assert response.json()["transcript"] == "Hello there. Fresh voice."


def test_dashboard_exposes_transcribe_review_and_sync_workflow():
    source = (Path(__file__).parents[1] / "frontend" / "index.html").read_text()
    assert 'data-tab="voices"' in source
    assert "Transcribe in Hub" in source
    assert "review and correct before saving" in source
    assert "Save &amp; sync to all Macs" in source
    assert "loadSharedVoices" in source
    assert "Connection drops retry automatically" in source
