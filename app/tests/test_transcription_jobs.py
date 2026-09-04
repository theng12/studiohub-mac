import asyncio
import contextlib
import io
import json
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException, UploadFile

from backend import broker, control_plane, ledger, transcription_jobs as jobs


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


async def _create_direct(count=3, *, model="mlx/whisper",
                         genstudio_execution_json=None):
    uploads = [UploadFile(file=io.BytesIO(f"audio-{i}".encode()), filename=f"c{i}.wav")
               for i in range(count)]
    batch, _ = await jobs.create_batch(
        uploads, [f"chapter-{i}" for i in range(count)], model,
        "en", False, "test", "project", "episode",
        genstudio_execution_json=genstudio_execution_json)
    return batch


def _genstudio_transcription_execution(*, operation="audio.transcription",
                                       model_revision="a" * 40,
                                       contract_hash="sha256:" + "b" * 64):
    return {
        "genstudio_job_id": "job-transcription",
        "genstudio_attempt_id": "attempt-transcription",
        "idempotency_key": "transcription-attempt-key",
        "fencing_token": 1,
        "site_id": "site-transcription",
        "operation": operation,
        "model_revision": model_revision,
        "contract_hash": contract_hash,
    }


def _audited_transcription_candidate(*, revision="a" * 40,
                                     contract_hash="sha256:" + "b" * 64,
                                     operation="audio.transcription",
                                     audit_status="passed",
                                     candidate_for_genstudio=True,
                                     schema_version=1):
    return {
        "schema": "studio.model-audit",
        "schema_version": schema_version,
        "audit_id": "audit-transcription",
        "audit_status": audit_status,
        "candidate_for_genstudio": candidate_for_genstudio,
        "runtime_revision": revision,
        "contract_hash": contract_hash,
        "approved_operations": [operation],
        "audited_at": "2026-09-05T00:00:00Z",
    }


class _Response:
    status_code = 200

    def __init__(self, srt="1\n00:00:00,000 --> 00:00:01,000\nHello\n", elapsed=1.25):
        self._srt = srt
        self._elapsed = elapsed

    def json(self):
        return {"srt": self._srt, "text": "Hello", "language": "en",
                "duration": 1.0, "elapsed_seconds": self._elapsed,
                "segments": [], "vtt": "WEBVTT"}


class _ChatResponse:
    status_code = 200
    text = ""

    def json(self):
        return {
            "choices": [{"message": {"content": "answer"}}],
            "elapsed_seconds": 0.1,
        }


class _CapacityResponse:
    status_code = 503
    text = "MemoryGuardError: not enough memory"

    def json(self):
        return {"detail": self.text}


@pytest.mark.asyncio
async def test_memory_guard_handoff_requeues_without_consuming_transcription_attempt(
        reset, monitor, monkeypatch):
    batch = await _create_direct(1)
    voice = next(studio for studio in monitor.registry if studio["id"] == "voice")
    monitor.status[voice["id"]] = {"status": "up"}
    calls = []

    async def availability(_studio):
        return {"available": True,
                "models": [{"repo": "mlx/whisper", "cached": True}]}

    async def handoff(_client, studio):
        calls.append(studio["id"])
        return {"attempted": 1, "released": 1, "busy": [], "failed": [],
                "deferred": False}

    async def post(*_args, **_kwargs):
        return _CapacityResponse()

    monkeypatch.setattr(monitor, "scheduling_transcription", availability)
    monkeypatch.setattr(monitor._client, "post", post)
    monkeypatch.setattr(broker, "release_idle_siblings", handoff)

    assert await jobs.dispatch_once(monitor) == 1
    await asyncio.gather(*list(jobs._item_tasks.values()))

    item = batch["items"][0]
    assert calls and item["state"] == "queued" and item["tries"] == 0
    assert broker.machine_protection_snapshot() == {}


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
    activity_ids = []

    async def post(url, **kwargs):
        studio_id = remote["id"] if "10.0.0.2" in url else local["id"]
        activity_ids.append(kwargs["data"]["activity_id"])
        await gates[studio_id].wait()
        return _Response()

    monkeypatch.setattr(monitor, "scheduling_transcription", availability)
    monkeypatch.setattr(monitor._client, "post", post)
    assert await jobs.dispatch_once(monitor) == 2
    ownership = ledger.activity_events()
    expected_ids = {
        item["studio_task_id"] for item in batch["items"] if item["state"] == "running"
    }
    assert {row["job_id"] for row in ownership} == expected_ids
    assert {row["operation"] for row in ownership} == {"transcription"}
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
    assert set(activity_ids) == {
        f"stt-{batch['id']}-{item['index']}" for item in batch["items"]
    }


@pytest.mark.asyncio
async def test_chat_and_transcription_take_shared_machine_turns(
        reset, monitor, monkeypatch):
    from backend import chat_jobs

    transcription_batch = await _create_direct(1)
    chat_batch, _ = chat_jobs.create_batch({
        "model": "mlx/chat-model",
        "kind": "completion",
        "label": "shared machine fairness",
        "packs": [
            {
                "pack_id": "chat-1", "scene_ids": ["response-1"],
                "messages": [{"role": "user", "content": "one"}],
                "params": {},
            },
            {
                "pack_id": "chat-2", "scene_ids": ["response-2"],
                "messages": [{"role": "user", "content": "two"}],
                "params": {},
            },
        ],
    })
    chat_studio = {
        "id": "chat@shared", "modality": "chat", "machine": "shared",
        "host": "127.0.0.1", "port": 47871,
    }
    voice_studio = {
        "id": "voice@shared", "modality": "voice", "machine": "shared",
        "host": "127.0.0.1", "port": 47870,
    }
    monitor.registry = [chat_studio, voice_studio]
    monitor.status = {
        studio["id"]: {"status": "up"}
        for studio in monitor.registry
    }

    async def catalog(_studio):
        return {"models": [{"repo": "mlx/chat-model", "cache": {"state": "cached"}}]}

    async def availability(_studio):
        return {"available": True,
                "models": [{"repo": "mlx/whisper", "cached": True}]}

    async def post(url, **_kwargs):
        return _ChatResponse() if "/v1/chat/completions" in url else _Response()

    monkeypatch.setattr(monitor, "scheduling_catalog", catalog)
    monkeypatch.setattr(monitor, "scheduling_transcription", availability)
    monkeypatch.setattr(monitor._client, "post", post)

    assert await chat_jobs.dispatch_once(monitor) == 1
    await asyncio.gather(*list(chat_jobs._pack_tasks.values()))
    # The token now points to transcription, so Chat cannot win the next
    # wake-up while one transcription item is still waiting.
    assert await chat_jobs.dispatch_once(monitor) == 0

    assert await jobs.dispatch_once(monitor) == 1
    await asyncio.gather(*list(jobs._item_tasks.values()))

    assert await chat_jobs.dispatch_once(monitor) == 1
    await asyncio.gather(*list(chat_jobs._pack_tasks.values()))
    assert chat_jobs.summary(chat_batch)["done"] == 2
    assert jobs.summary(transcription_batch)["done"] == 1


@pytest.mark.asyncio
async def test_ineligible_opposite_lane_does_not_block_shared_machine(
        reset, monitor, monkeypatch):
    from backend import chat_jobs

    # There is queued transcription work, but it names a model that the
    # shared Voice worker does not expose. A global queue-depth boolean must
    # not make that ineligible work reserve the machine's next turn.
    transcription_batch = await _create_direct(1, model="mlx/not-installed")
    chat_batch, _ = chat_jobs.create_batch({
        "model": "mlx/chat-model",
        "kind": "completion",
        "label": "machine-aware fairness",
        "packs": [{
            "pack_id": "chat-1", "scene_ids": ["response-1"],
            "messages": [{"role": "user", "content": "one"}],
            "params": {},
        }],
    })
    chat_studio = {
        "id": "chat@shared", "modality": "chat", "machine": "shared",
        "host": "127.0.0.1", "port": 47871,
    }
    voice_studio = {
        "id": "voice@shared", "modality": "voice", "machine": "shared",
        "host": "127.0.0.1", "port": 47870,
    }
    monitor.registry = [chat_studio, voice_studio]
    monitor.status = {
        studio["id"]: {"status": "up"}
        for studio in monitor.registry
    }

    async def catalog(_studio):
        return {"models": [{"repo": "mlx/chat-model",
                             "cache": {"state": "cached"}}]}

    async def availability(_studio):
        return {"available": True,
                "models": [{"repo": "mlx/whisper", "cached": True}]}

    async def post(*_args, **_kwargs):
        return _ChatResponse()

    monkeypatch.setattr(monitor, "scheduling_catalog", catalog)
    monkeypatch.setattr(monitor, "scheduling_transcription", availability)
    monkeypatch.setattr(monitor._client, "post", post)

    # The previous turn was Chat, so the turn token points to Transcription;
    # a naive global boolean would block Chat even though the opposite lane
    # cannot run on this machine.
    broker.note_external_dispatch("shared", "chat")
    assert await chat_jobs.dispatch_once(monitor) == 1
    await asyncio.gather(*list(chat_jobs._pack_tasks.values()))
    assert chat_jobs.summary(chat_batch)["done"] == 1
    assert transcription_batch["items"][0]["state"] == "queued"


@pytest.mark.asyncio
async def test_memory_ineligible_opposite_lane_does_not_block_shared_machine(
        reset, monitor, monkeypatch):
    from backend import chat_jobs

    transcription_batch = await _create_direct(1, model="mlx/whisper")
    chat_batch, _ = chat_jobs.create_batch({
        "model": "mlx/chat-model",
        "kind": "completion",
        "label": "memory-aware fairness",
        "packs": [{
            "pack_id": "chat-1", "scene_ids": ["response-1"],
            "messages": [{"role": "user", "content": "one"}],
            "params": {},
        }],
    })
    chat_studio = {
        "id": "chat@shared", "modality": "chat", "machine": "shared",
        "host": "127.0.0.1", "port": 47871,
    }
    voice_studio = {
        "id": "voice@shared", "modality": "voice", "machine": "shared",
        "host": "127.0.0.1", "port": 47870,
    }
    monitor.registry = [chat_studio, voice_studio]
    monitor.status = {
        studio["id"]: {"status": "up"}
        for studio in monitor.registry
    }

    async def catalog(_studio):
        return {"models": [{"repo": "mlx/chat-model",
                             "cache": {"state": "cached"}}]}

    async def availability(_studio):
        return {"available": True, "models": [{
            "repo": "mlx/whisper", "cached": True,
            "min_unified_memory_gb": 16,
        }]}

    async def post(*_args, **_kwargs):
        return _ChatResponse()

    monkeypatch.setattr(monitor, "scheduling_catalog", catalog)
    monkeypatch.setattr(monitor, "scheduling_transcription", availability)
    monkeypatch.setattr(monitor._client, "post", post)
    monkeypatch.setattr(
        broker, "_host_for_studio",
        lambda _studio: {"total_gb": 8, "available_gb": 6},
    )

    broker.note_external_dispatch("shared", "chat")
    assert await jobs.has_dispatchable_work(monitor, "shared") is False
    assert await chat_jobs.dispatch_once(monitor) == 1
    await asyncio.gather(*list(chat_jobs._pack_tasks.values()))
    assert chat_jobs.summary(chat_batch)["done"] == 1
    assert transcription_batch["items"][0]["state"] == "queued"


@pytest.mark.asyncio
async def test_memory_admission_failure_advances_shared_machine_turn(
        reset, monitor, monkeypatch):
    from backend import chat_jobs

    transcription_batch = await _create_direct(1)
    chat_batch, _ = chat_jobs.create_batch({
        "model": "mlx/chat-model",
        "kind": "completion",
        "label": "memory-failure fairness",
        "packs": [{
            "pack_id": "chat-1", "scene_ids": ["response-1"],
            "messages": [{"role": "user", "content": "one"}],
            "params": {},
        }],
    })
    chat_studio = {
        "id": "chat@shared", "modality": "chat", "machine": "shared",
        "host": "127.0.0.1", "port": 47871,
    }
    voice_studio = {
        "id": "voice@shared", "modality": "voice", "machine": "shared",
        "host": "127.0.0.1", "port": 47870,
    }
    monitor.registry = [chat_studio, voice_studio]
    monitor.status = {
        studio["id"]: {"status": "up"}
        for studio in monitor.registry
    }

    async def catalog(_studio):
        return {"models": [{"repo": "mlx/chat-model",
                             "cache": {"state": "cached"}}]}

    async def availability(_studio):
        return {"available": True,
                "models": [{"repo": "mlx/whisper", "cached": True}]}

    async def prepare(_client, _studio, model, _entry):
        if model == "mlx/whisper":
            return "wait", "waiting for memory"
        return "run", None

    async def post(*_args, **_kwargs):
        return _ChatResponse()

    monkeypatch.setattr(monitor, "scheduling_catalog", catalog)
    monkeypatch.setattr(monitor, "scheduling_transcription", availability)
    monkeypatch.setattr(monitor._client, "post", post)
    monkeypatch.setattr(broker, "prepare_machine_memory", prepare)

    broker.note_external_dispatch("shared", "chat")
    assert await jobs.dispatch_once(monitor) == 0
    assert transcription_batch["items"][0]["state"] == "queued"
    assert await chat_jobs.dispatch_once(monitor) == 1
    await asyncio.gather(*list(chat_jobs._pack_tasks.values()))
    assert chat_jobs.summary(chat_batch)["done"] == 1


def test_genstudio_transcription_rejects_wrong_operation_before_upload(reset):
    control_plane.save_settings({
        "role": "controller", "site_id": "site-transcription",
        "site_name": "Transcription", "controller_id": "controller-transcription",
        "database_mode": "off",
    })
    execution = _genstudio_transcription_execution(operation="voice.tts")
    uploads = [UploadFile(file=io.BytesIO(b"audio"), filename="clip.wav")]

    with pytest.raises(HTTPException) as raised:
        asyncio.run(jobs.create_batch(
            uploads, ["clip"], "mlx/whisper", "en", False, None, None, None,
            genstudio_execution_json=json.dumps(execution),
        ))

    assert getattr(raised.value, "status_code", None) == 400
    assert "audio.transcription" in str(raised.value)


@pytest.mark.asyncio
async def test_genstudio_transcription_requires_exact_revision_and_contract(
        reset, monitor, monkeypatch):
    control_plane.save_settings({
        "role": "controller", "site_id": "site-transcription",
        "site_name": "Transcription", "controller_id": "controller-transcription",
        "database_mode": "off",
    })
    execution = _genstudio_transcription_execution()
    batch = await _create_direct(
        1, genstudio_execution_json=json.dumps(execution),
    )
    assert batch["genstudio_execution"]["contract_hash"] == execution["contract_hash"]
    voice = next(studio for studio in monitor.registry if studio["id"] == "voice")
    monitor.status[voice["id"]] = {"status": "up"}
    wrong_revision = "d" * 40
    wrong_contract = "sha256:" + "c" * 64

    async def availability(revision, contract_hash):
        return {
            "available": True,
            "models": [{
                "repo": "mlx/whisper", "cached": True,
                "cache": {"state": "cached", "snapshot_revision": revision},
                "genstudio_candidate": _audited_transcription_candidate(
                    revision=revision, contract_hash=contract_hash,
                ),
                "genstudio_candidate_runtime_match": True,
            }],
        }

    async def post(*_args, **_kwargs):
        return _Response()

    current = {"revision": wrong_revision, "contract": execution["contract_hash"]}

    async def current_availability(_studio):
        return await availability(current["revision"], current["contract"])

    monkeypatch.setattr(monitor, "scheduling_transcription", current_availability)
    monkeypatch.setattr(monitor._client, "post", post)

    assert await jobs.dispatch_once(monitor) == 0
    assert batch["items"][0]["state"] == "queued"

    current["revision"] = execution["model_revision"]
    current["contract"] = wrong_contract
    assert await jobs.dispatch_once(monitor) == 0
    assert batch["items"][0]["state"] == "queued"

    current["contract"] = execution["contract_hash"]
    assert await jobs.dispatch_once(monitor) == 1
    await asyncio.gather(*list(jobs._item_tasks.values()))
    assert jobs.summary(batch)["status"] == "done"


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate_changes", [
    {"genstudio_candidate": None},
    {"genstudio_candidate": _audited_transcription_candidate(
        audit_status="failed")},
    {"genstudio_candidate": _audited_transcription_candidate(
        candidate_for_genstudio=False)},
    {"genstudio_candidate": _audited_transcription_candidate(
        operation="voice.tts")},
    {"genstudio_candidate": _audited_transcription_candidate(
        revision="mutable")},
    {"genstudio_candidate": _audited_transcription_candidate(
        contract_hash="not-a-contract-hash")},
    {"genstudio_candidate": _audited_transcription_candidate(
        schema_version=True)},
    {"genstudio_candidate_runtime_match": False},
    {"cached": None},
])
async def test_genstudio_transcription_requires_audited_cached_candidate(
        reset, monitor, candidate_changes):
    execution = _genstudio_transcription_execution()
    entry = {
        "repo": "mlx/whisper",
        "cached": True,
        "cache": {"snapshot_revision": execution["model_revision"]},
        "genstudio_candidate": _audited_transcription_candidate(),
        "genstudio_candidate_runtime_match": True,
    }
    entry.update(candidate_changes)
    if "cached" not in candidate_changes:
        entry["cached"] = True
    assert jobs._execution_matches_model(entry, execution) is False


def test_genstudio_transcription_does_not_assume_missing_cached_flag_is_true():
    execution = _genstudio_transcription_execution()
    entry = {
        "repo": "mlx/whisper",
        "cache": {"snapshot_revision": execution["model_revision"]},
        "genstudio_candidate": _audited_transcription_candidate(),
        "genstudio_candidate_runtime_match": True,
    }

    assert jobs._execution_matches_model(entry, execution) is False


@pytest.mark.parametrize("field,value,needle", [
    ("model_revision", "latest", "immutable"),
    ("contract_hash", "not-a-contract-hash", "contract hash"),
])
def test_genstudio_transcription_rejects_invalid_identity_before_upload(
        reset, field, value, needle):
    control_plane.save_settings({
        "role": "controller", "site_id": "site-transcription",
        "site_name": "Transcription", "controller_id": "controller-transcription",
        "database_mode": "off",
    })
    execution = _genstudio_transcription_execution()
    execution[field] = value
    uploads = [UploadFile(file=io.BytesIO(b"audio"), filename="clip.wav")]

    with pytest.raises(HTTPException) as raised:
        asyncio.run(jobs.create_batch(
            uploads, ["clip"], "mlx/whisper", "en", False, None, None, None,
            genstudio_execution_json=json.dumps(execution),
        ))

    assert getattr(raised.value, "status_code", None) == 409
    assert needle in str(raised.value)


@pytest.mark.asyncio
async def test_model_capability_and_existing_heavy_lease_filter_workers(reset, monitor, monkeypatch):
    local, remote = _add_remote_voice(monitor)
    monitor.status[local["id"]] = monitor.status[remote["id"]] = {"status": "up"}

    async def availability(studio):
        repo = "other/model" if studio["id"] == local["id"] else "mlx/whisper"
        return {"available": True, "models": [{"repo": repo, "cached": True}]}

    monkeypatch.setattr(monitor, "scheduling_transcription", availability)
    assert [s["id"] for s in await jobs._eligible_studios(monitor, "mlx/whisper")] == [remote["id"]]
    broker._busy.add("image")
    try:
        assert [s["id"] for s in await jobs._eligible_studios(monitor, "mlx/whisper")] == [remote["id"]]
    finally:
        broker._busy.clear()


@pytest.mark.asyncio
async def test_transcription_eligible_studios_use_smallest_sufficient_ram_first(
        reset, monitor, monkeypatch):
    local, remote = _add_remote_voice(monitor, "mac-8")
    remote_large = {
        **local,
        "id": "voice@mac-24",
        "machine": "mac-24",
        "host": "10.0.0.24",
    }
    monitor.registry.append(remote_large)
    for studio in (local, remote, remote_large):
        monitor.status[studio["id"]] = {"status": "up"}
    memory = {
        "local": {"total_gb": 16, "available_gb": 12},
        "mac-8": {"total_gb": 8, "available_gb": 6},
        "mac-24": {"total_gb": 24, "available_gb": 20},
    }

    async def availability(_studio):
        return {"available": True,
                "models": [{"repo": "mlx/whisper", "cached": True}]}

    monkeypatch.setattr(monitor, "scheduling_transcription", availability)
    monkeypatch.setattr(
        broker, "_host_for_studio",
        lambda studio: memory[studio["machine"]],
    )

    eligible = await jobs._eligible_studios(monitor, "mlx/whisper")

    assert [studio["machine"] for studio in eligible] == [
        "mac-8", "local", "mac-24",
    ]


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

    monkeypatch.setattr(monitor, "scheduling_transcription", availability)
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

    monkeypatch.setattr(monitor, "scheduling_transcription", availability)
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

    monkeypatch.setattr(monitor, "scheduling_transcription", availability)
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

    monkeypatch.setattr(monitor, "scheduling_transcription", availability)
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

    monkeypatch.setattr(monitor, "scheduling_transcription", availability)
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


@pytest.mark.asyncio
async def test_storage_cleanup_loop_runs_off_the_event_loop(reset, monkeypatch):
    """The hourly reclaim measures the spool with a full rglob+stat tree walk.

    Run on the event loop it froze this single-worker process for the whole
    walk, so `/health/live` timed out and the site read as flapping. The
    blocking stub can only be released by another task on the loop, so it
    completes if and only if the walk really happens off-loop.
    """
    from backend import job_storage

    entered, release = threading.Event(), threading.Event()

    def blocking_status():
        entered.set()
        assert release.wait(10), "job_storage.status ran on the event loop"
        return {"enabled": False}

    monkeypatch.setattr(job_storage, "status", blocking_status)

    loop_task = asyncio.create_task(jobs._cleanup_loop())
    try:
        assert await asyncio.to_thread(entered.wait, 10), "the loop never checked policy"
        release.set()
        # Policy is disabled, so the loop parks in its hour-long sleep instead
        # of reclaiming; it must still be alive and must not have raised.
        await asyncio.sleep(0)
        assert not loop_task.done()
    finally:
        loop_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await loop_task
