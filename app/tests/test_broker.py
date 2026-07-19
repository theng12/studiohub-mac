import base64

import httpx
import pytest

from backend import broker, ledger, shared_voices, workload_policy


def test_submit_validation(reset):
    assert "error" in broker.submit_batch({"modality": "bogus", "items": [{}], "model": "m"})
    assert "error" in broker.submit_batch({"modality": "image", "items": [], "model": "m"})
    assert "error" in broker.submit_batch({"modality": "image", "items": [{"prompt": "x"}]})  # no model
    too_many = [{"prompt": "x"}] * (broker.MAX_BATCH_ITEMS + 1)
    result = broker.submit_batch({"modality": "image", "items": too_many, "model": "m"})
    assert "limited" in result["error"]
    assert "routing" in broker.submit_batch({
        "modality": "image", "model": "m", "routing": "wherever", "items": [{"prompt": "x"}],
    })["error"]


def test_submit_rejects_oversized_json_payload(reset, monkeypatch):
    monkeypatch.setattr(broker, "MAX_BATCH_JSON_BYTES", 50)
    result = broker.submit_batch({"modality": "image", "model": "m",
                                  "items": [{"prompt": "x" * 100}]})
    assert "25 MB limit" in result["error"]


def test_submit_ok_and_summary(reset):
    r = broker.submit_batch({"modality": "image", "model": "a/b", "label": "t",
                             "items": [{"prompt": "one"}, {"prompt": "two"}]})
    assert r["items"] == 2
    b = broker.batches[r["batch_id"]]
    s = broker.batch_summary(b)
    assert s["total"] == 2 and s["queued"] == 2 and s["label"] == "t"
    assert s["routing"] == "pool"


def test_shared_clone_only_allows_synchronized_voice_workers(reset, monkeypatch):
    monkeypatch.setattr(
        shared_voices, "synced_studio_ids", lambda voice_id: {"voice@mac-a"}
        if voice_id == "074743daa991" else None,
    )
    batch = {
        "modality": "voice", "shared_params": {},
    }
    item = {"params": {"voice_library_id": "074743daa991"}}

    assert broker._shared_voice_allows_studio(
        batch, item, {"id": "voice@mac-a"}) is True
    assert broker._shared_voice_allows_studio(
        batch, item, {"id": "voice@mac-b"}) is False
    assert broker._shared_voice_allows_studio(
        batch, {"params": {"voice_library_id": "direct-only-id"}},
        {"id": "voice@mac-b"},
    ) is True


def test_summary_running_items_and_avg(reset):
    import time
    r = broker.submit_batch({"modality": "image", "model": "a/b",
                             "items": [{"prompt": "one"}, {"prompt": "two"}]})
    b = broker.batches[r["batch_id"]]
    # item 0 running on a remote machine with live progress; item 1 done+timed
    b["items"][0].update(state="running", studio="image@macmini-m1-01",
                         run_started=time.time() - 10, progress=0.5)
    b["items"][1].update(state="done", duration_s=8.0)
    s = broker.batch_summary(b)
    assert s["avg_s"] == 8.0
    assert len(s["running_items"]) == 1
    ri = s["running_items"][0]
    assert ri["machine"] == "macmini-m1-01" and ri["progress"] == 0.5
    assert ri["elapsed_s"] >= 9   # ~10s elapsed


def test_summary_local_running_item_machine(reset):
    import time
    r = broker.submit_batch({"modality": "image", "model": "a/b",
                             "items": [{"prompt": "x"}]})
    b = broker.batches[r["batch_id"]]
    b["items"][0].update(state="running", studio="image",
                         run_started=time.time(), progress=None)
    ri = broker.batch_summary(b)["running_items"][0]
    assert ri["machine"] == "local" and ri["progress"] is None


def test_summary_exposes_batch_elapsed_and_stalled_worker(reset, monkeypatch):
    r = broker.submit_batch({"modality": "image", "model": "a/b",
                             "items": [{"prompt": "x"}]})
    b = broker.batches[r["batch_id"]]
    b["created_at"] = 100.0
    b["items"][0].update(state="running", studio="image@macmini-m1-01",
                           run_started=110.0, last_progress_at=120.0)
    monkeypatch.setattr(broker.time, "time", lambda: 1100.0)

    summary = broker.batch_summary(b)

    assert summary["processing_started_at"] == 110.0
    assert summary["processing_elapsed_s"] == 990.0
    assert summary["last_activity_at"] == 120.0
    assert summary["no_progress_s"] == 980.0
    assert summary["stalled"] is True


def test_summary_distinguishes_delayed_retry(reset):
    import time
    r = broker.submit_batch({"modality": "image", "model": "a/b",
                             "items": [{"prompt": "x"}]})
    item = broker.batches[r["batch_id"]]["items"][0]
    item.update(error="try 1 failed: disconnected", retry_at=time.time() + 5)
    summary = broker.batch_summary(broker.batches[r["batch_id"]])
    assert summary["queued"] == 0 and summary["retrying"] == 1
    assert summary["next_retry_at"] == item["retry_at"]


class _CapClient:
    def __init__(self): self.posts = []
    async def post(self, url, json=None, timeout=None): self.posts.append((url, json))


class _RecoveryResponse:
    def __init__(self, job): self.status_code, self._job = 200, job
    def json(self): return {"job": self._job}


class _RecoveryClient:
    def __init__(self, jobs): self.jobs, self.calls = list(jobs), 0
    async def get(self, url, headers=None, timeout=None):
        self.calls += 1
        value = self.jobs.pop(0)
        if isinstance(value, Exception): raise value
        return _RecoveryResponse(value)


class _PeerRouteResponse:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload
        self.headers = {"content-type": "application/json"}
        self.text = ""

    def json(self):
        return self._payload


class _PeerRouteClient:
    def __init__(self):
        self.calls = []

    async def post(self, url, json=None, headers=None, **_kwargs):
        self.calls.append(("POST", url, headers))
        return _PeerRouteResponse({"job": {"id": "worker-1"}})

    async def get(self, url, headers=None, **_kwargs):
        self.calls.append(("GET", url, headers))
        return _PeerRouteResponse({"job": {
            "id": "worker-1", "state": "done", "output_path": "/tmp/x.png",
            "output_url": "/api/generate/jobs/worker-1/image",
        }})


class _RejectedClient:
    def __init__(self, status):
        self.status = status

    async def post(self, *_args, **_kwargs):
        response = _PeerRouteResponse({"detail": "rejected"})
        response.status_code = self.status
        return response


class _PollRejectedClient:
    async def post(self, *_args, **_kwargs):
        return _PeerRouteResponse({"job": {"id": "worker-1"}})

    async def get(self, *_args, **_kwargs):
        response = _PeerRouteResponse({"detail": "credential rejected"})
        response.status_code = 401
        return response


class _MemoryGuardClient:
    async def post(self, *_args, **_kwargs):
        return _PeerRouteResponse({"job": {"id": "worker-memory"}})

    async def get(self, *_args, **_kwargs):
        return _PeerRouteResponse({"job": {
            "id": "worker-memory", "state": "error",
            "error": "MemoryGuardError: needs ~5.6GB, ~2.7GB free",
        }})


@pytest.mark.asyncio
async def test_connection_drop_recovers_completed_worker_without_duplicate(reset):
    r = broker.submit_batch({"modality": "image", "model": "a/b",
                             "items": [{"prompt": "x"}]})
    b = broker.batches[r["batch_id"]]
    item = b["items"][0]
    item.update(state="running", tries=1, studio="image@mac-b",
                studio_job_id="worker-1")
    studio = {"id": "image@mac-b", "modality": "image", "machine": "mac-b",
              "host": "127.0.0.1", "port": 47868}
    client = _RecoveryClient([
        httpx.RemoteProtocolError("connection dropped"),
        {"id": "worker-1", "state": "done", "output_path": "/tmp/x.png",
         "output_url": "/api/generate/jobs/worker-1/image",
         "duration_seconds": 81.0, "resolved_seed": 7},
    ])
    ok = await broker._recover_worker_job(client, b, item, studio, {}, 0.0)
    assert ok is True
    assert item["state"] == "done" and item["asset_id"]
    assert client.calls == 2


@pytest.mark.asyncio
async def test_generation_uses_connected_peer_hub_route(reset, monkeypatch):
    """The broker must not bypass peers.studio_request for remote workers.

    A remote Studio can have a stale in-memory fleet token while its local Hub
    is connected with the current token. Routing through that peer Hub avoids a
    batch-wide 401 storm and keeps the credential inside the Hub fleet.
    """
    submitted = broker.submit_batch({
        "modality": "image", "model": "a/b", "items": [{"prompt": "x"}],
    })
    batch = broker.batches[submitted["batch_id"]]
    item = batch["items"][0]
    item.update(state="running", tries=1, studio="image@mac-b")
    studio = {
        "id": "image@mac-b", "modality": "image", "machine": "mac-b",
        "host": "100.1.1.1", "port": 47868,
    }
    routed_paths = []

    def route(_studio, path):
        routed_paths.append(path)
        return f"http://100.1.1.1:47873/studio/image/{path.lstrip('/')}", {
            "X-Hub-Token": "fleet-secret",
        }

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(broker, "studio_request", route)
    monkeypatch.setattr(broker.asyncio, "sleep", no_sleep)
    client = _PeerRouteClient()

    await broker._run_item(client, batch, item, studio)

    assert item["state"] == "done"
    assert routed_paths == [
        "/api/generate/txt2img",
        "/api/generate/jobs/worker-1",
    ]
    assert all(call[1].startswith("http://100.1.1.1:47873/studio/image/")
               for call in client.calls)
    assert all(call[2] == {"X-Hub-Token": "fleet-secret"}
               for call in client.calls)


@pytest.mark.asyncio
async def test_authentication_failure_is_terminal_without_retry(reset):
    submitted = broker.submit_batch({
        "modality": "image", "model": "a/b", "items": [{"prompt": "x"}],
    })
    batch = broker.batches[submitted["batch_id"]]
    item = batch["items"][0]
    item.update(state="running", tries=1, studio="image@mac-b")
    studio = {"id": "image@mac-b", "modality": "image", "machine": "mac-b",
              "host": "100.1.1.1", "port": 47868}

    await broker._run_item(_RejectedClient(401), batch, item, studio)

    assert item["state"] == "error"
    assert item["tries"] == 1 and item["retry_at"] is None


@pytest.mark.asyncio
async def test_transient_worker_failure_requeues_with_delay(reset):
    submitted = broker.submit_batch({
        "modality": "image", "model": "a/b", "items": [{"prompt": "x"}],
    })
    batch = broker.batches[submitted["batch_id"]]
    item = batch["items"][0]
    item.update(state="running", tries=1, studio="image@mac-b")
    studio = {"id": "image@mac-b", "modality": "image", "machine": "mac-b",
              "host": "100.1.1.1", "port": 47868}

    await broker._run_item(_RejectedClient(503), batch, item, studio)

    assert item["state"] == "queued" and item["retry_at"]
    assert item["infra_failures"] == 1
    assert item["error"].startswith("connection failure 1/8; recovering automatically")
    assert item["avoid_machines"]["mac-b"] > 0


@pytest.mark.asyncio
async def test_memory_guard_wait_does_not_consume_attempt(reset, monkeypatch):
    submitted = broker.submit_batch({
        "modality": "voice", "model": "a/b", "items": [{"text": "hello"}],
    })
    batch = broker.batches[submitted["batch_id"]]
    item = batch["items"][0]
    item.update(state="running", tries=1, studio="voice@mac-b")
    studio = {"id": "voice@mac-b", "modality": "voice", "machine": "mac-b",
              "host": "100.1.1.1", "port": 47869}

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(broker.asyncio, "sleep", no_sleep)
    await broker._run_item(_MemoryGuardClient(), batch, item, studio)

    assert item["state"] == "queued"
    assert item["tries"] == 0
    assert item["error"].startswith("waiting for capacity: MemoryGuardError")
    assert item["retry_at"] is not None
    assert broker.machine_protection_snapshot() == {}


@pytest.mark.asyncio
async def test_poll_authentication_failure_is_terminal(reset, monkeypatch):
    submitted = broker.submit_batch({
        "modality": "image", "model": "a/b", "items": [{"prompt": "x"}],
    })
    batch = broker.batches[submitted["batch_id"]]
    item = batch["items"][0]
    item.update(state="running", tries=1, studio="image@mac-b")
    studio = {"id": "image@mac-b", "modality": "image", "machine": "mac-b",
              "host": "100.1.1.1", "port": 47868}

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(broker.asyncio, "sleep", no_sleep)
    await broker._run_item(_PollRejectedClient(), batch, item, studio)

    assert item["state"] == "error"
    assert item["error"] == "HTTP 401: credential rejected"


@pytest.mark.asyncio
async def test_item_webhook_fires_once_on_terminal(reset):
    r = broker.submit_batch({"modality": "image", "model": "a/b",
                             "itemWebhook": "http://cb", "items": [{"prompt": "x"}]})
    b = broker.batches[r["batch_id"]]
    it = b["items"][0]
    it.update(state="done", studio="image@mac-b", artifact_url="http://u/1")
    c = _CapClient()
    await broker._post_item_webhook(c, b, it)
    await broker._post_item_webhook(c, b, it)   # already notified → no-op
    assert len(c.posts) == 1
    url, payload = c.posts[0]
    assert url == "http://cb"
    assert payload["index"] == 0 and payload["machine"] == "mac-b"
    assert payload["state"] == "done" and payload["total"] == 1 and payload["done"] == 1


@pytest.mark.asyncio
async def test_item_webhook_skips_non_terminal_and_when_unset(reset):
    r = broker.submit_batch({"modality": "image", "model": "a/b",
                             "itemWebhook": "http://cb", "items": [{"prompt": "x"}]})
    b = broker.batches[r["batch_id"]]
    it = b["items"][0]; it["state"] = "queued"
    c = _CapClient()
    await broker._post_item_webhook(c, b, it)   # not terminal → skip
    assert c.posts == []
    # no itemWebhook configured → skip even when terminal
    r2 = broker.submit_batch({"modality": "image", "model": "a/b", "items": [{"prompt": "x"}]})
    b2 = broker.batches[r2["batch_id"]]; b2["items"][0]["state"] = "done"
    await broker._post_item_webhook(c, b2, b2["items"][0])
    assert c.posts == []


def test_disabled_machine_takes_no_jobs(reset):
    from backend import registry
    mon = broker._monitor()
    img = next(s for s in mon.registry if s["modality"] == "image")
    mon.status[img["id"]] = {"status": "up"}
    machine = img.get("machine", "local")
    assert img["id"] in [s["id"] for s in broker._eligible_studios("image", "swarm")]
    try:
        registry.set_machine_enabled(machine, False)
        assert img["id"] not in [s["id"] for s in broker._eligible_studios("image", "swarm")]
    finally:
        registry.set_machine_enabled(machine, True)
    assert img["id"] in [s["id"] for s in broker._eligible_studios("image", "swarm")]


def test_repeated_connection_failures_temporarily_quarantine_machine(reset, monkeypatch):
    studio = {"id": "image@mac-b", "machine": "mac-b"}
    now = 1000.0
    monkeypatch.setattr(broker.time, "time", lambda: now)

    broker._mark_machine_failure(studio, "connection dropped")
    assert broker._machine_blocked("mac-b") is False
    broker._mark_machine_failure(studio, "connection dropped again")

    protection = broker.machine_protection_snapshot()["mac-b"]
    assert protection["quarantined"] is True
    assert protection["retry_in_s"] == broker.MACHINE_COOLDOWN_S

    broker._mark_machine_success(studio)
    assert broker.machine_protection_snapshot()["mac-b"]["quarantined"] is False
    assert broker.machine_protection_snapshot()["mac-b"]["failures"] == 0


def test_item_avoids_recently_failed_machine(reset):
    now = 100.0
    item = {"avoid_machines": {"mac-a": 150.0}}
    assert broker._item_allows_studio(item, {"machine": "mac-a"}, now) is False
    assert broker._item_allows_studio(item, {"machine": "mac-b"}, now) is True


def test_machine_lease_blocks_other_heavy_studios_on_same_mac(reset):
    mon = broker._monitor()
    img = next(s for s in mon.registry if s["modality"] == "image")
    render = next(s for s in mon.registry if s["modality"] == "render")
    # Defaults share the physical "local" machine.
    assert img.get("machine", "local") == render.get("machine", "local")
    mon.status[img["id"]] = {"status": "up"}
    mon.status[render["id"]] = {"status": "up", "health": {"render_score": 100}}
    broker._busy.add(img["id"])
    try:
        assert broker._eligible_studios("render", "pool") == []
    finally:
        broker._busy.discard(img["id"])
    assert [s["id"] for s in broker._eligible_studios("render", "pool")] == [render["id"]]


def test_render_batches_have_queue_priority_without_preemption(reset):
    image = broker.submit_batch({"modality": "image", "model": "a/b",
                                 "items": [{"prompt": "image"}]})
    render = broker.submit_batch({"modality": "render", "model": "episode-assembly-v1",
                                  "items": [{"prompt": "episode"}]})
    ordered = broker._queued_batches()
    assert ordered[0]["id"] == render["batch_id"]
    assert ordered[1]["id"] == image["batch_id"]


def test_pending_render_reserves_its_machine_from_external_queues(reset):
    mon = broker._monitor()
    render_studio = next(s for s in mon.registry if s["modality"] == "render")
    machine = render_studio.get("machine", "local")
    mon.status[render_studio["id"]] = {"status": "up", "health": {"render_score": 100}}
    render = broker.submit_batch({
        "modality": "render", "model": "episode-assembly-v1",
        "items": [{"prompt": "episode"}],
    })
    assert broker.acquire_external_machine(machine, "chat:episode:0") is False
    broker.batches[render["batch_id"]]["items"][0]["state"] = "running"
    assert broker.acquire_external_machine(machine, "chat:episode:0") is True
    broker.release_external_machine(machine, "chat:episode:0")


def test_render_workers_rank_by_reported_hardware_score(reset):
    mon = broker._monitor()
    local = next(s for s in mon.registry if s["modality"] == "render")
    remote = {**local, "id": "render@m4-16", "machine": "m4-16",
              "host": "10.0.0.2"}
    mon.registry.append(remote)
    mon.status[local["id"]] = {"status": "up", "health": {"render_score": 20}}
    mon.status[remote["id"]] = {"status": "up", "health": {"render_score": 100}}
    try:
        eligible = broker._eligible_studios("render", "pool")
        assert [s["id"] for s in eligible[:2]] == [remote["id"], local["id"]]
    finally:
        mon.registry.remove(remote)
        mon.status.pop(remote["id"], None)


def test_remote_render_routing_excludes_hub_machine(reset):
    mon = broker._monitor()
    local = next(s for s in mon.registry if s["modality"] == "render")
    remote = {**local, "id": "render@m4-16", "machine": "m4-16", "host": "10.0.0.2"}
    mon.registry.append(remote)
    mon.status[local["id"]] = {"status": "up", "health": {"render_score": 500}}
    mon.status[remote["id"]] = {"status": "up", "health": {"render_score": 1}}
    try:
        eligible = broker._eligible_studios("render", "remote")
        assert [s["id"] for s in eligible] == [remote["id"]]
    finally:
        mon.registry.remove(remote)
        mon.status.pop(remote["id"], None)


def test_elevenlabs_cloud_jobs_only_use_local_voice_gateway(reset):
    mon = broker._monitor()
    local = next(s for s in mon.registry if s["id"] == "voice")
    remote = {**local, "id": "voice@macmini-m1-01", "machine": "macmini-m1-01",
              "host": "10.0.0.3"}
    mon.registry.append(remote)
    mon.status[local["id"]] = {"status": "up"}
    mon.status[remote["id"]] = {"status": "up"}
    try:
        elevenlabs = broker._eligible_studios(
            "voice", "pool", "provider:elevenlabs:eleven_multilingual_v2",
        )
        local_tts = broker._eligible_studios("voice", "pool", "mlx-community/Kokoro")
        assert [s["id"] for s in elevenlabs] == ["voice"]
        assert {s["id"] for s in local_tts} == {"voice", "voice@macmini-m1-01"}
    finally:
        mon.registry.remove(remote)
        mon.status.pop(remote["id"], None)


def test_prompt_and_text_both_accepted(reset):
    r = broker.submit_batch({"modality": "voice", "model": "a/b",
                             "items": [{"text": "spoken"}]})
    b = broker.batches[r["batch_id"]]
    assert b["items"][0]["prompt"] == "spoken"


def test_uncertain_paid_worker_result_is_never_retryable():
    uncertain = broker._worker_terminal_error(
        "ProviderResultUncertain: paid result could not be recovered"
    )
    ordinary = broker._worker_terminal_error("RuntimeError: temporary worker failure")
    assert uncertain.retryable is False
    assert ordinary.retryable is True


@pytest.mark.asyncio
async def test_cancel_batch(reset):
    r = broker.submit_batch({"modality": "image", "model": "a/b",
                             "items": [{"prompt": "x"}]})
    result = await broker.cancel_batch(r["batch_id"])
    assert result["queued_cancelled"] == 1
    b = broker.batches[r["batch_id"]]
    assert b["cancelled"] and b["items"][0]["state"] == "cancelled"
    assert await broker.cancel_batch("nope") is None


class _CancelResponse:
    def __init__(self, status_code=200): self.status_code = status_code


class _CancelClient:
    def __init__(self, status_code=200): self.status_code, self.deletes = status_code, []
    async def delete(self, url, **kwargs):
        self.deletes.append((url, kwargs))
        return _CancelResponse(self.status_code)
    async def post(self, *args, **kwargs):
        return _CancelResponse()


@pytest.mark.asyncio
async def test_cancel_batch_signals_running_worker_immediately(reset):
    studio = next(s for s in broker._monitor().registry if s["modality"] == "image")
    r = broker.submit_batch({"modality": "image", "model": "a/b",
                             "items": [{"prompt": "x"}]})
    item = broker.batches[r["batch_id"]]["items"][0]
    item.update(state="running", studio=studio["id"], studio_job_id="worker-42")
    client = _CancelClient()
    result = await broker.cancel_batch(r["batch_id"], client)
    assert result["running_signalled"] == 1 and result["running_pending"] == 0
    assert client.deletes[0][0].endswith("/api/generate/jobs/worker-42")


def test_clear_finished_batches_keeps_generated_assets(reset):
    r = broker.submit_batch({"modality": "image", "model": "a/b",
                             "items": [{"prompt": "x"}]})
    batch_id = r["batch_id"]
    batch = broker.batches[batch_id]
    batch["items"][0]["state"] = "done"
    ledger.save_batch(batch)
    asset_id = ledger.record_asset(
        source="job", modality="image", batch_id=batch_id,
        artifact_path="/tmp/keep-this-image.png")

    result = broker.clear_finished_batches(modality="image")

    assert result == {"cleared": 1, "batch_ids": [batch_id]}
    assert batch_id not in broker.batches and ledger.load_batch(batch_id) is None
    assert ledger.get_asset(asset_id)["artifact_path"] == "/tmp/keep-this-image.png"


def test_multipart_fields():
    out = broker._multipart_fields({"repo": "a/b", "prompt": "hi", "width": 512,
                                    "seed": None, "lora_names": ["x", "y"],
                                    "lora_scales": [0.5, 1.0], "ignored": "z"})
    assert out["repo"] == "a/b" and out["width"] == "512"
    assert out["lora_names"] == "x,y" and out["lora_scales"] == "0.5,1.0"
    assert "seed" not in out and "ignored" not in out


def test_video_multipart_fields_are_img2video_only():
    out = broker._video_multipart_fields({
        "repo": "fal:provider/image-to-video",
        "mode": "img2video",
        "prompt": "gentle camera push",
        "duration": 5,
        "aspect_ratio": "16:9",
        "provider_params": {"secret": "must-not-forward"},
        "reference_images": [{"asset_id": "asset-1"}],
    })
    assert out == {
        "repo": "fal:provider/image-to-video",
        "mode": "img2video",
        "prompt": "gentle camera push",
        "duration": "5",
        "aspect_ratio": "16:9",
    }


@pytest.mark.asyncio
async def test_resolve_reference_b64():
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 20
    ref = {"b64": base64.b64encode(png).decode(), "mime": "image/png"}
    async with httpx.AsyncClient() as c:
        data, mime = await broker._resolve_reference(c, ref)
    assert data == png and mime == "image/png"


@pytest.mark.asyncio
async def test_resolve_reference_data_url_prefix():
    raw = base64.b64encode(b"hello").decode()
    ref = {"b64": f"data:image/png;base64,{raw}"}
    async with httpx.AsyncClient() as c:
        data, _ = await broker._resolve_reference(c, ref)
    assert data == b"hello"


@pytest.mark.asyncio
async def test_resolve_reference_asset_id(reset, tmp_path):
    p = tmp_path / "ref.png"
    p.write_bytes(b"IMGBYTES")
    aid = ledger.record_asset(source="upload", modality="image", machine="local",
                              artifact_path=str(p))
    async with httpx.AsyncClient() as c:
        data, _ = await broker._resolve_reference(c, {"asset_id": aid})
    assert data == b"IMGBYTES"


@pytest.mark.asyncio
async def test_resolve_reference_errors(reset):
    async with httpx.AsyncClient() as c:
        with pytest.raises(ValueError):
            await broker._resolve_reference(c, {})
        with pytest.raises(ValueError):
            await broker._resolve_reference(c, {"asset_id": "missing"})


def test_local_gate_skip_when_machine_too_small(reset):
    # BUG FIX: a too-small LOCAL machine must SKIP (so a bigger remote can take
    # the job), never error the whole batch.
    mem = {"min_total": 32, "size": 10}
    host = {"total_gb": 16, "available_gb": 12}
    decision, note = broker._local_gate(mem, host)
    assert decision == "skip" and "32GB" in note


def test_qwen_standard_voice_uses_8gb_workers_with_a_safe_free_ram_floor(reset):
    """The 0.6B MLX model uses 8 GB M1s only when a cold load can fit."""
    entry = {
        "repo": "mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-8bit",
        "min_unified_memory_gb": 8,
        "size_gb": 2,
    }
    required = workload_policy.required_total_memory_gb(entry["repo"], entry)
    required_free = workload_policy.required_free_memory_gb(entry["repo"], entry)
    assert required == 8
    assert required_free == 3.2
    assert workload_policy.required_total_memory_gb(
        "standard-voice", {**entry, "aliases": ["standard-voice"]},
    ) == 8

    decision, note = broker._memory_gate(
        {"min_total": required, "min_free": required_free, "size": entry["size_gb"]},
        {"total_gb": 8, "available_gb": 3.1},
    )
    assert decision == "wait" and "3.2GB" in note
    assert broker._memory_gate(
        {"min_total": required, "min_free": required_free, "size": entry["size_gb"]},
        {"total_gb": 8, "available_gb": 3.3},
    )[0] == "run"


def test_non_voice_models_keep_their_catalog_memory_requirement(reset):
    entry = {"repo": "acme/image-model", "min_unified_memory_gb": 8}
    assert workload_policy.required_total_memory_gb(entry["repo"], entry) == 8


def test_catalog_can_publish_an_explicit_runtime_free_memory_floor(reset):
    entry = {
        "repo": "acme/image-model",
        "min_free_memory_gb": "2.75",
        "size_gb": 100,
    }
    assert workload_policy.required_free_memory_gb(entry["repo"], entry) == 2.75


def test_local_gate_run_when_fits(reset):
    decision, _ = broker._local_gate({"min_total": 8, "size": 2},
                                     {"total_gb": 16, "available_gb": 10})
    assert decision == "run"


def test_download_size_is_not_treated_as_runtime_memory(reset):
    # Regression: Image Studio corrected this repo's *download* size from 2.3
    # to 4.6 GB. The Hub used to add 1 GB and falsely demand 5.6 GB free.
    mem = {"min_total": 8, "min_free": None, "size": 4.6}
    decision, note = broker._local_gate(
        mem, {"total_gb": 8, "available_gb": 2.4})
    assert decision == "run" and note is None


def test_local_gate_waits_at_real_low_memory_floor(reset):
    decision, note = broker._local_gate(
        {"min_total": 8, "size": 0.1},
        {"total_gb": 16, "available_gb": 1.9},
    )
    assert decision == "wait"
    assert "2.0GB" in note and "1.9GB free" in note


def test_local_gate_reservation_prevents_double_load(reset):
    mem = {"min_total": 8, "min_free": 3.0, "size": 60}
    host = {"total_gb": 16, "available_gb": 5.5}
    assert broker._local_gate(mem, host)[0] == "run"
    # Simulate one in-flight local dispatch reserving its admission floor.
    broker._reserved["gb"] = 3.0
    # Only ~2.5GB is effectively free, so a second 3GB workload must wait.
    assert broker._local_gate(mem, host)[0] == "wait"


def test_remote_memory_gate_uses_peer_host_snapshot(reset, monkeypatch):
    monkeypatch.setattr(
        broker.peers, "cached",
        lambda machine: {"host": {"total_gb": 8, "available_gb": 2.0}},
    )
    studio = {"machine": "mac-b"}
    host = broker._host_for_studio(studio)
    decision, note = broker._memory_gate({"min_total": 8, "size": 4.0}, host)
    assert decision == "run" and note is None


def test_restore_batches_requeues_running(reset):
    b = {"id": "bx", "modality": "image", "model": "a/b", "created_at": 1.0,
         "cancelled": False, "shared_params": {}, "routing": "pool",
         "items": [{"index": 0, "state": "running", "studio": "image",
                    "studio_job_id": "j1", "prompt": "x", "seed": None,
                    "params": {}, "tries": 1, "artifact_path": None,
                    "artifact_url": None, "asset_id": None, "error": None}]}
    ledger.save_batch(b)
    n = broker.restore_batches()
    assert n == 1
    # the in-flight item must be re-queued (its studio job is orphaned)
    assert broker.batches["bx"]["items"][0]["state"] == "queued"
    assert broker.batches["bx"]["items"][0]["studio"] is None
