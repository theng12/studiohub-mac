from pathlib import Path

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
    assert 'choose the machine\'s hardware profile' in dashboard
