import json
import time

from starlette.testclient import TestClient

from backend import (auth, broker, capabilities, control_plane,
                     hardware_profiles, model_exposure, peers, registry)


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
    assert payload["schema_version"] == 2
    assert payload["observed_at"].endswith("Z")
    assert payload["site_id"] == "site-a"
    assert payload["controller"] == {
        "controller_id": "controller-a",
        "role": "controller",
        "studiohub_version": payload["controller"]["studiohub_version"],
        "online": True,
        "ready": True,
        "drained": False,
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
