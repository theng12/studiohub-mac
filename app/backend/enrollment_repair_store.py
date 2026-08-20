"""Durable, digest-only state for Controller-managed enrollment repair.

The repair store lives beside the existing enrollment database, but owns only
the two repair tables.  In particular, it never stores a ticket, fleet token,
permanent enrollment code, or any other plaintext credential.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import enrollment


REQUEST_STATES = (
    "queued", "checking", "hub_update_required", "updating_hub",
    "ticket_issued", "dispatched", "redeemed", "verifying",
    "confirmation_pending", "complete", "retryable", "needs_review",
)
TICKET_STATES = ("issued", "redeemed", "expired")
_MAX_ERROR_CODE = 80
_MAX_EVIDENCE_BYTES = 16_384
_SHA256_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class RepairStoreError(ValueError):
    """Bounded state-machine failure safe for coordinator error mapping."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def enrollment_repair_journal_path() -> Path:
    return enrollment.DB_FILE.parent / ".enrollment_repair_journal.json"


def enrollment_repair_lock_path() -> Path:
    return enrollment.DB_FILE.parent / ".enrollment_repair_journal.json.lock"


@dataclass(frozen=True)
class ControllerIdentity:
    role: str
    site_id: str
    site_name: str
    controller_id: str


@dataclass(frozen=True)
class TargetIdentity:
    machine: str
    registry_host: str
    resolved_address: str
    controller_url: str


class RepairStore:
    """SQLite-backed repair records with stable, redacted read models."""

    def __init__(
        self,
        path: Path = enrollment.DB_FILE,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path)
        self.clock = clock
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(mode=0o600, exist_ok=True)
        os.chmod(self.path, 0o600)
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS enrollment_repair_batches (
                    batch_id TEXT PRIMARY KEY,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    state TEXT NOT NULL,
                    targets_json TEXT NOT NULL,
                    total_count INTEGER NOT NULL DEFAULT 0,
                    queued_count INTEGER NOT NULL DEFAULT 0,
                    complete_count INTEGER NOT NULL DEFAULT 0,
                    retryable_count INTEGER NOT NULL DEFAULT 0,
                    review_count INTEGER NOT NULL DEFAULT 0,
                    pending_count INTEGER NOT NULL DEFAULT 0,
                    repaired_count INTEGER NOT NULL DEFAULT 0,
                    attention_count INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT,
                    evidence_json TEXT,
                    CHECK (state IN ('queued', 'running', 'complete', 'complete_with_attention')),
                    CHECK (length(error_code) <= 80),
                    CHECK (length(evidence_json) <= 16384)
                );

                CREATE TABLE IF NOT EXISTS enrollment_repair_requests (
                    request_id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    target_machine TEXT NOT NULL,
                    registry_host TEXT,
                    resolved_address TEXT,
                    controller_url TEXT,
                    controller_role TEXT,
                    site_id TEXT,
                    site_name TEXT,
                    controller_id TEXT,
                    fleet_token_digest TEXT,
                    ticket_digest TEXT,
                    ticket_status TEXT,
                    issued_at REAL,
                    redemption_expires_at REAL,
                    dispatched_at REAL,
                    direct_source TEXT,
                    redeemed_at REAL,
                    observed_identity_json TEXT,
                    registry_snapshot_json TEXT,
                    state TEXT NOT NULL,
                    error_code TEXT,
                    evidence_json TEXT,
                    dispatch_slot INTEGER,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(batch_id, ordinal),
                    CHECK (length(error_code) <= 80),
                    CHECK (length(evidence_json) <= 16384),
                    CHECK (ticket_status IS NULL OR ticket_status IN ('issued', 'redeemed', 'expired')),
                    CHECK (state IN (
                        'queued', 'checking', 'hub_update_required', 'updating_hub',
                        'ticket_issued', 'dispatched', 'redeemed', 'verifying',
                        'confirmation_pending', 'complete', 'retryable', 'needs_review'
                    )),
                    FOREIGN KEY(batch_id) REFERENCES enrollment_repair_batches(batch_id)
                );

                CREATE TABLE IF NOT EXISTS enrollment_repair_batch_adoptions (
                    batch_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    PRIMARY KEY(batch_id, ordinal),
                    UNIQUE(batch_id, request_id),
                    FOREIGN KEY(batch_id) REFERENCES enrollment_repair_batches(batch_id),
                    FOREIGN KEY(request_id) REFERENCES enrollment_repair_requests(request_id)
                );

                CREATE INDEX IF NOT EXISTS repair_requests_batch_order
                    ON enrollment_repair_requests(batch_id, ordinal);
                CREATE INDEX IF NOT EXISTS repair_requests_target
                    ON enrollment_repair_requests(target_machine, updated_at);
                CREATE INDEX IF NOT EXISTS repair_batch_adoptions_request
                    ON enrollment_repair_batch_adoptions(request_id);
                CREATE UNIQUE INDEX IF NOT EXISTS one_repair_dispatch_slot
                    ON enrollment_repair_requests(dispatch_slot)
                    WHERE dispatch_slot = 1;
                CREATE UNIQUE INDEX IF NOT EXISTS one_unresolved_repair_per_target
                    ON enrollment_repair_requests(target_machine)
                    WHERE state NOT IN ('complete', 'retryable', 'hub_update_required');
                """
            )

    @staticmethod
    def _json(value: Any) -> str:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > _MAX_EVIDENCE_BYTES:
            raise ValueError("repair evidence is too large")
        return encoded

    @staticmethod
    def _targets_json(targets: Sequence[str]) -> str:
        return json.dumps(list(targets), separators=(",", ":"))

    @staticmethod
    def _require_digest(value: str, field: str) -> str:
        candidate = value if isinstance(value, str) else ""
        if not _SHA256_DIGEST.fullmatch(candidate):
            raise ValueError(f"{field} must be a canonical lowercase SHA-256 digest")
        return candidate

    @staticmethod
    def _safe_error(error_code: str | None) -> str | None:
        if error_code is None:
            return None
        value = str(error_code).strip()
        if len(value) > _MAX_ERROR_CODE:
            raise ValueError("repair error code is too long")
        return value or None

    def create_or_adopt_batch(self, machines: Sequence[str]) -> dict[str, Any]:
        """Create one ordered batch, adopting any unresolved target request."""
        ordered = sorted({str(machine).strip() for machine in machines if str(machine).strip()})
        if not ordered:
            raise ValueError("at least one target machine is required")
        now = float(self.clock())
        batch_id = uuid.uuid4().hex

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = []
            for machine in ordered:
                row = connection.execute(
                    """SELECT * FROM enrollment_repair_requests
                       WHERE target_machine = ?
                         AND state NOT IN ('complete', 'retryable', 'hub_update_required')
                       ORDER BY created_at ASC LIMIT 1""",
                    (machine,),
                ).fetchone()
                if row is not None:
                    existing.append(row)
            if len(existing) == len(ordered):
                existing_batch_ids = {row["batch_id"] for row in existing}
                if len(existing_batch_ids) == 1:
                    existing_batch_id = next(iter(existing_batch_ids))
                    existing_batch = connection.execute(
                        "SELECT targets_json FROM enrollment_repair_batches WHERE batch_id = ?",
                        (existing_batch_id,),
                    ).fetchone()
                    if (existing_batch is not None
                            and json.loads(existing_batch["targets_json"]) == ordered):
                            connection.rollback()
                            return self.batch(existing_batch_id)  # type: ignore[return-value]
                by_request_id = {row["request_id"]: row for row in existing}
                for existing_batch_id in existing_batch_ids:
                    existing_batch = connection.execute(
                        "SELECT targets_json FROM enrollment_repair_batches WHERE batch_id = ?",
                        (existing_batch_id,),
                    ).fetchone()
                    adopted_ids = [row["request_id"] for row in connection.execute(
                        """SELECT request_id FROM enrollment_repair_batch_adoptions
                           WHERE batch_id = ? ORDER BY ordinal ASC""",
                        (existing_batch_id,),
                    ).fetchall()]
                    if not adopted_ids or not all(request_id in by_request_id for request_id in adopted_ids):
                        continue
                    targets = set(json.loads(existing_batch["targets_json"]))
                    replay_targets = targets | {
                        by_request_id[request_id]["target_machine"] for request_id in adopted_ids
                    }
                    if replay_targets == set(ordered):
                        adopted = [self._request_result(by_request_id[request_id]) for request_id in adopted_ids]
                        connection.rollback()
                        result = self.batch(existing_batch_id)  # type: ignore[assignment]
                        result["adopted_requests"] = adopted
                        return result  # type: ignore[return-value]
                connection.rollback()
                raise ValueError("target machines already belong to active repair batches")
            adopted = [self._request_result(row) for row in existing]
            new_targets = [machine for machine in ordered if all(
                row["target_machine"] != machine for row in existing
            )]
            if not new_targets:
                connection.rollback()
                raise ValueError("target machines already belong to active repair batches")
            connection.execute(
                """INSERT INTO enrollment_repair_batches
                   (batch_id, created_at, updated_at, state, targets_json, total_count,
                    queued_count)
                   VALUES (?, ?, ?, 'queued', ?, ?, ?)""",
                (
                    batch_id, now, now, self._targets_json(new_targets), len(new_targets),
                    len(new_targets),
                ),
            )
            connection.executemany(
                """INSERT INTO enrollment_repair_batch_adoptions
                   (batch_id, request_id, ordinal) VALUES (?, ?, ?)""",
                [(batch_id, row["request_id"], ordinal) for ordinal, row in enumerate(existing)],
            )
            for ordinal, machine in enumerate(new_targets):
                request_id = uuid.uuid4().hex
                connection.execute(
                    """INSERT INTO enrollment_repair_requests
                       (request_id, batch_id, ordinal, target_machine, state,
                        created_at, updated_at)
                       VALUES (?, ?, ?, ?, 'queued', ?, ?)""",
                    (request_id, batch_id, ordinal, machine, now, now),
                )
            connection.execute(
                "UPDATE enrollment_repair_batches SET updated_at = ? WHERE batch_id = ?",
                (now, batch_id),
            )
            connection.commit()
        result = self.batch(batch_id)  # type: ignore[assignment]
        if adopted:
            result["adopted_requests"] = adopted
        return result  # type: ignore[return-value]

    def batch(self, batch_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            batch_row = connection.execute(
                "SELECT * FROM enrollment_repair_batches WHERE batch_id = ?",
                (str(batch_id),),
            ).fetchone()
            if batch_row is None:
                return None
            request_rows = connection.execute(
                """SELECT * FROM enrollment_repair_requests
                   WHERE batch_id = ? ORDER BY ordinal ASC""",
                (str(batch_id),),
            ).fetchall()
            adopted_rows = connection.execute(
                """SELECT request.* FROM enrollment_repair_batch_adoptions AS adoption
                   JOIN enrollment_repair_requests AS request
                     ON request.request_id = adoption.request_id
                   WHERE adoption.batch_id = ? ORDER BY adoption.ordinal ASC""",
                (str(batch_id),),
            ).fetchall()
        result = dict(batch_row)
        result["targets"] = json.loads(result.pop("targets_json"))
        result["evidence"] = self._decode_json(result.pop("evidence_json"))
        result["requests"] = [self._request_result(row) for row in request_rows]
        if adopted_rows:
            result["adopted_requests"] = [
                self._request_result(row) for row in adopted_rows
            ]
        return result

    @staticmethod
    def _decode_json(value: str | None) -> Any:
        if not value:
            return None
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None

    def _request_result(self, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        # Ticket material is intentionally not part of the owner-facing read
        # model, including its digest and lifecycle marker.  The durable row
        # retains those non-secret values for later state transitions.
        result.pop("ticket_digest", None)
        result.pop("ticket_status", None)
        result.pop("fleet_token_digest", None)
        result["evidence"] = self._decode_json(result.pop("evidence_json"))
        result["observed_identity"] = self._decode_json(
            result.pop("observed_identity_json")
        )
        result["registry_snapshot"] = self._decode_json(
            result.pop("registry_snapshot_json")
        )
        return result

    @staticmethod
    def _identity_dict(identity: ControllerIdentity | TargetIdentity) -> dict[str, str]:
        return {
            field: str(getattr(identity, field))
            for field in identity.__dataclass_fields__
        }

    @staticmethod
    def _observed_identity_evidence(
        observed: Mapping[str, Any],
        *,
        target: TargetIdentity,
        controller: ControllerIdentity,
    ) -> dict[str, bool]:
        """Reduce target-supplied identity strings to credential-free evidence."""
        expected = {
            "role": "agent",
            "site_id": controller.site_id,
            "site_name": controller.site_name,
            "controller_id": target.machine,
            "parent_controller_url": target.controller_url,
        }
        return {
            f"{field}_matches": (
                isinstance(observed.get(field), str)
                and hmac.compare_digest(observed[field], value)
            )
            for field, value in expected.items()
        }

    def _merge_evidence(self, raw: str | None, values: Mapping[str, Any]) -> str:
        evidence = self._decode_json(raw)
        if not isinstance(evidence, dict):
            evidence = {}
        evidence.update(values)
        return self._json(evidence)

    def _refresh_batch_locked(
        self,
        connection: sqlite3.Connection,
        batch_id: str,
        now: float,
    ) -> None:
        rows = connection.execute(
            "SELECT state FROM enrollment_repair_requests WHERE batch_id = ?",
            (batch_id,),
        ).fetchall()
        states = [row["state"] for row in rows]
        total = len(states)
        repaired = states.count("complete")
        retryable = states.count("retryable")
        review = states.count("needs_review") + states.count("hub_update_required")
        pending = sum(
            state in {
                "ticket_issued", "dispatched", "redeemed", "verifying",
                "confirmation_pending", "updating_hub",
            }
            for state in states
        )
        queued = sum(state in {"queued", "checking"} for state in states)
        terminal = repaired + retryable + review
        if total and repaired == total:
            batch_state = "complete"
        elif total and terminal == total:
            batch_state = "complete_with_attention"
        elif all(state == "queued" for state in states):
            batch_state = "queued"
        else:
            batch_state = "running"
        connection.execute(
            """UPDATE enrollment_repair_batches
               SET updated_at = ?, state = ?, total_count = ?, queued_count = ?,
                   complete_count = ?, retryable_count = ?, review_count = ?,
                   pending_count = ?, repaired_count = ?, attention_count = ?
               WHERE batch_id = ?""",
            (
                now,
                batch_state,
                total,
                queued,
                repaired,
                retryable,
                review,
                pending,
                repaired,
                retryable + review,
                batch_id,
            ),
        )

    def issue_ticket(
        self,
        request_id: str,
        *,
        target: TargetIdentity,
        controller: ControllerIdentity,
        fleet_token_digest: str,
        ticket_digest: str,
        redemption_expires_at: float,
    ) -> None:
        fleet_token_digest = self._require_digest(
            fleet_token_digest, "fleet_token_digest"
        )
        ticket_digest = self._require_digest(ticket_digest, "ticket_digest")
        now = float(self.clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """UPDATE enrollment_repair_requests
                   SET registry_host = ?, resolved_address = ?, controller_url = ?,
                       controller_role = ?, site_id = ?, site_name = ?, controller_id = ?,
                       fleet_token_digest = ?, ticket_digest = ?, ticket_status = 'issued',
                       issued_at = ?, redemption_expires_at = ?, state = 'ticket_issued',
                       attempt = attempt + 1, updated_at = ?, error_code = NULL
                   WHERE request_id = ? AND target_machine = ?
                     AND state IN ('queued', 'checking') AND ticket_status IS NULL""",
                (
                    target.registry_host,
                    target.resolved_address,
                    target.controller_url,
                    controller.role,
                    controller.site_id,
                    controller.site_name,
                    controller.controller_id,
                    fleet_token_digest,
                    ticket_digest,
                    now,
                    float(redemption_expires_at),
                    now,
                    str(request_id),
                    target.machine,
                ),
            ).rowcount
            if changed != 1:
                connection.rollback()
                raise RepairStoreError("request_not_issuable")
            batch_id = connection.execute(
                "SELECT batch_id FROM enrollment_repair_requests WHERE request_id = ?",
                (str(request_id),),
            ).fetchone()["batch_id"]
            self._refresh_batch_locked(connection, batch_id, now)
            connection.commit()

    def claim_next_dispatch(self) -> dict[str, Any] | None:
        """Claim the sole fresh-dispatch scheduling slot in stable order."""
        now = float(self.clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM enrollment_repair_requests WHERE dispatch_slot = 1"
            ).fetchone() is not None:
                connection.rollback()
                return None
            row = connection.execute(
                """SELECT request.request_id, request.batch_id
                   FROM enrollment_repair_requests AS request
                   JOIN enrollment_repair_batches AS batch
                     ON batch.batch_id = request.batch_id
                   WHERE request.state = 'queued' AND request.dispatch_slot IS NULL
                   ORDER BY batch.created_at ASC, batch.rowid ASC,
                            request.ordinal ASC LIMIT 1"""
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            changed = connection.execute(
                """UPDATE enrollment_repair_requests
                   SET state = 'checking', dispatch_slot = 1, updated_at = ?
                   WHERE request_id = ? AND state = 'queued' AND dispatch_slot IS NULL""",
                (now, row["request_id"]),
            ).rowcount
            if changed != 1:
                connection.rollback()
                return None
            self._refresh_batch_locked(connection, row["batch_id"], now)
            result = connection.execute(
                "SELECT * FROM enrollment_repair_requests WHERE request_id = ?",
                (row["request_id"],),
            ).fetchone()
            connection.commit()
        return self._request_result(result)

    def has_dispatchable_request(self) -> bool:
        """Return whether fresh repair work can claim the scheduling slot."""
        with self._connect() as connection:
            row = connection.execute(
                """SELECT 1
                   WHERE NOT EXISTS (
                       SELECT 1 FROM enrollment_repair_requests WHERE dispatch_slot = 1
                   ) AND EXISTS (
                       SELECT 1 FROM enrollment_repair_requests
                       WHERE state = 'queued' AND dispatch_slot IS NULL
                   )"""
            ).fetchone()
        return row is not None

    def mark_dispatched(self, request_id: str) -> None:
        now = float(self.clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT batch_id, state FROM enrollment_repair_requests WHERE request_id = ?",
                (str(request_id),),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise RepairStoreError("request_not_found")
            if row["state"] == "dispatched":
                connection.rollback()
                return
            changed = connection.execute(
                """UPDATE enrollment_repair_requests
                   SET state = 'dispatched', dispatched_at = ?, updated_at = ?
                   WHERE request_id = ? AND state = 'ticket_issued'
                     AND ticket_status = 'issued'""",
                (now, now, str(request_id)),
            ).rowcount
            if changed != 1:
                connection.rollback()
                raise RepairStoreError("request_not_dispatchable")
            self._refresh_batch_locked(connection, row["batch_id"], now)
            connection.commit()

    def redeem(
        self,
        request_id: str,
        *,
        ticket: str,
        redemption_expires_at: float,
        target_machine: str,
        direct_source: str,
        observed_identity: Mapping[str, Any],
        registry_snapshot: TargetIdentity,
        controller: ControllerIdentity,
        fleet_token_digest: str,
    ) -> dict[str, Any]:
        """Atomically consume one exact ticket and return its only claim."""
        supplied_fleet_digest = self._require_digest(
            fleet_token_digest, "fleet_token_digest"
        )
        supplied_ticket_digest = hashlib.sha256(
            (ticket if isinstance(ticket, str) else "").encode("utf-8")
        ).hexdigest()
        observed_json = self._json(self._observed_identity_evidence(
            observed_identity,
            target=registry_snapshot,
            controller=controller,
        ))
        registry_json = self._json(self._identity_dict(registry_snapshot))
        now = float(self.clock())
        expired = False
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM enrollment_repair_requests WHERE request_id = ?",
                (str(request_id),),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise RepairStoreError("request_not_found")
            if row["ticket_status"] in {"redeemed", "expired"}:
                code = (
                    "ticket_replayed"
                    if row["ticket_status"] == "redeemed"
                    else "ticket_expired"
                )
                connection.rollback()
                raise RepairStoreError(code)
            if row["ticket_status"] != "issued" or row["state"] not in {
                "ticket_issued", "dispatched"
            }:
                connection.rollback()
                raise RepairStoreError("ticket_not_redeemable")
            durable_expiry = float(row["redemption_expires_at"])
            if float(redemption_expires_at) != durable_expiry:
                connection.rollback()
                raise RepairStoreError("redemption_expiry_mismatch")
            if now >= durable_expiry:
                changed = connection.execute(
                    """UPDATE enrollment_repair_requests
                       SET ticket_status = 'expired', state = 'confirmation_pending',
                           error_code = 'ticket_expired', dispatch_slot = NULL,
                           updated_at = ?
                       WHERE request_id = ? AND ticket_status = 'issued'
                         AND state = ?""",
                    (now, str(request_id), row["state"]),
                ).rowcount
                if changed != 1:
                    connection.rollback()
                    raise RepairStoreError("ticket_not_redeemable")
                self._refresh_batch_locked(connection, row["batch_id"], now)
                connection.commit()
                expired = True
            else:
                exact_target = (
                    row["target_machine"] == str(target_machine)
                    and row["target_machine"] == registry_snapshot.machine
                    and row["registry_host"] == registry_snapshot.registry_host
                    and row["resolved_address"] == registry_snapshot.resolved_address
                    and row["controller_url"] == registry_snapshot.controller_url
                    and row["resolved_address"] == str(direct_source)
                )
                exact_controller = (
                    row["controller_role"] == controller.role
                    and row["site_id"] == controller.site_id
                    and row["site_name"] == controller.site_name
                    and row["controller_id"] == controller.controller_id
                )
                exact_ticket = hmac.compare_digest(
                    str(row["ticket_digest"] or ""), supplied_ticket_digest
                )
                exact_fleet_token = hmac.compare_digest(
                    str(row["fleet_token_digest"] or ""), supplied_fleet_digest
                )
                if not exact_ticket:
                    connection.rollback()
                    raise RepairStoreError("ticket_mismatch")
                if not exact_fleet_token:
                    connection.rollback()
                    raise RepairStoreError("fleet_token_mismatch")
                if not exact_target:
                    connection.rollback()
                    raise RepairStoreError("target_binding_mismatch")
                if not exact_controller:
                    connection.rollback()
                    raise RepairStoreError("controller_snapshot_changed")
                if connection.execute(
                    """SELECT 1 FROM enrollment_repair_requests
                       WHERE target_machine = ? AND request_id != ? AND state = 'complete'
                       LIMIT 1""",
                    (row["target_machine"], str(request_id)),
                ).fetchone() is not None:
                    connection.rollback()
                    raise RepairStoreError("repair_already_complete")
                changed = connection.execute(
                    """UPDATE enrollment_repair_requests
                       SET ticket_status = 'redeemed', state = 'redeemed',
                           direct_source = ?, redeemed_at = ?,
                           observed_identity_json = ?, registry_snapshot_json = ?,
                           updated_at = ?, error_code = NULL
                       WHERE request_id = ? AND ticket_status = 'issued'
                         AND state IN ('ticket_issued', 'dispatched')""",
                    (
                        str(direct_source),
                        now,
                        observed_json,
                        registry_json,
                        now,
                        str(request_id),
                    ),
                ).rowcount
                if changed != 1:
                    connection.rollback()
                    raise RepairStoreError("ticket_replayed")
                self._refresh_batch_locked(connection, row["batch_id"], now)
                connection.commit()
        if expired:
            raise RepairStoreError("ticket_expired")
        return {
            "schema": "studiohub.enrollment-repair-claim",
            "schema_version": 1,
            "request_id": str(request_id),
            "target_machine_id": str(target_machine),
            "role": "agent",
            "site_id": controller.site_id,
            "site_name": controller.site_name,
            "controller_id": str(target_machine),
        }

    def park(
        self,
        request_id: str,
        *,
        error_code: str = "confirmation_pending",
    ) -> None:
        now = float(self.clock())
        safe_error = self._safe_error(error_code)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT batch_id, state FROM enrollment_repair_requests WHERE request_id = ?",
                (str(request_id),),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise RepairStoreError("request_not_found")
            if row["state"] not in {
                "ticket_issued", "dispatched", "redeemed", "verifying",
                "confirmation_pending",
            }:
                connection.rollback()
                raise RepairStoreError("request_not_parkable")
            changed = connection.execute(
                """UPDATE enrollment_repair_requests
                   SET state = 'confirmation_pending', error_code = ?,
                       dispatch_slot = NULL, updated_at = ?
                   WHERE request_id = ? AND state = ?""",
                (safe_error, now, str(request_id), row["state"]),
            ).rowcount
            if changed != 1:
                connection.rollback()
                raise RepairStoreError("request_not_parkable")
            self._refresh_batch_locked(connection, row["batch_id"], now)
            connection.commit()

    def adopt_status(
        self,
        request_id: str,
        status: Mapping[str, Any],
        *,
        direct_source: str,
    ) -> dict[str, Any]:
        """Adopt authenticated target-terminal evidence without replaying a claim."""
        now = float(self.clock())
        terminal = str(status.get("state", ""))
        if terminal not in {"complete", "never_applied", "needs_review"}:
            raise RepairStoreError("status_not_terminal")
        if str(status.get("request_id", "")) != str(request_id):
            raise RepairStoreError("status_request_mismatch")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM enrollment_repair_requests WHERE request_id = ?",
                (str(request_id),),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise RepairStoreError("request_not_found")
            if str(direct_source) != row["resolved_address"]:
                connection.rollback()
                raise RepairStoreError("source_host_mismatch")
            if row["state"] in {"complete", "retryable", "needs_review"}:
                evidence = self._decode_json(row["evidence_json"])
                if isinstance(evidence, dict) and evidence.get("agent_terminal_state") == terminal:
                    connection.rollback()
                    return self._request_result(row)
                connection.rollback()
                raise RepairStoreError("terminal_status_conflict")
            if row["state"] not in {
                "ticket_issued", "dispatched", "redeemed", "verifying",
                "confirmation_pending",
            }:
                connection.rollback()
                raise RepairStoreError("status_not_adoptable")
            if terminal == "complete":
                identity = status.get("identity")
                expected = {
                    "role": "agent",
                    "site_id": row["site_id"],
                    "site_name": row["site_name"],
                    "controller_id": row["target_machine"],
                }
                if identity != expected or row["ticket_status"] != "redeemed":
                    connection.rollback()
                    raise RepairStoreError("status_identity_mismatch")
                stored_state = "complete"
                error_code = None
                ticket_status = "redeemed"
            elif terminal == "never_applied":
                stored_state = "retryable"
                error_code = self._safe_error(
                    str(status.get("error_code") or "never_applied")
                )
                ticket_status = (
                    "expired" if row["ticket_status"] == "issued" else row["ticket_status"]
                )
            else:
                stored_state = "needs_review"
                error_code = self._safe_error(
                    str(status.get("error_code") or "needs_review")
                )
                ticket_status = (
                    "expired" if row["ticket_status"] == "issued" else row["ticket_status"]
                )
            evidence = self._merge_evidence(
                row["evidence_json"],
                {"agent_terminal_state": terminal},
            )
            changed = connection.execute(
                """UPDATE enrollment_repair_requests
                   SET state = ?, ticket_status = ?, error_code = ?,
                       evidence_json = ?, dispatch_slot = NULL, updated_at = ?
                   WHERE request_id = ? AND state = ?""",
                (
                    stored_state,
                    ticket_status,
                    error_code,
                    evidence,
                    now,
                    str(request_id),
                    row["state"],
                ),
            ).rowcount
            if changed != 1:
                connection.rollback()
                raise RepairStoreError("terminal_status_conflict")
            self._refresh_batch_locked(connection, row["batch_id"], now)
            result = connection.execute(
                "SELECT * FROM enrollment_repair_requests WHERE request_id = ?",
                (str(request_id),),
            ).fetchone()
            connection.commit()
        return self._request_result(result)

    def request(self, request_id: str) -> dict[str, Any] | None:
        """Return one redacted request for coordinator-only status binding."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM enrollment_repair_requests WHERE request_id = ?",
                (str(request_id),),
            ).fetchone()
        return self._request_result(row) if row is not None else None

    def pending_status_requests(self) -> list[dict[str, Any]]:
        """List parked targets whose terminal evidence may be adopted safely."""
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM enrollment_repair_requests
                   WHERE state = 'confirmation_pending'
                   ORDER BY created_at ASC, rowid ASC"""
            ).fetchall()
        return [self._request_result(row) for row in rows]

    def scheduling_request(self) -> dict[str, Any] | None:
        """Return the sole fresh-dispatch slot holder, if one is durable."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM enrollment_repair_requests WHERE dispatch_slot = 1"
            ).fetchone()
        return self._request_result(row) if row is not None else None

    def fail_before_claim(
        self,
        request_id: str,
        *,
        state: str,
        error_code: str,
    ) -> None:
        if state not in {"retryable", "needs_review", "hub_update_required"}:
            raise ValueError("invalid preclaim state")
        now = float(self.clock())
        safe_error = self._safe_error(error_code)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT batch_id, state FROM enrollment_repair_requests WHERE request_id = ?",
                (str(request_id),),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise RepairStoreError("request_not_found")
            changed = connection.execute(
                """UPDATE enrollment_repair_requests
                   SET state = ?, error_code = ?, dispatch_slot = NULL, updated_at = ?
                   WHERE request_id = ? AND ticket_status IS NULL
                     AND issued_at IS NULL AND redeemed_at IS NULL
                     AND state IN ('queued', 'checking', 'hub_update_required', 'updating_hub')""",
                (state, safe_error, now, str(request_id)),
            ).rowcount
            if changed != 1:
                connection.rollback()
                raise RepairStoreError("request_has_claim_authority")
            self._refresh_batch_locked(connection, row["batch_id"], now)
            connection.commit()

    def resolve_preclaim_review(
        self,
        request_id: str,
        *,
        evidence_code: str,
    ) -> None:
        now = float(self.clock())
        safe_evidence = self._safe_error(evidence_code)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM enrollment_repair_requests WHERE request_id = ?",
                (str(request_id),),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise RepairStoreError("request_not_found")
            if not (
                row["state"] == "needs_review"
                and row["ticket_status"] is None
                and row["ticket_digest"] is None
                and row["issued_at"] is None
                and row["redeemed_at"] is None
            ):
                connection.rollback()
                raise RepairStoreError("review_not_preclaim")
            evidence = self._json({"preclaim_resolution": safe_evidence})
            changed = connection.execute(
                """UPDATE enrollment_repair_requests
                   SET state = 'retryable', error_code = NULL, evidence_json = ?,
                       updated_at = ? WHERE request_id = ? AND state = 'needs_review'
                         AND ticket_status IS NULL AND ticket_digest IS NULL
                         AND issued_at IS NULL AND redeemed_at IS NULL""",
                (evidence, now, str(request_id)),
            ).rowcount
            if changed != 1:
                connection.rollback()
                raise RepairStoreError("review_not_preclaim")
            self._refresh_batch_locked(connection, row["batch_id"], now)
            connection.commit()

    def flag_registry_changed(
        self,
        request_id: str,
        *,
        evidence_code: str = "registry_changed",
    ) -> None:
        now = float(self.clock())
        safe_evidence = self._safe_error(evidence_code)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM enrollment_repair_requests WHERE request_id = ?",
                (str(request_id),),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise RepairStoreError("request_not_found")
            if (
                row["state"] not in {"redeemed", "verifying", "confirmation_pending"}
                or row["ticket_status"] != "redeemed"
            ):
                connection.rollback()
                raise RepairStoreError("request_has_no_live_claim")
            evidence = self._merge_evidence(
                row["evidence_json"],
                {
                    "registry_changed_pending": True,
                    "registry_changed_code": safe_evidence,
                },
            )
            changed = connection.execute(
                """UPDATE enrollment_repair_requests
                   SET evidence_json = ?, updated_at = ?
                   WHERE request_id = ? AND state = ? AND ticket_status = 'redeemed'""",
                (evidence, now, str(request_id), row["state"]),
            ).rowcount
            if changed != 1:
                connection.rollback()
                raise RepairStoreError("request_has_no_live_claim")
            self._refresh_batch_locked(connection, row["batch_id"], now)
            connection.commit()

    def recover_scheduling_slot(self) -> int:
        """Clear a crash-orphaned slot without reconstructing ticket plaintext."""
        now = float(self.clock())
        recovered = 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM enrollment_repair_requests WHERE dispatch_slot = 1"
            ).fetchall()
            for row in rows:
                if row["state"] in {
                    "ticket_issued", "dispatched", "redeemed", "verifying",
                }:
                    changed = connection.execute(
                        """UPDATE enrollment_repair_requests
                           SET state = 'confirmation_pending', dispatch_slot = NULL,
                               error_code = 'confirmation_pending', updated_at = ?
                           WHERE request_id = ? AND dispatch_slot = 1 AND state = ?""",
                        (now, row["request_id"], row["state"]),
                    ).rowcount
                elif row["state"] == "checking" and row["ticket_status"] is None:
                    changed = connection.execute(
                        """UPDATE enrollment_repair_requests
                           SET state = 'queued', dispatch_slot = NULL, updated_at = ?
                           WHERE request_id = ? AND dispatch_slot = 1
                             AND state = 'checking' AND ticket_status IS NULL""",
                        (now, row["request_id"]),
                    ).rowcount
                else:
                    connection.rollback()
                    raise RepairStoreError("dispatch_slot_state_invalid")
                if changed != 1:
                    connection.rollback()
                    raise RepairStoreError("dispatch_slot_conflict")
                self._refresh_batch_locked(connection, row["batch_id"], now)
                recovered += 1
            connection.commit()
        return recovered

    def mutation_blocker(
        self,
        *,
        machine: str | None = None,
    ) -> dict[str, Any] | None:
        parameters: list[Any] = []
        machine_clause = ""
        if machine is not None:
            machine_clause = " AND target_machine = ?"
            parameters.append(str(machine))
        with self._connect() as connection:
            row = connection.execute(
                """SELECT request_id, target_machine, state, error_code, evidence_json
                   FROM enrollment_repair_requests
                   WHERE state IN ('redeemed', 'verifying', 'confirmation_pending')"""
                + machine_clause
                + " ORDER BY redeemed_at ASC, created_at ASC LIMIT 1",
                parameters,
            ).fetchone()
        if row is None:
            return None
        return {
            "request_id": row["request_id"],
            "target_machine": row["target_machine"],
            "state": row["state"],
            "error_code": row["error_code"],
            "evidence": self._decode_json(row["evidence_json"]),
        }


def reset_for_tests() -> None:
    """Compatibility seam for the isolated test harness.

    The store intentionally keeps no process-global cache or background task.
    """


__all__ = [
    "ControllerIdentity", "RepairStore", "RepairStoreError", "REQUEST_STATES",
    "TICKET_STATES", "TargetIdentity",
]
