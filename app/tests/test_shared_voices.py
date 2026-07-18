from __future__ import annotations

import asyncio
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


def test_synced_studio_ids_distinguishes_hub_and_direct_voice_ids(reset):
    voice = _create()
    shared_voices._save_state(voice["id"], {"targets": {
        "voice@mac-a": {"studio_id": "voice@mac-a", "status": "synced"},
        "voice@mac-b": {"studio_id": "voice@mac-b", "status": "pending"},
    }})

    assert shared_voices.synced_studio_ids(voice["id"]) == {"voice@mac-a"}
    assert shared_voices.synced_studio_ids("ffffffffffff") is None


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
async def test_rename_keeps_stable_id_and_updates_worker_metadata(reset, monitor, monkeypatch):
    voice = _create()
    studio = next(s for s in monitor.registry if s["id"] == "voice")
    monitor.status[studio["id"]] = {"status": "up"}
    captured = {}

    async def put(url, **kwargs):
        captured.update(kwargs["data"])
        return _Response(200, {
            "voice": {"id": voice["id"]},
            "sync": {"status": "updated", "sha256": voice["audio_sha256"]},
        })

    monkeypatch.setattr(monitor._client, "put", put)
    renamed = shared_voices.update(voice["id"], {"name": "Aiden — Calm"})
    result = await shared_voices.sync_voice(monitor, voice["id"])

    assert renamed["id"] == voice["id"]
    assert renamed["audio_sha256"] == voice["audio_sha256"]
    assert captured["name"] == "Aiden — Calm"
    assert result["sync"]["synced"] == 1


@pytest.mark.asyncio
async def test_rename_during_active_sync_queues_a_fresh_metadata_pass(reset, monitor, monkeypatch):
    voice = _create()
    studio = next(s for s in monitor.registry if s["id"] == "voice")
    monitor.status[studio["id"]] = {"status": "up"}
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    names = []

    async def put(url, **kwargs):
        names.append(kwargs["data"]["name"])
        if len(names) == 1:
            first_started.set()
            await release_first.wait()
        return _Response(200, {
            "voice": {"id": voice["id"]},
            "sync": {"status": "updated", "sha256": voice["audio_sha256"]},
        })

    monkeypatch.setattr(monitor._client, "put", put)
    assert shared_voices.start_sync(monitor, voice["id"]) is True
    await first_started.wait()
    shared_voices.update(voice["id"], {"name": "Aiden Latest"})
    assert shared_voices.start_sync(monitor, voice["id"]) is False
    release_first.set()
    for _ in range(20):
        if len(names) == 2 and voice["id"] not in shared_voices._tasks:
            break
        await asyncio.sleep(0)

    assert names == ["Aiden", "Aiden Latest"]


@pytest.mark.asyncio
async def test_delete_removes_master_and_exact_managed_worker_copy(reset, monitor, monkeypatch):
    voice = _create()
    studio = next(s for s in monitor.registry if s["id"] == "voice")
    monitor.status[studio["id"]] = {"status": "up"}
    captured = {}

    async def delete(url, **kwargs):
        captured["url"] = url
        captured["params"] = kwargs["params"]
        return _Response(200, {"deleted": voice["id"], "sha256": voice["audio_sha256"]})

    monkeypatch.setattr(monitor._client, "delete", delete)
    result = await shared_voices.delete_voice(monitor, voice["id"])

    assert shared_voices.audio_path(voice["id"]) is None
    assert shared_voices.list_voices(monitor) == []
    assert captured["url"].endswith(f"/api/voices/{voice['id']}/fleet-sync")
    assert captured["params"] == {"audio_sha256": voice["audio_sha256"]}
    assert result["sync"]["deleted"] == 1
    assert shared_voices.list_deletions(monitor) == []
    assert shared_voices.get_deletion(voice["id"], monitor)["targets"][0]["status"] == "deleted"


@pytest.mark.asyncio
async def test_offline_delete_tombstone_self_heals_when_mac_returns(reset, monitor, monkeypatch):
    voice = _create()
    studio = next(s for s in monitor.registry if s["id"] == "voice")
    monitor.status[studio["id"]] = {"status": "down"}

    first = await shared_voices.delete_voice(monitor, voice["id"])
    assert first["sync"]["pending"] == 1
    assert shared_voices.list_deletions(monitor)[0]["id"] == voice["id"]

    async def delete(url, **kwargs):
        return _Response(200, {"deleted": voice["id"], "sha256": voice["audio_sha256"]})

    monkeypatch.setattr(monitor._client, "delete", delete)
    monitor.status[studio["id"]] = {"status": "up"}
    await shared_voices.reconcile_once(monitor)

    assert shared_voices.list_deletions(monitor) == []
    assert shared_voices.get_deletion(voice["id"], monitor)["sync"]["deleted"] == 1


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


def test_authenticated_rename_and_delete_routes(authed, client, monkeypatch):
    voice = _create()
    monkeypatch.setattr(shared_voices, "start_sync", lambda monitor, voice_id: True)
    renamed = authed.patch(f"/api/hub/shared-voices/{voice['id']}", json={"name": "Aiden New"})
    assert renamed.status_code == 200
    assert renamed.json()["voice"]["name"] == "Aiden New"
    assert renamed.json()["voice"]["id"] == voice["id"]

    monkeypatch.setattr(shared_voices, "start_delete", lambda monitor, voice_id: True)
    assert client.delete(f"/api/hub/shared-voices/{voice['id']}").status_code == 401
    deleted = authed.delete(f"/api/hub/shared-voices/{voice['id']}")
    assert deleted.status_code == 200
    assert deleted.json()["deletion"]["id"] == voice["id"]
    assert shared_voices.audio_path(voice["id"]) is None


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
    assert "renameSharedVoice" in source
    assert "deleteSharedVoice" in source
    assert "Offline Macs will catch up automatically" in source
