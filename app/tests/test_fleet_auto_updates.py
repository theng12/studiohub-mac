from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from backend import fleet_auto_updates, fleet_ops
from backend.fleet_auto_updates import FleetAutoUpdates


class FakeHubUpdater:
    def public_status(self):
        return {"settings": {"mode": "off"}, "installed_version": "1.0.0"}


class FakeMonitor:
    def __init__(self):
        self.registry = [
            {"id": "voice@a", "title": "Voice A", "modality": "voice",
             "host": "127.0.0.1", "port": 47001, "machine": "a"},
            {"id": "image@b", "title": "Image B", "modality": "image",
             "host": "127.0.0.1", "port": 47002, "machine": "b"},
            {"id": "render", "title": "Render Studio KH", "modality": "render",
             "host": "127.0.0.1", "port": 47874, "machine": "local"},
        ]
        self.status = {"voice@a": {"status": "up"}, "image@b": {"status": "up"},
                       "render": {"status": "up"}}


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
    job = _job("voice@a", "image@b")
    asyncio.run(coordinator._run_updates(job, known))

    assert events == ["update:voice@a", "health:voice@a",
                      "update:image@b", "health:image@b"]
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
    job["items"][0].update(status="updating", detail="restarting", dependency_convergence=1)
    first._jobs[job["id"]] = job
    first._persist()

    restored = FleetAutoUpdates(FakeMonitor(), FakeHubUpdater(), state_path=state_path)
    assert restored._jobs["test"]["items"][0]["dependency_convergence"] == 1
    completed = asyncio.Event()

    async def finish(resumed_job, known):
        assert resumed_job["items"][0]["status"] == "queued"
        assert "resuming" in resumed_job["items"][0]["detail"]
        assert resumed_job["items"][0]["dependency_convergence"] == 1
        resumed_job["status"] = "complete"
        completed.set()

    monkeypatch.setattr(restored, "_run_updates", finish)
    assert restored.resume_pending() == 1
    await asyncio.wait_for(completed.wait(), timeout=1)


def test_resume_does_not_adopt_legacy_fleet_ops_history(monkeypatch, tmp_path):
    monkeypatch.setitem(fleet_ops._hub_updates, "legacy", {
        "id": "legacy", "status": "failed", "restart_interrupted": True,
        "items": [],
    })
    coordinator = FleetAutoUpdates(
        FakeMonitor(), FakeHubUpdater(), state_path=tmp_path / "fleet-jobs.json"
    )

    assert coordinator.resume_pending() == 0


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


async def _status_row_with_capability(value, monkeypatch):
    coordinator = FleetAutoUpdates(FakeMonitor(), FakeHubUpdater())

    async def request(target, method, path, payload=None):
        if path.endswith("/auto-update/status"):
            return {
                "settings": {"mode": "auto"}, "installed_version": "1.20.3",
                "latest_version": "1.20.3", "update_available": False,
                "state": "succeeded", "capabilities": {"dependency_convergence": value},
            }
        if path == "/api/update-status":
            return {"app_version": "1.20.3", "latest_version": "1.20.3"}
        raise AssertionError((target, method, path, payload))

    monkeypatch.setattr(coordinator, "_request", request)
    return await coordinator._status_one(coordinator._target("voice@a"))


def test_inventory_reports_exact_dependency_capability(monkeypatch):
    row = asyncio.run(_status_row_with_capability(1, monkeypatch))

    assert row["dependency_convergence"] == 1


@pytest.mark.parametrize("value", [None, True, 0, 2, "1", {}, []])
def test_inventory_does_not_coerce_dependency_capability(value, monkeypatch):
    row = asyncio.run(_status_row_with_capability(value, monkeypatch))

    assert row["dependency_convergence"] is None


def test_ordinary_update_records_exact_dependency_capability(monkeypatch):
    coordinator = FleetAutoUpdates(FakeMonitor(), FakeHubUpdater(), poll_seconds=0, update_timeout=1)
    started = False

    async def request(target, method, path, payload=None):
        nonlocal started
        if path.endswith("/check"):
            return {"state": "checking"}
        if path.endswith("/status"):
            return {
                "update_available": True,
                "state": "succeeded" if started else "idle",
                "capabilities": {"dependency_convergence": 1},
            }
        if path.endswith("/readiness"):
            return {"idle": True, "reasons": []}
        if path.endswith("/update"):
            started = True
            return {"state": "updating"}
        if path == "/api/health":
            return {"ok": True, "app_version": "2.0.0"}
        raise AssertionError((target, method, path, payload))

    monkeypatch.setattr(coordinator, "_request", request)
    item = _job("voice@a")["items"][0]
    asyncio.run(coordinator._update_one(coordinator._target("voice@a"), item))

    assert item["status"] == "complete"
    assert item["dependency_convergence"] == 1


def test_ordinary_update_without_capability_still_completes(monkeypatch):
    coordinator = FleetAutoUpdates(FakeMonitor(), FakeHubUpdater(), poll_seconds=0, update_timeout=1)
    started = False

    async def request(target, method, path, payload=None):
        nonlocal started
        if path.endswith("/check"):
            return {"state": "checking"}
        if path.endswith("/status"):
            return {"update_available": True, "state": "succeeded" if started else "idle"}
        if path.endswith("/readiness"):
            return {"idle": True, "reasons": []}
        if path.endswith("/update"):
            started = True
            return {"state": "updating"}
        if path == "/api/health":
            return {"ok": True, "app_version": "2.0.0"}
        raise AssertionError((target, method, path, payload))

    monkeypatch.setattr(coordinator, "_request", request)
    item = _job("voice@a")["items"][0]
    asyncio.run(coordinator._update_one(coordinator._target("voice@a"), item))

    assert item["status"] == "complete"
    assert item["dependency_convergence"] is None


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
    assert [row["id"] for row in rows] == ["hub@local", "voice", "image@b"]


def test_managed_selector_keeps_every_installed_image_and_voice():
    monitor = FakeMonitor()
    monitor.registry = [
        {"id": "image@a", "title": "Image A", "modality": "image",
         "host": "10.0.0.8", "port": 47868, "machine": "a"},
        {"id": "voice@a", "title": "Voice A", "modality": "voice",
         "host": "10.0.0.8", "port": 47870, "machine": "a"},
        {"id": "image@b", "title": "Image B", "modality": "image",
         "host": "10.0.0.9", "port": 47868, "machine": "b"},
        {"id": "voice@b", "title": "Voice B", "modality": "voice",
         "host": "10.0.0.9", "port": 47870, "machine": "b"},
        {"id": "chat@a", "title": "Chat A", "modality": "chat",
         "host": "10.0.0.8", "port": 47869, "machine": "a"},
    ]
    manifest = {"components": {
        "image": {"repository": "theng12/imagestudio-mac", "version": "1.30.1",
                  "commit": "a" * 40, "installed_only": True},
        "voice": {"repository": "theng12/voicestudio-mac", "version": "2.3.0",
                  "commit": "b" * 40, "installed_only": True},
    }}

    targets = fleet_auto_updates.managed_targets(monitor, manifest)

    assert {row["id"] for row in targets} == {"image@a", "voice@a", "image@b", "voice@b"}
    assert {(row["modality"], row["repository"], row["target_version"], row["target_commit"])
            for row in targets} == {
                ("image", "theng12/imagestudio-mac", "1.30.1", "a" * 40),
                ("voice", "theng12/voicestudio-mac", "2.3.0", "b" * 40),
            }


@pytest.mark.asyncio
async def test_managed_component_runner_posts_frozen_tuple_and_attests_exact_health(monkeypatch):
    monitor = FakeMonitor()
    monitor.registry = [{"id": "image@a", "title": "Image A", "modality": "image",
                         "host": "10.0.0.8", "port": 47868, "machine": "a"}]
    manifest = {"components": {
        "image": {"repository": "theng12/imagestudio-mac", "version": "1.30.1",
                  "commit": "a" * 40, "installed_only": True},
        "voice": {"repository": "theng12/voicestudio-mac", "version": "2.3.0",
                  "commit": "b" * 40, "installed_only": True},
    }}
    posted = []

    class Response:
        status_code = 200
        def __init__(self, payload): self.payload = payload
        def json(self): return self.payload
        def raise_for_status(self): return None

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def get(self, url, **kwargs):
            if url.endswith("/api/auto-update/status"):
                return Response({"state": "succeeded",
                                 "capabilities": {"managed_exact_commit": True,
                                                  "dependency_convergence": 1}})
            if url.endswith("/api/health"):
                return Response({"ok": True, "app_version": "1.30.1", "app_commit": "a" * 40})
            raise AssertionError(url)
        async def post(self, url, **kwargs):
            posted.append(kwargs["json"])
            return Response({"state": "updating"})

    monkeypatch.setattr(fleet_auto_updates.httpx, "AsyncClient", lambda **kwargs: Client())
    results = await fleet_auto_updates.run_managed_components(
        monitor, manifest, operation_id="release-12-machine-a", poll_seconds=0, update_timeout=1,
    )

    assert results[0]["state"] == "current"
    assert results[0]["operation_id"]
    assert len(results[0]["operation_id"]) <= 128
    assert posted == [{"after_current": True, "target_commit": "a" * 40,
                       "target_version": "1.30.1", "operation_id": results[0]["operation_id"]}]


@pytest.mark.asyncio
async def test_managed_component_runner_marks_capability_missing_retryable(monkeypatch):
    monitor = FakeMonitor()
    monitor.registry = [{"id": "voice@a", "title": "Voice A", "modality": "voice",
                         "host": "10.0.0.8", "port": 47870, "machine": "a"}]
    manifest = {"components": {
        "image": {"repository": "theng12/imagestudio-mac", "version": "1.30.1",
                  "commit": "a" * 40, "installed_only": True},
        "voice": {"repository": "theng12/voicestudio-mac", "version": "2.3.0",
                  "commit": "b" * 40, "installed_only": True},
    }}
    posts = []

    class Response:
        status_code = 200
        def json(self): return {"state": "idle", "capabilities": {}}
        def raise_for_status(self): return None

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def get(self, *args, **kwargs): return Response()
        async def post(self, *args, **kwargs): posts.append(True)

    monkeypatch.setattr(fleet_auto_updates.httpx, "AsyncClient", lambda **kwargs: Client())
    results = await fleet_auto_updates.run_managed_components(
        monitor, manifest, operation_id="release-12-machine-a", poll_seconds=0, update_timeout=1,
    )

    assert results[0]["state"] == "retryable_failure"
    assert results[0]["next_retry"] is not None
    assert "exact component updater unavailable" in results[0]["detail"]
    assert posts == []


@pytest.mark.asyncio
async def test_managed_component_runner_replays_lost_post_and_rejects_wrong_commit(monkeypatch):
    monitor = FakeMonitor()
    monitor.registry = [{"id": "image@a", "title": "Image A", "modality": "image",
                         "host": "10.0.0.8", "port": 47868, "machine": "a"}]
    manifest = {"components": {
        "image": {"repository": "theng12/imagestudio-mac", "version": "1.30.1",
                  "commit": "a" * 40, "installed_only": True},
        "voice": {"repository": "theng12/voicestudio-mac", "version": "2.3.0",
                  "commit": "b" * 40, "installed_only": True},
    }}
    posts = []

    class Response:
        status_code = 200
        def __init__(self, payload): self.payload = payload
        def json(self): return self.payload
        def raise_for_status(self): return None

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def get(self, url, **kwargs):
            if url.endswith("/api/auto-update/status"):
                return Response({"state": "succeeded",
                                 "capabilities": {"managed_exact_commit": True,
                                                  "dependency_convergence": 1}})
            if url.endswith("/api/health"):
                return Response({"ok": True, "app_version": "1.30.1", "app_commit": "b" * 40})
            raise AssertionError(url)
        async def post(self, url, **kwargs):
            posts.append(kwargs["json"])
            if len(posts) == 1:
                raise httpx.ReadTimeout("response lost after admission")
            return Response({"state": "updating"})

    monkeypatch.setattr(fleet_auto_updates.httpx, "AsyncClient", lambda **kwargs: Client())
    results = await fleet_auto_updates.run_managed_components(
        monitor, manifest, operation_id="release-12-machine-a", poll_seconds=0, update_timeout=1,
    )

    assert posts[0] == posts[1]
    assert results[0]["state"] == "retryable_failure"
    assert results[0]["next_retry"] is not None
    assert "attestation mismatch" in results[0]["detail"]


@pytest.mark.asyncio
async def test_managed_component_runner_preserves_clean_checkout_health_failure(monkeypatch):
    monitor = FakeMonitor()
    monitor.registry = [{"id": "image@a", "title": "Image A", "modality": "image",
                         "host": "10.0.0.8", "port": 47868, "machine": "a"}]
    manifest = {"components": {
        "image": {"repository": "theng12/imagestudio-mac", "version": "1.30.1",
                  "commit": "a" * 40, "installed_only": True},
        "voice": {"repository": "theng12/voicestudio-mac", "version": "2.3.0",
                  "commit": "b" * 40, "installed_only": True},
    }}
    polls = 0

    class Response:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

        def raise_for_status(self):
            return None

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            del args

        async def get(self, url, **kwargs):
            del url, kwargs
            nonlocal polls
            polls += 1
            if polls == 1:
                return Response({
                    "state": "idle",
                    "capabilities": {"managed_exact_commit": True,
                                     "dependency_convergence": 1},
                })
            return Response({
                "state": "failed",
                "capabilities": {"managed_exact_commit": True,
                                 "dependency_convergence": 1},
                "details": [
                    "The updated app did not attest to the expected commit and version."
                ],
            })

        async def post(self, *args, **kwargs):
            del args, kwargs
            return Response({"state": "updating"})

    monkeypatch.setattr(fleet_auto_updates.httpx, "AsyncClient", lambda **kwargs: Client())

    results = await fleet_auto_updates.run_managed_components(
        monitor, manifest, operation_id="release-12-machine-a",
        poll_seconds=0, update_timeout=1,
    )

    assert results[0]["state"] == "retryable_failure"
    assert results[0]["error_code"] == "clean_checkout_health_failure"
    assert "clean checkout health" in results[0]["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code, expected", [(401, "auth_blocked"), (403, "auth_blocked"),
                                                     (404, "retryable_failure"), (409, "retryable_failure")])
async def test_managed_component_http_errors_stay_target_local(monkeypatch, status_code, expected):
    monitor = FakeMonitor()
    monitor.registry = [{"id": "image@a", "title": "Image A", "modality": "image",
                         "host": "10.0.0.8", "port": 47868, "machine": "a"}]
    manifest = {"components": {
        "image": {"repository": "theng12/imagestudio-mac", "version": "1.30.1",
                  "commit": "a" * 40, "installed_only": True},
        "voice": {"repository": "theng12/voicestudio-mac", "version": "2.3.0",
                  "commit": "b" * 40, "installed_only": True},
    }}

    class Response:
        def raise_for_status(self):
            request = httpx.Request("GET", "http://10.0.0.8:47868/api/auto-update/status")
            raise httpx.HTTPStatusError("rejected", request=request,
                                        response=httpx.Response(status_code, request=request))

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def get(self, *args, **kwargs): return Response()

    monkeypatch.setattr(fleet_auto_updates.httpx, "AsyncClient", lambda **kwargs: Client())
    results = await fleet_auto_updates.run_managed_components(
        monitor, manifest, operation_id="release-12-machine-a", poll_seconds=0, update_timeout=1,
    )

    assert results[0]["state"] == expected
    assert results[0]["next_retry"] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", [[], "bad", None])
async def test_managed_component_malformed_json_is_retryable_and_later_target_runs(monkeypatch, malformed):
    monitor = FakeMonitor()
    monitor.registry = [
        {"id": "image@a", "title": "Image A", "modality": "image",
         "host": "10.0.0.8", "port": 47868, "machine": "a"},
        {"id": "voice@a", "title": "Voice A", "modality": "voice",
         "host": "10.0.0.8", "port": 47870, "machine": "a"},
    ]
    manifest = {"components": {
        "image": {"repository": "theng12/imagestudio-mac", "version": "1.30.1",
                  "commit": "a" * 40, "installed_only": True},
        "voice": {"repository": "theng12/voicestudio-mac", "version": "2.3.0",
                  "commit": "b" * 40, "installed_only": True},
    }}

    class Response:
        status_code = 200
        def __init__(self, payload): self.payload = payload
        def json(self): return self.payload
        def raise_for_status(self): return None

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def get(self, url, **kwargs):
            if ":47868/" in url:
                return Response(malformed)
            if url.endswith("/api/auto-update/status"):
                return Response({"state": "succeeded",
                                 "capabilities": {"managed_exact_commit": True,
                                                  "dependency_convergence": 1}})
            if url.endswith("/api/health"):
                return Response({"ok": True, "app_version": "2.3.0", "app_commit": "b" * 40})
            raise AssertionError(url)
        async def post(self, url, **kwargs): return Response({"state": "updating"})

    monkeypatch.setattr(fleet_auto_updates.httpx, "AsyncClient", lambda **kwargs: Client())
    results = await fleet_auto_updates.run_managed_components(
        monitor, manifest, operation_id="release-12-machine-a", poll_seconds=0, update_timeout=1,
    )

    assert [result["state"] for result in results] == ["retryable_failure", "current"]


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
