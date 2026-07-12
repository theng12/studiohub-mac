import json

import pytest

from backend import main


class FakeReq:
    def __init__(self, disconnected=False):
        self._d = disconnected

    async def is_disconnected(self):
        return self._d


def test_summary_has_alerts_active(authed):
    d = authed.get("/api/hub/summary").json()
    assert "alerts_active" in d and isinstance(d["alerts_active"], int)


def test_summary_includes_remote_machines(authed, reset):
    # Regression: _build_summary must call hub_resources(local_only=False). Calling
    # it bare uses the Query(False) default object (truthy), which silently drops
    # every remote machine from the live summary.
    reg = main.monitor.registry
    reg.append({"id": "image@mac-b", "modality": "image",
                "host": "100.0.0.9", "port": 47868, "machine": "mac-b"})
    try:
        machines = authed.get("/api/hub/summary").json()["resources"]["machines"]
    finally:
        reg[:] = [s for s in reg if s.get("machine") != "mac-b"]
    assert "mac-b" in machines
    assert "status" in machines["mac-b"]


def test_summary_marks_chat_worker_with_llm_workload(authed, reset):
    from backend import chat_jobs

    main.monitor.status["chat"] = {"status": "up", "last_seen": 1}
    chat_jobs.busy_studios.add("chat")
    chat_jobs.batches["chat-live"] = {
        "id": "chat-live", "project": "story", "episode": "EP0001",
        "packs": [{"state": "running", "studio": "chat", "pack_id": "pack-01",
                   "tries": 1, "started_at": 10}],
    }
    worker = next(s for s in authed.get("/api/hub/summary").json()["studios"]
                  if s["id"] == "chat")
    assert worker["busy"] is True
    assert worker["workload"] == {
        "kind": "chat", "batch_id": "chat-live", "project": "story",
        "episode": "EP0001", "pack_id": "pack-01", "attempt": 1,
        "max_attempts": 3, "started_at": 10,
    }


@pytest.mark.asyncio
async def test_sse_generator_emits_summary(reset):
    gen = main._sse_summary(FakeReq(), interval=0.01)
    try:
        chunk = await gen.__anext__()
    finally:
        await gen.aclose()
    assert chunk.startswith("data: ")
    payload = json.loads(chunk[6:])
    assert "studios" in payload and "alerts_active" in payload


@pytest.mark.asyncio
async def test_sse_generator_stops_on_disconnect(reset):
    gen = main._sse_summary(FakeReq(disconnected=True))
    with pytest.raises(StopAsyncIteration):
        await gen.__anext__()  # disconnected → yields nothing


def test_sse_requires_auth(client):
    # non-loopback without token → 401 BEFORE the stream starts (no hang).
    assert client.get("/api/hub/stream").status_code == 401
