"""Test harness — fully isolated from the live Hub.

Before ANY backend module is imported, STUDIOHUB_DATA_DIR is pointed at a fresh
temp dir, so tests never read/write the real studios.json / hub.db / tokens.
The TestClient is built WITHOUT the lifespan context, so the health poller and
broker dispatch loop never start — no network hits the real studios. Tests that
need studio data seed monitor state directly.
"""

import os
import tempfile
from pathlib import Path

# MUST happen before importing backend.* anywhere.
_DATA = Path(tempfile.mkdtemp(prefix="hubtest-"))
os.environ["STUDIOHUB_DATA_DIR"] = str(_DATA)

import pytest
from starlette.testclient import TestClient


def _reset_state():
    from backend import (alerts, auth, broker, chat_jobs, fleet_ops, job_storage,
                         ledger, metrics, peers, shared_voices, transcription_jobs)
    from backend import main
    from backend import registry as reg
    from backend.main import monitor
    alerts._recent.clear()
    try:
        alerts.ALERTS_FILE.unlink()
    except FileNotFoundError:
        pass
    # wipe ALL persisted state between tests (incl. tokens, or one test's token
    # leaks into the next)
    for f in (ledger.DB_FILE, reg.REGISTRY_FILE, reg.LABELS_FILE,
              peers.FLEET_TOKEN_FILE, peers.SHARED_STUDIO_TOKEN_FILE,
              auth.TOKEN_FILE, metrics.STATE_FILE, job_storage.SETTINGS_FILE):
        try:
            f.unlink()
        except FileNotFoundError:
            pass
    peers._inflight["v"] = False
    # in-memory state
    broker.batches.clear()
    broker._busy.clear()
    broker._maintenance.clear()
    broker._external_machine_leases.clear()
    broker._reserved["gb"] = 0.0
    transcription_jobs.reset_for_tests()
    chat_jobs.reset_for_tests()
    import shutil
    shutil.rmtree(transcription_jobs.ROOT, ignore_errors=True)
    shutil.rmtree(shared_voices.ROOT, ignore_errors=True)
    shared_voices._tasks.clear()
    try:
        transcription_jobs.SETTINGS_FILE.unlink()
    except FileNotFoundError:
        pass
    peers._cache.clear()
    fleet_ops._updates.clear()
    metrics.samples.clear()
    metrics.watchdog.clear()
    metrics._last_sample = 0.0
    main._transcription_busy.clear()
    reg._labels_cache = None
    reg._flags_cache = None
    # monitor: reload default registry, mark everything unknown (no network)
    monitor.reload_registry()
    monitor.status = {s["id"]: {"status": "unknown", "last_seen": None,
                                "last_checked": None} for s in monitor.registry}
    monitor._catalog_cache.clear()
    monitor._transcribe_cache.clear()
    monitor._provider_cache.clear()


@pytest.fixture
def reset():
    _reset_state()
    yield
    _reset_state()


@pytest.fixture
def app(reset):
    from backend.main import app as _app
    return _app


@pytest.fixture
def client(app):
    # No `with` -> lifespan (pollers/dispatcher) never starts.
    return TestClient(app)


@pytest.fixture
def token():
    from backend.main import HUB_TOKEN
    return HUB_TOKEN


@pytest.fixture
def authed(app, token):
    return TestClient(app, headers={"X-Hub-Token": token})


@pytest.fixture
def monitor(reset):
    from backend.main import monitor as m
    return m


@pytest.fixture
def seed_catalog(monitor):
    """Helper: mark a studio 'up' and seed its catalog into the cache so
    aggregate_catalog serves it WITHOUT any network call (cache is fresh)."""
    import time

    def _seed(studio_id, models, status="up"):
        monitor.status[studio_id] = {"status": status, "last_seen": time.time(),
                                     "last_checked": time.time()}
        monitor._catalog_cache[studio_id] = (time.time(), {"models": models})
        if studio_id.split("@", 1)[0] == "voice":
            monitor._transcribe_cache.setdefault(
                studio_id, (time.time(), {"available": False, "models": []}))
    return _seed
