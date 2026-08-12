from pathlib import Path

import httpx

from backend import hardware_profiles, registry


EXPECTED_PROFILES = {
    "mac-mini-m1-8gb": 5,
    "mac-mini-m1-16gb": 0,
    "mac-mini-m2-8gb": 6,
    "mac-mini-m2-16gb": 2,
    "mac-mini-m4-16gb": 3,
    "mac-mini-m4-24gb": 1,
    "macbook-m4-16gb": 1,
    "imac-m1-8gb": 4,
    "imac-m3-8gb": 2,
}


def test_default_catalog_matches_approved_fleet(reset):
    profiles = hardware_profiles.load_hardware_profiles()

    assert {row["id"]: row["planned_units"] for row in profiles} == EXPECTED_PROFILES
    assert all(row["custom"] is False for row in profiles)


def test_custom_profile_and_assignment_survive_cache_reload(reset):
    custom = hardware_profiles.add_custom_hardware_profile({
        "id": "mac-studio-m4-64gb",
        "display_name": "Mac Studio M4 · 64 GB",
        "machine_type": "Mac Studio",
        "machine_prefix": "macstudio-m4-64gb",
        "chip": "M4",
        "memory_gb": 64,
        "planned_units": 2,
    })
    assigned = hardware_profiles.set_machine_hardware_profile(
        "macstudio-m4-64gb-001", custom["id"],
    )

    hardware_profiles._custom_cache = None
    hardware_profiles._assignment_cache = None

    assert hardware_profiles.hardware_profile(custom["id"])["custom"] is True
    assert hardware_profiles.machine_hardware_profile(
        "macstudio-m4-64gb-001",
    )["memory_gb"] == 64
    assert assigned["id"] == custom["id"]


def test_catalog_suggests_stable_incrementing_machine_ids(reset):
    profile_id = "mac-mini-m2-8gb"
    hardware_profiles.set_machine_hardware_profile("older-name", profile_id)

    catalog = hardware_profiles.hardware_profile_catalog({
        "local", "macmini-m2-8gb-002",
    })
    profile = next(row for row in catalog["profiles"] if row["id"] == profile_id)

    assert profile["assigned_units"] == 1
    assert profile["suggested_machine_id"] == "macmini-m2-8gb-003"


def test_local_hardware_matches_apple_profile_and_normalizes_decimal_memory(reset, monkeypatch):
    match = hardware_profiles.matching_hardware_profile({
        "machine_type": "Mac mini", "chip": "Apple M4", "total_gb": 17.18,
    })
    assert match["id"] == "mac-mini-m4-16gb"

    monkeypatch.setattr(
        "backend.resources.hardware_identity",
        lambda: {"machine_name": "render-01", "machine_type": "Mac mini",
                 "chip": "Apple M4", "memory_gb": 16},
    )
    catalog = hardware_profiles.hardware_profile_catalog({"local"})
    assert catalog["local_hardware"]["machine_name"] == "render-01"
    assert catalog["local_hardware"]["profile_id"] == "mac-mini-m4-16gb"


def test_generated_terranash_hostname_never_guesses_ambiguous_profile_names():
    profile = hardware_profiles.hardware_profile("mac-mini-m4-16gb")

    assert hardware_profiles.generated_terranash_hostname(
        "macmini-m4-16gb-terranash-0209-49f38d3b-hub", profile,
    ) == "terranash-0209"
    assert hardware_profiles.generated_terranash_hostname(
        "macmini-m4-16gb-worker-49f38d3b-hub", profile,
    ) is None
    assert hardware_profiles.generated_terranash_hostname(
        "macmini-m4-16gb-foo-49f38d3b-hub", profile,
    ) is None


def test_local_machine_profile_uses_detected_hardware_until_overridden(reset, monkeypatch):
    monkeypatch.setattr(hardware_profiles, "local_hardware", lambda: {
        "machine_type": "Mac mini", "chip": "Apple M4", "memory_gb": 16,
    })

    assert hardware_profiles.machine_hardware_profile("local")["id"] == "mac-mini-m4-16gb"


def test_registration_profile_generates_id_and_is_published(authed):
    response = authed.post("/api/hub/registry/add", json={
        "host": "100.9.9.9",
        "hardware_profile_id": "mac-mini-m2-8gb",
        "modalities": ["voice"],
    })

    assert response.status_code == 200
    payload = response.json()
    assert payload["machine"] == "macmini-m2-8gb-001"
    assert payload["hardware_profile"]["id"] == "mac-mini-m2-8gb"

    studio = next(
        row for row in authed.get("/api/hub/studios").json()["studios"]
        if row["machine"] == payload["machine"]
    )
    machine = authed.get("/api/hub/resources").json()["machines"][payload["machine"]]
    assert studio["hardware_profile_id"] == "mac-mini-m2-8gb"
    assert machine["hardware_profile"]["memory_gb"] == 8


def test_discovery_detects_remote_hardware_without_profile(authed, monkeypatch):
    class Response:
        is_success = True

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None

        async def get(self, url, **kwargs):
            if url.endswith(":47873/api/hub/resources?local_only=true"):
                return Response({"host": {
                    "machine_name": "voice-01", "machine_type": "Mac mini",
                    "chip": "Apple M4", "total_gb": 17.18,
                }})
            if url.endswith(":47870/api/health"):
                return Response({"ok": True})
            if url.endswith(":47870/api/version"):
                return Response({"title": "Voice Studio KH"})
            raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: Client())

    response = authed.post("/api/hub/registry/discover", json={
        "host": "100.9.9.6",
    })

    assert response.status_code == 200
    payload = response.json()
    assert payload["machine"] == "macmini-m4-16gb-001"
    assert payload["hardware_profile"]["id"] == "mac-mini-m4-16gb"
    assert payload["detected_hardware"]["machine_name"] == "voice-01"
    assert payload["found"] == [{
        "port": 47870, "modality": "voice", "title": "Voice Studio KH",
    }]


def test_existing_machine_profile_can_change_and_clears_on_removal(authed):
    authed.post("/api/hub/registry/add", json={
        "host": "100.9.9.8", "machine": "mac-existing", "modalities": ["image"],
    })

    assigned = authed.put(
        "/api/hub/registry/machines/mac-existing/hardware-profile",
        json={"hardware_profile_id": "mac-mini-m4-16gb"},
    )
    assert assigned.status_code == 200
    assert assigned.json()["hardware_profile"]["chip"] == "M4"

    removed = authed.delete("/api/hub/registry/machines/mac-existing")
    assert removed.status_code == 200
    assert "mac-existing" not in hardware_profiles.load_machine_profile_ids()


def test_hardware_profile_endpoints_validate_profiles(authed):
    assert authed.post("/api/hub/registry/add", json={
        "host": "100.9.9.7", "hardware_profile_id": "missing-profile",
    }).status_code == 400
    assert authed.put(
        "/api/hub/registry/machines/local/hardware-profile",
        json={"hardware_profile_id": "missing-profile"},
    ).status_code == 400


def test_dashboard_requires_and_manages_hardware_profiles():
    dashboard = (Path(__file__).parents[1] / "frontend" / "index.html").read_text()

    assert 'id="d-profile"' in dashboard
    assert 'id="hp-type"' in dashboard
    assert "function selectRegistrationHardware" in dashboard
    assert "async function assignMachineHardware" in dashboard
    assert "Auto-detect from online Hub" in dashboard
    assert "they never limit machine count" in dashboard
    assert 'id="hp-units"' not in dashboard
    assert "profile.assigned_units}/${profile.planned_units" not in dashboard
