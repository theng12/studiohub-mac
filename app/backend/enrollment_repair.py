"""Controller-side selection, ticket issuance, and redemption coordination."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import math
import re
import secrets
import socket
import threading
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from . import control_plane, peers, registry
from .controller_settings_lock import SettingsWriterBusy, settings_writer_lock
from .enrollment_repair_store import (
    ControllerIdentity,
    RepairStore,
    RepairStoreError,
    TargetIdentity,
)
from .enrollment_repair_transport import (
    PinnedTransportError,
    canonical_private_address,
    open_pinned_json,
    resolve_private_origin,
)


NEW_ISSUANCE_ENABLED = True
TICKET_LIFETIME_SECONDS = 120.0
DISPATCH_TIMEOUT_SECONDS = 15.0
_URLSAFE_TICKET = re.compile(r"^[A-Za-z0-9_-]{43,256}$")
_REDEMPTION_KEYS = {
    "schema", "schema_version", "request_id", "target_machine_id", "ticket",
    "redemption_expires_at", "observed_identity",
}
_OBSERVED_IDENTITY_KEYS = {
    "role", "site_id", "site_name", "controller_id", "parent_controller_url",
}
_AGENT_STATES = {
    "accepted", "redemption_attempted", "applying", "complete", "never_applied",
    "needs_review",
}
_AGENT_ERROR_CODES = {
    "callback_source_mismatch", "callback_url_invalid", "claim_invalid",
    "database_mode_unsafe", "dispatch_invalid", "environment_locked",
    "fleet_token_unavailable", "journal_invalid", "request_conflict",
    "request_not_found", "settings_invalid", "settings_preimage_changed",
    "settings_state_ambiguous", "settings_writer_busy", "ticket_expired",
    "transport_unavailable",
}
_BOOTSTRAP_REQUIRED_GATES = (
    "target_exact", "token_exact", "update_supported", "restart_verifiable",
    "hub_idle", "studios_idle", "enabled", "conflict_free",
)
_SAFE_CONTAINING_RELEASE_STATES = {"complete", "blocked_release"}


@dataclass(frozen=True)
class ResolvedRegistryRows:
    rows: tuple[tuple[str, str, str, int | None], ...]
    addresses: tuple[tuple[str, str | None], ...]


@dataclass(frozen=True)
class EnrollmentRegistrationResolution:
    machine: str
    host: str
    address: str | None
    registry: ResolvedRegistryRows

_DETAIL = {
    "eligible": "Eligible for enrollment repair.",
    "fleet_token_missing": "A current fleet token is required.",
    "machine_local": "The local Controller cannot repair itself.",
    "machine_missing": "This registered machine is no longer present.",
    "machine_multi_host": "This machine has conflicting registered hosts.",
    "host_shared": "This host is shared by more than one registered machine.",
    "address_shared": "This private address is shared by more than one registered machine.",
    "host_address_ambiguous": "The registered host does not resolve to one private address.",
    "host_address_changed": "The registered host address changed and needs review.",
    "host_missing": "This machine has no registered host.",
    "host_invalid": "This machine has an invalid registered host.",
    "endpoint_missing": "This machine has an incomplete registered endpoint.",
    "endpoint_machine_conflict": "This machine has conflicting endpoint identity.",
    "endpoint_duplicate": "This machine has duplicate endpoint identity.",
    "issuance_disabled": "New repair issuance is currently disabled.",
    "controller_role_required": "Enrollment repair is available only on a Controller.",
}


class EnrollmentRepairCoordinator:
    """Owner selection plus the short authority-bearing Controller boundary."""

    def __init__(
        self,
        store: RepairStore,
        *,
        registry_loader: Callable[[], Sequence[dict[str, Any]]] = registry.load_registry,
        token_reader: Callable[[], str | None] = peers.current_fleet_token,
        settings_reader: Callable[[], dict[str, Any]] = control_plane.load_settings,
        resolver: Callable[..., Any] = socket.getaddrinfo,
        connection_factory: Callable[..., Any] = open_pinned_json,
        clock: Callable[[], float] | None = None,
        ticket_factory: Callable[[], str] | None = None,
        foreground_lock: asyncio.Lock | None = None,
        peer_invalidator: Callable[[str], None] = peers.invalidate,
        wake_peer: Callable[[str], Any] | None = None,
        capability_probe: Callable[[str, str, str], Any] | None = None,
        bootstrap_gate_reader: Callable[[str, str, str], Any] | None = None,
        hub_updater: Callable[[str, str, str], Any] | None = None,
        mutation_lock: threading.RLock | None = None,
        process_stop_verifier: Callable[[str], bool | None] | None = None,
        stopped_recovery: Callable[[], Mapping[str, Any] | None] | None = None,
    ) -> None:
        self.store = store
        self._registry_loader = registry_loader
        self._token_reader = token_reader
        self._settings_reader = settings_reader
        self._resolver = resolver
        self._connection_factory = connection_factory
        self._clock = clock or store.clock
        self._ticket_factory = ticket_factory or (lambda: secrets.token_urlsafe(32))
        self._foreground_lock = foreground_lock or asyncio.Lock()
        self._peer_invalidator = peer_invalidator
        self._wake_peer = wake_peer or self._wake_existing_release_peer
        self._capability_probe = capability_probe
        self._bootstrap_gate_reader = bootstrap_gate_reader
        self._hub_updater = hub_updater
        self._mutation_lock = mutation_lock or threading.RLock()
        self._mutation_state = threading.local()
        self._process_stop_verifier = process_stop_verifier
        self._stopped_recovery = stopped_recovery
        self._scheduler_task: asyncio.Task[None] | None = None
        self._status_task: asyncio.Task[None] | None = None
        self._started = False

    @staticmethod
    def _wake_existing_release_peer(machine: str) -> None:
        reconciler = peers.release_reconciler
        if reconciler is not None:
            reconciler.wake_peer(machine)

    def _rows(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._registry_loader() if isinstance(row, dict)]

    def _request_states(self) -> dict[str, str]:
        # The store intentionally redacts tickets from its public reads. This
        # read contains only target/state fields and makes no mutation.
        with self.store._connect() as connection:
            rows = connection.execute(
                """SELECT target_machine, state FROM enrollment_repair_requests
                   ORDER BY updated_at DESC, created_at DESC"""
            ).fetchall()
        states: dict[str, str] = {}
        for row in rows:
            states.setdefault(row["target_machine"], row["state"])
        return states

    def _live_claim_rows(self, *, redeemed_only: bool = False) -> list[dict[str, Any]]:
        ticket_clause = " AND ticket_status = 'redeemed'" if redeemed_only else ""
        with self.store._connect() as connection:
            rows = connection.execute(
                """SELECT request_id, batch_id, target_machine, registry_host,
                          resolved_address, state, ticket_status, evidence_json,
                          site_id, site_name
                   FROM enrollment_repair_requests
                   WHERE state IN ('redeemed', 'verifying', 'confirmation_pending')"""
                + ticket_clause
                + " ORDER BY redeemed_at ASC, created_at ASC"
            ).fetchall()
        return [dict(row) for row in rows]

    def _snapshot(
        self,
        rows: Sequence[dict[str, Any]],
        machine: str,
        *,
        expected_address: str | None = None,
    ):
        return registry.repair_machine_snapshot(
            rows,
            machine,
            resolver=self._resolver,
            expected_address=expected_address,
        )

    def _controller_identity(self) -> ControllerIdentity:
        settings = self._settings_reader()
        if not isinstance(settings, dict):
            raise RepairStoreError("controller_snapshot_invalid")
        identity = ControllerIdentity(
            role=str(settings.get("role", "")).strip().lower(),
            site_id=str(settings.get("site_id", "")).strip(),
            site_name=str(settings.get("site_name", "")).strip(),
            controller_id=str(settings.get("controller_id", "")).strip(),
        )
        if identity.role != "controller" or not all((
            identity.site_id, identity.site_name, identity.controller_id,
        )):
            raise RepairStoreError("controller_snapshot_invalid")
        return identity

    @staticmethod
    def _origin_for_address(address: str) -> str:
        canonical = canonical_private_address(address)
        display = f"[{canonical}]" if ":" in canonical else canonical
        return f"http://{display}:47873"

    def _target_origin(self, host: str):
        display = f"[{host}]" if ":" in host and not host.startswith("[") else host
        return resolve_private_origin(
            f"http://{display}:47873", resolver=self._resolver,
        )

    @staticmethod
    def _registry_rows(
        rows: Sequence[Mapping[str, Any]],
    ) -> tuple[tuple[str, str, str, int | None], ...]:
        normalized = (
            (
                str(row.get("id", "")),
                str(row.get("machine", "")),
                str(row.get("host", "")),
                row.get("port") if isinstance(row.get("port"), int) else None,
            )
            for row in rows
            if isinstance(row, Mapping)
        )
        return tuple(sorted(normalized, key=repr))

    def resolve_registry_rows(
        self,
        rows: Sequence[Mapping[str, Any]],
    ) -> ResolvedRegistryRows:
        """Resolve registry hosts before entering the short mutation lock."""
        normalized = self._registry_rows(rows)
        addresses = []
        for host in sorted({row[2] for row in normalized if row[2]}):
            try:
                address = self._target_origin(host).address
            except Exception:
                address = None
            addresses.append((host, address))
        return ResolvedRegistryRows(normalized, tuple(addresses))

    @staticmethod
    def _resolved_row_dicts(
        resolved: ResolvedRegistryRows,
    ) -> list[dict[str, Any]]:
        return [
            {"id": identifier, "machine": machine, "host": host, "port": port}
            for identifier, machine, host, port in resolved.rows
        ]

    @staticmethod
    def _resolved_address(
        resolved: ResolvedRegistryRows,
        host: str,
    ) -> str | None:
        return dict(resolved.addresses).get(host)

    def _resolved_snapshot(
        self,
        resolved: ResolvedRegistryRows,
        machine: str,
        *,
        expected_address: str | None = None,
    ):
        addresses = dict(resolved.addresses)

        def resolver(host: str, port: int, *, type: int):
            address = addresses.get(host)
            if address is None:
                raise OSError("host_address_ambiguous")
            return [(socket.AF_INET6 if ":" in address else socket.AF_INET,
                     type, socket.IPPROTO_TCP, "", (address, port))]

        return registry.repair_machine_snapshot(
            self._resolved_row_dicts(resolved),
            machine,
            resolver=resolver,
            expected_address=expected_address,
        )

    def _require_registry_rows_current(self, resolved: ResolvedRegistryRows) -> None:
        if self._registry_rows(self._rows()) != resolved.rows:
            raise RepairStoreError("registry_snapshot_changed")

    def resolve_enrollment_registration(
        self,
        machine: str,
        host: str,
    ) -> EnrollmentRegistrationResolution:
        proposed_machine = str(machine).strip()
        proposed_host = str(host).strip()
        resolved = self.resolve_registry_rows(self._rows())
        address = self._resolved_address(resolved, proposed_host)
        if address is None:
            try:
                address = self._target_origin(proposed_host).address
            except Exception:
                address = None
        return EnrollmentRegistrationResolution(
            proposed_machine, proposed_host, address, resolved,
        )

    @staticmethod
    def _preissuance_failure(exc: BaseException) -> tuple[str, str]:
        """Map a claimed-but-unissued failure to one bounded durable outcome."""
        if isinstance(exc, registry.RepairRegistryAmbiguity):
            return "needs_review", exc.code
        if isinstance(exc, PinnedTransportError):
            code = str(exc)
            if code == "callback_source_mismatch":
                return "needs_review", code
            return "retryable", "transport_unavailable"
        if isinstance(exc, RepairStoreError):
            code = exc.code
            if code == "hub_update_required":
                return "hub_update_required", code
            if code == "fleet_token_changed":
                return "needs_review", "fleet_token_mismatch"
            if code == "controller_snapshot_invalid":
                return "retryable", "controller_snapshot_changed"
            if code in {
                "callback_url_invalid", "fleet_token_missing",
                "fleet_token_mismatch", "host_address_changed",
            }:
                return "needs_review", code
            return "retryable", code
        if isinstance(exc, asyncio.CancelledError):
            return "retryable", "transport_unavailable"
        return "retryable", "transport_unavailable"

    @staticmethod
    async def _await(value: Any) -> Any:
        return await value if inspect.isawaitable(value) else value

    @staticmethod
    def _repair_schema_available(value: Any) -> bool:
        return (
            isinstance(value, Mapping)
            and type(value.get("repair_schema_version")) is int
            and value.get("repair_schema_version") == 1
        )

    async def _ensure_repair_capability(
        self,
        machine: str,
        host: str,
        address: str,
        token: str,
    ) -> None:
        """Use only a fully gated, one-shot ordinary Hub bootstrap."""
        if self._capability_probe is None:
            return
        try:
            capability = await self._await(
                self._capability_probe(machine, host, token)
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise RepairStoreError("hub_update_required") from exc
        if self._repair_schema_available(capability):
            return
        if self._bootstrap_gate_reader is None or self._hub_updater is None:
            raise RepairStoreError("hub_update_required")
        gates = await self._await(
            self._bootstrap_gate_reader(machine, host, address)
        )
        if not isinstance(gates, Mapping):
            raise RepairStoreError("hub_update_required")
        if any(gates.get(name) is not True for name in _BOOTSTRAP_REQUIRED_GATES):
            raise RepairStoreError("hub_update_required")
        contains_target = gates.get("managed_release_contains_target")
        release_state = gates.get("managed_release_state")
        if type(contains_target) is not bool:
            raise RepairStoreError("hub_update_required")
        if (
            contains_target
            and (
                not isinstance(release_state, str)
                or release_state not in _SAFE_CONTAINING_RELEASE_STATES
            )
        ):
            raise RepairStoreError("hub_update_required")
        version = gates.get("published_version")
        if (
            not isinstance(version, str)
            or not version
            or version != version.strip()
            or len(version) > 80
            or version.lower() in {"main", "master", "latest"}
            or type(gates.get("published_repair_schema_version")) is not int
            or gates.get("published_repair_schema_version") != 1
        ):
            raise RepairStoreError("hub_update_required")

        current_token = self._token_reader()
        if (
            not isinstance(current_token, str)
            or not hmac.compare_digest(current_token, token)
        ):
            raise RepairStoreError("hub_update_required")
        current_snapshot = self._snapshot(
            self._rows(), machine, expected_address=address,
        )
        if current_snapshot.registry_host != host:
            raise RepairStoreError("hub_update_required")
        try:
            await self._await(self._hub_updater(machine, version, token))
            capability = await self._await(
                self._capability_probe(machine, host, token)
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise RepairStoreError("hub_update_required") from exc
        if not self._repair_schema_available(capability):
            raise RepairStoreError("hub_update_required")

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _bounded(value: Any, minimum: int, maximum: int) -> str:
        if (not isinstance(value, str) or value != value.strip()
                or not minimum <= len(value) <= maximum):
            raise RepairStoreError("redemption_invalid")
        return value

    def _parse_redemption(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping) or set(payload) != _REDEMPTION_KEYS:
            raise RepairStoreError("redemption_invalid")
        if (payload.get("schema") != "studiohub.enrollment-repair-redemption"
                or type(payload.get("schema_version")) is not int
                or payload.get("schema_version") != 1):
            raise RepairStoreError("redemption_invalid")
        ticket = payload.get("ticket")
        expires = payload.get("redemption_expires_at")
        observed = payload.get("observed_identity")
        if (not isinstance(ticket, str) or not _URLSAFE_TICKET.fullmatch(ticket)
                or isinstance(expires, bool) or not isinstance(expires, (int, float))
                or not math.isfinite(float(expires))
                or not isinstance(observed, Mapping)
                or set(observed) != _OBSERVED_IDENTITY_KEYS):
            raise RepairStoreError("redemption_invalid")
        parent = observed.get("parent_controller_url")
        if parent is not None:
            parent = self._bounded(parent, 1, 500)
        return {
            "request_id": self._bounded(payload.get("request_id"), 16, 128),
            "target_machine_id": self._bounded(payload.get("target_machine_id"), 1, 100),
            "ticket": ticket,
            "redemption_expires_at": float(expires),
            "observed_identity": {
                "role": self._bounded(observed.get("role"), 0, 20),
                "site_id": self._bounded(observed.get("site_id"), 0, 100),
                "site_name": self._bounded(observed.get("site_name"), 0, 120),
                "controller_id": self._bounded(observed.get("controller_id"), 0, 100),
                "parent_controller_url": parent,
            },
        }

    def _issued_controller_url(self, request_id: str) -> str:
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT controller_url FROM enrollment_repair_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        if row is None or not row["controller_url"]:
            raise RepairStoreError("request_not_found")
        return str(row["controller_url"])

    @staticmethod
    def _dispatch_response(
        request_id: str,
        machine: str,
        controller: ControllerIdentity,
        response: Mapping[str, Any],
    ) -> dict[str, Any]:
        state = str(response.get("state", "needs_review"))
        if state not in _AGENT_STATES:
            state = "needs_review"
        result: dict[str, Any] = {"request_id": request_id, "state": state}
        error_code = response.get("error_code")
        if error_code in _AGENT_ERROR_CODES:
            result["error_code"] = error_code
        if response.get("outcome") == "unknown":
            result["outcome"] = "unknown"
        identity = response.get("identity")
        if state == "complete" and isinstance(identity, Mapping):
            fields = {key: identity.get(key) for key in (
                "role", "site_id", "site_name", "controller_id",
            )}
            expected = {
                "role": "agent",
                "site_id": controller.site_id,
                "site_name": controller.site_name,
                "controller_id": machine,
            }
            if fields == expected:
                result["identity"] = fields
        applied_at = response.get("applied_at")
        if (state == "complete" and not isinstance(applied_at, bool)
                and isinstance(applied_at, (int, float)) and math.isfinite(float(applied_at))):
            result["applied_at"] = float(applied_at)
        return result

    def _controller_role(self) -> bool:
        settings = self._settings_reader()
        return isinstance(settings, dict) and str(settings.get("role", "")).strip().lower() == "controller"

    @staticmethod
    def _remote_machines(rows: Sequence[dict[str, Any]]) -> list[str]:
        return sorted({
            str(row.get("machine", "")).strip()
            for row in rows
            if str(row.get("machine", "")).strip() not in {"", "local"}
        })

    def _outcome(
        self,
        rows: Sequence[dict[str, Any]],
        machine: str,
        *,
        token: str | None,
        controller_role: bool,
    ) -> dict[str, Any]:
        if not controller_role:
            snapshot = None
            code = "controller_role_required"
        else:
            try:
                snapshot = self._snapshot(rows, machine)
                code = "eligible"
            except registry.RepairRegistryAmbiguity as exc:
                snapshot = None
                code = exc.code
            if not NEW_ISSUANCE_ENABLED:
                code = "issuance_disabled"
            elif not token:
                code = "fleet_token_missing"
        host = snapshot.registry_host if snapshot is not None else next(
            (str(row.get("host", "")) for row in rows if str(row.get("machine", "")).strip() == machine),
            "",
        )
        return {
            "machine": machine,
            "display_label": registry.label_for(machine),
            "host": host,
            "eligible": code == "eligible",
            "code": code,
            "detail": _DETAIL.get(code, "This machine needs review before repair."),
            "snapshot": snapshot,
        }

    def eligibility(self) -> dict[str, Any]:
        rows = self._rows()
        token = self._token_reader()
        controller_role = self._controller_role()
        request_states = self._request_states()
        machines = []
        for machine in self._remote_machines(rows):
            outcome = self._outcome(
                rows, machine, token=token, controller_role=controller_role,
            )
            outcome.pop("snapshot")
            outcome["request_state"] = request_states.get(machine)
            machines.append(outcome)
        return {"issuance_enabled": bool(NEW_ISSUANCE_ENABLED), "machines": machines}

    def _resolve_preclaim_review(self, machine: str) -> None:
        with self.store._connect() as connection:
            row = connection.execute(
                """SELECT request_id FROM enrollment_repair_requests
                   WHERE target_machine = ? AND state = 'needs_review'
                     AND ticket_status IS NULL AND ticket_digest IS NULL
                     AND issued_at IS NULL AND redeemed_at IS NULL
                   ORDER BY created_at ASC LIMIT 1""",
                (machine,),
            ).fetchone()
        if row is not None:
            self.store.resolve_preclaim_review(
                row["request_id"], evidence_code="registry_unambiguous",
            )

    def create_batch(self, machines: Sequence[str]) -> dict[str, Any]:
        rows = self._rows()
        token = self._token_reader()
        requested = sorted({str(machine).strip() for machine in machines if str(machine).strip()})
        if not self._controller_role():
            return {
                "requests": [],
                "rejected": {machine: "controller_role_required" for machine in requested},
            }
        rejected: dict[str, str] = {}
        accepted: list[str] = []
        present = set(self._remote_machines(rows))
        for machine in requested:
            if machine == "local":
                rejected[machine] = "machine_local"
                continue
            if machine not in present:
                rejected[machine] = "machine_missing"
                continue
            outcome = self._outcome(rows, machine, token=token, controller_role=True)
            if not outcome["eligible"]:
                rejected[machine] = outcome["code"]
                continue
            self._resolve_preclaim_review(machine)
            accepted.append(machine)
        if not accepted:
            return {"requests": [], "rejected": rejected}
        batch = self.store.create_or_adopt_batch(accepted)
        result = dict(batch)
        result["rejected"] = rejected
        self._wake_scheduler()
        return result

    def batch(self, batch_id: str) -> dict[str, Any] | None:
        return self.store.batch(batch_id)

    def require_controller_identity_mutable(self) -> None:
        blocker = self.store.mutation_blocker()
        if blocker is not None:
            raise RepairStoreError("enrollment_repair_busy")

    def require_target_registry_mutable(self, machine: str) -> None:
        blocker = self.store.mutation_blocker(machine=str(machine))
        if blocker is not None:
            raise RepairStoreError("enrollment_repair_busy")

    def require_enrollment_registration_mutable(
        self,
        machine: str,
        host: str,
        *,
        resolved: EnrollmentRegistrationResolution | None = None,
    ) -> None:
        """Fence related live targets using only pre-resolved lock-time evidence."""
        if resolved is None:
            if getattr(self._mutation_state, "depth", 0):
                raise RepairStoreError("registry_resolution_required")
            resolved = self.resolve_enrollment_registration(machine, host)
        if (
            resolved.machine != str(machine).strip()
            or resolved.host != str(host).strip()
        ):
            raise RepairStoreError("registry_snapshot_changed")
        with self._mutation_lock:
            self._require_registry_rows_current(resolved.registry)
            related_machines = {resolved.machine}
            addresses = dict(resolved.registry.addresses)
            for _identifier, row_machine, row_host, _port in resolved.registry.rows:
                if not row_machine:
                    continue
                if row_machine == resolved.machine or row_host == resolved.host:
                    related_machines.add(row_machine)
                elif (
                    resolved.address is not None
                    and addresses.get(row_host) == resolved.address
                ):
                    related_machines.add(row_machine)
            for claim in self._live_claim_rows():
                if (
                    str(claim["target_machine"]) in related_machines
                    or str(claim.get("registry_host") or "") == resolved.host
                    or (
                        resolved.address is not None
                        and str(claim.get("resolved_address") or "") == resolved.address
                    )
                ):
                    raise RepairStoreError("enrollment_repair_busy")

    def note_registry_reload(
        self,
        registry_rows: Sequence[Mapping[str, Any]] | ResolvedRegistryRows,
    ) -> int:
        """Flag changed live target bindings without synthesizing terminal evidence."""
        if not isinstance(registry_rows, ResolvedRegistryRows):
            if getattr(self._mutation_state, "depth", 0):
                raise RepairStoreError("registry_resolution_required")
            registry_rows = self.resolve_registry_rows(registry_rows)
        current_resolution = None
        if not getattr(self._mutation_state, "depth", 0):
            current_resolution = self.resolve_registry_rows(self._rows())
        with self._mutation_lock:
            current_rows = self._registry_rows(self._rows())
            known_addresses = dict(registry_rows.addresses)
            if current_resolution is not None:
                known_addresses.update(current_resolution.addresses)
            effective_resolution = ResolvedRegistryRows(
                registry_rows.rows, tuple(sorted(known_addresses.items())),
            )
            changed = 0
            for claim in self._live_claim_rows(redeemed_only=True):
                code: str | None = None
                target_machine = str(claim["target_machine"])
                try:
                    snapshot = self._resolved_snapshot(
                        effective_resolution, target_machine,
                    )
                    if snapshot.registry_host != str(claim.get("registry_host") or ""):
                        code = "registry_host_changed"
                    elif snapshot.resolved_address != str(claim.get("resolved_address") or ""):
                        code = "registry_address_changed"
                except registry.RepairRegistryAmbiguity as exc:
                    code = exc.code
                if code is None:
                    prepared_binding = tuple(
                        row for row in registry_rows.rows if row[1] == target_machine
                    )
                    current_binding = tuple(
                        row for row in current_rows if row[1] == target_machine
                    )
                    if current_binding != prepared_binding:
                        code = "registry_snapshot_changed"
                if code is None:
                    target_host = str(claim.get("registry_host") or "")
                    target_address = str(claim.get("resolved_address") or "")
                    for row in current_rows:
                        _identifier, row_machine, row_host, _port = row
                        if (
                            not row_machine
                            or row_machine == "local"
                            or row_machine == target_machine
                            or not row_host
                        ):
                            continue
                        if row_host == target_host:
                            code = "host_shared"
                            break
                        if row_host not in known_addresses:
                            raise RepairStoreError("registry_snapshot_changed")
                        row_address = known_addresses[row_host]
                        if row_address is None:
                            raise RepairStoreError("registry_snapshot_changed")
                        if row_address == target_address:
                            code = "address_shared"
                            break
                if code is None:
                    continue
                evidence = self.store._decode_json(claim.get("evidence_json"))
                if (
                    isinstance(evidence, Mapping)
                    and evidence.get("registry_changed_pending") is True
                    and evidence.get("registry_changed_code") == code
                ):
                    continue
                try:
                    self.store.flag_registry_changed(
                        str(claim["request_id"]), evidence_code=code,
                    )
                except RepairStoreError as exc:
                    if exc.code != "request_has_no_live_claim":
                        raise
                else:
                    changed += 1
            return changed

    @contextmanager
    def controller_mutation(
        self,
        *,
        identity: bool = False,
        machine: str | None = None,
    ):
        with self._mutation_lock:
            self._mutation_state.depth = getattr(self._mutation_state, "depth", 0) + 1
            try:
                if identity:
                    self.require_controller_identity_mutable()
                if machine is not None:
                    self.require_target_registry_mutable(machine)
                yield
            finally:
                self._mutation_state.depth -= 1

    def release_fence_after_verified_stop(
        self,
        request_id: str,
    ) -> dict[str, Any]:
        """Adopt API-free recovery only after stop and shared-lock proof."""
        request = self.store.request(request_id)
        if request is None:
            raise RepairStoreError("request_not_found")
        machine = str(request["target_machine"])
        if (
            self._process_stop_verifier is None
            or self._process_stop_verifier(machine) is not True
        ):
            raise RepairStoreError("process_stop_unverified")
        if self._stopped_recovery is None:
            raise RepairStoreError("cleanup_classification_unverified")
        try:
            with settings_writer_lock():
                if self._process_stop_verifier(machine) is not True:
                    raise RepairStoreError("process_stop_unverified")
                classification = self._stopped_recovery()
        except SettingsWriterBusy as exc:
            raise RepairStoreError("settings_writer_busy") from exc
        if not isinstance(classification, Mapping):
            raise RepairStoreError("cleanup_classification_unverified")
        if str(classification.get("request_id", "")) != str(request_id):
            raise RepairStoreError("cleanup_classification_unverified")
        terminal = str(classification.get("state", ""))
        if terminal not in {"complete", "never_applied", "needs_review"}:
            raise RepairStoreError("cleanup_classification_unverified")
        now = float(self.store.clock())
        with self._mutation_lock, self.store._connect() as connection:
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
            if terminal == "complete":
                expected = {
                    "role": "agent",
                    "site_id": row["site_id"],
                    "site_name": row["site_name"],
                    "controller_id": row["target_machine"],
                }
                if classification.get("identity") != expected:
                    connection.rollback()
                    raise RepairStoreError("cleanup_classification_unverified")
                state, error_code = "complete", None
            elif terminal == "never_applied":
                state, error_code = "retryable", "never_applied"
            else:
                state, error_code = "needs_review", "needs_review"
            evidence = self.store._merge_evidence(row["evidence_json"], {
                "cleanup_terminal_state": terminal,
                "process_stop_verified": True,
                "shared_lock_classified": True,
            })
            updated = connection.execute(
                """UPDATE enrollment_repair_requests
                   SET state = ?, error_code = ?, evidence_json = ?,
                       dispatch_slot = NULL, updated_at = ?
                   WHERE request_id = ? AND state = ? AND ticket_status = 'redeemed'""",
                (state, error_code, evidence, now, str(request_id), row["state"]),
            ).rowcount
            if updated != 1:
                connection.rollback()
                raise RepairStoreError("cleanup_classification_conflict")
            self.store._refresh_batch_locked(connection, row["batch_id"], now)
            result = connection.execute(
                "SELECT * FROM enrollment_repair_requests WHERE request_id = ?",
                (str(request_id),),
            ).fetchone()
            connection.commit()
        return self.store._request_result(result)

    async def start(self) -> None:
        """Resume durable work without recreating any issued ticket plaintext."""
        self._started = True
        self.store.recover_scheduling_slot()
        self._wake_scheduler()
        if self._status_task is None or self._status_task.done():
            self._status_task = asyncio.create_task(self._status_loop())

    async def stop(self) -> None:
        """Stop local scheduling; durable requests remain available after restart."""
        self._started = False
        tasks = [
            task for task in (self._scheduler_task, self._status_task)
            if task is not None and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._scheduler_task = None
        self._status_task = None

    def _wake_scheduler(self) -> None:
        """Ensure one local scheduler exists after durable queue creation."""
        if (self._started
                and (self._scheduler_task is None or self._scheduler_task.done())):
            self._scheduler_task = asyncio.create_task(self._scheduler_loop())

    async def _scheduler_loop(self) -> None:
        """Start one fresh target, then free only its scheduling slot on timeout."""
        while True:
            try:
                dispatched = await self.dispatch_next()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Pre-claim failures release their slot in dispatch_next.  If
                # issuance already crossed its durable boundary, retain its
                # only authority but release just scheduling after the bounded
                # foreground interval.
                slot = self.store.scheduling_request()
                if slot is not None:
                    await self._park_after_foreground(str(slot["request_id"]))
                continue
            if dispatched is None:
                return
            request_id = str(dispatched["request_id"])
            response = dispatched.get("response")
            request = self.store.request(request_id)
            terminal = False
            if isinstance(response, Mapping) and request is not None:
                try:
                    adopted = await self.adopt_status(
                        request_id, response,
                        direct_source=str(request["resolved_address"] or ""),
                        registry_host=str(request["registry_host"] or ""),
                    )
                    terminal = adopted["state"] in {"complete", "retryable", "needs_review"}
                except RepairStoreError:
                    pass
            if not terminal:
                await self._park_after_foreground(request_id)

    async def _park_after_foreground(self, request_id: str) -> None:
        await asyncio.sleep(DISPATCH_TIMEOUT_SECONDS)
        request = self.store.request(request_id)
        if request is not None and request["state"] in {
            "ticket_issued", "dispatched", "redeemed", "verifying",
            "confirmation_pending",
        }:
            try:
                self.store.park(request_id)
            except RepairStoreError:
                pass

    async def _status_loop(self) -> None:
        """Probe parked targets independently of the one fresh-dispatch slot."""
        while True:
            for request in self.store.pending_status_requests():
                try:
                    await self._probe_status(request)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Status is evidence-only.  An unavailable/invalid target
                    # stays parked and does not delay later fresh dispatches.
                    continue
            await asyncio.sleep(DISPATCH_TIMEOUT_SECONDS)

    async def _probe_status(self, request: Mapping[str, Any]) -> None:
        request_id = str(request.get("request_id", ""))
        machine = str(request.get("target_machine", ""))
        registry_host = str(request.get("registry_host", ""))
        expected_address = str(request.get("resolved_address", ""))
        token = self._token_reader()
        if not request_id or not machine or not registry_host or not expected_address or not token:
            return
        snapshot = self._snapshot(
            self._rows(), machine, expected_address=expected_address,
        )
        if snapshot.registry_host != registry_host:
            raise RepairStoreError("status_registry_mismatch")
        origin = self._target_origin(registry_host)
        if origin.address != expected_address:
            raise RepairStoreError("host_address_changed")
        async with self._connection_factory(origin) as connection:
            connect = getattr(connection, "connect", None)
            if callable(connect):
                await connect(timeout=DISPATCH_TIMEOUT_SECONDS)
            try:
                source = canonical_private_address(connection.direct_peer)
            except (AttributeError, ValueError) as exc:
                raise RepairStoreError("source_host_mismatch") from exc
            if source != expected_address:
                raise RepairStoreError("source_host_mismatch")
            status_code, status = await connection.request_json(
                "GET", f"/api/hub/enrollment-repair/status/{request_id}",
                headers={"X-Hub-Token": token}, body=None,
                timeout=DISPATCH_TIMEOUT_SECONDS,
            )
        if status_code != 200 or not isinstance(status, Mapping):
            return
        await self.adopt_status(
            request_id, status, direct_source=source, registry_host=registry_host,
        )

    async def adopt_status(
        self,
        request_id: str,
        status: Mapping[str, Any],
        *,
        direct_source: str,
        registry_host: str,
    ) -> dict[str, Any]:
        """Persist terminal evidence only when it still binds to this target."""
        request = self.store.request(request_id)
        if request is None:
            raise RepairStoreError("request_not_found")
        try:
            source = canonical_private_address(direct_source)
        except ValueError as exc:
            raise RepairStoreError("source_host_mismatch") from exc
        machine = str(request["target_machine"])
        expected_host = str(request["registry_host"] or "")
        expected_address = str(request["resolved_address"] or "")
        if registry_host != expected_host or source != expected_address:
            raise RepairStoreError("source_host_mismatch")
        snapshot = self._snapshot(
            self._rows(), machine, expected_address=source,
        )
        if snapshot.registry_host != expected_host:
            raise RepairStoreError("status_registry_mismatch")
        if str(status.get("request_id", "")) != str(request_id):
            raise RepairStoreError("status_request_mismatch")
        if status.get("state") == "complete":
            expected_identity = {
                "role": "agent",
                "site_id": request["site_id"],
                "site_name": request["site_name"],
                "controller_id": machine,
            }
            if status.get("identity") != expected_identity:
                raise RepairStoreError("status_identity_mismatch")
        was_complete = request["state"] == "complete"
        adopted = self.store.adopt_status(request_id, status, direct_source=source)
        if adopted["state"] == "complete" and not was_complete:
            self._peer_invalidator(machine)
            self._wake_peer(machine)
        return adopted

    async def dispatch_next(self) -> dict[str, Any] | None:
        """Commit one target-bound ticket, then write it on the same pinned socket."""
        if not NEW_ISSUANCE_ENABLED:
            return None
        first_token = self._token_reader()
        if not first_token:
            raise RepairStoreError("fleet_token_missing")
        request = self.store.claim_next_dispatch()
        if request is None:
            return None
        request_id = str(request["request_id"])
        machine = str(request["target_machine"])
        ticket_issued = False
        try:
            resolved_registry = self.resolve_registry_rows(self._rows())
            initial_snapshot = self._resolved_snapshot(resolved_registry, machine)
            target_origin = self._target_origin(initial_snapshot.registry_host)
            if target_origin.address != initial_snapshot.resolved_address:
                raise RepairStoreError("host_address_changed")

            await self._ensure_repair_capability(
                machine,
                initial_snapshot.registry_host,
                initial_snapshot.resolved_address,
                first_token,
            )
            resolved_registry = self.resolve_registry_rows(self._rows())
            initial_snapshot = self._resolved_snapshot(resolved_registry, machine)
            target_origin = self._target_origin(initial_snapshot.registry_host)
            if target_origin.address != initial_snapshot.resolved_address:
                raise RepairStoreError("host_address_changed")

            async with self._connection_factory(target_origin) as connection:
                connect = getattr(connection, "connect", None)
                if callable(connect):
                    await connect(timeout=DISPATCH_TIMEOUT_SECONDS)
                try:
                    direct_peer = canonical_private_address(connection.direct_peer)
                except (AttributeError, ValueError) as exc:
                    raise PinnedTransportError("transport_unavailable") from exc
                try:
                    local_address = canonical_private_address(connection.local_address)
                except (AttributeError, ValueError) as exc:
                    raise RepairStoreError("callback_url_invalid") from exc
                if direct_peer != target_origin.address:
                    raise PinnedTransportError("callback_source_mismatch")
                resolved_registry = self.resolve_registry_rows(self._rows())
                current_snapshot = self._resolved_snapshot(
                    resolved_registry, machine, expected_address=direct_peer,
                )

                async with self._foreground_lock:
                    with self._mutation_lock:
                        current_token = self._token_reader()
                        if not current_token:
                            raise RepairStoreError("fleet_token_missing")
                        if not hmac.compare_digest(first_token, current_token):
                            raise RepairStoreError("fleet_token_changed")
                        self._require_registry_rows_current(resolved_registry)
                        controller = self._controller_identity()
                        ticket = self._ticket_factory()
                        if (
                            not isinstance(ticket, str)
                            or not _URLSAFE_TICKET.fullmatch(ticket)
                        ):
                            raise RepairStoreError("ticket_generation_failed")
                        expires_at = float(self._clock()) + TICKET_LIFETIME_SECONDS
                        controller_url = self._origin_for_address(local_address)
                        target = TargetIdentity(
                            machine=machine,
                            registry_host=current_snapshot.registry_host,
                            resolved_address=current_snapshot.resolved_address,
                            controller_url=controller_url,
                        )
                        self.store.issue_ticket(
                            request_id,
                            target=target,
                            controller=controller,
                            fleet_token_digest=self._digest(current_token),
                            ticket_digest=self._digest(ticket),
                            redemption_expires_at=expires_at,
                        )
                        ticket_issued = True
                        dispatch = {
                            "schema": "studiohub.enrollment-repair-dispatch",
                            "schema_version": 1,
                            "request_id": request_id,
                            "target_machine_id": machine,
                            "ticket": ticket,
                            "redemption_expires_at": expires_at,
                            "controller_url": controller_url,
                            "controller": {
                                "site_id": controller.site_id,
                                "site_name": controller.site_name,
                                "controller_id": controller.controller_id,
                            },
                        }

                status, response = await connection.request_json(
                    "POST",
                    "/api/hub/enrollment-repair/apply",
                    headers={"X-Hub-Token": current_token},
                    body=dispatch,
                    timeout=DISPATCH_TIMEOUT_SECONDS,
                )
                self.store.mark_dispatched(request_id)
                safe_response = self._dispatch_response(
                    request_id, machine, controller, response,
                )
        except BaseException as exc:
            if not ticket_issued:
                state, error_code = self._preissuance_failure(exc)
                try:
                    self.store.fail_before_claim(
                        request_id, state=state, error_code=error_code,
                    )
                except RepairStoreError as cleanup_error:
                    # If authority committed despite an exceptional return, the
                    # preclaim guard refuses to erase it; Task 9 parks that slot.
                    if cleanup_error.code != "request_has_claim_authority":
                        raise
            raise
        return {
            "request_id": request_id,
            "target_machine_id": machine,
            "status_code": status,
            "response": safe_response,
        }

    async def redeem(
        self,
        payload: Mapping[str, Any],
        *,
        direct_source: str,
        fleet_token: str,
    ) -> dict[str, Any]:
        """Revalidate live bindings and atomically return the one repair claim."""
        parsed = self._parse_redemption(payload)
        try:
            source = canonical_private_address(direct_source)
        except ValueError as exc:
            raise RepairStoreError("target_binding_mismatch") from exc
        machine = parsed["target_machine_id"]
        resolved_registry = self.resolve_registry_rows(self._rows())
        prepared_snapshot = self._resolved_snapshot(
            resolved_registry, machine, expected_address=source,
        )
        async with self._foreground_lock:
            with self._mutation_lock:
                current_token = self._token_reader()
                if not current_token:
                    raise RepairStoreError("fleet_token_missing")
                if (not isinstance(fleet_token, str)
                        or not hmac.compare_digest(current_token, fleet_token)):
                    raise RepairStoreError("fleet_token_mismatch")
                self._require_registry_rows_current(resolved_registry)
                controller = self._controller_identity()
                target = TargetIdentity(
                    machine=machine,
                    registry_host=prepared_snapshot.registry_host,
                    resolved_address=prepared_snapshot.resolved_address,
                    controller_url=self._issued_controller_url(parsed["request_id"]),
                )
                return self.store.redeem(
                    parsed["request_id"],
                    ticket=parsed["ticket"],
                    redemption_expires_at=parsed["redemption_expires_at"],
                    target_machine=machine,
                    direct_source=source,
                    observed_identity=parsed["observed_identity"],
                    registry_snapshot=target,
                    controller=controller,
                    fleet_token_digest=self._digest(current_token),
                )
