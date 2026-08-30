import asyncio
import threading
from pathlib import Path

import pytest

from backend import monitor as mon


def test_catalog_observation_runtime_state_is_ignored_by_git() -> None:
    root = Path(__file__).parents[2]
    ignored = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "catalog_observations.json" in ignored


def test_is_cached_semantics():
    # the exact bug that shipped once: a dict is truthy even when 'absent'
    assert mon.is_cached({"cache": {"state": "cached"}}) is True
    assert mon.is_cached({"cache": {"state": "absent"}}) is False
    assert mon.is_cached({"cache": {"state": "partial"}}) is False
    assert mon.is_cached({"cache": None}) is False
    assert mon.is_cached({}) is False
    assert mon.is_cached({"cache": True}) is True  # tolerate a bool


@pytest.mark.asyncio
async def test_active_chat_lease_suppresses_false_health_flap(reset, monitor, monkeypatch):
    from backend import alerts, chat_jobs

    studio = {"id": "chat", "title": "Chat Studio KH", "modality": "chat",
              "machine": "local", "host": "127.0.0.1", "port": 47871}
    monitor.registry.append(studio)
    monitor.status["chat"] = {
        "status": "up", "last_seen": 10, "last_checked": 10,
        "app_version": "1.0.0", "health": {"ok": True},
    }

    async def timeout(*args, **kwargs):
        raise RuntimeError("inference is blocking health")

    monkeypatch.setattr(monitor._client, "get", timeout)
    chat_jobs.busy_studios.add("chat")
    await monitor._poll_one(studio)
    assert monitor.status["chat"]["status"] == "up"
    assert monitor.status["chat"]["health_busy"] is True
    assert not any(event["kind"] == "studio_down" for event in alerts.recent(20))

    chat_jobs.busy_studios.discard("chat")
    await monitor._poll_one(studio)
    assert monitor.status["chat"]["status"] == "up"
    assert monitor.status["chat"]["health_probe_degraded"] is True
    await monitor._poll_one(studio)
    assert monitor.status["chat"]["status"] == "up"
    await monitor._poll_one(studio)
    assert monitor.status["chat"]["status"] == "down"
    assert any(event["kind"] == "studio_down" for event in alerts.recent(20))


@pytest.mark.asyncio
async def test_down_studio_needs_two_good_probes_to_rejoin(monitor, monkeypatch):
    studio = next(row for row in monitor.registry if row["id"] == "image")
    monitor.status["image"] = {
        "status": "down", "last_seen": 10, "last_checked": 20,
        "consecutive_failures": 3, "consecutive_successes": 0,
    }

    class Response:
        def json(self):
            return {"ok": True, "app_version": "1.2.3"}

    async def healthy(*args, **kwargs):
        return Response()

    monkeypatch.setattr(monitor._client, "get", healthy)
    await monitor._poll_one(studio)
    assert monitor.status["image"]["status"] == "down"
    assert monitor.status["image"]["health_recovering"] is True
    await monitor._poll_one(studio)
    assert monitor.status["image"]["status"] == "up"
    assert monitor.status["image"]["health_recovering"] is False


@pytest.mark.asyncio
async def test_activity_404_is_compatible_and_does_not_degrade_health(monitor, monkeypatch):
    studio = next(row for row in monitor.registry if row["id"] == "image")

    class Health:
        status_code = 200
        def json(self):
            return {"ok": True, "app_version": "1.2.3"}

    class Missing:
        status_code = 404
        def json(self):
            return {}

    calls = []
    async def get(url, **kwargs):
        calls.append((url, kwargs))
        return Health() if url.endswith("/api/health") else Missing()

    monkeypatch.setattr(monitor._client, "get", get)
    await monitor._poll_one(studio)
    assert monitor.status["image"]["status"] == "up"
    assert monitor.status["image"]["activity_support"] == "unavailable"
    assert calls[1][0].endswith("/api/fleet/activity")
    assert "headers" in calls[1][1]


@pytest.mark.asyncio
async def test_activity_failure_preserves_last_good_snapshot(monitor, monkeypatch):
    studio = next(row for row in monitor.registry if row["id"] == "image")
    last_good = {
        "schema": "kh-studio.activity.v1", "studio": "image", "observed_at": 10.0,
        "active": None, "latest": None,
    }
    monitor.status["image"] = {"status": "up", "activity": last_good,
                                "activity_support": "available"}

    class Health:
        status_code = 200
        def json(self):
            return {"ok": True, "app_version": "1.2.3"}

    async def get(url, **kwargs):
        if url.endswith("/api/health"):
            return Health()
        raise RuntimeError("activity transport lost")

    monkeypatch.setattr(monitor._client, "get", get)
    await monitor._poll_one(studio)
    assert monitor.status["image"]["status"] == "up"
    assert monitor.status["image"]["activity"] == last_good
    assert monitor.status["image"]["activity_support"] == "error"


def test_repeated_worker_restart_alert_is_edge_triggered(reset, monitor):
    from backend import alerts

    studio = next(row for row in monitor.registry if row["id"] == "voice")
    unhealthy = {
        "restart_health": {
            "alert": True, "status": "critical",
            "restarts_24h": 12, "restarts_7d": 30,
        },
    }
    monitor._note_restart_health(studio, unhealthy)
    monitor._note_restart_health(studio, unhealthy)
    assert len([
        event for event in alerts.recent(20)
        if event["kind"] == "worker_restart_rate"
    ]) == 1

    monitor._note_restart_health(
        studio, {"restart_health": {"alert": False, "status": "healthy"}},
    )
    assert alerts.recent(1)[0]["kind"] == "worker_restart_rate_recovered"


@pytest.mark.asyncio
async def test_models_dedup_and_availability(monitor, seed_catalog):
    from backend import registry as reg
    reg.add_user_entries([{"id": "image@mac-b", "modality": "image",
                           "host": "100.1.1.1", "port": 47868, "machine": "mac-b"}])
    monitor.reload_registry()
    common = "org/flux"
    seed_catalog("image", [
        {"repo": common, "label": "Flux", "cache": {"state": "cached"}},
        {"repo": "org/absent", "label": "Nope", "cache": {"state": "absent"}},
    ])
    seed_catalog("image@mac-b", [
        {"repo": common, "label": "Flux", "cache": {"state": "cached"}},
    ])
    rows = await monitor.models_by_repo()
    by_repo = {r["repo"]: r for r in rows}
    # deduped: one row for the shared repo, downloaded on BOTH machines
    assert set(by_repo[common]["cached_on"]) == {"local", "mac-b"}
    assert by_repo[common]["downloaded"] is True
    # the absent model is present but NOT downloaded anywhere
    assert by_repo["org/absent"]["downloaded"] is False
    assert by_repo["org/absent"]["cached_on"] == []


@pytest.mark.asyncio
async def test_aggregate_drops_hosted_rows_but_keeps_local_render(
    monitor, seed_catalog,
):
    monitor.registry.extend([
        {"id": "video", "title": "Video Studio KH", "modality": "video",
         "machine": "local", "host": "127.0.0.1", "port": 47869},
        {"id": "render", "title": "Render Studio KH", "modality": "render",
         "machine": "local", "host": "127.0.0.1", "port": 47874},
    ])
    seed_catalog("video", [
        {"repo": "local/ltx", "label": "LTX", "cache": {"state": "cached"},
         "size_gb": 8.0},
        {"repo": "fal/kling-v2", "label": "Kling v2", "is_cloud": True,
         "provider": "fal", "cost_tier": "paid-cloud", "status": "new",
         "size_gb": 0, "price": {"unit": "second", "amount": 0.05}},
        {"repo": "cloudflare/sdxl-base", "label": "SDXL", "is_cloud": True,
         "provider": "cloud", "size_gb": 0},
    ])
    seed_catalog("render", [
        {"repo": "episode-assembly-v1", "label": "Episode Assembly",
         "cache": {"state": "cached"}, "is_cloud": True},
    ])

    aggregate = await monitor.aggregate_catalog()
    assert {row["repo"] for row in aggregate["models"]} == {
        "local/ltx", "episode-assembly-v1",
    }
    assert aggregate["per_studio"]["video"]["retired_cloud_models"] == 2
    assert all("is_cloud" not in row for row in aggregate["models"])

    rows = await monitor.models_by_repo()
    by_repo = {r["repo"]: r for r in rows}
    assert set(by_repo) == {"local/ltx", "episode-assembly-v1"}
    assert by_repo["episode-assembly-v1"]["downloaded"] is True
    assert all("lane" not in row and "provider" not in row for row in rows)


@pytest.mark.asyncio
async def test_aggregate_skips_down_studios_no_network(monitor, seed_catalog):
    # only 'up' studios contribute; the 4 other defaults are 'unknown' and must
    # not be fetched (would hang/hit network). Seeding one up studio is enough.
    seed_catalog("voice", [{"repo": "x/y", "cache": {"state": "cached"}}])
    agg = await monitor.aggregate_catalog()
    assert agg["total"] == 1
    assert agg["per_studio"]["voice"]["ok"] is True


@pytest.mark.asyncio
async def test_whisper_models_join_fleet_inventory(monitor, seed_catalog):
    import time

    seed_catalog("voice", [{"repo": "org/tts", "cache": {"state": "cached"}}])
    monitor._transcribe_cache["voice"] = (time.time(), {
        "available": True,
        "default_model": "mlx/whisper-turbo",
        "models": [
            {"repo": "mlx/whisper-turbo", "label": "Whisper Turbo",
             "size_gb": 1.6, "recommended": True, "cached": True},
            {"repo": "mlx/whisper-small", "label": "Whisper Small",
             "size_gb": 0.5, "cached": False},
        ],
    })

    rows = await monitor.models_by_repo()
    turbo = next(r for r in rows if r["repo"] == "mlx/whisper-turbo")
    assert turbo["modality"] == "transcription"
    assert turbo["downloaded"] is True
    assert turbo["available_on"] == ["local"]

    inventory = await monitor.transcription_inventory()
    assert inventory["available"] is True
    assert inventory["default_model"] == "mlx/whisper-turbo"
    assert inventory["endpoint_count"] == 1
    assert inventory["ready_count"] == 1
    assert len(inventory["models"]) == 2


@pytest.mark.asyncio
async def test_health_poll_runs_caddy_inspection_off_the_event_loop(
    reset, monitor, monkeypatch,
):
    """A busy Mac must not make this single-worker process stop answering.

    `check_proxy_health` walks every process on the machine (name + cmdline per
    candidate) and gets slower exactly when the Mac is loaded. Run on the event
    loop by the 5s health poll, it froze the whole Hub for the length of that
    walk, so `/health/live` timed out and the site read as flapping.

    The blocking stub can only be released by another task on the loop, so it
    completes if and only if the walk really happens off-loop.
    """
    from backend import peers, resources

    entered, release = threading.Event(), threading.Event()

    def blocking_check():
        entered.set()
        assert release.wait(10), "check_proxy_health ran on the event loop"
        return {"status": "not_running"}

    async def no_poll(_studio):
        return None

    async def no_refresh(*_args, **_kwargs):
        return None

    monkeypatch.setattr(resources, "check_proxy_health", blocking_check)
    monkeypatch.setattr(monitor, "_poll_one", no_poll)
    monkeypatch.setattr(peers, "refresh", no_refresh)

    poll = asyncio.create_task(monitor.poll_all())
    assert await asyncio.to_thread(entered.wait, 10), "poll never inspected the proxy"
    release.set()
    await asyncio.wait_for(poll, 10)
