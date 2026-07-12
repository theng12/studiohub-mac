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
