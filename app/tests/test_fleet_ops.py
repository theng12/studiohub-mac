import pytest

from backend import broker, fleet_ops


def test_catalog_and_diagnostic_summaries():
    total, ready = fleet_ops._downloaded({"models": [
        {"cache": {"state": "cached"}}, {"cache": {"state": "absent"}}, {"is_cloud": True}
    ]})
    assert (total, ready) == (3, 2)
    assert fleet_ops._diag_state({"available": False}) == "warn"
    assert fleet_ops._diag_state({"available": True}) == "pass"


@pytest.mark.asyncio
async def test_preflight_reports_port_conflicts(monkeypatch, monitor):
    studio = dict(monitor.registry[0])
    duplicate = {**studio, "id": "duplicate"}
    monitor.registry = [studio, duplicate]
    monitor.status[studio["id"]] = {"status": "up"}

    class BrokenClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def get(self, *args, **kwargs): raise fleet_ops.httpx.ConnectError("stop after local checks")

    monkeypatch.setattr(fleet_ops.httpx, "AsyncClient", lambda **kwargs: BrokenClient())
    row = await fleet_ops._preflight_one(monitor, studio)
    port = next(c for c in row["checks"] if c["name"] == "port")
    assert port["status"] == "fail" and "duplicate" in port["detail"]


def test_maintenance_drains_broker(reset):
    mon = broker._monitor()
    image = next(s for s in mon.registry if s["id"] == "image")
    mon.status["image"] = {"status": "up"}
    assert image in broker._eligible_studios("image", "swarm")
    broker.set_maintenance("image", True)
    assert image not in broker._eligible_studios("image", "swarm")
    broker.set_maintenance("image", False)


@pytest.mark.asyncio
async def test_maintenance_drains_chat_and_transcription(reset, monitor, monkeypatch):
    from backend import chat_jobs, transcription_jobs

    chat = next(s for s in monitor.registry if s["id"] == "chat")
    voice = next(s for s in monitor.registry if s["id"] == "voice")
    monitor.status["chat"] = {"status": "up"}
    monitor.status["voice"] = {"status": "up"}

    async def chat_catalog(studio):
        return {"models": [{"repo": "chat/model", "cache": {"state": "cached"}}]}

    async def transcription_catalog(studio):
        return {"available": True, "models": [{"repo": "voice/model", "cached": True}]}

    monkeypatch.setattr(monitor, "get_catalog", chat_catalog)
    monkeypatch.setattr(monitor, "get_transcription", transcription_catalog)
    assert chat in await chat_jobs._eligible_studios(monitor, "chat/model")
    assert voice in await transcription_jobs._eligible_studios(monitor, "voice/model")
    broker.set_maintenance("chat", True)
    broker.set_maintenance("voice", True)
    assert await chat_jobs._eligible_studios(monitor, "chat/model") == []
    assert await transcription_jobs._eligible_studios(monitor, "voice/model") == []
    broker.set_maintenance("chat", False)
    broker.set_maintenance("voice", False)


def test_rolling_update_waits_for_every_queue_type(reset):
    from backend import chat_jobs, transcription_jobs

    broker._busy.add("image")
    chat_jobs.busy_studios.add("chat")
    transcription_jobs.busy_studios.add("voice")
    assert fleet_ops._active_studio_leases() == {"image", "chat", "voice"}


@pytest.mark.asyncio
async def test_update_health_waits_for_new_disk_version(monkeypatch, tmp_path):
    studio = {"id": "chat", "app": "chatstudio-mac", "host": "127.0.0.1", "port": 1}
    (tmp_path / "VERSION").write_text("2.0.0")
    item = {"expected_version": "1.0.0", "from_version": "1.0.0"}

    class Response:
        status_code = 200

        def __init__(self, version): self.version = version
        def json(self): return {"ok": True, "app_version": self.version}

    class Client:
        calls = 0
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def get(self, *args, **kwargs):
            self.calls += 1
            return Response("1.0.0" if self.calls == 1 else "2.0.0")

    client = Client()
    monkeypatch.setattr(fleet_ops, "resolve_app_dir", lambda studio: tmp_path)
    monkeypatch.setattr(fleet_ops.httpx, "AsyncClient", lambda **kwargs: client)

    async def no_sleep(seconds): return None
    monkeypatch.setattr(fleet_ops.asyncio, "sleep", no_sleep)
    await fleet_ops._wait_for_healthy(studio, item)
    assert client.calls == 2 and item["status"] == "complete"
    assert item["detail"] == "healthy on v2.0.0"


@pytest.mark.asyncio
async def test_updates_are_sequential_and_failure_is_contained(monkeypatch, monitor):
    calls = []

    async def fake_update(mon, studio, item):
        calls.append(studio["id"])
        if studio["id"] == "music":
            raise RuntimeError("install failed")
        item.update(status="complete", detail="healthy")

    monkeypatch.setattr(fleet_ops, "_update_one", fake_update)
    job = {"id": "x", "status": "queued", "created_at": 0, "finished_at": None,
           "items": [{"studio": "image", "status": "queued", "detail": ""},
                     {"studio": "music", "status": "queued", "detail": ""},
                     {"studio": "voice", "status": "queued", "detail": ""}]}
    await fleet_ops._run_updates(monitor, job)
    assert calls == ["image", "music", "voice"]
    assert job["status"] == "failed"
    assert job["items"][0]["status"] == "complete"
    assert job["items"][1]["status"] == "failed"
    assert job["items"][2]["status"] == "complete"


@pytest.mark.asyncio
async def test_remote_update_reconnects_after_status_connection_drop(monkeypatch):
    studio = {"id": "voice@mac-a", "modality": "voice", "machine": "mac-a",
              "host": "10.0.0.8", "hub_port": 47873}
    item = {"studio": studio["id"], "status": "updating", "detail": ""}

    class Response:
        status_code = 200

        def __init__(self, payload): self.payload = payload
        def json(self): return self.payload
        def raise_for_status(self): return None

    class Client:
        get_calls = 0

        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def post(self, *args, **kwargs): return Response({"id": "remote-job"})
        async def get(self, *args, **kwargs):
            self.get_calls += 1
            if self.get_calls == 1:
                raise fleet_ops.httpx.ReadError("server disconnected")
            return Response({"status": "complete", "items": [
                {"status": "complete", "detail": "healthy on v2.0.0"}
            ]})

    client = Client()
    monkeypatch.setattr(fleet_ops.httpx, "AsyncClient", lambda **kwargs: client)

    async def no_sleep(seconds): return None
    monkeypatch.setattr(fleet_ops.asyncio, "sleep", no_sleep)

    await fleet_ops._update_remote(studio, item)
    assert client.get_calls == 2
    assert item["status"] == "complete" and item["detail"] == "healthy on v2.0.0"


def test_start_hub_updates_requires_remote_machines(monitor):
    monitor.registry = [s for s in monitor.registry if s.get("machine", "local") == "local"]
    fleet_ops._hub_updates.clear()
    with pytest.raises(ValueError, match="no remote"):
        fleet_ops.start_hub_updates(monitor, "1.0.0", None)


@pytest.mark.asyncio
async def test_start_hub_updates_builds_job(monkeypatch, monitor):
    monitor.registry.append({"id": "image@mac-b", "modality": "image",
                             "host": "10.0.0.9", "port": 47868, "machine": "mac-b"})
    fleet_ops._hub_updates.clear()

    async def _noop(job):
        return None
    monkeypatch.setattr(fleet_ops, "_run_hub_updates", _noop)

    job = fleet_ops.start_hub_updates(monitor, "9.9.9", None)
    assert job["kind"] == "hub" and job["latest"] == "9.9.9"
    assert any(i["machine"] == "mac-b" and i["host"] == "10.0.0.9" for i in job["items"])

    fleet_ops._hub_updates.clear()
    with pytest.raises(ValueError, match="unknown"):
        fleet_ops.start_hub_updates(monitor, "9.9.9", ["does-not-exist"])
    fleet_ops._hub_updates.clear()


def test_self_update_endpoint_requires_auth(client):
    # non-loopback without the token → blocked before the handler runs
    assert client.post("/api/hub/maintenance/self-update").status_code == 401
    assert client.post("/api/hub/maintenance/hub-updates", json={}).status_code == 401


@pytest.mark.asyncio
async def test_preflight_401_is_warning_not_block(monkeypatch, monitor):
    import httpx as _httpx
    studio = dict(monitor.registry[0])
    monitor.registry = [studio]
    monitor.status[studio["id"]] = {"status": "up"}

    class Resp:
        def __init__(self, status, payload):
            self.status_code = status
            self._p = payload

        def json(self):
            return self._p

        def raise_for_status(self):
            if self.status_code >= 400:
                raise _httpx.HTTPStatusError(
                    "err", request=_httpx.Request("GET", "http://x"), response=self)

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None

        async def get(self, url, **kw):
            if url.endswith("/api/version"):
                return Resp(200, {"app_version": "9.9.9"})
            if url.endswith("/api/capabilities"):
                return Resp(200, {"schema_version": 1,
                                  "studio": {"modality": studio["modality"]},
                                  "operations": ["chat"]})
            if url.endswith("/api/catalog"):
                return Resp(401, {})           # studio rejects the fleet token
            return Resp(200, {})

    monkeypatch.setattr(fleet_ops.httpx, "AsyncClient", lambda **kw: Client())
    row = await fleet_ops._preflight_one(monitor, studio)
    fa = next(c for c in row["checks"] if c["name"] == "fleet authentication")
    assert fa["status"] == "warn"      # 401 → warn (non-blocking), not fail
    assert row["version"] == "9.9.9"   # version captured from the public endpoint
    assert row["status"] != "fail"     # so the studio stays eligible for update
