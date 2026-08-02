from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import pytest

from backend import broker, execution_assets


def test_voice_reference_stage_is_private_checksum_bound_and_idempotent(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(execution_assets, "ROOT", tmp_path / "execution-assets")
    payload = b"RIFF-private-customer-voice"
    digest = hashlib.sha256(payload).hexdigest()

    first = execution_assets.stage_voice_reference(
        audio_bytes=payload,
        filename="voice.wav",
        source_asset_id="genstudio-asset-123",
        declared_sha256=digest,
        transcript="Exact words.",
        transcript_segments=[{"start": 0, "end": 1, "text": "Exact words."}],
        ttl_seconds=600,
    )
    second = execution_assets.stage_voice_reference(
        audio_bytes=payload,
        filename="voice.wav",
        source_asset_id="genstudio-asset-123",
        declared_sha256=digest,
        transcript="Exact words.",
        transcript_segments=[{"start": 0, "end": 1, "text": "Exact words."}],
        ttl_seconds=900,
    )

    metadata, path = execution_assets.resolve_voice_reference(first["id"])
    assert second["id"] == first["id"]
    assert path.read_bytes() == payload
    assert metadata["sha256"] == digest
    public = execution_assets.public(metadata)
    assert "transcript" not in public
    assert "source_asset_fingerprint" not in public
    assert str(tmp_path) not in str(public)


def test_expired_voice_reference_has_machine_readable_error(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(execution_assets, "ROOT", tmp_path / "execution-assets")
    payload = b"private"
    staged = execution_assets.stage_voice_reference(
        audio_bytes=payload,
        filename="voice.wav",
        source_asset_id="asset-1",
        declared_sha256=hashlib.sha256(payload).hexdigest(),
        ttl_seconds=300,
    )

    with pytest.raises(execution_assets.ExecutionAssetError) as error:
        execution_assets.resolve_voice_reference(
            staged["id"], now=float(staged["expires_at"]) + 1
        )

    assert error.value.code == "VOICE_REFERENCE_ASSET_EXPIRED"


def test_private_staging_endpoint_requires_hub_auth_and_returns_no_source_id(
    client, authed,
) -> None:
    payload = b"RIFF-private-customer-voice"
    data = {
        "source_asset_id": "genstudio-asset-123",
        "source_sha256": hashlib.sha256(payload).hexdigest(),
        "transcript": "Exact words.",
    }
    files = {"audio": ("voice.wav", payload, "audio/wav")}

    assert client.post(
        "/api/hub/execution-assets/voice-references", data=data, files=files
    ).status_code == 401
    response = authed.post(
        "/api/hub/execution-assets/voice-references", data=data, files=files
    )

    assert response.status_code == 200
    asset = response.json()["asset"]
    assert asset["sha256"] == data["source_sha256"]
    assert "source_asset_id" not in asset
    assert "transcript" not in asset


@pytest.mark.asyncio
async def test_broker_streams_staged_voice_to_worker_multipart(
    tmp_path: Path, monkeypatch,
) -> None:
    reference_path = tmp_path / "reference.wav"
    reference_path.write_bytes(b"private-reference")
    reference = {
        "id": "a" * 24,
        "sha256": hashlib.sha256(reference_path.read_bytes()).hexdigest(),
        "audio_extension": ".wav",
        "media_type": "audio/wav",
        "transcript": "Exact words.",
        "transcript_segments": [{"start": 0, "end": 1, "text": "Exact words."}],
        "expires_at": time.time() + 600,
    }
    monkeypatch.setattr(
        broker.execution_assets,
        "resolve_voice_reference",
        lambda _asset_id: (reference, reference_path),
    )
    monkeypatch.setattr(broker, "POLL_S", 0)
    monkeypatch.setattr(broker, "_expire_genstudio_batch", lambda _batch: False)
    monkeypatch.setattr(broker, "_mark_machine_success", lambda _studio: None)

    async def no_finish(*_args, **_kwargs):
        return None

    monkeypatch.setattr(broker, "_post_item_webhook", no_finish)
    monkeypatch.setattr(broker, "_maybe_finish", no_finish)

    captured = {}

    class Response:
        status_code = 200

        def __init__(self, value):
            self.value = value

        def json(self):
            return self.value

    class Client:
        async def post(self, url, **kwargs):
            captured.update(url=url, **kwargs)
            return Response({"job": {"id": "worker-job"}})

        async def get(self, _url, **_kwargs):
            return Response({"job": {"id": "worker-job", "state": "done"}})

    async def record_success(_client, _batch, item, _studio, _job, _body, _started):
        item["state"] = "done"

    monkeypatch.setattr(broker, "_record_worker_success", record_success)
    batch = {
        "id": "batch-1",
        "modality": "voice",
        "model": "model/a",
        "shared_params": {"voice_reference_asset_id": "a" * 24},
        "cancelled": False,
        "genstudio_execution": None,
    }
    item = {
        "index": 0, "prompt": "Narration", "params": {}, "seed": None,
        "state": "running", "studio_job_id": None,
    }
    studio = {"id": "voice@mac-a", "host": "127.0.0.1", "port": 47870}

    await broker._run_item(Client(), batch, item, studio)

    assert item["state"] == "done"
    assert captured["files"]["audio"][1] == b"private-reference"
    request = json.loads(captured["data"]["request_json"])
    assert request["ref_transcript"] == "Exact words."
    assert "voice_reference_asset_id" not in request
    assert str(reference_path) not in json.dumps(request)


@pytest.mark.asyncio
async def test_broker_returns_expired_reference_without_worker_call(monkeypatch) -> None:
    def expired(_asset_id):
        raise execution_assets.ExecutionAssetError(
            "VOICE_REFERENCE_ASSET_EXPIRED",
            "The private voice reference has expired. Upload it again before generating.",
        )

    monkeypatch.setattr(broker.execution_assets, "resolve_voice_reference", expired)
    monkeypatch.setattr(broker, "_expire_genstudio_batch", lambda _batch: False)

    async def no_finish(*_args, **_kwargs):
        return None

    monkeypatch.setattr(broker, "_post_item_webhook", no_finish)
    monkeypatch.setattr(broker, "_maybe_finish", no_finish)

    class Client:
        async def post(self, *_args, **_kwargs):
            raise AssertionError("worker must not be called")

    batch = {
        "id": "batch-1", "modality": "voice", "model": "model/a",
        "shared_params": {"voice_reference_asset_id": "a" * 24},
        "cancelled": False, "genstudio_execution": None,
    }
    item = {
        "index": 0, "prompt": "Narration", "params": {}, "seed": None,
        "state": "running", "studio_job_id": None,
    }

    await broker._run_item(
        Client(), batch, item,
        {"id": "voice@mac-a", "host": "127.0.0.1", "port": 47870},
    )

    assert item["state"] == "error"
    assert item["error_code"] == "VOICE_REFERENCE_ASSET_EXPIRED"
