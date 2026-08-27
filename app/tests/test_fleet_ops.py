import asyncio
import time

import httpx
import pytest

from backend import broker, fleet_ops, peers


def test_catalog_and_diagnostic_summaries():
    total, ready = fleet_ops._downloaded({"models": [
        {"cache": {"state": "cached"}}, {"cache": {"state": "absent"}}, {"is_cloud": True}
    ]})
    assert (total, ready) == (3, 1)
    assert fleet_ops._diag_state({"available": False}) == "warn"
    assert fleet_ops._diag_state({"available": True}) == "pass"


def test_published_version_repositories_cover_every_studio_family():
    assert set(fleet_ops.PUBLISHED_REPOSITORIES) == {
        "hub", "voice", "chat", "image", "music", "video", "render"}


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


def test_version_status_requires_a_real_published_comparison():
    row = {"version": "1.2.3"}
    fleet_ops._apply_version_status(row, {"app_version": "1.2.3",
                                          "latest_version": None,
                                          "update_available": False})
    assert row["version_status"] == "unknown"
    assert row["update_available"] is None
    assert "could not be verified" in row["version_detail"]

    fleet_ops._apply_version_status(row, {"app_version": "1.2.3.build7",
                                          "latest_version": "1.2.3",
                                          "update_available": False})
    assert row["version_status"] == "current"
    assert row["update_available"] is False
    assert "matches latest published" in row["version_detail"]

    fleet_ops._apply_version_status(row, {"app_version": "1.2.3",
                                          "latest_version": "1.3.0",
                                          "update_available": True})
    assert row["version_status"] == "update_available"
    assert row["update_available"] is True
    assert row["latest_version"] == "1.3.0"


@pytest.mark.asyncio
async def test_studio_version_scan_only_reads_version_endpoints(monkeypatch, monitor):
    studio = {**monitor.registry[0], "id": "image@mac-a", "machine": "mac-a",
              "host": "10.0.0.8"}
    monitor.registry = [studio]
    requested = []

    class Response:
        status_code = 200
        def __init__(self, payload): self.payload = payload
        def json(self): return self.payload
        def raise_for_status(self): return None

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def get(self, url, **kwargs):
            requested.append(url)
            if url.endswith("/api/version"):
                return Response({"app_version": "1.2.0"})
            if url.endswith("/api/update-status"):
                return Response({"app_version": "1.2.0", "latest_version": "1.3.0"})
            raise AssertionError(f"unexpected preflight request: {url}")

    async def published(*, force=False):
        fleet_ops._published_versions["image"] = "1.3.0"
        return fleet_ops.published_version_snapshot()

    monkeypatch.setattr(fleet_ops.httpx, "AsyncClient", lambda **kwargs: Client())
    monkeypatch.setattr(fleet_ops, "refresh_published_versions", published)

    result = await fleet_ops.scan_studio_versions(monitor)

    row = result["studios"][0]
    assert row["reachable"] is True
    assert row["version"] == "1.2.0"
    assert row["latest_version"] == "1.3.0"
    assert row["update_available"] is True
    assert all(url.endswith(("/api/version", "/api/update-status")) for url in requested)


def test_saved_studio_version_snapshot_hides_legacy_rows(reset, monitor):
    fleet_ops._studio_versions = {
        "checked_at": 123,
        "studios": [
            {"id": "image", "modality": "image", "machine": "local"},
            {"id": "chat", "modality": "chat", "machine": "local"},
            {"id": "render@old", "modality": "render", "machine": "old"},
        ],
    }

    snapshot = fleet_ops.studio_versions_snapshot(monitor)

    assert [row["id"] for row in snapshot["studios"]] == ["image"]


def test_local_published_version_is_applied_to_every_worker_of_the_same_app():
    local = {"id": "voice", "modality": "voice", "machine": "local",
             "version": "1.20.3", "latest_version": "1.20.3",
             "checks": [{"name": "version", "status": "pass", "detail": "old"}]}
    remote = {"id": "voice@mac-a", "modality": "voice", "machine": "mac-a",
              "version": "1.20.2", "latest_version": "1.20.2",
              "checks": [{"name": "version", "status": "pass", "detail": "old"}]}

    fleet_ops._apply_canonical_published_versions([local, remote])

    assert remote["latest_version"] == "1.20.3"
    assert remote["version_status"] == "update_available"
    assert remote["update_available"] is True
    assert remote["checks"][-1]["status"] == "warn"
    assert "latest published v1.20.3" in remote["checks"][-1]["detail"]


def test_github_published_version_overrides_every_stale_worker_cache():
    local = {"id": "voice", "modality": "voice", "machine": "local",
             "version": "1.20.3", "latest_version": "1.20.3",
             "checks": [{"name": "version", "status": "pass", "detail": "old"}],
             "status": "pass"}
    remote = {"id": "voice@mac-a", "modality": "voice", "machine": "mac-a",
              "version": "1.20.2", "latest_version": "1.20.2",
              "checks": [{"name": "version", "status": "pass", "detail": "old"}],
              "status": "pass"}

    fleet_ops._apply_canonical_published_versions(
        [local, remote], {"voice": "1.20.4"})

    assert local["latest_version"] == "1.20.4"
    assert remote["latest_version"] == "1.20.4"
    assert local["update_available"] is True
    assert remote["update_available"] is True
    assert local["status"] == "warn"
    assert remote["status"] == "warn"


@pytest.mark.asyncio
async def test_local_studio_update_repair_uses_trusted_runner_and_releases_maintenance(
    monkeypatch,
):
    studio = {
        "id": "voice", "app": "voicestudio-mac", "modality": "voice",
        "machine": "local", "host": "127.0.0.1",
    }
    item = {"studio": "voice", "machine": "local", "status": "queued", "detail": "waiting"}
    monkeypatch.setattr(fleet_ops, "studio_has_active_work", lambda _sid: False)
    monkeypatch.setattr(
        fleet_ops, "run_studio_update_repair_sync",
        lambda value: {
            "ok": True, "studio": value["id"], "status": "migrated",
            "detail": "machine settings preserved; update and dependencies verified",
        },
    )

    await fleet_ops._studio_update_repair_one(studio, item)

    assert item["status"] == "complete"
    assert "dependencies verified" in item["detail"]
    assert "voice" not in broker._maintenance


@pytest.mark.asyncio
async def test_studio_update_repairs_are_serial_per_mac_and_parallel_across_macs(monkeypatch):
    studios = [
        {"id": "voice", "modality": "voice", "machine": "local"},
        {"id": "image", "modality": "image", "machine": "local"},
        {"id": "voice@mac-b", "modality": "voice", "machine": "mac-b"},
        {"id": "image@mac-b", "modality": "image", "machine": "mac-b"},
    ]
    monitor = type("Monitor", (), {"registry": studios})()
    active = {"local": 0, "mac-b": 0}
    max_per_machine = {"local": 0, "mac-b": 0}
    global_active = 0
    global_max = 0

    async def repair_one(studio, item):
        nonlocal global_active, global_max
        machine = studio["machine"]
        active[machine] += 1
        global_active += 1
        max_per_machine[machine] = max(max_per_machine[machine], active[machine])
        global_max = max(global_max, global_active)
        await asyncio.sleep(0.02)
        item.update(status="complete", detail="verified", finished_at=time.time())
        active[machine] -= 1
        global_active -= 1

    monkeypatch.setattr(fleet_ops, "_studio_update_repair_one", repair_one)
    job = {
        "id": "repair-groups", "kind": "studio_update_repair", "status": "queued",
        "created_at": time.time(), "finished_at": None,
        "items": [{
            "studio": studio["id"], "machine": studio["machine"],
            "modality": studio["modality"], "status": "queued", "detail": "waiting",
        } for studio in studios],
    }

    await fleet_ops._run_studio_update_repairs(monitor, job)

    assert max_per_machine == {"local": 1, "mac-b": 1}
    assert global_max == 2
    assert job["status"] == "complete"


@pytest.mark.asyncio
async def test_unreachable_remote_update_repair_stays_pending_and_retryable(monkeypatch):
    studio = {"id": "voice@mac-b", "modality": "voice", "machine": "mac-b"}
    monitor = type("Monitor", (), {"registry": [studio]})()

    async def pending(_studio, _item):
        raise fleet_ops.StudioUpdateRepairPending("Agent Hub is offline; retry when reachable")

    monkeypatch.setattr(fleet_ops, "_studio_update_repair_one", pending)
    job = {
        "id": "repair-pending", "kind": "studio_update_repair", "status": "queued",
        "created_at": time.time(), "finished_at": None,
        "items": [{"studio": studio["id"], "machine": "mac-b", "modality": "voice",
                   "status": "queued", "detail": "waiting"}],
    }

    await fleet_ops._run_studio_update_repairs(monitor, job)

    assert job["status"] == "pending"
    assert job["items"][0]["status"] == "pending"
    assert job["retryable_count"] == 1


def test_retry_studio_update_repair_targets_only_pending_and_failed(monkeypatch, reset):
    fleet_ops._studio_update_repairs["old"] = {
        "id": "old", "status": "pending", "items": [
            {"studio": "voice@mac-b", "status": "pending"},
            {"studio": "image@mac-b", "status": "failed"},
            {"studio": "voice@mac-c", "status": "complete"},
        ],
    }
    called = []
    monkeypatch.setattr(
        fleet_ops, "start_studio_update_repairs",
        lambda monitor, ids, local_only=False: called.append((monitor, ids, local_only)) or {"id": "new"},
    )
    monitor = object()

    result = fleet_ops.retry_studio_update_repairs(monitor, "old")

    assert result == {"id": "new"}
    assert called == [(monitor, ["voice@mac-b", "image@mac-b"], False)]


def test_active_update_repair_blocks_hub_self_update(reset):
    fleet_ops._studio_update_repairs["active"] = {"status": "running", "items": []}
    assert "a Studio update repair is active" in fleet_ops.hub_update_blockers()


def test_studio_update_repair_refuses_competing_maintenance(reset):
    monitor = type("Monitor", (), {"registry": [
        {"id": "voice", "modality": "voice", "machine": "local"},
    ]})()
    fleet_ops._generation_installs["generation"] = {"status": "running", "items": []}

    with pytest.raises(ValueError, match="generation dependency install"):
        fleet_ops.start_studio_update_repairs(monitor, ["voice"])


@pytest.mark.asyncio
async def test_github_refresh_is_cache_busted_and_retains_last_known_on_error(monkeypatch):
    requested = []

    class Response:
        def __init__(self, text, *, fail=False):
            self.text = text
            self.content = text.encode()
            self.fail = fail

        def raise_for_status(self):
            if self.fail:
                raise fleet_ops.httpx.ConnectError("offline")

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None

        async def get(self, url, params=None):
            requested.append((url, params))
            if ".git/info/refs" in url:
                return Response(f"003d{'a' * 40} refs/heads/main\n")
            if "voicestudio" in url:
                return Response("", fail=True)
            return Response("9.8.7\n")

    monkeypatch.setattr(fleet_ops.httpx, "AsyncClient", lambda **kwargs: Client())
    monkeypatch.setattr(fleet_ops, "_published_versions", {"voice": "1.20.4"})
    monkeypatch.setattr(fleet_ops, "_published_refs", {})
    monkeypatch.setattr(fleet_ops, "_published_checked_at", 0.0)
    monkeypatch.setattr(fleet_ops, "_published_errors", {})
    monkeypatch.setattr(fleet_ops, "_published_lock", None)

    result = await fleet_ops.refresh_published_versions(force=True)

    assert result["versions"]["voice"] == "1.20.4"
    assert result["versions"]["hub"] == "9.8.7"
    assert "voice" in result["errors"]
    ref_requests = [(url, params) for url, params in requested if ".git/info/refs" in url]
    assert len(ref_requests) == len(fleet_ops.PUBLISHED_REPOSITORIES)
    assert all(params and params.get("service") == "git-upload-pack"
               for _, params in ref_requests)
    assert all("/" + "a" * 40 + "/VERSION" in url
               for url, _ in requested if "raw.githubusercontent.com" in url)


@pytest.mark.asyncio
async def test_preflight_uses_remote_studio_update_contract(monkeypatch, monitor):
    studio = {**monitor.registry[0], "id": "image@mac-a", "machine": "mac-a",
              "host": "10.0.0.8"}
    monitor.registry = [studio]
    monitor.status[studio["id"]] = {"status": "up"}

    class Resp:
        status_code = 200

        def __init__(self, payload): self.payload = payload
        def json(self): return self.payload
        def raise_for_status(self): return None

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def get(self, url, **kwargs):
            if url.endswith("/api/version"):
                return Resp({"app_version": "1.2.0"})
            if url.endswith("/api/update-status"):
                return Resp({"app_version": "1.2.0", "latest_version": "1.3.0",
                             "update_available": True})
            if url.endswith("/api/capabilities"):
                return Resp({"schema_version": 1, "studio": {"modality": "image"},
                             "operations": ["txt2img"]})
            if url.endswith("/api/catalog"):
                return Resp({"models": [{"is_cloud": True}]})
            raise AssertionError(url)

    monkeypatch.setattr(fleet_ops.httpx, "AsyncClient", lambda **kwargs: Client())
    monkeypatch.setattr(fleet_ops.peers, "cached", lambda machine: None)
    row = await fleet_ops._preflight_one(monitor, studio)
    assert row["machine"] == "mac-a"
    assert row["version"] == "1.2.0"
    assert row["latest_version"] == "1.3.0"
    assert row["version_status"] == "update_available"
    assert row["update_available"] is True


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

    chat = {"id": "chat", "title": "Chat Studio KH", "modality": "chat",
            "machine": "local", "host": "127.0.0.1", "port": 47871}
    monitor.registry.append(chat)
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


@pytest.mark.asyncio
async def test_app_pause_drains_chat_and_transcription_independently(
        reset, monitor, monkeypatch):
    from backend import chat_jobs, registry, transcription_jobs

    chat = {"id": "chat", "title": "Chat Studio KH", "modality": "chat",
            "machine": "local", "host": "127.0.0.1", "port": 47871}
    monitor.registry.append(chat)
    voice = next(s for s in monitor.registry if s["id"] == "voice")
    monitor.status["chat"] = {"status": "up"}
    monitor.status["voice"] = {"status": "up"}

    async def chat_catalog(studio):
        return {"models": [{"repo": "chat/model", "cache": {"state": "cached"}}]}

    async def transcription_catalog(studio):
        return {"available": True, "models": [{"repo": "voice/model", "cached": True}]}

    monkeypatch.setattr(monitor, "get_catalog", chat_catalog)
    monkeypatch.setattr(monitor, "get_transcription", transcription_catalog)
    registry.set_studio_enabled("local", "chat", False)

    assert await chat_jobs._eligible_studios(monitor, "chat/model") == []
    assert voice in await transcription_jobs._eligible_studios(monitor, "voice/model")

    registry.set_studio_enabled("local", "chat", True)
    registry.set_studio_enabled("local", "voice", False)
    assert chat in await chat_jobs._eligible_studios(monitor, "chat/model")
    assert await transcription_jobs._eligible_studios(monitor, "voice/model") == []


def test_rolling_update_waits_for_every_queue_type(reset):
    from backend import chat_jobs, transcription_jobs

    broker._busy.add("image")
    chat_jobs.busy_studios.add("chat")
    transcription_jobs.busy_studios.add("voice")
    assert fleet_ops._active_studio_leases() == {"image", "chat", "voice"}


@pytest.mark.asyncio
async def test_updates_are_sequential_and_failure_is_contained(monkeypatch, monitor):
    calls = []
    refreshed = []

    async def fake_update(mon, studio, item):
        calls.append(studio["id"])
        if studio["id"] == "image":
            raise RuntimeError("install failed")
        item.update(status="complete", detail="healthy")

    monkeypatch.setattr(fleet_ops, "_update_one", fake_update)
    async def fake_version_scan(mon):
        refreshed.append(True)
        return {"checked_at": 1, "studios": []}
    monkeypatch.setattr(fleet_ops, "scan_studio_versions", fake_version_scan)
    job = {"id": "x", "status": "queued", "created_at": 0, "finished_at": None,
           "items": [{"studio": "image", "status": "queued", "detail": ""},
                     {"studio": "voice", "status": "queued", "detail": ""}]}
    await fleet_ops._run_updates(monitor, job)
    assert calls == ["image", "voice"]
    assert job["status"] == "complete"
    assert job["degraded"] is True
    assert job["failed_count"] == 1
    assert job["succeeded_count"] == 1
    assert job["items"][0]["status"] == "failed"
    assert job["items"][1]["status"] == "complete"
    assert refreshed == [True]


def test_rollout_fails_only_when_every_target_fails():
    partial = {"items": [{"status": "complete"}, {"status": "failed"}]}
    fleet_ops.finish_fleet_job(partial)
    assert partial["status"] == "complete" and partial["degraded"] is True

    unavailable = {"items": [{"status": "failed"}, {"status": "failed"}]}
    fleet_ops.finish_fleet_job(unavailable)
    assert unavailable["status"] == "failed" and unavailable["degraded"] is False


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
                {"status": "complete", "detail": "healthy on v2.0.0",
                 "from_version": "1.0.0", "expected_version": "2.0.0"}
            ]})

    client = Client()
    monkeypatch.setattr(fleet_ops.httpx, "AsyncClient", lambda **kwargs: client)

    async def no_sleep(seconds): return None
    monkeypatch.setattr(fleet_ops.asyncio, "sleep", no_sleep)

    await fleet_ops._update_remote(studio, item)
    assert client.get_calls == 2
    assert item["status"] == "complete" and item["detail"] == "healthy on v2.0.0"
    assert item["from_version"] == "1.0.0" and item["expected_version"] == "2.0.0"


@pytest.mark.asyncio
async def test_remote_update_stops_blocking_queue_after_prolonged_silence(monkeypatch):
    studio = {"id": "chat@mac-a", "modality": "chat", "machine": "mac-a",
              "host": "10.0.0.8", "hub_port": 47873}

    class Response:
        status_code = 200
        def json(self): return {"id": "remote-job"}

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def post(self, *args, **kwargs): return Response()
        async def get(self, *args, **kwargs):
            raise fleet_ops.httpx.ConnectTimeout("Mac disappeared")

    monkeypatch.setattr(fleet_ops.httpx, "AsyncClient", lambda **kwargs: Client())
    monkeypatch.setattr(fleet_ops, "REMOTE_STATUS_SILENCE_TIMEOUT", 0)

    async def no_sleep(seconds): return None
    monkeypatch.setattr(fleet_ops.asyncio, "sleep", no_sleep)

    with pytest.raises(RuntimeError, match="maintenance grace period"):
        await fleet_ops._update_remote(studio, {"studio": studio["id"]})


@pytest.mark.asyncio
async def test_local_update_accepts_null_monitor_health(monkeypatch, monitor):
    studio = dict(monitor.registry[0])
    studio["modality"] = "voice"
    monitor.status[studio["id"]] = {"app_version": None, "health": None}
    observed = {}

    monkeypatch.setattr(fleet_ops, "_published_versions", {"voice": "1.2.3"})
    monkeypatch.setattr(fleet_ops, "_published_refs", {"voice": "a" * 40})

    async def managed(target, **kwargs):
        observed.update(target)
        return {"state": "current", "detail": "healthy on exact v1.2.3",
                "target_version": "1.2.3"}

    monkeypatch.setattr(fleet_ops.fleet_auto_updates, "run_managed_component", managed)

    item = {"studio": studio["id"], "status": "queued", "detail": "waiting"}
    await fleet_ops._update_one(monitor, studio, item)

    assert item["from_version"] is None
    assert item["expected_version"] == "1.2.3"
    assert observed["target_commit"] == "a" * 40
    assert item["status"] == "complete"


@pytest.mark.asyncio
async def test_local_update_skips_when_running_release_is_already_current(
        monkeypatch, monitor):
    studio = dict(monitor.registry[0])
    studio["modality"] = "voice"
    monitor.status[studio["id"]] = {
        "app_version": "1.68.1",
        "health": {"app_version": "1.68.1", "app_commit": "a" * 40},
    }
    calls = []

    monkeypatch.setattr(fleet_ops, "_published_versions", {"voice": "1.68.1"})
    monkeypatch.setattr(fleet_ops, "_published_refs", {"voice": "a" * 40})
    monkeypatch.setattr(
        fleet_ops.fleet_auto_updates, "run_managed_component",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    item = {"studio": studio["id"], "status": "queued", "detail": "waiting"}
    await fleet_ops._update_one(monitor, studio, item)

    assert calls == []
    assert item["status"] == "complete"
    assert item["detail"] == "already current on v1.68.1"


@pytest.mark.asyncio
async def test_local_update_does_not_skip_stale_checked_out_release(
        monkeypatch, monitor):
    studio = dict(monitor.registry[0])
    studio["modality"] = "voice"
    monitor.status[studio["id"]] = {
        "app_version": "1.68.1",
        "health": {"app_version": "1.68.1"},
    }
    calls = []

    monkeypatch.setattr(fleet_ops, "_published_versions", {"voice": "1.68.2"})
    monkeypatch.setattr(fleet_ops, "_published_refs", {"voice": "b" * 40})

    async def managed(target, **kwargs):
        calls.append(target)
        return {"state": "current", "detail": "healthy on exact v1.68.2",
                "target_version": "1.68.2"}

    monkeypatch.setattr(fleet_ops.fleet_auto_updates, "run_managed_component", managed)
    item = {"studio": studio["id"], "status": "queued", "detail": "waiting"}
    await fleet_ops._update_one(monitor, studio, item)

    assert calls
    assert item["status"] == "complete"


@pytest.mark.asyncio
async def test_remote_update_surfaces_peer_conflict_detail(monkeypatch):
    studio = {"id": "render@mac-a", "modality": "render", "machine": "mac-a",
              "host": "10.0.0.8", "hub_port": 47873}

    class Response:
        status_code = 409
        text = ""
        def json(self): return {"detail": "an update job is already running"}

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def post(self, *args, **kwargs): return Response()

    monkeypatch.setattr(fleet_ops.httpx, "AsyncClient", lambda **kwargs: Client())
    with pytest.raises(RuntimeError, match="already running"):
        await fleet_ops._update_remote(studio, {"studio": studio["id"]})


def test_start_hub_updates_requires_remote_machines(monitor):
    monitor.registry = [s for s in monitor.registry if s.get("machine", "local") == "local"]
    fleet_ops._hub_updates.clear()
    with pytest.raises(ValueError, match="no remote"):
        fleet_ops.start_hub_updates(monitor, "1.0.0", "a" * 40, None)


def test_hub_version_snapshot_includes_peer_hardware(monitor):
    monitor.registry.append({"id": "image@mac-b", "modality": "image",
                             "host": "10.0.0.9", "port": 47868, "machine": "mac-b"})
    fleet_ops._hub_versions["mac-b"] = {"version": "1.0.0", "reachable": True}
    peers._cache["mac-b"] = (time.time(), {
        "reachable": True,
        "host": {"chip": "Apple M4", "total_gb": 24.0},
        "studios": {},
    })

    row = fleet_ops.hub_versions_snapshot(monitor)["mac-b"]

    assert row["chip"] == "Apple M4"
    assert row["total_memory_gb"] == 24.0


@pytest.mark.asyncio
async def test_start_hub_updates_builds_job(monkeypatch, monitor):
    monitor.registry.append({"id": "image@mac-b", "modality": "image",
                             "host": "10.0.0.9", "port": 47868, "machine": "mac-b"})
    fleet_ops._hub_updates.clear()

    async def _noop(job):
        return None
    monkeypatch.setattr(fleet_ops, "_run_hub_updates", _noop)

    commit = "a" * 40
    job = fleet_ops.start_hub_updates(monitor, "9.9.9", commit, None)
    assert job["kind"] == "hub" and job["latest"] == "9.9.9"
    assert job["latest_commit"] == commit
    assert all(item["operation_id"].startswith("fleet-hub-") for item in job["items"])
    assert any(i["machine"] == "mac-b" and i["host"] == "10.0.0.9" for i in job["items"])

    fleet_ops._hub_updates.clear()
    with pytest.raises(ValueError, match="unknown"):
        fleet_ops.start_hub_updates(monitor, "9.9.9", commit, ["does-not-exist"])
    fleet_ops._hub_updates.clear()


@pytest.mark.asyncio
async def test_agent_hub_updates_run_canary_first_then_continue_after_failure(monkeypatch):
    active = 0
    max_active = 0
    order = []

    async def update_one(item, latest, latest_commit):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        order.append(item["machine"])
        try:
            await asyncio.sleep(0)
            if item["machine"] == "mac-a":
                raise RuntimeError("unexpected restart poll failure")
            item.update(status="complete", detail=f"healthy on v{latest}")
        finally:
            active -= 1

    monkeypatch.setattr(fleet_ops, "_update_hub_one", update_one)
    job = {
        "id": "rolling-1",
        "status": "queued",
        "latest": "2.6.2",
        "items": [
            {"machine": "mac-a", "status": "queued"},
            {"machine": "mac-b", "status": "queued"},
            {"machine": "mac-c", "status": "queued"},
        ],
    }

    await fleet_ops._run_hub_updates(job)

    assert order == ["mac-a", "mac-b", "mac-c"]
    assert max_active == 1
    assert job["items"][0]["status"] == "failed"
    assert "unexpected restart poll failure" in job["items"][0]["detail"]
    assert [item["status"] for item in job["items"][1:]] == ["complete", "complete"]
    assert job["status"] == "complete"
    assert job["degraded"] is True


@pytest.mark.asyncio
async def test_slow_hub_version_check_retries_before_failing(monkeypatch, reset):
    item = {"machine": "mac-slow", "host": "10.0.0.9", "status": "queued",
            "detail": "waiting", "from_version": None, "to_version": None}

    class Response:
        status_code = 200
        def json(self): return {"app_version": "2.0.0", "app_commit": "b" * 40}
        def raise_for_status(self): return None

    class Client:
        get_calls = 0
        post_calls = 0
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def get(self, *args, **kwargs):
            self.get_calls += 1
            if self.get_calls < 3:
                raise httpx.ReadTimeout("slow Mac")
            return Response()
        async def post(self, *args, **kwargs):
            self.post_calls += 1
            return Response()

    client = Client()
    monkeypatch.setattr(fleet_ops.httpx, "AsyncClient", lambda **kwargs: client)

    async def no_sleep(seconds): return None
    monkeypatch.setattr(fleet_ops.asyncio, "sleep", no_sleep)

    await fleet_ops._update_hub_one(item, "2.0.0", "b" * 40)
    assert client.get_calls == 3
    assert client.post_calls == 0
    assert item["status"] == "current"


@pytest.mark.asyncio
async def test_agent_hub_update_uses_durable_exact_updater_and_verifies_dependencies(
        monkeypatch, reset):
    target_commit = "b" * 40
    item = {
        "machine": "mac-a", "host": "10.0.0.8", "status": "queued",
        "detail": "waiting", "from_version": None, "to_version": None,
        "operation_id": "fleet-hub-test-mac-a",
    }
    calls = []
    statuses = iter([
        {"state": "succeeded", "capabilities": {
            "managed_exact_commit": True, "dependency_convergence": 1,
        }, "managed_operation_history": [{
            "operation_id": "fleet-hub-test-mac-a", "result": "failed",
        }]},
        {"state": "deferred", "defer_reason": "active generation job"},
        {"state": "restarting"},
        {"state": "succeeded", "details": ["Dependencies installed."]},
    ])

    class Response:
        status_code = 200
        def __init__(self, payload): self._payload = payload
        def json(self): return self._payload
        def raise_for_status(self): return None

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def get(self, url, **kwargs):
            calls.append(("GET", url, kwargs.get("headers"), None))
            if url.endswith("/api/version"):
                return Response({"app_version": "2.13.0", "app_commit": "a" * 40})
            if url.endswith("/api/auto-update/status"):
                return Response(next(statuses))
            if url.endswith("/api/health"):
                return Response({"ok": True, "app_version": "2.13.2",
                                 "app_commit": target_commit})
            raise AssertionError(url)
        async def post(self, url, **kwargs):
            calls.append(("POST", url, kwargs.get("headers"), kwargs.get("json")))
            return Response({"state": "updating"})

    monkeypatch.setattr(fleet_ops.httpx, "AsyncClient", lambda **kwargs: Client())
    async def no_sleep(seconds): return None
    monkeypatch.setattr(fleet_ops.asyncio, "sleep", no_sleep)

    await fleet_ops._update_hub_one(item, "2.13.2", target_commit)

    post = next(call for call in calls if call[0] == "POST")
    assert post[1].endswith("/api/auto-update/update")
    assert not any(call[1].endswith("/api/hub/maintenance/self-update") for call in calls)
    assert post[3]["after_current"] is False
    assert post[3]["target_commit"] == target_commit
    assert post[3]["target_version"] == "2.13.2"
    assert post[3]["operation_id"].startswith("fleet-hub-")
    assert post[3]["operation_id"] != "fleet-hub-test-mac-a"
    assert item["operation_id"] == post[3]["operation_id"]
    assert item["status"] == "complete"
    assert item["to_version"] == "2.13.2"
    assert item["dependency_convergence"] == 1
    assert "dependencies" in item["detail"].lower()


@pytest.mark.asyncio
async def test_agent_hub_update_refuses_updater_without_dependency_convergence(
        monkeypatch, reset):
    item = {
        "machine": "mac-old", "host": "10.0.0.9", "status": "queued",
        "detail": "waiting", "from_version": None, "to_version": None,
        "operation_id": "fleet-hub-test-old",
    }

    class Response:
        status_code = 200
        def __init__(self, payload): self._payload = payload
        def json(self): return self._payload
        def raise_for_status(self): return None

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def get(self, url, **kwargs):
            if url.endswith("/api/version"):
                return Response({"app_version": "1.0.0", "app_commit": "a" * 40})
            return Response({"state": "succeeded", "capabilities": {
                "managed_exact_commit": True,
            }})
        async def post(self, *args, **kwargs):
            raise AssertionError("unsafe updater must not be started")

    monkeypatch.setattr(fleet_ops.httpx, "AsyncClient", lambda **kwargs: Client())

    await fleet_ops._update_hub_one(item, "2.13.2", "b" * 40)

    assert item["status"] == "failed"
    assert "dependency convergence" in item["detail"].lower()


def test_interrupted_remote_jobs_survive_restart_as_retryable_history(monkeypatch, tmp_path, reset):
    state_file = tmp_path / "fleet_versions.json"
    monkeypatch.setattr(fleet_ops, "_STATE_FILE", state_file)
    fleet_ops._hub_updates["job-1"] = {
        "id": "job-1", "status": "running", "created_at": time.time(),
        "items": [{"machine": "mac-a", "status": "restarting", "detail": "waiting"}],
    }
    fleet_ops._save_state()
    fleet_ops._hub_updates.clear()

    fleet_ops._load_state()

    restored = fleet_ops._hub_updates["job-1"]
    assert restored["status"] == "failed"
    assert restored["restart_interrupted"] is True
    assert "retry remotely" in restored["items"][0]["detail"]


def test_legacy_fleet_ops_restart_history_is_not_managed_desired_state(
        tmp_path, monkeypatch, reset):
    monkeypatch.setattr(fleet_ops, "_STATE_FILE", tmp_path / "fleet_versions.json")
    fleet_ops._hub_updates["legacy"] = {
        "id": "legacy", "status": "running",
        "items": [{"status": "restarting"}],
    }
    fleet_ops._save_state(); fleet_ops._hub_updates.clear(); fleet_ops._load_state()
    assert fleet_ops._hub_updates["legacy"]["restart_interrupted"] is True
    assert fleet_ops._hub_updates["legacy"]["status"] == "failed"


def test_self_update_endpoint_requires_auth(client):
    # non-loopback without the token → blocked before the handler runs
    assert client.post("/api/hub/maintenance/self-update").status_code == 401
    assert client.post("/api/hub/maintenance/hub-updates", json={}).status_code == 401


def test_automatic_hub_update_ignores_cancelled_batch_leftovers(reset):
    from backend import broker, fleet_ops

    broker.batches["old"] = {
        "cancelled": True, "items": [{"state": "queued"}],
    }
    assert "a generation batch is queued or running" not in fleet_ops.hub_update_blockers()

    broker.batches["active"] = {
        "cancelled": False, "items": [{"state": "queued"}],
    }
    assert "a generation batch is queued or running" in fleet_ops.hub_update_blockers()


@pytest.mark.asyncio
async def test_preflight_401_is_warning_not_block(monkeypatch, monitor):
    import httpx as _httpx
    studio = {**monitor.registry[0], "id": "image@mac-a", "machine": "mac-a",
              "host": "10.0.0.8"}
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
            if url.endswith("/api/update-status"):
                return Resp(200, {"app_version": "9.9.9",
                                  "latest_version": "9.9.9",
                                  "update_available": False})
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
