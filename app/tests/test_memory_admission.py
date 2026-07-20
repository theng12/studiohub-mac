import json

from backend import broker, memory_admission


FLUX_REPO = "AITRADER/FLUX2-klein-4B-mlx-4bit"


def _flux_entry():
    return {
        "repo": FLUX_REPO,
        "label": "FLUX.2 klein 4B — MLX 4-bit",
        "min_unified_memory_gb": 16,
        "cache": {"state": "cached"},
    }


def test_flux_4bit_uses_measured_8gb_fleet_default(reset):
    policy = memory_admission.describe(FLUX_REPO, _flux_entry())

    assert policy["catalog_min_total_memory_gb"] == 16
    assert policy["default_min_total_memory_gb"] == 8
    assert policy["effective_min_total_memory_gb"] == 8
    assert policy["effective_min_free_memory_gb"] == 2
    assert policy["source"] == "fleet_default"
    assert policy["overridden"] is False
    requirements = broker._admission_requirements(FLUX_REPO, _flux_entry())
    assert requirements == {"min_total": 8, "min_free": 2}
    assert broker._memory_gate(
        requirements, {"total_gb": 8.59, "available_gb": 2.4})[0] == "run"


def test_unknown_model_keeps_catalog_default(reset):
    entry = {"repo": "org/other-model", "min_unified_memory_gb": 24}
    policy = memory_admission.describe(entry["repo"], entry)

    assert policy["effective_min_total_memory_gb"] == 24
    assert policy["effective_min_free_memory_gb"] == 2
    assert policy["source"] == "catalog"


def test_operator_override_persists_and_reset_restores_default(reset):
    entry = _flux_entry()
    changed = memory_admission.set_override(
        FLUX_REPO,
        min_total_memory_gb=7.5,
        min_free_memory_gb=1.5,
        catalog_entry=entry,
    )

    assert changed["source"] == "operator_override"
    assert changed["effective_min_total_memory_gb"] == 7.5
    assert changed["effective_min_free_memory_gb"] == 1.5
    assert broker._admission_requirements(FLUX_REPO, entry) == {
        "min_total": 7.5, "min_free": 1.5,
    }
    stored = json.loads(memory_admission.SETTINGS_FILE.read_text())
    assert stored[FLUX_REPO.lower()]["min_total_memory_gb"] == 7.5

    memory_admission.reset_for_tests()
    assert memory_admission.describe(FLUX_REPO, entry)[
        "effective_min_total_memory_gb"
    ] == 7.5

    reset_policy = memory_admission.reset_override(FLUX_REPO, entry)
    assert reset_policy["source"] == "fleet_default"
    assert reset_policy["effective_min_total_memory_gb"] == 8
    assert not memory_admission.SETTINGS_FILE.exists()


def test_qwen_production_floor_is_identified_as_hub_default(reset):
    repo = "mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-8bit"
    policy = memory_admission.describe(repo, {
        "repo": repo, "min_unified_memory_gb": 8,
    })

    assert policy["effective_min_total_memory_gb"] == 8
    assert policy["effective_min_free_memory_gb"] == 3.2
    assert policy["source"] == "fleet_default"
    assert policy["default_reason"] == "Hub production qualification"


def test_models_api_and_ram_admission_controls(seed_catalog, authed):
    seed_catalog("image", [_flux_entry()])

    model = authed.get("/api/hub/models?modality=image").json()["models"][0]
    assert model["memory_admission"]["catalog_min_total_memory_gb"] == 16
    assert model["memory_admission"]["effective_min_total_memory_gb"] == 8

    inventory = authed.get("/api/hub/memory-admission").json()
    assert inventory["default_min_free_memory_gb"] == 2
    assert inventory["policies"][0]["source"] == "fleet_default"

    saved = authed.put("/api/hub/memory-admission", json={
        "model": FLUX_REPO,
        "min_total_memory_gb": 8,
        "min_free_memory_gb": 2.5,
    })
    assert saved.status_code == 200
    assert saved.json()["policy"]["source"] == "operator_override"
    assert saved.json()["policy"]["effective_min_free_memory_gb"] == 2.5

    restored = authed.delete(
        "/api/hub/memory-admission", params={"model": FLUX_REPO})
    assert restored.status_code == 200
    assert restored.json()["policy"]["source"] == "fleet_default"


def test_ram_admission_rejects_unknown_cloud_and_unsafe_values(seed_catalog, authed):
    seed_catalog("image", [{
        "repo": "provider:test/cloud-image", "label": "Cloud",
        "is_cloud": True, "provider": "test",
    }])

    unknown = authed.put("/api/hub/memory-admission", json={
        "model": "missing/model", "min_total_memory_gb": 8,
        "min_free_memory_gb": 2,
    })
    cloud = authed.put("/api/hub/memory-admission", json={
        "model": "provider:test/cloud-image", "min_total_memory_gb": 8,
        "min_free_memory_gb": 2,
    })
    unsafe = authed.put("/api/hub/memory-admission", json={
        "model": "missing/model", "min_total_memory_gb": 1,
        "min_free_memory_gb": 0,
    })

    assert unknown.status_code == 404
    assert cloud.status_code == 400
    assert unsafe.status_code == 422
