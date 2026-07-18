from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from backend import fleet_ops
from backend.fleet_auto_updates import FleetAutoUpdates


class FakeHubUpdater:
    def public_status(self):
        return {"settings": {"mode": "off"}, "installed_version": "1.0.0"}


class FakeMonitor:
    def __init__(self):
        self.registry = [
            {"id": "voice@a", "title": "Voice A", "modality": "voice",
             "host": "127.0.0.1", "port": 47001, "machine": "a"},
            {"id": "chat@b", "title": "Chat B", "modality": "chat",
             "host": "127.0.0.1", "port": 47002, "machine": "b"},
        ]
        self.status = {"voice@a": {"status": "up"}, "chat@b": {"status": "up"}}


def _job(*target_ids: str) -> dict:
    return {
        "id": "test", "status": "queued", "created_at": time.time(),
        "finished_at": None,
        "items": [{"target": value, "status": "queued", "detail": "waiting"}
                  for value in target_ids],
    }


@pytest.fixture(autouse=True)
def published_versions(monkeypatch):
    """Unit tests control release discovery without contacting live GitHub."""
    state = {"versions": {}, "checked_at": time.time(), "errors": {}}

    async def refresh(*, force=False):
        return state

    monkeypatch.setattr(fleet_ops, "refresh_published_versions", refresh)
    monkeypatch.setattr(fleet_ops, "published_version_snapshot", lambda: state)
    return state


def test_updates_are_staggered_and_health_gated(monkeypatch):
    coordinator = FleetAutoUpdates(FakeMonitor(), FakeHubUpdater(),
                                   stagger_seconds=0, poll_seconds=0, update_timeout=1)
    started: set[str] = set()
    events: list[str] = []

    async def request(target, method, path, payload=None):
        target_id = target["id"]
        if path.endswith("/check"):
            return {"state": "checking"}
        if path.endswith("/status"):
            return {"update_available": True, "state": "succeeded" if target_id in started else "idle"}
        if path.endswith("/readiness"):
            return {"idle": True, "reasons": []}
        if path.endswith("/update"):
            started.add(target_id)
            events.append("update:" + target_id)
            return {"state": "updating"}
        if path == "/api/health":
            events.append("health:" + target_id)
            return {"ok": True, "app_version": "2.0.0"}
        raise AssertionError((method, path, payload))

    monkeypatch.setattr(coordinator, "_request", request)
    known = {target["id"]: target for target in coordinator.targets()
             if target["kind"] == "studio"}
    job = _job("voice@a", "chat@b")
    asyncio.run(coordinator._run_updates(job, known))

    assert events == ["update:voice@a", "health:voice@a",
                      "update:chat@b", "health:chat@b"]
    assert job["status"] == "complete"
    assert [item["status"] for item in job["items"]] == ["complete", "complete"]


def test_connection_drop_reconnects_before_marking_success(monkeypatch):
    coordinator = FleetAutoUpdates(FakeMonitor(), FakeHubUpdater(),
                                   stagger_seconds=0, poll_seconds=0, update_timeout=1)
    polls = 0

    async def request(target, method, path, payload=None):
        nonlocal polls
        if path.endswith("/check"):
            return {"state": "checking"}
        if path.endswith("/status") and method == "GET":
            polls += 1
            if polls == 1:
                return {"update_available": True, "state": "idle"}
            if polls == 2:
                raise httpx.ConnectError("restart in progress")
            return {"update_available": True, "state": "succeeded"}
        if path.endswith("/readiness"):
            return {"idle": True, "reasons": []}
        if path.endswith("/update"):
            return {"state": "updating"}
        if path == "/api/health":
            return {"ok": True, "app_version": "2.0.0"}
        raise AssertionError((method, path))

    monkeypatch.setattr(coordinator, "_request", request)
    target = coordinator._target("voice@a")
    item = _job("voice@a")["items"][0]
    asyncio.run(coordinator._update_one(target, item))

    assert polls >= 3
    assert item["status"] == "complete"
    assert "healthy on v2.0.0" in item["detail"]


def test_active_target_is_durably_scheduled_until_idle(monkeypatch):
    coordinator = FleetAutoUpdates(FakeMonitor(), FakeHubUpdater(), poll_seconds=0)
    updates = []

    async def request(target, method, path, payload=None):
        if path.endswith("/check"):
            return {"state": "checking"}
        if path.endswith("/status"):
            return {"update_available": True, "state": "idle"}
        if path.endswith("/readiness"):
            return {"idle": False, "reasons": ["generation is running"]}
        if path.endswith("/update"):
            updates.append((target["id"], payload))
            return {"state": "deferred", "pending_manual": True}
        raise AssertionError((method, path))

    monkeypatch.setattr(coordinator, "_request", request)
    item = _job("voice@a")["items"][0]
    asyncio.run(coordinator._update_one(coordinator._target("voice@a"), item))

    assert item["status"] == "scheduled"
    assert "generation" in item["detail"]
    assert updates == [("voice@a", {"after_current": True})]


@pytest.mark.asyncio
async def test_interrupted_job_is_persisted_and_resumed(monkeypatch, tmp_path):
    state_path = tmp_path / "fleet-jobs.json"
    first = FleetAutoUpdates(FakeMonitor(), FakeHubUpdater(), state_path=state_path)
    job = _job("voice@a")
    job["status"] = "running"
    job["items"][0].update(status="updating", detail="restarting")
    first._jobs[job["id"]] = job
    first._persist()

    restored = FleetAutoUpdates(FakeMonitor(), FakeHubUpdater(), state_path=state_path)
    completed = asyncio.Event()

    async def finish(resumed_job, known):
        assert resumed_job["items"][0]["status"] == "queued"
        assert "resuming" in resumed_job["items"][0]["detail"]
        resumed_job["status"] = "complete"
        completed.set()

    monkeypatch.setattr(restored, "_run_updates", finish)
    assert restored.resume_pending() == 1
    await asyncio.wait_for(completed.wait(), timeout=1)


def test_failed_apps_can_be_retried_as_a_new_job(monkeypatch):
    coordinator = FleetAutoUpdates(FakeMonitor(), FakeHubUpdater())
    old = _job("voice@a", "chat@b")
    old["status"] = "failed"
    old["items"][0]["status"] = "failed"
    old["items"][1]["status"] = "complete"
    coordinator._jobs[old["id"]] = old
    started = {}

    def start(targets):
        started["targets"] = targets
        return {"id": "retry"}

    monkeypatch.setattr(coordinator, "start_idle_updates", start)
    assert coordinator.retry_failed(old["id"]) == {"id": "retry"}
    assert started["targets"] == ["voice@a"]


def test_per_app_mode_preserves_its_schedule(monkeypatch):
    coordinator = FleetAutoUpdates(FakeMonitor(), FakeHubUpdater())
    saved = {}

    async def request(target, method, path, payload=None):
        if path.endswith("/status"):
            return {"settings": {"mode": "off", "frequency": "weekly",
                                 "maintenance_hour": 22, "weekday": 3, "idle_only": False}}
        if path.endswith("/settings"):
            saved.update(payload or {})
            return {"settings": saved}
        raise AssertionError((method, path))

    monkeypatch.setattr(coordinator, "_request", request)
    asyncio.run(coordinator.set_mode("voice@a", "notify"))

    assert saved == {"mode": "notify", "frequency": "weekly",
                     "maintenance_hour": 22, "weekday": 3, "idle_only": False}


def test_inventory_prefers_published_version_over_stale_updater_history(monkeypatch):
    coordinator = FleetAutoUpdates(FakeMonitor(), FakeHubUpdater())

    async def request(target, method, path, payload=None):
        if path.endswith("/auto-update/status"):
            return {"settings": {"mode": "auto"}, "installed_version": "1.20.3",
                    "latest_version": "1.20.2", "update_available": True,
                    "state": "succeeded"}
        if path == "/api/update-status":
            return {"app_version": "1.20.3", "latest_version": "1.20.3",
                    "update_available": False}
        raise AssertionError((method, path, payload))

    monkeypatch.setattr(coordinator, "_request", request)
    row = asyncio.run(coordinator._status_one(coordinator._target("voice@a")))

    assert row["installed_version"] == "1.20.3"
    assert row["latest_version"] == "1.20.3"
    assert row["update_available"] is False


def test_inventory_uses_hub_github_watch_over_stale_studio_answers(
        monkeypatch, published_versions):
    published_versions["versions"] = {"voice": "1.20.4"}
    coordinator = FleetAutoUpdates(FakeMonitor(), FakeHubUpdater())

    async def request(target, method, path, payload=None):
        if path.endswith("/auto-update/status"):
            return {"settings": {"mode": "auto"}, "installed_version": "1.20.3",
                    "latest_version": "1.20.3", "update_available": False,
                    "state": "succeeded"}
        if path == "/api/update-status":
            return {"app_version": "1.20.3", "latest_version": "1.20.3",
                    "update_available": False}
        raise AssertionError((method, path, payload))

    monkeypatch.setattr(coordinator, "_request", request)
    row = asyncio.run(coordinator._status_one(coordinator._target("voice@a")))

    assert row["installed_version"] == "1.20.3"
    assert row["latest_version"] == "1.20.4"
    assert row["update_available"] is True


def test_inventory_shows_one_canonical_row_per_repository():
    monitor = FakeMonitor()
    monitor.registry.extend([
        {"id": "voice@remote", "title": "Remote Voice", "modality": "voice",
         "host": "10.0.0.8", "port": 47870, "machine": "remote"},
        {"id": "voice", "title": "Voice Studio KH", "modality": "voice",
         "host": "127.0.0.1", "port": 47870, "machine": "local"},
    ])
    rows = FleetAutoUpdates(monitor, FakeHubUpdater()).targets()
    assert [row["id"] for row in rows] == ["hub@local", "voice", "chat@b"]


def test_update_idle_api_starts_from_the_async_server_loop(authed, monkeypatch):
    from backend.main import fleet_auto_updates

    def start(target_ids):
        asyncio.get_running_loop()
        return {"id": "job-1", "status": "queued", "items": [],
                "target_ids": target_ids}

    monkeypatch.setattr(fleet_auto_updates, "start_idle_updates", start)
    response = authed.post("/api/hub/auto-updates/update-idle",
                           json={"target_ids": ["voice"]})
    assert response.status_code == 200
    assert response.json()["target_ids"] == ["voice"]
