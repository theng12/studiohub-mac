"""Studio Hub structurally refuses cloud work that originates in GenStudio.

The guarantee under test is one-sided on purpose: it is keyed on the SOURCE of
the work, not on the modality. A local operator keeps full access to every
cloud lane the fleet offers, and Render — which flags ``is_cloud=true`` purely
to bypass the download/memory governor — must never be caught.
"""

import hashlib
import json

import pytest

from backend import (broker, chat_jobs, cloud_guard, control_plane,
                     model_baselines, model_exposure)


GENSTUDIO_EXECUTION = {
    "genstudio_job_id": "job-1",
    "genstudio_attempt_id": "attempt-1",
    "idempotency_key": "idem-1",
    "fencing_token": 1,
    "site_id": "site-a",
    "operation": "image.generate",
}


def _as_controller():
    control_plane.save_settings(
        {"role": "controller", "site_id": "site-a", "controller_id": "hub-a"}
    )


@pytest.fixture
def controller(reset):
    """A controller Hub whose site id matches the GenStudio assignments above."""
    _as_controller()


def _genstudio_envelope(**overrides):
    envelope = {
        "modality": "image",
        "model": "cloudflare/flux-1-schnell",
        "label": "genstudio-kh:story-studio-kh",
        "items": [{"prompt": "a stick figure waving"}],
        "genstudio_execution": dict(GENSTUDIO_EXECUTION),
    }
    envelope.update(overrides)
    return envelope


# ── origin detection ───────────────────────────────────────────────────────
def test_genstudio_origin_is_recognised_from_either_marker():
    assert cloud_guard.is_genstudio_origin(
        {"label": "genstudio-kh:story-studio-kh"}) is True
    assert cloud_guard.is_genstudio_origin(
        {"genstudio_execution": dict(GENSTUDIO_EXECUTION)}) is True
    assert cloud_guard.is_genstudio_origin(
        {"genstudio_job_id": "job-1"}) is True
    # A local operator carries neither marker.
    assert cloud_guard.is_genstudio_origin({"label": "storystudio"}) is False
    assert cloud_guard.is_genstudio_origin({}) is False
    assert cloud_guard.is_genstudio_origin(None) is False


# ── cloud detection ────────────────────────────────────────────────────────
@pytest.mark.parametrize("model", [
    "fal:fal-ai/kling-video/v2/master/text-to-video",
    "kie:kling-3.0/video",
    "replicate:lightricks/ltx-2-fast",
    "provider:elevenlabs:eleven_v3",
    "cloudflare/flux-1-schnell",
    "gemini/gemini-2.5-flash-image",
    "together/flux-1-schnell-free",
    "nebius/flux-dev",
    "pollinations/flux",
    "huggingface/sd3-medium",
])
def test_every_cloud_id_in_the_fleet_is_detected_from_the_id_alone(model):
    assert cloud_guard.cloud_reason(model) is not None


@pytest.mark.parametrize("model", [
    "fal/AuraFlow-v0.3",              # local image model; owner happens to be "fal"
    "black-forest-labs/FLUX.1-schnell",
    "mlx-community/Llama-3.2-3B-Instruct-4bit",
    "facebook/musicgen-small",
    "episode-assembly-v1",
])
def test_local_model_ids_are_not_mistaken_for_cloud(model):
    assert cloud_guard.cloud_reason(model) is None


def test_cloud_is_detected_when_is_cloud_is_absent_but_the_entry_says_cloud():
    """Voice Studio's provider entries carried is_cloud=None — testing the flag
    alone would have missed every one of them."""
    entry = {"repo": "some/model", "is_cloud": None,
             "cache": {"state": "cloud"}, "kind": "cloud",
             "provider": "elevenlabs"}
    assert cloud_guard.cloud_reason("some/model", entries=[entry]) is not None
    assert cloud_guard.cloud_reason(
        "some/model", entries=[{"repo": "some/model", "cache": {"state": "cloud"}}],
    ) is not None
    assert cloud_guard.cloud_reason(
        "some/model", entries=[{"repo": "some/model", "kind": "cloud"}],
    ) is not None
    assert cloud_guard.cloud_reason(
        "some/model", entries=[{"repo": "some/model", "provider": "fal"}],
    ) is not None


def test_a_local_catalog_entry_is_not_cloud():
    entry = {"repo": "org/model", "is_cloud": False, "provider": "local",
             "cache": {"state": "cached"}}
    assert cloud_guard.cloud_reason("org/model", entries=[entry]) is None


def test_render_is_never_cloud_despite_carrying_is_cloud_true():
    """Render Studio sets is_cloud=true on episode-assembly-v1 ONLY to bypass
    the broker's download/memory governor. It is local FFmpeg work on a fleet
    Mac and must never be refused."""
    entry = {"repo": "episode-assembly-v1", "is_cloud": True,
             "cache": {"state": "cached"},
             "capabilities": ["video-assembly"]}
    assert cloud_guard.cloud_reason(
        "episode-assembly-v1", modality="render", entries=[entry]) is None
    # Exempt by id as well, even if the modality were ever lost in transit.
    assert cloud_guard.cloud_reason("episode-assembly-v1") is None
    # LOCAL_ONLY_MODALITIES is the single source of that fact.
    assert "render" in cloud_guard.LOCAL_ONLY_MODALITIES


# ── the job door ───────────────────────────────────────────────────────────
def test_genstudio_cloud_submission_is_refused_at_the_door(reset):
    result = broker.submit_batch(_genstudio_envelope())

    assert result["code"] == cloud_guard.REFUSAL_CODE
    assert "cloudflare/flux-1-schnell" in result["error"]
    assert "does not accept cloud work from GenStudio" in result["error"]
    # Refused, not queued-then-failed: nothing was created at all.
    assert broker.batches == {}


def test_genstudio_cloud_submission_leaves_no_execution_identity_behind(controller):
    """The refusal runs before the fence is recorded, so GenStudio can reuse
    the same job id for a local model without hitting a stale-fence error."""
    broker.submit_batch(_genstudio_envelope())

    local = broker.submit_batch(_genstudio_envelope(model="org/local-model"))
    assert "error" not in local
    assert local["items"] == 1


def test_genstudio_cloud_submission_is_refused_over_http(reset, authed):
    _as_controller()
    response = authed.post("/api/hub/jobs", json=_genstudio_envelope())

    assert response.status_code == 403
    body = response.json()["detail"]
    assert body["code"] == cloud_guard.REFUSAL_CODE
    assert "cloudflare/flux-1-schnell" in body["detail"]


@pytest.mark.parametrize("model", [
    "fal:fal-ai/veo3",
    "kie:kling-3.0/video",
    "replicate:lightricks/ltx-2-fast",
])
def test_genstudio_video_cloud_routes_are_refused(reset, model):
    result = broker.submit_batch(
        _genstudio_envelope(modality="video", model=model))
    assert result["code"] == cloud_guard.REFUSAL_CODE


def test_genstudio_cloud_is_refused_from_the_worker_catalogue_alone(
    reset, seed_catalog,
):
    """A cloud model whose id carries no scheme is still caught, because the
    last-good worker catalogue says what it is."""
    seed_catalog("image", [{"repo": "org/secretly-hosted",
                            "is_cloud": True, "cache": {"state": "cached"}}])

    result = broker.submit_batch(
        _genstudio_envelope(model="org/secretly-hosted"))
    assert result["code"] == cloud_guard.REFUSAL_CODE


# ── what must keep working ─────────────────────────────────────────────────
def test_genstudio_local_submission_still_succeeds(controller, seed_catalog):
    seed_catalog("image", [{"repo": "black-forest-labs/FLUX.1-schnell",
                            "is_cloud": False, "provider": "local",
                            "cache": {"state": "cached"}}])

    result = broker.submit_batch(
        _genstudio_envelope(model="black-forest-labs/FLUX.1-schnell"))

    assert "error" not in result
    assert result["items"] == 1


def test_a_local_operator_may_still_submit_cloud_work(reset):
    """Removing cloud support was never the point — the Hub's own Jobs tab
    keeps every cloud lane it had."""
    result = broker.submit_batch({
        "modality": "video",
        "model": "fal:fal-ai/veo3",
        "label": "jobs-tab",
        "items": [{"prompt": "a stick figure waving"}],
    })

    assert "error" not in result
    assert result["items"] == 1
    assert broker.batches[result["batch_id"]]["model"] == "fal:fal-ai/veo3"


def test_a_genstudio_render_batch_still_dispatches_normally(controller, seed_catalog):
    """episode-assembly-v1 carries is_cloud=true. It is still local work."""
    seed_catalog("render", [{"repo": "episode-assembly-v1", "is_cloud": True,
                             "cache": {"state": "cached"}}])

    result = broker.submit_batch(_genstudio_envelope(
        modality="render", model="episode-assembly-v1",
        genstudio_execution={**GENSTUDIO_EXECUTION, "operation": "render.assemble"},
        items=[{"label": "EP0067"}],
    ))

    assert "error" not in result
    batch = broker.batches[result["batch_id"]]
    assert batch["modality"] == "render"
    assert batch["items"][0]["state"] == "queued"


# ── chat ───────────────────────────────────────────────────────────────────
def _chat_payload(**overrides):
    payload = {
        "model": "mlx-community/Llama-3.2-3B-Instruct-4bit",
        "model_cost_tier": "local",
        "label": "genstudio-kh:story-studio-kh",
        "packs": [{"pack_id": "pack-1", "scene_ids": ["scene-1"],
                   "messages": [{"role": "user", "content": "hello"}]}],
        "genstudio_execution": {**GENSTUDIO_EXECUTION,
                                "operation": "chat.completion"},
    }
    payload.update(overrides)
    return payload


def test_genstudio_paid_chat_work_is_refused_by_cost_tier(reset):
    """Chat declares its lane on the job, not the catalogue entry."""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as raised:
        chat_jobs.create_batch(_chat_payload(model_cost_tier="paid"))

    assert raised.value.status_code == 403
    assert raised.value.detail["code"] == cloud_guard.REFUSAL_CODE
    assert chat_jobs.batches == {}


def test_genstudio_local_chat_work_still_succeeds(controller):
    batch, duplicate = chat_jobs.create_batch(_chat_payload())
    assert duplicate is False
    assert batch["model_cost_tier"] == "local"


def test_a_local_operator_may_still_submit_paid_chat_work(reset):
    batch, _duplicate = chat_jobs.create_batch({
        "model": "cloudflare/some-chat-model",
        "model_cost_tier": "paid",
        "label": "jobs-tab",
        "packs": [{"pack_id": "pack-1", "scene_ids": ["scene-1"],
                   "messages": [{"role": "user", "content": "hello"}]}],
    })
    assert batch["model_cost_tier"] == "paid"


# ── the catalog door ───────────────────────────────────────────────────────
def _desired(repo, *, sibling="image", revision="a" * 40,
             contract_hash="sha256:" + "b" * 64, operation="image.generate"):
    return {
        "candidate_key": model_exposure.exposure_key(
            repo, operation, revision, contract_hash),
        "internal_model_id": repo,
        "display_name": "Pushed model",
        "modality": sibling,
        "operation": operation,
        "runtime_revision": revision,
        "contract_hash": contract_hash,
        "sibling_studio": sibling,
        "inventory": "catalog",
        "deployment": {"mode": "all_eligible", "minimum_unified_memory_gb": 8},
    }


def _catalog(*models):
    canonical = json.dumps(list(models), sort_keys=True, separators=(",", ":"))
    return {
        "schema": model_baselines.CATALOG_SCHEMA,
        "schema_version": model_baselines.CATALOG_SCHEMA_VERSION,
        "authority": "genstudio",
        "revision": hashlib.sha256(canonical.encode()).hexdigest(),
        "generated_at": "2026-08-02T00:00:00+00:00",
        "models": list(models),
    }


def test_a_pushed_cloud_catalog_row_is_filtered_and_named_back(reset, authed):
    """Filtered, not rejected: the local rows in the same push are still
    useful. But the drop is explicit in the response, never silent."""
    _as_controller()
    payload = _catalog(
        _desired("black-forest-labs/FLUX.1-schnell"),
        _desired("cloudflare/flux-1-schnell"),
    )

    response = authed.post("/api/hub/fleet-model-catalog", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body["approved_models"] == 1
    assert [row["repo"] for row in body["refused_cloud_models"]] == [
        "cloudflare/flux-1-schnell"]
    assert "cloud" in body["refused_cloud_models"][0]["reason"]

    snapshot = authed.get("/api/hub/model-baselines").json()
    assert [row["repo"] for row in snapshot["models"]] == [
        "black-forest-labs/FLUX.1-schnell"]
    assert snapshot["summary"]["refused_cloud_models"] == 1


def test_a_cloud_only_push_approves_nothing_and_names_every_drop(reset, authed):
    """The whole push is cloud, so nothing is approved — but the caller is told
    exactly which rows were dropped and why, rather than seeing an empty
    catalog with no explanation."""
    _as_controller()
    body = authed.post(
        "/api/hub/fleet-model-catalog",
        json=_catalog(_desired("cloudflare/flux-1-schnell"),
                      _desired("fal:fal-ai/veo3", sibling="video",
                               operation="video.generate")),
    ).json()

    assert body["approved_models"] == 0
    assert sorted(row["repo"] for row in body["refused_cloud_models"]) == [
        "cloudflare/flux-1-schnell", "fal:fal-ai/veo3"]
    assert all(row["reason"] for row in body["refused_cloud_models"])
    # model_exposure never learns about the cloud rows.
    assert authed.get("/api/hub/model-baselines").json()["models"] == []
