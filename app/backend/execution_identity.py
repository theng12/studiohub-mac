"""Validate externally assigned GenStudio execution identity.

GenStudio is the sole global job authority.  Studio Hub only remembers enough
identity locally to make one site execution safe:

* GenStudio supplies job/attempt ids and the monotonically increasing fence.
* Studio Hub never creates or advances a global fencing token.
* An older fence is rejected after this controller has observed a newer one.
* Replaying the same idempotency identity and payload is safe; reusing it for a
  different payload is rejected before local dispatch.

The guard is SQLite-backed so it remains available when optional PostgreSQL
telemetry is disabled or unreachable.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from .registry import DATA_DIR

DB_FILE = DATA_DIR / "execution_identity.db"
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_OPERATION = re.compile(r"^[a-z][a-z0-9._:-]{0,79}$")
_FIELDS = {
    "genstudio_job_id", "genstudio_attempt_id", "idempotency_key",
    "fencing_token", "site_id", "operation", "model_revision",
    "voice_revision", "lease_expires_at",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS genstudio_fences (
  genstudio_job_id TEXT PRIMARY KEY,
  highest_fencing_token INTEGER NOT NULL,
  genstudio_attempt_id TEXT NOT NULL,
  idempotency_hash TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  operation TEXT NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS genstudio_idempotency (
  genstudio_job_id TEXT NOT NULL,
  idempotency_hash TEXT NOT NULL,
  genstudio_attempt_id TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  local_batch_id TEXT,
  accepted_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  PRIMARY KEY (genstudio_job_id, idempotency_hash)
);
CREATE INDEX IF NOT EXISTS idx_genstudio_attempt
  ON genstudio_idempotency(genstudio_attempt_id);
CREATE TABLE IF NOT EXISTS genstudio_attempt_fences (
  genstudio_attempt_id TEXT PRIMARY KEY,
  genstudio_job_id TEXT NOT NULL,
  highest_fencing_token INTEGER NOT NULL,
  idempotency_hash TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  lease_expires_at REAL,
  updated_at REAL NOT NULL
);
"""


class ExecutionIdentityError(ValueError):
    """The upstream assignment is stale, incomplete, or not idempotent."""


@dataclass(frozen=True)
class PreparedExecution:
    envelope: dict
    evidence: dict | None
    replay_batch_id: str | None = None


def _conn() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_FILE, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.executescript(_SCHEMA)
    columns = {
        row["name"]
        for row in connection.execute(
            "PRAGMA table_info(genstudio_attempt_fences)"
        ).fetchall()
    }
    if "lease_expires_at" not in columns:
        connection.execute(
            "ALTER TABLE genstudio_attempt_fences ADD COLUMN lease_expires_at REAL"
        )
    return connection


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _clean_id(name: str, value: object) -> str:
    text = str(value or "").strip()
    if not _ID.fullmatch(text):
        raise ExecutionIdentityError(
            f"{name} must be 1-200 safe letters, digits, or ._:-")
    return text


def _lease_timestamp(value: object) -> tuple[str, float]:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExecutionIdentityError(
            "lease_expires_at must be an ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None:
        raise ExecutionIdentityError("lease_expires_at must include a timezone")
    normalized = parsed.astimezone(UTC)
    return normalized.isoformat(), normalized.timestamp()


def _canonical_payload(envelope: dict, identity: dict) -> str:
    # Transport ownership may advance while the exact execution payload stays
    # the same. Exclude only transport identity; every dispatch-affecting field
    # remains part of the conflict fingerprint.
    payload = {
        key: value for key, value in envelope.items()
        if key not in _FIELDS
        and key not in {"clientRequestId", "genstudio_execution"}
    }
    payload["genstudio_job_id"] = identity["genstudio_job_id"]
    payload["genstudio_attempt_id"] = identity["genstudio_attempt_id"]
    payload["site_id"] = identity["site_id"]
    payload["operation"] = identity["operation"]
    payload["model_revision"] = identity.get("model_revision")
    payload["voice_revision"] = identity.get("voice_revision")
    try:
        return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ExecutionIdentityError("GenStudio execution payload must be valid JSON") from exc


def _extract(envelope: dict) -> dict | None:
    nested = envelope.get("genstudio_execution")
    supplied = {key: envelope.get(key) for key in _FIELDS if key in envelope}
    if isinstance(nested, dict):
        supplied = {**nested, **supplied}
    if not supplied:
        return None
    missing = [key for key in (
        "genstudio_job_id", "genstudio_attempt_id", "idempotency_key",
        "fencing_token", "site_id", "operation",
    ) if supplied.get(key) is None or str(supplied.get(key)).strip() == ""]
    if missing:
        raise ExecutionIdentityError(
            "GenStudio execution identity is incomplete: " + ", ".join(missing))

    from . import control_plane

    site_id = _clean_id("site_id", supplied["site_id"])
    configured_site = control_plane.load_settings()["site_id"]
    if site_id != configured_site:
        raise ExecutionIdentityError(
            f"GenStudio assigned site {site_id!r}, but this controller is {configured_site!r}")
    try:
        token = int(supplied["fencing_token"])
    except (TypeError, ValueError) as exc:
        raise ExecutionIdentityError("fencing_token must be a positive integer issued by GenStudio") from exc
    if isinstance(supplied["fencing_token"], bool) or token < 1:
        raise ExecutionIdentityError("fencing_token must be a positive integer issued by GenStudio")
    operation = str(supplied["operation"]).strip().lower()
    if not _OPERATION.fullmatch(operation):
        raise ExecutionIdentityError(
            "operation must start with a lowercase letter and use safe characters")
    lease_text = None
    if supplied.get("lease_expires_at") is not None:
        lease_text, _lease_epoch = _lease_timestamp(supplied["lease_expires_at"])
    return {
        "genstudio_job_id": _clean_id("genstudio_job_id", supplied["genstudio_job_id"]),
        "genstudio_attempt_id": _clean_id(
            "genstudio_attempt_id", supplied["genstudio_attempt_id"]),
        "idempotency_key": str(supplied["idempotency_key"]).strip(),
        "fencing_token": token,
        "site_id": site_id,
        "operation": operation,
        "model_revision": (
            str(supplied.get("model_revision")).strip()
            if supplied.get("model_revision") is not None else None),
        "voice_revision": (
            str(supplied.get("voice_revision")).strip()
            if supplied.get("voice_revision") is not None else None),
        "lease_expires_at": lease_text,
    }


def prepare(envelope: dict) -> PreparedExecution:
    """Validate and persist an optional upstream execution assignment.

    Envelopes without GenStudio identity retain the established local API and
    dispatch behavior byte-for-byte.
    """
    identity = _extract(envelope)
    if identity is None:
        return PreparedExecution(dict(envelope), None)
    if not identity["idempotency_key"] or len(identity["idempotency_key"]) > 512:
        raise ExecutionIdentityError("idempotency_key must be between 1 and 512 characters")

    idem_hash = _hash(identity.pop("idempotency_key"))
    payload_hash = _hash(_canonical_payload(envelope, identity))
    identity.update({
        "idempotency_hash": idem_hash,
        "payload_hash": payload_hash,
        "authority": "genstudio",
    })
    now = time.time()
    lease_epoch = None
    if identity.get("lease_expires_at"):
        _lease_text, lease_epoch = _lease_timestamp(identity["lease_expires_at"])
        if lease_epoch <= now:
            raise ExecutionIdentityError(
                "GenStudio execution lease has already expired"
            )
    replay_batch_id = None
    with _conn() as connection:
        connection.execute("BEGIN IMMEDIATE")
        fence = connection.execute(
            "SELECT * FROM genstudio_fences WHERE genstudio_job_id = ?",
            (identity["genstudio_job_id"],),
        ).fetchone()
        if fence is not None:
            highest = int(fence["highest_fencing_token"])
            if identity["fencing_token"] < highest:
                raise ExecutionIdentityError(
                    f"stale GenStudio fencing token {identity['fencing_token']}; "
                    f"this controller has already observed {highest}")
            if identity["fencing_token"] == highest and (
                fence["genstudio_attempt_id"] != identity["genstudio_attempt_id"]
                or fence["idempotency_hash"] != idem_hash
                or fence["payload_hash"] != payload_hash
            ):
                raise ExecutionIdentityError(
                    "fencing_token was already observed for a different GenStudio assignment")

        attempt_fence = connection.execute(
            "SELECT * FROM genstudio_attempt_fences WHERE genstudio_attempt_id = ?",
            (identity["genstudio_attempt_id"],),
        ).fetchone()
        if attempt_fence is not None:
            attempt_highest = int(attempt_fence["highest_fencing_token"])
            if identity["fencing_token"] < attempt_highest:
                raise ExecutionIdentityError(
                    f"stale GenStudio fencing token {identity['fencing_token']}; "
                    f"attempt {identity['genstudio_attempt_id']!r} has already observed "
                    f"{attempt_highest}")
            if (
                attempt_fence["genstudio_job_id"] != identity["genstudio_job_id"]
                or attempt_fence["idempotency_hash"] != idem_hash
                or attempt_fence["payload_hash"] != payload_hash
            ):
                raise ExecutionIdentityError(
                    "GenStudio attempt identity was already accepted for a different assignment")
            existing_lease = attempt_fence["lease_expires_at"]
            if existing_lease is not None and float(existing_lease) <= now:
                raise ExecutionIdentityError(
                    "GenStudio execution lease has expired and cannot be revived"
                )

        existing = connection.execute(
            "SELECT * FROM genstudio_idempotency "
            "WHERE genstudio_job_id = ? AND idempotency_hash = ?",
            (identity["genstudio_job_id"], idem_hash),
        ).fetchone()
        if existing is not None:
            if (existing["genstudio_attempt_id"] != identity["genstudio_attempt_id"]
                    or existing["payload_hash"] != payload_hash):
                raise ExecutionIdentityError(
                    "idempotency identity was already accepted for a different payload")
            replay_batch_id = existing["local_batch_id"]

        if fence is None or identity["fencing_token"] > int(fence["highest_fencing_token"]):
            connection.execute(
                "INSERT INTO genstudio_fences "
                "(genstudio_job_id, highest_fencing_token, genstudio_attempt_id, "
                " idempotency_hash, payload_hash, operation, updated_at) "
                "VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(genstudio_job_id) DO UPDATE SET "
                "highest_fencing_token=excluded.highest_fencing_token, "
                "genstudio_attempt_id=excluded.genstudio_attempt_id, "
                "idempotency_hash=excluded.idempotency_hash, "
                "payload_hash=excluded.payload_hash, operation=excluded.operation, "
                "updated_at=excluded.updated_at",
                (identity["genstudio_job_id"], identity["fencing_token"],
                 identity["genstudio_attempt_id"], idem_hash, payload_hash,
                 identity["operation"], now),
            )
        if (attempt_fence is None
                or identity["fencing_token"] > int(attempt_fence["highest_fencing_token"])):
            connection.execute(
                "INSERT INTO genstudio_attempt_fences "
                "(genstudio_attempt_id, genstudio_job_id, highest_fencing_token, "
                " idempotency_hash, payload_hash, lease_expires_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(genstudio_attempt_id) DO UPDATE SET "
                "highest_fencing_token=excluded.highest_fencing_token, "
                "lease_expires_at=excluded.lease_expires_at, "
                "updated_at=excluded.updated_at",
                (identity["genstudio_attempt_id"], identity["genstudio_job_id"],
                 identity["fencing_token"], idem_hash, payload_hash,
                 lease_epoch, now),
            )
        elif lease_epoch is not None:
            connection.execute(
                "UPDATE genstudio_attempt_fences "
                "SET lease_expires_at = ?, updated_at = ? "
                "WHERE genstudio_attempt_id = ?",
                (lease_epoch, now, identity["genstudio_attempt_id"]),
            )
        connection.execute(
            "INSERT INTO genstudio_idempotency "
            "(genstudio_job_id, idempotency_hash, genstudio_attempt_id, "
            " payload_hash, accepted_at, updated_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(genstudio_job_id,idempotency_hash) DO UPDATE SET "
            "updated_at=excluded.updated_at",
            (identity["genstudio_job_id"], idem_hash,
             identity["genstudio_attempt_id"], payload_hash, now, now),
        )

    normalized = dict(envelope)
    normalized.pop("genstudio_execution", None)
    for key in _FIELDS:
        normalized.pop(key, None)
    normalized["genstudio_execution"] = identity
    # A deterministic local broker identity prevents a caller-supplied
    # clientRequestId from bypassing GenStudio's accepted idempotency identity.
    normalized["clientRequestId"] = (
        "genstudio:" + _hash(
            f"{identity['genstudio_job_id']}:{identity['genstudio_attempt_id']}:{idem_hash}")[:40]
    )
    return PreparedExecution(normalized, identity, replay_batch_id)


def bind_local_batch(evidence: dict | None, batch_id: str) -> None:
    if not evidence:
        return
    with _conn() as connection:
        connection.execute(
            "UPDATE genstudio_idempotency SET local_batch_id = ?, updated_at = ? "
            "WHERE genstudio_job_id = ? AND idempotency_hash = ?",
            (batch_id, time.time(), evidence["genstudio_job_id"],
             evidence["idempotency_hash"]),
        )


def lease_expired(evidence: dict | None, *, now: float | None = None) -> bool:
    """Return whether a GenStudio-owned local execution lost its renewable lease."""
    if not evidence or not evidence.get("lease_expires_at"):
        return False
    try:
        _normalized, deadline = _lease_timestamp(evidence["lease_expires_at"])
    except ExecutionIdentityError:
        return True
    return deadline <= (time.time() if now is None else now)


def renew_lease(payload: dict) -> dict:
    """Renew one active attempt without allowing an expired fence to revive."""
    job_id = _clean_id("genstudio_job_id", payload.get("genstudio_job_id"))
    attempt_id = _clean_id(
        "genstudio_attempt_id", payload.get("genstudio_attempt_id")
    )
    try:
        token = int(payload.get("fencing_token"))
    except (TypeError, ValueError) as exc:
        raise ExecutionIdentityError(
            "fencing_token must be a positive integer issued by GenStudio"
        ) from exc
    if isinstance(payload.get("fencing_token"), bool) or token < 1:
        raise ExecutionIdentityError(
            "fencing_token must be a positive integer issued by GenStudio"
        )
    lease_text, lease_epoch = _lease_timestamp(payload.get("lease_expires_at"))
    now = time.time()
    if lease_epoch <= now:
        raise ExecutionIdentityError("renewed lease must expire in the future")

    with _conn() as connection:
        connection.execute("BEGIN IMMEDIATE")
        attempt = connection.execute(
            "SELECT * FROM genstudio_attempt_fences "
            "WHERE genstudio_attempt_id = ?",
            (attempt_id,),
        ).fetchone()
        fence = connection.execute(
            "SELECT * FROM genstudio_fences WHERE genstudio_job_id = ?",
            (job_id,),
        ).fetchone()
        if attempt is None or fence is None:
            raise ExecutionIdentityError("unknown GenStudio execution attempt")
        if (
            attempt["genstudio_job_id"] != job_id
            or int(attempt["highest_fencing_token"]) != token
            or fence["genstudio_attempt_id"] != attempt_id
            or int(fence["highest_fencing_token"]) != token
        ):
            raise ExecutionIdentityError(
                "GenStudio execution lease belongs to a stale fencing token"
            )
        current_deadline = attempt["lease_expires_at"]
        if current_deadline is None:
            raise ExecutionIdentityError(
                "GenStudio execution attempt has no renewable lease"
            )
        if float(current_deadline) <= now:
            raise ExecutionIdentityError(
                "GenStudio execution lease has expired and cannot be revived"
            )
        connection.execute(
            "UPDATE genstudio_attempt_fences "
            "SET lease_expires_at = ?, updated_at = ? "
            "WHERE genstudio_attempt_id = ?",
            (lease_epoch, now, attempt_id),
        )
        binding = connection.execute(
            "SELECT local_batch_id FROM genstudio_idempotency "
            "WHERE genstudio_attempt_id = ? ORDER BY accepted_at DESC LIMIT 1",
            (attempt_id,),
        ).fetchone()
    return {
        "genstudio_job_id": job_id,
        "genstudio_attempt_id": attempt_id,
        "fencing_token": token,
        "lease_expires_at": lease_text,
        "local_batch_id": binding["local_batch_id"] if binding else None,
    }


def reset_for_tests() -> None:
    DB_FILE.unlink(missing_ok=True)
