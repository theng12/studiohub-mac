import json
from pathlib import Path

import pytest

from backend import control_plane


def test_controller_defaults_preserve_existing_single_hub_behavior(reset):
    settings = control_plane.public_settings()

    assert settings["role"] == "standalone"
    assert settings["database_mode"] == "off"
    assert settings["sqlite_authoritative"] is True
    assert settings["global_job_claiming"] is False
    assert control_plane.accepts_customer_jobs() is True


def test_settings_and_database_secret_are_persisted_separately(reset):
    saved = control_plane.save_settings(
        {"role": "controller", "site_id": "phnom-penh-1",
         "site_name": "Phnom Penh 1", "controller_id": "controller-a",
         "database_mode": "shadow"},
        new_database_url="postgresql://owner:secret@db.internal:5432/studiohub",
    )

    assert saved["database_configured"] is True
    assert saved["database_endpoint"] == "postgresql://db.internal:5432/studiohub"
    assert "secret" not in json.dumps(saved)
    assert "database_url" not in json.loads(control_plane.SETTINGS_FILE.read_text())
    assert control_plane.DATABASE_URL_FILE.read_text().strip().endswith("/studiohub")
    assert oct(control_plane.DATABASE_URL_FILE.stat().st_mode & 0o777) == "0o600"


@pytest.mark.parametrize("values,message", [
    ({"role": "brain"}, "role must"),
    ({"site_id": "BAD SITE"}, "site_id must"),
    ({"controller_id": "bad controller"}, "controller_id must"),
    ({"role": "agent", "database_mode": "shadow"}, "controller role"),
])
def test_controller_settings_validation_is_safe(reset, values, message):
    with pytest.raises(ValueError, match=message):
        control_plane.save_settings(values)


def test_invalid_database_url_does_not_partially_save_settings(reset):
    before = control_plane.public_settings()
    with pytest.raises(ValueError, match="PostgreSQL"):
        control_plane.save_settings(
            {"role": "controller", "site_id": "site-a", "site_name": "Site A",
             "controller_id": "controller-a", "database_mode": "shadow"},
            new_database_url="https://example.com/not-postgres",
        )
    assert control_plane.public_settings()["role"] == before["role"]
    assert not control_plane.SETTINGS_FILE.exists()


def test_agent_role_rejects_customer_jobs_but_keeps_health_online(authed):
    response = authed.put("/api/hub/controller", json={
        "role": "agent", "site_id": "site-a", "site_name": "Site A",
        "controller_id": "agent-a", "database_mode": "off",
    })
    assert response.status_code == 200

    refused = authed.post("/api/hub/jobs", json={
        "modality": "image", "model": "org/model", "items": [{"prompt": "x"}],
    })
    assert refused.status_code == 409
    assert "agent mode" in refused.json()["detail"]
    assert authed.get("/api/health").status_code == 200


def test_controller_health_endpoints_are_public_and_truthful(client):
    live = client.get("/health/live")
    ready = client.get("/health/ready")
    capacity = client.get("/health/capacity")

    assert live.status_code == 200 and live.json()["live"] is True
    assert ready.status_code == 200 and ready.json()["ready"] is True
    assert capacity.status_code == 200
    assert capacity.json()["accepting_customer_jobs"] is True


def test_shadow_controller_is_not_ready_when_postgres_is_unavailable(client):
    control_plane.save_settings(
        {"role": "controller", "site_id": "site-a", "site_name": "Site A",
         "controller_id": "controller-a", "database_mode": "shadow"},
        new_database_url="postgresql://db.internal/studiohub",
    )
    control_plane.runtime._status.update(connected=False, last_error="database unavailable")

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["ready"] is False
    assert response.json()["global_job_claiming"] is False


class _FakeConnection:
    def __init__(self):
        self.calls = []
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def execute(self, statement, params=None):
        self.calls.append((str(statement), params))
        return self

    def commit(self):
        self.committed = True


@pytest.mark.asyncio
async def test_shadow_mode_initializes_schema_heartbeat_and_job_snapshot(
        reset, monitor, monkeypatch):
    connection = _FakeConnection()
    monkeypatch.setattr(control_plane, "_connect", lambda url: connection)
    control_plane.save_settings(
        {"role": "controller", "site_id": "site-a", "site_name": "Site A",
         "controller_id": "controller-a", "database_mode": "shadow"},
        new_database_url="postgresql://db.internal/studiohub",
    )
    control_plane.runtime._monitor = monitor
    control_plane.runtime._version = "1.53.0"
    control_plane.queue_shadow_job("generation", {
        "id": "batch-1", "created_at": 100.0, "client_request_id": "request-1",
        "request_fingerprint": "abc", "items": [
            {"index": 0, "state": "queued", "tries": 0, "prompt": "hello"},
        ],
    })

    result = await control_plane.runtime.check_now()

    statements = "\n".join(call[0] for call in connection.calls)
    assert result["ok"] is True
    assert result["schema_version"] == 1
    assert result["shadow_jobs_written"] == 1
    assert connection.committed is True
    assert "CREATE TABLE IF NOT EXISTS controllers" in statements
    assert "INSERT INTO controllers" in statements
    assert "INSERT INTO jobs" in statements
    assert "INSERT INTO job_items" in statements
    # Explicit casts keep nullable lifecycle timestamps valid in real
    # PostgreSQL; otherwise a pending job's NULL finish time is untyped.
    assert "CAST(%s AS double precision) IS NULL" in statements
    assert control_plane.runtime.status()["pending_job_snapshots"] == 0


@pytest.mark.asyncio
async def test_database_failure_keeps_shadow_work_and_redacts_credentials(
        reset, monkeypatch):
    url = "postgresql://owner:super-secret@db.internal/studiohub"
    control_plane.save_settings(
        {"role": "controller", "site_id": "site-a", "site_name": "Site A",
         "controller_id": "controller-a", "database_mode": "shadow"},
        new_database_url=url,
    )
    control_plane.queue_shadow_job("generation", {
        "id": "batch-1", "created_at": 1, "items": [{"state": "queued"}],
    })
    monkeypatch.setattr(
        control_plane, "_connect",
        lambda configured: (_ for _ in ()).throw(RuntimeError(f"cannot connect to {configured}")),
    )

    result = await control_plane.runtime.check_now()

    serialized = json.dumps(result)
    assert result["ok"] is False
    assert "super-secret" not in serialized
    assert "owner" not in serialized
    assert control_plane.runtime.status()["pending_job_snapshots"] == 1


def test_migration_schema_contains_future_lease_and_fencing_fields():
    sql = control_plane.MIGRATION_FILE.read_text(encoding="utf-8")
    for required in (
        "lease_owner", "lease_expires_at", "fencing_token",
        "global_operation_leases", "audit_events", "job_attempts",
    ):
        assert required in sql


def test_shadow_outage_queue_is_memory_bounded(reset, monkeypatch):
    monkeypatch.setattr(control_plane, "MAX_PENDING_SHADOW_BYTES", 180)
    control_plane.save_settings(
        {"role": "controller", "site_id": "site-a", "site_name": "Site A",
         "controller_id": "controller-a", "database_mode": "shadow"},
        new_database_url="postgresql://db.internal/studiohub",
    )

    for index in range(8):
        control_plane.queue_shadow_job("generation", {
            "id": f"batch-{index}", "created_at": index,
            "items": [{"index": 0, "state": "queued", "prompt": "x" * 40}],
        })

    status = control_plane.runtime.status()
    assert status["pending_job_snapshots"] < 8
    # A single valid snapshot may exceed an artificially tiny test cap, but an
    # outage can never retain an unbounded number of them.
    assert status["pending_job_snapshots"] == 1


def test_dashboard_exposes_controller_setup_and_migration_guard():
    dashboard = (Path(__file__).parents[1] / "frontend" / "index.html").read_text()
    assert 'id="controller-role"' in dashboard
    assert 'id="controller-db-mode"' in dashboard
    assert 'function saveController()' in dashboard
    assert "global job claiming is intentionally disabled" in dashboard
