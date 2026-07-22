import json
import sqlite3
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from backend import control_plane, enrollment, hardware_profiles, peers


def _controller():
    return control_plane.save_settings({
        "role": "controller",
        "site_id": "location-a",
        "site_name": "Location A",
        "controller_id": "controller-a",
        "database_mode": "off",
    })


def test_enrollment_code_is_permanent_and_privately_revealable(reset):
    _controller()
    issued = enrollment.create_enrollment_code(now=100)

    assert issued["permanent"] is True
    assert issued["expires_at"] is None
    assert issued["code"] not in enrollment.DB_FILE.read_bytes().decode(
        "utf-8", errors="ignore")
    assert enrollment.ENROLLMENT_CODE_FILE.stat().st_mode & 0o777 == 0o600
    status = enrollment.enrollment_credential_status(include_code=True)
    assert status["active"] is True and status["code"] == issued["code"]
    assert enrollment.enrollment_credential_status(include_code=False)["code"] is None


def test_enrollment_code_is_reusable_until_rotated_or_revoked(reset):
    _controller()
    peers.set_fleet_token("fleet-secret-for-location-a")
    issued = enrollment.create_enrollment_code(now=100)

    claimed = enrollment.claim_enrollment_code(issued["code"], now=200)
    replayed = enrollment.claim_enrollment_code(issued["code"], now=201)

    assert claimed == replayed == {
        "schema_version": 1,
        "site_id": "location-a",
        "site_name": "Location A",
        "controller_id": "controller-a",
        "fleet_token": "fleet-secret-for-location-a",
    }
    with sqlite3.connect(enrollment.DB_FILE) as connection:
        row = connection.execute(
            "SELECT code_hash, used_at, last_used_at, use_count FROM enrollment_codes"
        ).fetchone()
    assert len(row[0]) == 64 and row[1] is None and row[2:] == (201, 2)

    rotated = enrollment.create_enrollment_code(now=300)
    with pytest.raises(ValueError, match="revoked"):
        enrollment.claim_enrollment_code(issued["code"], now=301)
    assert enrollment.claim_enrollment_code(rotated["code"], now=302)["site_id"] == "location-a"

    revoked = enrollment.revoke_enrollment_credential(now=400)
    assert revoked["revoked"] is True
    assert not enrollment.ENROLLMENT_CODE_FILE.exists()
    with pytest.raises(ValueError, match="revoked"):
        enrollment.claim_enrollment_code(rotated["code"], now=401)


def test_legacy_one_time_rows_keep_expiry_and_single_use_semantics(reset):
    _controller()
    peers.set_fleet_token("fleet-secret-for-location-a")
    code = "a" * 43
    with sqlite3.connect(enrollment.DB_FILE) as connection:
        connection.execute(
            """CREATE TABLE enrollment_codes (
                 id TEXT PRIMARY KEY, code_hash TEXT NOT NULL UNIQUE,
                 created_at REAL NOT NULL, expires_at REAL NOT NULL, used_at REAL,
                 site_id TEXT NOT NULL, controller_id TEXT NOT NULL)"""
        )
        connection.execute(
            "INSERT INTO enrollment_codes VALUES (?, ?, ?, ?, NULL, ?, ?)",
            ("legacy", enrollment._code_hash(code), 100, 700,
             "location-a", "controller-a"),
        )
    assert enrollment.claim_enrollment_code(code, now=200)["site_id"] == "location-a"
    with pytest.raises(ValueError, match="already been used"):
        enrollment.claim_enrollment_code(code, now=201)


def test_enrollment_role_checks(reset):
    with pytest.raises(ValueError, match="only by a location controller"):
        enrollment.create_enrollment_code()

    control_plane.save_settings({
        "role": "agent", "site_id": "location-a", "site_name": "Location A",
        "controller_id": "agent-a", "database_mode": "off",
    })
    with pytest.raises(ValueError, match="not a location controller"):
        enrollment.claim_enrollment_code("a" * 43)


@pytest.mark.parametrize("url", [
    "http://8.8.8.8:47873",
    "http://user:secret@100.70.0.2:47873",
    "http://100.70.0.2:47873/path",
    "http://100.70.0.2:47873?token=secret",
    "ftp://100.70.0.2:47873",
])
def test_private_controller_url_rejects_unsafe_destinations(url):
    with pytest.raises(ValueError):
        enrollment.validate_private_controller_url(url)


@pytest.mark.parametrize(("url", "expected"), [
    ("http://127.0.0.1:47873", "http://127.0.0.1:47873"),
    ("http://192.168.1.20:47873/", "http://192.168.1.20:47873"),
    ("http://100.70.0.2:47873", "http://100.70.0.2:47873"),
    ("100.70.0.2:47873", "http://100.70.0.2:47873"),
    ("100.70.0.2", "http://100.70.0.2:47873"),
    ("https://100.70.0.2", "https://100.70.0.2"),
    ("https://100.70.0.2:443/", "https://100.70.0.2:443"),
])
def test_private_controller_url_accepts_common_private_forms(url, expected):
    assert enrollment.validate_private_controller_url(url) == expected


@pytest.mark.asyncio
async def test_controller_probe_preserves_normalized_url_and_reports_readiness(
        reset, monkeypatch):
    class Response:
        status_code = 200
        headers = {"content-type": "application/json"}

        @staticmethod
        def json():
            return {
                "schema_version": 1, "role": "controller",
                "site_id": "location-a", "site_name": "Location A",
                "controller_id": "controller-a", "version": "1.60.3",
                "enrollment_active": True,
            }

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None

        async def get(self, url, timeout):
            assert url == "http://100.70.0.2:47873/api/hub/enrollment/info"
            assert timeout == 8.0
            return Response()

    monkeypatch.setattr(enrollment.httpx, "AsyncClient", lambda **kwargs: Client())

    result = await enrollment.probe_remote_controller("100.70.0.2")

    assert result == {
        "ok": True, "controller_url": "http://100.70.0.2:47873",
        "site_id": "location-a", "site_name": "Location A",
        "controller_id": "controller-a", "version": "1.60.3",
        "enrollment_active": True,
    }


def test_claim_endpoint_requires_controller_and_private_source(app, token):
    _controller()
    issued = enrollment.create_enrollment_code()
    public = TestClient(app, client=("8.8.8.8", 50000))

    refused = public.post("/api/hub/enrollment/claim", json={"code": issued["code"]})

    assert refused.status_code == 403
    private = TestClient(app, client=("100.70.0.8", 50000))
    claimed = private.post("/api/hub/enrollment/claim", json={"code": issued["code"]})
    assert claimed.status_code == 200
    assert claimed.json()["site_id"] == "location-a"
    assert "fleet_token" in claimed.json()


def test_enrollment_info_is_private_read_only_and_contains_no_secret(app):
    _controller()
    issued = enrollment.create_enrollment_code()

    private = TestClient(app, client=("100.70.0.8", 50000))
    response = private.get("/api/hub/enrollment/info")

    assert response.status_code == 200
    assert response.json()["role"] == "controller"
    assert response.json()["enrollment_active"] is True
    assert issued["code"] not in response.text
    assert "fleet_token" not in response.text
    public = TestClient(app, client=("8.8.8.8", 50000))
    assert public.get("/api/hub/enrollment/info").status_code == 403


def test_code_creation_requires_authenticated_controller(app, token):
    _controller()
    remote = TestClient(app, client=("100.70.0.8", 50000))

    assert remote.post("/api/hub/enrollment-codes").status_code == 401
    allowed = remote.post(
        "/api/hub/enrollment-codes", headers={"X-Hub-Token": token})
    assert allowed.status_code == 200
    assert allowed.json()["permanent"] is True
    assert allowed.json()["expires_at"] is None
    revealed = remote.get(
        "/api/hub/enrollment-codes", headers={"X-Hub-Token": token})
    assert revealed.json()["code"] == allowed.json()["code"]

    peers.set_fleet_token("peer-fleet-token-123")
    peer = remote.post(
        "/api/hub/enrollment-codes", headers={"X-Hub-Token": "peer-fleet-token-123"})
    assert peer.status_code == 403
    peer_status = remote.get(
        "/api/hub/enrollment-codes", headers={"X-Hub-Token": "peer-fleet-token-123"})
    assert peer_status.status_code == 200
    assert peer_status.json()["active"] is True
    assert peer_status.json()["code"] is None


def test_agent_join_endpoint_requires_local_or_owner_access(app, token, monkeypatch):
    remote = TestClient(app, client=("100.70.0.8", 50000))

    refused = remote.post("/api/hub/setup/join", json={
        "controller_url": "http://100.70.0.2:47873",
        "enrollment_code": "a" * 43,
        "hardware_profile_id": "mac-mini-m4-16gb",
    })

    assert refused.status_code == 401

    peers.set_fleet_token("peer-fleet-token-123")
    fleet_only = remote.post(
        "/api/hub/setup/join", headers={"X-Hub-Token": "peer-fleet-token-123"},
        json={
            "controller_url": "100.70.0.2",
            "enrollment_code": "a" * 43,
            "hardware_profile_id": "mac-mini-m4-16gb",
        },
    )
    assert fleet_only.status_code == 403

    async def claim_remote(controller_url, code):
        assert controller_url == "100.70.0.2"
        return {
            "site_id": "location-a", "site_name": "Location A",
            "controller_id": "controller-a", "fleet_token": "new-site-token-123",
        }

    monkeypatch.setattr(enrollment, "claim_remote", claim_remote)
    allowed = remote.post(
        "/api/hub/setup/join", headers={"X-Hub-Token": token}, json={
            "controller_url": "100.70.0.2",
            "enrollment_code": "a" * 43,
            "hardware_profile_id": "mac-mini-m4-16gb",
        },
    )
    assert allowed.status_code == 200
    assert allowed.json()["settings"]["role"] == "agent"


def test_controller_check_endpoint_uses_read_only_probe(app, monkeypatch):
    async def probe(controller_url):
        assert controller_url == "100.70.0.2"
        return {"ok": True, "controller_url": "http://100.70.0.2:47873",
                "site_name": "Location A", "enrollment_active": True}

    monkeypatch.setattr(enrollment, "probe_remote_controller", probe)
    local = TestClient(app, client=("127.0.0.1", 50000))

    response = local.post(
        "/api/hub/setup/check-controller", json={"controller_url": "100.70.0.2"})

    assert response.status_code == 200
    assert response.json()["enrollment_active"] is True


def test_new_controller_setup_assigns_hardware_and_keeps_authority_local(reset):
    result = enrollment.configure_new_controller(
        "Kampot Studio", "kampot-studio", "mac-mini-m4-16gb")

    assert result["settings"]["role"] == "controller"
    assert result["settings"]["database_mode"] == "off"
    assert result["settings"]["global_job_claiming"] is False
    assert result["settings"]["controller_id"].endswith("-hub")
    assert hardware_profiles.machine_hardware_profile("local")["id"] == "mac-mini-m4-16gb"
    assert peers.fleet_token()


@pytest.mark.asyncio
async def test_loopback_join_configures_agent_and_clears_code(app, monkeypatch):
    async def claim_remote(controller_url, code):
        assert controller_url == "http://100.70.0.2:47873"
        assert code == "one-time-code"
        return {
            "site_id": "location-a", "site_name": "Location A",
            "controller_id": "controller-a", "fleet_token": "new-site-fleet-token",
        }

    monkeypatch.setattr(enrollment, "claim_remote", claim_remote)
    local = TestClient(app, client=("127.0.0.1", 50000))
    response = local.post("/api/hub/setup/join", json={
        "controller_url": "http://100.70.0.2:47873",
        "enrollment_code": "one-time-code",
        "hardware_profile_id": "mac-mini-m2-8gb",
    })

    assert response.status_code == 200
    body = response.json()
    assert body["settings"]["role"] == "agent"
    assert body["settings"]["site_id"] == "location-a"
    assert body["settings"]["parent_controller_url"] == "http://100.70.0.2:47873"
    assert peers.fleet_token() == "new-site-fleet-token"
    assert hardware_profiles.machine_hardware_profile("local")["id"] == "mac-mini-m2-8gb"


def test_join_failure_rolls_back_every_local_setting(reset, monkeypatch):
    _controller()
    peers.set_fleet_token("old-site-fleet-token")
    hardware_profiles.set_machine_hardware_profile("local", "mac-mini-m1-8gb")
    before_settings = json.loads(control_plane.SETTINGS_FILE.read_text())
    before_token = peers.FLEET_TOKEN_FILE.read_text()
    before_profiles = hardware_profiles.MACHINE_PROFILES_FILE.read_text()

    def fail_assignment(machine, profile_id):
        raise RuntimeError("disk write failed")

    monkeypatch.setattr(hardware_profiles, "set_machine_hardware_profile", fail_assignment)
    with pytest.raises(RuntimeError, match="disk write failed"):
        enrollment.configure_joined_agent(
            "http://100.70.0.2:47873", "mac-mini-m4-16gb", {
                "site_id": "location-b", "site_name": "Location B",
                "controller_id": "controller-b", "fleet_token": "new-site-fleet-token",
            })

    assert json.loads(control_plane.SETTINGS_FILE.read_text()) == before_settings
    assert peers.FLEET_TOKEN_FILE.read_text() == before_token
    assert hardware_profiles.MACHINE_PROFILES_FILE.read_text() == before_profiles
    assert control_plane.load_settings()["role"] == "controller"


def test_enrolled_agent_keeps_customer_submission_protection(app, token):
    enrollment.configure_joined_agent(
        "http://100.70.0.2:47873", "mac-mini-m4-16gb", {
            "site_id": "location-a", "site_name": "Location A",
            "controller_id": "controller-a", "fleet_token": "site-fleet-token-123",
        })
    remote = TestClient(app, headers={"X-Hub-Token": token})

    refused = remote.post("/api/hub/jobs", json={
        "modality": "image", "model": "org/model", "items": [{"prompt": "x"}],
    })

    assert refused.status_code == 409
    assert "agent mode" in refused.json()["detail"]


def test_dashboard_exposes_simple_setup_and_masks_secrets():
    dashboard = (Path(__file__).parents[1] / "frontend" / "index.html").read_text()

    assert 'id="simple-setup-card"' in dashboard
    assert "Add this Mac to your fleet" in dashboard
    assert "Starting the first Mac at a brand-new location?" in dashboard
    assert "you do not need Agent mode, Controller mode, or a fleet token" in dashboard
    assert 'id="setup-controller-check"' in dashboard
    assert 'api("/api/hub/setup/check-controller"' in dashboard
    assert "function checkSetupController" in dashboard
    assert 'class="card section setup-advanced"' in dashboard
    assert 'id="setup-enrollment-code" type="password" autocomplete="off"' in dashboard
    assert 'id="fleet-token" type="password"' in dashboard
    assert 'id="fleet-reveal"' in dashboard
    assert 'id="fleet-copy"' in dashboard
    assert 'id="acc-reveal"' in dashboard
    assert 'id="acc-copy"' in dashboard
    assert 'id="setup-code-reveal"' in dashboard
    assert 'id="setup-code-copy"' in dashboard
    assert 'id="setup-code-revoke"' in dashboard
    assert "function toggleEnrollmentCode()" in dashboard
    assert 'mask.dataset.revealed = "0"' in dashboard
    assert "function revokeEnrollmentCode()" in dashboard
    assert "function loadEnrollmentCode()" in dashboard
    assert 'copyPrivateValue(activeEnrollmentCode, $("#setup-code-copy"), "Copy code")' in dashboard
    assert "function toggleHubToken()" in dashboard
    assert "function copyFleetToken()" in dashboard
    assert 'api("/api/hub/enrollment-codes"' in dashboard
    assert 'api("/api/hub/setup/join"' in dashboard
    assert "enrollmentCodeExpiryTimer" not in dashboard
    assert "expires after 10 minutes" not in dashboard
    assert "localStorage.setItem" not in dashboard[dashboard.index("function createEnrollmentCode"):dashboard.index("function renderController")]
