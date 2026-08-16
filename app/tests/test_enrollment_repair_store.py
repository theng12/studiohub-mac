import hashlib
import json
import sqlite3
from unittest.mock import patch

import pytest

from backend.enrollment_repair_store import (
    ControllerIdentity,
    RepairStore,
    RepairStoreError,
    TargetIdentity,
)


CONTROLLER = ControllerIdentity(
    role="controller",
    site_id="site-a",
    site_name="Site A",
    controller_id="controller-a",
)
TARGET_A = TargetIdentity(
    machine="mac-a",
    registry_host="100.64.0.10",
    resolved_address="100.64.0.10",
    controller_url="http://100.64.0.20:47873",
)
TARGET_B = TargetIdentity(
    machine="mac-b",
    registry_host="100.64.0.11",
    resolved_address="100.64.0.11",
    controller_url="http://100.64.0.20:47873",
)


class MutableClock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


class MutatingConnection:
    """Force a stale read/write transition inside one store transaction."""

    def __init__(self, connection, mutate):
        self._connection = connection
        self._mutate = mutate
        self._mutated = False

    def __enter__(self):
        self._connection.__enter__()
        return self

    def __exit__(self, *args):
        return self._connection.__exit__(*args)

    def execute(self, sql, parameters=()):
        cursor = self._connection.execute(sql, parameters)
        normalized = " ".join(str(sql).split())
        if (
            not self._mutated
            and (
                normalized.startswith(
                    "SELECT * FROM enrollment_repair_requests WHERE request_id = ?"
                )
                or normalized.startswith(
                    "SELECT batch_id, state FROM enrollment_repair_requests WHERE request_id = ?"
                )
            )
        ):
            self._mutate(self._connection)
            self._mutated = True
        return cursor

    def __getattr__(self, name):
        return getattr(self._connection, name)


def run_with_stale_request_read(store, mutate, operation):
    connection = store._connect()
    wrapped = MutatingConnection(connection, mutate)
    with patch.object(store, "_connect", return_value=wrapped):
        operation()


def sha256_text(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@pytest.fixture
def clock():
    return MutableClock(1000.0)


@pytest.fixture
def store(reset, clock):
    from backend import enrollment

    return RepairStore(enrollment.DB_FILE, clock=clock)


def issue(store, request_id, *, target=TARGET_A, expires_at=1120.0):
    store.issue_ticket(
        request_id,
        target=target,
        controller=CONTROLLER,
        fleet_token_digest=sha256_text("fleet-secret"),
        ticket_digest=sha256_text("repair-secret"),
        redemption_expires_at=expires_at,
    )


def redeem(store, request_id, *, target=TARGET_A):
    return store.redeem(
        request_id,
        ticket="repair-secret",
        redemption_expires_at=1120.0,
        target_machine=target.machine,
        direct_source=target.resolved_address,
        observed_identity={
            "role": "standalone",
            "site_id": "old-site",
            "site_name": "Old Site",
            "controller_id": "old-controller",
            "parent_controller_url": None,
        },
        registry_snapshot=target,
        controller=CONTROLLER,
        fleet_token_digest=sha256_text("fleet-secret"),
    )


def test_create_batch_is_stable_ordered_and_contains_no_credentials(store):
    batch = store.create_or_adopt_batch(["mac-z", "mac-a"])

    assert [row["target_machine"] for row in batch["requests"]] == [
        "mac-a",
        "mac-z",
    ]
    assert batch["state"] == "queued"
    assert "ticket" not in json.dumps(batch)


def test_issue_ticket_persists_only_ticket_and_fleet_digests(store):
    batch = store.create_or_adopt_batch(["mac-a"])
    request_id = batch["requests"][0]["request_id"]

    store.issue_ticket(
        request_id,
        target=TARGET_A,
        controller=CONTROLLER,
        fleet_token_digest=sha256_text("fleet-secret"),
        ticket_digest=sha256_text("repair-secret"),
        redemption_expires_at=1120.0,
    )

    raw = store.path.read_bytes()
    assert b"repair-secret" not in raw
    assert b"fleet-secret" not in raw


@pytest.mark.parametrize("field", ["fleet_token_digest", "ticket_digest"])
@pytest.mark.parametrize("value", ["fleet-secret", "A" * 64, "a" * 63, "a" * 65])
def test_issue_ticket_rejects_noncanonical_digests_and_persists_nothing(
    store, field, value,
):
    batch = store.create_or_adopt_batch(["mac-a"])
    request_id = batch["requests"][0]["request_id"]
    kwargs = {
        "fleet_token_digest": sha256_text("fleet-secret"),
        "ticket_digest": sha256_text("repair-secret"),
        field: value,
    }

    with pytest.raises(ValueError, match="SHA-256 digest"):
        store.issue_ticket(
            request_id,
            target=TARGET_A,
            controller=CONTROLLER,
            **kwargs,
            redemption_expires_at=1120.0,
        )

    raw = store.path.read_bytes()
    assert value.encode("utf-8") not in raw


def test_duplicate_unresolved_machine_adopts_existing_batch_and_request(store):
    first = store.create_or_adopt_batch(["mac-a"])
    second = store.create_or_adopt_batch(["mac-a"])

    assert second["batch_id"] == first["batch_id"]
    assert [row["request_id"] for row in second["requests"]] == [
        first["requests"][0]["request_id"]
    ]
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM enrollment_repair_batches"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM enrollment_repair_requests"
        ).fetchone()[0] == 1


def test_mixed_batch_adopts_active_target_and_queues_unrelated_target(store):
    first = store.create_or_adopt_batch(["mac-a"])
    first_request = first["requests"][0]["request_id"]

    mixed = store.create_or_adopt_batch(["mac-a", "mac-b"])

    assert [row["target_machine"] for row in mixed["requests"]] == ["mac-b"]
    assert [row["request_id"] for row in mixed["adopted_requests"]] == [first_request]
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM enrollment_repair_requests"
        ).fetchone()[0] == 2


def test_large_mixed_batch_adoption_is_not_limited_by_evidence_size(store):
    active = [f"mac-{index:03d}" for index in range(480)]
    store.create_or_adopt_batch(active)

    mixed = store.create_or_adopt_batch([*active, "mac-new"])

    assert [row["target_machine"] for row in mixed["requests"]] == ["mac-new"]
    assert len(mixed["adopted_requests"]) == 480
    assert mixed["evidence"] is None
    replay = store.create_or_adopt_batch(["mac-new", *reversed(active)])
    assert replay["batch_id"] == mixed["batch_id"]
    assert replay["requests"] == mixed["requests"]
    assert replay["adopted_requests"] == mixed["adopted_requests"]


def test_batch_readback_after_restart_includes_sanitized_adopted_requests(store):
    first = store.create_or_adopt_batch(["mac-a", "mac-b"])
    mixed = store.create_or_adopt_batch(["mac-a", "mac-b", "mac-new"])
    reopened = RepairStore(store.path, clock=store.clock)

    readback = reopened.batch(mixed["batch_id"])

    assert [row["target_machine"] for row in readback["requests"]] == ["mac-new"]
    assert [row["request_id"] for row in readback["adopted_requests"]] == [
        row["request_id"] for row in first["requests"]
    ]
    assert [row["state"] for row in readback["adopted_requests"]] == ["queued", "queued"]
    assert "ticket_digest" not in json.dumps(readback)


def test_batch_accepts_500_max_length_machine_ids(store):
    machines = [f"mac-{index:03d}-" + "x" * 90 for index in range(500)]

    batch = store.create_or_adopt_batch(machines)

    assert len(batch["targets"]) == 500
    assert len(batch["requests"]) == 500


def test_batch_state_is_bounded(store):
    batch = store.create_or_adopt_batch(["mac-a"])

    with sqlite3.connect(store.path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE enrollment_repair_batches SET state = 'bogus' WHERE batch_id = ?",
                (batch["batch_id"],),
            )


def test_redeem_is_single_use_and_checks_absolute_expiry(store, clock):
    first = store.create_or_adopt_batch(["mac-a"])
    first_request = first["requests"][0]["request_id"]
    issue(store, first_request)
    clock.value = 1119.999

    claim = redeem(store, first_request)

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
    with pytest.raises(RepairStoreError, match="ticket_replayed"):
        redeem(store, first_request)

    second = store.create_or_adopt_batch(["mac-b"])
    second_request = second["requests"][0]["request_id"]
    issue(store, second_request, target=TARGET_B)
    clock.value = 1120.0

    with pytest.raises(RepairStoreError, match="ticket_expired"):
        redeem(store, second_request, target=TARGET_B)
    with sqlite3.connect(store.path) as connection:
        row = connection.execute(
            "SELECT ticket_status, state FROM enrollment_repair_requests WHERE request_id = ?",
            (second_request,),
        ).fetchone()
    assert row == ("expired", "confirmation_pending")


def test_redeem_rejects_a_different_completed_request_for_the_same_target(store, clock):
    first = store.create_or_adopt_batch(["mac-a"])
    first_request = first["requests"][0]["request_id"]
    issue(store, first_request)
    clock.value = 1100.0
    redeem(store, first_request)
    store.adopt_status(
        first_request,
        {
            "request_id": first_request,
            "state": "complete",
            "identity": {
                "role": "agent", "site_id": "site-a", "site_name": "Site A",
                "controller_id": "mac-a",
            },
        },
        direct_source=TARGET_A.resolved_address,
    )
    second = store.create_or_adopt_batch(["mac-a"])
    second_request = second["requests"][0]["request_id"]
    issue(store, second_request)

    with pytest.raises(RepairStoreError, match="repair_already_complete"):
        redeem(store, second_request)

    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT ticket_status, redeemed_at FROM enrollment_repair_requests WHERE request_id = ?",
            (second_request,),
        ).fetchone() == ("issued", None)


def test_duplicate_owner_batch_adopts_unresolved_target_without_new_ticket(store):
    first = store.create_or_adopt_batch(["mac-a"])
    second = store.create_or_adopt_batch(["mac-a"])

    assert second["batch_id"] == first["batch_id"]
    assert second["requests"][0]["request_id"] == first["requests"][0]["request_id"]
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM enrollment_repair_requests"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM enrollment_repair_requests WHERE ticket_digest IS NOT NULL"
        ).fetchone()[0] == 0


def test_restart_parks_orphaned_dispatch_slot_and_next_machine_can_start(store):
    batch = store.create_or_adopt_batch(["mac-b", "mac-a"])
    first = store.claim_next_dispatch()
    assert first["target_machine"] == "mac-a"
    issue(store, first["request_id"])
    store.mark_dispatched(first["request_id"])

    assert store.recover_scheduling_slot() == 1

    recovered = store.batch(batch["batch_id"])
    assert [row["state"] for row in recovered["requests"]] == [
        "confirmation_pending",
        "queued",
    ]
    second = store.claim_next_dispatch()
    assert second["target_machine"] == "mac-b"


def test_same_target_stays_locked_while_different_target_claims_dispatch_slot(store):
    first_batch = store.create_or_adopt_batch(["mac-a"])
    first = store.claim_next_dispatch()
    issue(store, first["request_id"])
    store.mark_dispatched(first["request_id"])
    store.park(first["request_id"])

    adopted = store.create_or_adopt_batch(["mac-a"])
    assert adopted["batch_id"] == first_batch["batch_id"]
    assert adopted["requests"][0]["request_id"] == first["request_id"]

    with sqlite3.connect(store.path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO enrollment_repair_requests
                   (request_id, batch_id, ordinal, target_machine, state, created_at, updated_at)
                   VALUES ('duplicate-mac-a', ?, 99, 'mac-a', 'queued', 1000, 1000)""",
                (first_batch["batch_id"],),
            )

    store.create_or_adopt_batch(["mac-b"])
    next_request = store.claim_next_dispatch()
    assert next_request["target_machine"] == "mac-b"


@pytest.mark.parametrize(
    ("agent_state", "stored_state", "may_create_new_request"),
    [
        ("complete", "complete", True),
        ("never_applied", "retryable", True),
        ("needs_review", "needs_review", False),
    ],
)
def test_terminal_agent_evidence_releases_identity_and_membership_fence(
    store, clock, agent_state, stored_state, may_create_new_request,
):
    batch = store.create_or_adopt_batch(["mac-a"])
    request_id = batch["requests"][0]["request_id"]
    issue(store, request_id)
    clock.value = 1100.0
    redeem(store, request_id)
    assert store.mutation_blocker() is not None
    assert store.mutation_blocker(machine="mac-a") is not None

    status = {"request_id": request_id, "state": agent_state}
    if agent_state == "complete":
        status["identity"] = {
            "role": "agent",
            "site_id": "site-a",
            "site_name": "Site A",
            "controller_id": "mac-a",
        }
    adopted = store.adopt_status(
        request_id,
        status,
        direct_source=TARGET_A.resolved_address,
    )

    assert adopted["state"] == stored_state
    assert adopted["evidence"]["agent_terminal_state"] == agent_state
    assert store.mutation_blocker() is None
    assert store.mutation_blocker(machine="mac-a") is None

    retried = store.create_or_adopt_batch(["mac-a"])
    if may_create_new_request:
        assert retried["requests"][0]["request_id"] != request_id
    else:
        assert retried["requests"][0]["request_id"] == request_id


def test_registry_changed_flag_does_not_release_live_claim_fence(store, clock):
    batch = store.create_or_adopt_batch(["mac-a"])
    request_id = batch["requests"][0]["request_id"]
    issue(store, request_id)
    clock.value = 1100.0
    redeem(store, request_id)

    store.flag_registry_changed(request_id, evidence_code="registry_host_changed")

    current = store.batch(batch["batch_id"])["requests"][0]
    assert current["state"] == "redeemed"
    assert current["evidence"] == {
        "registry_changed_pending": True,
        "registry_changed_code": "registry_host_changed",
    }
    assert store.mutation_blocker(machine="mac-a") is not None


def test_exact_revalidation_resolves_only_preclaim_review(store, clock):
    preclaim = store.create_or_adopt_batch(["mac-a"])
    preclaim_id = preclaim["requests"][0]["request_id"]
    store.fail_before_claim(
        preclaim_id,
        state="needs_review",
        error_code="duplicate_host",
    )

    store.resolve_preclaim_review(preclaim_id, evidence_code="registry_exact")

    resolved = store.batch(preclaim["batch_id"])["requests"][0]
    assert resolved["state"] == "retryable"
    assert resolved["evidence"] == {"preclaim_resolution": "registry_exact"}

    live = store.create_or_adopt_batch(["mac-b"])
    live_id = live["requests"][0]["request_id"]
    issue(store, live_id, target=TARGET_B)
    clock.value = 1100.0
    redeem(store, live_id, target=TARGET_B)
    store.adopt_status(
        live_id,
        {"request_id": live_id, "state": "needs_review", "error_code": "third_state"},
        direct_source=TARGET_B.resolved_address,
    )

    with pytest.raises(RepairStoreError, match="review_not_preclaim"):
        store.resolve_preclaim_review(live_id, evidence_code="registry_exact")
    assert store.batch(live["batch_id"])["requests"][0]["state"] == "needs_review"


def test_park_changes_only_scheduling_state_and_not_durable_authority(store):
    store.create_or_adopt_batch(["mac-a"])
    request = store.claim_next_dispatch()
    issue(store, request["request_id"])
    store.mark_dispatched(request["request_id"])
    with sqlite3.connect(store.path) as connection:
        connection.row_factory = sqlite3.Row
        before = dict(connection.execute(
            "SELECT * FROM enrollment_repair_requests WHERE request_id = ?",
            (request["request_id"],),
        ).fetchone())

    store.park(request["request_id"])

    with sqlite3.connect(store.path) as connection:
        connection.row_factory = sqlite3.Row
        after = dict(connection.execute(
            "SELECT * FROM enrollment_repair_requests WHERE request_id = ?",
            (request["request_id"],),
        ).fetchone())
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(enrollment_repair_requests)")
        }
    assert after["state"] == "confirmation_pending"
    assert after["dispatch_slot"] is None
    for field in (
        "ticket_digest",
        "ticket_status",
        "issued_at",
        "redemption_expires_at",
        "registry_host",
        "resolved_address",
        "controller_url",
        "site_id",
        "site_name",
        "controller_id",
        "fleet_token_digest",
    ):
        assert after[field] == before[field]
    assert columns.isdisjoint(
        {"claim", "journal", "authority_deadline", "recovery_deadline", "apply_gate"}
    )


def test_redeem_uses_constant_time_digest_comparisons(store, clock):
    batch = store.create_or_adopt_batch(["mac-a"])
    request_id = batch["requests"][0]["request_id"]
    issue(store, request_id)
    clock.value = 1100.0

    with patch(
        "backend.enrollment_repair_store.hmac.compare_digest",
        wraps=__import__("hmac").compare_digest,
    ) as compare_digest:
        redeem(store, request_id)

    assert compare_digest.call_count >= 2


def test_park_rejects_stale_prior_state_before_clearing_slot(store):
    store.create_or_adopt_batch(["mac-a"])
    request = store.claim_next_dispatch()
    issue(store, request["request_id"])
    store.mark_dispatched(request["request_id"])

    def stale_state(connection):
        connection.execute(
            "UPDATE enrollment_repair_requests SET state = 'complete' WHERE request_id = ?",
            (request["request_id"],),
        )

    with pytest.raises(RepairStoreError, match="request_not_parkable"):
        run_with_stale_request_read(
            store,
            stale_state,
            lambda: store.park(request["request_id"]),
        )

    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT state, dispatch_slot FROM enrollment_repair_requests WHERE request_id = ?",
            (request["request_id"],),
        ).fetchone() == ("dispatched", 1)


def test_resolve_preclaim_review_rechecks_all_preclaim_fields_on_write(store):
    batch = store.create_or_adopt_batch(["mac-a"])
    request_id = batch["requests"][0]["request_id"]
    store.fail_before_claim(request_id, state="needs_review", error_code="duplicate_host")

    def stale_claim_authority(connection):
        connection.execute(
            """UPDATE enrollment_repair_requests
               SET ticket_digest = ?, ticket_status = 'issued', issued_at = ?
               WHERE request_id = ?""",
            (sha256_text("stale-ticket"), 1100.0, request_id),
        )

    with pytest.raises(RepairStoreError, match="review_not_preclaim"):
        run_with_stale_request_read(
            store,
            stale_claim_authority,
            lambda: store.resolve_preclaim_review(
                request_id, evidence_code="registry_exact"
            ),
        )

    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT state, ticket_status, issued_at FROM enrollment_repair_requests WHERE request_id = ?",
            (request_id,),
        ).fetchone() == ("needs_review", None, None)


def test_flag_registry_changed_rechecks_live_claim_state_and_ticket(store, clock):
    batch = store.create_or_adopt_batch(["mac-a"])
    request_id = batch["requests"][0]["request_id"]
    issue(store, request_id)
    clock.value = 1100.0
    redeem(store, request_id)

    def stale_live_claim(connection):
        connection.execute(
            "UPDATE enrollment_repair_requests SET state = 'complete' WHERE request_id = ?",
            (request_id,),
        )

    with pytest.raises(RepairStoreError, match="request_has_no_live_claim"):
        run_with_stale_request_read(
            store,
            stale_live_claim,
            lambda: store.flag_registry_changed(request_id),
        )

    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT state, evidence_json FROM enrollment_repair_requests WHERE request_id = ?",
            (request_id,),
        ).fetchone() == ("redeemed", None)


def test_recover_scheduling_slot_rejects_unexpected_state_without_clearing_it(store):
    store.create_or_adopt_batch(["mac-a"])
    request = store.claim_next_dispatch()
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE enrollment_repair_requests SET state = 'complete' WHERE request_id = ?",
            (request["request_id"],),
        )

    with pytest.raises(RepairStoreError, match="dispatch_slot_state_invalid"):
        store.recover_scheduling_slot()

    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT state, dispatch_slot FROM enrollment_repair_requests WHERE request_id = ?",
            (request["request_id"],),
        ).fetchone() == ("complete", 1)


def test_expiry_rejects_stale_prior_request_state(store, clock):
    batch = store.create_or_adopt_batch(["mac-a"])
    request_id = batch["requests"][0]["request_id"]
    issue(store, request_id)
    clock.value = 1120.0

    def stale_state(connection):
        connection.execute(
            "UPDATE enrollment_repair_requests SET state = 'complete' WHERE request_id = ?",
            (request_id,),
        )

    with pytest.raises(RepairStoreError, match="ticket_not_redeemable"):
        run_with_stale_request_read(
            store,
            stale_state,
            lambda: redeem(store, request_id),
        )

    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT state, ticket_status FROM enrollment_repair_requests WHERE request_id = ?",
            (request_id,),
        ).fetchone() == ("ticket_issued", "issued")
