"""Durable, owner-controlled GenStudio model exposure policy.

Sibling Studios may publish audited *candidates*.  They cannot make themselves
routable to GenStudio.  Studio Hub pins an approval to the exact worker model
ID, operation, immutable runtime revision, and audited contract hash.  A new
revision or changed contract therefore returns to review automatically while
the historical approval remains available as evidence.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from .registry import DATA_DIR


STATE_FILE = DATA_DIR / "model_exposures.json"
SCHEMA_NAME = "studiohub.model-exposures"
SCHEMA_VERSION = 1
CANDIDATE_SCHEMA = "studio.model-audit"
CANDIDATE_SCHEMA_VERSION = 1

_IMMUTABLE_REVISION = re.compile(r"^(?:sha256:)?[0-9a-fA-F]{40,64}$")
_CONTRACT_HASH = re.compile(r"^sha256:[0-9a-fA-F]{64}$")
_PRIVATE_KEYS = {
    "token", "secret", "password", "credential", "authorization", "api_key",
    "filesystem_path", "cache_path", "artifact_path", "customer_content",
}
_PRIVATE_SUFFIXES = ("_token", "_secret", "_password", "_credential", "_path")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _read() -> dict[str, Any]:
    try:
        value = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        value = {}
    records = value.get("records") if isinstance(value, dict) else None
    return {
        "schema": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "records": records if isinstance(records, dict) else {},
        "global_catalog_revision": (
            value.get("global_catalog_revision")
            if isinstance(value, dict)
            and isinstance(value.get("global_catalog_revision"), str)
            else None
        ),
        "global_catalog_synced_at": (
            value.get("global_catalog_synced_at")
            if isinstance(value, dict)
            and isinstance(value.get("global_catalog_synced_at"), str)
            else None
        ),
    }


def _write(value: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_name(f".{STATE_FILE.name}.{os.getpid()}.tmp")
    try:
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, STATE_FILE)
        os.chmod(STATE_FILE, 0o600)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def _clean_text(value: object, limit: int = 500) -> str:
    return str(value or "").strip()[:limit]


def _safe_contract(value: object, *, depth: int = 0):
    """Bound and sanitize sibling-supplied contract metadata."""
    if depth > 5:
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:500]
    if isinstance(value, list):
        return [item for item in (
            _safe_contract(child, depth=depth + 1) for child in value[:100]
        ) if item is not None]
    if isinstance(value, dict):
        result = {}
        for raw_key, child in list(value.items())[:100]:
            key = _clean_text(raw_key, 120)
            lowered = key.lower()
            if (not key or lowered in _PRIVATE_KEYS
                    or lowered.endswith(_PRIVATE_SUFFIXES)):
                continue
            safe = _safe_contract(child, depth=depth + 1)
            if safe is not None:
                result[key] = safe
        return result
    return None


def candidate_summary(model: dict) -> dict[str, Any] | None:
    """Return a strictly validated sibling audit candidate summary.

    Invalid or incomplete evidence remains discoverable through the ordinary
    Hub model inventory, but cannot enter the exposure workflow.
    """
    value = model.get("genstudio_candidate")
    if not isinstance(value, dict):
        return None
    if value.get("schema") != CANDIDATE_SCHEMA:
        return None
    if (type(value.get("schema_version")) is not int
            or value.get("schema_version") != CANDIDATE_SCHEMA_VERSION):
        return None
    model_id = _clean_text(model.get("repo") or model.get("model_id"))
    revision = _clean_text(value.get("runtime_revision"), 80).lower()
    contract_hash = _clean_text(value.get("contract_hash"), 80).lower()
    operations = value.get("approved_operations")
    if not model_id or not _IMMUTABLE_REVISION.fullmatch(revision):
        return None
    if not _CONTRACT_HASH.fullmatch(contract_hash):
        return None
    if not isinstance(operations, list):
        return None
    approved_operations = sorted({
        _clean_text(operation, 120) for operation in operations
        if _clean_text(operation, 120)
    })
    if not approved_operations:
        return None
    raw_capacity = _safe_contract(value.get("capacity")) or {}
    capacity = {}
    for name in ("max_concurrency", "available_slots"):
        try:
            capacity[name] = max(0, min(1_000, int(raw_capacity.get(name))))
        except (TypeError, ValueError):
            continue
    return {
        "schema": CANDIDATE_SCHEMA,
        "schema_version": CANDIDATE_SCHEMA_VERSION,
        "internal_model_id": model_id,
        "display_name": _clean_text(model.get("label") or model_id),
        "audit_id": _clean_text(value.get("audit_id"), 200),
        "audit_status": _clean_text(value.get("audit_status"), 40).lower(),
        "candidate_for_genstudio": value.get("candidate_for_genstudio") is True,
        "contract_hash": contract_hash,
        "runtime_revision": revision,
        "approved_operations": approved_operations,
        "audited_at": _clean_text(value.get("audited_at"), 80) or None,
        "adapter": _safe_contract(value.get("adapter")) or {},
        "controls": _safe_contract(value.get("controls")) or {},
        "input_limits": _safe_contract(value.get("input_limits")) or {},
        "output_limits": _safe_contract(value.get("output_limits")) or {},
        "capacity": capacity,
        "hardware": _safe_contract(value.get("hardware")) or {},
    }


def normalized_immutable_revision(value: object) -> str | None:
    """Return a canonical hash-shaped runtime revision, or ``None``."""
    revision = _clean_text(value, 80)
    if not _IMMUTABLE_REVISION.fullmatch(revision):
        return None
    return revision.removeprefix("sha256:").lower()


def is_immutable_revision(value: object) -> bool:
    """Return whether ``value`` is a content-addressed runtime revision."""
    return normalized_immutable_revision(value) is not None


def is_contract_hash(value: object) -> bool:
    """Return whether ``value`` is the exact SHA-256 contract fingerprint."""
    return bool(_CONTRACT_HASH.fullmatch(_clean_text(value, 80).lower()))


def validated_execution_binding(operation: object, model_revision: object,
                                contract_hash: object) -> tuple[str, str] | None:
    """Validate the exact immutable binding used by GenStudio assignments."""
    normalized_operation = _clean_text(operation, 120).lower()
    revision = normalized_immutable_revision(model_revision)
    contract = _clean_text(contract_hash, 80).lower()
    if (normalized_operation != "audio.transcription" or revision is None
            or not is_contract_hash(contract)):
        return None
    return revision, contract


def execution_candidate(model: dict, operation: str) -> dict[str, Any] | None:
    """Return a passed, explicitly approved sibling candidate for execution."""
    if model.get("cached") is not True:
        return None
    candidate = candidate_summary(model)
    if candidate is None:
        return None
    if (candidate.get("audit_status") != "passed"
            or candidate.get("candidate_for_genstudio") is not True
            or operation not in candidate.get("approved_operations", [])):
        return None
    return candidate


def exposure_key(model_id: str, operation: str, revision: str,
                 contract_hash: str) -> str:
    identity = "\n".join((model_id, operation, revision, contract_hash))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def candidate_key(candidate: dict, operation: str) -> str:
    return exposure_key(
        candidate["internal_model_id"], operation,
        candidate["runtime_revision"], candidate["contract_hash"],
    )


def records() -> list[dict[str, Any]]:
    return sorted(
        (dict(row, exposure_key=key) for key, row in _read()["records"].items()
         if isinstance(row, dict)),
        key=lambda row: (row.get("internal_model_id", ""),
                         row.get("operation", ""), row.get("approved_at", "")),
    )


def global_authority_active() -> bool:
    return bool(_read().get("global_catalog_revision"))


def sync_global_catalog(models: list[dict[str, Any]], *, revision: str) -> None:
    """Replace site-local exposure authority with GenStudio desired state.

    Cached files and historical records are preserved.  Omitting an exact
    contract stops new publication by revoking it; it never deletes a model.
    """

    state = _read()
    desired: set[str] = set()
    for model in models:
        key = _clean_text(model.get("candidate_key"), 64).lower()
        model_id = _clean_text(
            model.get("internal_model_id") or model.get("repo"), 500
        )
        expected = exposure_key(
            model_id,
            _clean_text(model.get("operation"), 120),
            _clean_text(model.get("runtime_revision"), 80).lower(),
            _clean_text(model.get("contract_hash"), 80).lower(),
        )
        if key != expected:
            raise ValueError("GenStudio fleet catalog contains an invalid exact model key.")
        desired.add(key)
        previous = state["records"].get(key)
        approved_at = (
            previous.get("approved_at") if isinstance(previous, dict) else None
        ) or _now()
        state["records"][key] = {
            "state": "approved",
            "authority": "genstudio",
            "authority_revision": revision,
            "internal_model_id": model_id,
            "display_name": _clean_text(
                model.get("display_name") or model.get("label"), 256
            ),
            "operation": _clean_text(model.get("operation"), 120),
            "runtime_revision": _clean_text(model.get("runtime_revision"), 80).lower(),
            "contract_hash": _clean_text(model.get("contract_hash"), 80).lower(),
            "approved_at": approved_at,
            "updated_at": _now(),
            "reason": "Approved in GenStudio fleet catalog",
            "revoked_at": None,
        }
    for key, previous in list(state["records"].items()):
        if not isinstance(previous, dict) or key in desired:
            continue
        if previous.get("state") == "approved":
            state["records"][key] = {
                **previous,
                "state": "revoked",
                "authority": previous.get("authority") or "legacy_site",
                "updated_at": _now(),
                "revoked_at": _now(),
                "reason": "Not present in the current GenStudio fleet catalog",
            }
    state["global_catalog_revision"] = revision
    state["global_catalog_synced_at"] = _now()
    _write(state)


def state_for(candidate: dict | None, operation: str) -> dict[str, Any]:
    if candidate is None:
        return {"state": "unverified", "reason": "missing_or_invalid_audit"}
    if candidate.get("audit_status") != "passed":
        return {"state": "blocked", "reason": "audit_not_passed"}
    if not candidate.get("candidate_for_genstudio"):
        return {"state": "blocked", "reason": "sibling_candidate_not_approved"}
    if operation not in candidate.get("approved_operations", []):
        return {"state": "blocked", "reason": "operation_not_audited"}

    state = _read()["records"]
    key = candidate_key(candidate, operation)
    exact = state.get(key)
    if isinstance(exact, dict):
        return {
            "state": exact.get("state", "unexposed"),
            "reason": exact.get("reason"),
            "exposure_key": key,
            "approved_at": exact.get("approved_at"),
            "revoked_at": exact.get("revoked_at"),
        }

    prior = next((row for row in state.values() if isinstance(row, dict)
                  and row.get("internal_model_id") == candidate["internal_model_id"]
                  and row.get("operation") == operation
                  and row.get("state") == "approved"), None)
    if prior:
        return {
            "state": "suspended",
            "reason": "runtime_revision_or_contract_changed",
        }
    return {"state": "candidate", "reason": "awaiting_hub_approval"}


def is_approved(candidate: dict | None, operation: str) -> bool:
    return state_for(candidate, operation).get("state") == "approved"


def approve(candidate: dict, operation: str, *, reason: str | None = None) -> dict:
    status = state_for(candidate, operation)
    if status["state"] not in {"candidate", "approved", "revoked", "suspended"}:
        raise ValueError(status.get("reason") or "This model cannot be exposed.")
    key = candidate_key(candidate, operation)
    state = _read()
    previous = state["records"].get(key)
    approved_at = (
        previous.get("approved_at") if isinstance(previous, dict)
        else None
    ) or _now()
    state["records"][key] = {
        "state": "approved",
        "internal_model_id": candidate["internal_model_id"],
        "display_name": candidate.get("display_name"),
        "operation": operation,
        "runtime_revision": candidate["runtime_revision"],
        "contract_hash": candidate["contract_hash"],
        "audit_id": candidate.get("audit_id"),
        "approved_at": approved_at,
        "updated_at": _now(),
        "reason": _clean_text(reason, 500) or "Owner approved in Studio Hub",
        "revoked_at": None,
    }
    _write(state)
    return {**state["records"][key], "exposure_key": key}


def revoke(candidate: dict, operation: str, *, reason: str | None = None) -> dict:
    key = candidate_key(candidate, operation)
    state = _read()
    previous = state["records"].get(key)
    if not isinstance(previous, dict):
        raise ValueError("This exact model exposure has not been approved.")
    state["records"][key] = {
        **previous,
        "state": "revoked",
        "updated_at": _now(),
        "revoked_at": _now(),
        "reason": _clean_text(reason, 500) or "Owner revoked in Studio Hub",
    }
    _write(state)
    return {**state["records"][key], "exposure_key": key}


def revoke_key(key: str, *, reason: str | None = None) -> dict:
    """Revoke a historical exact exposure even if its worker disappeared.

    The last-good sibling inventory is evidence, not the authority for an owner
    safety action.  Keeping revocation addressable by its immutable exposure key
    lets the controller stop new publication during an outage or after a model
    has been removed from the sibling catalogue.
    """
    state = _read()
    previous = state["records"].get(key)
    if not isinstance(previous, dict):
        raise ValueError("This exact model exposure has not been approved.")
    state["records"][key] = {
        **previous,
        "state": "revoked",
        "updated_at": _now(),
        "revoked_at": _now(),
        "reason": _clean_text(reason, 500) or "Owner revoked in Studio Hub",
    }
    _write(state)
    return {**state["records"][key], "exposure_key": key}


def reset_for_tests() -> None:
    with contextlib.suppress(FileNotFoundError):
        STATE_FILE.unlink()
