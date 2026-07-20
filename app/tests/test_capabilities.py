import json
import time

from starlette.testclient import TestClient

from backend import auth, broker, capabilities, control_plane, hardware_profiles, peers, registry


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
    }]})
    monitor._catalog_cache["voice"] = (now, {"models": [{
        "repo": "org/voice-model",
        "revision": "main",
        "cache": {"state": "cached"},
        "capabilities": ["tts", "voice-cloning", "multilingual"],
        "languages": ["en", "km"],
        "sample_rate_hz": 24_000,
        "max_text_characters": 15_000,
    }]})
    monitor._transcribe_cache["voice"] = (now, {
        "available": True,
        "default_model": "org/whisper",
        "models": [{"repo": "org/whisper", "label": "Whisper",
                    "cached": True}],
    })
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
    assert payload["schema_version"] == 1
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
    model = _model(image, "image.generation")
    assert model["internal_model_id"] == "org/image-model"
    assert model["runtime_revision"] == "a" * 40
    assert model["revision_status"] == "verified_immutable"
    assert model["input_limits"] == {"max_prompt_characters": 15_000}
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
    assert tts["runtime_revision"] is None
    assert tts["revision_status"] == "reported_but_not_immutable"
    assert tts["availability"]["revision_pinning_ready"] is False
    assert tts["controls"]["voice_modes"] == ["reference_audio_clone"]
    assert tts["controls"]["languages"] == ["en", "km"]
    assert tts["output_limits"] == {"sample_rate_hz": 24_000}
    assert transcription["availability"]["available_now"] is True


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
    assert _model(image, "image.generation")["availability"]["reason"] == "worker_busy"
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
    monitor._catalog_cache["image"] = (time.time(), {"models": [{
        "repo": "AITRADER/FLUX2-klein-4B-mlx-4bit",
        "min_unified_memory_gb": 16,
        "cache": {"state": "cached"},
        "capabilities": ["txt2img"],
    }]})
    monkeypatch.setattr(capabilities, "host_stats", lambda: {
        "total_gb": 8.59, "available_gb": 2.4,
    })

    payload = authed.get("/api/hub/capabilities").json()
    model = _model(_worker(payload, "image"), "image.generation")

    assert model["memory_admission"]["catalog_min_total_memory_gb"] == 16
    assert model["memory_admission"]["effective_min_total_memory_gb"] == 8
    assert model["memory_admission"]["eligible_now"] is True
    assert model["availability"]["available_now"] is True
