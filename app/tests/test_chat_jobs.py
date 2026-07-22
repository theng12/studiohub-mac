import asyncio
import json

import pytest

from backend import broker, chat_jobs as jobs, control_plane


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


def _payload(pack_count: int = 1, scenes_per_pack: int = 10,
             model_cost_tier: str = "local") -> dict:
    return {
        "model": MODEL,
        "model_cost_tier": model_cost_tier,
        "kind": "visual",
        "label": "Scene visual prompts",
        "project": "dozing-knight",
        "episode": "DK0001",
        "packs": [_pack(index, scenes_per_pack) for index in range(pack_count)],
    }


class _Response:
    status_code = 200
    text = ""

    def __init__(self, content: str, elapsed: float = 1.25, *, usage=None,
                 model_revision=None):
        self.content = content
        self.elapsed = elapsed
        self.usage = usage
        self.model_revision = model_revision

    def json(self):
        payload = {
            "choices": [{"message": {"content": self.content}}],
            "elapsed_seconds": self.elapsed,
        }
        if self.usage is not None:
            payload["usage"] = self.usage
        if self.model_revision is not None:
            payload["model_revision"] = self.model_revision
        return payload


def _genstudio_payload() -> dict:
    revision = "7f0dc925e0d0afb0322d96f9255cfddf2ba5636e"
    payload = _payload(1, 1)
    payload["genstudio_execution"] = {
        "genstudio_job_id": "job-local-llama",
        "genstudio_attempt_id": "attempt-local-llama",
        "idempotency_key": "b" * 64,
        "fencing_token": 1,
        "site_id": "site-local-llama",
        "operation": "chat.completion",
        "model_revision": revision,
    }
    return payload


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


def test_genstudio_chat_identity_is_fenced_bound_and_replayed(reset):
    control_plane.save_settings({
        "role": "controller",
        "site_id": "site-local-llama",
        "site_name": "Local Llama",
        "controller_id": "controller-local-llama",
        "database_mode": "off",
    })
    created, duplicate = jobs.create_batch(_genstudio_payload())
    assert duplicate is False
    assert created["genstudio_execution"]["authority"] == "genstudio"
    replayed, duplicate = jobs.create_batch(_genstudio_payload())
    assert duplicate is True
    assert replayed["id"] == created["id"]


@pytest.mark.asyncio
async def test_genstudio_chat_preserves_verified_usage_and_revision(
        reset, monitor, monkeypatch):
    control_plane.save_settings({
        "role": "controller",
        "site_id": "site-local-llama",
        "site_name": "Local Llama",
        "controller_id": "controller-local-llama",
        "database_mode": "off",
    })
    batch, _ = jobs.create_batch(_genstudio_payload())
    _add_chat_workers(monitor, 1)
    revision = batch["genstudio_execution"]["model_revision"]

    async def catalog(studio):
        return {"models": [{
            "repo": MODEL,
            "cache": {"state": "cached"},
            "runtime_revision": revision,
            "verified_token_usage": True,
            "max_output_tokens": 32768,
        }]}

    async def post(url, **kwargs):
        return _Response(
            "Verified local response",
            usage={"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16},
            model_revision=revision,
        )

    monkeypatch.setattr(monitor, "get_catalog", catalog)
    monkeypatch.setattr(monitor._client, "post", post)
    assert await jobs.dispatch_once(monitor) == 1
    await asyncio.gather(*list(jobs._pack_tasks.values()))
    result = jobs.summary(batch)
    assert result["status"] == "done"
    assert result["usage"] == {
        "prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16,
    }
    assert result["model_revision"] == revision


def test_pack_validation_enforces_adaptive_tier_limits_and_unique_ids(authed):
    too_many = _payload()
    too_many["packs"][0]["scene_ids"] = [f"scene-{i}" for i in range(11)]
    assert authed.post("/api/hub/chat/jobs", json=too_many).status_code == 400

    free_too_many = _payload(1, 20, "free")
    assert authed.post("/api/hub/chat/jobs", json=free_too_many).status_code == 400

    paid_twenty = authed.post("/api/hub/chat/jobs", json=_payload(1, 20, "paid"))
    assert paid_twenty.status_code == 200
    assert paid_twenty.json()["scenes"] == 20

    paid_too_many = _payload(1, 10, "paid")
    paid_too_many["packs"][0]["scene_ids"] = [f"scene-{i}" for i in range(31)]
    assert authed.post("/api/hub/chat/jobs", json=paid_too_many).status_code == 400

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


def test_result_parser_salvages_rows_after_reasoning_and_from_truncated_outer_json():
    content = (
        "<|channel>thought\nI should reason before answering.\n"
        '{"results":['
        '{"scene_id":"scene-1","visual_prompt":"One complete prompt"},'
        '{"scene_id":"scene-2","visual_prompt":"Two complete prompts"},'
        '{"scene_id":"scene-3","visual_prompt":"truncated'
    )
    assert jobs.parse_scene_results(content, ["scene-1", "scene-2", "scene-3"], "visual") == {
        "scene-1": "One complete prompt",
        "scene-2": "Two complete prompts",
    }


def test_batch_with_completed_and_queued_packs_is_not_claimed_running(reset):
    batch, _ = jobs.create_batch(_payload(2))
    batch["packs"][0]["state"] = "done"
    result = jobs.summary(batch)
    assert result["status"] == "queued"
    assert result["running"] == 0 and result["done"] == 1 and result["queued"] == 1


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
async def test_two_hundred_scenes_flow_through_five_servers_in_four_waves(reset, monitor, monkeypatch):
    batch, _ = jobs.create_batch(_payload(20))
    _add_chat_workers(monitor, 5)

    async def catalog(studio):
        return {"models": [{"repo": MODEL, "cache": {"state": "cached"}}]}

    async def post(url, **kwargs):
        scene_ids = json.loads(kwargs["json"]["messages"][1]["content"])["scene_ids"]
        return _Response(_results(scene_ids))

    monkeypatch.setattr(monitor, "get_catalog", catalog)
    monkeypatch.setattr(monitor._client, "post", post)
    wave_sizes = []
    for _ in range(4):
        wave_sizes.append(await jobs.dispatch_once(monitor))
        await asyncio.gather(*list(jobs._pack_tasks.values()))
    assert wave_sizes == [5, 5, 5, 5]
    assert jobs.summary(batch)["completed_scenes"] == 200
    assert jobs.summary(batch)["status"] == "done"


@pytest.mark.asyncio
async def test_oldest_episode_fills_chat_wave_before_newer_work(reset, monitor, monkeypatch):
    first, _ = jobs.create_batch(_payload(2))
    second_payload = _payload(2)
    second_payload["episode"] = "EP0002"
    for pack_index, pack in enumerate(second_payload["packs"]):
        pack["pack_id"] = f"ep2-{pack_index}"
        pack["scene_ids"] = [f"ep2-{scene_id}" for scene_id in pack["scene_ids"]]
    second, _ = jobs.create_batch(second_payload)
    _add_chat_workers(monitor, 2)

    async def catalog(studio):
        return {"models": [{"repo": MODEL, "cache": {"state": "cached"}}]}

    gate = asyncio.Event()

    async def post(url, **kwargs):
        await gate.wait()
        return _Response("{}")

    monkeypatch.setattr(monitor, "get_catalog", catalog)
    monkeypatch.setattr(monitor._client, "post", post)
    assert await jobs.dispatch_once(monitor) == 2
    assert sum(p["state"] == "running" for p in first["packs"]) == 2
    assert sum(p["state"] == "running" for p in second["packs"]) == 0
    assert second["queue_note"].endswith("DK0001")
    gate.set()
    await asyncio.gather(*list(jobs._pack_tasks.values()))


def test_active_assignment_exposes_episode_pack_and_attempt(reset):
    batch, _ = jobs.create_batch(_payload())
    pack = batch["packs"][0]
    pack.update(state="running", studio="chat@mac-01", tries=2, started_at=123.0)
    assignment = jobs.active_assignments()["chat@mac-01"]
    assert assignment["kind"] == "chat" and assignment["episode"] == "DK0001"
    assert assignment["pack_id"] == "pack-01" and assignment["attempt"] == 2
    assert assignment["max_attempts"] == jobs.MAX_TRIES


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


@pytest.mark.asyncio
async def test_genstudio_chat_uses_only_exact_verified_executor(
        reset, monitor, monkeypatch):
    workers = _add_chat_workers(monitor, 2)
    revision = "7f0dc925e0d0afb0322d96f9255cfddf2ba5636e"

    async def catalog(studio):
        if studio["id"] == workers[0]["id"]:
            return {"models": [{
                "repo": MODEL,
                "cache": {"state": "cached"},
                "runtime_revision": revision,
                "verified_token_usage": True,
                "max_output_tokens": 32768,
            }]}
        return {"models": [{
            "repo": MODEL,
            "cache": {"state": "cached"},
        }]}

    monkeypatch.setattr(monitor, "get_catalog", catalog)
    eligible = await jobs._eligible_studios(
        monitor,
        MODEL,
        {"model_revision": revision},
        2048,
    )
    assert [studio["id"] for studio in eligible] == [workers[0]["id"]]


@pytest.mark.asyncio
async def test_remote_chat_pack_uses_connected_peer_hub(reset, monitor, monkeypatch):
    from backend import peers

    batch, _ = jobs.create_batch(_payload())
    worker = _add_chat_workers(monitor, 1)[0]
    peers.set_fleet_token("shared-secret")
    peers._cache[worker["machine"]] = (1.0, {"status": "connected", "reachable": True})

    async def catalog(studio):
        return {"models": [{"repo": MODEL, "cache": {"state": "cached"}}]}

    calls = []

    async def post(url, **kwargs):
        calls.append((url, kwargs.get("headers")))
        return _Response(_results(batch["packs"][0]["scene_ids"]))

    monkeypatch.setattr(monitor, "get_catalog", catalog)
    monkeypatch.setattr(monitor._client, "post", post)
    assert await jobs.dispatch_once(monitor) == 1
    await asyncio.gather(*list(jobs._pack_tasks.values()))
    assert calls == [(
        f"http://{worker['host']}:47873/studio/chat/v1/chat/completions",
        {"X-Hub-Token": "shared-secret"},
    )]
    assert jobs.summary(batch)["status"] == "done"


@pytest.mark.asyncio
async def test_transient_worker_failure_waits_before_retry(reset, monitor, monkeypatch):
    batch, _ = jobs.create_batch(_payload())
    _add_chat_workers(monitor, 1)

    async def catalog(studio):
        return {"models": [{"repo": MODEL, "cache": {"state": "cached"}}]}

    async def post(*args, **kwargs):
        raise OSError("model server is warming up")

    monkeypatch.setattr(monitor, "get_catalog", catalog)
    monkeypatch.setattr(monitor._client, "post", post)
    before = jobs.time.time()
    assert await jobs.dispatch_once(monitor) == 1
    await asyncio.gather(*list(jobs._pack_tasks.values()))
    pack = batch["packs"][0]
    assert pack["state"] == "queued" and pack["tries"] == 1
    assert pack["retry_at"] >= before + jobs.TRANSIENT_RETRY_DELAYS[0]
    assert await jobs.dispatch_once(monitor) == 0
    assert batch["queue_note"].startswith("Automatic retry in ")


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


def test_clear_finished_batches_keeps_running(reset):
    def _uniq(episode):  # distinct episode → distinct idempotency key
        payload = _payload(1)
        payload["episode"] = episode
        return payload

    done, _ = jobs.create_batch(_uniq("EP-done"))
    for pack in done["packs"]:
        pack["state"] = "done"
    jobs._save(done)
    running, _ = jobs.create_batch(_uniq("EP-run"))
    running["packs"][0]["state"] = "running"
    jobs._save(running)

    # a single finished batch removes; a running one is refused
    assert jobs.remove_batch(running["id"]) is False
    assert jobs.remove_batch(done["id"]) is True
    assert jobs.get_batch(done["id"]) is None

    # clear_terminal sweeps finished (incl. errored) but keeps the running one
    errored, _ = jobs.create_batch(_uniq("EP-err"))
    for pack in errored["packs"]:
        pack["state"] = "error"
    jobs._save(errored)
    cleared = jobs.clear_terminal()
    assert cleared >= 1
    assert jobs.get_batch(errored["id"]) is None
    assert jobs.get_batch(running["id"]) is not None
