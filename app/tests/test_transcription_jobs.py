import asyncio
import io
import json
import time
from pathlib import Path

import httpx
import pytest
from fastapi import UploadFile

from backend import broker, transcription_jobs as jobs


def _multipart(names=("chapter-1.wav", "chapter-2.wav"), bodies=(b"one", b"two")):
    return [("files", (name, body, "audio/wav")) for name, body in zip(names, bodies)]


def test_multi_file_submission_is_persistent_idempotent_and_authenticated(authed, client):
    data = {"item_ids": ["Introduction", "Chapter 1"], "model": "mlx/whisper-large-v3",
            "language": "en", "word_timestamps": "true", "label": "Story Studio",
            "project": "dark-kingdom", "episode": "DK0039"}
    response = authed.post("/api/hub/transcription/jobs", data=data, files=_multipart())
    assert response.status_code == 200
    created = response.json()
    assert created["items"] == 2 and created["queued"] == 2
    batch = jobs.get_batch(created["batch_id"])
    assert all(Path(item["input_path"]).is_file() for item in batch["items"])
    assert all(str(Path(item["input_path"]).resolve()).startswith(str(jobs.ROOT.resolve()))
               for item in batch["items"])

    duplicate = authed.post("/api/hub/transcription/jobs", data=data, files=_multipart())
    assert duplicate.json()["batch_id"] == created["batch_id"]
    assert duplicate.json()["duplicate"] is True
    assert client.post("/api/hub/transcription/jobs", data=data, files=_multipart()).status_code == 401


@pytest.mark.parametrize("name,item_id,status", [
    ("../escape.wav", "safe", 400),
    ("clip.wav", "../escape", 400),
    ("clip.exe", "safe", 415),
])
def test_invalid_uploads_and_path_traversal_are_rejected(authed, name, item_id, status):
    response = authed.post(
        "/api/hub/transcription/jobs",
        data={"item_ids": [item_id], "model": "mlx/whisper"},
        files=_multipart((name,), (b"audio",)),
    )
    assert response.status_code == status


def test_empty_upload_and_mismatched_ids_are_rejected(authed):
    empty = authed.post(
        "/api/hub/transcription/jobs",
        data={"item_ids": ["chapter"], "model": "mlx/whisper"},
        files=_multipart(("clip.wav",), (b"",)),
    )
    assert empty.status_code == 400
    mismatch = authed.post(
        "/api/hub/transcription/jobs",
        data={"item_ids": ["one"], "model": "mlx/whisper"},
        files=_multipart(),
    )
    assert mismatch.status_code == 400


def test_ordinary_punctuation_in_upload_filename_is_accepted(authed):
    filename = "Todd - Clear, Engaging and Educational - 19s.MP3"
    response = authed.post(
        "/api/hub/transcription/jobs",
        data={"item_ids": ["todd-reference"], "model": "mlx/whisper"},
        files=_multipart((filename,), (b"audio",)),
    )
    assert response.status_code == 200
    batch = jobs.get_batch(response.json()["batch_id"])
    assert batch["items"][0]["filename"] == filename


def test_clear_finished_transcription_removes_history_and_local_files(authed):
    created = authed.post(
        "/api/hub/transcription/jobs",
        data={"item_ids": ["chapter"], "model": "mlx/whisper"},
        files=_multipart(("clip.wav",), (b"audio",)),
    ).json()
    batch = jobs.get_batch(created["batch_id"])
    batch["items"][0]["state"] = "done"
    jobs._save(batch)
    batch_dir = jobs.ROOT / batch["id"]
    assert batch_dir.is_dir()

    active = authed.post(f"/api/hub/transcription/jobs/{batch['id']}/clear")
    assert active.status_code == 200
    assert active.json()["reclaimed_bytes"] >= len(b"audio")
    assert jobs.get_batch(batch["id"]) is None
    assert not batch_dir.exists()


def test_clear_active_transcription_is_refused(authed):
    created = authed.post(
        "/api/hub/transcription/jobs",
        data={"item_ids": ["chapter"], "model": "mlx/whisper"},
        files=_multipart(("clip.wav",), (b"audio",)),
    ).json()
    response = authed.post(f"/api/hub/transcription/jobs/{created['batch_id']}/clear")
    assert response.status_code == 409


def test_completed_plus_queued_transcription_is_not_claimed_running(reset):
    batch = {
        "id": "status", "model": "mlx/whisper", "created_at": 1,
        "items": [{"state": "done", "duration_seconds": 1},
                  {"state": "queued", "duration_seconds": None}],
    }
    result = jobs.summary(batch, include_items=False)
    assert result["status"] == "queued"
    assert result["running"] == 0 and result["done"] == 1 and result["queued"] == 1


def test_upload_size_limit_is_enforced(authed, monkeypatch):
    monkeypatch.setattr(jobs, "MAX_FILE_BYTES", 2)
    response = authed.post(
        "/api/hub/transcription/jobs",
        data={"item_ids": ["chapter"], "model": "mlx/whisper"},
        files=_multipart(("clip.wav",), (b"123",)),
    )
    assert response.status_code == 413
    assert not list(jobs.ROOT.glob("*"))


async def _create_direct(count=3):
    uploads = [UploadFile(file=io.BytesIO(f"audio-{i}".encode()), filename=f"c{i}.wav")
               for i in range(count)]
    batch, _ = await jobs.create_batch(
        uploads, [f"chapter-{i}" for i in range(count)], "mlx/whisper",
        "en", False, "test", "project", "episode")
    return batch


class _Response:
    status_code = 200

    def __init__(self, srt="1\n00:00:00,000 --> 00:00:01,000\nHello\n", elapsed=1.25):
        self._srt = srt
        self._elapsed = elapsed

    def json(self):
        return {"srt": self._srt, "text": "Hello", "language": "en",
                "duration": 1.0, "elapsed_seconds": self._elapsed,
                "segments": [], "vtt": "WEBVTT"}


def _add_remote_voice(monitor, machine="mac-b"):
    local = next(s for s in monitor.registry if s["id"] == "voice")
    remote = {**local, "id": f"voice@{machine}", "machine": machine, "host": "10.0.0.2"}
    monitor.registry.append(remote)
    return local, remote


@pytest.mark.asyncio
async def test_capable_workers_share_work_one_transcription_each(reset, monitor, monkeypatch):
    batch = await _create_direct(3)
    local, remote = _add_remote_voice(monitor)
    monitor.status[local["id"]] = {"status": "up"}
    monitor.status[remote["id"]] = {"status": "up"}

    async def availability(studio):
        return {"available": True, "models": [{"repo": "mlx/whisper", "cached": True}]}

    gates = {local["id"]: asyncio.Event(), remote["id"]: asyncio.Event()}

    async def post(url, **kwargs):
        studio_id = remote["id"] if "10.0.0.2" in url else local["id"]
        await gates[studio_id].wait()
        return _Response()

    monkeypatch.setattr(monitor, "get_transcription", availability)
    monkeypatch.setattr(monitor._client, "post", post)
    assert await jobs.dispatch_once(monitor) == 2
    await asyncio.sleep(0)
    assert len(jobs.busy_studios) == 2
    assert await jobs.dispatch_once(monitor) == 0
    assert len({i["studio"] for i in batch["items"] if i["state"] == "running"}) == 2

    first_tasks = list(jobs._item_tasks.values())
    for gate in gates.values():
        gate.set()
    await asyncio.gather(*first_tasks)
    assert await jobs.dispatch_once(monitor) == 1
    await asyncio.gather(*list(jobs._item_tasks.values()))
    assert jobs.summary(batch)["done"] == 3


@pytest.mark.asyncio
async def test_model_capability_and_existing_heavy_lease_filter_workers(reset, monitor, monkeypatch):
    local, remote = _add_remote_voice(monitor)
    monitor.status[local["id"]] = monitor.status[remote["id"]] = {"status": "up"}

    async def availability(studio):
        repo = "other/model" if studio["id"] == local["id"] else "mlx/whisper"
        return {"available": True, "models": [{"repo": repo, "cached": True}]}

    monkeypatch.setattr(monitor, "get_transcription", availability)
    assert [s["id"] for s in await jobs._eligible_studios(monitor, "mlx/whisper")] == [remote["id"]]
    broker._busy.add("image")
    try:
        assert [s["id"] for s in await jobs._eligible_studios(monitor, "mlx/whisper")] == [remote["id"]]
    finally:
        broker._busy.clear()


@pytest.mark.asyncio
async def test_restart_recovery_requeues_interrupted_work(reset):
    batch = await _create_direct(1)
    batch["items"][0].update(state="running", studio="voice", studio_task_id="task-1")
    jobs._save(batch)
    jobs.batches.clear()
    assert jobs.restore_batches() == 1
    restored = jobs.get_batch(batch["id"])
    assert restored["items"][0]["state"] == "queued"
    assert restored["items"][0]["interrupted"] is True
    assert restored["items"][0]["studio"] is None


@pytest.mark.asyncio
async def test_offline_failure_requeues_with_bounded_try(reset, monitor, monkeypatch):
    batch = await _create_direct(1)
    voice = next(s for s in monitor.registry if s["id"] == "voice")
    monitor.status[voice["id"]] = {"status": "up"}

    async def availability(studio):
        return {"available": True, "models": [{"repo": "mlx/whisper", "cached": True}]}

    async def post(*args, **kwargs):
        raise httpx.ConnectError("worker went offline")

    monkeypatch.setattr(monitor, "get_transcription", availability)
    monkeypatch.setattr(monitor._client, "post", post)
    assert await jobs.dispatch_once(monitor) == 1
    await asyncio.gather(*list(jobs._item_tasks.values()))
    item = batch["items"][0]
    assert item["state"] == "queued" and item["tries"] == 1
    assert "offline" in item["error"]


@pytest.mark.asyncio
async def test_transport_failure_avoids_worker_and_uses_another_voice_studio(reset, monitor, monkeypatch):
    batch = await _create_direct(1)
    local, remote = _add_remote_voice(monitor)
    monitor.status[local["id"]] = monitor.status[remote["id"]] = {"status": "up"}

    async def availability(studio):
        return {"available": True, "models": [{"repo": "mlx/whisper", "cached": True}]}

    attempted = []

    async def post(url, **kwargs):
        attempted.append(url)
        if "10.0.0.2" not in url:
            raise httpx.ConnectError("local worker went offline")
        return _Response()

    monkeypatch.setattr(monitor, "get_transcription", availability)
    monkeypatch.setattr(monitor._client, "post", post)
    assert await jobs.dispatch_once(monitor) == 1
    await asyncio.gather(*list(jobs._item_tasks.values()))
    item = batch["items"][0]
    assert item["state"] == "queued"
    assert item["avoid_machines"][local["machine"]] > time.time()
    assert await jobs.dispatch_once(monitor) == 1
    await asyncio.gather(*list(jobs._item_tasks.values()))
    assert item["state"] == "done"
    assert "10.0.0.2" in attempted[-1]


@pytest.mark.asyncio
async def test_partial_failure_keeps_success_and_retry_selects_only_error(reset, monitor, monkeypatch):
    batch = await _create_direct(2)
    local, remote = _add_remote_voice(monitor)
    monitor.status[local["id"]] = monitor.status[remote["id"]] = {"status": "up"}

    async def availability(studio):
        return {"available": True, "models": [{"repo": "mlx/whisper", "cached": True}]}

    async def post(url, **kwargs):
        filename = kwargs["files"]["file"][0]
        return _Response(srt="" if filename == "c1.wav" else _Response()._srt)

    monkeypatch.setattr(monitor, "get_transcription", availability)
    monkeypatch.setattr(monitor._client, "post", post)
    assert await jobs.dispatch_once(monitor) == 2
    await asyncio.gather(*list(jobs._item_tasks.values()))
    assert jobs.summary(batch)["status"] == "partial"
    successful = next(i for i in batch["items"] if i["state"] == "done")
    artifact = Path(successful["artifact_path"])
    assert artifact.is_file() and artifact.stat().st_size > 0
    _, retried = jobs.retry_batch(batch["id"])
    assert retried == 1
    assert successful["state"] == "done" and successful["artifact_path"] == str(artifact)
    assert sum(i["state"] == "queued" for i in batch["items"]) == 1


@pytest.mark.asyncio
async def test_cancellation_aborts_running_request_without_deleting_success(reset, monitor, monkeypatch):
    batch = await _create_direct(2)
    batch["items"][0].update(
        state="done", artifact_path=str(jobs.ROOT / batch["id"] / "output" / "0000.srt"))
    Path(batch["items"][0]["artifact_path"]).write_text("finished", encoding="utf-8")
    voice = next(s for s in monitor.registry if s["id"] == "voice")
    monitor.status[voice["id"]] = {"status": "up"}
    gate = asyncio.Event()

    async def availability(studio):
        return {"available": True, "models": [{"repo": "mlx/whisper", "cached": True}]}

    async def post(*args, **kwargs):
        await gate.wait()
        return _Response()

    monkeypatch.setattr(monitor, "get_transcription", availability)
    monkeypatch.setattr(monitor._client, "post", post)
    assert await jobs.dispatch_once(monitor) == 1
    await asyncio.sleep(0)
    task = next(iter(jobs._item_tasks.values()))
    await jobs.cancel_batch(batch["id"])
    await asyncio.gather(task, return_exceptions=True)
    assert batch["items"][1]["state"] == "cancelled"
    assert Path(batch["items"][0]["artifact_path"]).read_text() == "finished"


@pytest.mark.asyncio
async def test_graceful_hub_shutdown_requeues_running_item(reset, monitor, monkeypatch):
    batch = await _create_direct(1)
    voice = next(s for s in monitor.registry if s["id"] == "voice")
    monitor.status[voice["id"]] = {"status": "up"}
    gate = asyncio.Event()

    async def availability(studio):
        return {"available": True, "models": [{"repo": "mlx/whisper", "cached": True}]}

    async def post(*args, **kwargs):
        await gate.wait()
        return _Response()

    monkeypatch.setattr(monitor, "get_transcription", availability)
    monkeypatch.setattr(monitor._client, "post", post)
    assert await jobs.dispatch_once(monitor) == 1
    await asyncio.sleep(0)
    await jobs.stop()
    item = batch["items"][0]
    assert item["state"] == "queued" and item["interrupted"] is True
    assert item["studio"] is None


def test_artifact_endpoint_requires_nonempty_verified_srt(authed):
    batch_dir = jobs.ROOT / "artifacttest" / "output"
    batch_dir.mkdir(parents=True)
    artifact = batch_dir / "0000.srt"
    artifact.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n")
    now = 1.0
    batch = {
        "id": "artifacttest", "idempotency_key": "x", "created_at": now,
        "updated_at": now, "model": "mlx/whisper", "cancelled": False,
        "items": [{"index": 0, "item_id": "intro", "filename": "intro.wav",
                   "state": "done", "tries": 1, "studio": "voice",
                   "studio_task_id": None, "duration_seconds": 1.0,
                   "media_duration_seconds": 1.0, "artifact_path": str(artifact),
                   "error": None, "metadata": {}}],
    }
    jobs.batches[batch["id"]] = batch
    jobs._save(batch)
    status = authed.get("/api/hub/transcription/jobs/artifacttest").json()
    assert status["items"][0]["metadata"] == {}
    response = authed.get("/api/hub/transcription/jobs/artifacttest/items/0/artifact")
    assert response.status_code == 200 and b"Hello" in response.content
    artifact.write_text("")
    assert authed.get("/api/hub/transcription/jobs/artifacttest/items/0/artifact").status_code == 404


def test_retention_never_removes_active_batch(authed):
    created = authed.post(
        "/api/hub/transcription/jobs",
        data={"item_ids": ["chapter"], "model": "mlx/whisper"},
        files=_multipart(("chapter.wav",), (b"audio",)),
    ).json()
    result = authed.post(
        "/api/hub/transcription/cleanup",
        json={"batch_id": created["batch_id"], "all_terminal": True},
    ).json()
    assert result["cleaned"] == 0
    assert jobs.get_batch(created["batch_id"])["items"][0]["state"] == "queued"


def test_legacy_transcription_retention_migrates_once(reset):
    jobs.SETTINGS_FILE.write_text(json.dumps({"retention_days": 3}))

    assert jobs.settings()["retention_days"] == 30
    migrated = json.loads(jobs.SETTINGS_FILE.read_text())
    assert migrated == {"retention_days": 30, "policy_version": 2}

    jobs.set_retention(3)
    assert jobs.settings()["retention_days"] == 3


def test_manual_cleanup_removes_terminal_files_but_keeps_lifetime_stats(authed):
    root = jobs.ROOT / "cleaned"
    (root / "input").mkdir(parents=True)
    (root / "input" / "audio.wav").write_bytes(b"audio")
    now = 1.0
    batch = {
        "id": "cleaned", "idempotency_key": "cleaned-key", "created_at": now,
        "updated_at": now, "finished_at": now, "model": "mlx/whisper",
        "cancelled": False, "items": [{
            "index": 0, "item_id": "chapter", "filename": "chapter.wav",
            "input_path": str(root / "input" / "audio.wav"), "state": "done",
            "tries": 1, "studio": "voice", "studio_task_id": None,
            "duration_seconds": 2.0, "media_duration_seconds": 5.0,
            "artifact_path": None, "error": None, "metadata": {},
        }],
    }
    jobs.batches[batch["id"]] = batch
    jobs._save(batch)
    response = authed.post(
        "/api/hub/transcription/cleanup",
        json={"batch_id": "cleaned", "all_terminal": True},
    )
    assert response.json()["cleaned"] == 1 and not root.exists()
    assert jobs.statistics()["done"] == 1
    assert jobs.get_batch("cleaned")["storage_cleaned_at"] > 0
