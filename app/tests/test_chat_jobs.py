import asyncio
import json

import pytest

from backend import broker, chat_jobs as jobs


MODEL = "mlx-community/Llama-3.2-3B-Instruct-4bit"


def _pack(index: int, count: int = 10) -> dict:
    scene_ids = [f"scene-{index * 10 + offset + 1:03d}" for offset in range(count)]
    return {
        "pack_id": f"pack-{index + 1:02d}",
        "scene_ids": scene_ids,
        "messages": [
            {"role": "system", "content": "Return JSON results with stable scene IDs."},
            {"role": "user", "content": json.dumps({"scene_ids": scene_ids})},
        ],
        "params": {"temperature": 0.4, "max_tokens": 4096},
    }


def _payload(pack_count: int = 1, scenes_per_pack: int = 10) -> dict:
    return {
        "model": MODEL,
        "kind": "visual",
        "label": "Scene visual prompts",
        "project": "dozing-knight",
        "episode": "DK0001",
        "packs": [_pack(index, scenes_per_pack) for index in range(pack_count)],
    }


class _Response:
    status_code = 200
    text = ""

    def __init__(self, content: str, elapsed: float = 1.25):
        self.content = content
        self.elapsed = elapsed

    def json(self):
        return {
            "choices": [{"message": {"content": self.content}}],
            "elapsed_seconds": self.elapsed,
        }


def _results(scene_ids: list[str], key: str = "visual_prompt") -> str:
    return json.dumps({
        "results": [{"scene_id": scene_id, key: f"Prompt for {scene_id}"}
                    for scene_id in scene_ids],
    })


def _add_chat_workers(monitor, count: int) -> list[dict]:
    local = next(studio for studio in monitor.registry if studio["id"] == "chat")
    workers = []
    monitor.registry = [studio for studio in monitor.registry if studio["modality"] != "chat"]
    for index in range(count):
        machine = f"chat-mac-{index + 1:02d}"
        worker = {
            **local,
            "id": f"chat@{machine}",
            "machine": machine,
            "host": f"10.0.0.{index + 10}",
        }
        monitor.registry.append(worker)
        monitor.status[worker["id"]] = {"status": "up"}
        workers.append(worker)
    return workers


def test_api_submission_is_authenticated_persistent_and_idempotent(authed, client):
    response = authed.post("/api/hub/chat/jobs", json=_payload(2))
    assert response.status_code == 200
    created = response.json()
    assert created["packs"] == 2 and created["scenes"] == 20
    batch = jobs.get_batch(created["batch_id"])
    assert batch and len(batch["packs"]) == 2
    duplicate = authed.post("/api/hub/chat/jobs", json=_payload(2)).json()
    assert duplicate["batch_id"] == created["batch_id"]
    assert duplicate["duplicate"] is True
    assert client.post("/api/hub/chat/jobs", json=_payload()).status_code == 401


def test_pack_validation_enforces_ten_scene_limit_and_unique_ids(authed):
    too_many = _payload()
    too_many["packs"][0]["scene_ids"] = [f"scene-{i}" for i in range(11)]
    assert authed.post("/api/hub/chat/jobs", json=too_many).status_code == 400

    duplicate = _payload(2)
    duplicate["packs"][1]["scene_ids"][0] = duplicate["packs"][0]["scene_ids"][0]
    response = authed.post("/api/hub/chat/jobs", json=duplicate)
    assert response.status_code == 400
    assert "more than one pack" in response.json()["detail"]


def test_result_parser_accepts_supported_shapes_and_rejects_unknown_ids():
    expected = ["scene-1", "scene-2"]
    content = "```json\n" + json.dumps({
        "prompts": [
            {"scene_id": "scene-1", "visual_prompt": "One"},
            {"scene_id": "scene-2", "prompt": "Two"},
            {"scene_id": "scene-other", "prompt": "Never accept"},
        ],
    }) + "\n```"
    assert jobs.parse_scene_results(content, expected, "visual") == {
        "scene-1": "One", "scene-2": "Two",
    }
    assert jobs.parse_scene_results("plain prose", ["scene-1", "scene-2"], "visual") == {}
    assert jobs.parse_scene_results("plain prose", ["scene-1"], "visual") == {"scene-1": "plain prose"}


@pytest.mark.asyncio
async def test_ten_servers_process_one_hundred_scenes_in_one_wave(reset, monitor, monkeypatch):
    batch, _ = jobs.create_batch(_payload(10))
    workers = _add_chat_workers(monitor, 10)

    async def catalog(studio):
        return {"models": [{"repo": MODEL, "cache": {"state": "cached"}}]}

    async def post(url, **kwargs):
        scene_ids = json.loads(kwargs["json"]["messages"][1]["content"])["scene_ids"]
        return _Response(_results(scene_ids))

    monkeypatch.setattr(monitor, "get_catalog", catalog)
    monkeypatch.setattr(monitor._client, "post", post)
    assert await jobs.dispatch_once(monitor) == 10
    assert len(jobs.busy_studios) == 10
    assert len({pack["studio"] for pack in batch["packs"]}) == 10
    assert {pack["studio"] for pack in batch["packs"]} == {worker["id"] for worker in workers}
    await asyncio.gather(*list(jobs._pack_tasks.values()))
    result = jobs.summary(batch)
    assert result["done"] == 10
    assert result["completed_scenes"] == 100
    assert result["status"] == "done"


@pytest.mark.asyncio
async def test_partial_pack_saves_valid_results_and_retries_only_missing_ids(reset, monitor, monkeypatch):
    batch, _ = jobs.create_batch(_payload())
    _add_chat_workers(monitor, 1)
    calls = 0

    async def catalog(studio):
        return {"models": [{"repo": MODEL, "cache": {"state": "cached"}}]}

    async def post(url, **kwargs):
        nonlocal calls
        calls += 1
        scene_ids = batch["packs"][0]["scene_ids"]
        returned = scene_ids[:7] if calls == 1 else scene_ids[7:]
        if calls == 2:
            assert "ONLY these missing" in kwargs["json"]["messages"][-1]["content"]
            assert all(scene_id in kwargs["json"]["messages"][-1]["content"] for scene_id in scene_ids[7:])
        return _Response(_results(returned))

    monkeypatch.setattr(monitor, "get_catalog", catalog)
    monkeypatch.setattr(monitor._client, "post", post)
    assert await jobs.dispatch_once(monitor) == 1
    await asyncio.gather(*list(jobs._pack_tasks.values()))
    pack = batch["packs"][0]
    assert pack["state"] == "queued" and len(pack["results"]) == 7
    assert await jobs.dispatch_once(monitor) == 1
    await asyncio.gather(*list(jobs._pack_tasks.values()))
    assert pack["state"] == "done" and len(pack["results"]) == 10
    assert calls == 2


@pytest.mark.asyncio
async def test_model_capability_and_physical_machine_lease_filter_workers(reset, monitor, monkeypatch):
    workers = _add_chat_workers(monitor, 2)

    async def catalog(studio):
        repo = MODEL if studio["id"] == workers[1]["id"] else "other/model"
        return {"models": [{"repo": repo, "cache": {"state": "cached"}}]}

    monkeypatch.setattr(monitor, "get_catalog", catalog)
    assert [studio["id"] for studio in await jobs._eligible_studios(monitor, MODEL)] == [workers[1]["id"]]
    broker._external_machine_leases[workers[1]["machine"]] = "render:test"
    assert await jobs._eligible_studios(monitor, MODEL) == []


def test_restart_recovery_requeues_running_pack(reset):
    batch, _ = jobs.create_batch(_payload())
    batch["packs"][0].update(state="running", studio="chat@mac")
    jobs._save(batch)
    jobs.batches.clear()
    assert jobs.restore_batches() == 1
    restored = jobs.get_batch(batch["id"])
    assert restored["packs"][0]["state"] == "queued"
    assert restored["packs"][0]["studio"] is None


@pytest.mark.asyncio
async def test_cancel_preserves_completed_pack_and_aborts_running_pack(reset, monitor, monkeypatch):
    batch, _ = jobs.create_batch(_payload(2))
    first = batch["packs"][0]
    first.update(state="done", results={scene_id: "done" for scene_id in first["scene_ids"]})
    _add_chat_workers(monitor, 1)
    gate = asyncio.Event()

    async def catalog(studio):
        return {"models": [{"repo": MODEL, "cache": {"state": "cached"}}]}

    async def post(*args, **kwargs):
        await gate.wait()
        return _Response("never")

    monkeypatch.setattr(monitor, "get_catalog", catalog)
    monkeypatch.setattr(monitor._client, "post", post)
    assert await jobs.dispatch_once(monitor) == 1
    await asyncio.sleep(0)
    await jobs.cancel_batch(batch["id"])
    await asyncio.gather(*list(jobs._pack_tasks.values()), return_exceptions=True)
    assert first["state"] == "done" and len(first["results"]) == 10
    assert batch["packs"][1]["state"] == "cancelled"


def test_retry_preserves_completed_scene_results(reset):
    batch, _ = jobs.create_batch(_payload())
    pack = batch["packs"][0]
    pack.update(state="partial", results={pack["scene_ids"][0]: "already saved"}, tries=3)
    jobs._save(batch)
    _, retried = jobs.retry_batch(batch["id"])
    assert retried == 1 and pack["state"] == "queued" and pack["tries"] == 0
    assert pack["results"] == {pack["scene_ids"][0]: "already saved"}
