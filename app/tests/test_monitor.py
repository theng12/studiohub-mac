import pytest

from backend import monitor as mon


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

    studio = next(row for row in monitor.registry if row["id"] == "chat")
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
    assert monitor.status["chat"]["status"] == "down"
    assert any(event["kind"] == "studio_down" for event in alerts.recent(20))


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


def test_is_cloud_lane_excludes_render():
    assert mon.is_cloud_lane(True, "video") is True
    assert mon.is_cloud_lane(True, "image") is True
    assert mon.is_cloud_lane(False, "image") is False
    # render overloads is_cloud=true as a broker governor bypass — never cloud
    assert mon.is_cloud_lane(True, "render") is False
    assert mon.is_cloud_lane(None, "video") is False


@pytest.mark.asyncio
async def test_cloud_models_carry_lane_and_provider(monitor, seed_catalog):
    # Video Studio gateway surfaces cloud + local entries in one catalog.
    seed_catalog("video", [
        {"repo": "local/ltx", "label": "LTX", "cache": {"state": "cached"},
         "size_gb": 8.0},
        {"repo": "fal/kling-v2", "label": "Kling v2", "is_cloud": True,
         "provider": "fal", "cost_tier": "paid-cloud", "status": "new",
         "size_gb": 0, "price": {"unit": "second", "amount": 0.05}},
        {"repo": "fal/old-model", "label": "Old", "is_cloud": True,
         "provider": "fal", "cost_tier": "paid-cloud", "status": "deprecated",
         "size_gb": 0},
        # existing Image/Chat style: generic provider="cloud", real vendor in the
        # repo prefix — must derive "cloudflare" so it groups on its own.
        {"repo": "cloudflare/sdxl-base", "label": "SDXL", "is_cloud": True,
         "provider": "cloud", "size_gb": 0},
    ])
    # render flags is_cloud=true only to bypass the broker gates — it is LOCAL.
    seed_catalog("render", [
        {"repo": "episode-assembly-v1", "label": "Episode Assembly",
         "cache": {"state": "cached"}, "is_cloud": True},
    ])
    rows = await monitor.models_by_repo()
    by_repo = {r["repo"]: r for r in rows}
    # render is never in the cloud lane despite is_cloud=true at the source
    render = by_repo["episode-assembly-v1"]
    assert render["lane"] == "local"
    assert render["is_cloud"] is False
    # local entry stays in the local lane, no provider
    assert by_repo["local/ltx"]["lane"] == "local"
    assert by_repo["local/ltx"]["is_cloud"] is False
    assert by_repo["local/ltx"]["provider"] is None
    # cloud entries carry lane + provider + status + price verbatim
    kling = by_repo["fal/kling-v2"]
    assert kling["lane"] == "cloud"
    assert kling["is_cloud"] is True
    assert kling["provider"] == "fal"
    assert kling["status"] == "new"
    assert kling["price"] == {"unit": "second", "amount": 0.05}
    assert by_repo["fal/old-model"]["status"] == "deprecated"
    # generic provider="cloud" resolves to the repo-prefix vendor
    assert by_repo["cloudflare/sdxl-base"]["provider"] == "cloudflare"
    # local sorts before cloud in the row order
    lanes = [r["lane"] for r in rows]
    assert lanes.index("local") < lanes.index("cloud")


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
