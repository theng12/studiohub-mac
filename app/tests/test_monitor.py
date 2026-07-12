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
