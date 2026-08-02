import hashlib
import json
from pathlib import Path

import pytest

from backend import control_plane, model_baselines, model_exposure
from backend.main import monitor


def _voice(studio_id: str = "voice", machine: str = "test-machine") -> dict:
    return {
        "id": studio_id,
        "modality": "voice",
        "machine": machine,
        "host": "127.0.0.1",
        "port": 47870,
    }


def _audit(
    repo: str,
    *,
    operation: str = "voice.tts",
    revision: str = "a" * 40,
    contract_hash: str = "sha256:" + "b" * 64,
) -> dict:
    return {
        "schema": "studio.model-audit",
        "schema_version": 1,
        "audit_id": f"audit-{repo}",
        "audit_status": "passed",
        "candidate_for_genstudio": True,
        "contract_hash": contract_hash,
        "runtime_revision": revision,
        "approved_operations": [operation],
        "audited_at": "2026-08-02T00:00:00Z",
        "adapter": {"id": "mlx", "version": "1"},
        "controls": {},
        "input_limits": {},
        "output_limits": {},
        "capacity": {"max_concurrency": 1, "available_slots": 1},
        "hardware": {"minimum_unified_memory_gb": 8},
    }


def _worker_model(
    repo: str = "org/model",
    *,
    operation: str = "voice.tts",
    revision: str = "a" * 40,
    contract_hash: str = "sha256:" + "b" * 64,
    cache_state: str = "absent",
) -> dict:
    return {
        "repo": repo,
        "label": "Audited model",
        "cache": {"state": cache_state},
        "genstudio_candidate": _audit(
            repo,
            operation=operation,
            revision=revision,
            contract_hash=contract_hash,
        ),
    }


def _desired_model(
    repo: str = "org/model",
    *,
    operation: str = "voice.tts",
    revision: str = "a" * 40,
    contract_hash: str = "sha256:" + "b" * 64,
    memory_gb: int = 8,
    inventory: str = "catalog",
) -> dict:
    return {
        "candidate_key": model_exposure.exposure_key(
            repo, operation, revision, contract_hash
        ),
        "internal_model_id": repo,
        "display_name": "Audited model",
        "modality": "transcription" if inventory == "transcription" else "voice",
        "operation": operation,
        "runtime_revision": revision,
        "contract_hash": contract_hash,
        "sibling_studio": "voice",
        "inventory": inventory,
        "deployment": {
            "mode": "all_eligible",
            "minimum_unified_memory_gb": memory_gb,
        },
    }


def _catalog(*models: dict) -> dict:
    canonical = json.dumps(list(models), sort_keys=True, separators=(",", ":"))
    return {
        "schema": model_baselines.CATALOG_SCHEMA,
        "schema_version": model_baselines.CATALOG_SCHEMA_VERSION,
        "authority": "genstudio",
        "revision": hashlib.sha256(canonical.encode()).hexdigest(),
        "generated_at": "2026-08-02T00:00:00+00:00",
        "models": list(models),
    }


def test_model_baseline_runtime_state_is_ignored_by_git() -> None:
    root = Path(__file__).parents[2]
    ignored = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "model_baselines.json" in ignored


def test_controller_starts_without_a_hardcoded_model_baseline(authed, client):
    assert client.get("/api/hub/model-baselines").status_code == 401
    payload = authed.get("/api/hub/model-baselines").json()
    assert payload["authority"] == "awaiting_genstudio"
    assert payload["models"] == []
    assert payload["targets"] == []


def test_global_catalog_endpoint_requires_machine_auth_and_controller_role(
    client, authed, monkeypatch
):
    payload = _catalog(_desired_model())
    assert client.post("/api/hub/fleet-model-catalog", json=payload).status_code == 401
    assert authed.post("/api/hub/fleet-model-catalog", json=payload).status_code == 409

    control_plane.save_settings(
        {"role": "controller", "site_id": "site-a", "controller_id": "hub-a"}
    )
    monkeypatch.setattr(monitor, "registry", [_voice()])
    monkeypatch.setattr(model_baselines.FleetModelBaselines, "trigger_reconcile", lambda self: True)
    response = authed.post("/api/hub/fleet-model-catalog", json=payload)
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "accepted": True,
        "changed": True,
        "reconcile_scheduled": True,
        "revision": payload["revision"],
        "approved_models": 1,
    }
    assert model_exposure.global_authority_active()


def test_invalid_or_tampered_exact_contract_is_rejected(authed):
    control_plane.save_settings(
        {"role": "controller", "site_id": "site-a", "controller_id": "hub-a"}
    )
    model = _desired_model()
    model["candidate_key"] = "f" * 64
    response = authed.post("/api/hub/fleet-model-catalog", json=_catalog(model))
    assert response.status_code == 422
    assert "exact model key" in response.json()["detail"]


@pytest.mark.asyncio
async def test_reconcile_resumes_an_exact_partial_download(monkeypatch, authed):
    required = _desired_model()
    monitor.registry = [_voice()]
    monitor.status = {"voice": {"status": "up", "health": {"memory": {"total_gb": 8}}}}
    monitor._catalog_cache["voice"] = (0, {"models": []})
    from backend import main

    main.model_baselines.replace_catalog(_catalog(required))

    async def catalog(studio, force=False):
        return {"models": [_worker_model(cache_state="partial")]}

    async def transcription(studio, force=False):
        return {"models": []}

    posted = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"job": {"id": "download-resume", "state": "queued"}}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            posted.append(kwargs["json"])
            return Response()

    monkeypatch.setattr(monitor, "get_catalog", catalog)
    monkeypatch.setattr(monitor, "get_transcription", transcription)
    monkeypatch.setattr(model_baselines.httpx, "AsyncClient", lambda **kwargs: Client())
    monkeypatch.setattr(
        model_baselines.peers,
        "studio_request",
        lambda studio, path: ("http://voice/api/downloads", {}),
    )

    response = authed.post("/api/hub/model-baselines/reconcile")
    assert response.status_code == 200
    assert posted == [{"repo": "org/model"}]
    row = response.json()["targets"][0]
    assert row["state"] == "queued"
    assert row["job_id"] == "download-resume"


def test_ineligible_and_unknown_memory_are_never_downloaded(monkeypatch, authed):
    from backend import main

    monitor.registry = [_voice("voice@small", "small"), _voice("voice@unknown", "unknown")]
    monitor.status = {
        "voice@small": {"status": "up", "health": {"memory": {"total_gb": 8}}},
        "voice@unknown": {"status": "up"},
    }
    main.model_baselines.replace_catalog(_catalog(_desired_model(memory_gb=16)))

    async def catalog(studio, force=False):
        return {"models": []}

    async def transcription(studio, force=False):
        return {"models": []}

    monkeypatch.setattr(monitor, "get_catalog", catalog)
    monkeypatch.setattr(monitor, "get_transcription", transcription)
    monkeypatch.setattr(
        model_baselines.httpx,
        "AsyncClient",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("download not expected")),
    )
    response = authed.post("/api/hub/model-baselines/reconcile")
    assert response.status_code == 200
    states = {row["studio_id"]: row["state"] for row in response.json()["targets"]}
    assert states == {"voice@small": "ineligible", "voice@unknown": "eligibility_unknown"}


def test_revocation_stops_targeting_without_deleting_cached_or_partial_files(reset, tmp_path):
    class FakeMonitor:
        registry = [_voice()]
        status = {"voice": {"status": "down", "health": {"memory": {"total_gb": 8}}}}

    service = model_baselines.FleetModelBaselines(
        FakeMonitor(), state_path=tmp_path / "catalog.json"
    )
    first = _desired_model("org/first")
    second = _desired_model("org/second", revision="c" * 40)
    service.replace_catalog(_catalog(first, second))
    service.targets[service._target_key("voice", "org/second")] = {
        "state": "queued",
        "job_id": "partial-download",
    }
    service.replace_catalog(_catalog(first))

    assert [row["repo"] for row in service.models] == ["org/first"]
    assert all("org/second" not in key for key in service.targets)
    records = {row["internal_model_id"]: row for row in model_exposure.records()}
    assert records["org/second"]["state"] == "revoked"
    assert records["org/second"]["reason"].startswith("Not present")


def test_last_good_catalog_survives_controller_restart(reset, tmp_path):
    class FakeMonitor:
        registry = []
        status = {}

    path = tmp_path / "catalog.json"
    original = model_baselines.FleetModelBaselines(FakeMonitor(), state_path=path)
    payload = _catalog(_desired_model())
    original.replace_catalog(payload)
    restarted = model_baselines.FleetModelBaselines(FakeMonitor(), state_path=path)

    assert restarted.catalog_revision == payload["revision"]
    assert restarted.models[0]["repo"] == "org/model"


def test_automatic_catalog_can_be_paused_without_losing_desired_state(authed):
    from backend import main

    main.model_baselines.replace_catalog(_catalog(_desired_model()))
    response = authed.post("/api/hub/model-baselines", json={"enabled": False})
    assert response.status_code == 200
    assert response.json()["enabled"] is False
    assert response.json()["summary"]["approved_models"] == 1
