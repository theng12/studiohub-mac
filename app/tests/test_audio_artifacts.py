import hashlib
import io
import wave

import pytest

from backend import artifact_metadata, broker, ledger, main


def wav_fixture(sample_rate=24_000, channels=1, duration_s=11.525) -> bytes:
    """A real PCM WAV fixture matching the production Kokoro qualification."""
    frames = round(sample_rate * duration_s)
    out = io.BytesIO()
    with wave.open(out, "wb") as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(b"\0\0" * frames * channels)
    return out.getvalue()


class _ArtifactResponse:
    status_code = 200

    def __init__(self, content, content_type="audio/wav"):
        self.content = content
        self.headers = {"content-type": content_type}

    def raise_for_status(self):
        return None


class _VoiceClient:
    def __init__(self, content):
        self.content = content
        self.calls = []

    async def get(self, url, headers=None, **_kwargs):
        self.calls.append((url, headers))
        return _ArtifactResponse(self.content)


class _StreamResponse:
    status_code = 200

    def __init__(self, content, content_type):
        self.content = content
        self.headers = {"content-type": content_type}
        self.closed = False

    def raise_for_status(self):
        return None

    async def aiter_bytes(self, _chunk_size):
        yield self.content

    async def aread(self):
        return self.content

    async def aclose(self):
        self.closed = True


class _StreamClient:
    async def aclose(self):
        return None


@pytest.mark.asyncio
async def test_voice_terminal_wav_metadata_separates_runtime_from_audio(reset):
    payload = wav_fixture()
    submitted = broker.submit_batch({"modality": "voice", "model": "mlx/kokoro",
                                     "items": [{"text": "hello"}]})
    batch = broker.batches[submitted["batch_id"]]
    item = batch["items"][0]
    item.update(state="running", studio="voice", studio_job_id="worker-1")
    client = _VoiceClient(payload)
    studio = {"id": "voice", "modality": "voice", "machine": "local",
              "host": "127.0.0.1", "port": 47870}

    await broker._record_worker_success(client, batch, item, studio, {
        "id": "worker-1", "output_path": "/worker/private/output.wav",
        "output_url": "/api/generate/jobs/worker-1/audio",
        "duration_seconds": 7.6208,
        "model_revision": "1" * 40,
        "voice_revision": "a" * 64,
    }, {"voice_library_id": "074743daa991"}, 0.0)

    assert item["media_type"] == "audio/wav"
    assert item["format"] == "wav"
    assert item["bytes"] == len(payload) == 553_244
    assert item["sha256"] == hashlib.sha256(payload).hexdigest()
    assert item["sample_rate_hz"] == 24_000 and item["channels"] == 1
    assert item["audio_duration_ms"] == 11_525
    assert item["audio_duration_s"] == 11.525
    assert item["runtime_s"] == item["duration_s"] == 7.6208
    assert item["runtime_s"] != item["audio_duration_s"]
    assert item["model_revision"] == "1" * 40
    assert item["voice_revision"] == "a" * 64
    assert item["voice_library_id"] == "074743daa991"
    terminal = broker.terminal_result(batch, item)
    assert terminal["model_revision"] == "1" * 40
    assert terminal["voice_revision"] == "a" * 64
    assert ledger.get_asset(item["asset_id"])["runtime_s"] == 7.6208

    asset_id = item["asset_id"]
    await broker._record_worker_success(client, batch, item, studio, {
        "id": "worker-1", "output_url": "/api/generate/jobs/worker-1/audio"}, {}, 0.0)
    assert item["asset_id"] == asset_id and len(client.calls) == 1


@pytest.mark.asyncio
async def test_voice_metadata_uses_peer_auth_and_never_records_local_path(reset, monkeypatch):
    payload = wav_fixture(duration_s=1)
    item = {}
    calls = []

    def route(studio, path):
        calls.append((studio["id"], path))
        return "http://peer/studio/voice/artifact", {"X-Hub-Token": "test-fleet-token"}

    monkeypatch.setattr(broker, "studio_request", route)
    client = _VoiceClient(payload)
    await broker._cache_voice_artifact_metadata(
        client, item, {"id": "voice@remote"}, "/api/generate/jobs/x/audio", None, None)

    assert calls == [("voice@remote", "/api/generate/jobs/x/audio")]
    assert client.calls[0][1] == {"X-Hub-Token": "test-fleet-token"}
    assert "artifact_path" not in item and item["media_type"] == "audio/wav"


@pytest.mark.parametrize("modality,upstream,expected", [
    ("image", "image/png", "image/png"),
    ("video", "video/mp4", "video/mp4"),
])
def test_proxy_keeps_allowed_image_and_video_media_types(authed, monkeypatch,
                                                         modality, upstream, expected):
    submitted = broker.submit_batch({"modality": modality, "model": "fixture/model",
                                     "items": [{"prompt": "fixture"}]})
    batch = broker.batches[submitted["batch_id"]]
    studio_id = "image" if modality == "image" else "video"
    item = batch["items"][0]
    item.update(state="done", studio=studio_id, artifact_url="/worker/artifact")

    async def open_fixture(_studio, _url):
        return _StreamClient(), _StreamResponse(b"fixture", upstream)

    monkeypatch.setattr(main, "_open_worker_artifact", open_fixture)
    response = authed.get(f"/api/hub/jobs/{batch['id']}/items/0/artifact")
    assert response.status_code == 200
    assert response.headers["content-type"] == expected
    assert response.content == b"fixture"


def test_proxy_uses_cached_wav_type_and_public_result_hides_worker_path(authed, monkeypatch):
    payload = wav_fixture(duration_s=1)
    submitted = broker.submit_batch({"modality": "voice", "model": "mlx/kokoro",
                                     "items": [{"text": "fixture"}]})
    batch = broker.batches[submitted["batch_id"]]
    item = batch["items"][0]
    item.update(state="done", studio="voice", artifact_url="/worker/audio",
                artifact_path="/worker/private/audio.wav", asset_id="asset-fixture",
                runtime_s=0.2, **artifact_metadata.wav_metadata(payload))

    async def open_fixture(_studio, _url):
        return _StreamClient(), _StreamResponse(payload, "video/mp4")

    monkeypatch.setattr(main, "_open_worker_artifact", open_fixture)
    artifact = authed.get(f"/api/hub/jobs/{batch['id']}/items/0/artifact")
    artifact_again = authed.get(f"/api/hub/jobs/{batch['id']}/items/0/artifact")
    result = authed.get(f"/api/hub/jobs/{batch['id']}").json()["items"][0]
    assert artifact.headers["content-type"] == "audio/wav"
    assert artifact_again.content == artifact.content == payload
    assert result["terminal_result"]["artifact_url"].endswith("/items/0/artifact")
    assert result["terminal_result"]["audio_duration_ms"] == 1_000
    assert "artifact_path" not in result and "worker_artifact_url" not in result


def test_proxy_backfills_legacy_wav_metadata_with_valid_upstream_type(authed, monkeypatch):
    payload = wav_fixture(duration_s=1)
    submitted = broker.submit_batch({"modality": "voice", "model": "mlx/kokoro",
                                     "items": [{"text": "legacy"}]})
    batch = broker.batches[submitted["batch_id"]]
    batch["items"][0].update(state="done", studio="voice", artifact_url="/worker/audio",
                              runtime_s=0.4)

    async def open_fixture(_studio, _url):
        return _StreamClient(), _StreamResponse(payload, "audio/wav")

    monkeypatch.setattr(main, "_open_worker_artifact", open_fixture)
    artifact = authed.get(f"/api/hub/jobs/{batch['id']}/items/0/artifact")
    result = authed.get(f"/api/hub/jobs/{batch['id']}").json()["items"][0]["terminal_result"]
    assert artifact.headers["content-type"] == "audio/wav"
    assert result["bytes"] == len(payload)
    assert result["audio_duration_ms"] == 1_000
    assert result["sha256"] == hashlib.sha256(payload).hexdigest()


def test_proxy_rejects_missing_or_nonterminal_artifacts(authed):
    submitted = broker.submit_batch({"modality": "voice", "model": "mlx/kokoro",
                                     "items": [{"text": "waiting"}]})
    batch_id = submitted["batch_id"]
    assert authed.get(f"/api/hub/jobs/{batch_id}/items/0/artifact").status_code == 404
    assert authed.get("/api/hub/jobs/missing/items/0/artifact").status_code == 404
