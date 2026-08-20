import asyncio
import base64
import hashlib
import json
import sqlite3
import threading
from contextlib import asynccontextmanager

import pytest

from backend import peers, registry
from backend.enrollment_repair_store import (
    ControllerIdentity,
    RepairStore,
    RepairStoreError,
    TargetIdentity,
)


def _records(*machines):
    rows = []
    for machine, host, endpoints in machines:
        rows.extend({
            "id": f"{endpoint}@{machine}",
            "machine": machine,
            "host": host,
            "port": 47868 + index,
        } for index, endpoint in enumerate(endpoints))
    return rows


def _resolver(addresses):
    def resolve(host, port, *, type):
        return [
            (2, type, 6, "", (address, port))
            for address in addresses[host]
        ]
    return resolve


@pytest.fixture
def repair_store(reset):
    from backend import enrollment

    return RepairStore(enrollment.DB_FILE)


def _coordinator(repair_store, rows, addresses):
    from backend.enrollment_repair import EnrollmentRepairCoordinator

    return EnrollmentRepairCoordinator(
        repair_store,
        registry_loader=lambda: rows,
        token_reader=lambda: "fleet-token",
        settings_reader=lambda: {"role": "controller"},
        resolver=_resolver(addresses),
    )


class MutableClock:
    def __init__(self, value=1000.0):
        self.value = value

    def __call__(self):
        return self.value


class ConnectedDispatch:
    def __init__(
        self,
        *,
        direct_peer="100.64.0.10",
        local_address="100.64.0.20",
        callback=None,
        response=None,
        connect_error=None,
        on_connect=None,
    ):
        self.direct_peer = direct_peer
        self.local_address = local_address
        self.callback = callback
        self.response = response
        self.connect_error = connect_error
        self.on_connect = on_connect
        self.calls = []
        self.connected = False

    async def connect(self, *, timeout):
        assert 0 < timeout <= 30
        if self.on_connect is not None:
            self.on_connect()
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = True

    async def request_json(self, method, path, *, headers, body, timeout):
        assert self.connected
        self.calls.append((method, path, headers, body, timeout))
        if self.callback is not None:
            await self.callback(method, path, headers, body)
        response = self.response or {
            "request_id": body["request_id"], "state": "accepted",
        }
        return 202, dict(response)


def _connected_factory(connection, opened):
    @asynccontextmanager
    async def factory(origin):
        opened.append(origin)
        yield connection

    return factory


def _task8_coordinator(
    repair_store,
    rows,
    addresses,
    connection,
    *,
    clock,
    token_state=None,
    settings_state=None,
    opened=None,
    connection_factory=None,
    ticket_factory=None,
    **coordinator_options,
):
    from backend.enrollment_repair import EnrollmentRepairCoordinator

    token_state = token_state or {"value": "fleet-token"}
    settings_state = settings_state or {
        "role": "controller",
        "site_id": "site-a",
        "site_name": "Site A",
        "controller_id": "controller-a",
    }
    opened = opened if opened is not None else []
    return EnrollmentRepairCoordinator(
        repair_store,
        registry_loader=lambda: rows,
        token_reader=lambda: token_state["value"],
        settings_reader=lambda: dict(settings_state),
        resolver=_resolver(addresses),
        connection_factory=(
            connection_factory or _connected_factory(connection, opened)
        ),
        clock=clock,
        ticket_factory=ticket_factory or (lambda: "A" * 43),
        **coordinator_options,
    )


def _bootstrap_gates(**changes):
    gates = {
        "target_exact": True,
        "token_exact": True,
        "update_supported": True,
        "restart_verifiable": True,
        "hub_idle": True,
        "studios_idle": True,
        "enabled": True,
        "managed_release_state": None,
        "managed_release_contains_target": False,
        "conflict_free": True,
        "published_version": "2.9.0",
        "published_repair_schema_version": 1,
    }
    gates.update(changes)
    return gates


def _live_claim(store, *, machine="mac-a", host="agent-a.test", address="100.64.0.10"):
    request_id = store.create_or_adopt_batch([machine])["requests"][0]["request_id"]
    store.claim_next_dispatch()
    target = TargetIdentity(machine, host, address, "http://100.64.0.20:47873")
    controller = ControllerIdentity("controller", "site-a", "Site A", "controller-a")
    expires_at = float(store.clock()) + 120.0
    store.issue_ticket(
        request_id,
        target=target,
        controller=controller,
        fleet_token_digest=hashlib.sha256(b"fleet-token").hexdigest(),
        ticket_digest=hashlib.sha256(b"A" * 43).hexdigest(),
        redemption_expires_at=expires_at,
    )
    store.mark_dispatched(request_id)
    store.redeem(
        request_id,
        ticket="A" * 43,
        redemption_expires_at=expires_at,
        target_machine=machine,
        direct_source=address,
        observed_identity={"role": "standalone"},
        registry_snapshot=target,
        controller=controller,
        fleet_token_digest=hashlib.sha256(b"fleet-token").hexdigest(),
    )
    store.park(request_id)
    return request_id


def test_eligibility_requires_one_remote_machine_one_host_one_private_address(repair_store):
    rows = _records(
        ("local", "127.0.0.1", ["image"]),
        ("mac-a", "agent-a.test", ["image", "voice"]),
    )
    coordinator = _coordinator(repair_store, rows, {"agent-a.test": ["100.64.0.10"]})

    result = coordinator.eligibility()

    assert result["issuance_enabled"] is True
    assert result["machines"] == [{
        "machine": "mac-a", "display_label": "mac-a", "host": "agent-a.test",
        "eligible": True, "code": "eligible", "detail": "Eligible for enrollment repair.",
        "request_state": None,
    }]


def test_duplicate_host_and_multi_host_machine_are_needs_review_not_guessed(repair_store):
    rows = _records(
        ("mac-a", "shared.test", ["image"]),
        ("mac-b", "shared.test", ["voice"]),
        ("mac-c", "agent-c.test", ["image"]),
    ) + [{
        "id": "voice@mac-c", "machine": "mac-c", "host": "other-c.test", "port": 47870,
    }]
    coordinator = _coordinator(repair_store, rows, {
        "shared.test": ["100.64.0.10"],
        "agent-c.test": ["100.64.0.11"],
        "other-c.test": ["100.64.0.12"],
    })

    by_machine = {row["machine"]: row for row in coordinator.eligibility()["machines"]}

    assert by_machine["mac-a"]["code"] == "host_shared"
    assert by_machine["mac-b"]["code"] == "host_shared"
    assert by_machine["mac-c"]["code"] == "machine_multi_host"
    assert not any(row["eligible"] for row in by_machine.values())
    assert coordinator.create_batch(["mac-a", "mac-b", "mac-c"])["requests"] == []


def test_local_unknown_and_duplicate_machine_ids_are_excluded(repair_store):
    rows = _records(
        ("local", "127.0.0.1", ["image"]),
        ("mac-a", "agent-a.test", ["image"]),
    )
    coordinator = _coordinator(repair_store, rows, {"agent-a.test": ["100.64.0.10"]})

    batch = coordinator.create_batch(["local", "missing", "mac-a", "mac-a"])

    assert [row["target_machine"] for row in batch["requests"]] == ["mac-a"]
    assert batch["rejected"] == {
        "local": "machine_local", "missing": "machine_missing",
    }


@pytest.mark.parametrize("contents", [None, b" \n\t"])
def test_missing_or_empty_current_fleet_token_is_ineligible_without_creating_one(
    repair_store, monkeypatch, contents,
):
    peers.FLEET_TOKEN_FILE.unlink(missing_ok=True)
    peers.SHARED_STUDIO_TOKEN_FILE.unlink(missing_ok=True)
    if contents is not None:
        peers.FLEET_TOKEN_FILE.write_bytes(contents)
        peers.FLEET_TOKEN_FILE.chmod(0o640)
    before = (
        peers.FLEET_TOKEN_FILE.read_bytes() if peers.FLEET_TOKEN_FILE.exists() else None,
        peers.FLEET_TOKEN_FILE.stat().st_mode if peers.FLEET_TOKEN_FILE.exists() else None,
    )
    monkeypatch.delenv("STUDIOHUB_FLEET_TOKEN", raising=False)
    from backend.enrollment_repair import EnrollmentRepairCoordinator

    rows = _records(("mac-a", "agent-a.test", ["image"]))
    coordinator = EnrollmentRepairCoordinator(
        repair_store, registry_loader=lambda: rows,
        settings_reader=lambda: {"role": "controller"},
        resolver=_resolver({"agent-a.test": ["100.64.0.10"]}),
    )

    eligibility = coordinator.eligibility()
    batch = coordinator.create_batch(["mac-a"])

    assert eligibility["machines"][0]["code"] == "fleet_token_missing"
    assert batch["requests"] == []
    after = (
        peers.FLEET_TOKEN_FILE.read_bytes() if peers.FLEET_TOKEN_FILE.exists() else None,
        peers.FLEET_TOKEN_FILE.stat().st_mode if peers.FLEET_TOKEN_FILE.exists() else None,
    )
    assert after == before
    assert not peers.SHARED_STUDIO_TOKEN_FILE.exists()


def test_batch_re_evaluates_server_side_and_orders_by_stable_machine_id(repair_store):
    rows = _records(
        ("mac-z", "agent-z.test", ["image"]),
        ("mac-a", "agent-a.test", ["voice"]),
    )
    coordinator = _coordinator(repair_store, rows, {
        "agent-a.test": ["100.64.0.10"], "agent-z.test": ["100.64.0.11"],
    })
    assert all(row["eligible"] for row in coordinator.eligibility()["machines"])
    rows.append({"id": "image@mac-a", "machine": "mac-a", "host": "changed.test", "port": 47868})

    batch = coordinator.create_batch(["mac-z", "mac-a"])

    assert [row["target_machine"] for row in batch["requests"]] == ["mac-z"]
    assert batch["rejected"] == {"mac-a": "machine_multi_host"}


def test_double_click_adopts_same_unresolved_request(repair_store):
    rows = _records(("mac-a", "agent-a.test", ["image"]))
    coordinator = _coordinator(repair_store, rows, {"agent-a.test": ["100.64.0.10"]})

    first = coordinator.create_batch(["mac-a"])
    second = coordinator.create_batch(["mac-a"])

    assert first["requests"][0]["request_id"] == second["requests"][0]["request_id"]
    assert first["requests"][0]["state"] == second["requests"][0]["state"] == "queued"
    with sqlite3.connect(repair_store.path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM enrollment_repair_requests WHERE ticket_digest IS NOT NULL"
        ).fetchone()[0] == 0


@pytest.mark.parametrize("role", ["standalone", "agent"])
def test_non_controller_role_is_ineligible_and_cannot_persist_a_batch(repair_store, role):
    from backend.enrollment_repair import EnrollmentRepairCoordinator

    rows = _records(("mac-a", "agent-a.test", ["image"]))
    coordinator = EnrollmentRepairCoordinator(
        repair_store,
        registry_loader=lambda: rows,
        token_reader=lambda: "fleet-token",
        settings_reader=lambda: {"role": role},
        resolver=_resolver({"agent-a.test": ["100.64.0.10"]}),
    )

    eligibility = coordinator.eligibility()
    batch = coordinator.create_batch(["mac-a"])

    assert eligibility["machines"][0]["eligible"] is False
    assert eligibility["machines"][0]["code"] == "controller_role_required"
    assert batch == {"requests": [], "rejected": {"mac-a": "controller_role_required"}}
    with sqlite3.connect(repair_store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM enrollment_repair_requests").fetchone()[0] == 0


def test_non_controller_eligibility_does_not_resolve_remote_hosts(repair_store):
    from backend.enrollment_repair import EnrollmentRepairCoordinator

    rows = _records(("mac-a", "agent-a.test", ["image"]))
    coordinator = EnrollmentRepairCoordinator(
        repair_store,
        registry_loader=lambda: rows,
        token_reader=lambda: "fleet-token",
        settings_reader=lambda: {"role": "agent"},
        resolver=lambda *args, **kwargs: pytest.fail("non-controller resolved a remote host"),
    )

    assert coordinator.eligibility()["machines"][0]["code"] == "controller_role_required"


def test_mixed_batch_adopts_live_target_and_queues_other_machine(repair_store):
    rows = _records(
        ("mac-a", "agent-a.test", ["image"]),
        ("mac-b", "agent-b.test", ["voice"]),
    )
    coordinator = _coordinator(repair_store, rows, {
        "agent-a.test": ["100.64.0.10"], "agent-b.test": ["100.64.0.11"],
    })
    first = coordinator.create_batch(["mac-a"])
    first_request = first["requests"][0]["request_id"]

    mixed = coordinator.create_batch(["mac-a", "mac-b"])

    assert [row["target_machine"] for row in mixed["requests"]] == ["mac-b"]
    assert [row["request_id"] for row in mixed["adopted_requests"]] == [first_request]
    assert mixed["adopted_requests"][0]["state"] == "queued"


def test_exact_mixed_batch_replay_adopts_the_same_queued_and_live_requests(repair_store):
    rows = _records(
        ("mac-a", "agent-a.test", ["image"]),
        ("mac-b", "agent-b.test", ["voice"]),
    )
    coordinator = _coordinator(repair_store, rows, {
        "agent-a.test": ["100.64.0.10"], "agent-b.test": ["100.64.0.11"],
    })
    coordinator.create_batch(["mac-a"])
    first = coordinator.create_batch(["mac-a", "mac-b"])

    replay = coordinator.create_batch(["mac-b", "mac-a"])

    assert replay["batch_id"] == first["batch_id"]
    assert replay["requests"] == first["requests"]
    assert replay["adopted_requests"] == first["adopted_requests"]


def test_resolved_preclaim_ambiguity_can_retry_without_unlocking_agent_review(repair_store):
    rows = _records(("mac-a", "agent-a.test", ["image"]))
    coordinator = _coordinator(repair_store, rows, {"agent-a.test": ["100.64.0.10"]})
    first = coordinator.create_batch(["mac-a"])
    request_id = first["requests"][0]["request_id"]
    repair_store.fail_before_claim(request_id, state="needs_review", error_code="host_shared")

    retry = coordinator.create_batch(["mac-a"])

    assert retry["requests"][0]["request_id"] != request_id
    assert retry["requests"][0]["state"] == "queued"
    live = retry["requests"][0]["request_id"]
    repair_store.issue_ticket(
        live,
        target=TargetIdentity(
            "mac-a", "agent-a.test", "100.64.0.10", "http://100.64.0.20:47873",
        ),
        controller=ControllerIdentity(
            "controller", "site", "Site", "controller",
        ),
        fleet_token_digest="a" * 64,
        ticket_digest=hashlib.sha256(b"repair-ticket").hexdigest(),
        redemption_expires_at=9999999999.0,
    )
    repair_store.redeem(
        live, ticket="repair-ticket", redemption_expires_at=9999999999.0,
        target_machine="mac-a", direct_source="100.64.0.10",
        observed_identity={},
        registry_snapshot=TargetIdentity("mac-a", "agent-a.test", "100.64.0.10", "http://100.64.0.20:47873"),
        controller=ControllerIdentity("controller", "site", "Site", "controller"),
        fleet_token_digest="a" * 64,
    )
    repair_store.adopt_status(
        live, {"request_id": live, "state": "needs_review"}, direct_source="100.64.0.10",
    )
    # An Agent-side review remains the same locked request after registry recovery.
    assert coordinator.create_batch(["mac-a"])["requests"][0]["request_id"] == live


@pytest.mark.asyncio
async def test_controller_stores_digest_before_dispatch_and_plaintext_only_in_memory(
    repair_store,
):
    clock = MutableClock()
    repair_store.clock = clock
    rows = _records(("mac-a", "agent-a.test", ["image", "voice"]))
    addresses = {"agent-a.test": ["100.64.0.10"]}
    inspected = {}

    async def inspect_committed_row(method, path, headers, body):
        with sqlite3.connect(repair_store.path) as connection:
            connection.row_factory = sqlite3.Row
            row = dict(connection.execute(
                "SELECT * FROM enrollment_repair_requests WHERE request_id = ?",
                (body["request_id"],),
            ).fetchone())
        inspected.update(row=row, body=dict(body), headers=dict(headers))
        assert method == "POST"
        assert path == "/api/hub/enrollment-repair/apply"
        assert row["state"] == "ticket_issued"
        assert row["ticket_status"] == "issued"
        assert row["ticket_digest"] == hashlib.sha256(body["ticket"].encode()).hexdigest()
        assert row["fleet_token_digest"] == hashlib.sha256(b"fleet-token").hexdigest()
        assert row["issued_at"] == 1000.0
        assert row["redemption_expires_at"] == 1120.0
        padding = "=" * (-len(body["ticket"]) % 4)
        assert len(base64.urlsafe_b64decode(body["ticket"] + padding)) == 32
        raw = repair_store.path.read_bytes()
        assert body["ticket"].encode() not in raw
        assert b"fleet-token" not in raw

    connection = ConnectedDispatch(callback=inspect_committed_row)
    coordinator = _task8_coordinator(
        repair_store, rows, addresses, connection, clock=clock,
    )
    batch = coordinator.create_batch(["mac-a"])

    result = await coordinator.dispatch_next()

    assert result == {
        "request_id": batch["requests"][0]["request_id"],
        "target_machine_id": "mac-a",
        "status_code": 202,
        "response": {"request_id": batch["requests"][0]["request_id"], "state": "accepted"},
    }
    assert set(inspected["body"]) == {
        "schema", "schema_version", "request_id", "target_machine_id", "ticket",
        "redemption_expires_at", "controller_url", "controller",
    }
    assert inspected["headers"] == {"X-Hub-Token": "fleet-token"}
    assert "ticket" not in json.dumps(result)
    assert "fleet-token" not in json.dumps(result)


@pytest.mark.asyncio
async def test_dispatch_uses_same_socket_that_selected_controller_origin(repair_store):
    clock = MutableClock()
    repair_store.clock = clock
    rows = _records(("mac-a", "agent-a.test", ["image"]))
    addresses = {"agent-a.test": ["100.64.0.10"]}
    opened = []
    connection = ConnectedDispatch(
        direct_peer="100.64.0.10", local_address="100.64.0.20",
    )
    coordinator = _task8_coordinator(
        repair_store, rows, addresses, connection, clock=clock, opened=opened,
    )
    batch = coordinator.create_batch(["mac-a"])

    await coordinator.dispatch_next()

    assert len(opened) == 1
    assert opened[0].address == connection.direct_peer
    body = connection.calls[0][3]
    assert body["controller_url"] == "http://100.64.0.20:47873"
    with sqlite3.connect(repair_store.path) as database:
        row = database.execute(
            """SELECT resolved_address, controller_url, state
               FROM enrollment_repair_requests WHERE request_id = ?""",
            (batch["requests"][0]["request_id"],),
        ).fetchone()
    assert row == (connection.direct_peer, body["controller_url"], "dispatched")


@pytest.mark.asyncio
async def test_dispatch_missing_current_token_does_not_claim_or_mutate_request(repair_store):
    clock = MutableClock()
    repair_store.clock = clock
    rows = _records(("mac-a", "agent-a.test", ["image"]))
    addresses = {"agent-a.test": ["100.64.0.10"]}
    token_state = {"value": "fleet-token"}
    connection = ConnectedDispatch()
    coordinator = _task8_coordinator(
        repair_store, rows, addresses, connection, clock=clock,
        token_state=token_state,
    )
    coordinator.create_batch(["mac-a"])
    before = repair_store.path.read_bytes()
    token_state["value"] = None

    with pytest.raises(RepairStoreError, match="fleet_token_missing"):
        await coordinator.dispatch_next()

    assert repair_store.path.read_bytes() == before
    assert connection.calls == []


@pytest.mark.asyncio
async def test_empty_dispatch_queue_does_not_require_a_fleet_token(repair_store):
    """A fresh Standalone Hub must become ready without enrollment state."""
    from backend.enrollment_repair import EnrollmentRepairCoordinator

    token_reads = []
    coordinator = EnrollmentRepairCoordinator(
        repair_store,
        registry_loader=lambda: [],
        token_reader=lambda: token_reads.append(True) or None,
        settings_reader=lambda: {"role": "standalone"},
    )

    assert await coordinator.dispatch_next() is None
    assert token_reads == []


@pytest.mark.asyncio
async def test_feature_disable_leaves_queued_and_recovered_rows_without_issuing(
    repair_store, monkeypatch,
):
    from backend import enrollment_repair

    clock = MutableClock()
    repair_store.clock = clock
    rows = _records(("mac-a", "agent-a.test", ["image"]))
    connection = ConnectedDispatch()
    tickets = []
    coordinator = _task8_coordinator(
        repair_store, rows, {"agent-a.test": ["100.64.0.10"]},
        connection, clock=clock,
        ticket_factory=lambda: tickets.append("A" * 43) or tickets[-1],
    )
    request_id = coordinator.create_batch(["mac-a"])["requests"][0]["request_id"]
    monkeypatch.setattr(enrollment_repair, "NEW_ISSUANCE_ENABLED", False)

    assert await coordinator.dispatch_next() is None

    restarted = _task8_coordinator(
        RepairStore(repair_store.path, clock=clock), rows,
        {"agent-a.test": ["100.64.0.10"]}, connection, clock=clock,
        ticket_factory=lambda: tickets.append("B" * 43) or tickets[-1],
    )
    assert await restarted.dispatch_next() is None
    assert connection.connected is False
    assert connection.calls == []
    assert tickets == []
    with sqlite3.connect(repair_store.path) as database:
        assert database.execute(
            """SELECT state, ticket_status, ticket_digest, fleet_token_digest,
                      issued_at, dispatch_slot
               FROM enrollment_repair_requests WHERE request_id = ?""",
            (request_id,),
        ).fetchone() == ("queued", None, None, None, None, None)


@pytest.mark.parametrize(
    ("failure", "expected_state", "expected_code"),
    [
        ("offline", "retryable", "transport_unavailable"),
        ("resolution", "needs_review", "host_address_ambiguous"),
        ("connect", "retryable", "transport_unavailable"),
        ("controller_origin", "needs_review", "callback_url_invalid"),
        ("registry_changed", "needs_review", "host_address_changed"),
        ("token_missing", "needs_review", "fleet_token_missing"),
        ("token_rotated", "needs_review", "fleet_token_mismatch"),
        ("controller_snapshot", "retryable", "controller_snapshot_changed"),
        ("ticket_generation", "retryable", "ticket_generation_failed"),
        ("precommit", "retryable", "request_not_issuable"),
    ],
)
@pytest.mark.asyncio
async def test_preissuance_failure_releases_slot_and_dispatches_next(
    repair_store, failure, expected_state, expected_code, monkeypatch,
):
    from backend.enrollment_repair_transport import PinnedTransportError

    clock = MutableClock()
    repair_store.clock = clock
    rows = _records(
        ("mac-a", "agent-a.test", ["image"]),
        ("mac-b", "agent-b.test", ["voice"]),
    )
    addresses = {
        "agent-a.test": ["100.64.0.10"],
        "agent-b.test": ["100.64.0.11"],
    }
    token_state = {"value": "fleet-token"}
    settings_state = {
        "role": "controller", "site_id": "site-a", "site_name": "Site A",
        "controller_id": "controller-a",
    }
    opened = []
    first = ConnectedDispatch()
    second = ConnectedDispatch(direct_peer="100.64.0.11")
    ticket_factory = None
    first_connection = first
    if failure == "offline":
        first_connection = PinnedTransportError("transport_unavailable")
    elif failure == "connect":
        first.connect_error = PinnedTransportError("transport_timeout")
    elif failure == "controller_origin":
        first.local_address = "8.8.8.8"
    elif failure == "registry_changed":
        first.on_connect = lambda: addresses.update(
            {"agent-a.test": ["100.64.0.12"]}
        )
    elif failure == "token_missing":
        first.on_connect = lambda: token_state.update(value=None)
    elif failure == "token_rotated":
        first.on_connect = lambda: token_state.update(value="rotated-token")
    elif failure == "ticket_generation":
        ticket_calls = {"count": 0}

        def ticket_factory():
            ticket_calls["count"] += 1
            return "bad" if ticket_calls["count"] == 1 else "A" * 43
    @asynccontextmanager
    async def connection_factory(origin):
        opened.append(origin)
        connection = second if origin.address == "100.64.0.11" else first_connection
        if isinstance(connection, BaseException):
            raise connection
        yield connection
    coordinator = _task8_coordinator(
        repair_store, rows, addresses, first, clock=clock,
        token_state=token_state, settings_state=settings_state,
        opened=opened, connection_factory=connection_factory,
        ticket_factory=ticket_factory,
    )
    batch = coordinator.create_batch(["mac-a", "mac-b"])
    if failure == "resolution":
        addresses["agent-a.test"] = []
    elif failure == "controller_snapshot":
        settings_state["site_id"] = ""
    elif failure == "precommit":
        original_issue = repair_store.issue_ticket
        calls = {"count": 0}

        def fail_once(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RepairStoreError("request_not_issuable")
            return original_issue(*args, **kwargs)

        monkeypatch.setattr(repair_store, "issue_ticket", fail_once)

    with pytest.raises(Exception):
        await coordinator.dispatch_next()

    with sqlite3.connect(repair_store.path) as database:
        failed = database.execute(
            """SELECT state, error_code, dispatch_slot, ticket_status,
                      ticket_digest, fleet_token_digest, issued_at
               FROM enrollment_repair_requests WHERE target_machine = 'mac-a'"""
        ).fetchone()
    assert failed == (
        expected_state, expected_code, None, None, None, None, None,
    )
    assert first.calls == []

    addresses["agent-a.test"] = ["100.64.0.10"]
    token_state["value"] = "fleet-token"
    settings_state["site_id"] = "site-a"
    result = await coordinator.dispatch_next()

    assert result["target_machine_id"] == "mac-b"
    assert second.calls[0][3]["target_machine_id"] == "mac-b"
    assert [row["target_machine"] for row in batch["requests"]] == ["mac-a", "mac-b"]


@pytest.mark.asyncio
async def test_dispatch_response_redacts_ticket_and_token_echoes(repair_store):
    clock = MutableClock()
    repair_store.clock = clock
    rows = _records(("mac-a", "agent-a.test", ["image"]))
    addresses = {"agent-a.test": ["100.64.0.10"]}
    connection = ConnectedDispatch(response={
        "request_id": "not-authoritative",
        "state": "accepted",
        "ticket": "echoed-ticket",
        "fleet_token": "echoed-token",
        "error_code": "echoed-ticket",
        "nested": {"authorization": "echoed-secret"},
    })
    coordinator = _task8_coordinator(
        repair_store, rows, addresses, connection, clock=clock,
    )
    batch = coordinator.create_batch(["mac-a"])

    result = await coordinator.dispatch_next()

    assert result["response"] == {
        "request_id": batch["requests"][0]["request_id"],
        "state": "accepted",
    }
    assert "echoed" not in json.dumps(result)


@pytest.mark.asyncio
async def test_dispatch_response_rejects_nonexact_identity_echo(repair_store):
    clock = MutableClock()
    repair_store.clock = clock
    rows = _records(("mac-a", "agent-a.test", ["image"]))
    addresses = {"agent-a.test": ["100.64.0.10"]}
    connection = ConnectedDispatch(response={
        "state": "complete",
        "identity": {
            "role": "agent", "site_id": "echoed-ticket", "site_name": "Site A",
            "controller_id": "mac-a",
        },
    })
    coordinator = _task8_coordinator(
        repair_store, rows, addresses, connection, clock=clock,
    )
    batch = coordinator.create_batch(["mac-a"])

    result = await coordinator.dispatch_next()

    assert result["response"] == {
        "request_id": batch["requests"][0]["request_id"],
        "state": "complete",
    }
    assert "echoed" not in json.dumps(result)


@pytest.mark.parametrize(
    "changed_binding",
    [
        "request", "target", "registry", "source", "controller", "token",
        "token_missing", "expiry", "ticket", "state",
    ],
)
@pytest.mark.asyncio
async def test_redeem_rejects_each_changed_request_target_registry_source_controller_and_token_binding(
    repair_store, changed_binding,
):
    clock = MutableClock()
    repair_store.clock = clock
    rows = _records(
        ("mac-a", "agent-a.test", ["image"]),
        ("mac-b", "agent-b.test", ["voice"]),
    )
    addresses = {
        "agent-a.test": ["100.64.0.10"],
        "agent-b.test": ["100.64.0.11"],
        "changed.test": ["100.64.0.12"],
    }
    token_state = {"value": "fleet-token"}
    settings_state = {
        "role": "controller", "site_id": "site-a", "site_name": "Site A",
        "controller_id": "controller-a",
    }
    connection = ConnectedDispatch()
    coordinator = _task8_coordinator(
        repair_store, rows, addresses, connection, clock=clock,
        token_state=token_state, settings_state=settings_state,
    )
    batch = coordinator.create_batch(["mac-a"])
    request_id = batch["requests"][0]["request_id"]
    await coordinator.dispatch_next()
    dispatch_body = connection.calls[0][3]
    redemption = {
        "schema": "studiohub.enrollment-repair-redemption",
        "schema_version": 1,
        "request_id": request_id,
        "target_machine_id": "mac-a",
        "ticket": dispatch_body["ticket"],
        "redemption_expires_at": dispatch_body["redemption_expires_at"],
        "observed_identity": {
            "role": "standalone", "site_id": "old", "site_name": "Old",
            "controller_id": "old", "parent_controller_url": None,
        },
    }
    direct_source = "100.64.0.10"
    header_token = "fleet-token"
    if changed_binding == "request":
        redemption["request_id"] = "different-request-0000000000000000"
    elif changed_binding == "target":
        redemption["target_machine_id"] = "mac-b"
        direct_source = "100.64.0.11"
    elif changed_binding == "registry":
        for row in rows:
            if row["machine"] == "mac-a":
                row["host"] = "changed.test"
        direct_source = "100.64.0.12"
    elif changed_binding == "source":
        direct_source = "100.64.0.12"
    elif changed_binding == "controller":
        settings_state["site_name"] = "Changed Site"
    elif changed_binding == "token":
        token_state["value"] = "rotated-fleet-token"
        header_token = "rotated-fleet-token"
    elif changed_binding == "token_missing":
        token_state["value"] = None
    elif changed_binding == "expiry":
        redemption["redemption_expires_at"] += 1
    elif changed_binding == "ticket":
        redemption["ticket"] = "B" * len(redemption["ticket"])
    elif changed_binding == "state":
        with sqlite3.connect(repair_store.path) as database:
            database.execute(
                "UPDATE enrollment_repair_requests SET state = 'confirmation_pending' WHERE request_id = ?",
                (request_id,),
            )

    with pytest.raises((RepairStoreError, registry.RepairRegistryAmbiguity)):
        await coordinator.redeem(
            redemption, direct_source=direct_source, fleet_token=header_token,
        )

    with sqlite3.connect(repair_store.path) as database:
        row = database.execute(
            """SELECT ticket_status, redeemed_at, direct_source
               FROM enrollment_repair_requests WHERE request_id = ?""",
            (request_id,),
        ).fetchone()
    assert row == ("issued", None, None)


@pytest.mark.asyncio
async def test_redeem_returns_exact_claim_once_before_expiry(
    repair_store, monkeypatch,
):
    from backend import enrollment_repair

    clock = MutableClock()
    repair_store.clock = clock
    rows = _records(
        ("mac-a", "agent-a.test", ["image"]),
        ("mac-b", "agent-b.test", ["voice"]),
    )
    addresses = {
        "agent-a.test": ["100.64.0.10"],
        "agent-b.test": ["100.64.0.11"],
    }
    connection = ConnectedDispatch()
    coordinator = _task8_coordinator(
        repair_store, rows, addresses, connection, clock=clock,
    )
    first_batch = coordinator.create_batch(["mac-a"])
    first_request = first_batch["requests"][0]["request_id"]
    await coordinator.dispatch_next()
    first_dispatch = connection.calls[-1][3]
    redemption = {
        "schema": "studiohub.enrollment-repair-redemption",
        "schema_version": 1,
        "request_id": first_request,
        "target_machine_id": "mac-a",
        "ticket": first_dispatch["ticket"],
        "redemption_expires_at": first_dispatch["redemption_expires_at"],
        "observed_identity": {
            "role": "standalone", "site_id": "old", "site_name": "Old",
            "controller_id": "old", "parent_controller_url": None,
        },
    }
    clock.value = 1119.999
    monkeypatch.setattr(enrollment_repair, "NEW_ISSUANCE_ENABLED", False)

    claim = await coordinator.redeem(
        redemption, direct_source="100.64.0.10", fleet_token="fleet-token",
    )

    assert claim == {
        "schema": "studiohub.enrollment-repair-claim",
        "schema_version": 1,
        "request_id": first_request,
        "target_machine_id": "mac-a",
        "role": "agent",
        "site_id": "site-a",
        "site_name": "Site A",
        "controller_id": "mac-a",
    }
    assert set(claim) == {
        "schema", "schema_version", "request_id", "target_machine_id", "role",
        "site_id", "site_name", "controller_id",
    }
    assert not ({"fleet_token", "ticket", "controller_url", "expires_at", "timer"} & claim.keys())
    with pytest.raises(RepairStoreError, match="ticket_replayed"):
        await coordinator.redeem(
            redemption, direct_source="100.64.0.10", fleet_token="fleet-token",
        )
    monkeypatch.setattr(enrollment_repair, "NEW_ISSUANCE_ENABLED", True)
    repair_store.adopt_status(
        first_request,
        {
            "request_id": first_request,
            "state": "complete",
            "identity": {
                "role": "agent", "site_id": "site-a", "site_name": "Site A",
                "controller_id": "mac-a",
            },
        },
        direct_source="100.64.0.10",
    )

    second_batch = coordinator.create_batch(["mac-b"])
    second_request = second_batch["requests"][0]["request_id"]
    clock.value = 1000.0
    connection.direct_peer = "100.64.0.11"
    await coordinator.dispatch_next()
    second_dispatch = connection.calls[-1][3]
    second_redemption = dict(redemption)
    second_redemption.update(
        request_id=second_request,
        target_machine_id="mac-b",
        ticket=second_dispatch["ticket"],
        redemption_expires_at=second_dispatch["redemption_expires_at"],
    )
    clock.value = second_dispatch["redemption_expires_at"]
    monkeypatch.setattr(enrollment_repair, "NEW_ISSUANCE_ENABLED", False)

    with pytest.raises(RepairStoreError, match="ticket_expired"):
        await coordinator.redeem(
            second_redemption, direct_source="100.64.0.11", fleet_token="fleet-token",
        )
    with sqlite3.connect(repair_store.path) as database:
        assert database.execute(
            "SELECT ticket_status FROM enrollment_repair_requests WHERE request_id = ?",
            (second_request,),
        ).fetchone() == ("expired",)


@pytest.mark.asyncio
async def test_foreground_wait_parks_uncertain_mac_then_dispatches_next(
    repair_store, monkeypatch,
):
    """A lost terminal response must park only its target after the 15s slot."""
    from backend import enrollment_repair

    clock = MutableClock()
    repair_store.clock = clock
    rows = _records(
        ("mac-a", "agent-a.test", ["image"]),
        ("mac-b", "agent-b.test", ["voice"]),
    )
    coordinator = _task8_coordinator(
        repair_store, rows,
        {"agent-a.test": ["100.64.0.10"], "agent-b.test": ["100.64.0.11"]},
        ConnectedDispatch(), clock=clock,
    )
    batch = coordinator.create_batch(["mac-a", "mac-b"])
    dispatched = []

    async def dispatch():
        request = repair_store.claim_next_dispatch()
        if request is None:
            return None
        dispatched.append(request["target_machine"])
        if request["target_machine"] == "mac-a":
            repair_store.issue_ticket(
                request["request_id"], target=TargetIdentity(
                    "mac-a", "agent-a.test", "100.64.0.10", "http://100.64.0.20:47873",
                ), controller=ControllerIdentity("controller", "site-a", "Site A", "controller-a"),
                fleet_token_digest=hashlib.sha256(b"fleet-token").hexdigest(),
                ticket_digest=hashlib.sha256(b"A" * 43).hexdigest(),
                redemption_expires_at=1120,
            )
            return {"request_id": request["request_id"], "response": {"state": "accepted"}}
        return {"request_id": request["request_id"], "response": {"state": "complete"}}

    coordinator.dispatch_next = dispatch
    monkeypatch.setattr(enrollment_repair, "DISPATCH_TIMEOUT_SECONDS", 0.001)
    await coordinator.start()
    await asyncio.sleep(0.02)
    await coordinator.stop()

    assert dispatched == ["mac-a", "mac-b"]
    assert repair_store.batch(batch["batch_id"])["requests"][0]["state"] == "confirmation_pending"


@pytest.mark.asyncio
async def test_unresolved_same_target_gets_no_second_ticket_while_later_mac_proceeds(
    repair_store, monkeypatch,
):
    """Overlapping owner batches retain the first target's ticket authority."""
    from backend import enrollment_repair

    clock = MutableClock()
    repair_store.clock = clock
    rows = _records(
        ("mac-a", "agent-a.test", ["image"]),
        ("mac-b", "agent-b.test", ["voice"]),
    )
    ticket_calls = {"count": 0}

    def ticket():
        ticket_calls["count"] += 1
        return chr(64 + ticket_calls["count"]) * 43

    coordinator = _task8_coordinator(
        repair_store, rows,
        {"agent-a.test": ["100.64.0.10"], "agent-b.test": ["100.64.0.11"]},
        ConnectedDispatch(), clock=clock, ticket_factory=ticket,
    )
    first = coordinator.create_batch(["mac-a"])
    second = coordinator.create_batch(["mac-a", "mac-b"])
    assert second["adopted_requests"][0]["request_id"] == first["requests"][0]["request_id"]

    async def dispatch():
        request = repair_store.claim_next_dispatch()
        if request is None:
            return None
        repair_store.issue_ticket(
            request["request_id"], target=TargetIdentity(
                request["target_machine"], f"agent-{request['target_machine'][-1]}.test",
                "100.64.0.10" if request["target_machine"] == "mac-a" else "100.64.0.11",
                "http://100.64.0.20:47873",
            ), controller=ControllerIdentity("controller", "site-a", "Site A", "controller-a"),
            fleet_token_digest=hashlib.sha256(b"fleet-token").hexdigest(),
            ticket_digest=hashlib.sha256(ticket().encode()).hexdigest(), redemption_expires_at=1120,
        )
        return {"request_id": request["request_id"], "response": {"state": "accepted"}}

    coordinator.dispatch_next = dispatch
    monkeypatch.setattr(enrollment_repair, "DISPATCH_TIMEOUT_SECONDS", 0.001)
    await coordinator.start()
    await asyncio.sleep(0.02)
    await coordinator.stop()

    assert ticket_calls["count"] == 2
    assert repair_store.batch(second["batch_id"])["requests"][0]["state"] == "confirmation_pending"


@pytest.mark.asyncio
async def test_status_adoption_confirms_exact_identity_then_wakes_existing_release_once(
    repair_store,
):
    """Only an exact target terminal complete result wakes managed release work."""
    clock = MutableClock()
    repair_store.clock = clock
    rows = _records(("mac-a", "agent-a.test", ["image"]))
    invalidated = []
    awakened = []
    coordinator = _task8_coordinator(
        repair_store, rows, {"agent-a.test": ["100.64.0.10"]},
        ConnectedDispatch(), clock=clock,
    )
    coordinator._peer_invalidator = invalidated.append
    coordinator._wake_peer = awakened.append
    request_id = coordinator.create_batch(["mac-a"])["requests"][0]["request_id"]
    repair_store.claim_next_dispatch()
    repair_store.issue_ticket(
        request_id, target=TargetIdentity("mac-a", "agent-a.test", "100.64.0.10", "http://100.64.0.20:47873"),
        controller=ControllerIdentity("controller", "site-a", "Site A", "controller-a"),
        fleet_token_digest=hashlib.sha256(b"fleet-token").hexdigest(),
        ticket_digest=hashlib.sha256(b"A" * 43).hexdigest(), redemption_expires_at=1120,
    )
    repair_store.mark_dispatched(request_id)
    repair_store.redeem(
        request_id, ticket="A" * 43, redemption_expires_at=1120, target_machine="mac-a",
        direct_source="100.64.0.10", observed_identity={"role": "standalone"},
        registry_snapshot=TargetIdentity("mac-a", "agent-a.test", "100.64.0.10", "http://100.64.0.20:47873"),
        controller=ControllerIdentity("controller", "site-a", "Site A", "controller-a"),
        fleet_token_digest=hashlib.sha256(b"fleet-token").hexdigest(),
    )
    status = {"request_id": request_id, "state": "complete", "identity": {
        "role": "agent", "site_id": "site-a", "site_name": "Site A", "controller_id": "mac-a",
    }}

    await coordinator.adopt_status(request_id, status, direct_source="100.64.0.10", registry_host="agent-a.test")
    await coordinator.adopt_status(request_id, status, direct_source="100.64.0.10", registry_host="agent-a.test")
    assert invalidated == ["mac-a"]
    assert awakened == ["mac-a"]
    with pytest.raises(RepairStoreError):
        await coordinator.adopt_status(request_id, status, direct_source="100.64.0.11", registry_host="agent-a.test")
    assert invalidated == ["mac-a"]
    assert awakened == ["mac-a"]


@pytest.mark.asyncio
async def test_restart_parks_issued_or_inflight_request_before_continuing(repair_store):
    """Restart never recreates ticket plaintext and can still start the next Mac."""
    clock = MutableClock()
    repair_store.clock = clock
    rows = _records(
        ("mac-a", "agent-a.test", ["image"]),
        ("mac-b", "agent-b.test", ["voice"]),
    )
    first = _task8_coordinator(
        repair_store, rows,
        {"agent-a.test": ["100.64.0.10"], "agent-b.test": ["100.64.0.11"]},
        ConnectedDispatch(), clock=clock,
    )
    batch = first.create_batch(["mac-a", "mac-b"])
    claimed = repair_store.claim_next_dispatch()
    repair_store.issue_ticket(
        claimed["request_id"], target=TargetIdentity("mac-a", "agent-a.test", "100.64.0.10", "http://100.64.0.20:47873"),
        controller=ControllerIdentity("controller", "site-a", "Site A", "controller-a"),
        fleet_token_digest=hashlib.sha256(b"fleet-token").hexdigest(),
        ticket_digest=hashlib.sha256(b"A" * 43).hexdigest(), redemption_expires_at=1120,
    )
    ticket_calls = {"count": 0}
    second = _task8_coordinator(
        repair_store, rows,
        {"agent-a.test": ["100.64.0.10"], "agent-b.test": ["100.64.0.11"]},
        ConnectedDispatch(direct_peer="100.64.0.11"), clock=clock,
        ticket_factory=lambda: ticket_calls.__setitem__("count", ticket_calls["count"] + 1) or "B" * 43,
    )

    await second.start()
    await asyncio.sleep(0)
    await second.stop()

    assert ticket_calls["count"] == 1
    assert repair_store.batch(batch["batch_id"])["requests"][0]["state"] == "confirmation_pending"


@pytest.mark.asyncio
async def test_idle_start_wakes_one_scheduler_for_a_later_batch_and_stops_cleanly(
    repair_store, monkeypatch,
):
    """Lifespan start must not strand an owner batch created after idle."""
    from backend import enrollment_repair

    rows = _records(
        ("mac-a", "agent-a.test", ["image"]),
        ("mac-b", "agent-b.test", ["voice"]),
    )
    coordinator = _task8_coordinator(
        repair_store, rows,
        {"agent-a.test": ["100.64.0.10"], "agent-b.test": ["100.64.0.11"]},
        ConnectedDispatch(), clock=MutableClock(),
    )
    dispatched = []

    async def dispatch():
        request = repair_store.claim_next_dispatch()
        if request is None:
            return None
        dispatched.append(request["request_id"])
        return {"request_id": request["request_id"], "response": {"state": "accepted"}}

    coordinator.dispatch_next = dispatch
    monkeypatch.setattr(enrollment_repair, "DISPATCH_TIMEOUT_SECONDS", 0.001)

    await coordinator.start()
    await asyncio.sleep(0)
    assert coordinator._scheduler_task.done()

    first = coordinator.create_batch(["mac-a"])
    replay = coordinator.create_batch(["mac-a"])
    await asyncio.sleep(0.02)

    assert replay["requests"][0]["request_id"] == first["requests"][0]["request_id"]
    assert dispatched == [first["requests"][0]["request_id"]]

    await coordinator.stop()
    coordinator.create_batch(["mac-b"])
    await asyncio.sleep(0)
    assert dispatched == [first["requests"][0]["request_id"]]


@pytest.mark.parametrize(
    "missing_gate",
    [
        {"target_exact": False},
        {"token_exact": False},
        {"update_supported": False},
        {"restart_verifiable": False},
        {"hub_idle": False},
        {"studios_idle": False},
        {"enabled": False},
        {"managed_release_state": "running", "managed_release_contains_target": True},
        {"managed_release_state": "degraded", "managed_release_contains_target": True},
        {"managed_release_state": None, "managed_release_contains_target": True},
        {"managed_release_state": 7, "managed_release_contains_target": True},
        {"managed_release_state": "future_state", "managed_release_contains_target": True},
        {"conflict_free": False},
        {"published_version": None},
        {"published_repair_schema_version": None},
    ],
    ids=[
        "target", "token", "update", "restart", "hub-idle", "studios-idle",
        "enabled", "active-release", "degraded-release", "release-state-missing",
        "release-state-malformed", "release-state-unknown", "conflict",
        "published-version", "published-schema",
    ],
)
@pytest.mark.asyncio
async def test_older_hub_bootstrap_requires_every_safe_gate(
    repair_store, missing_gate,
):
    rows = _records(("mac-a", "agent-a.test", ["image"]))
    updates = []

    async def capability_probe(machine, host, token):
        return {"repair_schema_version": None}

    async def gate_reader(machine, host, address):
        return _bootstrap_gates(**missing_gate)

    async def hub_updater(machine, target_version, token):
        updates.append((machine, target_version, token))

    coordinator = _task8_coordinator(
        repair_store,
        rows,
        {"agent-a.test": ["100.64.0.10"]},
        ConnectedDispatch(),
        clock=MutableClock(),
        capability_probe=capability_probe,
        bootstrap_gate_reader=gate_reader,
        hub_updater=hub_updater,
    )
    request_id = coordinator.create_batch(["mac-a"])["requests"][0]["request_id"]

    with pytest.raises(RepairStoreError, match="hub_update_required"):
        await coordinator.dispatch_next()

    assert updates == []
    assert repair_store.request(request_id)["state"] == "hub_update_required"


@pytest.mark.asyncio
async def test_safe_older_hub_bootstrap_updates_only_hub_once_then_reprobes(repair_store):
    rows = _records(("mac-a", "agent-a.test", ["image", "voice"]))
    probes = []
    updates = []

    async def capability_probe(machine, host, token):
        probes.append((machine, host, token))
        return {"repair_schema_version": 1 if len(probes) == 2 else None}

    async def gate_reader(machine, host, address):
        return _bootstrap_gates()

    async def hub_updater(machine, target_version, token):
        updates.append((machine, target_version, token))
        return {"state": "complete", "to_version": target_version}

    connection = ConnectedDispatch()
    coordinator = _task8_coordinator(
        repair_store,
        rows,
        {"agent-a.test": ["100.64.0.10"]},
        connection,
        clock=MutableClock(),
        capability_probe=capability_probe,
        bootstrap_gate_reader=gate_reader,
        hub_updater=hub_updater,
    )
    coordinator.create_batch(["mac-a"])

    result = await coordinator.dispatch_next()

    assert result["target_machine_id"] == "mac-a"
    assert probes == [
        ("mac-a", "agent-a.test", "fleet-token"),
        ("mac-a", "agent-a.test", "fleet-token"),
    ]
    assert updates == [("mac-a", "2.9.0", "fleet-token")]
    assert [call[1] for call in connection.calls] == [
        "/api/hub/enrollment-repair/apply",
    ]


@pytest.mark.parametrize("release_state", ["running", "degraded"])
@pytest.mark.asyncio
async def test_active_or_degraded_managed_release_never_uses_moving_main_bootstrap(
    repair_store, release_state,
):
    rows = _records(("mac-a", "agent-a.test", ["image", "voice"]))

    async def capability_probe(machine, host, token):
        return {"repair_schema_version": None}

    async def gate_reader(machine, host, address):
        return _bootstrap_gates(
            managed_release_state=release_state,
            managed_release_contains_target=True,
        )

    async def hub_updater(*args):
        pytest.fail("active/degraded release used moving-main Hub bootstrap")

    coordinator = _task8_coordinator(
        repair_store,
        rows,
        {"agent-a.test": ["100.64.0.10"]},
        ConnectedDispatch(),
        clock=MutableClock(),
        capability_probe=capability_probe,
        bootstrap_gate_reader=gate_reader,
        hub_updater=hub_updater,
    )
    coordinator.create_batch(["mac-a"])

    with pytest.raises(RepairStoreError, match="hub_update_required"):
        await coordinator.dispatch_next()


@pytest.mark.parametrize("field", ["role", "site_id", "site_name", "controller_id"])
@pytest.mark.asyncio
async def test_paused_before_replace_blocks_controller_role_site_name_and_id_changes(
    repair_store, field,
):
    rows = _records(("mac-a", "agent-a.test", ["image"]))
    coordinator = _coordinator(
        repair_store, rows, {"agent-a.test": ["100.64.0.10"]},
    )
    request_id = _live_claim(repair_store)
    applied = []

    with pytest.raises(RepairStoreError, match="enrollment_repair_busy"):
        with coordinator.controller_mutation(identity=True):
            applied.append(field)
    assert applied == []

    status = {"request_id": request_id, "state": "never_applied"}
    repair_store.adopt_status(request_id, status, direct_source="100.64.0.10")
    with coordinator.controller_mutation(identity=True):
        applied.append(field)
    assert applied == [field]


@pytest.mark.parametrize("mutation", ["delete", "rekey", "host", "address"])
@pytest.mark.asyncio
async def test_paused_before_replace_blocks_target_delete_rekey_host_and_address_change(
    repair_store, mutation,
):
    rows = _records(("mac-a", "agent-a.test", ["image"]))
    coordinator = _coordinator(
        repair_store, rows, {"agent-a.test": ["100.64.0.10"]},
    )
    request_id = _live_claim(repair_store)
    applied = []

    with pytest.raises(RepairStoreError, match="enrollment_repair_busy"):
        with coordinator.controller_mutation(machine="mac-a"):
            applied.append(mutation)
    assert applied == []

    status = {"request_id": request_id, "state": "complete", "identity": {
        "role": "agent",
        "site_id": "site-a",
        "site_name": "Site A",
        "controller_id": "mac-a",
    }}
    repair_store.adopt_status(request_id, status, direct_source="100.64.0.10")
    with coordinator.controller_mutation(machine="mac-a"):
        applied.append(mutation)
    assert applied == [mutation]


def test_unrelated_registry_change_and_fleet_token_rotation_are_not_fenced(repair_store):
    rows = _records(
        ("mac-a", "agent-a.test", ["image"]),
        ("mac-b", "agent-b.test", ["voice"]),
    )
    token_state = {"value": "fleet-token"}
    from backend.enrollment_repair import EnrollmentRepairCoordinator

    coordinator = EnrollmentRepairCoordinator(
        repair_store,
        registry_loader=lambda: rows,
        token_reader=lambda: token_state["value"],
        settings_reader=lambda: {"role": "controller"},
        resolver=_resolver({
            "agent-a.test": ["100.64.0.10"],
            "agent-b.test": ["100.64.0.11"],
        }),
    )
    request_id = _live_claim(repair_store)

    coordinator.require_target_registry_mutable("mac-b")
    coordinator.require_enrollment_registration_mutable("mac-b", "agent-b.test")
    with coordinator.controller_mutation():
        token_state["value"] = "rotated-token"

    assert repair_store.request(request_id)["state"] == "confirmation_pending"
    assert repair_store.mutation_blocker(machine="mac-a") is not None


def test_registry_ambiguity_flags_but_does_not_release_live_claim_fence(repair_store):
    rows = _records(("mac-a", "agent-a.test", ["image"]))
    coordinator = _coordinator(
        repair_store, rows, {
            "agent-a.test": ["100.64.0.10"],
            "agent-b.test": ["100.64.0.11"],
        },
    )
    request_id = _live_claim(repair_store)
    ambiguous = rows + _records(("mac-b", "agent-a.test", ["voice"]))

    assert coordinator.note_registry_reload(ambiguous) == 1

    request = repair_store.request(request_id)
    assert request["state"] == "confirmation_pending"
    assert request["evidence"]["registry_changed_pending"] is True
    with pytest.raises(RepairStoreError, match="enrollment_repair_busy"):
        coordinator.require_controller_identity_mutable()
    with pytest.raises(RepairStoreError, match="enrollment_repair_busy"):
        coordinator.require_target_registry_mutable("mac-a")


def test_registry_reload_marks_exact_live_target_changed_without_releasing_fence(
    repair_store,
):
    rows = _records(("mac-a", "agent-a.test", ["image"]))
    addresses = {
        "agent-a.test": ["100.64.0.10"],
        "changed.test": ["100.64.0.12"],
    }
    coordinator = _coordinator(repair_store, rows, addresses)
    request_id = _live_claim(repair_store)
    changed = _records(("mac-a", "changed.test", ["image"]))

    assert coordinator.note_registry_reload(changed) == 1

    request = repair_store.request(request_id)
    assert request["state"] == "confirmation_pending"
    assert request["evidence"] == {
        "registry_changed_pending": True,
        "registry_changed_code": "registry_host_changed",
    }
    assert repair_store.mutation_blocker() is not None
    assert repair_store.mutation_blocker(machine="mac-a") is not None


def test_registry_reload_for_unrelated_machine_does_not_flag_live_target(repair_store):
    rows = _records(
        ("mac-a", "agent-a.test", ["image"]),
        ("mac-b", "agent-b.test", ["voice"]),
    )
    coordinator = _coordinator(repair_store, rows, {
        "agent-a.test": ["100.64.0.10"],
        "agent-b.test": ["100.64.0.11"],
        "changed.test": ["100.64.0.12"],
    })
    request_id = _live_claim(repair_store)
    before = repair_store.request(request_id)
    unrelated = _records(
        ("mac-a", "agent-a.test", ["image"]),
        ("mac-b", "changed.test", ["voice"]),
    )

    assert coordinator.note_registry_reload(unrelated) == 0
    after = repair_store.request(request_id)
    assert after["registry_snapshot"] == before["registry_snapshot"]
    assert after["evidence"] == before["evidence"] is None


class ProbeMutationLock:
    def __init__(self):
        self._lock = threading.RLock()
        self._owner = None
        self.contended = threading.Event()

    def __enter__(self):
        current = threading.get_ident()
        if self._owner not in {None, current}:
            self.contended.set()
        self._lock.acquire()
        self._owner = current
        return self

    def __exit__(self, *_exc):
        self._owner = None
        self._lock.release()

    def owned_by_current_thread(self):
        return self._owner == threading.get_ident()


def test_guarded_target_mutation_fence_finishes_before_redeem_revalidates_and_commits(
    repair_store,
):
    rows = _records(("mac-a", "agent-a.test", ["image"]))
    addresses = {
        "agent-a.test": ["100.64.0.10"],
        "changed.test": ["100.64.0.12"],
    }
    clock = MutableClock()
    repair_store.clock = clock
    lock = ProbeMutationLock()
    connection = ConnectedDispatch()
    coordinator = _task8_coordinator(
        repair_store, rows, addresses, connection, clock=clock, mutation_lock=lock,
    )
    coordinator.create_batch(["mac-a"])
    asyncio.run(coordinator.dispatch_next())
    dispatch = connection.calls[0][3]
    redemption = {
        "schema": "studiohub.enrollment-repair-redemption",
        "schema_version": 1,
        "request_id": dispatch["request_id"],
        "target_machine_id": "mac-a",
        "ticket": dispatch["ticket"],
        "redemption_expires_at": dispatch["redemption_expires_at"],
        "observed_identity": {
            "role": "standalone", "site_id": "old", "site_name": "Old",
            "controller_id": "old", "parent_controller_url": None,
        },
    }
    mutation_entered = threading.Event()
    release_mutation = threading.Event()
    redemption_result = {}

    def mutate():
        with coordinator.controller_mutation(machine="mac-a"):
            mutation_entered.set()
            assert release_mutation.wait(2)
            rows[0]["host"] = "changed.test"

    def redeem():
        try:
            redemption_result["claim"] = asyncio.run(coordinator.redeem(
                redemption,
                direct_source="100.64.0.10",
                fleet_token="fleet-token",
            ))
        except BaseException as exc:
            redemption_result["error"] = exc

    mutation_thread = threading.Thread(target=mutate)
    mutation_thread.start()
    assert mutation_entered.wait(2)
    redemption_thread = threading.Thread(target=redeem)
    redemption_thread.start()
    try:
        assert lock.contended.wait(2), "redemption did not join the mutation lock order"
    finally:
        release_mutation.set()
        mutation_thread.join(2)
        redemption_thread.join(2)

    assert isinstance(redemption_result.get("error"), RepairStoreError)
    assert repair_store.request(dispatch["request_id"])["state"] == "dispatched"


def test_fleet_token_commit_serializes_dispatch_and_redeem_snapshots(repair_store):
    """The final local token commit wins before either authority transaction."""
    rows = _records(("mac-a", "agent-a.test", ["image"]))
    addresses = {"agent-a.test": ["100.64.0.10"]}
    clock = MutableClock()
    repair_store.clock = clock
    token_state = {"value": "fleet-token"}
    mutation_lock = ProbeMutationLock()
    connected = threading.Event()
    connection = ConnectedDispatch(on_connect=connected.set)
    coordinator = _task8_coordinator(
        repair_store, rows, addresses, connection, clock=clock,
        token_state=token_state, mutation_lock=mutation_lock,
    )
    first = coordinator.create_batch(["mac-a"])["requests"][0]["request_id"]
    dispatch_result = {}

    def dispatch():
        try:
            dispatch_result["value"] = asyncio.run(coordinator.dispatch_next())
        except BaseException as exc:
            dispatch_result["error"] = exc

    with coordinator.controller_mutation():
        worker = threading.Thread(target=dispatch)
        worker.start()
        assert connected.wait(2)
        assert mutation_lock.contended.wait(2)
        token_state["value"] = "rotated-fleet-token"
    worker.join(2)

    assert isinstance(dispatch_result.get("error"), RepairStoreError)
    assert dispatch_result["error"].code == "fleet_token_changed"
    assert repair_store.request(first)["state"] == "needs_review"
    with sqlite3.connect(repair_store.path) as database:
        assert database.execute(
            "SELECT ticket_status FROM enrollment_repair_requests WHERE request_id = ?",
            (first,),
        ).fetchone() == (None,)

    # A fresh request issued under the rotated token remains issued when a
    # later final local commit races its one redemption transaction.
    second = coordinator.create_batch(["mac-a"])["requests"][0]["request_id"]
    connected.clear()
    connection.on_connect = None
    asyncio.run(coordinator.dispatch_next())
    sent = connection.calls[-1][3]
    redemption = {
        "schema": "studiohub.enrollment-repair-redemption",
        "schema_version": 1,
        "request_id": second,
        "target_machine_id": "mac-a",
        "ticket": sent["ticket"],
        "redemption_expires_at": sent["redemption_expires_at"],
        "observed_identity": {
            "role": "standalone", "site_id": "old", "site_name": "Old",
            "controller_id": "old", "parent_controller_url": None,
        },
    }
    redemption_result = {}
    mutation_lock.contended.clear()

    def redeem():
        try:
            redemption_result["value"] = asyncio.run(coordinator.redeem(
                redemption,
                direct_source="100.64.0.10",
                fleet_token="rotated-fleet-token",
            ))
        except BaseException as exc:
            redemption_result["error"] = exc

    with coordinator.controller_mutation():
        worker = threading.Thread(target=redeem)
        worker.start()
        assert mutation_lock.contended.wait(2)
        token_state["value"] = "final-fleet-token"
    worker.join(2)

    assert isinstance(redemption_result.get("error"), RepairStoreError)
    assert redemption_result["error"].code == "fleet_token_mismatch"
    issued = repair_store.request(second)
    assert issued["state"] == "dispatched"
    with sqlite3.connect(repair_store.path) as database:
        assert database.execute(
            "SELECT ticket_status FROM enrollment_repair_requests WHERE request_id = ?",
            (second,),
        ).fetchone() == ("issued",)


def test_registry_reload_resolution_finishes_before_short_controller_mutation_lock(
    repair_store,
):
    rows = _records(
        ("mac-a", "agent-a.test", ["image"]),
        ("mac-b", "agent-b.test", ["voice"]),
    )
    lock = ProbeMutationLock()
    resolver_calls = []

    def resolver(host, port, *, type):
        resolver_calls.append((host, lock.owned_by_current_thread()))
        address = {
            "agent-a.test": "100.64.0.10",
            "agent-b.test": "100.64.0.11",
        }[host]
        return [(2, type, 6, "", (address, port))]

    from backend.enrollment_repair import EnrollmentRepairCoordinator

    coordinator = EnrollmentRepairCoordinator(
        repair_store,
        registry_loader=lambda: rows,
        token_reader=lambda: "fleet-token",
        settings_reader=lambda: {"role": "controller"},
        resolver=resolver,
        mutation_lock=lock,
    )
    _live_claim(repair_store)
    registration = coordinator.resolve_enrollment_registration(
        "mac-b", "agent-b.test",
    )
    reload_snapshot = coordinator.resolve_registry_rows(rows)

    with coordinator.controller_mutation():
        coordinator.require_enrollment_registration_mutable(
            "mac-b", "agent-b.test", resolved=registration,
        )
        assert coordinator.note_registry_reload(reload_snapshot) == 0

    assert resolver_calls
    assert not any(held for _host, held in resolver_calls)


def test_stale_prepared_registry_reload_flags_exact_target_without_resolution_in_lock(
    repair_store,
):
    rows = _records(("mac-a", "agent-a.test", ["image"]))
    lock = ProbeMutationLock()
    resolver_calls = []

    def resolver(host, port, *, type):
        resolver_calls.append((host, lock.owned_by_current_thread()))
        address = {
            "agent-a.test": "100.64.0.10",
            "changed.test": "100.64.0.12",
        }[host]
        return [(2, type, 6, "", (address, port))]

    from backend.enrollment_repair import EnrollmentRepairCoordinator

    coordinator = EnrollmentRepairCoordinator(
        repair_store,
        registry_loader=lambda: rows,
        token_reader=lambda: "fleet-token",
        settings_reader=lambda: {"role": "controller"},
        resolver=resolver,
        mutation_lock=lock,
    )
    request_id = _live_claim(repair_store)
    reload_snapshot = coordinator.resolve_registry_rows(rows)
    rows[0]["host"] = "changed.test"

    with coordinator.controller_mutation():
        assert coordinator.note_registry_reload(reload_snapshot) == 1

    request = repair_store.request(request_id)
    assert request["state"] == "confirmation_pending"
    assert request["evidence"] == {
        "registry_changed_pending": True,
        "registry_changed_code": "registry_snapshot_changed",
    }
    assert repair_store.mutation_blocker(machine="mac-a") is not None
    assert resolver_calls
    assert not any(held for _host, held in resolver_calls)


def test_stale_prepared_registry_reload_flags_new_machine_sharing_target_host(
    repair_store,
):
    rows = _records(("mac-a", "agent-a.test", ["image"]))
    lock = ProbeMutationLock()
    resolver_calls = []

    def resolver(host, port, *, type):
        resolver_calls.append((host, lock.owned_by_current_thread()))
        return [(2, type, 6, "", ("100.64.0.10", port))]

    from backend.enrollment_repair import EnrollmentRepairCoordinator

    coordinator = EnrollmentRepairCoordinator(
        repair_store,
        registry_loader=lambda: rows,
        token_reader=lambda: "fleet-token",
        settings_reader=lambda: {"role": "controller"},
        resolver=resolver,
        mutation_lock=lock,
    )
    request_id = _live_claim(repair_store)
    reload_snapshot = coordinator.resolve_registry_rows(rows)
    rows.extend(_records(("mac-b", "agent-a.test", ["voice"])))

    with coordinator.controller_mutation():
        assert coordinator.note_registry_reload(reload_snapshot) == 1

    request = repair_store.request(request_id)
    assert request["state"] == "confirmation_pending"
    assert request["evidence"] == {
        "registry_changed_pending": True,
        "registry_changed_code": "host_shared",
    }
    assert repair_store.mutation_blocker(machine="mac-a") is not None
    assert resolver_calls
    assert not any(held for _host, held in resolver_calls)


def test_stale_new_hostname_requires_external_refresh_before_address_shared_flag(
    repair_store,
):
    rows = _records(("mac-a", "agent-a.test", ["image"]))
    lock = ProbeMutationLock()
    resolver_calls = []

    def resolver(host, port, *, type):
        resolver_calls.append((host, lock.owned_by_current_thread()))
        return [(2, type, 6, "", ("100.64.0.10", port))]

    from backend.enrollment_repair import EnrollmentRepairCoordinator

    coordinator = EnrollmentRepairCoordinator(
        repair_store,
        registry_loader=lambda: rows,
        token_reader=lambda: "fleet-token",
        settings_reader=lambda: {"role": "controller"},
        resolver=resolver,
        mutation_lock=lock,
    )
    request_id = _live_claim(repair_store)
    stale_snapshot = coordinator.resolve_registry_rows(rows)
    rows.extend(_records(("mac-b", "alias.test", ["voice"])))

    with coordinator.controller_mutation():
        with pytest.raises(RepairStoreError, match="registry_snapshot_changed"):
            coordinator.note_registry_reload(stale_snapshot)

    assert repair_store.request(request_id)["evidence"] is None
    assert repair_store.mutation_blocker(machine="mac-a") is not None
    refreshed_snapshot = coordinator.resolve_registry_rows(rows)
    with coordinator.controller_mutation():
        assert coordinator.note_registry_reload(refreshed_snapshot) == 1
    assert repair_store.request(request_id)["evidence"] == {
        "registry_changed_pending": True,
        "registry_changed_code": "address_shared",
    }
    assert not any(held for _host, held in resolver_calls)


def test_fresh_current_address_overrides_stale_prepared_alias_evidence(repair_store):
    rows = _records(
        ("mac-a", "agent-a.test", ["image"]),
        ("mac-b", "alias.test", ["voice"]),
    )
    lock = ProbeMutationLock()
    addresses = {
        "agent-a.test": "100.64.0.10",
        "alias.test": "100.64.0.11",
    }
    resolver_calls = []

    def resolver(host, port, *, type):
        resolver_calls.append((host, lock.owned_by_current_thread()))
        return [(2, type, 6, "", (addresses[host], port))]

    from backend.enrollment_repair import EnrollmentRepairCoordinator

    coordinator = EnrollmentRepairCoordinator(
        repair_store,
        registry_loader=lambda: rows,
        token_reader=lambda: "fleet-token",
        settings_reader=lambda: {"role": "controller"},
        resolver=resolver,
        mutation_lock=lock,
    )
    request_id = _live_claim(repair_store)
    stale_snapshot = coordinator.resolve_registry_rows(rows)
    addresses["alias.test"] = "100.64.0.10"

    assert coordinator.note_registry_reload(stale_snapshot) == 1

    request = repair_store.request(request_id)
    assert request["state"] == "confirmation_pending"
    assert request["evidence"] == {
        "registry_changed_pending": True,
        "registry_changed_code": "address_shared",
    }
    assert repair_store.mutation_blocker(machine="mac-a") is not None
    assert resolver_calls
    assert not any(held for _host, held in resolver_calls)


def test_fresh_current_distinct_address_keeps_stale_prepared_alias_unrelated(
    repair_store,
):
    rows = _records(
        ("mac-a", "agent-a.test", ["image"]),
        ("mac-b", "alias.test", ["voice"]),
    )
    lock = ProbeMutationLock()
    addresses = {
        "agent-a.test": "100.64.0.10",
        "alias.test": "100.64.0.11",
    }
    resolver_calls = []

    def resolver(host, port, *, type):
        resolver_calls.append((host, lock.owned_by_current_thread()))
        return [(2, type, 6, "", (addresses[host], port))]

    from backend.enrollment_repair import EnrollmentRepairCoordinator

    coordinator = EnrollmentRepairCoordinator(
        repair_store,
        registry_loader=lambda: rows,
        token_reader=lambda: "fleet-token",
        settings_reader=lambda: {"role": "controller"},
        resolver=resolver,
        mutation_lock=lock,
    )
    request_id = _live_claim(repair_store)
    stale_snapshot = coordinator.resolve_registry_rows(rows)
    addresses["alias.test"] = "100.64.0.12"

    assert coordinator.note_registry_reload(stale_snapshot) == 0
    assert repair_store.request(request_id)["evidence"] is None
    assert repair_store.mutation_blocker(machine="mac-a") is not None
    assert not any(held for _host, held in resolver_calls)


def test_missing_fresh_address_evidence_does_not_fall_back_to_stale_preparation(
    repair_store,
):
    rows = _records(
        ("mac-a", "agent-a.test", ["image"]),
        ("mac-b", "alias.test", ["voice"]),
    )
    lock = ProbeMutationLock()
    addresses = {
        "agent-a.test": ["100.64.0.10"],
        "alias.test": ["100.64.0.11"],
    }
    resolver_calls = []

    def resolver(host, port, *, type):
        resolver_calls.append((host, lock.owned_by_current_thread()))
        return [(2, type, 6, "", (address, port)) for address in addresses[host]]

    from backend.enrollment_repair import EnrollmentRepairCoordinator

    coordinator = EnrollmentRepairCoordinator(
        repair_store,
        registry_loader=lambda: rows,
        token_reader=lambda: "fleet-token",
        settings_reader=lambda: {"role": "controller"},
        resolver=resolver,
        mutation_lock=lock,
    )
    request_id = _live_claim(repair_store)
    stale_snapshot = coordinator.resolve_registry_rows(rows)
    addresses["alias.test"] = []

    with pytest.raises(RepairStoreError, match="registry_snapshot_changed"):
        coordinator.note_registry_reload(stale_snapshot)

    assert repair_store.request(request_id)["evidence"] is None
    assert repair_store.mutation_blocker(machine="mac-a") is not None
    assert resolver_calls
    assert not any(held for _host, held in resolver_calls)


def test_cleanup_cannot_release_fence_until_target_process_stop_is_verified(repair_store):
    rows = _records(("mac-a", "agent-a.test", ["image"]))
    stop_state = {"value": False}
    recovery_calls = []
    request_id = _live_claim(repair_store)

    def recover():
        recovery_calls.append(request_id)
        return {"request_id": request_id, "state": "never_applied"}

    from backend.enrollment_repair import EnrollmentRepairCoordinator

    coordinator = EnrollmentRepairCoordinator(
        repair_store,
        registry_loader=lambda: rows,
        token_reader=lambda: "fleet-token",
        settings_reader=lambda: {"role": "controller"},
        resolver=_resolver({"agent-a.test": ["100.64.0.10"]}),
        process_stop_verifier=lambda machine: stop_state["value"],
        stopped_recovery=recover,
    )

    for stopped in (False, None):
        stop_state["value"] = stopped
        with pytest.raises(RepairStoreError, match="process_stop_unverified"):
            coordinator.release_fence_after_verified_stop(request_id)
        assert repair_store.mutation_blocker(machine="mac-a") is not None

    from backend.controller_settings_lock import settings_writer_lock

    lock_held = threading.Event()
    release_lock = threading.Event()

    def hold_settings_lock():
        with settings_writer_lock():
            lock_held.set()
            assert release_lock.wait(2)

    holder = threading.Thread(target=hold_settings_lock)
    holder.start()
    assert lock_held.wait(2)
    stop_state["value"] = True
    try:
        with pytest.raises(RepairStoreError, match="settings_writer_busy"):
            coordinator.release_fence_after_verified_stop(request_id)
        assert repair_store.mutation_blocker(machine="mac-a") is not None
        assert recovery_calls == []
    finally:
        release_lock.set()
        holder.join(2)

    coordinator.release_fence_after_verified_stop(request_id)
    assert recovery_calls == [request_id]
    assert repair_store.mutation_blocker(machine="mac-a") is None
