import asyncio
import stat
import time
from pathlib import Path

import pytest

from backend import auth, broker, control_plane, hardware_profiles, model_exposure
from backend.monitor import StudioMonitor


def test_model_exposure_runtime_state_is_ignored_by_git() -> None:
    root = Path(__file__).parents[2]
    ignored = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "model_exposures.json" in ignored


def _audit(model_id="org/model", *, operation="image.text_to_image",
           revision="a" * 40, approved=True, status="passed"):
    return {
        "schema": "studio.model-audit",
        "schema_version": 1,
        "audit_id": f"audit-{model_id}",
        "audit_status": status,
        "candidate_for_genstudio": approved,
        "contract_hash": "sha256:" + "b" * 64,
        "runtime_revision": revision,
        "approved_operations": [operation],
        "audited_at": "2026-08-02T00:00:00Z",
        "adapter": {"id": "mlx", "version": "1"},
        "controls": {"steps": {"default": 4, "min": 1, "max": 20}},
        "input_limits": {"max_prompt_characters": 15_000},
        "output_limits": {"max_pixels": 4_194_304},
        "capacity": {"max_concurrency": 2, "available_slots": 2},
        "hardware": {"min_unified_memory_gb": 12},
    }


def _model(model_id="org/model", **candidate_kwargs):
    return {
        "repo": model_id,
        "label": "Audited model",
        "cache": {"state": "cached"},
        "min_unified_memory_gb": 12,
        "genstudio_candidate": _audit(model_id, **candidate_kwargs),
    }


def test_candidate_requires_a_passed_deliberate_sibling_audit(reset):
    passed = model_exposure.candidate_summary(_model())
    assert passed is not None
    assert passed["capacity"] == {"max_concurrency": 2, "available_slots": 2}
    assert model_exposure.state_for(
        passed, "image.text_to_image",
    )["state"] == "candidate"

    unapproved = model_exposure.candidate_summary(_model(approved=False))
    failed = model_exposure.candidate_summary(_model(status="failed"))
    assert model_exposure.state_for(
        unapproved, "image.text_to_image",
    )["state"] == "blocked"
    assert model_exposure.state_for(
        failed, "image.text_to_image",
    )["state"] == "blocked"


def test_approval_is_exact_and_revocation_survives_candidate_removal(reset):
    candidate = model_exposure.candidate_summary(_model())
    operation = "image.text_to_image"
    approved = model_exposure.approve(candidate, operation)
    assert model_exposure.is_approved(candidate, operation)

    changed = model_exposure.candidate_summary(_model(revision="c" * 40))
    assert model_exposure.state_for(changed, operation) == {
        "state": "suspended",
        "reason": "runtime_revision_or_contract_changed",
    }

    revoked = model_exposure.revoke_key(approved["exposure_key"])
    assert revoked["state"] == "revoked"
    assert not model_exposure.is_approved(candidate, operation)
    assert model_exposure.records()[0]["state"] == "revoked"
    assert stat.S_IMODE(model_exposure.STATE_FILE.stat().st_mode) == 0o600


def test_new_candidate_appears_from_cache_without_restart(monitor):
    studio = next(row for row in monitor.registry if row["id"] == "image")
    monitor.registry = [studio]
    monitor.status = {"image": {"status": "up"}}
    monitor._catalog_cache["image"] = (time.time(), {"models": []})
    assert monitor.candidate_models() == []

    monitor._catalog_cache["image"] = (time.time(), {"models": [_model()]})
    rows = monitor.candidate_models()
    assert len(rows) == 1
    assert rows[0]["internal_model_id"] == "org/model"
    assert rows[0]["exposure"]["state"] == "candidate"


def test_owner_can_approve_then_revoke_after_candidate_disappears(client, monitor):
    studio = next(row for row in monitor.registry if row["id"] == "image")
    monitor.registry = [studio]
    monitor.status = {"image": {"status": "up"}}
    monitor._catalog_cache["image"] = (time.time(), {"models": [_model()]})
    control_plane.save_settings({
        "role": "controller", "site_id": "site-a", "controller_id": "hub-a",
    })
    client.cookies.set(auth.SESSION_COOKIE_NAME, auth.create_browser_session())

    inventory = client.get("/api/hub/model-exposures").json()
    assert inventory["candidates"][0]["supply"][
        "eligible_physical_slots_total"
    ] == 1
    assert inventory["candidates"][0]["machines"][0][
        "capacity_eligible"
    ] is True
    candidate_key = inventory["candidates"][0]["candidate_key"]
    approved = client.post(
        "/api/hub/model-exposures/approve",
        json={"candidate_key": candidate_key},
    )
    assert approved.status_code == 200
    assert approved.json()["exposure"]["state"] == "approved"

    monitor._catalog_cache["image"] = (time.time(), {"models": []})
    revoked = client.post(
        "/api/hub/model-exposures/revoke",
        json={"candidate_key": candidate_key},
    )
    assert revoked.status_code == 200
    assert revoked.json()["exposure"]["state"] == "revoked"


def test_models_workspace_contains_guided_exposure_controls(client):
    html = client.get("/").text
    assert "Audited model candidates" in html
    assert "Refresh audited models" in html
    assert "Approve for GenStudio" in html
    assert "/api/hub/model-exposures/${action}" in html


def test_global_catalog_replaces_site_approval_without_deleting_history(reset):
    first = model_exposure.candidate_summary(_model("org/first"))
    second = model_exposure.candidate_summary(_model("org/second"))
    first_key = model_exposure.candidate_key(first, "image.text_to_image")
    second_key = model_exposure.candidate_key(second, "image.text_to_image")

    def desired(candidate, key):
        return {
            "candidate_key": key,
            "internal_model_id": candidate["internal_model_id"],
            "display_name": candidate["display_name"],
            "operation": "image.text_to_image",
            "runtime_revision": candidate["runtime_revision"],
            "contract_hash": candidate["contract_hash"],
        }

    model_exposure.sync_global_catalog(
        [desired(first, first_key), desired(second, second_key)], revision="a" * 64
    )
    assert model_exposure.global_authority_active()
    assert {row["state"] for row in model_exposure.records()} == {"approved"}

    model_exposure.sync_global_catalog([desired(first, first_key)], revision="b" * 64)
    records = {row["internal_model_id"]: row for row in model_exposure.records()}
    assert records["org/first"]["state"] == "approved"
    assert records["org/second"]["state"] == "revoked"


def test_site_owner_cannot_override_global_catalog(client, monitor):
    studio = next(row for row in monitor.registry if row["id"] == "image")
    monitor.registry = [studio]
    monitor.status = {"image": {"status": "up"}}
    monitor._catalog_cache["image"] = (time.time(), {"models": [_model()]})
    control_plane.save_settings(
        {"role": "controller", "site_id": "site-a", "controller_id": "hub-a"}
    )
    client.cookies.set(auth.SESSION_COOKIE_NAME, auth.create_browser_session())
    candidate = model_exposure.candidate_summary(_model())
    key = model_exposure.candidate_key(candidate, "image.text_to_image")
    model_exposure.sync_global_catalog([], revision="a" * 64)

    inventory = client.get("/api/hub/model-exposures").json()
    assert inventory["managed_by"] == "genstudio"
    assert inventory["can_expose"] is False
    response = client.post(
        "/api/hub/model-exposures/approve", json={"candidate_key": key}
    )
    assert response.status_code == 409
    assert "Approved Fleet Model Catalog" in response.json()["detail"]


def test_models_read_is_cache_only(authed, monitor, monkeypatch):
    studio = next(row for row in monitor.registry if row["id"] == "image")
    monitor.registry = [studio]
    monitor._catalog_cache["image"] = (time.time(), {"models": [_model()]})

    async def unexpected_network(*args, **kwargs):
        raise AssertionError("ordinary Models reads must remain cache-only")

    monkeypatch.setattr(monitor, "aggregate_catalog", unexpected_network)
    monkeypatch.setattr(monitor, "get_catalog", unexpected_network)
    response = authed.get("/api/hub/models")
    assert response.status_code == 200
    assert response.json()["models"][0]["repo"] == "org/model"


@pytest.mark.asyncio
async def test_overlapping_catalog_refresh_is_rejected(monitor):
    monitor._catalog_refresh_lock = asyncio.Lock()
    await monitor._catalog_refresh_lock.acquire()
    try:
        assert await monitor.refresh_catalogs() == {
            "started": False, "reason": "refresh_in_progress",
        }
    finally:
        monitor._catalog_refresh_lock.release()


def test_supply_keeps_machine_states_hardware_and_reasons_distinct(
        monitor, monkeypatch):
    machine_states = {
        "ready": "up",
        "busy": "up",
        "offline": "down",
        "quarantine": "up",
        "lowmem": "up",
    }
    monitor.registry = [{
        "id": f"image@{machine}", "title": "Image Studio",
        "modality": "image", "machine": machine,
        "host": "127.0.0.1", "port": 47868,
    } for machine in machine_states]
    monitor.status = {
        f"image@{machine}": {
            "status": state,
            "health": {"memory": {"total_gb": 8 if machine == "lowmem" else 24}},
        }
        for machine, state in machine_states.items()
    }
    for machine in machine_states:
        profile = "mac-mini-m1-8gb" if machine == "lowmem" else "mac-mini-m4-24gb"
        hardware_profiles.set_machine_hardware_profile(machine, profile)
        monitor._catalog_cache[f"image@{machine}"] = (
            time.time(), {"models": [_model()]},
        )
    monkeypatch.setattr(broker, "busy_studios", lambda: set())
    monkeypatch.setattr(broker, "busy_machines", lambda: {"busy"})
    monkeypatch.setattr(
        broker, "machine_protection_snapshot",
        lambda: {"quarantine": {"quarantined": True}},
    )

    row = monitor.candidate_models()[0]
    assert row["supply"] == {
        "installed_machine_count": 5,
        "online_machine_count": 4,
        "ready_machine_count": 1,
        "busy_machine_count": 1,
        "offline_machine_count": 1,
        "quarantined_machine_count": 1,
        "offline_or_quarantined_machine_count": 2,
        "available_physical_slots": 1,
        "eligible_physical_slots_total": 2,
    }
    evidence = {item["machine_id"]: item for item in row["machines"]}
    assert evidence["busy"]["availability_reason"] == "machine_busy"
    assert evidence["busy"]["capacity_eligible"] is True
    assert evidence["offline"]["availability_reason"] == "worker_offline"
    assert evidence["offline"]["capacity_eligible"] is False
    assert evidence["quarantine"]["availability_reason"] == "machine_quarantined"
    assert evidence["quarantine"]["capacity_eligible"] is False
    assert evidence["lowmem"]["availability_reason"] == "insufficient_total_memory"
    assert evidence["lowmem"]["capacity_eligible"] is False
    assert evidence["ready"]["hardware_profile"]["memory_gb"] == 24
    assert evidence["ready"]["capacity_eligible"] is True
    assert evidence["lowmem"]["hardware_profile"]["memory_gb"] == 8


@pytest.mark.asyncio
async def test_failed_refresh_retains_last_good_catalog(monitor, monkeypatch):
    studio = next(row for row in monitor.registry if row["id"] == "image")
    monitor.registry = [studio]
    monitor.status["image"] = {"status": "up"}
    original = {"models": [_model()]}
    observed_at = time.time() - 30
    monitor._catalog_cache["image"] = (observed_at, original)

    async def unavailable(*args, **kwargs):
        raise RuntimeError("worker unavailable")

    monkeypatch.setattr(monitor._client, "get", unavailable)
    result = await monitor._refresh_studio_catalog(studio, force=True)
    assert result["refreshed"] is False
    assert monitor._catalog_cache["image"] == (observed_at, original)
    assert monitor.catalog_observation("image")["last_error"] == "worker unavailable"


@pytest.mark.asyncio
async def test_persisted_last_good_catalog_survives_monitor_restart(
        monitor, tmp_path):
    path = tmp_path / "catalog-observations.json"
    monitor.catalog_state_path = path
    monitor._catalog_cache["image"] = (time.time(), {"models": [_model()]})
    monitor._catalog_meta["image"] = {
        "last_success_at": time.time(), "last_error": None,
    }
    monitor._save_catalog_state()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    restarted = StudioMonitor(catalog_state_path=path)
    try:
        assert restarted._catalog_cache["image"][1]["models"][0]["repo"] == "org/model"
        assert restarted.catalog_observation("image")["source"] == "persisted_last_good"
    finally:
        await restarted._client.aclose()
