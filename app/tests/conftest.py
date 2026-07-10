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
    from backend import broker, ledger, metrics, peers
    from backend import registry as reg
    from backend.main import monitor
    # wipe persisted DB + state files
    for f in (ledger.DB_FILE, _DATA / "studios.json",
              _DATA / "machine_labels.json", _DATA / "hub_state.json"):
        try:
            f.unlink()
        except FileNotFoundError:
            pass
    # in-memory state
    broker.batches.clear()
    broker._busy.clear()
    peers._cache.clear()
    metrics.samples.clear()
    metrics.watchdog.clear()
    metrics._last_sample = 0.0
    reg._labels_cache = None
    # monitor: reload default registry, mark everything unknown (no network)
    monitor.reload_registry()
    monitor.status = {s["id"]: {"status": "unknown", "last_seen": None,
                                "last_checked": None} for s in monitor.registry}
    monitor._catalog_cache.clear()


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
    return _seed
