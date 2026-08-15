import hashlib
import json
import time

from starlette.testclient import TestClient

from backend import (auth, broker, capabilities, control_plane,
                     hardware_profiles, model_exposure, peers, registry)


def _managed_release_evidence(*, image_state="current", voice_state="current",
                              hub_state="current", observed_image=None,
                              site_state="running"):
    release_id = "sha256:" + "d" * 64

    def component(name, state, expected_version, expected_commit, observed=None):
        observed = observed or (
            {"version": expected_version, "commit": expected_commit}
            if state == "current" else {"version": None, "commit": None}
        )
        return {
            "component": name,
            "desired_release_id": release_id,
            "expected_version": expected_version,
            "expected_commit": expected_commit,
            "observed_version": observed["version"],
            "observed_commit": observed["commit"],
            "state": state,
            "next_retry": 1_700_000_060.0 if "pending" in state else None,
            "converged": state == "current" and observed == {
                "version": expected_version, "commit": expected_commit,
            },
        }

    components = {
        "hub": component("hub", hub_state, "2.8.0", "a" * 40),
        "image": component(
            "image", image_state, "1.30.1", "b" * 40,
            observed=observed_image,
        ),
        "voice": component("voice", voice_state, "2.3.0", "c" * 40),
    }
    return {
        "desired": {
            "release_id": release_id,
            "sequence": 7,
            "created_at": "2026-08-15T00:00:00Z",
            "received_at": 1_700_000_000.0,
            "components": {
                key: {
                    "expected_version": row["expected_version"],
                    "expected_commit": row["expected_commit"],
                }
                for key, row in components.items()
            },
        },
        "activation": {
            "release_id": release_id,
            "job_id": "release-job-test",
            "activated_at": 1_700_000_001.0,
        },
        "site_state": site_state,
        "next_retry": 1_700_000_060.0 if site_state == "degraded" else None,
        "controller": components["hub"],
        "machines": {
            "local": {
                "desired_release_id": release_id,
                "state": site_state,
                "next_retry": 1_700_000_060.0 if site_state == "degraded" else None,
                "converged": all(row["converged"] for row in components.values()),
                "components": components,
            },
        },
        "catalog": {
            "state": "pending",
            "requested_at": None,
            "acknowledged_at": None,
            "requested_revision": None,
            "requested_models": None,
            "next_retry": None,
        },
    }


class _ReleaseEvidenceService:
    def __init__(self, evidence):
        self.evidence = evidence

    def capability_evidence(self):
        return self.evidence


def _release_manifest():
    value = {
        "schema": "genstudio.studio-fleet-release-intent",
        "schema_version": 1,
        "sequence": 7,
        "created_at": "2026-08-15T00:00:00Z",
        "components": {
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
        },
    }
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    value["release_id"] = "sha256:" + hashlib.sha256(canonical).hexdigest()
    return value


def _candidate(model_id, revision, operation, *, controls=None,
               input_limits=None, output_limits=None, hardware=None):
    value = {
        "schema": "studio.model-audit",
        "schema_version": 1,
        "audit_id": f"audit-{model_id}",
        "audit_status": "passed",
        "candidate_for_genstudio": True,
        "contract_hash": "sha256:" + "b" * 64,
        "runtime_revision": revision,
        "approved_operations": [operation],
        "audited_at": "2026-08-02T00:00:00Z",
        "adapter": {"id": "test-adapter", "version": "1"},
        "controls": controls or {},
        "input_limits": input_limits or {},
        "output_limits": output_limits or {},
        "hardware": (
            {"min_unified_memory_gb": 8}
            if hardware is None else hardware
        ),
    }
    return value


def _approve(model_id, candidate, operation):
    model_exposure.approve({
        "internal_model_id": model_id,
        "display_name": model_id,
        "audit_id": candidate["audit_id"],
        "audit_status": candidate["audit_status"],
        "candidate_for_genstudio": True,
        "contract_hash": candidate["contract_hash"],
        "runtime_revision": candidate["runtime_revision"],
        "approved_operations": [operation],
    }, operation)


def _seed_capability_site(monitor):
    image = next(row for row in monitor.registry if row["id"] == "image")
    voice = next(row for row in monitor.registry if row["id"] == "voice")
    monitor.registry = [image, voice]
    now = time.time()
    monitor.status = {
        "image": {
            "status": "up", "app_version": "1.22.1", "last_seen": now,
            "health": {"ok": True},
        },
        "voice": {
            "status": "up", "app_version": "1.21.1", "last_seen": now,
            "health": {"ok": True},
        },
    }
    image_candidate = _candidate(
        "org/image-model", "a" * 40, "image.text_to_image",
        controls={
            "aspect_ratios": ["16:9", "1:1"],
            "generation_controls": {"steps": True, "seed": True},
            "defaults": {"steps": 4},
        },
        input_limits={"max_prompt_characters": 15_000},
    )
    monitor._catalog_cache["image"] = (now, {"models": [{
        "repo": "org/image-model",
        "revision": "a" * 40,
        "cache": {
            "state": "cached",
            "path": "/private/cache/path-that-must-not-leak",
        },
        "capabilities": ["txt2img", "img2img"],
        "sizes": [
            {"aspect_ratio": "1:1", "width": 1024, "height": 1024,
             "tier": "balanced", "default": True},
            {"aspect_ratio": "16:9", "width": 1344, "height": 768,
             "tier": "balanced"},
        ],
        "custom": {"min_px": 512, "max_px": 1536, "step": 16,
                   "max_pixels": 1_400_000, "private": "no"},
        "generation_profile": {
            "controls": {"steps": True, "seed": True, "api_key": True},
            "defaults": {"steps": 4, "api_key": "nested-secret-must-not-leak",
                         "private": {"no": "dicts"}},
        },
        "max_prompt_characters": 15_000,
        "prompt": "customer prompt must never leak",
        "api_key": "secret-must-never-leak",
        "genstudio_candidate": image_candidate,
    }]})
    voice_candidate = _candidate(
        "org/voice-model", "c" * 40, "voice.tts",
        controls={"voice_modes": ["reference_audio_clone"],
                  "languages": ["en", "km"]},
        input_limits={"max_text_characters": 15_000},
        output_limits={"sample_rate_hz": 24_000},
    )
    monitor._catalog_cache["voice"] = (now, {"models": [{
        "repo": "org/voice-model",
        "revision": "main",
        "cache": {"state": "cached"},
        "capabilities": ["tts", "voice-cloning", "multilingual"],
        "languages": ["en", "km"],
        "sample_rate_hz": 24_000,
        "max_text_characters": 15_000,
        "genstudio_candidate": voice_candidate,
    }]})
    whisper_candidate = _candidate(
        "org/whisper", "d" * 40, "audio.transcription",
    )
    monitor._transcribe_cache["voice"] = (now, {
        "available": True,
        "default_model": "org/whisper",
        "models": [{"repo": "org/whisper", "label": "Whisper",
                    "cached": True,
                    "genstudio_candidate": whisper_candidate}],
    })
    _approve("org/image-model", image_candidate, "image.text_to_image")
    _approve("org/voice-model", voice_candidate, "voice.tts")
    _approve("org/whisper", whisper_candidate, "audio.transcription")
    hardware_profiles.set_machine_hardware_profile("local", "mac-mini-m4-16gb")
    control_plane.save_settings({
        "role": "controller", "site_id": "site-a", "site_name": "Site A",
        "controller_id": "controller-a", "database_mode": "off",
    })


def _worker(payload, service_id):
    return next(row for row in payload["workers"] if row["service_id"] == service_id)


def _model(worker, operation):
    return next(row for row in worker["models"] if row["operation"] == operation)


def _all_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from _all_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _all_keys(child)


def test_private_capability_snapshot_contract_is_versioned_and_truthful(
        authed, monitor):
    _seed_capability_site(monitor)

    response = authed.get("/api/hub/capabilities")
    payload = response.json()

    assert response.status_code == 200
    assert payload["schema"] == "studiohub.site-capabilities"
    assert payload["schema_version"] == 3
    assert payload["observed_at"].endswith("Z")
    assert payload["site_id"] == "site-a"
    assert payload["controller"] == {
        "controller_id": "controller-a",
        "role": "controller",
        "studiohub_version": payload["controller"]["studiohub_version"],
        "online": True,
        "ready": True,
        "drained": False,
        "managed_release": None,
    }
    assert payload["authority"] == {
        "global": "genstudio",
        "site_local_scheduler": "sqlite",
        "global_job_claiming": False,
        "postgresql": "optional_shadow_evidence_only",
    }
    assert payload["capacity"]["available_physical_machine_slots"] == 1
    assert payload["capacity"]["eligible_worker_services"] == 2

    machine = payload["machines"][0]
    assert machine["physical_machine_id"] == "local"
    assert machine["hardware_profile"]["id"] == "mac-mini-m4-16gb"
    assert machine["available_capacity"]["worker_slots"] == 1

    image = _worker(payload, "image")
    assert image["studio_type"] == "image"
    assert image["studio_version"] == "1.22.1"
    assert image["physical_machine_id"] == "local"
    assert image["online"] and image["ready"] and not image["busy"]
    model = _model(image, "image.text_to_image")
    assert model["internal_model_id"] == "org/image-model"
    assert model["runtime_revision"] == "a" * 40
    assert model["revision_status"] == "verified_immutable"
    assert model["input_limits"] == {"max_prompt_characters": 15_000}
    assert model["hardware"] == {"min_unified_memory_gb": 8}
    assert model["controls"]["aspect_ratios"] == ["16:9", "1:1"]
    assert model["controls"]["generation_controls"] == {
        "steps": True, "seed": True,
    }
    assert model["controls"]["defaults"] == {"steps": 4}
    assert model["availability"]["available_now"] is True
    assert model["availability"]["revision_pinning_ready"] is True

    voice = _worker(payload, "voice")
    tts = _model(voice, "voice.tts")
    transcription = _model(voice, "audio.transcription")
    assert tts["runtime_revision"] == "c" * 40
    assert tts["revision_status"] == "verified_immutable"
    assert tts["availability"]["revision_pinning_ready"] is True
    assert tts["controls"]["voice_modes"] == ["reference_audio_clone"]
    assert tts["controls"]["languages"] == ["en", "km"]
    assert tts["output_limits"] == {"sample_rate_hz": 24_000}
    assert transcription["availability"]["available_now"] is True


def test_pending_managed_release_quarantines_worker(
        authed, monitor, monkeypatch):
    from backend import main

    _seed_capability_site(monitor)
    evidence = _managed_release_evidence(
        image_state="pending_offline", site_state="degraded",
    )
    monkeypatch.setattr(
        main, "release_reconciler", _ReleaseEvidenceService(evidence),
    )

    payload = authed.get("/api/hub/capabilities").json()
    image = _worker(payload, "image")
    model = _model(image, "image.text_to_image")

    assert payload["schema_version"] == 3
    assert payload["managed_release"]["desired"]["release_id"] == evidence["desired"]["release_id"]
    assert payload["controller"]["managed_release"] == evidence["controller"]
    assert payload["machines"][0]["managed_release"] == evidence["machines"]["local"]
    assert image["managed_release"] == evidence["machines"]["local"]["components"]["image"]
    assert model["availability"]["available_now"] is False
    assert model["availability"]["reason"] == "managed_release_pending"


def test_release_reconciler_capability_evidence_is_bounded_and_sanitized(
        monitor, tmp_path):
    from backend.release_reconciliation import ReleaseReconciler

    _seed_capability_site(monitor)
    service = ReleaseReconciler(
        monitor,
        state_path=tmp_path / "release-reconciliation.json",
        loaded_version="2.7.1",
        loaded_commit="f" * 40,
    )
    manifest = _release_manifest()
    service.replace_intent(manifest)
    job = service.activate(manifest["release_id"], genstudio_run_reference=None)
    service.record_component(
        job["id"], "local", "image", state="pending_offline",
        next_retry=service._clock() + 60,
    )

    evidence = service.capability_evidence()

    assert evidence["desired"] == {
        "release_id": manifest["release_id"],
        "sequence": 7,
        "created_at": "2026-08-15T00:00:00Z",
        "received_at": evidence["desired"]["received_at"],
        "components": {
            "hub": {"expected_version": "2.8.0", "expected_commit": "a" * 40},
            "image": {"expected_version": "1.30.1", "expected_commit": "b" * 40},
            "voice": {"expected_version": "2.3.0", "expected_commit": "c" * 40},
        },
    }
    assert evidence["controller"]["observed_version"] == "2.7.1"
    assert evidence["controller"]["observed_commit"] == "f" * 40
    assert evidence["machines"]["local"]["components"]["image"]["state"] == "pending_offline"
    assert evidence["machines"]["local"]["components"]["image"]["next_retry"] is not None
    assert evidence["catalog"]["state"] == "pending"
    assert "repository" not in json.dumps(evidence)
    assert "detail" not in json.dumps(evidence)
    assert "error_code" not in json.dumps(evidence)


def test_release_reconciler_omits_unattested_loaded_identity(monitor, tmp_path):
    from backend.release_reconciliation import ReleaseReconciler

    _seed_capability_site(monitor)
    service = ReleaseReconciler(
        monitor,
        state_path=tmp_path / "release-reconciliation.json",
        loaded_version="not-semver/private/path",
        loaded_commit="unknown",
    )
    service.replace_intent(_release_manifest())

    evidence = service.capability_evidence()

    assert evidence["controller"]["observed_version"] is None
    assert evidence["controller"]["observed_commit"] is None


def test_blocked_managed_release_quarantines_worker_with_blocked_reason(
        authed, monitor, monkeypatch):
    from backend import main

    _seed_capability_site(monitor)
    evidence = _managed_release_evidence(
        hub_state="release_blocked", site_state="blocked_release",
    )
    monkeypatch.setattr(
        main, "release_reconciler", _ReleaseEvidenceService(evidence),
    )

    model = _model(
        _worker(authed.get("/api/hub/capabilities").json(), "voice"),
        "voice.tts",
    )

    assert model["availability"]["available_now"] is False
    assert model["availability"]["reason"] == "managed_release_blocked"


def test_mismatched_managed_release_quarantines_worker_with_mismatch_reason(
        authed, monitor, monkeypatch):
    from backend import main

    _seed_capability_site(monitor)
    evidence = _managed_release_evidence(
        image_state="checking",
        observed_image={"version": "1.29.9", "commit": "e" * 40},
    )
    monkeypatch.setattr(
        main, "release_reconciler", _ReleaseEvidenceService(evidence),
    )

    model = _model(
        _worker(authed.get("/api/hub/capabilities").json(), "image"),
        "image.text_to_image",
    )

    assert model["availability"]["available_now"] is False
    assert model["availability"]["reason"] == "managed_release_mismatch"


def test_current_managed_release_preserves_existing_availability_reason(
        authed, monitor, monkeypatch):
    from backend import main

    _seed_capability_site(monitor)
    source = monitor._catalog_cache["image"][1]["models"][0]
    source["qualified_revision_match"] = False
    monkeypatch.setattr(
        main,
        "release_reconciler",
        _ReleaseEvidenceService(_managed_release_evidence(site_state="complete")),
    )

    model = _model(
        _worker(authed.get("/api/hub/capabilities").json(), "image"),
        "image.text_to_image",
    )

    assert model["availability"]["available_now"] is False
    assert model["availability"]["reason"] == "runtime_revision_mismatch"


def test_image_revision_mismatch_is_published_and_blocks_new_routing(
        authed, monitor):
    _seed_capability_site(monitor)
    source = monitor._catalog_cache["image"][1]["models"][0]
    source["qualified_revision_match"] = False
    source["execution_ready"] = False

    payload = authed.get("/api/hub/capabilities").json()
    model = _model(_worker(payload, "image"), "image.text_to_image")

    assert model["availability"]["qualified_revision_match"] is False
    assert model["availability"]["execution_ready"] is False
    assert model["availability"]["available_now"] is False
    assert model["availability"]["reason"] == "runtime_revision_mismatch"


def test_image_execution_unready_is_published_and_blocks_new_routing(
        authed, monitor):
    _seed_capability_site(monitor)
    source = monitor._catalog_cache["image"][1]["models"][0]
    source["qualified_revision_match"] = True
    source["execution_ready"] = False

    payload = authed.get("/api/hub/capabilities").json()
    model = _model(_worker(payload, "image"), "image.text_to_image")

    assert model["availability"]["qualified_revision_match"] is True
    assert model["availability"]["execution_ready"] is False
    assert model["availability"]["available_now"] is False
    assert model["availability"]["reason"] == "worker_execution_unready"


def test_candidate_hardware_is_sanitized_before_capability_publication(
        authed, monitor):
    _seed_capability_site(monitor)
    raw_candidate = monitor._catalog_cache["image"][1]["models"][0][
        "genstudio_candidate"
    ]
    raw_candidate["hardware"] = {
        "min_unified_memory_gb": 8,
        "accelerator": "Apple GPU",
        "token": "must-not-leak",
        "cache_path": "/private/cache",
        "nested": {"credential": "must-not-leak", "safe": "published"},
    }

    payload = authed.get("/api/hub/capabilities").json()
    model = _model(_worker(payload, "image"), "image.text_to_image")

    assert model["hardware"] == {
        "min_unified_memory_gb": 8,
        "accelerator": "Apple GPU",
        "nested": {"safe": "published"},
    }
    serialized = json.dumps(model["hardware"])
    assert "must-not-leak" not in serialized
    assert "/private/cache" not in serialized


def test_capability_snapshot_uses_stale_caches_without_worker_network(
        authed, monitor, monkeypatch):
    _seed_capability_site(monitor)
    stale = time.time() - 3600
    monitor._catalog_cache["image"] = (stale, monitor._catalog_cache["image"][1])
    monitor._catalog_cache["voice"] = (stale, monitor._catalog_cache["voice"][1])
    monitor._transcribe_cache["voice"] = (
        stale, monitor._transcribe_cache["voice"][1],
    )

    async def unexpected_network(*args, **kwargs):
        raise AssertionError("capability snapshot must not contact a worker")

    monkeypatch.setattr(monitor._client, "get", unexpected_network)
    monkeypatch.setattr(monitor, "get_catalog", unexpected_network)
    monkeypatch.setattr(monitor, "get_transcription", unexpected_network)
    monkeypatch.setattr(monitor, "aggregate_catalog", unexpected_network)

    response = authed.get("/api/hub/capabilities")
    payload = response.json()

    assert response.status_code == 200
    assert _model(_worker(payload, "image"), "image.text_to_image")[
        "internal_model_id"
    ] == "org/image-model"
    voice = _worker(payload, "voice")
    assert _model(voice, "voice.tts")["internal_model_id"] == "org/voice-model"
    assert _model(voice, "audio.transcription")[
        "internal_model_id"
    ] == "org/whisper"


def test_unapproved_or_revoked_model_is_not_advertised(authed, monitor):
    _seed_capability_site(monitor)
    image_candidate = model_exposure.candidate_summary(
        monitor._catalog_cache["image"][1]["models"][0]
    )
    key = model_exposure.candidate_key(
        image_candidate, "image.text_to_image",
    )
    model_exposure.revoke_key(key)

    payload = authed.get("/api/hub/capabilities").json()
    image = _worker(payload, "image")
    assert image["models"] == []
    assert "image.text_to_image" not in image["supported_operations"]
    assert not any(
        row["internal_model_id"] == "org/image-model"
        for row in payload["model_supply"]
    )


def test_candidate_without_hub_approval_is_not_advertised(authed, monitor):
    _seed_capability_site(monitor)
    model_exposure.STATE_FILE.unlink()

    payload = authed.get("/api/hub/capabilities").json()
    assert all(worker["models"] == [] for worker in payload["workers"])
    assert payload["model_supply"] == []
    assert payload["capacity"]["by_operation"] == {}


def test_chat_capability_reports_verified_usage_revision_and_output_limit(
        authed, monitor):
    _seed_capability_site(monitor)
    chat = {
        **next(row for row in registry.load_registry() if row["id"] == "chat"),
        "id": "chat",
        "machine": "local",
    }
    monitor.registry.append(chat)
    monitor.status["chat"] = {
        "status": "up", "app_version": "1.24.0", "last_seen": time.time(),
        "health": {"ok": True},
    }
    revision = "7f0dc925e0d0afb0322d96f9255cfddf2ba5636e"
    chat_candidate = _candidate(
        "mlx-community/Llama-3.2-3B-Instruct-4bit", revision,
        "chat.completion",
        controls={"verified_token_usage": True},
        output_limits={"max_output_tokens": 32768},
    )
    monitor._catalog_cache["chat"] = (time.time(), {"models": [{
        "repo": "mlx-community/Llama-3.2-3B-Instruct-4bit",
        "runtime_revision": revision,
        "cache": {"state": "cached"},
        "max_output_tokens": 32768,
        "verified_token_usage": True,
        "genstudio_candidate": chat_candidate,
    }]})
    _approve("mlx-community/Llama-3.2-3B-Instruct-4bit",
             chat_candidate, "chat.completion")

    payload = authed.get("/api/hub/capabilities").json()
    model = _model(_worker(payload, "chat"), "chat.completion")
    assert model["runtime_revision"] == revision
    assert model["output_limits"] == {"max_output_tokens": 32768}
    assert model["controls"]["verified_token_usage"] is True


def test_capability_snapshot_is_strictly_header_authenticated_even_on_loopback(
        app, token, monitor):
    _seed_capability_site(monitor)
    local = TestClient(app, client=("127.0.0.1", 50000))

    denied = local.get("/api/hub/capabilities")
    accepted = local.get(
        "/api/hub/capabilities", headers={"Authorization": f"Bearer {token}"})

    assert denied.status_code == 401
    assert denied.headers["www-authenticate"] == "Bearer"
    assert accepted.status_code == 200


def test_browser_session_cookie_does_not_authenticate_machine_contract(
        app, monitor):
    _seed_capability_site(monitor)
    local = TestClient(app, client=("127.0.0.1", 50000))
    local.cookies.set(auth.SESSION_COOKIE_NAME, auth.create_browser_session())

    assert local.get("/api/hub/capabilities").status_code == 401


def test_fleet_token_authenticates_private_capability_snapshot(app, monitor):
    _seed_capability_site(monitor)
    peers.set_fleet_token("fleet-machine-secret")
    client = TestClient(app, headers={"X-Hub-Token": "fleet-machine-secret"})

    assert client.get("/api/hub/capabilities").status_code == 200


def test_capability_snapshot_never_exposes_content_credentials_or_ownership_ids(
        authed, monitor):
    _seed_capability_site(monitor)

    payload = authed.get("/api/hub/capabilities").json()
    keys = set(_all_keys(payload))
    serialized = json.dumps(payload)

    assert not keys.intersection({
        "prompt", "text", "content", "artifact_path", "path", "cache",
        "api_key", "genstudio_job_id", "genstudio_attempt_id",
        "idempotency_key", "fencing_token",
    })
    assert "customer prompt must never leak" not in serialized
    assert "secret-must-never-leak" not in serialized
    assert "nested-secret-must-not-leak" not in serialized
    assert "/private/cache" not in serialized


def test_busy_physical_machine_has_zero_available_capacity(authed, monitor):
    _seed_capability_site(monitor)
    broker._busy.add("image")

    payload = authed.get("/api/hub/capabilities").json()
    image = _worker(payload, "image")
    voice = _worker(payload, "voice")

    assert image["busy"] is True
    assert image["available_capacity"]["slots"] == 0
    assert _model(image, "image.text_to_image")["availability"]["reason"] == "worker_busy"
    assert voice["busy"] is False
    assert voice["physical_machine_busy"] is True
    assert voice["available_capacity"]["slots"] == 0
    assert _model(voice, "voice.tts")["availability"]["reason"] == "physical_machine_busy"
    assert payload["capacity"]["available_physical_machine_slots"] == 0


def test_pause_and_maintenance_are_reported_as_drain_without_mutating_work(
        authed, monitor):
    _seed_capability_site(monitor)
    registry.set_studio_enabled("local", "image", False)
    broker.set_maintenance("voice", True)

    payload = authed.get("/api/hub/capabilities").json()

    assert _worker(payload, "image")["drained"] is True
    assert _worker(payload, "image")["maintenance"] is False
    assert _worker(payload, "voice")["drained"] is True
    assert _worker(payload, "voice")["maintenance"] is True
    assert payload["controller"]["drained"] is True
    assert payload["controller"]["ready"] is False


def test_agent_reports_drained_and_never_claims_global_work(authed, monitor):
    _seed_capability_site(monitor)
    control_plane.save_settings({
        "role": "agent", "site_id": "site-a", "site_name": "Site A",
        "controller_id": "agent-a", "database_mode": "off",
    })

    payload = authed.get("/api/hub/capabilities").json()

    assert payload["controller"]["role"] == "agent"
    assert payload["controller"]["drained"] is True
    assert payload["controller"]["ready"] is False
    assert payload["authority"]["global_job_claiming"] is False


def test_capability_snapshot_uses_effective_flux_ram_policy(
        authed, monitor, monkeypatch):
    _seed_capability_site(monitor)
    flux_candidate = _candidate(
        "AITRADER/FLUX2-klein-4B-mlx-4bit", "e" * 40,
        "image.text_to_image",
    )
    monitor._catalog_cache["image"] = (time.time(), {"models": [{
        "repo": "AITRADER/FLUX2-klein-4B-mlx-4bit",
        "min_unified_memory_gb": 16,
        "cache": {"state": "cached"},
        "capabilities": ["txt2img"],
        "genstudio_candidate": flux_candidate,
    }]})
    _approve("AITRADER/FLUX2-klein-4B-mlx-4bit", flux_candidate,
             "image.text_to_image")
    monkeypatch.setattr(capabilities, "host_stats", lambda: {
        "total_gb": 8.59, "available_gb": 2.4,
    })

    payload = authed.get("/api/hub/capabilities").json()
    model = _model(_worker(payload, "image"), "image.text_to_image")

    assert model["memory_admission"]["catalog_min_total_memory_gb"] == 16
    assert model["memory_admission"]["effective_min_total_memory_gb"] == 8
    assert model["memory_admission"]["eligible_now"] is True
    assert model["availability"]["available_now"] is True
