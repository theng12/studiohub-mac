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
