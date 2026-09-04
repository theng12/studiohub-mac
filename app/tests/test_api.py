import asyncio
import ast
import hashlib
import json
from copy import deepcopy
from pathlib import Path
import re
import subprocess

import pytest


def test_enrollment_repair_keeps_capability_release_vocabularies_and_product_scope():
    from backend import capabilities, release_reconciliation

    assert (capabilities.SCHEMA_NAME, capabilities.SCHEMA_VERSION) == (
        "studiohub.site-capabilities", 3,
    )
    assert release_reconciliation.SITE_STATES == {
        "pending", "queued", "running", "waiting_busy", "degraded",
        "blocked_release", "complete",
    }
    assert release_reconciliation.COMPONENT_STATES == {
        "not_installed", "pending_offline", "pending_busy", "checking",
        "updating", "restarting", "verifying", "current",
        "retryable_failure", "auth_blocked", "release_blocked",
        "excluded_disabled",
    }

    repair_modules = [
        Path(__file__).parents[1] / "backend" / name
        for name in (
            "enrollment_repair.py",
            "enrollment_repair_executor.py",
            "enrollment_repair_store.py",
            "enrollment_repair_transport.py",
        )
    ]
    forbidden_import_roots = {
        "image", "voice", "generation", "genstudio", "model_exposure",
        "fleet_ops", "control",
    }
    forbidden_calls = {
        "configure_joined_agent", "create_enrollment_code",
        "set_fleet_token", "save_registry", "add_user_entries",
        "remove_machine", "run_hub_script",
    }
    for path in repair_modules:
        tree = ast.parse(path.read_text(), filename=str(path))
        imported = set()
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[-1] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.update(alias.name.split(".")[-1] for alias in node.names)
            elif isinstance(node, ast.Call):
                function = node.func
                if isinstance(function, ast.Name):
                    called.add(function.id)
                elif isinstance(function, ast.Attribute):
                    called.add(function.attr)
        assert imported.isdisjoint(forbidden_import_roots), path.name
        assert called.isdisjoint(forbidden_calls), path.name


RELEASE_COMPONENTS = {
    "hub": {
        "repository": "theng12/studiohub-mac",
        "version": "2.8.0",
        "commit": "a" * 40,
    },
    "image": {
        "repository": "theng12/imagestudio-mac",
        "version": "1.30.1",
        "commit": "b" * 40,
        "installed_only": True,
    },
    "voice": {
        "repository": "theng12/voicestudio-mac",
        "version": "2.3.0",
        "commit": "c" * 40,
        "installed_only": True,
    },
}


def _release_manifest(sequence: int = 1) -> dict:
    manifest = {
        "schema": "genstudio.studio-fleet-release-intent",
        "schema_version": 1,
        "sequence": sequence,
        "created_at": "2026-08-15T00:00:00Z",
        "components": deepcopy(RELEASE_COMPONENTS),
    }
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    manifest["release_id"] = "sha256:" + hashlib.sha256(canonical).hexdigest()
    return manifest


def _managed_bundle() -> dict:
    manifest = _release_manifest()
    return {
        "release_id": manifest["release_id"],
        "operation_id": "managed-test-operation",
        "components": manifest["components"],
    }


def _release_service(monkeypatch, tmp_path):
    from backend import main
    from backend.release_reconciliation import ReleaseReconciler

    service = ReleaseReconciler(
        main.monitor,
        state_path=tmp_path / "release_reconciliation.json",
        loaded_version=main._app_version(),
        loaded_commit=main.APP_COMMIT,
    )
    monkeypatch.setattr(main, "release_reconciler", service, raising=False)
    return service


def test_startup_reconciles_idle_automatic_update_scheduler(monkeypatch):
    from backend import main

    calls = []
    monkeypatch.setattr(
        main.auto_updater,
        "apply_scheduler_if_idle",
        lambda: calls.append("reconciled") or True,
    )

    assert main._reconcile_auto_update_scheduler() is True
    assert calls == ["reconciled"]


def test_startup_defers_scheduler_reconciliation_during_active_update(monkeypatch):
    from backend import main

    calls = []
    monkeypatch.setattr(main.auto_updater, "apply_scheduler_if_idle", lambda: False)
    monkeypatch.setattr(
        main, "_schedule_auto_update_reconciliation",
        lambda: calls.append("scheduled-after-update"),
    )

    assert main._reconcile_auto_update_scheduler() is False
    assert calls == ["scheduled-after-update"]


def test_dashboard_includes_render_studio():
    dashboard = (Path(__file__).parents[1] / "frontend" / "index.html").read_text()
    assert '["image", "chat", "voice", "music", "video", "render"]' in dashboard
    assert 'class="d-mod" value="render"' in dashboard
    assert '<option value="render">Render</option>' in dashboard
    assert 'class="workspace-head"' in dashboard
    assert 'const TAB_META = {' in dashboard
    assert 'id="res-machine-sort"' in dashboard
    assert 'localStorage.getItem("res_machine_sort") || "status"' in dashboard
    assert 'localStorage.getItem("res_sort") || "status"' in dashboard
    assert 'class="resource-studio-table"' in dashboard
    assert '<col style="width:32%"><col style="width:23%">' in dashboard
    assert 'id="a-sort"' in dashboard
    assert 'localStorage.getItem("asset_sort") || "newest"' in dashboard
    assert '>Working</button>' in dashboard
    assert 'function stState(s) { return s.busy ? "generating"' in dashboard
    assert 'return compact ? "LLM" : "LLM working"' in dashboard
    assert 'Priority #${rank}' in dashboard
    assert 'loadActiveJobQueues();' in dashboard
    apply_summary = dashboard[dashboard.index("function applySummary(sum)"):
                              dashboard.index("async function pollOnce()")]
    assert "renderBatches(sum.jobs)" not in apply_summary
    assert 'const JOB_QUEUE_REFRESH_MS = 3000;' in dashboard
    assert 'if (vis("jobs") && !document.hidden) loadActiveJobQueues();' in dashboard
    assert 'document.addEventListener("visibilitychange"' in dashboard
    assert 'id="fleet-save"' in dashboard
    assert 'id="fleet-save-result" role="status" aria-live="polite"' in dashboard
    assert 'JSON.stringify({ token, sync: true })' in dashboard
    assert 'id="su-rescan"' in dashboard
    assert 'id="su-progress" class="update-progress hide" role="status"' in dashboard
    assert 'id="hubupd-status" class="update-progress hide" role="status"' in dashboard
    assert 'id="hubupd-sort"' in dashboard
    assert 'id="hubupd-sort-dir"' in dashboard
    assert 'localStorage.getItem("hub_machine_sort") || "status"' in dashboard
    assert 'function _hubHardware(machine, row = {})' in dashboard
    assert '<th>Machine</th><th>Chip</th><th>RAM</th>' in dashboard
    assert 'onclick="updateReadyHubs()"' in dashboard
    assert 'function startHubUpdate(machines = null)' in dashboard
    assert 'class="btn primary compact"' in dashboard
    # Voice Studio no longer exposes /api/providers, so the dashboard must not
    # render (or ask for) cloud-audio provider health anywhere.
    assert 'providerHealth' not in dashboard
    assert 'cloud_providers' not in dashboard
    assert '>Cancel image queue</button>' in dashboard
    assert 'data-job-kind="image"' in dashboard
    assert 'data-job-kind="voice"' in dashboard
    assert 'data-job-kind="transcription"' in dashboard
    assert 'data-job-kind="chat"' in dashboard
    assert 'per: 10' in dashboard
    assert 'generationDetailToggle(this' in dashboard
    assert 'function resourceUsageHTML(resource)' in dashboard
    assert '<th>Resources</th><th>Failure</th>' in dashboard
    assert 'worker peak ${Number(worker.peak_rss_gb).toFixed(2)} GB' in dashboard
    assert 'function toggleStudio(id, enabled)' in dashboard
    assert 'new jobs for only that app' in dashboard
    assert 'id="hau-restart"' in dashboard
    assert 'function restartHub(force = false, confirmed = false)' in dashboard
    assert 'function waitForHubRestart(expectedVersion' in dashboard


def test_job_storage_cap_defaults_to_safe_fleet_policy_and_is_configurable(authed):
    initial = authed.get("/api/hub/job-storage")
    assert initial.status_code == 200
    assert initial.json()["enabled"] is True
    assert initial.json()["max_bytes"] == 80 * 1024 ** 3
    assert initial.json()["retention_days"] == 30
    saved = authed.post("/api/hub/job-storage", json={"enabled": True, "max_gb": 5})
    assert saved.status_code == 200
    assert saved.json()["enabled"] is True
    assert saved.json()["max_bytes"] == 5 * 1024 ** 3


def test_health_and_version(client):
    h = client.get("/api/health").json()
    assert h["ok"] is True and "app_version" in h
    assert re.fullmatch(r"[0-9a-f]{40}", h["app_commit"])
    v = client.get("/api/version").json()
    assert v["title"] == "Studio Hub KH"
    assert re.fullmatch(r"[0-9a-f]{40}", v["app_commit"])


def test_managed_update_route_advertises_capability_and_threads_full_tuple(authed, monkeypatch):
    from backend import main

    request = {
        "after_current": True,
        "target_commit": "a" * 40,
        "target_version": "2.8.0",
        "operation_id": "hub-op-1",
    }
    calls = []
    monkeypatch.setattr(main.auto_updater, "trigger_update", lambda **kwargs: calls.append(kwargs) or {"state": "deferred"})

    status = authed.get("/api/auto-update/status")
    response = authed.post("/api/auto-update/update", json=request)

    assert status.json()["capabilities"] == {
        "managed_exact_commit": True,
        "dependency_convergence": 1,
    }
    assert response.status_code == 200
    assert calls == [request]


def test_legacy_self_update_route_uses_verified_updater(authed, monkeypatch):
    from backend import main

    calls = []
    monkeypatch.setattr(
        main.auto_updater, "trigger_update",
        lambda **kwargs: calls.append(kwargs) or {"state": "updating"},
    )

    response = authed.post("/api/hub/maintenance/self-update")

    assert response.status_code == 200
    assert response.json()["state"] == "updating"
    assert calls == [{"after_current": False}]


@pytest.mark.asyncio
async def test_managed_hub_runner_preserves_clean_checkout_health_failure(monkeypatch):
    from backend import main

    monkeypatch.setattr(main.auto_updater, "trigger_update", lambda **kwargs: {
        "state": "failed",
        "details": [
            "The updated app did not attest to the expected commit and version."
        ],
    })

    result = await main._run_managed_hub_update(
        {"commit": "a" * 40, "version": "2.8.0"}, "hub-op-clean-failure",
    )

    assert result == {
        "component": "hub",
        "state": "retryable_failure",
        "error_code": "clean_checkout_health_failure",
    }


def test_release_intent_requires_machine_auth_and_controller_role(
    client, authed, monkeypatch, tmp_path,
):
    from backend import control_plane

    _release_service(monkeypatch, tmp_path)
    manifest = _release_manifest()

    assert client.put(
        "/api/hub/maintenance/release-intent", json=manifest,
    ).status_code == 401
    assert authed.put(
        "/api/hub/maintenance/release-intent", json=manifest,
    ).status_code == 409

    control_plane.save_settings({
        "role": "controller",
        "site_id": "site-a",
        "site_name": "Site A",
        "controller_id": "controller-a",
    })
    accepted = authed.put("/api/hub/maintenance/release-intent", json=manifest)
    assert accepted.status_code == 200
    assert accepted.json()["release_id"] == manifest["release_id"]
    assert accepted.json()["site_id"] == "site-a"
    assert accepted.json()["controller_id"] == "controller-a"


def _controller_site() -> None:
    from backend import control_plane

    control_plane.save_settings({
        "role": "controller",
        "site_id": "site-a",
        "site_name": "Site A",
        "controller_id": "controller-a",
    })


def test_withdrawing_release_intent_unblocks_an_intent_a_stuck_job_had_frozen(
    authed, monkeypatch, tmp_path,
):
    """A job that can never terminate must not freeze the intent forever.

    ``replace_intent`` refuses a new intent while a prior job is nonterminal.
    Some jobs never terminate by design, so without a withdrawal path the
    controller is permanently stuck on an intent it cannot satisfy or replace.
    """
    service = _release_service(monkeypatch, tmp_path)
    _controller_site()
    stuck = _release_manifest()
    assert authed.put(
        "/api/hub/maintenance/release-intent", json=stuck,
    ).status_code == 200

    async def no_execution(_release_id):
        return None

    monkeypatch.setattr(service, "run", no_execution)
    activated = authed.post(
        f"/api/hub/maintenance/release-intent/{stuck['release_id']}/activate"
    )
    assert activated.status_code == 202

    successor = _release_manifest(sequence=2)
    blocked = authed.put("/api/hub/maintenance/release-intent", json=successor)
    assert blocked.status_code == 422
    assert "nonterminal" in blocked.json()["detail"]

    withdrawn = authed.delete("/api/hub/maintenance/release-intent")

    assert withdrawn.status_code == 200
    body = withdrawn.json()
    assert body["ok"] is True
    assert body["withdrawn"] is True
    assert body["release_id"] == stuck["release_id"]
    assert body["sequence"] == 1
    assert body["created_at"] == "2026-08-15T00:00:00Z"
    assert body["received_at"] is not None
    assert body["activation"]["release_id"] == stuck["release_id"]
    assert [job["id"] for job in body["jobs"]] == [activated.json()["job_id"]]
    assert body["jobs"][0]["release_id"] == stuck["release_id"]
    assert body["site_id"] == "site-a"
    assert body["controller_id"] == "controller-a"

    cleared = authed.get("/api/hub/maintenance/release-intent").json()
    assert cleared["desired"] is None
    assert cleared["activation"] is None
    assert cleared["jobs"] == []
    assert service.capability_evidence()["desired"] is None

    accepted = authed.put("/api/hub/maintenance/release-intent", json=successor)
    assert accepted.status_code == 200
    assert accepted.json()["release_id"] == successor["release_id"]


def test_withdrawing_release_intent_records_the_abandoned_release(
    authed, monkeypatch, tmp_path, caplog,
):
    """The withdrawn intent stays traceable after its durable state is gone."""
    import logging

    from backend import main

    _release_service(monkeypatch, tmp_path)
    _controller_site()
    manifest = _release_manifest()
    assert authed.put(
        "/api/hub/maintenance/release-intent", json=manifest,
    ).status_code == 200

    main._hub_log.propagate = True
    try:
        with caplog.at_level(logging.WARNING, logger="studiohub"):
            assert authed.delete(
                "/api/hub/maintenance/release-intent",
            ).status_code == 200
    finally:
        main._hub_log.propagate = False

    line = next(
        record.getMessage() for record in caplog.records
        if record.getMessage().startswith("release-intent-withdrawn ")
    )
    recorded = json.loads(line.split(" ", 1)[1])
    assert recorded["withdrawn"] is True
    assert recorded["release_id"] == manifest["release_id"]
    assert recorded["sequence"] == 1
    assert recorded["created_at"] == "2026-08-15T00:00:00Z"
    assert recorded["site_id"] == "site-a"
    assert recorded["controller_id"] == "controller-a"


def test_withdrawing_release_intent_is_a_safe_no_op_when_none_is_published(
    authed, monkeypatch, tmp_path,
):
    _release_service(monkeypatch, tmp_path)
    _controller_site()

    first = authed.delete("/api/hub/maintenance/release-intent")

    assert first.status_code == 200
    assert first.json()["withdrawn"] is False
    assert first.json()["release_id"] is None
    assert first.json()["jobs"] == []

    manifest = _release_manifest()
    assert authed.put(
        "/api/hub/maintenance/release-intent", json=manifest,
    ).status_code == 200
    assert authed.delete(
        "/api/hub/maintenance/release-intent",
    ).json()["withdrawn"] is True

    repeated = authed.delete("/api/hub/maintenance/release-intent")

    assert repeated.status_code == 200
    assert repeated.json()["withdrawn"] is False
    assert authed.get(
        "/api/hub/maintenance/release-intent",
    ).json()["desired"] is None


def test_withdrawing_release_intent_requires_the_same_credentials_as_publishing(
    app, client, authed, token, monkeypatch, tmp_path,
):
    from starlette.testclient import TestClient
    from backend import auth, control_plane

    _release_service(monkeypatch, tmp_path)

    assert client.delete(
        "/api/hub/maintenance/release-intent",
    ).status_code == 401
    assert authed.delete(
        "/api/hub/maintenance/release-intent",
    ).status_code == 409

    _controller_site()
    owner = TestClient(app)
    owner.cookies.set(auth.SESSION_COOKIE_NAME, auth.create_browser_session())
    assert owner.delete(
        "/api/hub/maintenance/release-intent",
    ).status_code in {401, 403}
    assert authed.delete(
        "/api/hub/maintenance/release-intent",
        headers={"Origin": "https://attacker.invalid"},
    ).status_code == 403

    control_plane.save_settings({
        "role": "agent",
        "site_id": "site-a",
        "site_name": "Site A",
        "controller_id": "controller-a",
    })
    assert authed.delete(
        "/api/hub/maintenance/release-intent",
    ).status_code == 403


def test_activation_replay_adopts_one_job(authed, monkeypatch, tmp_path):
    from backend import control_plane

    service = _release_service(monkeypatch, tmp_path)
    manifest = _release_manifest()
    control_plane.save_settings({
        "role": "controller",
        "site_id": "site-a",
        "site_name": "Site A",
        "controller_id": "controller-a",
    })
    assert authed.put(
        "/api/hub/maintenance/release-intent", json=manifest,
    ).status_code == 200

    async def no_execution(_release_id):
        return None

    monkeypatch.setattr(service, "run", no_execution)
    path = f"/api/hub/maintenance/release-intent/{manifest['release_id']}/activate"
    first = authed.post(path)
    second = authed.post(path)

    assert first.status_code == second.status_code == 202
    assert first.json()["job_id"] == second.json()["job_id"]
    polled = authed.get(
        f"/api/hub/maintenance/release-jobs/{first.json()['job_id']}"
    ).json()
    assert {
        key: polled[key] for key in ("role", "site_id", "controller_id")
    } == {
        "role": "controller",
        "site_id": "site-a",
        "controller_id": "controller-a",
    }


def test_release_writes_require_header_machine_token_and_local_role(
    app, token, monkeypatch, tmp_path,
):
    from starlette.testclient import TestClient
    from backend import auth, control_plane

    service = _release_service(monkeypatch, tmp_path)
    manifest = _release_manifest()
    owner = TestClient(app)
    owner.cookies.set(auth.SESSION_COOKIE_NAME, auth.create_browser_session())
    machine = TestClient(app, headers={"X-Hub-Token": token})

    control_plane.save_settings({
        "role": "controller",
        "site_id": "site-a",
        "site_name": "Site A",
        "controller_id": "controller-a",
    })
    assert owner.put(
        "/api/hub/maintenance/release-intent", json=manifest,
    ).status_code in {401, 403}
    assert machine.put(
        "/api/hub/maintenance/release-intent", json=manifest,
    ).status_code == 200

    async def no_execution(_release_id):
        return None

    monkeypatch.setattr(service, "run", no_execution)
    activate = f"/api/hub/maintenance/release-intent/{manifest['release_id']}/activate"
    assert machine.post(activate, json={}).status_code == 202
    assert machine.post(
        "/api/hub/maintenance/managed-update", json=_managed_bundle(),
    ).status_code == 403

    control_plane.save_settings({
        "role": "agent",
        "site_id": "site-a",
        "site_name": "Site A",
        "controller_id": "controller-a",
    })
    child = service.admit_managed_update(_managed_bundle())
    polled = machine.get(
        f"/api/hub/maintenance/managed-update/{child['job_id']}"
    ).json()
    assert {
        key: polled[key] for key in ("role", "site_id", "controller_id")
    } == {
        "role": "agent",
        "site_id": "site-a",
        "controller_id": "controller-a",
    }
    monkeypatch.setattr(
        service,
        "admit_and_schedule_managed_update",
        lambda bundle: {"job_id": "agent-test", "adopted": False},
    )
    assert owner.post(
        "/api/hub/maintenance/managed-update", json=_managed_bundle(),
    ).status_code in {401, 403}
    assert machine.post(
        "/api/hub/maintenance/managed-update", json=_managed_bundle(),
    ).status_code == 202
    assert machine.put(
        "/api/hub/maintenance/release-intent", json=manifest,
    ).status_code == 403


def test_release_write_rejects_missing_invalid_and_cross_origin_credentials(
    app, token, monkeypatch, tmp_path,
):
    from starlette.testclient import TestClient
    from backend import control_plane

    _release_service(monkeypatch, tmp_path)
    control_plane.save_settings({
        "role": "controller",
        "site_id": "site-a",
        "site_name": "Site A",
        "controller_id": "controller-a",
    })
    manifest = _release_manifest()
    client = TestClient(app)
    machine = TestClient(app, headers={"X-Hub-Token": token})

    assert client.put(
        "/api/hub/maintenance/release-intent", json=manifest,
    ).status_code == 401
    assert client.put(
        "/api/hub/maintenance/release-intent",
        headers={"X-Hub-Token": "bad"},
        json=manifest,
    ).status_code == 401
    assert machine.put(
        "/api/hub/maintenance/release-intent",
        headers={"Origin": "https://attacker.invalid"},
        json=manifest,
    ).status_code == 403


@pytest.mark.asyncio
async def test_release_reconciler_lifecycle_adopts_and_stops_due_scanner(
    monkeypatch, tmp_path,
):
    from backend import main
    from backend.release_reconciliation import ReleaseReconciler

    service = ReleaseReconciler(
        main.monitor,
        state_path=tmp_path / "release_reconciliation.json",
    )
    manifest = _release_manifest()
    service.replace_intent(manifest)
    service.activate(manifest["release_id"], genstudio_run_reference=None)
    executed = []
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def run(release_id):
        executed.append(release_id)
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(service, "run", run)
    resumed = await service.start()
    await asyncio.wait_for(started.wait(), timeout=1)
    due_task = service._due_task
    await service.stop()

    assert resumed == {"managed_updates": 0, "release_jobs": 1}
    assert executed == [manifest["release_id"]]
    assert cancelled.is_set()
    assert due_task is not None and due_task.cancelled()


@pytest.mark.asyncio
async def test_recovered_peer_wakes_one_release_pass_without_duplicate_execution(
    monkeypatch, tmp_path,
):
    from backend.release_reconciliation import ReleaseReconciler

    class Monitor:
        registry = [
            {"id": "voice", "modality": "voice", "machine": "local"},
            {"id": "voice@mac-a", "modality": "voice", "machine": "mac-a"},
            {"id": "voice@mac-b", "modality": "voice", "machine": "mac-b"},
        ]

    service = ReleaseReconciler(
        Monitor(), state_path=tmp_path / "release_reconciliation.json",
    )
    manifest = _release_manifest()
    service.replace_intent(manifest)
    job_id = service.activate(
        manifest["release_id"], genstudio_run_reference=None,
    )["id"]
    service.record_component(job_id, "mac-a", "voice", state="pending_offline")
    service.record_component(job_id, "mac-b", "voice", state="pending_offline")
    calls = []
    started = asyncio.Event()

    async def run(release_id):
        calls.append(release_id)
        started.set()
        return service.job_snapshot(job_id)

    monkeypatch.setattr(service, "run", run)
    assert service.wake_peer("mac-b") == 1
    assert service.wake_peer("mac-b") == 1
    await asyncio.wait_for(started.wait(), timeout=1)

    snapshot = service.job_snapshot(job_id)
    assert calls == [manifest["release_id"]]
    assert snapshot["machines"]["mac-b"]["components"]["voice"]["state"] == "checking"
    assert snapshot["machines"]["mac-a"]["components"]["voice"]["state"] == "pending_offline"
    await service.stop()


def test_reported_version_is_snapshot_of_loaded_process(tmp_path, monkeypatch):
    from backend import main

    monkeypatch.setattr(main, "LAUNCHER_ROOT", tmp_path)
    (tmp_path / "VERSION").write_text("99.0.0")
    assert main._read_app_version() == "99.0.0"
    assert main._app_version() == main.APP_VERSION
    assert main._app_version() != "99.0.0"


def test_hub_health_and_studios(authed):
    hh = authed.get("/api/hub/health").json()
    assert hh["studios_total"] == 2 and hh["studios_up"] == 0
    studios = authed.get("/api/hub/studios").json()["studios"]
    assert len(studios) == 2
    assert all("machine_label" in s for s in studios)


def test_voice_provider_aggregation_is_retired(authed):
    """Voice Studio dropped /api/providers, so the Hub no longer aggregates it.

    The endpoint is gone rather than returning a permanently empty result, and
    neither the summary nor the resource snapshot carries provider health.
    """
    from backend import main

    assert authed.get("/api/hub/providers").status_code == 404
    assert not hasattr(main.monitor, "provider_health")
    assert not hasattr(main.monitor, "get_provider_health")
    assert not hasattr(main, "_cloud_provider_inventory")

    summary = authed.get("/api/hub/summary").json()
    assert "cloud_providers" not in summary
    assert all("cloud_providers" not in s for s in summary["studios"])
    resources = authed.get("/api/hub/resources").json()
    assert all("cloud_providers" not in (row or {})
               for row in resources["studios"].values())



def test_render_asset_stream_round_trip(authed):
    payload = b"render-input" * 100
    checksum = __import__("hashlib").sha256(payload).hexdigest()
    uploaded = authed.post(
        "/api/hub/render-assets", content=payload,
        headers={"X-File-Name": "scene.mp4", "X-Content-SHA256": checksum})
    assert uploaded.status_code == 200
    result = uploaded.json()
    assert result["bytes"] == len(payload) and result["sha256"] == checksum
    downloaded = authed.get(result["path"])
    assert downloaded.content == payload
    retained = authed.get(f"/api/hub/render-assets/by-sha/{checksum}?extension=.mp4")
    assert retained.status_code == 200
    assert retained.json()["asset_id"] == result["asset_id"]
    # Uploading the same immutable bytes is a no-op instead of a second file.
    duplicate = authed.post(
        "/api/hub/render-assets", content=payload,
        headers={"X-File-Name": "scene.mp4", "X-Content-SHA256": checksum})
    assert duplicate.status_code == 200
    assert duplicate.json()["asset_id"] == result["asset_id"]
    assert authed.delete(result["path"]).status_code == 409


def test_render_asset_rejects_unsafe_type(authed):
    response = authed.post(
        "/api/hub/render-assets", content=b"bad",
        headers={"X-File-Name": "script.sh"})
    assert response.status_code == 415


def test_update_status(authed):
    d = authed.get("/api/update-status").json()
    assert "app_version" in d and "update_available" in d


def test_stats_empty(authed):
    d = authed.get("/api/hub/stats").json()
    assert d["total"] == 0 and d["by_machine"] == {}
    assert d["fleet_activity"]["machines"]
    assert "by_lane" not in d
    assert "lane" not in d["filters"]


def test_models_empty_when_all_down(authed):
    d = authed.get("/api/hub/models").json()
    assert d["count"] == 0
    assert "lanes" not in d and "providers" not in d


def test_transcription_empty_when_all_voice_studios_down(authed):
    d = authed.get("/api/hub/transcription").json()
    assert d["available"] is False
    assert d["models"] == []
    assert d["endpoint_count"] == 0


def test_transcription_gateway_routes_with_studio_auth(authed, monkeypatch):
    import time
    from backend import main, peers

    main.monitor.status["voice"] = {"status": "up", "last_seen": time.time()}
    main.monitor._transcribe_cache["voice"] = (time.time(), {
        "available": True,
        "models": [{"repo": "mlx/whisper", "cached": True}],
    })
    captured = {}

    class Response:
        status_code = 200
        def json(self):
            return {"srt": "1\n00:00:00,000 --> 00:00:01,000\nHello\n"}

    async def post(url, **kwargs):
        captured.update(url=url, **kwargs)
        return Response()

    monkeypatch.setattr(main.monitor._client, "post", post)
    response = authed.post(
        "/api/hub/transcribe",
        data={"model": "mlx/whisper", "language": "en"},
        files={"file": ("clip.wav", b"audio", "audio/wav")},
    )
    assert response.status_code == 200
    assert captured["url"].endswith("/api/transcribe")
    voice = next(s for s in main.monitor.registry if s["id"] == "voice")
    assert captured["headers"] == peers.studio_headers(voice)
    assert main._transcription_busy == set()


def test_fleet_get_set(authed):
    assert authed.get("/api/hub/fleet").json()["fleet_token_set"] is True
    response = authed.post("/api/hub/fleet", json={"token": "a-valid-fleet-token"})
    assert response.json()["ok"] is True
    assert authed.get("/api/hub/fleet").json()["fleet_token_set"] is True
    from backend import peers
    import stat
    assert stat.S_IMODE(peers.FLEET_TOKEN_FILE.stat().st_mode) == 0o600


def test_owner_session_can_reveal_tokens_but_machine_token_cannot(app, token):
    from starlette.testclient import TestClient
    from backend import auth, peers

    peers.set_fleet_token("owner-fleet-secret")
    machine = TestClient(app, client=("100.66.3.3", 50000),
                         headers={"X-Hub-Token": token})
    assert "token" not in machine.get("/api/hub/access").json()
    assert "token" not in machine.get("/api/hub/fleet").json()

    owner = TestClient(app, client=("100.66.3.3", 50000))
    owner.cookies.set(auth.SESSION_COOKIE_NAME, auth.create_browser_session())
    assert owner.get("/api/hub/access").json()["token"] == token
    assert owner.get("/api/hub/fleet").json()["token"] == "owner-fleet-secret"


def test_fleet_save_rejects_ambiguous_short_credentials(authed):
    response = authed.post("/api/hub/fleet", json={"token": "short"})
    assert response.status_code == 400


def test_update_route_schedules_on_event_loop(authed, monkeypatch):
    from backend import fleet_ops

    async def finish(mon, job):
        job["status"] = "complete"
        job["finished_at"] = 1

    monkeypatch.setattr(fleet_ops, "_run_updates", finish)
    response = authed.post("/api/hub/maintenance/updates", json={"studio_ids": ["image"]})
    assert response.status_code == 200
    assert response.json()["id"] in fleet_ops._updates


def test_update_route_refreshes_published_releases_before_scheduling(authed, monkeypatch):
    from backend import fleet_ops

    refreshed = []

    async def refresh(*, force=False):
        refreshed.append(force)
        return {"versions": {"voice": "2.7.0"}}

    async def finish(mon, job):
        job["status"] = "complete"
        job["finished_at"] = 1

    monkeypatch.setattr(fleet_ops, "refresh_published_versions", refresh)
    monkeypatch.setattr(fleet_ops, "_run_updates", finish)

    response = authed.post(
        "/api/hub/maintenance/updates", json={"studio_ids": ["voice"]},
    )

    assert response.status_code == 200
    assert refreshed == [True]


def test_update_route_refuses_a_stale_target_when_forced_refresh_fails(authed, monkeypatch):
    from backend import fleet_ops

    async def refresh(*, force=False):
        return {
            "versions": {"voice": "2.6.1"},
            "errors": {"voice": "GitHub timed out"},
        }

    monkeypatch.setattr(fleet_ops, "refresh_published_versions", refresh)

    response = authed.post(
        "/api/hub/maintenance/updates", json={"studio_ids": ["voice"]},
    )

    assert response.status_code == 409
    assert "freshly verify" in response.json()["detail"]
    assert not fleet_ops._updates


def test_asset_upload_limits_and_types(authed, monkeypatch):
    from backend import main
    ok = authed.post("/api/hub/assets/upload", files={"file": ("ref.png", b"png", "image/png")})
    assert ok.status_code == 200 and ok.json()["bytes"] == 3
    bad = authed.post("/api/hub/assets/upload", files={"file": ("ref.svg", b"<svg/>", "image/svg+xml")})
    assert bad.status_code == 415
    monkeypatch.setattr(main, "_MAX_IMAGE_UPLOAD_BYTES", 2)
    large = authed.post("/api/hub/assets/upload", files={"file": ("large.png", b"123", "image/png")})
    assert large.status_code == 413


def test_registry_add_rename_remove(authed):
    r = authed.post("/api/hub/registry/add",
                    json={"host": "100.9.9.9", "machine": "mac-z",
                          "modalities": ["image", "voice"]})
    assert r.json()["registered"] == 2
    studios = authed.get("/api/hub/studios").json()["studios"]
    assert any(s["id"] == "image@mac-z" for s in studios)
    # rename (label alias) — key stays, label changes
    authed.post("/api/hub/registry/machines/mac-z/name", json={"name": "Zeta"})
    studios = authed.get("/api/hub/studios").json()["studios"]
    z = next(s for s in studios if s["machine"] == "mac-z")
    assert z["machine_label"] == "Zeta" and z["id"] == "image@mac-z"
    # the same encoded id used by the dashboard controls a remote app only
    paused = authed.post(
        "/api/hub/registry/studios/image%40mac-z/enabled", json={"enabled": False})
    assert paused.status_code == 200 and paused.json()["studio"] == "image@mac-z"
    studios = authed.get("/api/hub/studios").json()["studios"]
    assert next(s for s in studios if s["id"] == "image@mac-z")["enabled"] is False
    assert next(s for s in studios if s["id"] == "voice@mac-z")["enabled"] is True
    # remove
    assert authed.request("DELETE", "/api/hub/registry/machines/mac-z").json()["removed"] == 2


def test_registry_add_immediately_supplements_active_managed_release(
    authed, monkeypatch,
):
    from backend import main

    calls = []

    class ReleaseService:
        def wake_registry(self):
            calls.append("wake_registry")
            return 1

    service = ReleaseService()
    monkeypatch.setattr(main, "release_reconciler", service)

    response = authed.post("/api/hub/registry/add", json={
        "host": "100.64.0.90",
        "machine": "managed-agent-90",
        "modalities": ["image", "voice"],
    })

    assert response.status_code == 200
    assert calls == ["wake_registry"]


def test_machine_reenable_immediately_reconciles_active_managed_release(
    authed, monkeypatch,
):
    from backend import main

    calls = []

    class ReleaseService:
        def wake_registry(self):
            calls.append("wake_registry")
            return 1

    monkeypatch.setattr(main, "release_reconciler", ReleaseService())

    disabled = authed.post(
        "/api/hub/registry/machines/local/enabled", json={"enabled": False},
    )
    assert disabled.status_code == 200
    assert calls == []

    response = authed.post(
        "/api/hub/registry/machines/local/enabled", json={"enabled": True},
    )

    assert response.status_code == 200
    assert calls == ["wake_registry"]


def test_remote_placeholder_name_uses_stable_enrollment_hostname(authed):
    import time
    from backend import hardware_profiles, peers, registry as reg

    machine = "macmini-m4-16gb-terranash-0209-49f38d3b-hub"
    response = authed.post("/api/hub/registry/add", json={
        "host": "100.89.30.5", "machine": machine,
        "modalities": ["image", "voice"],
    })
    assert response.status_code == 200
    reg.set_label(machine, "local")
    peers._cache[machine] = (time.time(), {
        "reachable": True, "status": "connected", "studios": {},
        "host": {"chip": "Apple M4", "total_gb": 17.18},
    })
    hardware_profiles.set_machine_hardware_profile(machine, "mac-mini-m4-16gb")

    studios = authed.get("/api/hub/studios").json()["studios"]

    assert {row["machine_label"] for row in studios if row["machine"] == machine} == {
        "terranash-0209"
    }


def test_remote_machine_name_sort_uses_visible_labels():
    dashboard = (Path(__file__).parents[1] / "frontend" / "index.html").read_text()
    start = dashboard.index("function compareMachineEntries(")
    end = dashboard.index("function renderMachines()", start)
    helper = dashboard[start:end]
    script = """
const labels = {"a-hidden-id": "Zulu", "z-hidden-id": "Alpha", local: "Controller"};
const mlabel = machine => labels[machine] || machine;
const machSort = "status";
""" + helper + """
const rows = [{m: "a-hidden-id", up: 2}, {m: "z-hidden-id", up: 2}];
console.log(JSON.stringify({
  name: rows.slice().sort((a, b) => compareMachineEntries(a, b, "name")).map(row => row.m),
  studios: rows.slice().sort((a, b) => compareMachineEntries(a, b, "studios")).map(row => row.m),
  status: rows.slice().sort((a, b) => compareMachineEntries(a, b, "status")).map(row => row.m),
  local: [...rows, {m: "local", up: 0}].sort((a, b) => compareMachineEntries(a, b, "name")).map(row => row.m),
}));
"""
    result = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True,
    )
    sorted_rows = json.loads(result.stdout)

    assert sorted_rows["name"] == ["z-hidden-id", "a-hidden-id"]
    assert sorted_rows["studios"] == sorted_rows["name"]
    assert sorted_rows["status"] == sorted_rows["name"]
    assert sorted_rows["local"][0] == "local"


def test_studio_scheduler_toggle_is_reported_without_interrupting_work(authed):
    from backend import broker, registry as reg

    broker._busy.add("image")
    response = authed.post(
        "/api/hub/registry/studios/image/enabled", json={"enabled": False})

    assert response.status_code == 200
    assert response.json() == {
        "ok": True, "studio": "image", "machine": "local", "enabled": False,
    }
    image = next(row for row in authed.get("/api/hub/studios").json()["studios"]
                 if row["id"] == "image")
    assert image["enabled"] is False
    assert image["machine_enabled"] is True
    assert "image" in broker._busy  # scheduler pause never cancels active work
    assert reg.studio_enabled("local", "image") is False


def test_studio_scheduler_toggle_validates_target_and_boolean(authed):
    assert authed.post(
        "/api/hub/registry/studios/missing/enabled", json={"enabled": False}
    ).status_code == 404
    assert authed.post(
        "/api/hub/registry/studios/image/enabled", json={"enabled": "false"}
    ).status_code == 400


def test_remove_machine_purges_live_inventory_and_update_state(authed):
    import time
    from backend import fleet_ops, peers, registry as reg
    from backend.main import monitor

    authed.post("/api/hub/registry/add",
                json={"host": "100.9.9.8", "machine": "mac-clean",
                      "modalities": ["image", "voice"]})
    studio_ids = {"image@mac-clean", "voice@mac-clean"}
    reg.set_label("mac-clean", "Cleanup Mac")
    reg.set_machine_enabled("mac-clean", False)
    reg.set_studio_enabled("mac-clean", "image@mac-clean", False)
    for studio_id in studio_ids:
        monitor._catalog_cache[studio_id] = (time.time(), {"models": []})
        monitor._transcribe_cache[studio_id] = (time.time(), {"models": []})
    peers._cache["mac-clean"] = (time.time(), {"reachable": True})
    fleet_ops._studio_versions = {
        "checked_at": time.time(),
        "studios": [{"id": studio_id, "machine": "mac-clean"}
                    for studio_id in studio_ids],
    }
    fleet_ops._hub_versions["mac-clean"] = {"version": "1.0.0"}

    response = authed.delete("/api/hub/registry/machines/mac-clean")

    assert response.status_code == 200
    assert not studio_ids.intersection({row["id"] for row in monitor.registry})
    assert not studio_ids.intersection(monitor.status)
    assert not studio_ids.intersection(monitor._catalog_cache)
    assert not studio_ids.intersection(monitor._transcribe_cache)
    assert "mac-clean" not in peers._cache
    assert "mac-clean" not in reg.load_labels()
    assert "mac-clean" not in reg.load_flags()
    assert fleet_ops._studio_versions["studios"] == []
    assert "mac-clean" not in fleet_ops._hub_versions


def test_remove_machine_removes_the_active_stats_row_but_keeps_historical_stats(authed):
    """Deleting a controller registration must not erase retained evidence."""
    import time
    from backend import control_plane, ledger
    from backend.main import monitor

    authed.post("/api/hub/registry/add", json={
        "host": "100.9.9.8", "machine": "controller-0300-m4",
        "modalities": ["voice"],
    }).raise_for_status()
    receipt = time.time()
    ledger.record_activity_event(
        machine="controller-0300-m4", studio="voice@controller-0300-m4",
        job_id="retained-subtitle", state="done", model="org/whisper",
        operation="transcription", source="direct", started_at=receipt - 5,
        finished_at=receipt, runtime_s=5, observed_at=receipt,
    )

    before = authed.get("/api/hub/stats").json()
    assert "controller-0300-m4" in {
        row["machine"] for row in before["fleet_activity"]["machines"]
    }
    assert before["total"] == 1
    assert before["by_machine"]["controller-0300-m4"]["count"] == 1
    assert before["available_machines"] == ["controller-0300-m4"]

    response = authed.delete("/api/hub/registry/machines/controller-0300-m4")

    assert response.status_code == 200
    removed = response.json()
    settings = control_plane.load_settings()
    assert removed.get("machine") == "controller-0300-m4"
    assert removed.get("controller_id") == settings["controller_id"]
    assert removed.get("site_id") == settings["site_id"]
    assert isinstance(removed.get("epoch_closed_at"), float)
    assert removed.get("registry_absent") is True
    assert "controller-0300-m4" not in {
        row["machine"] for row in monitor.registry
    }
    after = authed.get("/api/hub/stats").json()
    assert "controller-0300-m4" not in {
        row["machine"] for row in after["fleet_activity"]["machines"]
    }
    assert after["total"] == 1
    assert after["by_machine"]["controller-0300-m4"]["count"] == 1
    assert after["available_machines"] == ["controller-0300-m4"]


def test_readding_a_removed_machine_starts_an_active_stats_epoch(authed):
    """A new registration cannot display the stable ID's old live evidence."""
    import time
    from backend import ledger
    from backend.main import monitor

    machine = "controller-0300-reenrolled"
    authed.post("/api/hub/registry/add", json={
        "host": "100.9.9.8", "machine": machine, "modalities": ["voice"],
    }).raise_for_status()
    receipt = time.time()
    ledger.record_activity_event(
        machine=machine, studio=f"voice@{machine}", job_id="retained-subtitle",
        state="done", model="org/whisper", operation="transcription",
        source="direct", started_at=receipt - 5, finished_at=receipt,
        runtime_s=5, observed_at=receipt,
    )

    authed.delete(f"/api/hub/registry/machines/{machine}").raise_for_status()
    authed.post("/api/hub/registry/add", json={
        "host": "100.9.9.9", "machine": machine, "modalities": ["voice"],
    }).raise_for_status()
    monitor.status[f"voice@{machine}"].update(
        status="up", activity_support="available",
    )

    active = authed.get("/api/hub/stats").json()["fleet_activity"]["machines"]
    row = next(item for item in active if item["machine"] == machine)

    assert row["state"] == "ready"
    assert row["completed"] == 0
    assert row["latest"] is None
    assert row["timeline"] == []
    historical = authed.get("/api/hub/stats").json()
    assert historical["total"] == 1
    assert historical["by_machine"][machine]["count"] == 1


def test_reenrollment_epoch_uses_controller_receipt_not_worker_clock(authed):
    """A skewed worker timestamp cannot carry an old event into a new epoch."""
    import time
    from backend import ledger
    from backend.main import monitor

    machine = "controller-0300-clock-skew"
    authed.post("/api/hub/registry/add", json={
        "host": "100.9.9.8", "machine": machine, "modalities": ["voice"],
    }).raise_for_status()
    old_receipt = time.time()
    ledger.record_activity_event(
        machine=machine, studio=f"voice@{machine}", job_id="skewed-subtitle",
        state="done", model="org/whisper", operation="transcription",
        source="direct", started_at=old_receipt - 5, finished_at=old_receipt,
        runtime_s=5, observed_at=old_receipt, activity_received_at=old_receipt,
        reported_at=old_receipt + 86400,
    )

    authed.delete(f"/api/hub/registry/machines/{machine}").raise_for_status()
    authed.post("/api/hub/registry/add", json={
        "host": "100.9.9.9", "machine": machine, "modalities": ["voice"],
    }).raise_for_status()
    monitor.status[f"voice@{machine}"].update(
        status="up", activity_support="available",
    )

    rows = authed.get("/api/hub/stats").json()["fleet_activity"]["machines"]
    row = next(item for item in rows if item["machine"] == machine)

    assert row["state"] == "ready"
    assert row["completed"] == 0
    assert row["latest"] is None
    assert row["timeline"] == []


def test_reenrollment_replay_keeps_the_old_terminal_event_out_of_active_stats(authed):
    """A replay refreshes evidence, never the terminal event's receipt identity."""
    import time
    from backend import activity
    from backend.main import monitor

    machine = "controller-0300-terminal-replay"
    studio_id = f"voice@{machine}"
    authed.post("/api/hub/registry/add", json={
        "host": "100.9.9.8", "machine": machine, "modalities": ["voice"],
    }).raise_for_status()
    initial_receipt = time.time()
    terminal = {
        "id": "replayed-terminal", "state": "done", "model": "org/whisper",
        "operation": "transcription", "source": "direct", "progress": 1.0,
        "created_at": initial_receipt - 10, "started_at": initial_receipt - 5,
        "updated_at": initial_receipt, "finished_at": initial_receipt,
        "runtime_s": 5.0,
    }
    statuses = {studio_id: {
        "status": "up", "activity_support": "available",
        "activity_received_at": initial_receipt,
        "activity": {"schema": activity.SCHEMA, "studio": "voice",
                     "observed_at": initial_receipt, "active": None,
                     "latest": terminal},
    }}
    activity.observe_poll(monitor.registry, statuses, {}, now=initial_receipt)

    authed.delete(f"/api/hub/registry/machines/{machine}").raise_for_status()
    authed.post("/api/hub/registry/add", json={
        "host": "100.9.9.9", "machine": machine, "modalities": ["voice"],
    }).raise_for_status()
    replay_receipt = time.time()
    statuses[studio_id]["activity_received_at"] = replay_receipt
    activity.observe_poll(monitor.registry, statuses, {}, now=replay_receipt)

    snapshot = activity.fleet_snapshot(
        monitor.registry, statuses, {}, since_s=0.0,
        now=replay_receipt + activity.LONG_IDLE_S + 1,
    )
    row = next(item for item in snapshot["machines"] if item["machine"] == machine)

    assert row["state"] == "ready"
    assert row["latest"] is None
    assert row["completed"] == 0


def test_cannot_remove_local(authed):
    assert authed.request("DELETE", "/api/hub/registry/machines/local").status_code == 400


def test_jobs_submit_list_get_cancel(authed):
    r = authed.post("/api/hub/jobs", json={"modality": "image", "model": "a/b",
                                           "items": [{"prompt": "x"}]})
    bid = r.json()["batch_id"]
    assert any(b["id"] == bid for b in authed.get("/api/hub/jobs").json()["batches"])
    got = authed.get(f"/api/hub/jobs/{bid}").json()
    assert got["total"] == 1 and got["items"][0]["prompt"] == "x"
    cancelled = authed.request("DELETE", f"/api/hub/jobs/{bid}").json()
    assert cancelled["ok"] is True and cancelled["queued_cancelled"] == 1
    cleared = authed.post(f"/api/hub/jobs/{bid}/clear").json()
    assert cleared["ok"] is True and cleared["cleared"] == 1
    assert authed.get(f"/api/hub/jobs/{bid}").status_code == 404
    assert authed.get("/api/hub/jobs/does-not-exist").status_code == 404


def test_finished_jobs_remain_in_list_after_broker_memory_is_cleared(authed):
    from backend import broker, ledger

    batch = {
        "id": "durable-finished", "created_at": 123.0, "modality": "image",
        "model": "org/model", "cancelled": False,
        "items": [{"index": 0, "state": "done", "tries": 1}],
    }
    ledger.save_batch(batch)
    broker.batches.pop(batch["id"], None)

    listed = authed.get("/api/hub/jobs").json()["batches"]
    summary = authed.get("/api/hub/summary").json()["jobs"]
    exact = authed.get(f"/api/hub/jobs/{batch['id']}").json()

    assert [row["id"] for row in listed] == [batch["id"]]
    assert summary == []
    assert exact["id"] == batch["id"]
    assert exact["items"][0]["state"] == "done"


def test_legacy_terminal_job_item_exposes_null_execution_started_at(authed):
    from backend import broker, ledger

    batch = {
        "id": "legacy-terminal", "created_at": 123.0, "modality": "image",
        "model": "org/model", "cancelled": False,
        "items": [{"index": 0, "state": "done", "tries": 1}],
    }
    ledger.save_batch(batch)
    broker.batches.pop(batch["id"], None)

    item = authed.get(f"/api/hub/jobs/{batch['id']}").json()["items"][0]

    assert item["execution_started_at"] is None


def test_ledger_reloaded_job_item_exposes_execution_started_at(authed):
    from backend import broker, ledger

    batch = {
        "id": "execution-started", "created_at": 123.0, "modality": "image",
        "model": "org/model", "cancelled": False,
        "items": [{
            "index": 0, "state": "done", "tries": 1,
            "execution_started_at": 55.25,
        }],
    }
    ledger.save_batch(batch)
    broker.batches.pop(batch["id"], None)

    item = authed.get(f"/api/hub/jobs/{batch['id']}").json()["items"][0]

    assert item["execution_started_at"] == 55.25


def test_genstudio_execution_lease_renews_through_authenticated_api(client, authed):
    from datetime import UTC, datetime, timedelta

    from backend import broker, control_plane

    control_plane.save_settings({
        "role": "controller",
        "site_id": "site-a",
        "site_name": "Site A",
        "controller_id": "controller-a",
        "database_mode": "off",
    })
    initial_deadline = datetime.now(UTC) + timedelta(minutes=5)
    submitted = authed.post("/api/hub/jobs", json={
        "modality": "voice",
        "model": "org/qwen-tts",
        "items": [{"text": "Qualification lease contract"}],
        "genstudio_execution": {
            "genstudio_job_id": "job-qualification",
            "genstudio_attempt_id": "attempt-qualification",
            "idempotency_key": "qualification-lease-contract",
            "fencing_token": 1,
            "site_id": "site-a",
            "operation": "voice.tts",
            "model_revision": "model-sha-1",
            "voice_revision": "voice-sha-1",
            "lease_expires_at": initial_deadline.isoformat(),
        },
    })
    assert submitted.status_code == 200
    batch_id = submitted.json()["batch_id"]
    renewed_deadline = datetime.now(UTC) + timedelta(minutes=10)
    body = {
        "genstudio_job_id": "job-qualification",
        "genstudio_attempt_id": "attempt-qualification",
        "fencing_token": 1,
        "lease_expires_at": renewed_deadline.isoformat(),
    }

    assert client.post("/api/hub/executions/leases", json=body).status_code == 401
    renewed = authed.post("/api/hub/executions/leases", json=body)

    assert renewed.status_code == 200
    assert renewed.json()["lease_expires_at"] == renewed_deadline.isoformat()
    assert (
        broker.batches[batch_id]["genstudio_execution"]["lease_expires_at"]
        == renewed_deadline.isoformat()
    )


def test_bulk_image_cancel_and_clear_do_not_touch_other_modalities(authed):
    image = authed.post("/api/hub/jobs", json={
        "modality": "image", "model": "a/b", "items": [{"prompt": "image"}],
    }).json()["batch_id"]
    voice = authed.post("/api/hub/jobs", json={
        "modality": "voice", "model": "c/d", "items": [{"text": "voice"}],
    }).json()["batch_id"]

    cancelled = authed.post("/api/hub/jobs/cancel", json={"modality": "image"}).json()
    assert cancelled["batches_cancelled"] == 1
    assert authed.get(f"/api/hub/jobs/{image}").json()["cancelled"] is True
    assert authed.get(f"/api/hub/jobs/{voice}").json()["queued"] == 1

    cleared = authed.post("/api/hub/jobs/clear", json={"modality": "image"}).json()
    assert cleared["cleared"] == 1
    assert authed.get(f"/api/hub/jobs/{image}").status_code == 404
    assert authed.get(f"/api/hub/jobs/{voice}").status_code == 200


def test_jobs_bad_modality_400(authed):
    r = authed.post("/api/hub/jobs", json={"modality": "nope", "model": "a/b",
                                           "items": [{"prompt": "x"}]})
    assert r.status_code == 400


def test_watchdog_toggle(authed):
    r = authed.post("/api/hub/studios/image/watchdog", json={"enabled": True})
    assert r.json()["watchdog"]["enabled"] is True
    assert authed.post("/api/hub/studios/bogus/watchdog", json={"enabled": True}).status_code == 404


def test_update_status_never_calls_pulled_code_loaded(monkeypatch, authed):
    from backend import main

    monkeypatch.setattr(main.auto_updater, "public_status", lambda: {
        "installed_version": "9.9.9", "state": "succeeded",
        "last_update_result": "Updated successfully",
    })
    response = authed.get("/api/auto-update/status")
    assert response.status_code == 200
    payload = response.json()
    assert payload["installed_version"] == main.APP_VERSION
    assert payload["loaded_version"] == main.APP_VERSION
    assert payload["disk_version"] == "9.9.9"
    assert payload["state"] == "restart_required"
    assert payload["restart_required"] is True


def test_fleet_update_inventory_uses_loaded_hub_version(monkeypatch, authed):
    from backend import main

    async def snapshot():
        return {"apps": [{"id": "hub@local", "kind": "hub",
                           "installed_version": "9.9.9", "state": "succeeded"}]}

    monkeypatch.setattr(main.fleet_auto_updates, "snapshot", snapshot)
    monkeypatch.setattr(main.auto_updater, "public_status", lambda: {
        "installed_version": "9.9.9", "state": "succeeded",
    })
    payload = authed.get("/api/hub/auto-updates").json()
    assert payload["apps"][0]["installed_version"] == main.APP_VERSION
    assert payload["apps"][0]["disk_version"] == "9.9.9"
    assert payload["apps"][0]["state"] == "restart_required"


def test_hub_restart_schedules_installed_service(monkeypatch, authed):
    from backend import control, main

    monkeypatch.setattr(main.auto_updater, "restart_safety", lambda: {
        "ready": True, "mode": "service", "expected_version": "1.62.0",
        "commit": "a" * 40,
    })
    monkeypatch.setattr(main, "_automatic_update_blockers", lambda: [])
    monkeypatch.setattr(control, "restart_hub_service", lambda: {
        "ok": True, "state": "restarting",
        "service": "com.kh.studiohub.server", "delay_seconds": 1.5,
    })
    response = authed.post("/api/hub/maintenance/restart", json={"force": False})
    assert response.status_code == 202
    assert response.json()["expected_version"] == "1.62.0"
    assert response.json()["service"] == "com.kh.studiohub.server"
    assert response.json()["forced"] is False


def test_hub_restart_protects_active_work_unless_explicitly_forced(monkeypatch, authed):
    from backend import control, main

    monkeypatch.setattr(main.auto_updater, "restart_safety", lambda: {
        "ready": True, "mode": "service", "expected_version": "1.62.0",
        "commit": "a" * 40,
    })
    monkeypatch.setattr(main, "_automatic_update_blockers",
                        lambda: ["image generation is running"])
    scheduled = []
    monkeypatch.setattr(control, "restart_hub_service", lambda: (
        scheduled.append(True) or {
            "ok": True, "state": "restarting",
            "service": "com.kh.studiohub.server", "delay_seconds": 1.5,
        }
    ))
    refused = authed.post("/api/hub/maintenance/restart", json={"force": False})
    assert refused.status_code == 409
    assert "Active work prevents" in refused.json()["detail"]
    assert scheduled == []

    forced = authed.post("/api/hub/maintenance/restart", json={"force": True})
    assert forced.status_code == 202
    assert forced.json()["forced"] is True
    assert forced.json()["active_work"] == ["image generation is running"]
    assert scheduled == [True]


def test_hub_restart_cannot_bypass_repository_safety(monkeypatch, authed):
    from backend import control, main

    def unsafe():
        raise main.UpdateError("Working tree has local changes")

    monkeypatch.setattr(main.auto_updater, "restart_safety", unsafe)
    monkeypatch.setattr(
        control, "restart_hub_service",
        lambda: (_ for _ in ()).throw(AssertionError("unsafe restart was scheduled")),
    )
    response = authed.post("/api/hub/maintenance/restart", json={"force": True})
    assert response.status_code == 409
    assert "local changes" in response.json()["detail"]


def test_settings_body_carries_drain_timeout_without_resetting_it():
    """An older client that omits the field must not wipe the owner's value."""
    from backend.main import AutoUpdateSettingsBody

    legacy = AutoUpdateSettingsBody(mode="off", frequency="daily", maintenance_hour=1)
    assert "drain_timeout_minutes" not in legacy.model_dump(exclude_none=True)

    explicit = AutoUpdateSettingsBody(mode="off", frequency="daily",
                                      maintenance_hour=1, drain_timeout_minutes=90)
    assert explicit.model_dump(exclude_none=True)["drain_timeout_minutes"] == 90
