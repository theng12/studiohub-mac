"""Durable release intent, fenced serial execution, and restart recovery.

The service owns controller/agent execution and persistence. External API,
authorization, and application-lifespan wiring intentionally remain separate.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import tempfile
import threading
import time
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

import httpx

from . import peers, registry
from .fleet_auto_updates import run_managed_components


STATE_FILE = registry.DATA_DIR / "release_reconciliation.json"
LOCK_FILE = registry.DATA_DIR / "release_reconciliation.json.lock"
DUE_SCAN_INTERVAL_SECONDS = 15 * 60
RETRY_DELAYS = (60, 300, 900, 3600, 14_400, 86_400)
ADOPTION_LEASE_SECONDS = 5 * 60

_SCHEMA = "genstudio.studio-fleet-release-intent"
_REPOSITORIES = {
    "hub": "theng12/studiohub-mac",
    "image": "theng12/imagestudio-mac",
    "voice": "theng12/voicestudio-mac",
}
_TOP_LEVEL_FIELDS = {
    "schema", "schema_version", "release_id", "sequence", "created_at", "components",
}
_SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_RELEASE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9._:@-]{1,200}$")
_OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

SITE_STATES = {
    "pending", "queued", "running", "waiting_busy", "degraded",
    "blocked_release", "complete",
}
COMPONENT_STATES = {
    "not_installed", "pending_offline", "pending_busy", "checking", "updating",
    "restarting", "verifying", "current", "retryable_failure", "auth_blocked",
    "release_blocked", "excluded_disabled",
}
TERMINAL_SITE_STATES = {"complete", "blocked_release"}
RETRYABLE_COMPONENT_STATES = {
    "pending_offline", "pending_busy", "retryable_failure", "auth_blocked",
}
_HOST_STATUSES = {
    "connected", "no_hub", "unreachable", "no_token", "token_rejected", "unknown",
}
_ERROR_DETAILS = {
    "offline": "managed target is currently offline",
    "busy": "managed target is busy and will be retried",
    "auth_rejected": "managed update authentication was rejected",
    "updater_unavailable": "exact managed updater is unavailable",
    "transport_unavailable": "managed update transport is unavailable",
    "health_mismatch": "exact managed health attestation did not match",
    "update_refused": "managed updater refused the exact target",
    "invalid_evidence": "managed updater returned invalid evidence",
    "unknown_failure": "managed update failed; inspect local logs",
    "clean_checkout_health_failure": "clean checkout health verification failed",
    "identity_mismatch": "managed agent identity did not match enrollment",
    "manifest_mismatch": "managed release manifest identity did not match",
    "sha_mismatch": "managed release commit identity did not match",
}
_DEFAULT_ERROR_CODES = {
    "pending_offline": "offline",
    "pending_busy": "busy",
    "retryable_failure": "unknown_failure",
    "auth_blocked": "auth_rejected",
    "release_blocked": "health_mismatch",
}

_PATH_LOCKS: dict[str, threading.RLock] = {}
_PATH_OWNERS: dict[str, str] = {}
_PATH_LOCKS_GUARD = threading.Lock()


class LeaseLostError(RuntimeError):
    """Raised when an executor no longer owns its durable fencing generation."""


def retry_delay(attempt: int) -> int:
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise ValueError("attempt must be a positive integer")
    return RETRY_DELAYS[min(attempt, len(RETRY_DELAYS)) - 1]


def _canonical_manifest(manifest: dict[str, Any]) -> bytes:
    unsigned = {key: value for key, value in manifest.items() if key != "release_id"}
    return json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _validate_manifest(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("release manifest must be a JSON object")
    manifest = deepcopy(value)
    if set(manifest) != _TOP_LEVEL_FIELDS:
        raise ValueError("release manifest fields are invalid")
    if manifest.get("schema") != _SCHEMA or manifest.get("schema_version") != 1:
        raise ValueError("release manifest schema is invalid")
    sequence = manifest.get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise ValueError("release sequence must be a positive integer")
    created_at = manifest.get("created_at")
    if not isinstance(created_at, str) or not _TIMESTAMP_RE.fullmatch(created_at):
        raise ValueError("release created_at must be an RFC3339 UTC timestamp")
    try:
        datetime.fromisoformat(created_at[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("release created_at must be an RFC3339 UTC timestamp") from exc
    components = manifest.get("components")
    if not isinstance(components, dict) or set(components) != set(_REPOSITORIES):
        raise ValueError("release components must be exactly hub, image, and voice")
    for name, repository in _REPOSITORIES.items():
        component = components.get(name)
        if not isinstance(component, dict):
            raise ValueError(f"release {name} component must be an object")
        required = {"repository", "version", "commit"}
        if name != "hub":
            required.add("installed_only")
        if component.get("repository") != repository:
            raise ValueError(f"release {name} repository is invalid")
        if not isinstance(component.get("version"), str) or not _SEMVER_RE.fullmatch(component["version"]):
            raise ValueError(f"release {name} version is invalid")
        if not isinstance(component.get("commit"), str) or not _COMMIT_RE.fullmatch(component["commit"]):
            raise ValueError(f"release {name} commit is invalid")
        if name != "hub" and component.get("installed_only") is not True:
            raise ValueError(f"release {name} installed_only must be true")
        if set(component) != required:
            raise ValueError(f"release {name} component fields are invalid")
    release_id = manifest.get("release_id")
    expected = "sha256:" + hashlib.sha256(_canonical_manifest(manifest)).hexdigest()
    if not isinstance(release_id, str) or not _RELEASE_ID_RE.fullmatch(release_id):
        raise ValueError("release_id must be a canonical SHA-256 identifier")
    if release_id != expected:
        raise ValueError("release_id does not match canonical manifest content")
    return manifest


def _identifier(value: object, label: str, *, opaque: bool = False) -> str:
    pattern = _OPAQUE_ID_RE if opaque else _IDENTIFIER_RE
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"{label} is invalid")
    return value


def _finite_time(value: object, label: str, *, allow_none: bool = False,
                 minimum: float = 0.0) -> float | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite numeric timestamp")
    number = float(value)
    if not math.isfinite(number) or number < minimum:
        raise ValueError(f"{label} must be a finite numeric timestamp not before {minimum}")
    return number


def _host_evidence(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    evidence: dict[str, Any] = {}
    for key in ("reachable", "auth"):
        if isinstance(value.get(key), bool):
            evidence[key] = value[key]
    if value.get("status") in _HOST_STATUSES:
        evidence["status"] = value["status"]
    return evidence


def _path_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _PATH_LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.RLock())


def _path_key(path: Path) -> str:
    return str(path.resolve())


def _pid_alive(pid: int) -> bool:
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value}")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory = os.open(path.parent, flags)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _job_id(release_id: str, supplement_generation: int = 0) -> str:
    base = "release-" + hashlib.sha256(release_id.encode("utf-8")).hexdigest()[:24]
    return base if supplement_generation == 0 else f"{base}-s{supplement_generation}"


def _operation_id(release_id: str, machine: str, supplement_generation: int = 0) -> str:
    seed = (
        f"{release_id}\0{machine}\0managed-update\0{supplement_generation}"
    ).encode("utf-8")
    return "managed-" + hashlib.sha256(seed).hexdigest()


def _agent_job_id(operation_id: str) -> str:
    return "agent-" + hashlib.sha256(operation_id.encode("utf-8")).hexdigest()[:24]


def _catalog_operation_id(release_id: str, supplement_generation: int = 0) -> str:
    return "catalog-" + hashlib.sha256(
        f"{release_id}\0catalog-reconcile\0{supplement_generation}".encode("utf-8")
    ).hexdigest()[:24]


def _validate_managed_bundle(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"release_id", "operation_id", "components"}:
        raise ValueError("managed update bundle fields are invalid")
    bundle = deepcopy(value)
    release_id = bundle["release_id"]
    if not isinstance(release_id, str) or not _RELEASE_ID_RE.fullmatch(release_id):
        raise ValueError("managed update release_id is invalid")
    _identifier(bundle["operation_id"], "managed operation", opaque=True)
    components = bundle["components"]
    if not isinstance(components, dict) or set(components) != set(_REPOSITORIES):
        raise ValueError("managed update components are invalid")
    for name, repository in _REPOSITORIES.items():
        component = components[name]
        fields = {"repository", "version", "commit"}
        if name != "hub":
            fields.add("installed_only")
        if not isinstance(component, dict) or set(component) != fields:
            raise ValueError(f"managed {name} target fields are invalid")
        if component["repository"] != repository:
            raise ValueError(f"managed {name} repository is invalid")
        if not isinstance(component["version"], str) or not _SEMVER_RE.fullmatch(component["version"]):
            raise ValueError(f"managed {name} version is invalid")
        if not isinstance(component["commit"], str) or not _COMMIT_RE.fullmatch(component["commit"]):
            raise ValueError(f"managed {name} commit is invalid")
        if name != "hub" and component["installed_only"] is not True:
            raise ValueError(f"managed {name} installed_only must be true")
    return bundle


def _validate_agent_state(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"schema_version", "jobs"}:
        raise ValueError("managed child state fields are invalid")
    if value["schema_version"] != 1 or not isinstance(value["jobs"], dict):
        raise ValueError("managed child state is invalid")
    for job_id, job in value["jobs"].items():
        fields = {"id", "operation_id", "bundle", "state", "created_at", "started_at",
                  "finished_at", "components", "execution_lease", "lease_generation"}
        if not isinstance(job, dict) or set(job) != fields or job_id != job["id"]:
            raise ValueError("managed child job fields are invalid")
        bundle = _validate_managed_bundle(job["bundle"])
        if job_id != _agent_job_id(bundle["operation_id"]) or job["operation_id"] != bundle["operation_id"]:
            raise ValueError("managed child identity is invalid")
        if job["state"] not in {"queued", "running", "degraded", "blocked_release", "complete"}:
            raise ValueError("managed child job state is invalid")
        created = _finite_time(job["created_at"], "managed child created_at")
        generation = job["lease_generation"]
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise ValueError("managed child lease generation is invalid")
        started = _finite_time(job["started_at"], "managed child started_at", allow_none=True)
        finished = _finite_time(job["finished_at"], "managed child finished_at", allow_none=True)
        if started is not None and started < created:
            raise ValueError("managed child start time is invalid")
        _validate_adoption_lease(job["execution_lease"], created_at=created)
        if (job["execution_lease"] is not None
                and job["execution_lease"]["generation"] != generation):
            raise ValueError("managed child lease generation is inconsistent")
        if job["execution_lease"] is not None and started is None:
            raise ValueError("leased managed child lacks started_at")
        if job["state"] in {"complete", "blocked_release"}:
            if finished is None:
                raise ValueError("terminal managed child lacks finished_at")
            if job["execution_lease"] is not None:
                raise ValueError("terminal managed child retains execution lease")
        elif finished is not None:
            raise ValueError("nonterminal managed child has finished_at")
        if not isinstance(job["components"], dict) or set(job["components"]) != set(_REPOSITORIES):
            raise ValueError("managed child components are invalid")
        for name, row in job["components"].items():
            row_fields = {"component", "installed", "state", "observed_version",
                          "observed_commit", "observed_release_id", "error_code"}
            if not isinstance(row, dict) or set(row) != row_fields or row["component"] != name:
                raise ValueError("managed child component row is invalid")
            if not isinstance(row["installed"], bool) or row["state"] not in COMPONENT_STATES:
                raise ValueError("managed child component evidence is invalid")
            if not row["installed"] and row["state"] != "not_installed":
                raise ValueError("uninstalled managed child component state is invalid")
            if row["installed"] and row["state"] == "not_installed":
                raise ValueError("installed managed child component is not_installed")
            if row["error_code"] is not None and row["error_code"] not in _ERROR_DETAILS:
                raise ValueError("managed child error code is invalid")
            if row["state"] in RETRYABLE_COMPONENT_STATES | {"release_blocked"}:
                if row["error_code"] is None:
                    raise ValueError("managed child error state lacks an error code")
            elif row["error_code"] is not None:
                raise ValueError("managed child non-error state has an error code")
            observed_version = row["observed_version"]
            observed_commit = row["observed_commit"]
            observed_release = row["observed_release_id"]
            if observed_version is not None and (
                not isinstance(observed_version, str) or not _SEMVER_RE.fullmatch(observed_version)
            ):
                raise ValueError("managed child observed version is invalid")
            if observed_commit is not None and (
                not isinstance(observed_commit, str) or not _COMMIT_RE.fullmatch(observed_commit)
            ):
                raise ValueError("managed child observed commit is invalid")
            if observed_release is not None and (
                not isinstance(observed_release, str)
                or not _RELEASE_ID_RE.fullmatch(observed_release)
            ):
                raise ValueError("managed child observed release is invalid")
            target = bundle["components"][name]
            if row["state"] == "current" and (
                observed_version != target["version"]
                or observed_commit != target["commit"]
                or observed_release is not None
            ):
                raise ValueError("managed child current attestation is invalid")
            if row["state"] == "release_blocked":
                validated_manifest = (
                    observed_release is not None
                    and observed_release != bundle["release_id"]
                )
                validated_tuple = (
                    observed_version is not None
                    and observed_commit is not None
                    and (
                        observed_version != target["version"]
                        or observed_commit != target["commit"]
                    )
                )
                if not validated_manifest and not validated_tuple:
                    raise ValueError("managed child block evidence is invalid")
            elif observed_release is not None:
                raise ValueError("managed child non-block state has release evidence")
            if row["state"] == "current":
                target = bundle["components"][name]
                if (row["observed_version"] != target["version"]
                        or row["observed_commit"] != target["commit"]):
                    raise ValueError("managed child current evidence is not exact")
            elif row["state"] != "release_blocked" and (
                row["observed_version"] is not None or row["observed_commit"] is not None
            ):
                raise ValueError("noncurrent managed child has observed evidence")
        rows = list(job["components"].values())
        if job["state"] == "complete" and not all(
            (not row["installed"] and row["state"] == "not_installed")
            or (row["installed"] and row["state"] == "current")
            for row in rows
        ):
            raise ValueError("complete managed child is not exactly converged")
        if job["state"] == "blocked_release" and not any(
            row["state"] == "release_blocked" for row in rows
        ):
            raise ValueError("blocked managed child lacks release-blocked evidence")
        if job["state"] == "degraded" and not any(
            row["state"] in RETRYABLE_COMPONENT_STATES for row in rows
        ):
            raise ValueError("degraded managed child lacks retryable evidence")
        if job["state"] == "queued" and started is not None:
            raise ValueError("queued managed child has started_at")
        if job["state"] != "queued" and started is None:
            raise ValueError("started managed child lacks started_at")
    return value


def _result_error_code(result: dict[str, Any], state: str) -> str | None:
    code = result.get("error_code")
    if code in _ERROR_DETAILS:
        return code
    detail = str(result.get("detail") or "").lower()
    if "clean checkout health" in detail:
        return "clean_checkout_health_failure"
    if "manifest" in detail and ("mismatch" in detail or "invalid" in detail):
        return "manifest_mismatch"
    if "attestation mismatch" in detail or "commit mismatch" in detail:
        return "sha_mismatch"
    if "authentication" in detail or "rejected the fleet token" in detail:
        return "auth_rejected"
    if "unavailable" in detail or "managed_exact_commit" in detail:
        return "updater_unavailable"
    return _DEFAULT_ERROR_CODES.get(state)


def _result_state(result: dict[str, Any]) -> str:
    state = result.get("state") or result.get("status")
    if state in {"complete", "succeeded"}:
        return "current"
    if state == "failed":
        return "retryable_failure"
    return state if state in COMPONENT_STATES else "retryable_failure"


def _row_is_current(row: dict[str, Any]) -> bool:
    return (
        row["state"] == "current"
        and row["observed_version"] == row["expected_version"]
        and row["observed_commit"] == row["expected_commit"]
    )


def _job_is_converged(job: dict[str, Any]) -> bool:
    return all(
        (not row["installed"] and row["state"] == "not_installed")
        or row["state"] == "excluded_disabled"
        or (row["installed"] and _row_is_current(row))
        for machine in job["machines"].values()
        for row in machine["components"].values()
    )


def _machine_summary(machine: dict[str, Any]) -> str:
    rows = list(machine["components"].values())
    if any(row["state"] in RETRYABLE_COMPONENT_STATES | {"release_blocked"}
           for row in rows):
        return "degraded"
    if all((not row["installed"] and row["state"] == "not_installed")
           or row["state"] == "excluded_disabled"
           or (row["installed"] and _row_is_current(row)) for row in rows):
        if any(row["state"] == "excluded_disabled" for row in rows):
            return "excluded"
        return "current"
    return "running"


def _refresh_job(job: dict[str, Any]) -> None:
    pending = []
    for machine in job["machines"].values():
        rows = list(machine["components"].values())
        machine_pending = [row for row in rows if row["state"] in RETRYABLE_COMPONENT_STATES]
        pending.extend(machine_pending)
        machine["state"] = _machine_summary(machine)
    retry_times = [row["next_retry"] for row in pending]
    if job["catalog"]["state"] == "retryable_failure":
        retry_times.append(job["catalog"]["next_retry"])
    if retry_times:
        job.update(
            state="degraded",
            next_retry=min(retry_times),
            finished_at=None,
        )
    elif job["state"] not in TERMINAL_SITE_STATES:
        job.update(state="running", next_retry=None, finished_at=None)


def _validate_component(row: object, *, installed_target: dict[str, Any] | None = None) -> None:
    fields = {
        "installed", "expected_version", "expected_commit", "observed_version",
        "observed_commit", "state", "attempt", "error_code", "detail", "next_retry",
    }
    if not isinstance(row, dict) or set(row) != fields:
        raise ValueError("component row fields are invalid")
    if not isinstance(row["installed"], bool):
        raise ValueError("component installed flag is invalid")
    if not isinstance(row["expected_version"], str) or not _SEMVER_RE.fullmatch(row["expected_version"]):
        raise ValueError("component expected version is invalid")
    if not isinstance(row["expected_commit"], str) or not _COMMIT_RE.fullmatch(row["expected_commit"]):
        raise ValueError("component expected commit is invalid")
    if installed_target and (
        row["expected_version"] != installed_target["version"]
        or row["expected_commit"] != installed_target["commit"]
    ):
        raise ValueError("component expected target differs from desired release")
    observed_version = row["observed_version"]
    observed_commit = row["observed_commit"]
    if observed_version is not None and (
        not isinstance(observed_version, str) or not _SEMVER_RE.fullmatch(observed_version)
    ):
        raise ValueError("component observed version is invalid")
    if observed_commit is not None and (
        not isinstance(observed_commit, str) or not _COMMIT_RE.fullmatch(observed_commit)
    ):
        raise ValueError("component observed commit is invalid")
    if row["state"] not in COMPONENT_STATES:
        raise ValueError("component state is invalid")
    if not row["installed"] and row["state"] != "not_installed":
        raise ValueError("uninstalled component state is invalid")
    if row["installed"] and row["state"] == "not_installed":
        raise ValueError("installed component cannot be not_installed")
    if row["state"] == "current" and not _row_is_current(row):
        raise ValueError("current component lacks exact observed attestation")
    if isinstance(row["attempt"], bool) or not isinstance(row["attempt"], int) or row["attempt"] < 0:
        raise ValueError("component attempt is invalid")
    code = row["error_code"]
    if code is not None and code not in _ERROR_DETAILS:
        raise ValueError("component error code is invalid")
    expected_detail = _ERROR_DETAILS.get(code) if code else None
    if row["detail"] != expected_detail:
        raise ValueError("component error detail is invalid")
    retry_at = _finite_time(row["next_retry"], "component next_retry", allow_none=True)
    if row["state"] in RETRYABLE_COMPONENT_STATES:
        if retry_at is None or code is None:
            raise ValueError("retryable component lacks retry evidence")
    elif retry_at is not None:
        raise ValueError("non-retryable component has retry time")
    if row["state"] not in RETRYABLE_COMPONENT_STATES | {"release_blocked"} and code is not None:
        raise ValueError("non-error component has an error code")


def _validate_adoption_lease(value: object, *, created_at: float) -> None:
    if value is None:
        return
    fields = {"owner_id", "pid", "generation", "acquired_at", "heartbeat_at", "expires_at"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("adoption lease fields are invalid")
    _identifier(value["owner_id"], "adoption owner", opaque=True)
    pid = value["pid"]
    if isinstance(pid, bool) or not isinstance(pid, int) or not 1 <= pid <= 2**31 - 1:
        raise ValueError("adoption owner pid is invalid")
    generation = value["generation"]
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise ValueError("adoption lease generation is invalid")
    acquired = _finite_time(value["acquired_at"], "adoption acquired_at", minimum=created_at)
    heartbeat = _finite_time(value["heartbeat_at"], "adoption heartbeat_at", minimum=acquired)
    expires = _finite_time(value["expires_at"], "adoption expires_at", minimum=heartbeat)
    if expires <= heartbeat:
        raise ValueError("adoption lease expiry is invalid")


def _validate_state(value: object) -> dict[str, Any]:
    try:
        if not isinstance(value, dict) or set(value) != {"schema_version", "desired", "activation", "jobs"}:
            raise ValueError("top-level state fields are invalid")
        if value["schema_version"] != 1:
            raise ValueError("state schema version is invalid")
        desired = value["desired"]
        manifest = None
        if desired is not None:
            if not isinstance(desired, dict) or set(desired) != {"manifest", "received_at"}:
                raise ValueError("desired state fields are invalid")
            manifest = _validate_manifest(desired["manifest"])
            _finite_time(desired["received_at"], "desired received_at")
        jobs = value["jobs"]
        if not isinstance(jobs, dict):
            raise ValueError("jobs must be an object")
        leased_job_ids = []
        for key, job in jobs.items():
            job_fields = {
                "id", "release_id", "genstudio_run_reference", "state", "created_at",
                "started_at", "finished_at", "next_retry", "machines", "catalog",
                "adoption_lease", "lease_generation", "clean_failure_machines",
                "job_generation", "supplement_generation", "supersedes_job_id",
            }
            if not isinstance(job, dict) or set(job) != job_fields:
                raise ValueError("release job fields are invalid")
            if not isinstance(job["release_id"], str) or not _RELEASE_ID_RE.fullmatch(job["release_id"]):
                raise ValueError("job release_id is invalid")
            supplement_generation = job["supplement_generation"]
            if (
                isinstance(supplement_generation, bool)
                or not isinstance(supplement_generation, int)
                or supplement_generation < 0
            ):
                raise ValueError("job supplement generation is invalid")
            job_generation = job["job_generation"]
            if (
                isinstance(job_generation, bool)
                or not isinstance(job_generation, int)
                or not 0 <= job_generation <= supplement_generation
            ):
                raise ValueError("release job generation is invalid")
            if key != job["id"] or job["id"] != _job_id(
                job["release_id"], job_generation,
            ):
                raise ValueError("job identity is not deterministic")
            supersedes = job["supersedes_job_id"]
            if job_generation == 0:
                if supersedes is not None:
                    raise ValueError("base release job cannot supersede another job")
            elif supersedes != _job_id(job["release_id"], job_generation - 1):
                raise ValueError("supplemental release job predecessor is invalid")
            reference = job["genstudio_run_reference"]
            if reference is not None:
                _identifier(reference, "GenStudio run reference", opaque=True)
            if job["state"] not in SITE_STATES:
                raise ValueError("job state is invalid")
            created = _finite_time(job["created_at"], "job created_at")
            generation = job["lease_generation"]
            if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
                raise ValueError("job lease generation is invalid")
            started = _finite_time(job["started_at"], "job started_at", allow_none=True)
            finished = _finite_time(job["finished_at"], "job finished_at", allow_none=True)
            retry_at = _finite_time(job["next_retry"], "job next_retry", allow_none=True)
            _validate_adoption_lease(job["adoption_lease"], created_at=created)
            if job["adoption_lease"] is not None:
                if job["adoption_lease"]["generation"] != generation:
                    raise ValueError("job lease generation is inconsistent")
                leased_job_ids.append(job["id"])
                if started is None:
                    raise ValueError("adopted job lacks started_at")
            if not isinstance(job["machines"], dict) or not job["machines"]:
                raise ValueError("job machines are invalid")
            current_target = manifest if manifest and job["release_id"] == manifest["release_id"] else None
            for machine_id, machine in job["machines"].items():
                _identifier(machine_id, "stable machine id")
                machine_fields = {
                    "id", "host_evidence", "agent_job_id", "operation_id", "state", "components",
                    "operation_generation",
                }
                if not isinstance(machine, dict) or set(machine) != machine_fields or machine["id"] != machine_id:
                    raise ValueError("machine row fields are invalid")
                operation_generation = machine["operation_generation"]
                if (
                    isinstance(operation_generation, bool)
                    or not isinstance(operation_generation, int)
                    or not 0 <= operation_generation <= supplement_generation
                ):
                    raise ValueError("machine operation generation is invalid")
                if machine["operation_id"] != _operation_id(
                    job["release_id"], machine_id, operation_generation,
                ):
                    raise ValueError("machine operation identity is not deterministic")
                if machine["agent_job_id"] is not None:
                    _identifier(machine["agent_job_id"], "agent_job_id", opaque=True)
                if machine["state"] not in {
                    "queued", "running", "degraded", "current", "excluded",
                }:
                    raise ValueError("machine state is invalid")
                if machine["host_evidence"] != _host_evidence(machine["host_evidence"]):
                    raise ValueError("host evidence is invalid")
                if not isinstance(machine["components"], dict) or set(machine["components"]) != set(_REPOSITORIES):
                    raise ValueError("machine components are invalid")
                for component, row in machine["components"].items():
                    target = current_target["components"][component] if current_target else None
                    _validate_component(row, installed_target=target)
                if machine["state"] != _machine_summary(machine):
                    raise ValueError("machine summary is inconsistent")
            clean_failures = job["clean_failure_machines"]
            if (
                not isinstance(clean_failures, list)
                or clean_failures != sorted(set(clean_failures))
                or any(machine not in job["machines"] for machine in clean_failures)
            ):
                raise ValueError("clean checkout failure history is invalid")
            catalog = job["catalog"]
            catalog_fields = {
                "operation_id", "state", "attempt", "next_retry",
                "requested_at", "acknowledged_at", "requested_revision",
                "requested_models",
            }
            if not isinstance(catalog, dict) or set(catalog) != catalog_fields:
                raise ValueError("catalog evidence is invalid")
            if catalog["operation_id"] != _catalog_operation_id(
                job["release_id"], supplement_generation,
            ):
                raise ValueError("catalog operation identity is invalid")
            if catalog["state"] not in {"pending", "requesting", "retryable_failure", "acknowledged"}:
                raise ValueError("catalog state is invalid")
            if (isinstance(catalog["attempt"], bool) or not isinstance(catalog["attempt"], int)
                    or catalog["attempt"] < 0):
                raise ValueError("catalog attempt is invalid")
            catalog_retry = _finite_time(
                catalog["next_retry"], "catalog next_retry", allow_none=True,
            )
            requested = _finite_time(
                catalog["requested_at"], "catalog requested_at", allow_none=True,
            )
            acknowledged = _finite_time(
                catalog["acknowledged_at"], "catalog acknowledged_at", allow_none=True,
            )
            requested_revision = catalog["requested_revision"]
            if requested_revision is not None and (
                not isinstance(requested_revision, str)
                or not re.fullmatch(r"[0-9a-f]{64}", requested_revision)
            ):
                raise ValueError("catalog requested revision is invalid")
            requested_models = catalog["requested_models"]
            if requested_models is not None and (
                isinstance(requested_models, bool)
                or not isinstance(requested_models, int)
                or requested_models < 0
            ):
                raise ValueError("catalog requested model count is invalid")
            if catalog["state"] == "retryable_failure" and catalog_retry is None:
                raise ValueError("retryable catalog lacks next_retry")
            if catalog["state"] != "retryable_failure" and catalog_retry is not None:
                raise ValueError("non-retryable catalog has next_retry")
            if catalog["state"] in {"requesting", "retryable_failure", "acknowledged"} and requested is None:
                raise ValueError("dispatched catalog lacks requested_at")
            if catalog["state"] == "acknowledged":
                if acknowledged is None or acknowledged < requested:
                    raise ValueError("catalog acknowledgement is invalid")
            elif acknowledged is not None:
                raise ValueError("unacknowledged catalog has acknowledged_at")
            pending = [row for machine in job["machines"].values()
                       for row in machine["components"].values()
                       if row["state"] in RETRYABLE_COMPONENT_STATES]
            retry_times = [row["next_retry"] for row in pending]
            if catalog["state"] == "retryable_failure":
                retry_times.append(catalog_retry)
            if retry_times and job["state"] != "blocked_release":
                earliest = min(retry_times)
                if job["state"] != "degraded" or retry_at != earliest or finished is not None:
                    raise ValueError("degraded job summary is inconsistent")
            elif not retry_times and job["state"] == "degraded":
                raise ValueError("degraded job lacks retryable components")
            if job["state"] == "complete" and (
                not _job_is_converged(job) or catalog["state"] != "acknowledged"
            ):
                raise ValueError("complete job is not exactly converged")
            if job["state"] in TERMINAL_SITE_STATES:
                if finished is None or retry_at is not None or job["adoption_lease"] is not None:
                    raise ValueError("terminal job timestamps are invalid")
            elif finished is not None:
                raise ValueError("nonterminal job has finished_at")
        activation = value["activation"]
        if activation is not None:
            fields = {"release_id", "activated_at", "genstudio_run_reference", "job_id"}
            if not isinstance(activation, dict) or set(activation) != fields:
                raise ValueError("activation fields are invalid")
            if manifest is None or activation["release_id"] != manifest["release_id"]:
                raise ValueError("activation does not match desired release")
            _finite_time(activation["activated_at"], "activation activated_at")
            reference = activation["genstudio_run_reference"]
            if reference is not None:
                _identifier(reference, "GenStudio run reference", opaque=True)
            job = jobs.get(activation["job_id"])
            if not job or job["release_id"] != activation["release_id"]:
                raise ValueError("activation job is invalid")
        if leased_job_ids:
            if activation is None or leased_job_ids != [activation["job_id"]]:
                raise ValueError("adoption lease is not anchored to current activation")
        for job in jobs.values():
            supersedes = job["supersedes_job_id"]
            if supersedes is not None:
                predecessor = jobs.get(supersedes)
                if (
                    predecessor is None
                    or predecessor["state"] != "complete"
                    or predecessor["finished_at"] is None
                    or predecessor["finished_at"] > job["created_at"]
                ):
                    raise ValueError("supplemental release predecessor is invalid")
        return value
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("durable release reconciliation state is invalid") from exc


class ReleaseReconciler:
    """Validate and atomically persist one controller's managed-release state."""

    def __init__(
        self,
        monitor: Any,
        *,
        state_path: Path | str = STATE_FILE,
        clock: Callable[[], float] = time.time,
        peer_reader: Callable[[str], dict[str, Any] | None] = peers.cached,
        owner_id: str | None = None,
        pid: int | None = None,
        owner_alive: Callable[[int], bool] = _pid_alive,
        lease_seconds: float = ADOPTION_LEASE_SECONDS,
        heartbeat_seconds: float | None = None,
        remote_bundle_runner: Callable[..., Any] | None = None,
        identity_reader: Callable[[], dict[str, Any]] | None = None,
        component_runner: Callable[..., Any] = run_managed_components,
        hub_runner: Callable[..., Any] | None = None,
        catalog_requester: Callable[..., Any] | None = None,
        loaded_version: str | None = None,
        loaded_commit: str | None = None,
        poll_seconds: float = 3.0,
        execution_timeout: float = 20 * 60,
    ) -> None:
        self.monitor = monitor
        self.state_path = Path(state_path)
        self.lock_path = self.state_path.with_name(f"{self.state_path.name}.lock")
        self._clock = clock
        self._peer_reader = peer_reader
        resolved_owner = owner_id if owner_id is not None else f"lease-{secrets.token_hex(16)}"
        self._owner_id = _identifier(resolved_owner, "adoption owner", opaque=True)
        self._pid = os.getpid() if pid is None else pid
        if isinstance(self._pid, bool) or not isinstance(self._pid, int) or not 1 <= self._pid <= 2**31 - 1:
            raise ValueError("adoption owner pid is invalid")
        if not callable(owner_alive):
            raise ValueError("owner_alive must be callable")
        self._owner_alive = owner_alive
        self._lease_seconds = _finite_time(lease_seconds, "adoption lease duration")
        if self._lease_seconds <= 0:
            raise ValueError("adoption lease duration must be positive")
        heartbeat = self._lease_seconds / 3 if heartbeat_seconds is None else heartbeat_seconds
        self._heartbeat_seconds = _finite_time(heartbeat, "adoption heartbeat interval")
        if self._heartbeat_seconds <= 0 or self._heartbeat_seconds >= self._lease_seconds:
            raise ValueError("adoption heartbeat interval must be positive and shorter than the lease")
        self._poll_seconds = _finite_time(poll_seconds, "managed poll interval")
        self._execution_timeout = _finite_time(execution_timeout, "managed execution timeout")
        if self._poll_seconds <= 0 or self._execution_timeout <= 0:
            raise ValueError("managed execution timing must be positive")
        self._uses_default_remote_runner = remote_bundle_runner is None
        self._remote_bundle_runner = remote_bundle_runner or self._request_remote_bundle
        self._identity_reader = identity_reader
        self._component_runner = component_runner
        self._hub_runner = hub_runner
        self._catalog_requester = catalog_requester
        self._loaded_version = loaded_version
        self._loaded_commit = loaded_commit
        self._path_key = _path_key(self.state_path)
        self._thread_lock = _path_lock(self.state_path)
        self.agent_state_path = self.state_path.with_name(f"{self.state_path.name}.managed-jobs")
        self._agent_tasks: dict[str, asyncio.Task] = {}
        self._site_tasks: dict[str, asyncio.Task] = {}
        self._lifecycle_tasks: set[asyncio.Task] = set()
        self._peer_recovery_tasks: dict[str, asyncio.Task] = {}
        self._registry_recovery_task: asyncio.Task | None = None
        self._due_task: asyncio.Task | None = None
        with self._locked():
            self._state = self._load_disk()
            self._agent_state = self._load_agent_disk()

    async def start(self) -> dict[str, int]:
        """Adopt durable child/site work and start the bounded due scanner."""
        resumed_children = 0
        for job_id, job in self._read_agent()["jobs"].items():
            if job["state"] not in TERMINAL_SITE_STATES:
                self._ensure_agent_task(job_id)
                resumed_children += 1
        self.reconcile_registry()
        resumed_release = self.resume_pending()
        if resumed_release:
            activation = self._read()["activation"]
            if activation is not None:
                self.schedule(activation["release_id"])
        if self._due_task is None or self._due_task.done():
            self._due_task = asyncio.create_task(self._due_loop())
        return {"managed_updates": resumed_children, "release_jobs": resumed_release}

    async def stop(self) -> None:
        """Stop local schedulers without changing durable retry intent."""
        tasks = [
            task for task in (
                self._due_task,
                *self._site_tasks.values(),
                *self._agent_tasks.values(),
                *self._lifecycle_tasks,
            )
            if task is not None and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._due_task = None
        self._site_tasks.clear()
        self._agent_tasks.clear()
        self._lifecycle_tasks.clear()
        self._peer_recovery_tasks.clear()
        self._registry_recovery_task = None

    def schedule(self, release_id: str) -> asyncio.Task:
        """Schedule one adopted release and keep it inside service lifecycle."""
        _identifier(release_id, "release_id")
        task = asyncio.create_task(self.run(release_id))
        self._lifecycle_tasks.add(task)

        def finished(done: asyncio.Task) -> None:
            self._lifecycle_tasks.discard(done)
            self._consume_background_result(done)

        task.add_done_callback(finished)
        return task

    def wake_peer(self, machine: str) -> int:
        """Mark one recovered machine due and immediately adopt its active release."""
        supplemented = self.reconcile_registry()
        due = self.note_peer_recovered(machine)
        if not due and not supplemented:
            return 0
        current = self._peer_recovery_tasks.get(machine)
        if current is not None and not current.done():
            return due
        task = asyncio.create_task(self._resume_recovered_peer())
        self._peer_recovery_tasks[machine] = task
        self._lifecycle_tasks.add(task)

        def finished(done: asyncio.Task) -> None:
            if self._peer_recovery_tasks.get(machine) is done:
                self._peer_recovery_tasks.pop(machine, None)
            self._lifecycle_tasks.discard(done)
            self._consume_background_result(done)

        task.add_done_callback(finished)
        return due or supplemented

    def wake_registry(self) -> int:
        """Reconcile fleet changes now or immediately after the active pass."""
        supplemented = self.reconcile_registry()
        state = self._read()
        activation = state["activation"]
        if supplemented:
            if activation is not None:
                self.schedule(activation["release_id"])
            return supplemented
        if activation is None:
            return 0
        job = self._find_job(state, activation["job_id"])
        if job["state"] in TERMINAL_SITE_STATES or job["adoption_lease"] is None:
            return 0
        current = self._registry_recovery_task
        if current is not None and not current.done():
            return 0
        task = asyncio.create_task(self._resume_reconciled_registry())
        self._registry_recovery_task = task
        self._lifecycle_tasks.add(task)

        def finished(done: asyncio.Task) -> None:
            if self._registry_recovery_task is done:
                self._registry_recovery_task = None
            self._lifecycle_tasks.discard(done)
            self._consume_background_result(done)

        task.add_done_callback(finished)
        return 0

    async def _resume_reconciled_registry(self) -> None:
        await asyncio.sleep(0)
        while True:
            state = self._read()
            activation = state["activation"]
            if activation is None:
                return
            release_id = activation["release_id"]
            job = self._find_job(state, activation["job_id"])
            if job["adoption_lease"] is not None:
                active = self._site_tasks.get(release_id)
                if active is not None and not active.done():
                    await asyncio.gather(asyncio.shield(active), return_exceptions=True)
                else:
                    if self.resume_pending() == 1:
                        generation = self._site_generation(activation["job_id"])
                        self.release_adoption(
                            activation["job_id"], generation=generation,
                        )
                        continue
                    await asyncio.sleep(min(self._poll_seconds, 1.0))
                continue
            if self.reconcile_registry():
                stale = self._site_tasks.get(release_id)
                if stale is not None:
                    if not stale.done():
                        await asyncio.gather(
                            asyncio.shield(stale), return_exceptions=True,
                        )
                    if self._site_tasks.get(release_id) is stale:
                        self._site_tasks.pop(release_id, None)
                if self.resume_pending() == 1:
                    await self.run(release_id)
                return
            refreshed = self._read()
            current = refreshed["activation"]
            if current is None or current["release_id"] != release_id:
                return
            if self._find_job(refreshed, current["job_id"])["adoption_lease"] is None:
                return

    async def _resume_recovered_peer(self) -> None:
        await asyncio.sleep(0)
        state = self._read()
        activation = state["activation"]
        if activation is None:
            return
        release_id = activation["release_id"]
        active = self._site_tasks.get(release_id)
        if active is not None and not active.done():
            await asyncio.gather(asyncio.shield(active), return_exceptions=True)
        self.resume_due()
        if not self.resume_pending():
            return
        await self.run(release_id)

    @staticmethod
    def _consume_background_result(task: asyncio.Task) -> None:
        if not task.cancelled():
            task.exception()

    async def _due_loop(self) -> None:
        while True:
            await asyncio.sleep(DUE_SCAN_INTERVAL_SECONDS)
            supplemented = self.reconcile_registry()
            due = self.resume_due()
            if not (supplemented or due) or not self.resume_pending():
                continue
            activation = self._read()["activation"]
            if activation is not None:
                self.schedule(activation["release_id"])

    @staticmethod
    def _fresh_state() -> dict[str, Any]:
        return {"schema_version": 1, "desired": None, "activation": None, "jobs": {}}

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._thread_lock:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            with self.lock_path.open("a+", encoding="utf-8") as handle:
                os.chmod(self.lock_path, 0o600)
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _load_disk(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return self._fresh_state()
        os.chmod(self.state_path, 0o600)
        try:
            value = json.loads(
                self.state_path.read_text(encoding="utf-8"),
                parse_constant=_reject_json_constant,
            )
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError("durable release reconciliation state is invalid") from exc
        return _validate_state(value)

    def _load_agent_disk(self) -> dict[str, Any]:
        if not self.agent_state_path.exists():
            return {"schema_version": 1, "jobs": {}}
        os.chmod(self.agent_state_path, 0o600)
        try:
            value = json.loads(
                self.agent_state_path.read_text(encoding="utf-8"),
                parse_constant=_reject_json_constant,
            )
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError("durable managed child state is invalid") from exc
        try:
            return _validate_agent_state(value)
        except ValueError as exc:
            raise ValueError("durable managed child state is invalid") from exc

    def _read(self) -> dict[str, Any]:
        with self._locked():
            current = self._load_disk()
            self._state = current
            return current

    def _write(
        self,
        mutate: Callable[[dict[str, Any]], Any],
        *,
        after_commit: Callable[[dict[str, Any], Any], None] | None = None,
    ) -> Any:
        with self._locked():
            current = self._load_disk()
            candidate = deepcopy(current)
            try:
                result = mutate(candidate)
                _validate_state(candidate)
                if candidate != current:
                    _atomic_json(self.state_path, candidate)
            except Exception:
                self._state = current
                raise
            self._state = candidate
            if after_commit is not None:
                after_commit(candidate, result)
            return deepcopy(result)

    def _read_agent(self) -> dict[str, Any]:
        with self._locked():
            current = self._load_agent_disk()
            self._agent_state = current
            return current

    def _write_agent(self, mutate: Callable[[dict[str, Any]], Any]) -> Any:
        with self._locked():
            current = self._load_agent_disk()
            candidate = deepcopy(current)
            try:
                result = mutate(candidate)
                _validate_agent_state(candidate)
                if candidate != current:
                    _atomic_json(self.agent_state_path, candidate)
            except Exception:
                self._agent_state = current
                raise
            self._agent_state = candidate
            return deepcopy(result)

    def _lease_owner_is_alive(self, pid: int) -> bool:
        try:
            return bool(self._owner_alive(pid))
        except Exception:
            return True

    def _require_site_fence(self, job: dict[str, Any], generation: int) -> None:
        lease = job["adoption_lease"]
        if (
            lease is None
            or lease["owner_id"] != self._owner_id
            or lease["pid"] != self._pid
            or lease["generation"] != generation
            or lease["expires_at"] <= self._clock()
        ):
            raise LeaseLostError("release execution lease was lost")

    def _assert_site_fence(self, job_id: str, generation: int) -> None:
        self._require_site_fence(self._find_job(self._read(), job_id), generation)

    def _require_agent_fence(self, job: dict[str, Any], generation: int) -> None:
        lease = job["execution_lease"]
        if (
            lease is None
            or lease["owner_id"] != self._owner_id
            or lease["pid"] != self._pid
            or lease["generation"] != generation
            or lease["expires_at"] <= self._clock()
        ):
            raise LeaseLostError("managed child execution lease was lost")

    def _assert_agent_fence(self, job_id: str, generation: int) -> None:
        state = self._read_agent()
        try:
            job = state["jobs"][job_id]
        except KeyError as exc:
            raise ValueError("unknown managed child job") from exc
        self._require_agent_fence(job, generation)

    def _remember_path_owner(self) -> None:
        with _PATH_LOCKS_GUARD:
            _PATH_OWNERS[self._path_key] = self._owner_id

    def _current_path_owner(self) -> str | None:
        with _PATH_LOCKS_GUARD:
            return _PATH_OWNERS.get(self._path_key)

    def _forget_path_owner(self) -> None:
        with _PATH_LOCKS_GUARD:
            if _PATH_OWNERS.get(self._path_key) == self._owner_id:
                _PATH_OWNERS.pop(self._path_key, None)

    def _clear_path_owner(self) -> None:
        with _PATH_LOCKS_GUARD:
            _PATH_OWNERS.pop(self._path_key, None)

    @staticmethod
    def _find_job(state: dict[str, Any], job_id: str) -> dict[str, Any]:
        try:
            return state["jobs"][job_id]
        except KeyError as exc:
            raise ValueError("unknown release job") from exc

    def replace_intent(self, manifest: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        validated = _validate_manifest(manifest)

        def mutate(state: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
            desired = state["desired"]
            if desired:
                current = desired["manifest"]
                if validated["release_id"] == current["release_id"]:
                    if validated != current:
                        raise ValueError("release_id replay changed canonical manifest content")
                    return False, current
                if validated["sequence"] < current["sequence"]:
                    raise ValueError("release sequence cannot move backwards")
                if any(job["state"] not in TERMINAL_SITE_STATES for job in state["jobs"].values()):
                    raise ValueError("cannot replace intent while a prior release job is nonterminal")
            now = _finite_time(self._clock(), "received_at")
            state["desired"] = {"manifest": validated, "received_at": now}
            state["activation"] = None
            return True, validated

        return self._write(mutate)

    def withdraw_intent(self) -> dict[str, Any]:
        """Abandon the desired release intent, its activation, and every job.

        ``replace_intent`` refuses a new intent while any prior job is
        nonterminal, and some jobs can never terminate -- an intent that pins a
        release older than what a machine already runs is refused by the
        updater's ancestor check by design, so the job stays nonterminal
        forever.  Withdrawal is the operator's only exit from that state.

        It clears exactly the three release-intent fields in one transaction and
        touches nothing else: not capacity, not worker records, not the updater.
        Readers already treat an absent intent as first-class clean state, so a
        withdrawal removes the release signal rather than inverting it.

        Returns the record of what was withdrawn, so the abandoned intent stays
        traceable after its state is gone.  Withdrawing nothing is a no-op that
        reports ``withdrawn: False``.
        """

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            desired = state["desired"]
            manifest = desired["manifest"] if desired else None
            activation = state["activation"]
            record = {
                "withdrawn": bool(
                    desired is not None or activation is not None or state["jobs"]
                ),
                "release_id": manifest["release_id"] if manifest else None,
                "sequence": manifest["sequence"] if manifest else None,
                "created_at": manifest["created_at"] if manifest else None,
                "received_at": desired["received_at"] if desired else None,
                "activation": deepcopy(activation),
                "jobs": [
                    {
                        "id": job["id"],
                        "release_id": job["release_id"],
                        "state": job["state"],
                        "created_at": job["created_at"],
                        "leased": job["adoption_lease"] is not None,
                    }
                    for job in sorted(
                        state["jobs"].values(), key=lambda job: job["id"]
                    )
                ],
            }
            state["desired"] = None
            state["activation"] = None
            state["jobs"] = {}
            return record

        return self._write(mutate)

    def _machines(
        self, manifest: dict[str, Any], *, operation_generation: int = 0,
    ) -> dict[str, Any]:
        installed: dict[str, set[str]] = (
            {"local": set()} if registry.machine_enabled("local") else {}
        )
        for studio in self.monitor.registry:
            machine = _identifier(studio.get("machine", "local"), "stable machine id")
            if not registry.machine_enabled(machine):
                continue
            installed.setdefault(machine, set())
            if studio.get("modality") in {"image", "voice"}:
                installed[machine].add(studio["modality"])
        machines = {}
        for machine in sorted(installed):
            try:
                evidence = _host_evidence(self._peer_reader(machine))
            except Exception:
                evidence = {}
            components = {}
            for name in _REPOSITORIES:
                target = manifest["components"][name]
                present = name == "hub" or name in installed[machine]
                components[name] = {
                    "installed": present,
                    "expected_version": target["version"],
                    "expected_commit": target["commit"],
                    "observed_version": None,
                    "observed_commit": None,
                    "state": "checking" if present else "not_installed",
                    "attempt": 0,
                    "error_code": None,
                    "detail": None,
                    "next_retry": None,
                }
            machines[machine] = {
                "id": machine,
                "host_evidence": evidence,
                "agent_job_id": None,
                "operation_id": _operation_id(
                    manifest["release_id"], machine, operation_generation,
                ),
                "operation_generation": operation_generation,
                "state": "running",
                "components": components,
            }
        return machines

    def activate(self, release_id: str, *, genstudio_run_reference: str | None) -> dict[str, Any]:
        if not isinstance(release_id, str) or not _RELEASE_ID_RE.fullmatch(release_id):
            raise ValueError("release_id is invalid")
        if genstudio_run_reference is not None:
            _identifier(genstudio_run_reference, "GenStudio run reference", opaque=True)

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            desired = state["desired"]
            if not desired or desired["manifest"]["release_id"] != release_id:
                raise ValueError("release_id is not the current desired intent")
            job_id = _job_id(release_id)
            existing = state["jobs"].get(job_id)
            if existing:
                if existing["genstudio_run_reference"] != genstudio_run_reference:
                    raise ValueError("activation replay changed GenStudio run reference")
                activation = state["activation"]
                if activation and activation["release_id"] == release_id:
                    return self._find_job(state, activation["job_id"])
                return existing
            now = _finite_time(self._clock(), "activated_at")
            job = {
                "id": job_id,
                "release_id": release_id,
                "genstudio_run_reference": genstudio_run_reference,
                "state": "queued",
                "created_at": now,
                "started_at": None,
                "finished_at": None,
                "next_retry": None,
                "adoption_lease": None,
                "lease_generation": 0,
                "clean_failure_machines": [],
                "job_generation": 0,
                "supplement_generation": 0,
                "supersedes_job_id": None,
                "machines": self._machines(desired["manifest"]),
                "catalog": {
                    "operation_id": _catalog_operation_id(release_id),
                    "state": "pending",
                    "attempt": 0,
                    "next_retry": None,
                    "requested_at": None,
                    "acknowledged_at": None,
                    "requested_revision": None,
                    "requested_models": None,
                },
            }
            state["jobs"][job_id] = job
            state["activation"] = {
                "release_id": release_id,
                "activated_at": now,
                "genstudio_run_reference": genstudio_run_reference,
                "job_id": job_id,
            }
            return job

        return self._write(mutate)

    def reconcile_registry(self) -> int:
        """Durably add fleet growth to the active immutable release.

        Nonterminal jobs are supplemented in place while unleased. A completed
        job remains immutable: its exact snapshot is copied into a new,
        explicitly linked supplemental job that executes only new machines or
        newly installed Image/Voice components.
        """
        now = _finite_time(self._clock(), "registry reconciliation time")

        def mutate(state: dict[str, Any]) -> int:
            desired = state["desired"]
            activation = state["activation"]
            if (
                desired is None
                or activation is None
                or activation["release_id"] != desired["manifest"]["release_id"]
            ):
                return 0
            current = self._find_job(state, activation["job_id"])
            if current["state"] == "blocked_release" or current["adoption_lease"] is not None:
                return 0

            manifest = desired["manifest"]
            inventory = self._machines(
                manifest,
                operation_generation=current["supplement_generation"] + 1,
            )
            changed: dict[str, set[str]] = {}
            for machine_id, observed in inventory.items():
                existing = current["machines"].get(machine_id)
                if existing is None:
                    changed[machine_id] = {
                        name for name, row in observed["components"].items() if row["installed"]
                    }
                    continue
                installed = {
                    name for name, row in observed["components"].items()
                    if row["installed"] and (
                        not existing["components"][name]["installed"]
                        or existing["components"][name]["state"] == "excluded_disabled"
                    )
                }
                if installed:
                    changed[machine_id] = installed
            if not changed:
                return 0

            generation = current["supplement_generation"] + 1
            if current["state"] == "complete":
                job = deepcopy(current)
                job_generation = current["job_generation"] + 1
                job_id = _job_id(manifest["release_id"], job_generation)
                job.update(
                    id=job_id,
                    state="queued",
                    created_at=now,
                    started_at=None,
                    finished_at=None,
                    next_retry=None,
                    adoption_lease=None,
                    lease_generation=0,
                    job_generation=job_generation,
                    supplement_generation=generation,
                    supersedes_job_id=current["id"],
                )
                state["jobs"][job_id] = job
                activation.update(job_id=job_id, activated_at=now)
            else:
                job = current
                job["supplement_generation"] = generation

            for machine_id, installed in changed.items():
                observed = inventory[machine_id]
                machine = job["machines"].get(machine_id)
                if machine is None:
                    job["machines"][machine_id] = observed
                    continue
                machine.update(
                    host_evidence=observed["host_evidence"],
                    agent_job_id=None,
                    operation_id=_operation_id(
                        manifest["release_id"], machine_id, generation,
                    ),
                    operation_generation=generation,
                )
                for component in installed:
                    machine["components"][component] = observed["components"][component]
                machine["state"] = _machine_summary(machine)

            job["catalog"] = {
                "operation_id": _catalog_operation_id(manifest["release_id"], generation),
                "state": "pending",
                "attempt": 0,
                "next_retry": None,
                "requested_at": None,
                "acknowledged_at": None,
                "requested_revision": None,
                "requested_models": None,
            }
            _refresh_job(job)
            return len(changed)

        return self._write(mutate)

    def persist_remote_job(
        self, job_id: str, machine: str, agent_job_id: str, *, fence: int | None = None,
    ) -> bool:
        _identifier(job_id, "job id")
        _identifier(machine, "stable machine id")
        _identifier(agent_job_id, "agent_job_id", opaque=True)

        def mutate(state: dict[str, Any]) -> bool:
            job = self._find_job(state, job_id)
            if fence is not None:
                self._require_site_fence(job, fence)
            if job["state"] in TERMINAL_SITE_STATES:
                raise ValueError("terminal release job cannot be mutated")
            try:
                row = job["machines"][machine]
            except KeyError as exc:
                raise ValueError("unknown stable machine") from exc
            current = row["agent_job_id"]
            if current == agent_job_id:
                return False
            if current is not None:
                raise ValueError("agent job identity cannot change")
            row["agent_job_id"] = agent_job_id
            row["state"] = _machine_summary(row)
            return True

        return self._write(mutate)

    def record_component(
        self,
        job_id: str,
        machine: str,
        component: str,
        *,
        state: str,
        observed_version: str | None = None,
        observed_commit: str | None = None,
        error_code: str | None = None,
        next_retry: float | None = None,
        fence: int | None = None,
        _clean_failure: bool = False,
    ) -> dict[str, Any]:
        _identifier(job_id, "job id")
        _identifier(machine, "stable machine id")
        if component not in _REPOSITORIES:
            raise ValueError("component is invalid")
        if state not in COMPONENT_STATES:
            raise ValueError("component state is invalid")
        if observed_version is not None and (
            not isinstance(observed_version, str) or not _SEMVER_RE.fullmatch(observed_version)
        ):
            raise ValueError("observed version is invalid")
        if observed_commit is not None and (
            not isinstance(observed_commit, str) or not _COMMIT_RE.fullmatch(observed_commit)
        ):
            raise ValueError("observed commit is invalid")
        if error_code is not None and error_code not in _ERROR_DETAILS:
            raise ValueError("error code is invalid")
        if error_code is not None and state not in RETRYABLE_COMPONENT_STATES | {"release_blocked"}:
            raise ValueError("error code is invalid for this component state")
        if _clean_failure and (
            state not in RETRYABLE_COMPONENT_STATES
            or error_code != "clean_checkout_health_failure"
        ):
            raise ValueError("clean failure observation is inconsistent")
        now = _finite_time(self._clock(), "recorded_at")
        validated_retry = None
        if next_retry is not None:
            if state not in RETRYABLE_COMPONENT_STATES:
                raise ValueError("next_retry is valid only for retryable component states")
            validated_retry = _finite_time(next_retry, "next_retry", minimum=now)

        def mutate(durable: dict[str, Any]) -> dict[str, Any]:
            job = self._find_job(durable, job_id)
            if fence is not None:
                self._require_site_fence(job, fence)
            if job["state"] in TERMINAL_SITE_STATES:
                raise ValueError("terminal release job cannot be mutated")
            try:
                machine_row = job["machines"][machine]
                row = machine_row["components"][component]
            except KeyError as exc:
                raise ValueError("unknown machine component") from exc
            if not row["installed"] and state != "not_installed":
                raise ValueError("uninstalled component cannot be mutated")
            if row["installed"] and state == "not_installed":
                raise ValueError("installed component cannot be not_installed")
            if state == "current" and (
                observed_version != row["expected_version"]
                or observed_commit != row["expected_commit"]
            ):
                raise ValueError("current requires exact observed version and commit")
            code = error_code or _DEFAULT_ERROR_CODES.get(state)
            row.update(
                state=state,
                observed_version=observed_version,
                observed_commit=observed_commit,
                error_code=code,
                detail=_ERROR_DETAILS.get(code) if code else None,
            )
            if state in RETRYABLE_COMPONENT_STATES:
                row["attempt"] += 1
                row["next_retry"] = validated_retry or now + retry_delay(row["attempt"])
            else:
                row["next_retry"] = None
            if _clean_failure:
                history = job["clean_failure_machines"]
                if machine not in history:
                    history.append(machine)
                    history.sort()
                if len(history) >= 2:
                    for durable_machine in job["machines"].values():
                        for durable_row in durable_machine["components"].values():
                            if durable_row["installed"] and durable_row["state"] != "current":
                                durable_row.update(
                                    state="release_blocked",
                                    error_code="clean_checkout_health_failure",
                                    detail=_ERROR_DETAILS["clean_checkout_health_failure"],
                                    next_retry=None,
                                )
                        durable_machine["state"] = _machine_summary(durable_machine)
                    job.update(
                        state="blocked_release", finished_at=now, next_retry=None,
                        adoption_lease=None,
                    )
                else:
                    _refresh_job(job)
            elif state == "release_blocked":
                machine_row["state"] = "degraded"
                job.update(
                    state="blocked_release", finished_at=now, next_retry=None,
                    adoption_lease=None,
                )
            else:
                _refresh_job(job)
            return row

        return self._write(
            mutate,
            after_commit=lambda candidate, _result: self._clear_path_owner()
            if candidate["jobs"][job_id]["state"] == "blocked_release" else None,
        )

    def persist_job(
        self, job_id: str, *, state: str, fence: int | None = None,
    ) -> dict[str, Any]:
        _identifier(job_id, "job id")
        if state not in SITE_STATES:
            raise ValueError("site state is invalid")
        now = _finite_time(self._clock(), "job state time")

        def mutate(durable: dict[str, Any]) -> dict[str, Any]:
            job = self._find_job(durable, job_id)
            if fence is not None:
                self._require_site_fence(job, fence)
            if job["state"] in TERMINAL_SITE_STATES:
                if job["state"] == state:
                    return job
                raise ValueError("terminal release job state cannot change")
            if state == "complete":
                if not _job_is_converged(job):
                    raise ValueError("release job is not exactly converged")
                if job["catalog"]["state"] != "acknowledged":
                    raise ValueError("catalog reconciliation is not acknowledged")
                for machine in job["machines"].values():
                    machine["state"] = _machine_summary(machine)
                job.update(
                    state="complete", finished_at=now, next_retry=None,
                    adoption_lease=None,
                )
            elif state == "blocked_release":
                job.update(
                    state="blocked_release", finished_at=now, next_retry=None,
                    adoption_lease=None,
                )
            elif state == "degraded":
                _refresh_job(job)
                if job["state"] != "degraded":
                    raise ValueError("degraded job requires retryable components")
            else:
                job.update(state=state, finished_at=None)
            return job

        return self._write(
            mutate,
            after_commit=(lambda _candidate, _result: self._clear_path_owner())
            if state in TERMINAL_SITE_STATES else None,
        )

    def resume_pending(self) -> int:
        def mutate(state: dict[str, Any]) -> int:
            desired = state["desired"]
            activation = state["activation"]
            if not desired or not activation:
                return 0
            job_id = activation["job_id"]
            job = self._find_job(state, job_id)
            if (
                activation["release_id"] != desired["manifest"]["release_id"]
                or job["release_id"] != activation["release_id"]
                or job["state"] in TERMINAL_SITE_STATES
            ):
                return 0
            now = _finite_time(self._clock(), "resume time")
            lease = job["adoption_lease"]
            path_owner = self._current_path_owner()
            if lease is None and path_owner is not None:
                return 0
            if lease is not None:
                if lease["owner_id"] == self._owner_id and lease["expires_at"] > now:
                    return 0
                if lease["expires_at"] > now and self._lease_owner_is_alive(lease["pid"]):
                    return 0
            if job["state"] in {"pending", "queued"}:
                job["state"] = "running"
            for machine in job["machines"].values():
                for row in machine["components"].values():
                    if (
                        row["state"] in RETRYABLE_COMPONENT_STATES
                        and row["next_retry"] <= now
                    ):
                        row.update(
                            state="checking", next_retry=None,
                            error_code=None, detail=None,
                        )
            if (
                job["catalog"]["state"] == "retryable_failure"
                and job["catalog"]["next_retry"] <= now
            ):
                job["catalog"].update(state="pending", next_retry=None)
            _refresh_job(job)
            job["started_at"] = job["started_at"] or now
            job["lease_generation"] += 1
            job["adoption_lease"] = {
                "owner_id": self._owner_id,
                "pid": self._pid,
                "generation": job["lease_generation"],
                "acquired_at": now,
                "heartbeat_at": now,
                "expires_at": now + self._lease_seconds,
            }
            return 1

        return self._write(
            mutate,
            after_commit=lambda _candidate, count: self._remember_path_owner() if count else None,
        )

    def refresh_adoption(
        self, job_id: str | None = None, *, generation: int | None = None,
    ) -> bool:
        if job_id is not None:
            _identifier(job_id, "job id")
        now = _finite_time(self._clock(), "adoption heartbeat time")

        def mutate(state: dict[str, Any]) -> bool:
            activation = state["activation"]
            if activation is None:
                return False
            current_job_id = activation["job_id"]
            if job_id is not None and job_id != current_job_id:
                return False
            job = self._find_job(state, current_job_id)
            lease = job["adoption_lease"]
            if (
                job["state"] in TERMINAL_SITE_STATES
                or lease is None
                or lease["owner_id"] != self._owner_id
                or lease["pid"] != self._pid
                or (generation is not None and lease["generation"] != generation)
                or lease["expires_at"] <= now
            ):
                return False
            lease.update(heartbeat_at=now, expires_at=now + self._lease_seconds)
            return True

        return self._write(
            mutate,
            after_commit=lambda _candidate, refreshed: self._remember_path_owner()
            if refreshed else None,
        )

    def release_adoption(
        self, job_id: str | None = None, *, generation: int | None = None,
    ) -> bool:
        if job_id is not None:
            _identifier(job_id, "job id")

        def mutate(state: dict[str, Any]) -> bool:
            activation = state["activation"]
            if activation is None:
                return False
            current_job_id = activation["job_id"]
            if job_id is not None and job_id != current_job_id:
                return False
            job = self._find_job(state, current_job_id)
            lease = job["adoption_lease"]
            if (
                lease is None
                or lease["owner_id"] != self._owner_id
                or lease["pid"] != self._pid
                or (generation is not None and lease["generation"] != generation)
            ):
                return False
            job["adoption_lease"] = None
            return True

        return self._write(
            mutate,
            after_commit=lambda _candidate, _released: self._forget_path_owner(),
        )

    def resume_due(self) -> int:
        now = _finite_time(self._clock(), "due scan time")

        def mutate(state: dict[str, Any]) -> int:
            desired = state["desired"]
            activation = state["activation"]
            if not desired or not activation or activation["release_id"] != desired["manifest"]["release_id"]:
                return 0
            job = self._find_job(state, activation["job_id"])
            if job["state"] in TERMINAL_SITE_STATES:
                return 0
            due_rows = [
                row
                for machine in job["machines"].values()
                for row in machine["components"].values()
                if row["state"] in RETRYABLE_COMPONENT_STATES and row["next_retry"] <= now
            ]
            catalog_due = (
                job["catalog"]["state"] == "retryable_failure"
                and job["catalog"]["next_retry"] <= now
            )
            due = len(due_rows) + int(catalog_due)
            lease = job["adoption_lease"]
            if due and lease is not None:
                return due
            for row in due_rows:
                row.update(state="checking", next_retry=None, error_code=None, detail=None)
            if catalog_due:
                job["catalog"].update(state="pending", next_retry=None)
            if due:
                _refresh_job(job)
            return due

        return self._write(mutate)

    def note_peer_recovered(self, machine: str) -> int:
        """Make only one known recovered machine immediately eligible for retry."""
        _identifier(machine, "stable machine id")
        now = _finite_time(self._clock(), "peer recovery time")

        def mutate(state: dict[str, Any]) -> int:
            desired = state["desired"]
            activation = state["activation"]
            if not desired or not activation or activation["release_id"] != desired["manifest"]["release_id"]:
                return 0
            job = self._find_job(state, activation["job_id"])
            if job["state"] in TERMINAL_SITE_STATES or machine not in job["machines"]:
                return 0
            due = 0
            for row in job["machines"][machine]["components"].values():
                if row["state"] in RETRYABLE_COMPONENT_STATES:
                    row["next_retry"] = now
                    due += 1
            if due:
                _refresh_job(job)
            return due

        return self._write(mutate)

    def admit_managed_update(self, bundle: dict[str, Any]) -> dict[str, Any]:
        """Durably admit one agent-local child operation before any execution."""
        validated = _validate_managed_bundle(bundle)
        operation_id = validated["operation_id"]
        job_id = _agent_job_id(operation_id)
        local_installed = {
            studio.get("modality") for studio in self.monitor.registry
            if studio.get("machine", "local") == "local"
        }

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            existing = state["jobs"].get(job_id)
            if existing is not None:
                if existing["bundle"] != validated:
                    raise ValueError("managed operation replay changed the exact bundle")
                return {"job_id": job_id, "adopted": True}
            if any(job["state"] not in TERMINAL_SITE_STATES for job in state["jobs"].values()):
                raise ValueError("another managed operation is active")
            now = _finite_time(self._clock(), "managed child admission time")
            components = {}
            for name in _REPOSITORIES:
                installed = name == "hub" or name in local_installed
                components[name] = {
                    "component": name,
                    "installed": installed,
                    "state": "checking" if installed else "not_installed",
                    "observed_version": None,
                    "observed_commit": None,
                    "observed_release_id": None,
                    "error_code": None,
                }
            state["jobs"][job_id] = {
                "id": job_id,
                "operation_id": operation_id,
                "bundle": validated,
                "state": "queued",
                "created_at": now,
                "started_at": None,
                "finished_at": None,
                "execution_lease": None,
                "lease_generation": 0,
                "components": components,
            }
            return {"job_id": job_id, "adopted": False}

        return self._write_agent(mutate)

    def managed_update_snapshot(self, job_id: str) -> dict[str, Any]:
        _identifier(job_id, "managed child job id", opaque=True)
        try:
            job = deepcopy(self._read_agent()["jobs"][job_id])
        except KeyError as exc:
            raise ValueError("unknown managed child job") from exc
        job.pop("execution_lease")
        job.pop("lease_generation")
        job["components"] = [job["components"][name] for name in ("hub", "image", "voice")]
        return job

    def _agent_record_result(
        self, job_id: str, result: dict[str, Any], *, fence: int,
    ) -> None:
        component = result.get("component") or result.get("modality")
        if component not in _REPOSITORIES:
            raise ValueError("managed child component result is invalid")
        state_name = _result_state(result)
        error_code = _result_error_code(result, state_name)

        def mutate(state: dict[str, Any]) -> None:
            try:
                job = state["jobs"][job_id]
                row = job["components"][component]
            except KeyError as exc:
                raise ValueError("unknown managed child component") from exc
            self._require_agent_fence(job, fence)
            if not row["installed"]:
                return
            target = job["bundle"]["components"][component]
            observed_version = (result.get("observed_version") or result.get("target_version")
                                or result.get("to_version"))
            observed_commit = result.get("observed_commit") or result.get("target_commit")
            valid_attestation = (
                isinstance(observed_version, str)
                and _SEMVER_RE.fullmatch(observed_version)
                and isinstance(observed_commit, str)
                and _COMMIT_RE.fullmatch(observed_commit)
            )
            observed_release = result.get("observed_release_id")
            manifest_mismatch = (
                isinstance(observed_release, str)
                and _RELEASE_ID_RE.fullmatch(observed_release)
                and observed_release != job["bundle"]["release_id"]
            )
            explicit_version = result.get("observed_version")
            explicit_commit = result.get("observed_commit")
            valid_explicit_tuple = (
                isinstance(explicit_version, str)
                and _SEMVER_RE.fullmatch(explicit_version)
                and isinstance(explicit_commit, str)
                and _COMMIT_RE.fullmatch(explicit_commit)
            )
            if manifest_mismatch:
                state = "release_blocked"
                code = "manifest_mismatch"
                observed_version = observed_commit = None
            elif valid_explicit_tuple and (
                explicit_version != target["version"]
                or explicit_commit != target["commit"]
            ):
                state = "release_blocked"
                code = "sha_mismatch"
                observed_version = explicit_version
                observed_commit = explicit_commit
            elif state_name == "current" and not valid_attestation:
                state = "retryable_failure"
                code = "invalid_evidence"
                observed_version = observed_commit = None
            elif state_name == "current" and (
                observed_version != target["version"] or observed_commit != target["commit"]
            ):
                state = "release_blocked"
                code = "sha_mismatch"
            elif state_name == "release_blocked":
                state = "retryable_failure"
                code = error_code
                observed_version = observed_commit = None
            else:
                state = state_name
                code = error_code
            row.update(
                state=state,
                observed_version=(
                    observed_version if state in {"current", "release_blocked"} else None
                ),
                observed_commit=(
                    observed_commit if state in {"current", "release_blocked"} else None
                ),
                observed_release_id=(
                    observed_release if state == "release_blocked" and manifest_mismatch else None
                ),
                error_code=code,
            )
            rows = list(job["components"].values())
            now = _finite_time(self._clock(), "managed child result time")
            if any(item["state"] == "release_blocked" for item in rows):
                job.update(state="blocked_release", finished_at=now, execution_lease=None)
            elif all(
                (not item["installed"] and item["state"] == "not_installed")
                or (item["installed"] and item["state"] == "current")
                for item in rows
            ):
                job.update(state="complete", finished_at=now, execution_lease=None)
            elif any(item["state"] in RETRYABLE_COMPONENT_STATES for item in rows):
                job.update(state="degraded", finished_at=None)
            else:
                job.update(state="running", finished_at=None)

        self._write_agent(mutate)

    def _claim_agent_execution(self, job_id: str) -> int | None:
        def mutate(state: dict[str, Any]) -> int | None:
            try:
                job = state["jobs"][job_id]
            except KeyError as exc:
                raise ValueError("unknown managed child job") from exc
            if job["state"] in TERMINAL_SITE_STATES:
                return None
            now = _finite_time(self._clock(), "managed child start time")
            lease = job["execution_lease"]
            if lease is not None and lease["expires_at"] > now:
                if lease["owner_id"] == self._owner_id:
                    return None
                if self._lease_owner_is_alive(lease["pid"]):
                    return None
            job["lease_generation"] += 1
            generation = job["lease_generation"]
            job.update(
                state="running",
                started_at=job["started_at"] or now,
                finished_at=None,
                execution_lease={
                    "owner_id": self._owner_id,
                    "pid": self._pid,
                    "generation": generation,
                    "acquired_at": now,
                    "heartbeat_at": now,
                    "expires_at": now + self._lease_seconds,
                },
            )
            return generation

        return self._write_agent(mutate)

    def _refresh_agent_execution(self, job_id: str, generation: int) -> bool:
        now = _finite_time(self._clock(), "managed child heartbeat time")

        def mutate(state: dict[str, Any]) -> bool:
            try:
                job = state["jobs"][job_id]
            except KeyError as exc:
                raise ValueError("unknown managed child job") from exc
            lease = job["execution_lease"]
            if (
                lease is None
                or lease["owner_id"] != self._owner_id
                or lease["pid"] != self._pid
                or lease["generation"] != generation
                or lease["expires_at"] <= now
            ):
                return False
            lease.update(heartbeat_at=now, expires_at=now + self._lease_seconds)
            return True

        return self._write_agent(mutate)

    def _release_agent_execution(self, job_id: str, generation: int) -> bool:
        def mutate(state: dict[str, Any]) -> bool:
            try:
                job = state["jobs"][job_id]
            except KeyError as exc:
                raise ValueError("unknown managed child job") from exc
            lease = job["execution_lease"]
            if (
                lease is None
                or lease["owner_id"] != self._owner_id
                or lease["pid"] != self._pid
                or lease["generation"] != generation
            ):
                return False
            job["execution_lease"] = None
            return True

        return self._write_agent(mutate)

    async def _execute_managed_update(self, job_id: str, fence: int) -> dict[str, Any]:

        snapshot = self.managed_update_snapshot(job_id)
        if snapshot["state"] in TERMINAL_SITE_STATES:
            return snapshot
        rows = {row["component"]: row for row in snapshot["components"]}
        bundle = snapshot["bundle"]
        operation_id = snapshot["operation_id"]
        hub = rows["hub"]
        if hub["state"] != "current":
            target = bundle["components"]["hub"]
            if (self._loaded_version == target["version"]
                    and self._loaded_commit == target["commit"]):
                result = {
                    "component": "hub",
                    "state": "current",
                    "observed_version": self._loaded_version,
                    "observed_commit": self._loaded_commit,
                }
            elif self._hub_runner is None:
                result = {"component": "hub", "state": "retryable_failure",
                          "error_code": "updater_unavailable"}
            else:
                self._assert_agent_fence(job_id, fence)
                try:
                    result = await self._await(self._hub_runner(target, operation_id))
                except LeaseLostError:
                    raise
                except Exception:
                    result = {"component": "hub", "state": "retryable_failure",
                              "error_code": "unknown_failure"}
                if not isinstance(result, dict):
                    result = {"component": "hub", "state": "retryable_failure",
                              "error_code": "invalid_evidence"}
                else:
                    result = dict(result, component="hub")
            self._assert_agent_fence(job_id, fence)
            self._agent_record_result(job_id, result, fence=fence)
        snapshot = self.managed_update_snapshot(job_id)
        rows = {row["component"]: row for row in snapshot["components"]}
        if snapshot["state"] in TERMINAL_SITE_STATES or rows["hub"]["state"] != "current":
            return snapshot

        pending = [name for name in ("image", "voice")
                   if rows[name]["installed"] and rows[name]["state"] != "current"]
        if pending:
            local_registry = [studio for studio in self.monitor.registry
                              if studio.get("machine", "local") == "local"]
            local_monitor = type("AgentManagedMonitor", (), {"registry": local_registry})()
            self._assert_agent_fence(job_id, fence)
            try:
                results = await self._await(self._component_runner(
                    local_monitor,
                    {"components": bundle["components"]},
                    operation_id=operation_id,
                ))
            except LeaseLostError:
                raise
            except Exception:
                results = []
            self._assert_agent_fence(job_id, fence)
            seen = set()
            for result in results if isinstance(results, list) else []:
                if not isinstance(result, dict):
                    continue
                component = result.get("component") or result.get("modality")
                if component in pending:
                    seen.add(component)
                    self._agent_record_result(job_id, result, fence=fence)
            for component in set(pending) - seen:
                self._agent_record_result(job_id, {
                    "component": component,
                    "state": "retryable_failure",
                    "error_code": "invalid_evidence",
                }, fence=fence)
        return self.managed_update_snapshot(job_id)

    async def _agent_heartbeat(
        self, job_id: str, generation: int, lost: asyncio.Event,
    ) -> None:
        while True:
            try:
                refreshed = self._refresh_agent_execution(job_id, generation)
            except Exception:
                refreshed = False
            if not refreshed:
                lost.set()
                return
            await asyncio.sleep(self._heartbeat_seconds)

    async def _run_managed_update_once(self, job_id: str) -> dict[str, Any]:
        generation = self._claim_agent_execution(job_id)
        if generation is None:
            return self.managed_update_snapshot(job_id)
        lost = asyncio.Event()
        heartbeat = asyncio.create_task(self._agent_heartbeat(job_id, generation, lost))
        execution = asyncio.create_task(self._execute_managed_update(job_id, generation))
        lost_wait = asyncio.create_task(lost.wait())
        try:
            done, _pending = await asyncio.wait(
                {execution, lost_wait}, return_when=asyncio.FIRST_COMPLETED,
            )
            if lost_wait in done and lost.is_set() and not execution.done():
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)
                raise RuntimeError("managed child execution lease was lost")
            return await execution
        finally:
            heartbeat.cancel()
            lost_wait.cancel()
            await asyncio.gather(heartbeat, lost_wait, return_exceptions=True)
            snapshot = self.managed_update_snapshot(job_id)
            if snapshot["state"] not in TERMINAL_SITE_STATES:
                self._release_agent_execution(job_id, generation)

    def _ensure_agent_task(self, job_id: str) -> asyncio.Task:
        task = self._agent_tasks.get(job_id)
        if task is None or task.done():
            task = asyncio.create_task(self._run_managed_update_once(job_id))
            self._agent_tasks[job_id] = task

            def finished(done: asyncio.Task) -> None:
                if self._agent_tasks.get(job_id) is done:
                    self._agent_tasks.pop(job_id, None)
                if not done.cancelled():
                    done.exception()

            task.add_done_callback(finished)
        return task

    def admit_and_schedule_managed_update(self, bundle: dict[str, Any]) -> dict[str, Any]:
        """Persist an idempotent admission, then ensure that same child is running."""
        admission = self.admit_managed_update(bundle)
        self._ensure_agent_task(admission["job_id"])
        return admission

    async def run_managed_update(self, job_id: str) -> dict[str, Any]:
        """Execute or adopt one persisted agent child without duplicate work."""
        _identifier(job_id, "managed child job id", opaque=True)
        return await asyncio.shield(self._ensure_agent_task(job_id))

    def _owns_adoption(self, job_id: str) -> bool:
        state = self._read()
        job = self._find_job(state, job_id)
        lease = job["adoption_lease"]
        return bool(
            lease
            and lease["owner_id"] == self._owner_id
            and lease["pid"] == self._pid
            and lease["expires_at"] > self._clock()
        )

    def _site_generation(self, job_id: str) -> int:
        job = self._find_job(self._read(), job_id)
        lease = job["adoption_lease"]
        if lease is None:
            raise LeaseLostError("release execution lease was lost")
        self._require_site_fence(job, lease["generation"])
        return lease["generation"]

    def _ordered_machines(self, job: dict[str, Any]) -> list[str]:
        remote = sorted(
            machine for machine in job["machines"]
            if machine != "local" and job["machines"][machine]["state"] != "excluded"
        )
        reachable = []
        for machine in remote:
            try:
                snapshot = self._peer_reader(machine) or {}
            except Exception:
                snapshot = {}
            if snapshot.get("reachable") is True and snapshot.get("auth", True) is True:
                reachable.append(machine)
        if reachable:
            canary = reachable[0]
            remote = [canary, *(machine for machine in remote if machine != canary)]
        return (
            [*remote, "local"]
            if "local" in job["machines"]
            and job["machines"]["local"]["state"] != "excluded"
            else remote
        )

    @staticmethod
    async def _await(value: Any) -> Any:
        if hasattr(value, "__await__"):
            return await value
        return value

    def _machine_host(self, machine: str) -> str:
        for studio in self.monitor.registry:
            if studio.get("machine", "local") == machine:
                return str(studio["host"])
        raise ValueError("unknown stable machine")

    def _remote_identity_matches(self, machine: str, response: dict[str, Any]) -> bool:
        if self._identity_reader is None:
            return True
        try:
            expected = self._identity_reader()
        except Exception:
            return False
        return bool(
            isinstance(expected, dict)
            and expected.get("role") == "controller"
            and isinstance(expected.get("site_id"), str)
            and response.get("role") == "agent"
            and response.get("site_id") == expected["site_id"]
            and response.get("controller_id") == machine
        )

    async def _request_remote_bundle(
        self,
        machine: str,
        body: dict[str, Any],
        existing_job_id: str | None,
        persist_child: Callable[[str], Any] | None = None,
        fence_guard: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Idempotently re-admit one operation, persist its child, then observe it."""
        base = f"http://{self._machine_host(machine)}:{peers.DEFAULT_HUB_PORT}"
        headers = {"X-Hub-Token": peers.fleet_token() or ""}
        deadline = time.monotonic() + self._execution_timeout
        child_id = existing_job_id
        expected_child_id = _agent_job_id(body["operation_id"])
        replayed = child_id is not None
        admitted_ok = False
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=8.0)) as client:
            if fence_guard is not None:
                fence_guard()
            while not admitted_ok and time.monotonic() < deadline:
                try:
                    if fence_guard is not None:
                        fence_guard()
                    response = await client.post(
                        f"{base}/api/hub/maintenance/managed-update",
                        headers=headers,
                        json=body,
                    )
                    if fence_guard is not None:
                        fence_guard()
                    response.raise_for_status()
                    admitted = response.json()
                    if not isinstance(admitted, dict):
                        raise ValueError("managed child admission must be an object")
                    if not self._remote_identity_matches(machine, admitted):
                        return {
                            "state": "auth_blocked",
                            "error_code": "identity_mismatch",
                        }
                    returned_id = _identifier(
                        admitted.get("job_id"), "agent_job_id", opaque=True,
                    )
                    if returned_id != expected_child_id or (
                        child_id is not None and returned_id != child_id
                    ):
                        raise ValueError("managed child identity is not deterministic")
                    if replayed and admitted.get("adopted") is not True:
                        raise ValueError("managed child replay was not adopted")
                    child_id = returned_id
                    if persist_child is not None:
                        persist_child(child_id)
                    admitted_ok = True
                except (httpx.TransportError, httpx.TimeoutException):
                    replayed = True
                    await asyncio.sleep(self._poll_seconds)
                    if fence_guard is not None:
                        fence_guard()
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code in {401, 403}:
                        return {"state": "auth_blocked", "error_code": "auth_rejected"}
                    if exc.response.status_code == 409:
                        return {"state": "pending_busy", "error_code": "busy"}
                    return {"state": "retryable_failure", "error_code": "update_refused"}
            if not admitted_ok or child_id is None:
                return {"state": "pending_offline", "error_code": "transport_unavailable"}
            while time.monotonic() < deadline:
                try:
                    if fence_guard is not None:
                        fence_guard()
                    response = await client.get(
                        f"{base}/api/hub/maintenance/managed-update/{child_id}",
                        headers=headers,
                    )
                    if fence_guard is not None:
                        fence_guard()
                    response.raise_for_status()
                    snapshot = response.json()
                    if not isinstance(snapshot, dict):
                        raise ValueError("managed child snapshot must be an object")
                    if not self._remote_identity_matches(machine, snapshot):
                        return {
                            "job_id": child_id,
                            "state": "auth_blocked",
                            "error_code": "identity_mismatch",
                        }
                    snapshot.setdefault("job_id", child_id)
                    if snapshot.get("state") in TERMINAL_SITE_STATES | {"degraded"}:
                        return snapshot
                except (httpx.TransportError, httpx.TimeoutException):
                    pass
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code in {401, 403}:
                        return {"job_id": child_id, "state": "auth_blocked",
                                "error_code": "auth_rejected"}
                    return {"job_id": child_id, "state": "retryable_failure",
                            "error_code": "update_refused"}
                await asyncio.sleep(self._poll_seconds)
                if fence_guard is not None:
                    fence_guard()
        return {
            "job_id": child_id,
            "state": "pending_offline",
            "error_code": "transport_unavailable",
        }

    @staticmethod
    def _component_results(result: dict[str, Any]) -> list[dict[str, Any]]:
        components = result.get("components")
        if isinstance(components, list):
            return [row for row in components if isinstance(row, dict)]
        return []

    def _validated_remote_results(
        self, result: dict[str, Any], machine: dict[str, Any],
    ) -> list[dict[str, Any]] | None:
        rows = self._component_results(result)
        if len(rows) != len(_REPOSITORIES):
            return None
        by_component: dict[str, dict[str, Any]] = {}
        for row in rows:
            component = row.get("component") or row.get("modality")
            if component not in _REPOSITORIES or component in by_component:
                return None
            raw_state = row.get("state") or row.get("status")
            if raw_state not in COMPONENT_STATES | {"complete", "succeeded", "failed"}:
                return None
            expected_installed = machine["components"][component]["installed"]
            if (expected_installed and raw_state == "not_installed") or (
                not expected_installed and raw_state != "not_installed"
            ):
                return None
            error_code = row.get("error_code")
            if error_code is not None and error_code not in _ERROR_DETAILS:
                return None
            observed_release = row.get("observed_release_id")
            if observed_release is not None and (
                not isinstance(observed_release, str)
                or not _RELEASE_ID_RE.fullmatch(observed_release)
            ):
                return None
            for key, pattern in (
                ("observed_version", _SEMVER_RE),
                ("observed_commit", _COMMIT_RE),
            ):
                value = row.get(key)
                if value is not None and (not isinstance(value, str) or not pattern.fullmatch(value)):
                    return None
            if expected_installed and _result_state(row) == "current":
                attested_version = (
                    row.get("observed_version")
                    or row.get("target_version")
                    or row.get("to_version")
                )
                attested_commit = row.get("observed_commit") or row.get("target_commit")
                if (
                    not isinstance(attested_version, str)
                    or not _SEMVER_RE.fullmatch(attested_version)
                    or not isinstance(attested_commit, str)
                    or not _COMMIT_RE.fullmatch(attested_commit)
                ):
                    return None
            by_component[component] = row
        if set(by_component) != set(_REPOSITORIES):
            return None
        return [by_component[name] for name in ("hub", "image", "voice")]

    def _block_remaining(
        self, job_id: str, error_code: str, *, fence: int,
    ) -> None:
        now = _finite_time(self._clock(), "release block time")

        def mutate(state: dict[str, Any]) -> None:
            job = self._find_job(state, job_id)
            self._require_site_fence(job, fence)
            if job["state"] in TERMINAL_SITE_STATES:
                return
            for machine in job["machines"].values():
                for row in machine["components"].values():
                    if row["installed"] and row["state"] != "current":
                        row.update(
                            state="release_blocked",
                            error_code=error_code,
                            detail=_ERROR_DETAILS[error_code],
                            next_retry=None,
                        )
                machine["state"] = _machine_summary(machine)
            job.update(
                state="blocked_release",
                finished_at=now,
                next_retry=None,
                adoption_lease=None,
            )

        self._write(mutate, after_commit=lambda _candidate, _result: self._clear_path_owner())

    def _record_result(
        self,
        job_id: str,
        machine: str,
        result: dict[str, Any],
        *,
        fence: int,
    ) -> bool:
        component = result.get("component") or result.get("modality")
        if component not in _REPOSITORIES:
            return False
        state = _result_state(result)
        error_code = _result_error_code(result, state)
        observed_version = (result.get("observed_version") or result.get("target_version")
                            or result.get("to_version"))
        observed_commit = result.get("observed_commit") or result.get("target_commit")
        job = self.job_snapshot(job_id)
        expected = job["machines"][machine]["components"][component]
        valid_attestation = (
            isinstance(observed_version, str)
            and _SEMVER_RE.fullmatch(observed_version)
            and isinstance(observed_commit, str)
            and _COMMIT_RE.fullmatch(observed_commit)
        )
        if state == "current":
            if not valid_attestation:
                state = "retryable_failure"
                error_code = "invalid_evidence"
                observed_version = observed_commit = None
            elif (observed_version != expected["expected_version"]
                  or observed_commit != expected["expected_commit"]):
                self._block_remaining(job_id, "sha_mismatch", fence=fence)
                return True
        observed_release = result.get("observed_release_id")
        if (
            isinstance(observed_release, str)
            and _RELEASE_ID_RE.fullmatch(observed_release)
            and observed_release != job["release_id"]
        ):
            self._block_remaining(job_id, "manifest_mismatch", fence=fence)
            return True
        explicit_version = result.get("observed_version")
        explicit_commit = result.get("observed_commit")
        valid_explicit_tuple = (
            isinstance(explicit_version, str)
            and _SEMVER_RE.fullmatch(explicit_version)
            and isinstance(explicit_commit, str)
            and _COMMIT_RE.fullmatch(explicit_commit)
        )
        if valid_explicit_tuple and (
            explicit_version != expected["expected_version"]
            or explicit_commit != expected["expected_commit"]
        ):
            self._block_remaining(job_id, "sha_mismatch", fence=fence)
            return True
        if state == "release_blocked":
            state = "retryable_failure"
        kwargs: dict[str, Any] = {"state": state}
        if state == "current":
            kwargs.update(
                observed_version=observed_version,
                observed_commit=observed_commit,
            )
        elif state in RETRYABLE_COMPONENT_STATES:
            kwargs["error_code"] = error_code
        clean_failure = (
            state in RETRYABLE_COMPONENT_STATES
            and error_code == "clean_checkout_health_failure"
        )
        self.record_component(
            job_id, machine, component, fence=fence,
            _clean_failure=clean_failure, **kwargs,
        )
        return clean_failure and self.job_snapshot(job_id)["state"] == "blocked_release"

    def _mark_machine_pending(
        self, job_id: str, machine: str, state: str, error_code: str, *, fence: int,
    ) -> None:
        job = self.job_snapshot(job_id)
        for component, row in job["machines"][machine]["components"].items():
            if row["installed"] and row["state"] != "current":
                self.record_component(
                    job_id, machine, component, state=state, error_code=error_code,
                    fence=fence,
                )

    def _exclude_machine_if_disabled(
        self, job_id: str, machine: str, *, fence: int,
    ) -> bool:
        if registry.machine_enabled(machine):
            return False

        def mutate(state: dict[str, Any]) -> bool:
            job = self._find_job(state, job_id)
            self._require_site_fence(job, fence)
            if job["state"] in TERMINAL_SITE_STATES:
                return True
            machine_row = job["machines"].get(machine)
            if machine_row is None:
                return True
            for row in machine_row["components"].values():
                if row["installed"] and row["state"] != "current":
                    row.update(
                        state="excluded_disabled",
                        observed_version=None,
                        observed_commit=None,
                        error_code=None,
                        detail=None,
                        next_retry=None,
                    )
            machine_row["state"] = _machine_summary(machine_row)
            _refresh_job(job)
            return True

        return self._write(mutate)

    def _sync_disabled_machines(self, job_id: str, *, fence: int) -> None:
        for machine in self.job_snapshot(job_id)["machines"]:
            self._exclude_machine_if_disabled(job_id, machine, fence=fence)

    async def _run_remote_machine(
        self, job_id: str, machine: str, manifest: dict[str, Any], *, fence: int,
    ) -> bool:
        self._assert_site_fence(job_id, fence)
        if self._exclude_machine_if_disabled(job_id, machine, fence=fence):
            return False
        try:
            peer = self._peer_reader(machine) or {}
        except Exception:
            peer = {}
        if peer.get("auth") is False:
            self._mark_machine_pending(
                job_id, machine, "auth_blocked", "auth_rejected", fence=fence,
            )
            return False
        if peer and peer.get("reachable") is not True:
            self._mark_machine_pending(
                job_id, machine, "pending_offline", "offline", fence=fence,
            )
            return False
        machine_row = self.job_snapshot(job_id)["machines"][machine]
        eligible = {"checking", "updating", "restarting", "verifying"}
        if not any(row["installed"] and row["state"] in eligible
                   for row in machine_row["components"].values()):
            return False
        body = {
            "release_id": manifest["release_id"],
            "operation_id": machine_row["operation_id"],
            "components": deepcopy(manifest["components"]),
        }
        try:
            if self._uses_default_remote_runner:
                result = await self._request_remote_bundle(
                    machine,
                    body,
                    machine_row["agent_job_id"],
                    lambda child_id: self.persist_remote_job(
                        job_id, machine, child_id, fence=fence,
                    ),
                    lambda: self._assert_site_fence(job_id, fence),
                )
            else:
                self._assert_site_fence(job_id, fence)
                result = await self._await(self._remote_bundle_runner(
                    machine, body, machine_row["agent_job_id"],
                ))
                self._assert_site_fence(job_id, fence)
        except LeaseLostError:
            raise
        except (httpx.TransportError, httpx.TimeoutException):
            self._mark_machine_pending(
                job_id, machine, "pending_offline", "transport_unavailable", fence=fence,
            )
            return False
        except ValueError:
            self._mark_machine_pending(
                job_id, machine, "retryable_failure", "invalid_evidence", fence=fence,
            )
            return False
        except Exception:
            self._mark_machine_pending(
                job_id, machine, "retryable_failure", "unknown_failure", fence=fence,
            )
            return False
        if not isinstance(result, dict):
            self._mark_machine_pending(
                job_id, machine, "retryable_failure", "invalid_evidence", fence=fence,
            )
            return False
        child_id = result.get("job_id")
        if child_id is not None:
            self.persist_remote_job(job_id, machine, child_id, fence=fence)
        rows = self._validated_remote_results(result, machine_row)
        if not rows:
            if not any(key in result for key in ("components", "machines")):
                result_state = _result_state(result)
                if result_state == "release_blocked":
                    result_state = "retryable_failure"
                retry_state = (
                    result_state
                    if result_state in RETRYABLE_COMPONENT_STATES
                    else "retryable_failure"
                )
                error_code = _result_error_code(result, retry_state)
                self._mark_machine_pending(
                    job_id, machine, retry_state,
                    error_code if error_code in _ERROR_DETAILS else "invalid_evidence",
                    fence=fence,
                )
                return False
            self._mark_machine_pending(
                job_id, machine, "retryable_failure", "invalid_evidence", fence=fence,
            )
            return False
        return any(
            self._record_result(job_id, machine, row, fence=fence) for row in rows
        )

    async def _run_local_machine(
        self, job_id: str, manifest: dict[str, Any], *, fence: int,
    ) -> bool:
        self._assert_site_fence(job_id, fence)
        machine = "local"
        if self._exclude_machine_if_disabled(job_id, machine, fence=fence):
            return False
        row = self.job_snapshot(job_id)["machines"][machine]
        local_registry = [studio for studio in self.monitor.registry
                          if studio.get("machine", "local") == "local"]
        local_monitor = type("LocalManagedMonitor", (), {"registry": local_registry})()
        eligible = {"checking", "updating", "restarting", "verifying"}
        pending = [name for name in ("image", "voice")
                   if row["components"][name]["installed"]
                   and row["components"][name]["state"] in eligible]
        if pending:
            self._assert_site_fence(job_id, fence)
            try:
                results = await self._await(self._component_runner(
                    local_monitor, manifest, operation_id=row["operation_id"],
                ))
            except LeaseLostError:
                raise
            except Exception:
                results = []
            self._assert_site_fence(job_id, fence)
            seen = set()
            for result in results if isinstance(results, list) else []:
                if not isinstance(result, dict):
                    continue
                component = result.get("component") or result.get("modality")
                if component in pending:
                    seen.add(component)
                    if self._record_result(job_id, machine, result, fence=fence):
                        return True
            for component in set(pending) - seen:
                self.record_component(
                    job_id, machine, component,
                    state="retryable_failure", error_code="invalid_evidence",
                    fence=fence,
                )
        hub = self.job_snapshot(job_id)["machines"][machine]["components"]["hub"]
        if hub["state"] in eligible:
            if self._exclude_machine_if_disabled(job_id, machine, fence=fence):
                return False
            if (self._loaded_version == hub["expected_version"]
                    and self._loaded_commit == hub["expected_commit"]):
                self.record_component(
                    job_id, machine, "hub", state="current",
                    observed_version=self._loaded_version,
                    observed_commit=self._loaded_commit,
                    fence=fence,
                )
            elif self._hub_runner is None:
                self.record_component(
                    job_id, machine, "hub", state="retryable_failure",
                    error_code="updater_unavailable",
                    fence=fence,
                )
            else:
                target = manifest["components"]["hub"]
                self._assert_site_fence(job_id, fence)
                try:
                    result = await self._await(self._hub_runner(target, row["operation_id"]))
                except LeaseLostError:
                    raise
                except Exception:
                    result = {"component": "hub", "state": "retryable_failure",
                              "error_code": "unknown_failure"}
                if not isinstance(result, dict):
                    result = {"component": "hub", "state": "retryable_failure",
                              "error_code": "invalid_evidence"}
                else:
                    result = dict(result, component="hub")
                self._assert_site_fence(job_id, fence)
                if self._record_result(job_id, machine, result, fence=fence):
                    return True
        return False

    async def _request_catalog(self, job_id: str, *, fence: int) -> bool:
        catalog = self.job_snapshot(job_id)["catalog"]
        if catalog["state"] == "acknowledged":
            return True
        if catalog["state"] == "retryable_failure":
            return False
        now = _finite_time(self._clock(), "catalog dispatch time")

        def dispatch(state: dict[str, Any]) -> str:
            job = self._find_job(state, job_id)
            self._require_site_fence(job, fence)
            row = job["catalog"]
            if row["state"] == "acknowledged":
                return row["operation_id"]
            if row["state"] not in {"pending", "requesting"}:
                raise ValueError("catalog request is not due")
            row.update(
                state="requesting",
                attempt=row["attempt"] + 1,
                requested_at=row["requested_at"] or now,
                next_retry=None,
            )
            return row["operation_id"]

        operation_id = self._write(dispatch)
        request_evidence: dict[str, Any] = {}
        try:
            self._assert_site_fence(job_id, fence)
            if self._catalog_requester is not None:
                result = await self._await(self._catalog_requester(operation_id))
                if result is not None:
                    if not isinstance(result, dict):
                        raise ValueError("catalog requester returned invalid evidence")
                    returned_operation = result.get("operation_id")
                    if returned_operation is not None and returned_operation != operation_id:
                        raise ValueError("catalog requester changed operation identity")
                    revision = result.get("requested_revision")
                    count = result.get("requested_models")
                    if revision is not None and (
                        not isinstance(revision, str)
                        or not re.fullmatch(r"[0-9a-f]{64}", revision)
                    ):
                        raise ValueError("catalog requester returned invalid revision")
                    if count is not None and (
                        isinstance(count, bool) or not isinstance(count, int) or count < 0
                    ):
                        raise ValueError("catalog requester returned invalid model count")
                    request_evidence = {
                        "requested_revision": revision,
                        "requested_models": count,
                    }
            self._assert_site_fence(job_id, fence)
        except LeaseLostError:
            raise
        except Exception:
            failed_at = _finite_time(self._clock(), "catalog failure time")

            def fail(state: dict[str, Any]) -> None:
                job = self._find_job(state, job_id)
                self._require_site_fence(job, fence)
                row = job["catalog"]
                row.update(
                    state="retryable_failure",
                    next_retry=failed_at + retry_delay(row["attempt"]),
                    acknowledged_at=None,
                )
                _refresh_job(job)

            self._write(fail)
            return False

        acknowledged_at = _finite_time(self._clock(), "catalog acknowledgement time")

        def acknowledge(state: dict[str, Any]) -> None:
            job = self._find_job(state, job_id)
            self._require_site_fence(job, fence)
            row = job["catalog"]
            if row["operation_id"] != operation_id:
                raise ValueError("catalog operation identity changed")
            row.update(
                state="acknowledged", next_retry=None,
                acknowledged_at=acknowledged_at,
                **request_evidence,
            )
            _refresh_job(job)

        self._write(acknowledge)
        return True

    async def _execute(
        self, job_id: str, manifest: dict[str, Any], *, fence: int,
    ) -> dict[str, Any]:
        self._sync_disabled_machines(job_id, fence=fence)
        job = self.job_snapshot(job_id)
        for machine in self._ordered_machines(job):
            self._assert_site_fence(job_id, fence)
            if self.job_snapshot(job_id)["state"] in TERMINAL_SITE_STATES:
                break
            if self._exclude_machine_if_disabled(job_id, machine, fence=fence):
                continue
            blocked = (
                await self._run_local_machine(job_id, manifest, fence=fence)
                if machine == "local"
                else await self._run_remote_machine(
                    job_id, machine, manifest, fence=fence,
                )
            )
            if blocked:
                break
        job = self.job_snapshot(job_id)
        if job["state"] not in TERMINAL_SITE_STATES and _job_is_converged(job):
            if await self._request_catalog(job_id, fence=fence):
                self.persist_job(job_id, state="complete", fence=fence)
        return self.job_snapshot(job_id)

    async def _heartbeat(
        self, job_id: str, generation: int, lost: asyncio.Event,
    ) -> None:
        while True:
            try:
                refreshed = self.refresh_adoption(job_id, generation=generation)
            except Exception:
                refreshed = False
            if not refreshed:
                lost.set()
                return
            await asyncio.sleep(self._heartbeat_seconds)

    async def _run_release(self, release_id: str) -> dict[str, Any]:
        desired = self.intent_snapshot()
        if not desired or desired["manifest"]["release_id"] != release_id:
            raise ValueError("release_id is not the current desired intent")
        self.reconcile_registry()
        state = self.state_snapshot()
        activation = state["activation"]
        if not activation or activation["release_id"] != release_id:
            raise ValueError("release is not activated")
        job_id = activation["job_id"]
        if state["jobs"][job_id]["state"] in TERMINAL_SITE_STATES:
            return state["jobs"][job_id]
        if not self._owns_adoption(job_id) and self.resume_pending() != 1:
            raise RuntimeError("release execution lease is owned by another process")
        generation = self._site_generation(job_id)
        lost = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat(job_id, generation, lost))
        execution = asyncio.create_task(
            self._execute(job_id, desired["manifest"], fence=generation)
        )
        lost_wait = asyncio.create_task(lost.wait())
        try:
            done, _pending = await asyncio.wait(
                {execution, lost_wait}, return_when=asyncio.FIRST_COMPLETED,
            )
            if lost_wait in done and lost.is_set() and not execution.done():
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)
                raise RuntimeError("release execution lease was lost")
            return await execution
        finally:
            heartbeat.cancel()
            lost_wait.cancel()
            await asyncio.gather(heartbeat, lost_wait, return_exceptions=True)
            if self.job_snapshot(job_id)["state"] not in TERMINAL_SITE_STATES:
                self.release_adoption(job_id, generation=generation)

    async def run(self, release_id: str) -> dict[str, Any]:
        """Execute or adopt one lease-owned serial controller release."""
        task = self._site_tasks.get(release_id)
        if task is None or task.done():
            task = asyncio.create_task(self._run_release(release_id))
            self._site_tasks[release_id] = task
        try:
            return await asyncio.shield(task)
        finally:
            if task.done() and self._site_tasks.get(release_id) is task:
                self._site_tasks.pop(release_id, None)

    def job_snapshot(self, job_id: str) -> dict[str, Any]:
        _identifier(job_id, "job id")
        return self._public_job(self._find_job(self._read(), job_id))

    def intent_snapshot(self) -> dict[str, Any] | None:
        return deepcopy(self._read()["desired"])

    def state_snapshot(self) -> dict[str, Any]:
        state = deepcopy(self._read())
        state["jobs"] = {
            job_id: self._public_job(job) for job_id, job in state["jobs"].items()
        }
        return state

    def capability_evidence(self) -> dict[str, Any]:
        """Return bounded managed-release facts safe for routing and display."""
        state = self._read()
        desired = state["desired"]
        if desired is None:
            return {
                "desired": None,
                "activation": None,
                "site_state": None,
                "next_retry": None,
                "canary_machine_id": None,
                "controller": None,
                "machines": {},
                "catalog": None,
            }

        manifest = desired["manifest"]
        release_id = manifest["release_id"]
        loaded_version = (
            self._loaded_version
            if isinstance(self._loaded_version, str)
            and _SEMVER_RE.fullmatch(self._loaded_version)
            else None
        )
        loaded_commit = (
            self._loaded_commit
            if isinstance(self._loaded_commit, str)
            and _COMMIT_RE.fullmatch(self._loaded_commit)
            else None
        )
        targets = {
            name: {
                "expected_version": component["version"],
                "expected_commit": component["commit"],
            }
            for name, component in manifest["components"].items()
        }
        public_desired = {
            "release_id": release_id,
            "sequence": manifest["sequence"],
            "created_at": manifest["created_at"],
            "received_at": desired["received_at"],
            "components": targets,
        }
        activation = state["activation"]
        if activation is None or activation["release_id"] != release_id:
            hub = {
                "component": "hub",
                "desired_release_id": release_id,
                **targets["hub"],
                "observed_version": loaded_version,
                "observed_commit": loaded_commit,
                "state": "checking",
                "next_retry": None,
                "converged": False,
            }
            return {
                "desired": public_desired,
                "activation": None,
                "site_state": "pending",
                "next_retry": None,
                "canary_machine_id": None,
                "controller": hub,
                "machines": {},
                "catalog": None,
            }

        job = self._find_job(state, activation["job_id"])

        def component_evidence(name: str, row: dict[str, Any], *, local: bool) -> dict[str, Any]:
            observed_version = row["observed_version"]
            observed_commit = row["observed_commit"]
            if local and name == "hub":
                observed_version = observed_version or loaded_version
                observed_commit = observed_commit or loaded_commit
            return {
                "component": name,
                "desired_release_id": release_id,
                "expected_version": row["expected_version"],
                "expected_commit": row["expected_commit"],
                "observed_version": observed_version,
                "observed_commit": observed_commit,
                "state": row["state"],
                "next_retry": row["next_retry"],
                "converged": bool(
                    row["state"] == "current"
                    and observed_version == row["expected_version"]
                    and observed_commit == row["expected_commit"]
                ),
            }

        machines: dict[str, dict[str, Any]] = {}
        for machine_id, machine in job["machines"].items():
            components = {
                name: component_evidence(
                    name, row, local=machine_id == "local",
                )
                for name, row in machine["components"].items()
            }
            machines[machine_id] = {
                "desired_release_id": release_id,
                "state": machine["state"],
                "next_retry": min(
                    (row["next_retry"] for row in components.values()
                     if row["next_retry"] is not None),
                    default=None,
                ),
                "converged": all(
                    (not machine["components"][name]["installed"]
                     and row["state"] == "not_installed")
                    or (machine["components"][name]["installed"] and row["converged"])
                    for name, row in components.items()
                ),
                "components": components,
            }

        canary = None
        for machine_id in sorted(
            machine for machine in machines
            if machine != "local" and machines[machine]["state"] != "excluded"
        ):
            try:
                peer = self._peer_reader(machine_id) or {}
            except Exception:
                peer = {}
            if peer.get("reachable") is True and peer.get("auth", True) is True:
                canary = machine_id
                break

        catalog = job["catalog"]
        public_catalog = {
            "state": catalog["state"],
            "requested_at": catalog["requested_at"],
            "acknowledged_at": catalog["acknowledged_at"],
            "requested_revision": catalog["requested_revision"],
            "requested_models": catalog["requested_models"],
            "next_retry": catalog["next_retry"],
        }
        public_activation = {
            "release_id": activation["release_id"],
            "job_id": activation["job_id"],
            "activated_at": activation["activated_at"],
        }
        return {
            "desired": public_desired,
            "activation": public_activation,
            "site_state": job["state"],
            "next_retry": job["next_retry"],
            "canary_machine_id": canary,
            "controller": (machines.get("local") or {}).get("components", {}).get("hub"),
            "machines": machines,
            "catalog": public_catalog,
        }

    @staticmethod
    def _public_job(job: dict[str, Any]) -> dict[str, Any]:
        public = deepcopy(job)
        public["adoption"] = {"claimed": public.pop("adoption_lease") is not None}
        public.pop("lease_generation")
        public.pop("clean_failure_machines")
        return public
