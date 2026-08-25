import hashlib
import json
import sqlite3
import stat
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from backend import auth, control_plane, enrollment, hardware_profiles, peers, registry
from backend.enrollment_repair import EnrollmentRepairCoordinator
from backend.enrollment_repair_store import (
    ControllerIdentity,
    RepairStore,
    RepairStoreError,
    TargetIdentity,
)


FLEET_TOKEN = "fleet-token-for-repair-tests"
HUB_TOKEN_SENTINEL = "unique-hub-token-sentinel"
PERMANENT_CODE_SENTINEL = "permanent-code-sentinel"


def _set_controller():
    control_plane.save_settings({
        "role": "controller",
        "site_id": "site-a",
        "site_name": "Site A",
        "controller_id": "controller-a",
        "database_mode": "off",
    })


class OwnerCoordinator:
    def __init__(self):
        self.created = []

    def create_batch(self, machines):
        self.created.append(list(machines))
        return {
            "batch_id": "batch-a",
            "state": "queued",
            "targets": ["mac-a"],
            "requests": [{
                "request_id": "request-a",
                "target_machine": "mac-a",
                "state": "queued",
                "error_code": None,
            }],
            "rejected": {},
        }

    def eligibility(self):
        return {
            "issuance_enabled": True,
            "machines": [{
                "machine": "mac-a",
                "display_label": "Mac A",
                "host": "100.64.0.10",
                "eligible": True,
                "code": "eligible",
                "detail": "Eligible for enrollment repair.",
                "request_state": None,
            }],
        }

    def batch(self, batch_id):
        if batch_id != "batch-a":
            return None
        return {
            "batch_id": "batch-a",
            "state": "running",
            "targets": ["mac-a"],
            "requests": [{
                "request_id": "request-a",
                "target_machine": "mac-a",
                "state": "confirmation_pending",
                "evidence": {"registry_changed_pending": True},
                "observed_identity": {"role": "standalone"},
            }],
        }


class ServiceCoordinator:
    def __init__(self):
        self.calls = []

    async def redeem(self, payload, *, direct_source, fleet_token):
        self.calls.append(("redeem", payload, direct_source, fleet_token))
        return {
            "schema": "studiohub.enrollment-repair-claim",
            "schema_version": 1,
            "request_id": payload["request_id"],
            "target_machine_id": payload["target_machine_id"],
            "role": "agent",
            "site_id": "site-a",
            "site_name": "Site A",
            "controller_id": payload["target_machine_id"],
        }


class ServiceExecutor:
    def __init__(self):
        self.calls = []

    async def apply(self, payload, *, direct_source):
        self.calls.append(("apply", payload, direct_source))
        return {"request_id": payload["request_id"], "state": "accepted"}

    def status(self, request_id, *, direct_source):
        self.calls.append(("status", request_id, direct_source))
        return {"request_id": request_id, "state": "complete", "identity": {
            "role": "agent", "site_id": "site-a", "site_name": "Site A",
            "controller_id": "mac-a",
        }}

    def expected_status_source(self, request_id):
        assert request_id == "request-a"
        return "100.64.0.20"


def _inject(app, *, coordinator=None, executor=None):
    app.state.enrollment_repair_coordinator = coordinator or OwnerCoordinator()
    app.state.enrollment_repair_executor = executor or ServiceExecutor()


def _owner_client(app, *, host="100.66.3.3"):
    client = TestClient(app, client=(host, 50000))
    client.cookies.set(auth.SESSION_COOKIE_NAME, auth.create_browser_session())
    return client


def test_controller_loopback_and_valid_owner_session_can_create_repair(app):
    _set_controller()
    coordinator = OwnerCoordinator()
    _inject(app, coordinator=coordinator)

    local = TestClient(app, client=("127.0.0.1", 50000))
    remote_owner = _owner_client(app)
    local_result = local.post(
        "/api/hub/enrollment-repairs", json={"machines": ["mac-a"]},
    )
    remote_result = remote_owner.post(
        "/api/hub/enrollment-repairs", json={"machines": ["mac-a"]},
    )

    assert local_result.status_code == remote_result.status_code == 202
    assert local_result.json() == remote_result.json() == {
        "batch_id": "batch-a",
        "state": "queued",
        "targets": ["mac-a"],
        "requests": [{
            "request_id": "request-a",
            "target_machine": "mac-a",
            "state": "queued",
            "error_code": None,
        }],
        "rejected": {},
    }
    assert coordinator.created == [["mac-a"], ["mac-a"]]


def test_feature_disable_rejects_owner_creation_but_keeps_delayed_prebound_apply(
    app, monkeypatch,
):
    from backend import enrollment_repair

    _set_controller()
    peers.set_fleet_token(FLEET_TOKEN)

    class RollbackExecutor(ServiceExecutor):
        pass

    executor = RollbackExecutor()
    coordinator = OwnerCoordinator()
    _inject(app, coordinator=coordinator, executor=executor)
    monkeypatch.setattr(enrollment_repair, "NEW_ISSUANCE_ENABLED", False)

    owner = TestClient(app, client=("127.0.0.1", 50000)).post(
        "/api/hub/enrollment-repairs", json={"machines": ["mac-a"]},
    )
    apply = _service_request(
        app, SERVICE_ROUTES[0], host="100.64.0.20",
        headers={"X-Hub-Token": FLEET_TOKEN},
    )

    assert owner.status_code == 503
    assert owner.json()["detail"]["code"] == "repair_issuance_disabled"
    assert coordinator.created == []
    assert apply.status_code == 200
    assert apply.json() == {"request_id": "request-a-000000", "state": "accepted"}
    assert executor.calls[-1][0] == "apply"


@pytest.mark.parametrize("credential", [
    "hub_token", "fleet_token", "agent_session", "missing_session", "noncontroller",
])
def test_hub_token_fleet_token_agent_session_missing_session_and_noncontroller_cannot_create_repair(
    app, token, credential,
):
    _set_controller()
    peers.set_fleet_token(FLEET_TOKEN)
    coordinator = OwnerCoordinator()
    _inject(app, coordinator=coordinator)
    client = TestClient(app, client=("100.66.3.3", 50000))
    headers = {}
    if credential == "hub_token":
        headers["X-Hub-Token"] = token
    elif credential == "fleet_token":
        headers["X-Hub-Token"] = FLEET_TOKEN
    elif credential in {"agent_session", "noncontroller"}:
        client.cookies.set(auth.SESSION_COOKIE_NAME, auth.create_browser_session())
        control_plane.save_settings({
            "role": "agent",
            "site_id": "site-a",
            "site_name": "Site A",
            "controller_id": "mac-a",
            "database_mode": "off",
            "parent_controller_url": "http://100.64.0.20:47873",
        })

    response = client.post(
        "/api/hub/enrollment-repairs",
        headers=headers,
        json={"machines": ["mac-a"]},
    )

    assert response.status_code in {401, 403, 409}
    assert coordinator.created == []


def test_cross_origin_owner_write_is_rejected_before_batch_creation(app):
    _set_controller()
    coordinator = OwnerCoordinator()
    _inject(app, coordinator=coordinator)
    owner = _owner_client(app)

    response = owner.post(
        "/api/hub/enrollment-repairs",
        headers={"Origin": "https://evil.example"},
        json={"machines": ["mac-a"]},
    )

    assert response.status_code == 403
    assert coordinator.created == []


def test_eligibility_static_route_precedes_dynamic_batch_route(app):
    _set_controller()
    _inject(app)
    response = TestClient(app, client=("127.0.0.1", 50000)).get(
        "/api/hub/enrollment-repairs/eligibility",
    )
    assert response.status_code == 200
    assert response.json()["machines"][0]["code"] == "eligible"


def test_owner_batch_get_is_sanitized(app):
    _set_controller()
    _inject(app)
    response = TestClient(app, client=("127.0.0.1", 50000)).get(
        "/api/hub/enrollment-repairs/batch-a",
    )
    assert response.status_code == 200
    encoded = json.dumps(response.json(), sort_keys=True).lower()
    for forbidden in ("ticket", "token", "claim", "credential"):
        assert forbidden not in encoded
    for secret in (FLEET_TOKEN, HUB_TOKEN_SENTINEL, PERMANENT_CODE_SENTINEL):
        assert secret.lower() not in encoded


def test_real_redemption_persists_only_credential_free_observed_mismatch_evidence(
    app,
):
    from backend import main

    _set_controller()
    peers.set_fleet_token(FLEET_TOKEN)
    registry.add_user_entries(registry.build_machine_entries(
        "100.64.0.10", "mac-a", ["image", "voice"],
    ))
    main.monitor.reload_registry()
    store = RepairStore(enrollment.DB_FILE, clock=lambda: 1000.0)
    coordinator = EnrollmentRepairCoordinator(
        store,
        registry_loader=lambda: list(main.monitor.registry),
        token_reader=lambda: FLEET_TOKEN,
        settings_reader=lambda: {
            "role": "controller",
            "site_id": "site-a",
            "site_name": "Site A",
            "controller_id": "controller-a",
        },
        clock=lambda: 1000.0,
    )
    app.state.enrollment_repair_coordinator = coordinator
    ticket = "T" * 43
    batch = store.create_or_adopt_batch(["mac-a"])
    request_id = batch["requests"][0]["request_id"]
    store.claim_next_dispatch()
    store.issue_ticket(
        request_id,
        target=TargetIdentity(
            "mac-a", "100.64.0.10", "100.64.0.10",
            "http://100.64.0.20:47873",
        ),
        controller=ControllerIdentity(
            "controller", "site-a", "Site A", "controller-a",
        ),
        fleet_token_digest=hashlib.sha256(FLEET_TOKEN.encode()).hexdigest(),
        ticket_digest=hashlib.sha256(ticket.encode()).hexdigest(),
        redemption_expires_at=1120.0,
    )
    store.mark_dispatched(request_id)
    body = {
        "schema": "studiohub.enrollment-repair-redemption",
        "schema_version": 1,
        "request_id": request_id,
        "target_machine_id": "mac-a",
        "ticket": ticket,
        "redemption_expires_at": 1120.0,
        "observed_identity": {
            "role": "standalone",
            "site_id": "wrong-site",
            "site_name": ticket,
            "controller_id": FLEET_TOKEN,
            "parent_controller_url": None,
        },
    }

    redeemed = TestClient(app, client=("100.64.0.10", 50000)).post(
        "/api/hub/enrollment-repair-tickets/redeem",
        headers={"X-Hub-Token": FLEET_TOKEN},
        json=body,
    )
    owner = TestClient(app, client=("127.0.0.1", 50000)).get(
        f"/api/hub/enrollment-repairs/{batch['batch_id']}",
    )

    assert redeemed.status_code == 200
    assert owner.status_code == 200
    assert owner.json()["requests"][0]["observed_identity"] == {
        "role_matches": False,
        "site_id_matches": False,
        "site_name_matches": False,
        "controller_id_matches": False,
        "parent_controller_url_matches": False,
    }
    durable = store.path.read_bytes()
    owner_bytes = owner.content
    assert ticket.encode() not in durable
    assert FLEET_TOKEN.encode() not in durable
    assert ticket.encode() not in owner_bytes
    assert FLEET_TOKEN.encode() not in owner_bytes


def _dispatch():
    return {
        "schema": "studiohub.enrollment-repair-dispatch",
        "schema_version": 1,
        "request_id": "request-a-000000",
        "target_machine_id": "mac-a",
        "ticket": "A" * 43,
        "redemption_expires_at": 9999999999.0,
        "controller_url": "http://100.64.0.20:47873",
        "controller": {
            "site_id": "site-a", "site_name": "Site A",
            "controller_id": "controller-a",
        },
    }


def _redemption():
    return {
        "schema": "studiohub.enrollment-repair-redemption",
        "schema_version": 1,
        "request_id": "request-a-000000",
        "target_machine_id": "mac-a",
        "ticket": "A" * 43,
        "redemption_expires_at": 9999999999.0,
        "observed_identity": {
            "role": "standalone", "site_id": "old-site",
            "site_name": "Old Site", "controller_id": "old-controller",
            "parent_controller_url": None,
        },
    }


SERVICE_ROUTES = (
    ("apply", "POST", "/api/hub/enrollment-repair/apply", _dispatch, "100.64.0.20"),
    ("redeem", "POST", "/api/hub/enrollment-repair-tickets/redeem", _redemption, "100.64.0.10"),
    ("status", "GET", "/api/hub/enrollment-repair/status/request-a", None, "100.64.0.20"),
)


def _service_request(app, route, *, host, headers=None, cookies=None, query=""):
    name, method, path, body_factory, _expected = route
    client = TestClient(app, client=(host, 50000), headers=headers or {})
    for key, value in (cookies or {}).items():
        client.cookies.set(key, value)
    url = path + query
    if method == "POST":
        return client.post(url, json=body_factory())
    return client.get(url)


@pytest.mark.parametrize("route", SERVICE_ROUTES, ids=lambda route: route[0])
def test_three_service_routes_accept_only_exact_current_fleet_header_and_expected_private_source(
    app, route,
):
    _set_controller()
    peers.set_fleet_token(FLEET_TOKEN)
    executor = ServiceExecutor()
    coordinator = ServiceCoordinator()
    _inject(app, coordinator=coordinator, executor=executor)
    registry.add_user_entries(registry.build_machine_entries(
        "100.64.0.10", "mac-a", ["image", "voice"],
    ))
    from backend.main import monitor
    monitor.reload_registry()

    response = _service_request(
        app, route, host=route[4], headers={"X-Hub-Token": FLEET_TOKEN},
    )

    assert response.status_code in {200, 202}
    assert len(executor.calls) + len(coordinator.calls) == 1


@pytest.mark.parametrize("route", SERVICE_ROUTES, ids=lambda route: route[0])
@pytest.mark.parametrize("substitute", [
    "missing", "wrong", "hub", "owner_cookie", "agent_cookie", "bearer",
    "query", "permanent", "public", "wrong_peer", "forwarded_spoof",
])
def test_three_service_routes_reject_every_credential_and_source_substitute_before_state_access(
    app, token, route, substitute,
):
    _set_controller()
    peers.set_fleet_token(FLEET_TOKEN)
    executor = ServiceExecutor()
    coordinator = ServiceCoordinator()
    _inject(app, coordinator=coordinator, executor=executor)
    registry.add_user_entries(registry.build_machine_entries(
        "100.64.0.10", "mac-a", ["image", "voice"],
    ))
    from backend.main import monitor
    monitor.reload_registry()
    headers = {}
    cookies = {}
    query = ""
    host = route[4]
    if substitute == "wrong":
        headers["X-Hub-Token"] = "wrong-fleet-token"
    elif substitute == "hub":
        headers["X-Hub-Token"] = token
    elif substitute in {"owner_cookie", "agent_cookie"}:
        cookies[auth.SESSION_COOKIE_NAME] = auth.create_browser_session()
    elif substitute == "bearer":
        headers["Authorization"] = f"Bearer {FLEET_TOKEN}"
    elif substitute == "query":
        query = f"?token={FLEET_TOKEN}"
    elif substitute == "permanent":
        headers["X-Hub-Token"] = PERMANENT_CODE_SENTINEL
    elif substitute == "public":
        headers["X-Hub-Token"] = FLEET_TOKEN
        host = "8.8.8.8"
    elif substitute == "wrong_peer":
        headers["X-Hub-Token"] = FLEET_TOKEN
        host = "100.64.0.99"
    elif substitute == "forwarded_spoof":
        headers.update({
            "X-Hub-Token": FLEET_TOKEN,
            "X-Forwarded-Host": route[4],
        })
        host = "100.64.0.99"

    response = _service_request(
        app, route, host=host, headers=headers, cookies=cookies, query=query,
    )

    assert response.status_code in {401, 403}
    assert executor.calls == []
    assert coordinator.calls == []


@pytest.mark.parametrize("state", ["absent", "empty", "whitespace"])
def test_all_three_service_routes_fail_closed_when_current_fleet_token_is_absent_or_empty_without_creating_it(
    app, state,
):
    _set_controller()
    executor = ServiceExecutor()
    coordinator = ServiceCoordinator()
    _inject(app, coordinator=coordinator, executor=executor)
    for path in (peers.FLEET_TOKEN_FILE, peers.SHARED_STUDIO_TOKEN_FILE):
        path.unlink(missing_ok=True)
    if state != "absent":
        peers.FLEET_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        peers.FLEET_TOKEN_FILE.write_bytes(b"" if state == "empty" else b" \n\t")
        peers.FLEET_TOKEN_FILE.chmod(0o640)
    before = {
        path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
        for path in (peers.FLEET_TOKEN_FILE, peers.SHARED_STUDIO_TOKEN_FILE)
        if path.exists()
    }

    for route in SERVICE_ROUTES:
        response = _service_request(
            app, route, host=route[4], headers={"X-Hub-Token": FLEET_TOKEN},
        )
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "fleet_token_unavailable"

    after = {
        path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
        for path in (peers.FLEET_TOKEN_FILE, peers.SHARED_STUDIO_TOKEN_FILE)
        if path.exists()
    }
    assert after == before
    assert executor.calls == []
    assert coordinator.calls == []


@pytest.mark.parametrize("path", [
    "/api/hub/enrollment-repair/apply",
    "/api/hub/enrollment-repair-tickets/redeem",
])
@pytest.mark.parametrize(
    ("credential_state", "expected_status", "expected_code"),
    [
        ("missing", 401, "fleet_token_mismatch"),
        ("wrong", 401, "fleet_token_mismatch"),
        ("foreign_origin_missing", 401, "fleet_token_mismatch"),
        ("absent_current", 503, "fleet_token_unavailable"),
    ],
)
def test_service_post_auth_precedes_body_parsing_and_generic_origin(
    app, path, credential_state, expected_status, expected_code,
):
    secret_input = "request-secret-that-must-not-be-reflected"
    headers = {"Content-Type": "application/json"}
    if credential_state == "absent_current":
        peers.FLEET_TOKEN_FILE.unlink(missing_ok=True)
        peers.SHARED_STUDIO_TOKEN_FILE.unlink(missing_ok=True)
        headers["X-Hub-Token"] = FLEET_TOKEN
    else:
        peers.set_fleet_token(FLEET_TOKEN)
        if credential_state == "wrong":
            headers["X-Hub-Token"] = "wrong-fleet-token"
        if credential_state == "foreign_origin_missing":
            headers["Origin"] = "https://evil.example"

    response = TestClient(app, client=("100.64.0.10", 50000)).post(
        path,
        headers=headers,
        content=json.dumps({"ticket": secret_input}),
    )

    assert response.status_code == expected_status
    assert response.json() == {"detail": {"code": expected_code}}
    assert secret_input not in response.text


@pytest.mark.parametrize("path", [
    "/api/hub/enrollment-repair/apply",
    "/api/hub/enrollment-repair-tickets/redeem",
])
def test_authenticated_service_validation_error_is_stable_and_redacted(app, path):
    peers.set_fleet_token(FLEET_TOKEN)
    secret_input = "authenticated-secret-that-must-not-be-reflected"

    response = TestClient(app, client=("100.64.0.10", 50000)).post(
        path,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Token": FLEET_TOKEN,
            "Origin": "https://evil.example",
        },
        content=json.dumps({"ticket": secret_input}),
    )

    assert response.status_code == 422
    assert response.json() == {"detail": {"code": "repair_request_invalid"}}
    assert secret_input not in response.text


def test_repair_request_models_forbid_unknown_fields(app):
    _set_controller()
    peers.set_fleet_token(FLEET_TOKEN)
    _inject(app, coordinator=ServiceCoordinator(), executor=ServiceExecutor())
    local = TestClient(app, client=("127.0.0.1", 50000))
    assert local.post(
        "/api/hub/enrollment-repairs",
        json={"machines": ["mac-a"], "ticket": "forbidden"},
    ).status_code == 422
    dispatch = _dispatch()
    dispatch["fleet_token"] = FLEET_TOKEN
    assert _service_request(
        app,
        ("apply", "POST", "/api/hub/enrollment-repair/apply", lambda: dispatch, "100.64.0.20"),
        host="100.64.0.20", headers={"X-Hub-Token": FLEET_TOKEN},
    ).status_code == 422


@pytest.mark.parametrize("invalid_deadline", [float("nan"), float("inf"), float("-inf")])
def test_repair_request_models_reject_non_finite_deadlines(invalid_deadline):
    from backend.main import EnrollmentRepairDispatchBody

    dispatch = _dispatch()
    dispatch["redemption_expires_at"] = invalid_deadline

    with pytest.raises(ValueError, match="finite number"):
        EnrollmentRepairDispatchBody.model_validate(dispatch)


def _permanent_code_row():
    with sqlite3.connect(enrollment.DB_FILE) as connection:
        connection.row_factory = sqlite3.Row
        return dict(connection.execute(
            "SELECT use_count, last_used_at FROM enrollment_codes WHERE kind = 'permanent'",
        ).fetchone())


class EnrollmentFenceCoordinator:
    def __init__(self, *, blocked_machine="mac-a", events=None):
        self.blocked_machine = blocked_machine
        self.events = events if events is not None else []

    def resolve_enrollment_registration(self, machine, host):
        self.events.append(("resolve", machine, host))
        return SimpleNamespace(machine=machine, host=host)

    @contextmanager
    def controller_mutation(self, *, identity=False, machine=None):
        self.events.append(("enter", machine))
        try:
            yield
        finally:
            self.events.append(("exit", machine))

    def require_enrollment_registration_mutable(self, machine, host, *, resolved=None):
        self.events.append(("require", machine, host, resolved))
        if machine == self.blocked_machine:
            raise RepairStoreError("enrollment_repair_busy")

    def note_registry_reload(self, rows):
        self.events.append(("reload", tuple(row["id"] for row in rows)))


def test_paused_claim_blocks_same_target_enrollment_before_permanent_code_consume(app):
    _set_controller()
    peers.set_fleet_token(FLEET_TOKEN)
    code = enrollment.create_enrollment_code(now=100)["code"]
    registry.add_user_entries(registry.build_machine_entries(
        "100.64.0.10", "mac-a", ["image", "voice"],
    ))
    registry.set_label("mac-a", "Mac A")
    hardware_profiles.set_machine_hardware_profile("mac-a", "mac-mini-m4-16gb")
    before = {
        "code": _permanent_code_row(),
        "registry": registry.REGISTRY_FILE.read_bytes(),
        "labels": registry.LABELS_FILE.read_bytes(),
        "profiles": hardware_profiles.MACHINE_PROFILES_FILE.read_bytes(),
    }
    coordinator = EnrollmentFenceCoordinator()
    _inject(app, coordinator=coordinator)

    response = TestClient(app, client=("100.64.0.10", 50000)).post(
        "/api/hub/enrollment/claim",
        json={
            "code": code, "machine": "mac-a", "machine_name": "Changed",
            "hardware_profile_id": "mac-mini-m2-8gb", "modalities": ["image"],
        },
    )

    assert response.status_code == 423
    assert _permanent_code_row() == before["code"]
    assert registry.REGISTRY_FILE.read_bytes() == before["registry"]
    assert registry.LABELS_FILE.read_bytes() == before["labels"]
    assert hardware_profiles.MACHINE_PROFILES_FILE.read_bytes() == before["profiles"]


def test_paused_claim_allows_unrelated_new_machine_enrollment(app):
    _set_controller()
    peers.set_fleet_token(FLEET_TOKEN)
    code = enrollment.create_enrollment_code(now=100)["code"]
    coordinator = EnrollmentFenceCoordinator(blocked_machine="mac-a")
    _inject(app, coordinator=coordinator)

    response = TestClient(app, client=("100.64.0.11", 50000)).post(
        "/api/hub/enrollment/claim",
        json={"code": code, "machine": "mac-b", "modalities": ["image", "voice"]},
    )

    assert response.status_code == 200
    assert response.json()["registration"]["machine"] == "mac-b"
    assert _permanent_code_row()["use_count"] == 1


def test_enrollment_claim_revalidates_target_inside_lock_before_code_consume(app):
    _set_controller()
    peers.set_fleet_token(FLEET_TOKEN)
    code = enrollment.create_enrollment_code(now=100)["code"]
    events = []
    coordinator = EnrollmentFenceCoordinator(events=events)
    _inject(app, coordinator=coordinator)

    response = TestClient(app, client=("100.64.0.10", 50000)).post(
        "/api/hub/enrollment/claim",
        json={"code": code, "machine": "mac-a", "modalities": ["image"]},
    )

    assert response.status_code == 423
    assert [event[0] for event in events[:3]] == ["resolve", "enter", "require"]
    assert _permanent_code_row()["use_count"] == 0


class ProbeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.is_success = 200 <= status_code < 300

    def json(self):
        return self._payload


class ProbeClient:
    def __init__(self, events):
        self.events = events

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url, **_kwargs):
        self.events.append(("network", url))
        if url.endswith("/api/health") and ":47868/" in url:
            return ProbeResponse(payload={"ok": True})
        if url.endswith("/api/version") and ":47868/" in url:
            return ProbeResponse(payload={"title": "Image Studio KH"})
        return ProbeResponse(404)


def test_every_main_registry_reload_notifies_repair_coordinator(
    app, monkeypatch,
):
    from backend import main

    _set_controller()
    coordinator = app.state.enrollment_repair_coordinator
    notifications = []
    monkeypatch.setattr(
        coordinator, "note_registry_reload",
        lambda rows: notifications.append(tuple(row["id"] for row in main.monitor.registry)),
    )
    client = TestClient(app, client=("127.0.0.1", 50000))

    added = client.post("/api/hub/registry/add", json={
        "host": "100.64.0.31", "machine": "mac-reload",
        "modalities": ["image", "voice"],
    })
    assert added.status_code == 200
    assert len(notifications) == 1

    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **_kwargs: ProbeClient([]))
    refetched = client.post("/api/hub/registry/discover", json={
        "host": "100.64.0.31", "machine": "mac-reload",
        "hardware_profile_id": "mac-mini-m4-16gb",
    })
    assert refetched.status_code == 200
    assert len(notifications) == 2

    removed = client.delete("/api/hub/registry/machines/mac-reload")
    assert removed.status_code == 200
    assert len(notifications) == 3

    reloaded = client.post("/api/hub/registry/reload")
    assert reloaded.status_code == 200
    assert len(notifications) == 4


@pytest.mark.parametrize("refetch", [False, True])
def test_discover_and_refetch_network_io_precedes_short_controller_mutation_lock(
    app, monkeypatch, refetch,
):
    from backend import main

    _set_controller()
    events = []
    if refetch:
        registry.add_user_entries(registry.build_machine_entries(
            "100.64.0.32", "mac-probe", ["voice"],
        ))
        main.monitor.reload_registry()

    class OrderedCoordinator:
        def resolve_enrollment_registration(self, machine, host):
            events.append(("resolve_registration", machine, host))
            return SimpleNamespace(machine=machine, host=host)

        def resolve_registry_rows(self, rows):
            events.append(("resolve_reload", len(rows)))
            return tuple(row["id"] for row in rows)

        @contextmanager
        def controller_mutation(self, *, identity=False, machine=None):
            events.append(("lock_enter", machine))
            try:
                yield
            finally:
                events.append(("lock_exit", machine))

        def require_enrollment_registration_mutable(self, machine, host, *, resolved=None):
            events.append(("revalidate", machine, host))

        def note_registry_reload(self, rows):
            events.append(("notify", rows))

    app.state.enrollment_repair_coordinator = OrderedCoordinator()
    monkeypatch.setattr(
        main.httpx, "AsyncClient", lambda **_kwargs: ProbeClient(events),
    )

    response = TestClient(app, client=("127.0.0.1", 50000)).post(
        "/api/hub/registry/discover",
        json={
            "host": "100.64.0.32", "machine": "mac-probe",
            "hardware_profile_id": "mac-mini-m4-16gb",
        },
    )

    assert response.status_code == 200
    kinds = [event[0] for event in events]
    assert "network" in kinds
    assert max(index for index, kind in enumerate(kinds) if kind == "network") < kinds.index("lock_enter")
    assert kinds.index("lock_enter") < kinds.index("revalidate") < kinds.index("notify") < kinds.index("lock_exit")


def _patch_lifecycle_dependencies(main, monkeypatch, events, *, failure=None):
    class Reconciler:
        def __init__(self, *_args, **_kwargs):
            events.append("release_created")

        async def start(self):
            events.append("release_start")

        async def stop(self):
            events.append("release_stop")

    class Store:
        def __init__(self):
            events.append("repair_store_created")

    class Executor:
        def __init__(self):
            events.append("repair_executor_created")

        def recover(self):
            events.append("repair_recover")
            if failure == "recover":
                raise RuntimeError("recover failed")

    class Coordinator:
        def __init__(self, store, **_kwargs):
            assert isinstance(store, Store)
            events.append("repair_coordinator_created")

        async def start(self):
            events.append("repair_start")
            if failure == "start":
                raise RuntimeError("start failed")

        async def stop(self):
            events.append("repair_stop")
            if failure == "stop":
                raise RuntimeError("stop failed")

    async def async_event(name):
        events.append(name)

    monkeypatch.setattr(
        main.auto_updater,
        "apply_scheduler",
        lambda: events.append("scheduler_reconciled"),
    )
    monkeypatch.setattr(main, "ReleaseReconciler", Reconciler)
    monkeypatch.setattr(main, "RepairStore", Store)
    monkeypatch.setattr(main, "RepairExecutor", Executor)
    monkeypatch.setattr(main, "EnrollmentRepairCoordinator", Coordinator)
    monkeypatch.setattr(main.execution_assets, "cleanup_expired", lambda: 0)
    monkeypatch.setattr(main.monitor, "start", lambda: events.append("monitor_start"))
    monkeypatch.setattr(main.monitor, "stop", lambda: async_event("monitor_stop"))
    monkeypatch.setattr(main.control_plane.runtime, "start", lambda *_args: async_event("runtime_start"))
    monkeypatch.setattr(main.control_plane.runtime, "stop", lambda: async_event("runtime_stop"))
    monkeypatch.setattr(main.fleet_ops, "start_published_version_monitor", lambda: None)
    monkeypatch.setattr(main.fleet_ops, "stop_published_version_monitor", lambda: async_event("version_stop"))
    monkeypatch.setattr(main.fleet_auto_updates, "resume_pending", lambda: 0)
    monkeypatch.setattr(main.broker, "restore_batches", lambda: 0)
    monkeypatch.setattr(main.broker, "start_dispatcher", lambda: None)
    monkeypatch.setattr(main.transcription_jobs, "restore_batches", lambda: 0)
    monkeypatch.setattr(main.transcription_jobs, "start_dispatcher", lambda *_args: None)
    monkeypatch.setattr(main.transcription_jobs, "stop", lambda: async_event("transcription_stop"))
    monkeypatch.setattr(main.chat_jobs, "restore_batches", lambda: 0)
    monkeypatch.setattr(main.chat_jobs, "start_dispatcher", lambda *_args: None)
    monkeypatch.setattr(main.chat_jobs, "stop", lambda: async_event("chat_stop"))
    monkeypatch.setattr(main.shared_voices, "start_reconciler", lambda *_args: None)
    monkeypatch.setattr(main.shared_voices, "stop", lambda: async_event("voices_stop"))
    monkeypatch.setattr(main.fleet_storage, "start", lambda *_args: None)
    monkeypatch.setattr(main.fleet_storage, "stop", lambda: async_event("storage_stop"))
    monkeypatch.setattr(main.model_baselines, "start", lambda: None)
    monkeypatch.setattr(main.model_baselines, "stop", lambda: async_event("baselines_stop"))
    main.release_reconciler = None
    main.peers.release_reconciler = None


@pytest.mark.asyncio
async def test_repair_lifecycle_recovers_and_starts_after_reconciler_then_stops_before_it(
    reset, monkeypatch,
):
    from backend import main

    events = []
    _patch_lifecycle_dependencies(main, monkeypatch, events)

    async with main.lifespan(main.app):
        assert events.index("scheduler_reconciled") < events.index("monitor_start")
        assert events.index("release_start") < events.index("repair_recover") < events.index("repair_start")

    assert events.index("repair_stop") < events.index("release_stop")
    assert main.release_reconciler is None
    assert main.peers.release_reconciler is None


@pytest.mark.parametrize(
    ("capability", "dispatches"),
    [
        ({"schema_version": 1}, False),
        ({"repair_schema_version": 0}, False),
        ({"repair_schema_version": "1"}, False),
        (None, False),
        ({"repair_schema_version": 1}, True),
    ],
    ids=["missing", "old", "malformed", "unreachable", "supported"],
)
@pytest.mark.asyncio
async def test_real_lifespan_capability_probe_fails_closed_before_apply(
    reset, monkeypatch, capability, dispatches,
):
    from contextlib import asynccontextmanager

    from backend import main

    _set_controller()
    peers.set_fleet_token(FLEET_TOKEN)
    registry.add_user_entries(registry.build_machine_entries(
        "100.64.0.10", "mac-a", ["image", "voice"],
    ))
    main.monitor.reload_registry()
    events = []
    requests = []
    _patch_lifecycle_dependencies(main, monkeypatch, events)

    class AgentConnection:
        direct_peer = "100.64.0.10"
        local_address = "100.64.0.20"

        async def connect(self, *, timeout):
            assert 0 < timeout <= 30

        async def request_json(self, method, path, *, headers, body, timeout):
            requests.append((method, path, dict(headers), body, timeout))
            if path == "/api/hub/enrollment/info":
                if capability is None:
                    raise OSError("unreachable")
                return 200, capability
            return 202, {"request_id": body["request_id"], "state": "accepted"}

    @asynccontextmanager
    async def connection_factory(origin):
        assert origin.address == "100.64.0.10"
        yield AgentConnection()

    class RealAppCoordinator(EnrollmentRepairCoordinator):
        def __init__(self, store, **kwargs):
            super().__init__(store, connection_factory=connection_factory, **kwargs)

    monkeypatch.setattr(main, "RepairStore", RepairStore)
    monkeypatch.setattr(main, "EnrollmentRepairCoordinator", RealAppCoordinator)
    monkeypatch.setattr(
        main.enrollment_repair, "open_pinned_json", connection_factory,
    )

    async with main.lifespan(main.app):
        coordinator = main.app.state.enrollment_repair_coordinator
        await coordinator.stop()
        batch = coordinator.store.create_or_adopt_batch(["mac-a"])
        request_id = batch["requests"][0]["request_id"]

        if dispatches:
            result = await coordinator.dispatch_next()
            assert result["target_machine_id"] == "mac-a"
        else:
            with pytest.raises(RepairStoreError, match="hub_update_required"):
                await coordinator.dispatch_next()
            assert coordinator.store.request(request_id)["state"] == "hub_update_required"

    expected_paths = ["/api/hub/enrollment/info"]
    if dispatches:
        expected_paths.append("/api/hub/enrollment-repair/apply")
    assert [path for _method, path, _headers, _body, _timeout in requests] == expected_paths


@pytest.mark.parametrize("failure", ["recover", "start", "stop"])
@pytest.mark.asyncio
async def test_repair_lifecycle_failure_still_cleans_release_runtime_and_monitor(
    reset, monkeypatch, failure,
):
    from backend import main

    events = []
    _patch_lifecycle_dependencies(main, monkeypatch, events, failure=failure)

    with pytest.raises(RuntimeError, match=f"{failure} failed"):
        async with main.lifespan(main.app):
            pass

    if failure == "start":
        assert "repair_stop" in events
    assert "release_stop" in events
    assert "runtime_stop" in events
    assert "monitor_stop" in events
    assert main.release_reconciler is None
    assert main.peers.release_reconciler is None
