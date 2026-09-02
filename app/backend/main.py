"""Studio Hub KH — control plane for the KH Studio family.

Monitoring dashboard and local fleet control plane.
  - host-aware studio registry
  - health/version poller
  - unified (pass-through) model catalog
  - host + per-studio resource monitor

The /api/health and /api/version shapes intentionally mirror the sibling
studios, so the Hub itself is monitorable by the same convention.
"""

import asyncio
import gzip
import hashlib
import json
import re
import secrets
import subprocess
import threading
import time
import uuid
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from typing import Annotated, Any, Literal

import httpx

from starlette.background import BackgroundTask
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from . import (activity, alerts, artifact_metadata, auth, broadcast, broker, capabilities, chat_jobs, cloud_guard, control_plane, enrollment, enrollment_repair, execution_assets, execution_identity, fleet_ops, fleet_storage, gateway, hardware_profiles, hf_credentials, job_storage, memory_admission, model_exposure,
               ledger, metrics, peers, recipes, registry, shared_voices, startup_services, transcription_jobs,
               voice_qualification)
from .auto_update import UpdateError
from .auto_update_config import create_updater
from .fleet_auto_updates import FleetAutoUpdates, TERMINAL_ITEM_STATES, managed_failure_code
from .auth import is_loopback, is_tailscale, load_token, make_middleware
from .control import control_studio
from .controller_settings_lock import SettingsWriterBusy, settings_writer_lock
from .enrollment_repair import EnrollmentRepairCoordinator
from .enrollment_repair_executor import RepairExecutor, RepairExecutorError
from .enrollment_repair_store import RepairStore, RepairStoreError
from .enrollment_repair_transport import resolve_private_origin
from .monitor import StudioMonitor
from .memory_control import FleetMemoryControl
from .model_baselines import FleetModelBaselines
from .process_title import PROCESS_TITLE, apply_process_title
from .release_reconciliation import ReleaseReconciler
from .registry import DATA_DIR, LAUNCHER_ROOT, base_url
from .resources import host_stats, proxy_stats, studio_process_stats

TITLE = "Studio Hub KH"
FRONTEND_DIR = Path(__file__).resolve().parents[1] / "frontend"
PROCESS_TITLE_APPLIED = apply_process_title()


class UpdateRequest(BaseModel):
    studio_ids: list[str] = Field(min_length=1, max_length=100)


class GenerationInstallRequest(BaseModel):
    studio_ids: list[str] | None = Field(default=None, max_length=100)
    local_only: bool = False


class StudioUpdateRepairRequest(BaseModel):
    studio_ids: list[str] | None = Field(default=None, max_length=100)
    local_only: bool = False


class AutoUpdateSettingsBody(BaseModel):
    mode: str
    frequency: str
    maintenance_hour: int
    idle_only: bool = True
    drain_timeout_minutes: int | None = None


class AutoUpdateRequestBody(BaseModel):
    after_current: bool = False
    target_commit: str | None = None
    target_version: str | None = None
    operation_id: str | None = None


class HubRestartBody(BaseModel):
    force: bool = False


class ReleaseActivationBody(BaseModel):
    genstudio_run_reference: str | None = Field(default=None, max_length=128)


class FleetAutoModeBody(BaseModel):
    mode: str


class FleetAutoRunBody(BaseModel):
    target_ids: list[str] | None = Field(default=None, max_length=100)


class ModelBaselineSettingsBody(BaseModel):
    enabled: bool = True


class FleetMemoryPolicyBody(BaseModel):
    mode: str
    studio_ids: list[str] | None = Field(default=None, max_length=100)


class FleetMemoryReleaseBody(BaseModel):
    studio_ids: list[str] | None = Field(default=None, max_length=100)


class MemoryAdmissionBody(BaseModel):
    model: str = Field(min_length=1, max_length=500)
    min_total_memory_gb: float = Field(ge=4, le=512)
    min_free_memory_gb: float = Field(ge=0.5, le=128)


class ModelExposureActionBody(BaseModel):
    candidate_key: str = Field(min_length=64, max_length=64,
                               pattern=r"^[0-9a-f]{64}$")
    reason: str | None = Field(default=None, max_length=500)


class VoiceQualificationBody(BaseModel):
    """Owner-only voice research request; never a customer job envelope."""
    client_request_id: str = Field(min_length=8, max_length=160,
                                   pattern=r"^[A-Za-z0-9._:-]+$")
    target_studio_id: str = Field(min_length=1, max_length=120)
    machine_tier_gb: int = Field(ge=8, le=24)
    model: str = Field(min_length=1, max_length=500)
    operation: str | None = Field(default=None, max_length=80)
    case_type: str = Field(min_length=1, max_length=40)
    text: str = Field(min_length=1, max_length=40_000)
    params: dict[str, Any] = Field(default_factory=dict)
    voice_reference_asset_id: str | None = Field(default=None, max_length=120)
    allow_controller_local: bool = False
    excluded_machine_ids: list[str] = Field(default_factory=list, max_length=100)


class SharedVoiceUpdateBody(BaseModel):
    name: str | None = None
    language: str | None = None
    gender: str | None = None
    license: str | None = None
    notes: str | None = None
    source_url: str | None = None
    transcript: str | None = None


class OwnerPasswordBody(BaseModel):
    password: str = Field(min_length=1, max_length=1024)


class FleetStoragePolicyBody(BaseModel):
    enabled: bool = True
    retention_days: int = 30
    max_gb: float = Field(default=80, ge=1, le=1000)


class ControllerSettingsBody(BaseModel):
    role: str
    site_id: str
    site_name: str
    controller_id: str
    machine_name: str | None = Field(default=None, max_length=120)
    database_mode: str = "off"
    database_url: str | None = Field(default=None, max_length=4096)
    clear_database_url: bool = False


class SimpleControllerSetupBody(BaseModel):
    location_name: str = Field(min_length=1, max_length=120)
    site_id: str = Field(min_length=1, max_length=100)
    hardware_profile_id: str | None = Field(default=None, max_length=64)


class EnrollmentCodeBody(BaseModel):
    code: str = Field(min_length=1, max_length=256)
    machine: str | None = Field(default=None, max_length=100)
    machine_name: str | None = Field(default=None, max_length=120)
    hardware_profile_id: str | None = Field(default=None, max_length=64)
    modalities: list[str] | None = None


RepairMachineId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$",
    ),
]
RepairRequestId = Annotated[
    str,
    StringConstraints(
        min_length=16,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    ),
]
RepairTicket = Annotated[
    str,
    StringConstraints(min_length=43, max_length=256, pattern=r"^[A-Za-z0-9_-]+$"),
]
BoundedSiteId = Annotated[str, StringConstraints(min_length=1, max_length=100)]
BoundedSiteName = Annotated[str, StringConstraints(min_length=1, max_length=120)]


class StrictRepairBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EnrollmentRepairBatchBody(StrictRepairBody):
    machines: list[RepairMachineId] = Field(min_length=1, max_length=100)


class EnrollmentRepairControllerBody(StrictRepairBody):
    site_id: BoundedSiteId
    site_name: BoundedSiteName
    controller_id: RepairMachineId


class EnrollmentRepairDispatchBody(StrictRepairBody):
    schema_name: Literal["studiohub.enrollment-repair-dispatch"] = Field(alias="schema")
    schema_version: Annotated[int, Field(strict=True, ge=1, le=1)]
    request_id: RepairRequestId
    target_machine_id: RepairMachineId
    ticket: RepairTicket
    redemption_expires_at: Annotated[float, Field(strict=True, allow_inf_nan=False)]
    controller_url: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    controller: EnrollmentRepairControllerBody


class EnrollmentRepairObservedIdentityBody(StrictRepairBody):
    role: Annotated[str, StringConstraints(max_length=20)]
    site_id: Annotated[str, StringConstraints(max_length=100)]
    site_name: Annotated[str, StringConstraints(max_length=120)]
    controller_id: Annotated[str, StringConstraints(max_length=100)]
    parent_controller_url: Annotated[str, StringConstraints(min_length=1, max_length=500)] | None


class EnrollmentRepairRedemptionBody(StrictRepairBody):
    schema_name: Literal["studiohub.enrollment-repair-redemption"] = Field(alias="schema")
    schema_version: Annotated[int, Field(strict=True, ge=1, le=1)]
    request_id: RepairRequestId
    target_machine_id: RepairMachineId
    ticket: RepairTicket
    redemption_expires_at: Annotated[float, Field(strict=True, allow_inf_nan=False)]
    observed_identity: EnrollmentRepairObservedIdentityBody


class ControllerProbeBody(BaseModel):
    controller_url: str = Field(min_length=1, max_length=500)


class AgentJoinBody(BaseModel):
    controller_url: str = Field(min_length=1, max_length=500)
    enrollment_code: str = Field(min_length=1, max_length=256)
    hardware_profile_id: str | None = Field(default=None, max_length=64)
    machine_name: str | None = Field(default=None, max_length=120)


class HardwareProfileBody(BaseModel):
    id: str = Field(min_length=3, max_length=64)
    display_name: str = Field(min_length=1, max_length=100)
    machine_type: str = Field(min_length=1, max_length=50)
    machine_prefix: str = Field(min_length=3, max_length=80)
    chip: str = Field(min_length=2, max_length=18)
    memory_gb: int = Field(ge=4, le=512)
    planned_units: int = Field(default=0, ge=0, le=10_000)


class MachineHardwareProfileBody(BaseModel):
    hardware_profile_id: str | None = Field(default=None, max_length=64)


PRODUCTION_STUDIO_MODALITIES = ("image", "voice")

# Give our loggers a handler regardless of how uvicorn configures logging, so
# structured warnings/alerts actually reach the service log.
import logging as _logging
_hub_log = _logging.getLogger("studiohub")
if not _hub_log.handlers:
    _h = _logging.StreamHandler()
    _h.setFormatter(_logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    _hub_log.addHandler(_h)
    _hub_log.setLevel(_logging.INFO)
    _hub_log.propagate = False


def _read_app_version() -> str:
    try:
        return (LAUNCHER_ROOT / "VERSION").read_text().strip()
    except OSError:
        return "0.0.0"


APP_VERSION = _read_app_version()


def _read_app_commit() -> str:
    """Commit attested by this loaded process, never by a later checkout."""
    try:
        commit = subprocess.check_output(
            ["/usr/bin/git", "rev-parse", "HEAD"], cwd=LAUNCHER_ROOT,
            text=True, stderr=subprocess.DEVNULL, timeout=5,
        ).strip()
        return commit if re.fullmatch(r"[0-9a-f]{40}", commit) else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


APP_COMMIT = _read_app_commit()


def _app_version() -> str:
    """Version of the code loaded by this process, not a later disk checkout."""
    return APP_VERSION


monitor = StudioMonitor()
memory_control = FleetMemoryControl(monitor)


def _automatic_update_blockers() -> list[str]:
    reasons = fleet_ops.hub_update_blockers()
    coordinator = globals().get("fleet_auto_updates")
    if coordinator:
        active = next((job for job in coordinator.jobs()
                       if job["status"] in {"queued", "running"}), None)
        if active:
            reasons.append("a staggered automatic fleet update is active")
    return reasons


# Blockers a drain cannot clear by withdrawing the site: queued batch items stop
# dispatching the moment every worker is in maintenance, and an in-flight item is
# already counted as a lease, so the drain waits on leases and on Hub-owned
# maintenance operations only. Waiting on the batch queues themselves is what
# made "Update after current work" wait forever on a production fleet.
_DRAIN_NEUTRALISED_BLOCKERS = frozenset({
    "a fleet worker owns an active lease",
    "a generation batch is queued or running",
    "a Chat batch is queued or running",
    "a transcription batch is queued or running",
})


class _SiteDrain:
    """Withdraw this site from fleet routing so an update can install now.

    Every registered worker is put into the same maintenance state the rolling
    Studio updater already uses, which stops new lease grants in the broker and
    reports ``drained`` for each worker — and therefore for the controller — in
    the capability snapshot GenStudio reads, so GenStudio routes elsewhere.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._withdrawn: list[str] = []
        self._active = False

    def active(self) -> bool:
        return self._active

    def begin(self) -> list[str]:
        with self._lock:
            # Only workers this drain withdrew are given back later, so a
            # concurrent Studio update keeps the worker it already owns.
            newly = [studio["id"] for studio in monitor.registry
                     if not broker.in_maintenance(studio["id"])]
            for studio_id in newly:
                broker.set_maintenance(studio_id, True)
            self._withdrawn = newly
            self._active = True
            return newly

    def pending(self) -> list[str]:
        leases = [f"{studio_id} is still running an item"
                  for studio_id in sorted(fleet_ops.active_studio_leases())]
        return leases + [reason for reason in _automatic_update_blockers()
                         if reason not in _DRAIN_NEUTRALISED_BLOCKERS]

    def release(self) -> None:
        with self._lock:
            for studio_id in self._withdrawn:
                broker.set_maintenance(studio_id, False)
            self._withdrawn = []
            self._active = False


hub_site_drain = _SiteDrain()
auto_updater = create_updater(readiness=_automatic_update_blockers, drain=hub_site_drain)


def _schedule_auto_update_reconciliation() -> None:
    def wait_until_idle() -> None:
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            time.sleep(1)
            try:
                if auto_updater.apply_scheduler_if_idle():
                    return
            except (UpdateError, OSError, subprocess.SubprocessError) as exc:
                print(f"[hub] automatic-update scheduler reconciliation deferred: {exc}")
                return

    threading.Thread(
        target=wait_until_idle,
        name="hub-updater-wrapper-migration",
        daemon=True,
    ).start()


def _reconcile_auto_update_scheduler() -> bool:
    """Migrate the scheduler only when its current update helper is idle."""
    try:
        if not auto_updater.apply_scheduler_if_idle():
            print("[hub] automatic-update scheduler reconciliation deferred: update active")
            _schedule_auto_update_reconciliation()
            return False
        return True
    except (UpdateError, OSError, subprocess.SubprocessError) as exc:
        print(f"[hub] automatic-update scheduler reconciliation deferred: {exc}")
        return False


fleet_auto_updates = FleetAutoUpdates(
    monitor, auto_updater,
    state_path=DATA_DIR / "auto_update" / "fleet_jobs.json",
)
model_baselines = FleetModelBaselines(
    monitor, state_path=DATA_DIR / "model_baselines.json",
)
release_reconciler: ReleaseReconciler | None = None


def _reconcile_managed_registry() -> int:
    """Persist fleet growth without making registration depend on rollout state."""
    service = release_reconciler
    if service is None:
        return 0
    return service.wake_registry()


async def _request_managed_release_catalog(operation_id: str) -> dict[str, Any]:
    """Request one catalog pass and return request evidence, never cache completion."""
    snapshot = await model_baselines.reconcile()
    revision = snapshot.get("catalog_revision")
    approved = (snapshot.get("summary") or {}).get("approved_models")
    return {
        "operation_id": operation_id,
        "requested_revision": revision if isinstance(revision, str) else None,
        "requested_models": approved if isinstance(approved, int) else 0,
    }


async def _run_managed_hub_update(
    target: dict[str, Any], operation_id: str,
) -> dict[str, Any]:
    try:
        result = auto_updater.trigger_update(
            after_current=True,
            target_commit=target["commit"],
            target_version=target["version"],
            operation_id=operation_id,
        )
    except (UpdateError, ValueError, OSError):
        failure_code = managed_failure_code(auto_updater.public_status())
        return {
            "component": "hub",
            "state": "retryable_failure",
            "error_code": failure_code or "update_refused",
        }
    if not isinstance(result, dict) or result.get("state") == "failed":
        return {
            "component": "hub",
            "state": "retryable_failure",
            "error_code": managed_failure_code(result) or "update_refused",
        }
    return {"component": "hub", "state": "restarting"}


async def _probe_enrollment_repair_capability(
    machine: str,
    host: str,
    _fleet_token: str,
) -> dict[str, Any]:
    """Read one registered Agent's repair capability over its pinned socket."""
    try:
        snapshot = await asyncio.to_thread(
            registry.repair_machine_snapshot, list(monitor.registry), machine,
        )
        if snapshot.registry_host != host:
            return {}
        origin = await asyncio.to_thread(
            resolve_private_origin, f"http://{host}:47873",
        )
        if origin.address != snapshot.resolved_address:
            return {}
        async with enrollment_repair.open_pinned_json(origin) as connection:
            connect = getattr(connection, "connect", None)
            if callable(connect):
                await connect(timeout=enrollment_repair.DISPATCH_TIMEOUT_SECONDS)
            if getattr(connection, "direct_peer", None) != origin.address:
                return {}
            status, capability = await connection.request_json(
                "GET", "/api/hub/enrollment/info", headers={}, body=None,
                timeout=enrollment_repair.DISPATCH_TIMEOUT_SECONDS,
            )
        if status != 200 or not isinstance(capability, dict):
            return {}
        return capability
    except asyncio.CancelledError:
        raise
    except Exception:
        return {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global release_reconciler
    monitor_start_attempted = False
    runtime_start_attempted = False
    release_start_attempted = False
    repair_start_attempted = False
    version_start_attempted = False
    transcription_start_attempted = False
    chat_start_attempted = False
    voices_start_attempted = False
    storage_start_attempted = False
    baselines_start_attempted = False
    repair_coordinator = None
    primary_error: BaseException | None = None
    try:
        recovered_ledger = ledger.prepare_database()
        if recovered_ledger is not None:
            print(
                "[hub] recovered startup from a corrupt hub.db; "
                f"the original database is preserved at {recovered_ledger}",
                flush=True,
            )
        _reconcile_auto_update_scheduler()
        for cleanup in startup_services.reconcile_removal_intents():
            if not cleanup.get("ok"):
                print(
                    "[hub] interrupted Studio removal still needs attention: "
                    f"{cleanup.get('modality')}: {cleanup.get('error')}"
                )
        removed_execution_assets = execution_assets.cleanup_expired()
        if removed_execution_assets:
            print(f"[hub] removed {removed_execution_assets} expired execution asset(s)")
        monitor_start_attempted = True
        monitor.start()
        runtime_start_attempted = True
        await control_plane.runtime.start(monitor, _app_version())
        release_reconciler = ReleaseReconciler(
            monitor,
            state_path=DATA_DIR / "release_reconciliation.json",
            loaded_version=_app_version(),
            loaded_commit=APP_COMMIT,
            identity_reader=control_plane.public_settings,
            hub_runner=_run_managed_hub_update,
            catalog_requester=_request_managed_release_catalog,
        )
        peers.release_reconciler = release_reconciler
        release_start_attempted = True
        await release_reconciler.start()
        repair_store = RepairStore()
        repair_executor = RepairExecutor()
        repair_coordinator = EnrollmentRepairCoordinator(
            repair_store,
            registry_loader=lambda: list(monitor.registry),
            capability_probe=_probe_enrollment_repair_capability,
        )
        app.state.enrollment_repair_store = repair_store
        app.state.enrollment_repair_executor = repair_executor
        app.state.enrollment_repair_coordinator = repair_coordinator
        repair_executor.recover()
        repair_start_attempted = True
        await repair_coordinator.start()
        version_start_attempted = True
        fleet_ops.start_published_version_monitor()
        resumed_updates = fleet_auto_updates.resume_pending()
        if resumed_updates:
            print(f"[hub] resumed {resumed_updates} interrupted fleet update job(s)")
        restored = broker.restore_batches()
        if restored:
            print(f"[hub] resumed {restored} unfinished batch(es) from hub.db")
        broker.start_dispatcher()
        transcription_restored = transcription_jobs.restore_batches()
        if transcription_restored:
            print(f"[hub] resumed {transcription_restored} transcription batch(es) from hub.db")
        transcription_start_attempted = True
        transcription_jobs.start_dispatcher(monitor)
        chat_restored = chat_jobs.restore_batches()
        if chat_restored:
            print(f"[hub] resumed {chat_restored} Chat batch(es) from hub.db")
        chat_start_attempted = True
        chat_jobs.start_dispatcher(monitor)
        voices_start_attempted = True
        shared_voices.start_reconciler(monitor)
        storage_start_attempted = True
        fleet_storage.start(monitor)
        baselines_start_attempted = True
        model_baselines.start()
        yield
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        cleanup_error: BaseException | None = None

        async def stop(attempted: bool, callback) -> None:
            nonlocal cleanup_error
            if not attempted:
                return
            try:
                await callback()
            except BaseException as exc:
                if cleanup_error is None:
                    cleanup_error = exc

        await stop(
            repair_start_attempted and repair_coordinator is not None,
            repair_coordinator.stop if repair_coordinator is not None else None,
        )
        await stop(
            release_start_attempted and release_reconciler is not None,
            release_reconciler.stop if release_reconciler is not None else None,
        )
        release_reconciler = None
        peers.release_reconciler = None
        await stop(version_start_attempted, fleet_ops.stop_published_version_monitor)
        await stop(runtime_start_attempted, control_plane.runtime.stop)
        await stop(storage_start_attempted, fleet_storage.stop)
        await stop(baselines_start_attempted, model_baselines.stop)
        await stop(voices_start_attempted, shared_voices.stop)
        await stop(chat_start_attempted, chat_jobs.stop)
        await stop(transcription_start_attempted, transcription_jobs.stop)
        await stop(monitor_start_attempted, monitor.stop)
        if cleanup_error is not None and primary_error is None:
            raise cleanup_error


app = FastAPI(title=TITLE, lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def redacted_repair_request_validation(
    request: Request,
    exc: RequestValidationError,
):
    if auth.strict_fleet_service_path(request.url.path):
        return JSONResponse(
            {"detail": {"code": "repair_request_invalid"}},
            status_code=422,
        )
    return await request_validation_exception_handler(request, exc)

# The Hub is the canonical API other clients (Story Studio KH, scripts, LLM
# directors) converge on — allow browser clients from anywhere on the tailnet.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Token auth: loopback is exempt; remote clients need the Hub token.
HUB_TOKEN = load_token()
app.middleware("http")(make_middleware(HUB_TOKEN))
app.middleware("http")(gateway.fleet_job_safe_headers)

# Unified gateway: {HUB}/studio/{id}/{path} -> the right studio.
app.include_router(gateway.router)


# ── browser owner sign-in ──────────────────────────────────────────────────
@app.get("/api/auth/status")
def auth_status(request: Request):
    """Public, non-sensitive browser-login capability check."""
    return {"password_configured": auth.password_configured(),
            "can_configure_here": is_loopback(request),
            "password_login_allowed": is_loopback(request) or is_tailscale(request),
            "session_active": auth.valid_browser_session(
                request.cookies.get(auth.SESSION_COOKIE_NAME)),
            "remember_days": auth.SESSION_TTL_DAYS}


@app.post("/api/auth/setup")
def auth_setup_owner_password(request: Request, body: OwnerPasswordBody):
    """Set/replace the owner password only from the Hub Mac itself."""
    if not is_loopback(request):
        raise HTTPException(403, "Set the owner password on the Hub Mac itself.")
    try:
        auth.set_owner_password(body.password)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "remember_days": auth.SESSION_TTL_DAYS,
            "message": "Owner password saved. Existing remembered devices were signed out."}


@app.post("/api/auth/login")
def auth_login(request: Request, body: OwnerPasswordBody):
    """Issue a 90-day opaque, HttpOnly remembered-device session."""
    if not is_loopback(request) and not is_tailscale(request):
        raise HTTPException(403, "Password sign-in is available through the Tailscale address only.")
    if not auth.password_configured():
        raise HTTPException(409, "Set an owner password locally on the Hub Mac first.")
    if not auth.login_allowed(request):
        raise HTTPException(429, "Too many attempts. Try again in 15 minutes.")
    if not auth.verify_owner_password(body.password):
        auth.record_login_failure(request)
        raise HTTPException(401, "Incorrect password.")
    auth.clear_login_failures(request)
    response = JSONResponse({"ok": True, "remember_days": auth.SESSION_TTL_DAYS})
    auth.set_browser_session_cookie(response, auth.create_browser_session())
    return response


@app.post("/api/auth/logout")
def auth_logout(request: Request):
    """Forget the current browser, whether or not it is still valid."""
    auth.forget_browser_session(request.cookies.get(auth.SESSION_COOKIE_NAME))
    response = JSONResponse({"ok": True})
    auth.clear_browser_session_cookie(response)
    return response


# ── sibling-convention endpoints (Hub is monitorable like a studio) ────────
@app.get("/api/health")
def health():
    return {"ok": True, "version": "0.1.0", "app_version": _app_version(),
            "app_commit": APP_COMMIT,
            "process_title": PROCESS_TITLE, "process_title_applied": PROCESS_TITLE_APPLIED,
            "control_plane": control_plane.runtime.readiness()}


@app.get("/health/live")
def controller_liveness():
    """Load-balancer liveness: this process can answer HTTP."""
    settings = control_plane.public_settings()
    return {"live": True, "app_version": _app_version(),
            "role": settings["role"], "site_id": settings["site_id"],
            "controller_id": settings["controller_id"]}


@app.get("/health/ready")
def controller_readiness():
    """Site-execution readiness; optional PostgreSQL never gates dispatch."""
    result = control_plane.runtime.readiness()
    return JSONResponse(result, status_code=200 if result["ready"] else 503)


@app.get("/health/capacity")
def controller_capacity():
    """Non-secret capacity signal for GenStudio's future site router."""
    return control_plane.runtime.capacity()


@app.get("/api/hub/capabilities")
async def controller_capabilities(request: Request):
    """Versioned private capability snapshot for GenStudio's site router."""
    if not auth.valid_machine_token(request, HUB_TOKEN):
        raise HTTPException(
            401,
            "Hub or fleet token required for the private capability snapshot.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    managed_release = (
        release_reconciler.capability_evidence()
        if release_reconciler is not None else None
    )
    return await capabilities.build_snapshot(
        monitor,
        app_version=_app_version(),
        settings=control_plane.public_settings(),
        readiness=control_plane.runtime.readiness(),
        base_capacity=control_plane.runtime.capacity(monitor),
        managed_release=managed_release,
    )


# ── Update auto-check (surfaced by the web-UI banner; mirrors the studios) ──
import threading as _threading
import time as _time
import urllib.request as _urlreq

_UPDATE_REPO = "theng12/studiohub-mac"
_update_state = {"checked_at": 0.0, "latest": None, "commit": None}


def _parse_ver(v):
    try:
        return tuple(int(x) for x in str(v).strip().lstrip("v").split(".")[:3])
    except Exception:
        return (0,)


def _refresh_latest_version():
    try:
        import re
        ref_url = (f"https://github.com/{_UPDATE_REPO}.git/info/refs"
                   "?service=git-upload-pack")
        with _urlreq.urlopen(ref_url, timeout=5) as response:
            advertised = response.read()
        match = re.search(rb"([0-9a-f]{40}) refs/heads/main(?:\x00|\n)", advertised)
        if not match:
            raise ValueError("main branch ref was not advertised")
        commit = match.group(1).decode("ascii")
        url = f"https://raw.githubusercontent.com/{_UPDATE_REPO}/{commit}/VERSION"
        with _urlreq.urlopen(url, timeout=5) as response:
            _update_state["latest"] = response.read().decode("utf-8").strip()
            _update_state["commit"] = commit
    except Exception:
        pass
    finally:
        _update_state["checked_at"] = _time.time()


@app.get("/api/update-status")
def update_status():
    """Behind-the-published-version check for the web-UI banner. Remote VERSION is
    fetched from the repo raw file at most every ~6h, in a background thread, so a
    slow/unreachable GitHub never blocks the request."""
    if _time.time() - _update_state["checked_at"] > 6 * 3600:
        _threading.Thread(target=_refresh_latest_version, daemon=True).start()
    latest = _update_state["latest"]
    current = _app_version()
    return {
        "app_version": current,
        "latest_version": latest,
        "update_available": bool(latest and _parse_ver(latest) > _parse_ver(current)),
        "generation_required": False,
        "generation_ok": None,
    }


@app.get("/api/version")
def version():
    return {"app_version": _app_version(), "title": TITLE,
            "app_commit": APP_COMMIT,
            "process_title": PROCESS_TITLE, "process_title_applied": PROCESS_TITLE_APPLIED,
            "studio_update_repair_schema": 1}


def _release_notes() -> list[dict]:
    """Read published details from CHANGELOG so What's New cannot go stale."""
    try:
        text = (LAUNCHER_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    except OSError:
        return []
    headings = list(re.finditer(
        r"^## \[([^]]+)\]\s+[—-]\s+(.+?)\s*$", text, flags=re.MULTILINE,
    ))
    releases = []
    for index, match in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        details: list[str] = []
        section = ""
        for raw in text[match.end():end].splitlines():
            line = raw.strip()
            if line.startswith("### "):
                section = line[4:].strip()
                continue
            if line.startswith("- "):
                detail = line[2:].strip()
                if section:
                    detail = f"{section}: {detail}"
                details.append(detail)
            elif line and details and not line.startswith(("#", "```")):
                details[-1] += " " + line
        details = [re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", item)
                   .replace("**", "").replace("`", "") for item in details]
        releases.append({"version": match.group(1), "date": match.group(2),
                         "details": details})
    return releases


@app.get("/api/releases")
def releases():
    return {"current_version": _app_version(), "releases": _release_notes()}


# ── controller / agent migration foundation ───────────────────────────────
@app.get("/api/hub/controller")
def controller_status():
    return {
        "settings": control_plane.public_settings(),
        "runtime": control_plane.runtime.status(),
        "readiness": control_plane.runtime.readiness(),
        "capacity": control_plane.runtime.capacity(),
    }


@app.put("/api/hub/controller")
async def controller_save_settings(body: ControllerSettingsBody):
    try:
        coordinator = _repair_coordinator_from_app()
        with coordinator.controller_mutation():
            current = control_plane.load_settings()
            proposed_identity = (
                body.role, body.site_id, body.site_name, body.controller_id,
            )
            current_identity = tuple(current.get(key) for key in (
                "role", "site_id", "site_name", "controller_id",
            ))
            if proposed_identity != current_identity:
                coordinator.require_controller_identity_mutable()
            with settings_writer_lock():
                control_plane.save_settings(
                    body.model_dump(exclude={"database_url", "clear_database_url", "machine_name"}),
                    new_database_url=body.database_url,
                    clear_database_url=body.clear_database_url,
                )
                if body.machine_name is not None:
                    from .registry import set_label
                    set_label("local", body.machine_name)
                # Controller mode is immediately usable: both credentials exist as
                # soon as the role is saved. Existing permanent codes are retained.
                if control_plane.load_settings()["role"] == "controller":
                    peers.fleet_token()
                    code = enrollment.enrollment_credential_status(include_code=True)
                    if not code.get("active") or not code.get("code"):
                        enrollment.create_enrollment_code()
    except RepairStoreError as exc:
        _raise_registry_mutation_error(exc)
    except SettingsWriterBusy as exc:
        raise HTTPException(423, {"code": "settings_writer_busy"}) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    # Return an immediate, truthful database/schema result rather than making
    # the operator wait for the next ten-second heartbeat.
    return await control_plane.runtime.check_now()


@app.post("/api/hub/controller/check")
async def controller_check_database():
    return await control_plane.runtime.check_now()


@app.post("/api/hub/setup/controller")
def setup_new_location_controller(request: Request, body: SimpleControllerSetupBody):
    if not is_loopback(request):
        raise HTTPException(403, "Set up this Mac from its local Studio Hub dashboard.")
    try:
        profile_id = body.hardware_profile_id or hardware_profiles.local_hardware().get(
            "profile_id")
        if not profile_id:
            raise ValueError("This Mac's chip and RAM could not be matched automatically; choose a hardware profile.")
        coordinator = _repair_coordinator(request)
        with coordinator.controller_mutation(identity=True):
            return enrollment.configure_new_controller(
                body.location_name.strip(), body.site_id.strip().lower(),
                profile_id,
            )
    except RepairStoreError as exc:
        _raise_registry_mutation_error(exc)
    except SettingsWriterBusy as exc:
        raise HTTPException(423, {"code": "settings_writer_busy"}) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def _can_manage_enrollment(request: Request) -> bool:
    if is_loopback(request) or auth.valid_browser_session(
            request.cookies.get(auth.SESSION_COOKIE_NAME)):
        return True
    offered = auth.presented_token(request)
    return bool(offered and secrets.compare_digest(offered, HUB_TOKEN))


def _can_configure_this_hub(request: Request) -> bool:
    """Local setup also works from an owner-authenticated remote browser."""
    return _can_manage_enrollment(request)


def _repair_coordinator(request: Request) -> EnrollmentRepairCoordinator:
    service = getattr(request.app.state, "enrollment_repair_coordinator", None)
    if service is None:
        raise HTTPException(503, {"code": "repair_service_unavailable"})
    return service


def _repair_coordinator_from_app() -> EnrollmentRepairCoordinator:
    service = getattr(app.state, "enrollment_repair_coordinator", None)
    if service is None:
        raise HTTPException(503, {"code": "repair_service_unavailable"})
    return service


def _raise_registry_mutation_error(exc: RepairStoreError) -> None:
    status = 423 if exc.code == "enrollment_repair_busy" else 409
    raise HTTPException(status, {"code": str(exc.code)[:80]}) from exc


def _repair_executor(request: Request) -> RepairExecutor:
    service = getattr(request.app.state, "enrollment_repair_executor", None)
    if service is None:
        raise HTTPException(503, {"code": "repair_service_unavailable"})
    return service


def _require_repair_owner(request: Request) -> EnrollmentRepairCoordinator:
    owner = is_loopback(request) or auth.valid_browser_session(
        request.cookies.get(auth.SESSION_COOKIE_NAME)
    )
    if not owner:
        raise HTTPException(403, {"code": "owner_access_required"})
    if control_plane.load_settings().get("role") != "controller":
        raise HTTPException(409, {"code": "controller_role_required"})
    return _repair_coordinator(request)


def _require_repair_service(
    request: Request,
    *,
    expected_source: str | None = None,
) -> tuple[str, str]:
    try:
        return auth.require_exact_fleet_service_request(
            request, expected_source=expected_source,
        )
    except auth.ExactFleetServiceRequestError as exc:
        raise HTTPException(exc.status_code, {"code": exc.code}) from exc


_REPAIR_ERROR_HTTP = {
    "dispatch_invalid": 400,
    "redemption_invalid": 400,
    "claim_invalid": 400,
    "fleet_token_mismatch": 401,
    "fleet_token_missing": 503,
    "fleet_token_unavailable": 503,
    "private_source_required": 403,
    "source_host_mismatch": 403,
    "target_binding_mismatch": 403,
    "callback_source_mismatch": 403,
    "callback_url_invalid": 403,
    "ticket_expired": 410,
    "enrollment_repair_busy": 423,
    "settings_writer_busy": 423,
    "transport_unavailable": 503,
}


def _raise_repair_error(exc: RepairStoreError | RepairExecutorError) -> None:
    code = str(exc.code)[:80]
    status = _REPAIR_ERROR_HTTP.get(code, 409)
    raise HTTPException(status, {"code": code}) from exc


def _repair_status_expected_source(executor: Any, request_id: str) -> str:
    reader = getattr(executor, "expected_status_source", None)
    if callable(reader):
        return str(reader(request_id))
    journal_reader = getattr(executor, "_load_journal", None)
    if not callable(journal_reader):
        raise RepairExecutorError("request_not_found")
    journal = journal_reader()
    if (
        not isinstance(journal, dict)
        or str(journal.get("request_id", "")) != request_id
        or not journal.get("controller_address")
    ):
        raise RepairExecutorError("request_not_found")
    return str(journal["controller_address"])


def _registry_with_entries(
    rows: list[dict[str, Any]], entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Predict the post-write registry for pre-lock address resolution."""
    by_id = {str(row.get("id")): dict(row) for row in rows if row.get("id")}
    for entry in entries:
        studio_id = str(entry.get("id") or "")
        if studio_id:
            by_id[studio_id] = {**by_id.get(studio_id, {}), **entry}
    return list(by_id.values())


def _registry_identity_changes(
    rows: list[dict[str, Any]], entries: list[dict[str, Any]],
) -> bool:
    current = {str(row.get("id")): row for row in rows if row.get("id")}
    identity_fields = ("machine", "host", "port")
    return any(
        str(entry.get("id") or "") not in current
        or any(
            current[str(entry["id"])].get(field) != entry.get(field)
            for field in identity_fields
        )
        for entry in entries
        if entry.get("id")
    )


def _reload_registry_and_note_repair(prepared_registry: Any = None) -> None:
    """Reload once, then notify repair and managed-release coordinators."""
    monitor.reload_registry()
    coordinator = getattr(app.state, "enrollment_repair_coordinator", None)
    if coordinator is not None:
        coordinator.note_registry_reload(
            prepared_registry if prepared_registry is not None else monitor.registry
        )
    _reconcile_managed_registry()


_REPAIR_OWNER_OBSERVED_FIELDS = (
    "role_matches", "site_id_matches", "site_name_matches",
    "controller_id_matches", "parent_controller_url_matches",
)
_REPAIR_OWNER_SECRET_KEYS = ("ticket", "token", "claim", "credential")


def _sanitize_repair_owner_payload(value: Any) -> Any:
    """Recursively enforce a credential-free owner-facing repair read model."""
    current_token = peers.current_fleet_token()

    def sanitize(item: Any, *, key: str = "") -> Any:
        if isinstance(item, dict):
            if key == "observed_identity":
                return {
                    field: bool(item[field])
                    for field in _REPAIR_OWNER_OBSERVED_FIELDS
                    if type(item.get(field)) is bool
                }
            result = {}
            for raw_key, raw_value in item.items():
                field = str(raw_key)
                lowered = field.lower()
                if any(secret in lowered for secret in _REPAIR_OWNER_SECRET_KEYS):
                    continue
                if current_token and current_token in field:
                    continue
                result[field] = sanitize(raw_value, key=field)
            return result
        if isinstance(item, (list, tuple)):
            return [sanitize(child) for child in item]
        if isinstance(item, str) and current_token and current_token in item:
            return item.replace(current_token, "[redacted]")
        return item

    return sanitize(value)


@app.post("/api/hub/enrollment-repairs", status_code=202)
def create_enrollment_repair(
    request: Request,
    body: EnrollmentRepairBatchBody,
):
    coordinator = _require_repair_owner(request)
    if not enrollment_repair.NEW_ISSUANCE_ENABLED:
        raise HTTPException(503, {"code": "repair_issuance_disabled"})
    try:
        return _sanitize_repair_owner_payload(
            coordinator.create_batch(body.machines)
        )
    except (RepairStoreError, ValueError) as exc:
        if isinstance(exc, RepairStoreError):
            _raise_repair_error(exc)
        raise HTTPException(409, {"code": "repair_request_rejected"}) from exc


@app.get("/api/hub/enrollment-repairs/eligibility")
def enrollment_repair_eligibility(request: Request):
    return _sanitize_repair_owner_payload(
        _require_repair_owner(request).eligibility()
    )


@app.get("/api/hub/enrollment-repairs/{batch_id}")
def enrollment_repair_batch(request: Request, batch_id: str):
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", batch_id):
        raise HTTPException(404, {"code": "repair_batch_not_found"})
    batch = _require_repair_owner(request).batch(batch_id)
    if batch is None:
        raise HTTPException(404, {"code": "repair_batch_not_found"})
    return _sanitize_repair_owner_payload(batch)


@app.post("/api/hub/enrollment-repair/apply")
async def apply_enrollment_repair(
    request: Request,
    body: EnrollmentRepairDispatchBody,
):
    _require_repair_service(request)
    try:
        origin = await asyncio.to_thread(
            resolve_private_origin, body.controller_url,
        )
        direct_source, _token = _require_repair_service(
            request, expected_source=origin.address,
        )
        return await _repair_executor(request).apply(
            body.model_dump(by_alias=True), direct_source=direct_source,
        )
    except (RepairStoreError, RepairExecutorError) as exc:
        _raise_repair_error(exc)
    except (OSError, TypeError, ValueError) as exc:
        raise HTTPException(403, {"code": "callback_url_invalid"}) from exc


@app.post("/api/hub/enrollment-repair-tickets/redeem")
async def redeem_enrollment_repair_ticket(
    request: Request,
    body: EnrollmentRepairRedemptionBody,
):
    direct_source, token = _require_repair_service(request)
    try:
        snapshot = await asyncio.to_thread(
            registry.repair_machine_snapshot,
            list(monitor.registry),
            body.target_machine_id,
        )
        direct_source, token = _require_repair_service(
            request, expected_source=snapshot.resolved_address,
        )
        return await _repair_coordinator(request).redeem(
            body.model_dump(by_alias=True), direct_source=direct_source, fleet_token=token,
        )
    except registry.RepairRegistryAmbiguity as exc:
        raise HTTPException(403, {"code": "source_host_mismatch"}) from exc
    except (RepairStoreError, RepairExecutorError) as exc:
        _raise_repair_error(exc)


@app.get("/api/hub/enrollment-repair/status/{request_id}")
def enrollment_repair_status(request: Request, request_id: str):
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", request_id):
        raise HTTPException(400, {"code": "request_invalid"})
    _require_repair_service(request)
    executor = _repair_executor(request)
    try:
        expected_source = _repair_status_expected_source(executor, request_id)
        direct_source, _token = _require_repair_service(
            request, expected_source=expected_source,
        )
        return executor.status(request_id, direct_source=direct_source)
    except (RepairStoreError, RepairExecutorError) as exc:
        _raise_repair_error(exc)


@app.get("/api/hub/enrollment-codes")
def get_agent_enrollment_code(request: Request):
    return enrollment.enrollment_credential_status(
        include_code=_can_manage_enrollment(request))


@app.post("/api/hub/enrollment-codes")
def create_agent_enrollment_code(request: Request):
    if not _can_manage_enrollment(request):
        raise HTTPException(403, "Owner access is required to rotate the enrollment code.")
    try:
        return enrollment.create_enrollment_code()
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.delete("/api/hub/enrollment-codes")
def revoke_agent_enrollment_code(request: Request):
    if not _can_manage_enrollment(request):
        raise HTTPException(403, "Owner access is required to revoke the enrollment code.")
    try:
        return enrollment.revoke_enrollment_credential()
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/hub/enrollment/info")
def enrollment_info(request: Request):
    """Private, read-only information used before sending an enrollment code."""
    if not enrollment.private_request_host(request.client.host if request.client else None):
        raise HTTPException(403, "Enrollment discovery is available only over a private LAN or Tailscale link.")
    settings = control_plane.load_settings()
    status = enrollment.enrollment_credential_status(include_code=False)
    return {
        "schema_version": 1,
        "repair_schema_version": 1,
        "role": settings["role"],
        "site_id": settings["site_id"],
        "site_name": settings["site_name"],
        "controller_id": settings["controller_id"],
        "version": _app_version(),
        "enrollment_active": status["active"],
    }


@app.post("/api/hub/enrollment/claim")
async def claim_agent_enrollment(request: Request, body: EnrollmentCodeBody):
    if not enrollment.private_request_host(request.client.host if request.client else None):
        raise HTTPException(403, "Agent enrollment is available only over a private LAN or Tailscale link.")
    try:
        if not body.machine:
            return enrollment.claim_enrollment_code(body.code)
        host, machine, profile_id = _registration_identity({
            "host": request.client.host,
            "machine": body.machine,
            "hardware_profile_id": body.hardware_profile_id,
        })
        modalities = (body.modalities if body.modalities is not None
                      else list(PRODUCTION_STUDIO_MODALITIES))
        unknown = sorted(set(modalities) - set(registry.MODALITY_PORT))
        if unknown:
            raise ValueError(f"unknown modalities: {unknown}")
        entries = registry.build_machine_entries(host, machine, modalities)
        coordinator = _repair_coordinator(request)
        registration_resolution = coordinator.resolve_enrollment_registration(
            machine, host,
        )
        prepared_reload = None
        resolve_rows = getattr(coordinator, "resolve_registry_rows", None)
        if callable(resolve_rows):
            prepared_reload = resolve_rows(
                _registry_with_entries(list(monitor.registry), entries)
            )
        with coordinator.controller_mutation(machine=machine):
            coordinator.require_enrollment_registration_mutable(
                machine, host, resolved=registration_resolution,
            )
            claim = enrollment.claim_enrollment_code(body.code)
            added = registry.add_user_entries(
                entries,
            )
            _reload_registry_and_note_repair(prepared_reload)
            profile = hardware_profiles.set_machine_hardware_profile(
                machine, profile_id) if profile_id else None
            if body.machine_name:
                registry.set_label(machine, body.machine_name)
            claim["registration"] = {
                "machine": machine,
                "host": host,
                "registered": added,
                "hardware_profile": profile,
            }
        return claim
    except RepairStoreError as exc:
        if exc.code == "enrollment_repair_busy":
            raise HTTPException(423, {"code": exc.code}) from exc
        raise HTTPException(409, {"code": exc.code}) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/hub/setup/check-controller")
async def check_existing_location(request: Request, body: ControllerProbeBody):
    if not _can_configure_this_hub(request):
        raise HTTPException(403, "Open this Hub locally or sign in as its owner before changing its location.")
    try:
        return await enrollment.probe_remote_controller(body.controller_url)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/hub/setup/join")
async def join_existing_location(request: Request, body: AgentJoinBody):
    if not _can_configure_this_hub(request):
        raise HTTPException(403, "Open this Hub locally or sign in as its owner before changing its location.")
    try:
        local_hardware = hardware_profiles.local_hardware()
        profile_id = body.hardware_profile_id or local_hardware.get("profile_id")
        if not profile_id:
            raise ValueError("This Mac's chip and RAM could not be matched automatically; choose a hardware profile.")
        machine_name = (str(body.machine_name or "").strip()
                        or str(local_hardware.get("machine_name") or "").strip()
                        or None)
        agent_id = enrollment.suggested_local_hub_id(profile_id)
        claim = await enrollment.claim_remote(
            body.controller_url, body.enrollment_code,
            registration={
                "machine": agent_id,
                "machine_name": machine_name,
                "hardware_profile_id": profile_id,
                "modalities": list(PRODUCTION_STUDIO_MODALITIES),
            },
        )
        coordinator = _repair_coordinator(request)
        with coordinator.controller_mutation(identity=True):
            result = enrollment.configure_joined_agent(
                body.controller_url, profile_id, claim,
                machine_name=machine_name,
            )
        if claim.get("registration"):
            result["controller_registration"] = claim["registration"]
            result["checklist"].insert(1, "Controller registered this Mac and its Studios")
        return result
    except RepairStoreError as exc:
        _raise_registry_mutation_error(exc)
    except SettingsWriterBusy as exc:
        raise HTTPException(423, {"code": "settings_writer_busy"}) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/auto-update/status")
def automatic_update_status():
    return _truthful_hub_update_status()


def _truthful_hub_update_status() -> dict:
    """Distinguish a pulled checkout from code loaded by the live service."""
    result = dict(auto_updater.public_status())
    disk_version = result.get("installed_version")
    loaded_version = _app_version()
    result["disk_version"] = disk_version
    result["loaded_version"] = loaded_version
    result["installed_version"] = loaded_version
    if disk_version and disk_version != loaded_version:
        result.update(
            state="restart_required",
            restart_required=True,
            defer_reason=(
                f"v{disk_version} is on disk, but the running service is still "
                f"v{loaded_version}; restart Studio Hub to finish"
            ),
            last_update_result="Update downloaded; service restart still required",
        )
    else:
        result["restart_required"] = False
    return result


@app.get("/api/auto-update/readiness")
def automatic_update_readiness():
    return auto_updater.readiness_status()


@app.post("/api/auto-update/settings")
def automatic_update_settings(body: AutoUpdateSettingsBody):
    try:
        # exclude_none keeps an older client that never sends the drain timeout
        # from silently resetting the owner's stored value.
        return auto_updater.save_settings(body.model_dump(exclude_none=True))
    except UpdateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/auto-update/check")
def automatic_update_check():
    try:
        return auto_updater.trigger_check()
    except UpdateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/auto-update/update")
def automatic_update_run(body: AutoUpdateRequestBody):
    try:
        return auto_updater.trigger_update(after_current=body.after_current,
                                           target_commit=body.target_commit,
                                           target_version=body.target_version,
                                           operation_id=body.operation_id)
    except UpdateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/auto-update/retry")
def automatic_update_retry():
    try:
        return auto_updater.retry()
    except UpdateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/hub/maintenance/restart", status_code=202)
def restart_hub(body: HubRestartBody):
    """Safely restart the installed Hub service after returning this response."""
    try:
        safety = auto_updater.restart_safety()
    except UpdateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    blockers = _automatic_update_blockers()
    if blockers and not body.force:
        raise HTTPException(
            status_code=409,
            detail=(
                "Active work prevents a normal restart: "
                + "; ".join(blockers)
                + ". Retry with force=true only if interruption is acceptable."
            ),
        )
    from .control import restart_hub_service
    result = restart_hub_service()
    if not result["ok"]:
        raise HTTPException(status_code=409, detail=result["error"])
    return {
        **result,
        "expected_version": safety["expected_version"],
        "loaded_version": _app_version(),
        "forced": bool(body.force),
        "active_work": blockers,
        "message": "Restart accepted. Reconnect to this Hub after a few seconds.",
    }


@app.get("/api/hub/auto-updates")
async def fleet_automatic_update_status():
    result = await fleet_auto_updates.snapshot()
    truth = _truthful_hub_update_status()
    hub = next((row for row in result.get("apps", []) if row.get("kind") == "hub"), None)
    if hub is not None:
        hub.update(
            installed_version=truth["loaded_version"],
            disk_version=truth["disk_version"],
            state=truth["state"],
            restart_required=truth["restart_required"],
            defer_reason=truth.get("defer_reason"),
            last_update_result=truth.get("last_update_result"),
        )
    return result


@app.post("/api/hub/auto-updates/check-all")
async def fleet_automatic_update_check_all():
    return await fleet_auto_updates.check_all()


@app.post("/api/hub/auto-updates/{target_id}/mode")
async def fleet_automatic_update_mode(target_id: str, body: FleetAutoModeBody):
    try:
        return await fleet_auto_updates.set_mode(target_id, body.mode)
    except (ValueError, UpdateError, httpx.HTTPError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/hub/auto-updates/update-idle")
async def fleet_automatic_update_run(body: FleetAutoRunBody):
    try:
        return fleet_auto_updates.start_idle_updates(body.target_ids)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/hub/auto-updates/jobs")
def fleet_automatic_update_jobs():
    return {"updates": fleet_auto_updates.jobs()}


@app.get("/api/hub/auto-updates/jobs/{job_id}")
def fleet_automatic_update_job(job_id: str):
    job = fleet_auto_updates.job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="automatic fleet update not found")
    return job


@app.post("/api/hub/auto-updates/jobs/{job_id}/retry")
def retry_fleet_automatic_update_job(job_id: str):
    try:
        return fleet_auto_updates.retry_failed(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# ── canonical hub API ──────────────────────────────────────────────────────
@app.get("/api/hub/studios")
def studios():
    """Registry + live status per studio."""
    from .registry import load_labels, machine_enabled, studio_enabled

    labels = load_labels()
    out = []
    for s in monitor.registry:
        st = monitor.status.get(s["id"], {})
        machine = s.get("machine", "local")
        hardware_profile = hardware_profiles.machine_hardware_profile(machine)
        machine_label = labels.get(machine, machine)
        if machine != "local" and machine_label.casefold() == "local":
            reported = (peers.cached(machine) or {}).get("host") or {}
            reported_name = str(reported.get("machine_name") or "").strip()
            generated_name = hardware_profiles.generated_terranash_hostname(
                machine, hardware_profile)
            candidate = reported_name or generated_name
            if candidate and candidate.casefold() != "local":
                machine_label = candidate
        out.append({**s, "url": base_url(s),
                    "machine_label": machine_label, **st,
                    "enabled": studio_enabled(machine, s["id"]),
                    "machine_enabled": machine_enabled(machine),
                    "hardware_profile_id": (hardware_profile or {}).get("id")})
    return {"studios": out}


def _registered_machine_ids() -> set[str]:
    return {s.get("machine", "local") for s in monitor.registry}


@app.get("/api/hub/registry/hardware-profiles")
def registry_hardware_profiles():
    """Reusable machine classes plus persistent machine assignments."""
    return hardware_profiles.hardware_profile_catalog(_registered_machine_ids())


@app.post("/api/hub/registry/hardware-profiles")
def create_registry_hardware_profile(body: HardwareProfileBody):
    """Add a reusable profile without changing existing machine records."""
    try:
        profile = hardware_profiles.add_custom_hardware_profile(body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "profile": profile,
        "catalog": hardware_profiles.hardware_profile_catalog(_registered_machine_ids()),
    }


@app.put("/api/hub/registry/machines/{machine}/hardware-profile")
def set_registry_machine_hardware_profile(
    machine: str, body: MachineHardwareProfileBody,
):
    if machine not in _registered_machine_ids():
        raise HTTPException(404, f"no registered machine {machine!r}")
    try:
        profile = hardware_profiles.set_machine_hardware_profile(
            machine, body.hardware_profile_id,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "machine": machine, "hardware_profile": profile}


@app.post("/api/hub/registry/machines/{machine}/name")
def rename_machine(machine: str, body: dict):
    """Set a friendly display name for a machine (the underlying key is
    unchanged, so control/routing keep working). Works for 'local' too.
    An empty name clears the alias."""
    from .registry import set_label
    set_label(machine, body.get("name", ""))
    return {"ok": True, "machine": machine, "name": body.get("name") or machine}


@app.post("/api/hub/registry/machines/{machine}/enabled")
async def set_machine_enabled_ep(machine: str, body: dict):
    """Enable/disable a machine in the fleet. A disabled machine stays
    registered and monitored but the broker sends it no jobs — use it to quiesce
    a machine before updating/restarting it. Body: {"enabled": <bool>}."""
    from .registry import set_machine_enabled
    enabled = bool(body.get("enabled", True))
    set_machine_enabled(machine, enabled)
    if enabled:
        _reconcile_managed_registry()
    return {"ok": True, "machine": machine, "enabled": enabled}


@app.post("/api/hub/registry/studios/{studio_id:path}/enabled")
def set_studio_enabled_ep(studio_id: str, body: dict):
    """Pause/resume new work for one Studio while leaving it online.

    Running work is deliberately not cancelled. The machine-wide toggle remains
    the master switch and can suppress every Studio regardless of these values.
    """
    from .registry import set_studio_enabled

    studio = next((row for row in monitor.registry if row["id"] == studio_id), None)
    if studio is None:
        raise HTTPException(404, f"no registered studio {studio_id!r}")
    if not isinstance(body.get("enabled"), bool):
        raise HTTPException(400, "enabled must be true or false")
    enabled = body["enabled"]
    machine = studio.get("machine", "local")
    set_studio_enabled(machine, studio_id, enabled)
    return {"ok": True, "studio": studio_id, "machine": machine, "enabled": enabled}


@app.get("/api/hub/health")
def hub_health():
    up = sum(1 for st in monitor.status.values() if st.get("status") == "up")
    return {
        "ok": True,
        "studios_total": len(monitor.registry),
        "studios_up": up,
        "statuses": monitor.status,
    }


@app.get("/api/hub/catalog")
async def hub_catalog(
    modality: str | None = Query(None),
    q: str | None = Query(None, description="substring match on repo/label"),
    downloaded: bool | None = Query(None),
    force: bool = Query(False, description="bypass the 60s cache"),
):
    agg = await monitor.aggregate_catalog(force=force)
    models = agg["models"]
    if modality:
        models = [m for m in models if m.get("hub_modality") == modality]
    if q:
        needle = q.lower()
        models = [
            m for m in models
            if needle in str(m.get("repo", "")).lower()
            or needle in str(m.get("label", "")).lower()
        ]
    if downloaded is not None:
        # hub_cached is the corrected download flag (cache.state == 'cached').
        models = [m for m in models if bool(m.get("hub_cached")) == downloaded]
    return {
        "models": models,
        "count": len(models),
        "total_unfiltered": agg["total"],
        "per_studio": agg["per_studio"],
    }


@app.get("/api/hub/resources")
def hub_resources(local_only: bool = Query(False)):
    """Host memory/CPU + per-studio process stats.

    Local studios are measured directly. Remote studios' stats come from each
    machine's own peer Hub (cached by the poll loop). `local_only=true` returns
    ONLY this machine — peers call with it to prevent recursive fan-out.
    Remote studios are keyed by their local id (= modality) so a peer's reply
    maps straight onto our federated ids."""
    from .registry import machine_enabled
    protections = broker.machine_protection_snapshot()
    local_proxy = proxy_stats()
    machines = {"local": {"host": host_stats(), "reachable": True,
                          "enabled": machine_enabled("local"),
                          "hardware_profile": hardware_profiles.machine_hardware_profile("local"),
                          "proxy": local_proxy,
                          "protection": protections.get("local")}}
    per_studio = {}
    for s in monitor.registry:
        machine = s.get("machine", "local")
        st = monitor.status.get(s["id"], {})
        if machine == "local":
            process = studio_process_stats(s["port"]) if st.get("status") == "up" else None
            per_studio[s["id"]] = process
        elif local_only:
            continue
        else:
            peer = peers.cached(machine)
            machines[machine] = {
                "host": peer["host"] if peer else None,
                "reachable": bool(peer and peer.get("reachable")),
                "has_hub": bool(peer and peer.get("host") is not None),
                # why the peer is (dis)connected, for the Remote tab:
                # connected | no_hub | unreachable | token_rejected | no_token | pending
                "status": (peer.get("status") if peer else "pending"),
                # operator toggle — a disabled machine takes no jobs
                "enabled": machine_enabled(machine),
                "hardware_profile": hardware_profiles.machine_hardware_profile(machine),
                "proxy": peer.get("proxy") if peer else None,
                "protection": protections.get(machine),
            }
            per_studio[s["id"]] = (
                (peer.get("studios", {}) or {}).get(s["modality"]) if peer else None)
    return {"host": machines["local"]["host"], "proxy": local_proxy,
            "machines": machines,
            "studios": per_studio, "fleet_token_set": peers.fleet_token() is not None,
            "ts": time.time()}


def _build_summary() -> dict:
    workloads = {
        studio_id: {"kind": "generation"}
        for studio_id in broker.busy_studios()
    }
    chat_active = chat_jobs.active_assignments()
    for studio_id in chat_jobs.busy_studios:
        workloads[studio_id] = chat_active.get(studio_id, {"kind": "chat"})
    transcription_active = transcription_jobs.active_assignments()
    for studio_id in transcription_jobs.busy_studios:
        workloads[studio_id] = transcription_active.get(
            studio_id, {"kind": "transcription"})
    resources = hub_resources(local_only=False)
    studio_list = studios()["studios"]
    for s in studio_list:
        s["workload"] = workloads.get(s["id"])
        s["busy"] = s["workload"] is not None
    now = time.time()
    active_alerts = sum(1 for e in alerts.recent(100)
                        if now - e["ts"] < 3600 and e["kind"] != "studio_recovered")
    return {
        "hub": {"title": TITLE, "app_version": _app_version()},
        "studios": studio_list,
        # NB: pass local_only explicitly. Calling hub_resources() bare uses the
        # FastAPI Query(False) default object, which is truthy — that would drop
        # every remote machine from the summary (and thus the live dashboard).
        "resources": resources,
        "control_plane": {
            "settings": control_plane.public_settings(),
            "runtime": control_plane.runtime.status(),
            "readiness": control_plane.runtime.readiness(),
        },
        "watchdog": metrics.watchdog_status(),
        "jobs": [broker.batch_summary(b) for b in broker.batches.values()],
        "alerts_active": active_alerts,
    }


@app.get("/api/hub/summary")
def hub_summary():
    """One-shot dashboard payload (polling fallback)."""
    return _build_summary()


async def _sse_summary(request, interval: float = 2.0):
    """Yield the summary as SSE frames until the client disconnects. Extracted
    from the endpoint so it's unit-testable without an endless HTTP stream."""
    import asyncio
    import json
    try:
        while True:
            try:
                if await request.is_disconnected():
                    break
            except Exception:
                break
            try:
                yield f"data: {json.dumps(_build_summary())}\n\n"
            except Exception:
                yield ": error\n\n"  # keep the stream alive on a transient hiccup
            await asyncio.sleep(interval)
    except asyncio.CancelledError:  # client went away mid-sleep
        pass


@app.get("/api/hub/stream")
async def hub_stream(request: Request):
    """Server-Sent Events: pushes the summary every ~2s so the dashboard updates
    live instead of polling. Falls back gracefully — the dashboard reverts to
    /api/hub/summary polling if the stream drops. Auth: loopback exempt; a
    remote dashboard authenticates one normal header-bearing request first,
    then EventSource uses the resulting HttpOnly same-site session cookie."""
    return StreamingResponse(_sse_summary(request), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "Connection": "keep-alive",
                                      "X-Accel-Buffering": "no"})


@app.get("/api/hub/access")
def hub_access(request: Request):
    """Shareable remote URLs for this Hub. The token itself is only revealed
    to loopback clients — read it on the Hub machine, use it everywhere else."""
    import ipaddress
    import socket

    import psutil as _ps

    port = request.url.port or 47873
    addresses = []
    for ifname, addrs in _ps.net_if_addrs().items():
        for a in addrs:
            if a.family != socket.AF_INET or a.address.startswith("127."):
                continue
            ip = ipaddress.ip_address(a.address)
            kind = "tailscale" if ip in ipaddress.ip_network("100.64.0.0/10") \
                else ("lan" if ip.is_private else "public")
            addresses.append({
                "interface": ifname, "ip": a.address, "kind": kind,
                "url": f"http://{a.address}:{port}",
            })
    addresses.sort(key=lambda x: {"tailscale": 0, "lan": 1, "public": 2}[x["kind"]])
    out = {"addresses": addresses, "auth": "token required for non-loopback clients"}
    owner_session = auth.valid_browser_session(
        request.cookies.get(auth.SESSION_COOKIE_NAME)
    )
    if is_loopback(request) or owner_session:
        out["token"] = HUB_TOKEN
    return out


# ── metrics + watchdog ─────────────────────────────────────────────────────
@app.get("/api/hub/metrics")
def hub_metrics(minutes: int = Query(60, ge=1, le=1440)):
    return metrics.get_metrics(minutes)


@app.get("/api/hub/watchdog")
def hub_watchdog():
    return metrics.watchdog_status()


# NOTE: defined before the generic {action} route so it wins the match.
@app.post("/api/hub/studios/{studio_id}/watchdog")
def studio_watchdog(studio_id: str, body: dict):
    if not any(s["id"] == studio_id for s in monitor.registry):
        raise HTTPException(404, f"unknown studio: {studio_id}")
    metrics.set_watchdog(studio_id, bool(body.get("enabled")))
    return {"ok": True, "studio": studio_id,
            "watchdog": metrics.watchdog_status().get(studio_id)}


# ── broadcaster ────────────────────────────────────────────────────────────
def _pick_studios(ids: list | None) -> list[dict]:
    if not ids:
        return monitor.registry
    return [s for s in monitor.registry if s["id"] in ids]


@app.post("/api/hub/broadcast/download")
async def hub_broadcast_download(body: dict):
    repo = body.get("repo")
    if not repo:
        raise HTTPException(400, "repo is required")
    import httpx
    studios = _pick_studios(body.get("studios"))
    async with httpx.AsyncClient() as client:
        results = await broadcast.broadcast_download(
            client, studios, repo, body.get("token"))
    download = broadcast.record_download(repo, studios, results)
    return {"repo": repo, "results": results, "download": download}


@app.get("/api/hub/broadcast/downloads")
async def hub_broadcast_downloads():
    async with httpx.AsyncClient() as client:
        downloads = await broadcast.refresh_downloads(client, monitor.registry)
    return {"downloads": downloads}


@app.delete("/api/hub/broadcast/downloads/{run_id}/studios/{studio_id:path}")
async def hub_cancel_broadcast_download(run_id: str, studio_id: str):
    try:
        async with httpx.AsyncClient() as client:
            run = await broadcast.cancel_download(
                client, run_id, studio_id, monitor.registry)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(503, f"worker could not cancel the download: {exc}") from exc
    return {"ok": True, "download": run}


@app.get("/api/hub/model-baselines")
def hub_model_baselines():
    """Cache-only status for the GenStudio-approved fleet model catalog."""
    return model_baselines.snapshot()


@app.post("/api/hub/model-baselines")
def save_hub_model_baselines(body: ModelBaselineSettingsBody):
    return model_baselines.save_settings(enabled=body.enabled)


@app.post("/api/hub/model-baselines/reconcile")
async def reconcile_hub_model_baselines():
    """Check every eligible sibling Studio; missing targets retry safely."""
    return await model_baselines.reconcile()


@app.post("/api/hub/fleet-model-catalog")
async def receive_fleet_model_catalog(request: Request, body: dict[str, Any]):
    """Persist GenStudio desired state and reconcile it without blocking sync."""
    if not auth.valid_machine_token(request, HUB_TOKEN):
        raise HTTPException(
            401,
            "Hub or fleet token required for the private fleet model catalog.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if control_plane.public_settings().get("role") != "controller":
        raise HTTPException(409, "Only a location controller accepts fleet desired state.")
    try:
        changed, snapshot = model_baselines.replace_catalog(body)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    scheduled = model_baselines.trigger_reconcile() if changed else False
    refused = snapshot.get("refused_cloud_models") or []
    if refused:
        _hub_log.warning(
            "GenStudio fleet catalog push %s dropped %d cloud model(s): %s",
            snapshot["catalog_revision"], len(refused),
            ", ".join(row["repo"] for row in refused),
        )
    return {
        "ok": True,
        "accepted": True,
        "changed": changed,
        "reconcile_scheduled": scheduled,
        "revision": snapshot["catalog_revision"],
        "approved_models": snapshot["summary"]["approved_models"],
        # Never a silent drop: Studio Hub caches local fleet models only, so
        # cloud rows are refused and named back to the caller.
        "refused_cloud_models": refused,
    }


def _hf_studios(ids: list | None) -> list[dict]:
    """Return every registered Studio that can hold a Hugging Face token."""
    return [studio for studio in _pick_studios(ids)
            if studio.get("modality") != "render"]


async def _validate_hf_token(token: str) -> dict:
    """Validate a token without storing or returning it."""
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            response = await client.get(
                "https://huggingface.co/api/whoami-v2",
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.HTTPError as exc:
        raise HTTPException(503, "Could not reach Hugging Face to validate the token.") from exc
    if response.status_code != 200:
        raise HTTPException(400, "Hugging Face rejected this token.")
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    return {"name": payload.get("name") or payload.get("fullname")}


async def _broadcast_saved_hf_token(studios: list[dict]) -> dict:
    token = hf_credentials.get_token()
    if not token:
        raise HTTPException(409, "No Hugging Face credential is saved on this Hub.")
    async with httpx.AsyncClient() as client:
        results = await broadcast.broadcast_hf_token(client, studios, token)
    credential = hf_credentials.record_delivery(results)
    return {"credential": credential, "results": results}


@app.get("/api/hub/credentials/huggingface")
def get_huggingface_credential():
    """Return non-secret Hugging Face credential and delivery status."""
    return hf_credentials.status()


@app.post("/api/hub/credentials/huggingface")
async def save_huggingface_credential(body: dict):
    """Validate, store in Keychain, and fan out a Hugging Face token."""
    token = str(body.get("token") or "").strip()
    if not token:
        raise HTTPException(400, "token is required")
    await _validate_hf_token(token)
    try:
        hf_credentials.save_token(token)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    return await _broadcast_saved_hf_token(_hf_studios(body.get("studios")))


@app.post("/api/hub/credentials/huggingface/retry")
async def retry_huggingface_credential(body: dict | None = None):
    """Retry delivery to all registered or explicitly selected Studios."""
    body = body or {}
    return await _broadcast_saved_hf_token(_hf_studios(body.get("studios")))


@app.post("/api/hub/broadcast/hf-token")
async def hub_broadcast_hf_token(body: dict):
    """Compatibility alias for the durable credential endpoint."""
    return await save_huggingface_credential(body)


@app.post("/api/hub/broadcast/env")
def hub_broadcast_env(body: dict):
    key, value = body.get("key"), body.get("value")
    if not key or value is None:
        raise HTTPException(400, "key and value are required")
    out = broadcast.broadcast_env(_pick_studios(body.get("studios")), key, str(value))
    if "error" in out:
        raise HTTPException(400, out["error"])
    return out


# ── job broker / Swarm Batch ───────────────────────────────────────────────
@app.post("/api/hub/execution-assets/voice-references")
async def hub_stage_voice_reference(
    audio: UploadFile = File(...),
    source_asset_id: str = Form(...),
    source_sha256: str = Form(...),
    transcript: str = Form(""),
    transcript_segments_json: str = Form(""),
    language: str = Form(""),
    ttl_seconds: int = Form(execution_assets.DEFAULT_TTL_SECONDS),
):
    """Stage one checksum-bound GenStudio reference for a site attempt.

    This is temporary execution storage, not a second customer voice library.
    The source asset remains owned and retained by GenStudio.
    """
    if not control_plane.accepts_customer_jobs():
        raise HTTPException(409, "Stage customer assets on a controller Hub.")
    data = await audio.read(execution_assets.MAX_BYTES + 1)
    segments = None
    if transcript_segments_json.strip():
        try:
            segments = json.loads(transcript_segments_json)
        except json.JSONDecodeError as exc:
            raise HTTPException(400, {
                "code": "REFERENCE_TRANSCRIPT_SEGMENTS_INVALID",
                "detail": "Reference transcript segments are not valid JSON.",
            }) from exc
    try:
        asset = execution_assets.stage_voice_reference(
            audio_bytes=data,
            filename=audio.filename or "reference.wav",
            source_asset_id=source_asset_id,
            declared_sha256=source_sha256,
            transcript=transcript,
            transcript_segments=segments,
            language=language,
            ttl_seconds=ttl_seconds,
        )
    except execution_assets.ExecutionAssetError as exc:
        raise HTTPException(422, {"code": exc.code, "detail": exc.detail}) from exc
    return {"asset": execution_assets.public(asset)}


@app.delete("/api/hub/execution-assets/voice-references/{asset_id}")
def hub_delete_voice_reference(asset_id: str):
    try:
        deleted = execution_assets.delete(asset_id)
    except execution_assets.ExecutionAssetError as exc:
        raise HTTPException(404, {"code": exc.code, "detail": exc.detail}) from exc
    if not deleted:
        raise HTTPException(404, {
            "code": "VOICE_REFERENCE_ASSET_MISSING",
            "detail": "The private voice reference is already unavailable.",
        })
    return {"deleted": True, "asset_id": asset_id}


@app.post("/api/hub/jobs")
def hub_submit_jobs(envelope: dict):
    if not control_plane.accepts_customer_jobs():
        raise HTTPException(409, "This Hub is in agent mode; submit customer jobs to a controller.")
    result = broker.submit_batch(envelope)
    if result.get("code") == cloud_guard.REFUSAL_CODE:
        # A policy refusal, not a malformed request: this Hub is local-only.
        raise HTTPException(403, {"code": result["code"], "detail": result["error"]})
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


@app.get("/api/hub/jobs")
def hub_list_jobs():
    # Finished batches leave broker memory after a restart, but remain in the
    # SQLite ledger until the operator clears them.  Merge both sources so the
    # Jobs workspace is a durable history instead of a process-lifetime view.
    batches = {batch["id"]: batch for batch in ledger.load_finished_batches()}
    batches.update({batch["id"]: batch for batch in broker.batches.values()})
    return {"batches": [broker.batch_summary(batch)
                        for batch in sorted(batches.values(),
                                            key=lambda row: -row["created_at"])]}


@app.get("/api/hub/jobs/{batch_id}")
def hub_get_batch(batch_id: str):
    b = broker.batches.get(batch_id) or ledger.load_batch(batch_id)
    if b is None:
        raise HTTPException(404, "unknown batch")
    return {**broker.batch_summary(b),
            "items": [broker.public_item(b, item) for item in b["items"]]}


async def _open_worker_artifact(studio: dict, worker_artifact_url: str):
    """Open a worker stream early enough to preserve a verified MIME header."""
    from .peers import studio_request
    url, headers = studio_request(studio, worker_artifact_url)
    client = httpx.AsyncClient(follow_redirects=True)
    try:
        response = await client.send(client.build_request("GET", url, headers=headers), stream=True)
        response.raise_for_status()
    except Exception:
        await client.aclose()
        raise
    return client, response


@app.get("/api/hub/jobs/{batch_id}/items/{item_index}/artifact")
async def hub_proxy_job_artifact(batch_id: str, item_index: int):
    """Stream a worker artifact through Hub so clients need only Hub auth."""
    b = broker.batches.get(batch_id) or ledger.load_batch(batch_id)
    if not b:
        raise HTTPException(404, "unknown batch")
    item = next((i for i in b["items"] if i.get("index") == item_index), None)
    worker_artifact_url = (item or {}).get("worker_artifact_url") or (item or {}).get("artifact_url")
    if not item or item.get("state") != "done" or not worker_artifact_url:
        raise HTTPException(404, "artifact is not available")
    studio = next((s for s in monitor.registry if s["id"] == item.get("studio")), None)
    if not studio:
        raise HTTPException(503, "render worker is no longer registered")

    try:
        client, response = await _open_worker_artifact(studio, worker_artifact_url)
    except httpx.HTTPError as exc:
        raise HTTPException(502, "worker artifact could not be read") from exc

    async def close_worker_stream():
        await response.aclose()
        await client.aclose()

    media_type = artifact_metadata.media_type_for_proxy(
        b["modality"], item.get("media_type"), response.headers.get("content-type"))
    # Legacy completed voice jobs predate terminal metadata. Read and validate
    # their audio exactly once, persist it, then serve the same verified bytes.
    needs_voice_metadata = (
        b["modality"] == "voice"
        and not item.get("audio_duration_ms")
        and media_type in {"audio/wav", "application/octet-stream"}
    )
    if needs_voice_metadata:
        try:
            content = await response.aread()
            metadata = artifact_metadata.wav_metadata(content)
            item.update(metadata)
            item.pop("artifact_metadata_error", None)
            ledger.save_batch(b)
            return Response(content=content, media_type=metadata["media_type"], headers={
                "Content-Length": str(metadata["bytes"]),
                "X-Content-SHA256": metadata["sha256"],
            })
        except ValueError as exc:
            raise HTTPException(422, "voice artifact is not a validated WAV") from exc
        finally:
            await close_worker_stream()

    headers = {}
    if item.get("bytes"):
        headers["Content-Length"] = str(item["bytes"])
    if item.get("sha256"):
        headers["X-Content-SHA256"] = item["sha256"]
    return StreamingResponse(response.aiter_bytes(1024 * 1024), media_type=media_type,
                             headers=headers, background=BackgroundTask(close_worker_stream))


@app.post("/api/hub/jobs/{batch_id}/items/{item_index}/ack")
async def hub_ack_job_artifact(batch_id: str, item_index: int):
    """Start worker retention only after the main machine verifies receipt."""
    import httpx
    from .peers import studio_request
    b = broker.batches.get(batch_id) or ledger.load_batch(batch_id)
    if not b:
        raise HTTPException(404, "unknown batch")
    item = next((i for i in b["items"] if i.get("index") == item_index), None)
    studio = next((s for s in monitor.registry if s["id"] == (item or {}).get("studio")), None)
    if not item or not studio or not item.get("studio_job_id"):
        raise HTTPException(404, "worker job is not available")
    ack_url, ack_headers = studio_request(
        studio, f"/api/generate/jobs/{item['studio_job_id']}/ack")
    async with httpx.AsyncClient() as client:
        response = await client.post(
            ack_url, headers=ack_headers, timeout=15.0)
    if response.status_code >= 400:
        raise HTTPException(502, "render worker did not acknowledge receipt")
    item["receipt_acked_at"] = time.time()
    ledger.save_batch(b)
    return {"ok": True}


@app.delete("/api/hub/jobs/{batch_id}")
async def hub_cancel_batch(batch_id: str):
    result = await broker.cancel_batch(batch_id)
    if not result:
        raise HTTPException(404, "unknown batch")
    return {"ok": True, **{k: v for k, v in result.items() if k != "batch"}}


@app.post("/api/hub/jobs/cancel")
async def hub_cancel_batches(body: dict):
    modality = body.get("modality")
    if modality is not None and modality not in broker.MODALITY:
        raise HTTPException(400, "unknown modality")
    return {"ok": True, **await broker.cancel_batches(modality)}


@app.post("/api/hub/jobs/clear")
def hub_clear_finished_batches(body: dict):
    modality = body.get("modality")
    if modality is not None and modality not in broker.MODALITY:
        raise HTTPException(400, "unknown modality")
    result = broker.clear_finished_batches(modality=modality)
    return {"ok": True, **result,
            **ledger.remove_job_assets(result["batch_ids"])}


@app.post("/api/hub/jobs/{batch_id}/clear")
def hub_clear_finished_batch(batch_id: str):
    b = broker.batches.get(batch_id) or ledger.load_batch(batch_id)
    if not b:
        raise HTTPException(404, "unknown batch")
    if any(it.get("state") in ("queued", "running") for it in b.get("items", [])):
        raise HTTPException(409, "cancel the active batch before clearing it")
    result = broker.clear_finished_batches(batch_id=batch_id)
    return {"ok": True, **result,
            **ledger.remove_job_assets(result["batch_ids"])}


# ── asset ledger ───────────────────────────────────────────────────────────
@app.get("/api/hub/assets")
def hub_assets(q: str | None = None, modality: str | None = None,
               studio: str | None = None, batch_id: str | None = None,
               sort: str = Query("newest", pattern="^(newest|oldest|name|type|studio|model)$"),
               limit: int = Query(100, ge=1, le=500)):
    return {"assets": ledger.query_assets(q, modality, studio, batch_id, limit, sort)}


@app.get("/api/hub/models")
async def hub_models(modality: str | None = None, q: str | None = None,
                     downloaded: bool | None = None, force: bool = False):
    """Local models deduped by repo with per-machine availability."""
    rows = await monitor.models_by_repo(force=force)
    for row in rows:
        row["memory_admission"] = (
            None if not memory_admission.applies_to(row.get("modality"))
            else memory_admission.describe(row["repo"], row)
        )
    if modality:
        rows = [r for r in rows if r["modality"] == modality]
    if q:
        needle = q.lower()
        rows = [r for r in rows
                if needle in r["repo"].lower() or needle in r["label"].lower()]
    if downloaded is not None:
        rows = [r for r in rows if r["downloaded"] == downloaded]
    return {"models": rows, "count": len(rows)}


def _require_exposure_owner(request: Request) -> None:
    if is_loopback(request):
        return
    if auth.valid_browser_session(request.cookies.get(auth.SESSION_COOKIE_NAME)):
        return
    raise HTTPException(
        403,
        "Model exposure changes require the owner browser session or the Hub Mac.",
    )


def _require_qualification_operator(request: Request) -> None:
    """Authorize evidence collection without widening model-approval access.

    Qualification is an operational controller action used by authenticated
    fleet automation. It cannot approve, expose, price, route, or publish a
    model, so the Hub/fleet machine-token boundary is appropriate here while
    model exposure remains browser-owner or loopback only.
    """
    if is_loopback(request):
        return
    if auth.valid_browser_session(request.cookies.get(auth.SESSION_COOKIE_NAME)):
        return
    if auth.valid_machine_token(request, HUB_TOKEN):
        return
    raise HTTPException(
        403,
        "Voice qualification requires the owner session or an authenticated controller client.",
    )


def _candidate_by_key(candidate_key: str) -> dict:
    row = next((item for item in monitor.candidate_models()
                if item.get("candidate_key") == candidate_key), None)
    if row is None:
        raise HTTPException(
            404,
            "This exact audited model candidate is not present in the last-good catalogue.",
        )
    if row.get("contract_conflict"):
        raise HTTPException(
            409,
            "Different contracts or revisions are reported for this model and operation.",
        )
    return row


@app.get("/api/hub/model-exposures")
def hub_model_exposures():
    """Cache-only candidate and historical exposure inventory for the owner."""
    candidates = monitor.candidate_models()
    controller_role = control_plane.public_settings().get("role")
    return {
        "schema": model_exposure.SCHEMA_NAME,
        "schema_version": model_exposure.SCHEMA_VERSION,
        "candidates": candidates,
        "candidate_count": len(candidates),
        "approved_count": sum(
            item.get("exposure", {}).get("state") == "approved"
            for item in candidates
        ),
        "controller_role": controller_role,
        "can_expose": (
            controller_role == "controller"
            and not model_exposure.global_authority_active()
        ),
        "managed_by": (
            "genstudio" if model_exposure.global_authority_active() else "studiohub"
        ),
        "history": model_exposure.records(),
    }


@app.post("/api/hub/model-exposures/approve")
def approve_hub_model_exposure(request: Request, body: ModelExposureActionBody):
    _require_exposure_owner(request)
    if model_exposure.global_authority_active():
        raise HTTPException(
            409,
            "This controller is managed by GenStudio's Approved Fleet Model Catalog.",
        )
    if control_plane.public_settings().get("role") != "controller":
        raise HTTPException(409, "Only a location controller can expose models.")
    row = _candidate_by_key(body.candidate_key)
    candidate = {
        "internal_model_id": row["internal_model_id"],
        "display_name": row["display_name"],
        "audit_id": row.get("audit_id"),
        "audit_status": row.get("audit_status"),
        "candidate_for_genstudio": row.get("candidate_for_genstudio"),
        "runtime_revision": row["runtime_revision"],
        "contract_hash": row["contract_hash"],
        "approved_operations": [row["operation"]],
    }
    try:
        exposure = model_exposure.approve(
            candidate, row["operation"], reason=body.reason,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True, "exposure": exposure}


@app.post("/api/hub/model-exposures/revoke")
def revoke_hub_model_exposure(request: Request, body: ModelExposureActionBody):
    _require_exposure_owner(request)
    if model_exposure.global_authority_active():
        raise HTTPException(
            409,
            "This controller is managed by GenStudio's Approved Fleet Model Catalog.",
        )
    if control_plane.public_settings().get("role") != "controller":
        raise HTTPException(409, "Only a location controller can revoke models.")
    try:
        exposure = model_exposure.revoke_key(
            body.candidate_key, reason=body.reason,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True, "exposure": exposure}


@app.post("/api/hub/catalog/refresh")
async def refresh_hub_catalog(request: Request):
    _require_exposure_owner(request)
    return await monitor.refresh_catalogs(force=True)


# ── Wave 1 Voice Studio qualification ────────────────────────────────────
# These are deliberately separate from customer batches and from model exposure.
# They are a controlled evidence collector for 8/16/24 GB workers. The local
# controller path is explicitly opt-in and can be fenced by physical machine ID.
@app.get("/api/hub/admin/voice-qualifications")
def list_voice_qualifications(request: Request, limit: int = Query(100, ge=1, le=500)):
    _require_qualification_operator(request)
    return {"attempts": [voice_qualification._public(item)
                         for item in voice_qualification.list_attempts(limit)]}


@app.get("/api/hub/admin/voice-qualifications/{attempt_id}")
def get_voice_qualification(request: Request, attempt_id: str):
    _require_qualification_operator(request)
    attempt = voice_qualification.get(attempt_id)
    if attempt is None:
        raise HTTPException(404, "Unknown qualification attempt.")
    return voice_qualification._public(attempt)


@app.get("/api/hub/admin/voice-qualifications/{attempt_id}/artifact")
async def get_voice_qualification_artifact(request: Request, attempt_id: str):
    """Download completed listening audio without exposing its worker URL."""
    _require_qualification_operator(request)
    attempt = voice_qualification.get(attempt_id)
    if attempt is None:
        raise HTTPException(404, "Unknown qualification attempt.")
    if attempt.get("state") != "succeeded" or not attempt.get("worker_job_id"):
        raise HTTPException(425, "Qualification audio is not ready.")
    studio = next((item for item in monitor.registry
                   if item.get("id") == (attempt.get("target") or {}).get("studio_id")), None)
    if studio is None:
        raise HTTPException(503, "Qualification worker is no longer registered.")
    try:
        client, response = await _open_worker_artifact(
            studio, f"/api/generate/jobs/{attempt['worker_job_id']}/audio",
        )
    except httpx.HTTPError as exc:
        raise HTTPException(502, "Qualification artifact could not be read.") from exc

    async def close_worker_stream():
        await response.aclose()
        await client.aclose()

    media_type = response.headers.get("content-type", "audio/wav").split(";", 1)[0]
    if not media_type.startswith("audio/"):
        await close_worker_stream()
        raise HTTPException(502, "Qualification worker returned a non-audio artifact.")
    return StreamingResponse(
        response.aiter_bytes(), media_type=media_type,
        background=BackgroundTask(close_worker_stream),
        headers={"Content-Disposition": f'attachment; filename="{attempt_id}.wav"'},
    )


@app.post("/api/hub/admin/voice-qualifications")
async def submit_voice_qualification(request: Request, body: VoiceQualificationBody):
    _require_qualification_operator(request)
    try:
        async with httpx.AsyncClient() as worker_client:
            return await voice_qualification.submit(
                monitor, body.model_dump(), worker_client,
            )
    except voice_qualification.QualificationError as exc:
        raise HTTPException(409, {"code": exc.code, "detail": exc.detail}) from exc


@app.post("/api/hub/admin/voice-qualifications/{attempt_id}/poll")
async def poll_voice_qualification(request: Request, attempt_id: str):
    _require_qualification_operator(request)
    try:
        async with httpx.AsyncClient() as worker_client:
            return await voice_qualification.poll(monitor, attempt_id, worker_client)
    except voice_qualification.QualificationError as exc:
        raise HTTPException(404 if exc.code == "ATTEMPT_NOT_FOUND" else 409,
                            {"code": exc.code, "detail": exc.detail}) from exc


@app.delete("/api/hub/admin/voice-qualifications/{attempt_id}")
async def cancel_voice_qualification(request: Request, attempt_id: str):
    _require_qualification_operator(request)
    try:
        async with httpx.AsyncClient() as worker_client:
            return await voice_qualification.cancel(monitor, attempt_id, worker_client)
    except voice_qualification.QualificationError as exc:
        raise HTTPException(404 if exc.code == "ATTEMPT_NOT_FOUND" else 409,
                            {"code": exc.code, "detail": exc.detail}) from exc


async def _local_model_for_admission(model: str) -> dict:
    row = next(
        (item for item in await monitor.models_by_repo(force=False)
         if item.get("repo") == model),
        None,
    )
    if row is None:
        raise HTTPException(404, "Model is not present in the current fleet catalog.")
    if not memory_admission.applies_to(row.get("modality")):
        raise HTTPException(
            400, "This model's queue does not use the local generation RAM governor.")
    return row


@app.get("/api/hub/memory-admission")
async def get_memory_admission():
    """Effective per-model local RAM floors and their visible source."""
    rows = [row for row in await monitor.models_by_repo(force=False)
            if memory_admission.applies_to(row.get("modality"))]
    return {
        "default_min_free_memory_gb": memory_admission.DEFAULT_MIN_FREE_MEMORY_GB,
        "policies": [memory_admission.describe(row["repo"], row) for row in rows],
    }


@app.put("/api/hub/memory-admission")
async def put_memory_admission(body: MemoryAdmissionBody):
    """Save an owner-selected site-local override without changing workers."""
    row = await _local_model_for_admission(body.model)
    policy = memory_admission.set_override(
        body.model,
        min_total_memory_gb=body.min_total_memory_gb,
        min_free_memory_gb=body.min_free_memory_gb,
        catalog_entry=row,
    )
    broker.wake_dispatcher()
    return {"ok": True, "policy": policy}


@app.delete("/api/hub/memory-admission")
async def delete_memory_admission(model: str = Query(min_length=1, max_length=500)):
    """Remove an override and return to the visible Hub/catalog default."""
    row = await _local_model_for_admission(model)
    policy = memory_admission.reset_override(model, row)
    broker.wake_dispatcher()
    return {"ok": True, "policy": policy}


@app.get("/api/hub/transcription")
async def hub_transcription(force: bool = False):
    """Fleet-wide Whisper availability with per-machine cache status."""
    return await monitor.transcription_inventory(force=force)


# ── Hub-owned shared voice library ────────────────────────────────────────
@app.get("/api/hub/shared-voices")
def hub_shared_voices():
    return {
        "voices": shared_voices.list_voices(monitor),
        "deletions": shared_voices.list_deletions(monitor),
    }


@app.post("/api/hub/shared-voices/transcribe")
async def hub_transcribe_shared_voice(
    audio: UploadFile = File(...),
    model: str = Form(...),
    language: str | None = Form(None),
):
    """Transcribe a reference clip in Hub before the shared voice is saved."""
    payload = await _run_single_transcription(
        audio, model, language, False, label="shared-voice-transcription"
    )
    transcript = str(payload.get("text") or "").strip()
    if not transcript:
        transcript = shared_voices.srt_to_text(str(payload.get("srt") or ""))
    if not transcript:
        raise HTTPException(502, "transcription completed without readable text")
    return {
        "transcript": transcript,
        "model": model,
        "language": payload.get("language") or language,
        "studio": payload.get("studio"),
        "elapsed_seconds": payload.get("elapsed_seconds"),
    }


@app.post("/api/hub/shared-voices")
async def hub_create_shared_voice(
    audio: UploadFile = File(...),
    name: str = Form(...),
    language: str = Form(...),
    gender: str = Form(...),
    license: str = Form(...),
    notes: str = Form(""),
    source_url: str = Form(""),
    transcript: str = Form(""),
    permission_acknowledged: bool = Form(False),
):
    try:
        data = await audio.read(shared_voices.MAX_BYTES + 1)
        voice = shared_voices.create(
            audio_bytes=data, filename=audio.filename or "reference.wav",
            name=name, language=language, gender=gender, license=license,
            notes=notes, source_url=source_url or None, transcript=transcript or None,
            permission_acknowledged=permission_acknowledged,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    shared_voices.start_sync(monitor, voice["id"])
    return {"voice": shared_voices.serialize(voice, monitor), "sync_started": True}


@app.get("/api/hub/shared-voices/{voice_id}/audio")
def hub_shared_voice_audio(voice_id: str):
    try:
        path = shared_voices.audio_path(voice_id)
    except ValueError:
        path = None
    if not path:
        raise HTTPException(404, "shared voice audio not found")
    mime = {
        ".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
        ".aac": "audio/aac", ".flac": "audio/flac", ".ogg": "audio/ogg",
        ".opus": "audio/ogg",
    }.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=mime, filename=path.name)


@app.patch("/api/hub/shared-voices/{voice_id}")
async def hub_update_shared_voice(voice_id: str, body: SharedVoiceUpdateBody):
    changes = body.model_dump(exclude_unset=True)
    try:
        voice = shared_voices.update(voice_id, changes)
    except KeyError:
        raise HTTPException(404, "shared voice not found")
    except (ValueError, shared_voices.SharedVoiceConflict) as exc:
        raise HTTPException(400, str(exc))
    started = shared_voices.start_sync(monitor, voice_id)
    return {
        "voice": shared_voices.serialize(voice, monitor),
        "sync_started": started,
        "sync_queued": not started,
    }


@app.delete("/api/hub/shared-voices/{voice_id}")
async def hub_delete_shared_voice(voice_id: str):
    try:
        tombstone = shared_voices.prepare_delete(voice_id)
        started = shared_voices.start_delete(monitor, voice_id)
    except KeyError:
        raise HTTPException(404, "shared voice not found")
    except (ValueError, shared_voices.SharedVoiceConflict) as exc:
        raise HTTPException(409, str(exc))
    return {
        "deletion": shared_voices.serialize_deletion(tombstone, monitor),
        "sync_started": started,
        "already_running": not started,
    }


@app.post("/api/hub/shared-voices/{voice_id}/delete-sync")
async def hub_retry_shared_voice_delete(voice_id: str):
    try:
        started = shared_voices.start_delete(monitor, voice_id)
        deletion = shared_voices.get_deletion(voice_id, monitor)
    except (KeyError, ValueError):
        raise HTTPException(404, "shared voice deletion not found")
    return {
        "deletion": deletion,
        "sync_started": started,
        "already_running": not started,
    }


@app.post("/api/hub/shared-voices/{voice_id}/sync")
async def hub_retry_shared_voice_sync(voice_id: str):
    try:
        exists = any(v["id"] == voice_id for v in shared_voices.list_voices(monitor))
    except ValueError:
        exists = False
    if not exists:
        raise HTTPException(404, "shared voice not found")
    started = shared_voices.start_sync(monitor, voice_id)
    return {"voice_id": voice_id, "sync_started": started, "already_running": not started}


# Kept as a public compatibility alias for diagnostics and older tests.
_transcription_busy = transcription_jobs.busy_studios


@app.post("/api/hub/transcription/jobs")
async def hub_create_transcription_job(
    files: list[UploadFile] = File(...),
    item_ids: list[str] = Form(...),
    model: str = Form(...),
    language: str | None = Form(None),
    word_timestamps: bool = Form(False),
    label: str | None = Form(None),
    project: str | None = Form(None),
    episode: str | None = Form(None),
    genstudio_execution: str | None = Form(None),
):
    """Spool an episode upload and immediately enqueue its chapters."""
    if not control_plane.accepts_customer_jobs():
        raise HTTPException(409, "This Hub is in agent mode; submit transcription to a controller.")
    batch, duplicate = await transcription_jobs.create_batch(
        files, item_ids, model, language, word_timestamps, label, project,
        episode, genstudio_execution)
    transcription_jobs.start_dispatcher(monitor)
    result = {"batch_id": batch["id"], "items": len(batch["items"]),
              "queued": sum(i["state"] == "queued" for i in batch["items"])}
    if duplicate:
        result["duplicate"] = True
    return result


@app.post("/api/hub/executions/leases")
def hub_renew_execution_lease(body: dict):
    """Renew one GenStudio-owned batch without reviving an expired fence."""
    try:
        renewal = execution_identity.renew_lease(body)
    except execution_identity.ExecutionIdentityError as exc:
        raise HTTPException(409, str(exc)) from exc
    updated = any(
        updater(renewal)
        for updater in (
            broker.renew_execution_lease,
            transcription_jobs.renew_execution_lease,
            chat_jobs.renew_execution_lease,
        )
    )
    if not updated:
        raise HTTPException(404, "GenStudio execution batch is not available")
    return {
        "genstudio_job_id": renewal["genstudio_job_id"],
        "genstudio_attempt_id": renewal["genstudio_attempt_id"],
        "fencing_token": renewal["fencing_token"],
        "lease_expires_at": renewal["lease_expires_at"],
    }


@app.get("/api/hub/transcription/jobs")
def hub_list_transcription_jobs():
    return {"batches": transcription_jobs.list_batches(),
            "stats": transcription_jobs.statistics()}


@app.get("/api/hub/transcription/settings")
def hub_transcription_settings():
    return transcription_jobs.settings()


@app.post("/api/hub/transcription/settings")
def hub_set_transcription_settings(body: dict):
    return transcription_jobs.set_retention(body.get("retention_days"))


@app.post("/api/hub/transcription/cleanup")
def hub_cleanup_transcription(body: dict | None = None):
    body = body or {}
    return transcription_jobs.cleanup(
        batch_id=body.get("batch_id"), expired_only=not bool(body.get("all_terminal")))


@app.post("/api/hub/transcription/jobs/clear")
def hub_clear_transcription_jobs():
    """Permanently remove all completed transcription batches and their files."""
    return {"ok": True, **transcription_jobs.clear_terminal()}


@app.post("/api/hub/transcription/jobs/{batch_id}/clear")
def hub_clear_transcription_job(batch_id: str):
    """Permanently remove one completed transcription batch and its files."""
    result = transcription_jobs.remove_batch(batch_id)
    if not result:
        raise HTTPException(409, "batch is still active or unknown — cancel it first")
    return {"ok": True, **result}


@app.get("/api/hub/job-storage")
def hub_job_storage_status():
    return job_storage.status()


@app.post("/api/hub/job-storage")
def hub_save_job_storage(body: dict):
    return job_storage.save(body.get("enabled"), body.get("max_gb"))


@app.post("/api/hub/job-storage/cleanup")
def hub_enforce_job_storage():
    return job_storage.enforce_budget()


# ── fleet local-backup storage protection ────────────────────────────────
@app.get("/api/hub/storage-policy")
async def hub_fleet_storage_status(local_only: bool = Query(False)):
    return (await fleet_storage.local_status(monitor) if local_only
            else await fleet_storage.fleet_status(monitor))


@app.put("/api/hub/storage-policy")
async def hub_save_fleet_storage(body: FleetStoragePolicyBody,
                                 local_only: bool = Query(False)):
    return await fleet_storage.save_fleet(
        monitor, body.enabled, body.retention_days, body.max_gb,
        local_only=local_only)


@app.post("/api/hub/storage-policy/cleanup")
async def hub_cleanup_fleet_storage(local_only: bool = Query(False)):
    return await fleet_storage.cleanup_fleet(monitor, local_only=local_only)


@app.get("/api/hub/transcription/jobs/{batch_id}")
def hub_get_transcription_job(batch_id: str):
    batch = transcription_jobs.get_batch(batch_id)
    if not batch:
        raise HTTPException(404, "unknown transcription batch")
    return transcription_jobs.summary(batch, include_metadata=True)


@app.get("/api/hub/transcription/jobs/{batch_id}/items/{item_index}/artifact")
def hub_get_transcription_artifact(batch_id: str, item_index: int):
    batch = transcription_jobs.get_batch(batch_id)
    if not batch:
        raise HTTPException(404, "unknown transcription batch")
    item = next((i for i in batch["items"] if i["index"] == item_index), None)
    path = Path((item or {}).get("artifact_path") or "")
    root = transcription_jobs.ROOT.resolve()
    try:
        safe = path.resolve().is_relative_to(root)
    except OSError:
        safe = False
    if (not item or item["state"] != "done" or not safe or not path.is_file()
            or path.stat().st_size == 0):
        raise HTTPException(404, "SRT artifact is not available")
    return FileResponse(path, media_type="application/x-subrip",
                        filename=f"{item['item_id']}.srt")


@app.delete("/api/hub/transcription/jobs/{batch_id}")
async def hub_cancel_transcription_job(batch_id: str):
    batch = await transcription_jobs.cancel_batch(batch_id)
    if not batch:
        raise HTTPException(404, "unknown transcription batch")
    return transcription_jobs.summary(batch)


@app.post("/api/hub/transcription/jobs/{batch_id}/retry")
def hub_retry_transcription_job(batch_id: str):
    batch, retried = transcription_jobs.retry_batch(batch_id)
    if not batch:
        raise HTTPException(404, "unknown transcription batch")
    transcription_jobs.start_dispatcher(monitor)
    return {"batch_id": batch_id, "retried": retried,
            "status": transcription_jobs.summary(batch)["status"]}


# ── saved Chat Studio packs ───────────────────────────────────────────────
@app.post("/api/hub/chat/jobs")
async def hub_create_chat_job(body: dict):
    if not control_plane.accepts_customer_jobs():
        raise HTTPException(409, "This Hub is in agent mode; submit Chat work to a controller.")
    batch, duplicate = chat_jobs.create_batch(body)
    chat_jobs.start_dispatcher(monitor)
    result = {"batch_id": batch["id"], "packs": len(batch["packs"]),
              "scenes": sum(len(pack["scene_ids"]) for pack in batch["packs"])}
    if duplicate:
        result["duplicate"] = True
    return result


@app.get("/api/hub/chat/jobs")
def hub_list_chat_jobs():
    return {"batches": chat_jobs.list_batches(), "stats": chat_jobs.statistics()}


@app.get("/api/hub/chat/jobs/{batch_id}")
def hub_get_chat_job(batch_id: str, include_raw: bool = False):
    batch = chat_jobs.get_batch(batch_id)
    if not batch:
        raise HTTPException(404, "unknown Chat batch")
    return chat_jobs.summary(batch, include_raw=include_raw)


@app.delete("/api/hub/chat/jobs/{batch_id}")
async def hub_cancel_chat_job(batch_id: str):
    batch = await chat_jobs.cancel_batch(batch_id)
    if not batch:
        raise HTTPException(404, "unknown Chat batch")
    return chat_jobs.summary(batch)


@app.post("/api/hub/chat/jobs/{batch_id}/retry")
async def hub_retry_chat_job(batch_id: str):
    batch, retried = chat_jobs.retry_batch(batch_id)
    if not batch:
        raise HTTPException(404, "unknown Chat batch")
    chat_jobs.start_dispatcher(monitor)
    return {"batch_id": batch_id, "retried": retried,
            "status": chat_jobs.summary(batch)["status"]}


@app.post("/api/hub/chat/jobs/clear")
def hub_clear_chat_jobs():
    """Remove all finished Chat prompt batches (done/partial/error/cancelled).
    Running/queued batches are kept."""
    return {"ok": True, "cleared": chat_jobs.clear_terminal()}


@app.post("/api/hub/chat/jobs/{batch_id}/clear")
def hub_clear_chat_job(batch_id: str):
    """Remove ONE finished Chat prompt batch. 409 if it's still running."""
    if not chat_jobs.remove_batch(batch_id):
        raise HTTPException(409, "batch is still active or unknown — cancel it first")
    return {"ok": True, "removed": batch_id}


async def _run_single_transcription(
    file: UploadFile,
    model: str,
    language: str | None,
    word_timestamps: bool,
    *,
    label: str = "single-file-api",
) -> dict:
    """Run one file through the durable fleet queue and return its payload."""
    item_id = _single_transcription_item_id(file.filename)
    batch, _ = await transcription_jobs.create_batch(
        [file], [item_id], model, language, word_timestamps,
        label, None, None, deduplicate=False)
    transcription_jobs.start_dispatcher(monitor)
    deadline = time.monotonic() + 305.0
    item = batch["items"][0]
    while time.monotonic() < deadline and item["state"] in {"queued", "running"}:
        await asyncio.sleep(0.1)
    if item["state"] != "done":
        if item["state"] in {"queued", "running"}:
            await transcription_jobs.cancel_batch(batch["id"])
            raise HTTPException(503, f"No free Voice Studio has '{model}' ready")
        raise HTTPException(502, item.get("error") or "Voice Studio transcription failed")
    artifact = Path(item["artifact_path"])
    return {
        **(item.get("metadata") or {}),
        "studio": item.get("studio"),
        "srt": artifact.read_text(encoding="utf-8"),
    }


def _single_transcription_item_id(filename: str | None) -> str:
    """Create a queue-safe ID from a user-facing upload filename.

    Queue item IDs intentionally have a narrow character set, while ordinary
    audio filenames commonly contain punctuation (for example commas). The
    original filename remains intact as display metadata; only its internal
    queue identifier is normalized.
    """
    stem = Path(filename or "audio").stem
    normalized = re.sub(r"[^A-Za-z0-9._ -]+", " ", stem)
    normalized = re.sub(r"\s+", " ", normalized).strip(" .-")
    return normalized[:120] or "audio"


@app.post("/api/hub/transcribe")
async def hub_transcribe(
    file: UploadFile = File(...),
    model: str = Form(...),
    language: str | None = Form(None),
    word_timestamps: bool = Form(False),
):
    """Backward-compatible one-file request, implemented through the queue."""
    if not control_plane.accepts_customer_jobs():
        raise HTTPException(409, "This Hub is in agent mode; submit transcription to a controller.")
    return await _run_single_transcription(file, model, language, word_timestamps)


@app.post("/api/hub/assets/scan")
def hub_assets_scan():
    return ledger.scan_outputs(monitor.registry)


# Large render inputs use a raw streaming lane rather than multipart so Story
# Studio never has to hold an episode's audio/video bytes in memory.
_RENDER_UPLOADS = DATA_DIR / "render_uploads"
_RENDER_UPLOADS.mkdir(exist_ok=True)
_RENDER_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".webp", ".mp4", ".mov", ".m4v",
    ".mp3", ".wav", ".m4a", ".aac", ".srt", ".ass", ".txt",
}
_MAX_RENDER_UPLOAD_BYTES = 20 * 1024 * 1024 * 1024
_RENDER_ASSET_RETENTION_DAYS = 7
_RENDER_ASSET_CLEANUP_INTERVAL_SECONDS = 60 * 60
_last_render_asset_cleanup = 0.0


def _is_render_sha256(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{64}", value.lower()))


def _render_asset_path(asset_id: str) -> Path | None:
    if not asset_id.isalnum():
        return None
    return next((p for p in _RENDER_UPLOADS.glob(f"{asset_id}.*")
                 if p.is_file() and not p.name.endswith(".partial")), None)


def _render_asset_payload(path: Path, sha256: str | None = None) -> dict:
    digest = (sha256 or path.stem).lower()
    return {
        "asset_id": digest if _is_render_sha256(digest) else path.stem,
        "bytes": path.stat().st_size,
        "sha256": digest,
        "path": f"/api/hub/render-assets/{digest if _is_render_sha256(digest) else path.stem}",
    }


def _cleanup_expired_render_assets() -> int:
    """Remove only immutable, content-addressed inputs after their lease ages out."""
    cutoff = time.time() - (_RENDER_ASSET_RETENTION_DAYS * 24 * 60 * 60)
    removed = 0
    for candidate in _RENDER_UPLOADS.iterdir():
        if not candidate.is_file() or candidate.name.endswith(".partial"):
            continue
        if not _is_render_sha256(candidate.stem) or candidate.stat().st_mtime > cutoff:
            continue
        candidate.unlink(missing_ok=True)
        removed += 1
    return removed


def _maybe_cleanup_expired_render_assets() -> None:
    global _last_render_asset_cleanup
    now = time.time()
    if now - _last_render_asset_cleanup < _RENDER_ASSET_CLEANUP_INTERVAL_SECONDS:
        return
    _last_render_asset_cleanup = now
    _cleanup_expired_render_assets()


@app.post("/api/hub/render-assets")
async def hub_render_asset_upload(request: Request):
    """Stream one immutable render input to the Hub and return its digest.

    Assets are named by SHA-256, so a Story Studio retry (or a second episode
    sharing the same media) can retain and reuse the first transfer safely.
    """
    _maybe_cleanup_expired_render_assets()
    original = request.headers.get("x-file-name", "asset.bin")
    ext = Path(original).suffix.lower()
    if ext not in _RENDER_EXTENSIONS:
        raise HTTPException(415, f"unsupported render asset type: {ext or '(none)'}")
    declared = request.headers.get("content-length")
    if declared and int(declared) > _MAX_RENDER_UPLOAD_BYTES:
        raise HTTPException(413, "render asset exceeds 20 GB")
    declared_digest = request.headers.get("x-content-sha256", "").lower().strip()
    if declared_digest and not _is_render_sha256(declared_digest):
        raise HTTPException(400, "invalid X-Content-SHA256 header")
    if declared_digest:
        retained = _render_asset_path(declared_digest)
        if retained and retained.suffix == ext:
            retained.touch(exist_ok=True)
            return _render_asset_payload(retained, declared_digest)
    asset_id = uuid.uuid4().hex[:16]
    partial = _RENDER_UPLOADS / f".{asset_id}{ext}.partial"
    digest = hashlib.sha256()
    total = 0
    try:
        with partial.open("xb") as handle:
            async for chunk in request.stream():
                total += len(chunk)
                if total > _MAX_RENDER_UPLOAD_BYTES:
                    raise HTTPException(413, "render asset exceeds 20 GB")
                digest.update(chunk)
                handle.write(chunk)
        if not total:
            raise HTTPException(400, "empty render asset")
        sha256 = digest.hexdigest()
        if declared_digest and sha256 != declared_digest:
            raise HTTPException(400, "render asset checksum does not match X-Content-SHA256")
        final = _RENDER_UPLOADS / f"{sha256}{ext}"
        # A concurrent retry may have completed while this stream was running.
        # Keep the already-verified immutable file and discard our duplicate.
        if final.exists():
            partial.unlink(missing_ok=True)
        else:
            partial.replace(final)
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    return _render_asset_payload(final, sha256)


@app.get("/api/hub/render-assets/by-sha/{sha256}")
def hub_render_asset_by_sha(sha256: str, extension: str = Query(...)):
    """Return a retained asset by content identity and refresh its seven-day lease."""
    normalized = sha256.lower()
    if not _is_render_sha256(normalized):
        raise HTTPException(400, "invalid SHA-256")
    if extension.lower() not in _RENDER_EXTENSIONS:
        raise HTTPException(415, "unsupported render asset extension")
    retained = _render_asset_path(normalized)
    if not retained or retained.suffix.lower() != extension.lower():
        raise HTTPException(404, "render asset not retained")
    retained.touch(exist_ok=True)
    return _render_asset_payload(retained, normalized)


@app.get("/api/hub/render-assets/{asset_id}")
def hub_render_asset_download(asset_id: str):
    path = _render_asset_path(asset_id)
    if not path:
        raise HTTPException(404, "render asset not found")
    path.touch(exist_ok=True)
    return FileResponse(path, filename=path.name)


@app.delete("/api/hub/render-assets/{asset_id}")
def hub_render_asset_delete(asset_id: str):
    path = _render_asset_path(asset_id)
    if not path:
        raise HTTPException(404, "render asset not found")
    if _is_render_sha256(asset_id):
        raise HTTPException(409, "content-addressed render assets are retained for seven days")
    path.unlink()
    return {"ok": True}


# The upload-once endpoint receives multipart, which needs python-multipart.
# Guard it so a Hub that pulled the code but hasn't re-run Install/Update still
# BOOTS — b64/url reference images keep working; only upload-once degrades.
try:
    import python_multipart as _multipart_pkg  # noqa: F401  (current package name)
    _HAS_MULTIPART = True
except ImportError:
    try:
        import multipart as _multipart_pkg  # noqa: F401  (older name)
        _HAS_MULTIPART = True
    except ImportError:
        _HAS_MULTIPART = False

if _HAS_MULTIPART:
    _UPLOAD_CHUNK_BYTES = 1024 * 1024
    _MAX_IMAGE_UPLOAD_BYTES = 20 * 1024 * 1024
    _IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}

    @app.post("/api/hub/assets/upload")
    async def hub_asset_upload(file: UploadFile = File(...)):
        """Upload a reference image ONCE, get an asset_id, then reference it from
        many jobs (`reference_images:[{asset_id}]`) — avoids re-sending megabytes
        per scene for continuity. The Hub reads the file locally and forwards its
        bytes to whichever machine runs each job."""
        import uuid
        from pathlib import Path
        uploads = DATA_DIR / "uploads"
        uploads.mkdir(exist_ok=True)
        ext = (Path(file.filename or "").suffix or "").lower()
        if ext not in _IMAGE_EXTENSIONS:
            raise HTTPException(415, "reference image must be PNG, JPEG, or WebP")
        asset_id = uuid.uuid4().hex[:12]
        path = uploads / (asset_id + ext)
        total = 0
        try:
            with path.open("xb") as out:
                while chunk := await file.read(_UPLOAD_CHUNK_BYTES):
                    total += len(chunk)
                    if total > _MAX_IMAGE_UPLOAD_BYTES:
                        raise HTTPException(413, "reference image exceeds the 20 MB limit")
                    out.write(chunk)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        if not total:
            path.unlink(missing_ok=True)
            raise HTTPException(400, "empty file")
        ledger.record_asset(id=asset_id, source="upload", modality="image",
                            machine="local", artifact_path=str(path.resolve()))
        return {"asset_id": asset_id, "bytes": total}
else:
    @app.post("/api/hub/assets/upload")
    def hub_asset_upload_unavailable():
        raise HTTPException(501, "upload needs python-multipart — run Update/Install "
                            "on this Hub. b64/url reference images work without it.")


@app.get("/api/hub/alerts")
def get_alerts(limit: int = Query(100, ge=1, le=200)):
    """Recent alert events + current alert config (studio-down / batch-failed)."""
    return {"config": alerts.load_config(), "recent": alerts.recent(limit)}


@app.post("/api/hub/alerts")
def set_alerts(body: dict):
    """Configure alerting: {"webhook": <url|"">, "desktop": <bool>}."""
    cfg = {}
    if body.get("webhook"):
        cfg["webhook"] = str(body["webhook"])
    if body.get("desktop"):
        cfg["desktop"] = True
    alerts.set_config(cfg)
    return {"ok": True, "config": cfg}


@app.post("/api/hub/alerts/clear")
def clear_alerts():
    """Wipe the alert log (also resets the header bell count)."""
    return {"ok": True, "cleared": alerts.clear()}


@app.get("/api/hub/stats")
def hub_stats(
    hours: int | None = Query(None, ge=1, description="limit to last N hours"),
    source: str = Query("all", pattern="^(all|job|direct)$",
                        description="all | job (Hub-dispatched) | direct (in-studio)"),
    modality: str | None = Query(None, description="filter to one operation type"),
    machine: str | None = Query(None, description="filter to one machine"),
):
    """Generation analytics: per-machine / operation-type / model counts +
    speed, plus a time-bucketed throughput series (bucket sized to the window).
    Counts span every source by default; `source`, `modality`, and `machine`
    narrow the view and throughput chart to match."""
    since = time.time() - hours * 3600 if hours else None
    bucket = 300 if hours == 1 else (3600 if hours == 24 else 86400)
    result = ledger.stats(since_s=since, source=source, op=modality, machine=machine)
    result["timeline"] = ledger.timeline(since, bucket, source=source, op=modality,
                                          machine=machine)
    activity_batches = dict(broker.batches)
    activity_batches.update({
        f"transcription:{batch_id}": {
            "model": batch.get("model"), "operation": "transcription",
            "items": batch.get("items") or [],
        }
        for batch_id, batch in transcription_jobs.batches.items()
    })
    result["fleet_activity"] = activity.fleet_snapshot(
        monitor.registry, monitor.status, activity_batches, since_s=since,
    )
    result["filters"] = {"source": source, "modality": modality,
                         "machine": machine, "hours": hours}
    return result


# ── recipes + director ─────────────────────────────────────────────────────
@app.post("/api/hub/recipes/run")
async def hub_run_recipe(body: dict):
    if not control_plane.accepts_customer_jobs():
        raise HTTPException(409, "This Hub is in agent mode; run recipes on a controller.")
    recipe = body.get("recipe")
    if not recipe:
        raise HTTPException(400, "recipe is required")
    try:
        run_id = await recipes.run_recipe(recipe, body.get("brief", ""))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"run_id": run_id}


@app.get("/api/hub/recipes/runs")
def hub_recipe_runs():
    return {"runs": sorted(recipes.runs.values(),
                           key=lambda r: -r["created_at"])}


@app.get("/api/hub/recipes/runs/{run_id}")
def hub_recipe_run(run_id: str):
    run = recipes.runs.get(run_id)
    if run is None:
        raise HTTPException(404, "unknown run")
    return run


@app.post("/api/hub/director")
async def hub_director(body: dict):
    if not control_plane.accepts_customer_jobs():
        raise HTTPException(409, "This Hub is in agent mode; run the director on a controller.")
    brief = body.get("brief")
    if not brief:
        raise HTTPException(400, "brief is required")
    result = await recipes.direct(brief, body.get("chat_model"))
    if "error" in result:
        return result  # director failures are data, not HTTP errors
    if body.get("auto_run"):
        result["run_id"] = await recipes.run_recipe(result["recipe"], brief)
    return result


async def _delayed_start(studio: dict, delay: float = 4.0):
    """Second half of a restart: start after the stop has had time to settle."""
    await asyncio.sleep(delay)
    control_studio(studio, "start")
    try:
        await monitor.poll_all()
    except Exception:  # best-effort refresh; the poll loop catches up regardless
        pass


@app.post("/api/hub/studios/{studio_id}/{action}")
async def studio_lifecycle(studio_id: str, action: str):
    """Start / stop / restart a studio. Local studios go through Pinokio's pterm
    CLI; remote studios are proxied to their own machine's Hub. Returns
    immediately; the health poller reflects the change within seconds."""
    if action not in ("start", "stop", "restart"):
        raise HTTPException(400, "action must be 'start', 'stop', or 'restart'")
    studio = next((s for s in monitor.registry if s["id"] == studio_id), None)
    if studio is None:
        raise HTTPException(404, f"unknown studio: {studio_id}")
    if studio.get("machine", "local") == "local":
        if action == "restart":
            # stop now, then start on a short delay so the port frees first
            stop = control_studio(studio, "stop")
            if not stop["ok"]:
                raise HTTPException(409, stop["error"])
            asyncio.create_task(_delayed_start(studio))
            result = {"ok": True, "action": "restart", "studio": studio_id}
        else:
            result = control_studio(studio, action)          # local: pterm
    else:
        result = await peers.control_remote(monitor._client, studio, action)  # remote: peer Hub
    if not result["ok"]:
        raise HTTPException(409, result["error"])
    await monitor.poll_all()  # reflect the transition quickly
    return result


@app.get("/api/hub/startup-services")
async def fleet_startup_services(local_only: bool = Query(False)):
    """Audit sibling startup services locally or across authenticated peer Hubs."""
    local = startup_services.local_snapshot()
    if local_only:
        return local
    remote = await peers.startup_services_status(monitor.registry, monitor._client)
    return {
        "schema_version": 1,
        "observed_at": time.time(),
        "machines": {"local": local, **remote},
    }


@app.post("/api/hub/startup-services/{machine}/{modality}/install")
async def install_fleet_startup_service(machine: str, modality: str):
    """Install one sibling's startup service on its own machine only."""
    if modality not in startup_services.SERVICE_SPECS:
        raise HTTPException(404, f"unknown Studio type: {modality}")
    if machine == "local":
        broker.set_maintenance(modality, True)
        try:
            if fleet_ops.studio_has_active_work(modality):
                raise HTTPException(
                    409, "This Studio has active Hub work; wait for it to finish before installing startup.")
            try:
                return await asyncio.to_thread(startup_services.install_service, modality)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
        finally:
            broker.set_maintenance(modality, False)
    target = next((row for row in monitor.registry
                   if row.get("machine") == machine
                   and row.get("modality") == modality), None)
    peer = target or next((row for row in monitor.registry
                           if row.get("machine") == machine), None)
    if peer is None:
        raise HTTPException(404, f"unknown machine: {machine}")
    maintenance_id = target["id"] if target else None
    if maintenance_id:
        broker.set_maintenance(maintenance_id, True)
    try:
        if maintenance_id and fleet_ops.studio_has_active_work(maintenance_id):
            raise HTTPException(
                409, "This Studio has active Hub work; wait for it to finish before installing startup.")
        result = await peers.install_remote_startup_service(
            monitor._client, peer, modality)
        if not result.get("ok"):
            raise HTTPException(409, result.get("error", "remote startup installation failed"))
        return result
    finally:
        if maintenance_id:
            broker.set_maintenance(maintenance_id, False)


_RETIRED_DATA_PRESERVED = ["launcher", "models", "caches", "outputs", "settings"]


def _startup_retirement_update_active(studio_id: str) -> bool:
    return any(
        job.get("status") in {"queued", "running"}
        and any(
            item.get("target") == studio_id
            and item.get("status") not in TERMINAL_ITEM_STATES
            for item in job.get("items", [])
        )
        for job in fleet_auto_updates.jobs()
    )


async def _retire_local_startup_service(modality: str) -> dict:
    """Retire one local legacy sibling while keeping its app data intact."""
    if modality not in startup_services.RETIRABLE_MODALITIES:
        raise HTTPException(400, "Only Music, Chat, Video, and Render may be retired.")
    target = next((row for row in monitor.registry
                   if row.get("machine", "local") == "local"
                   and row.get("modality") == modality), None)
    if target is None:
        raise HTTPException(404, f"{modality.title()} Studio is not registered on this Mac")
    studio_id = target["id"]
    broker.set_maintenance(studio_id, True)
    try:
        if fleet_ops.studio_has_active_work(studio_id):
            raise HTTPException(
                409, "This Studio has active Hub work; wait for it to finish before retiring it.")
        try:
            with startup_services.retirement_lock(modality):
                if _startup_retirement_update_active(studio_id):
                    raise HTTPException(
                        409, "This Studio has an active update; wait for it to finish before retiring it.")
                updater = await fleet_auto_updates.retirement_status(studio_id)
                if updater.get("managed_update") or str(updater.get("state") or "idle").lower() in {
                        "checking", "downloading", "updating", "restarting"}:
                    raise HTTPException(
                        409, "This Studio has an active update; wait for it to finish before retiring it.")
                await fleet_auto_updates.set_mode(studio_id, "off")
                from . import registry
                registry.set_studio_enabled("local", studio_id, False)
                result = await asyncio.to_thread(startup_services.uninstall_service, modality)
        except HTTPException:
            raise
        except (ValueError, OSError, httpx.HTTPError) as exc:
            raise HTTPException(409, str(exc)) from exc
        return {**result, "retired": True, "modality": modality,
                "routing_enabled": False, "updater_mode": "off",
                "preserved": _RETIRED_DATA_PRESERVED}
    finally:
        broker.set_maintenance(studio_id, False)


@app.post("/api/hub/service/startup-services/local/{modality}/retire")
async def retire_local_startup_service_for_peer(request: Request, modality: str):
    """Strict fleet-service route; the peer performs its authoritative checks."""
    await _require_parent_controller_service(request)
    return await _retire_local_startup_service(modality)


@app.post("/api/hub/startup-services/{machine}/{modality}/retire")
async def retire_fleet_startup_service(request: Request, machine: str, modality: str):
    """Retire one legacy sibling without deleting its launcher or app data."""
    _require_exposure_owner(request)
    if modality not in startup_services.RETIRABLE_MODALITIES:
        raise HTTPException(400, "Only Music, Chat, Video, and Render may be retired.")
    if machine == "local":
        return await _retire_local_startup_service(modality)
    target = next((row for row in monitor.registry
                   if row.get("machine") == machine
                   and row.get("modality") == modality), None)
    if target is None:
        raise HTTPException(404, f"unknown {modality} Studio on machine: {machine}")
    studio_id = target["id"]
    broker.set_maintenance(studio_id, True)
    try:
        if fleet_ops.studio_has_active_work(studio_id):
            raise HTTPException(
                409, "This Studio has active Hub work; wait for it to finish before retiring it.")
        from . import registry
        registry.set_studio_enabled(machine, studio_id, False)
        result = await peers.retire_remote_startup_service(
            monitor._client, target, modality)
        if not result.get("ok"):
            raise HTTPException(409, {
                "code": "routing_disabled_pending_retry",
                "message": result.get("error", "remote startup retirement failed"),
                "routing_disabled": True,
            })
        return {**result, "retired": True, "modality": modality,
                "routing_enabled": False, "preserved": _RETIRED_DATA_PRESERVED}
    finally:
        broker.set_maintenance(studio_id, False)


async def _remove_local_studio(modality: str) -> dict:
    if modality not in startup_services.RETIRABLE_MODALITIES:
        raise HTTPException(400, "Only Music, Chat, Video, and Render may be fully removed.")
    if startup_services.is_fully_removed(modality):
        return {
            "ok": True, "changed": False, "removed": True,
            "already_removed": True, "modality": modality,
            "routing_enabled": False,
            "detail": "Studio was already fully removed",
        }
    if startup_services.has_removal_intent(modality):
        try:
            result = await asyncio.to_thread(
                startup_services.finalize_absent_studio_removal, modality,
            )
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            raise HTTPException(409, str(exc)) from exc
        from . import registry
        registry.set_studio_removal_complete("local", modality, True)
        return {**result, "routing_enabled": False}
    if not startup_services.inspect_service(modality).get("app_installed"):
        # A prior manual deletion or lost successful response can leave the
        # controller registration behind after the checkout is already gone.
        # The explicit, controller-authenticated removal request is durable
        # intent; clear named services and verify the port before acknowledging
        # absence so the controller can safely prune its retained row.
        try:
            with startup_services.retirement_lock(modality):
                from . import registry
                registry.set_studio_removed("local", modality, True)
                registry.set_studio_removal_complete("local", modality, False)
                registry.set_studio_enabled("local", modality, False)
                result = await asyncio.to_thread(
                    startup_services.finalize_absent_studio_removal, modality,
                )
                registry.set_studio_removal_complete("local", modality, True)
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            raise HTTPException(409, str(exc)) from exc
        monitor.reload_registry()
        return {**result, "routing_enabled": False}
    # The checkout is installed. Legacy families are no longer tracked, so it
    # usually has no registry row at all — removal is about the folder on disk,
    # not the registration, so an absent row must not refuse the request and
    # leave the Studio installed. Fall back to the modality, which is also the
    # key `startup_services` already uses for these families' removal flags.
    target = next((row for row in monitor.registry
                   if row.get("machine", "local") == "local"
                   and row.get("modality") == modality), None)
    studio_id = target["id"] if target is not None else modality
    broker.set_maintenance(studio_id, True)
    try:
        if fleet_ops.studio_has_active_work(studio_id):
            raise HTTPException(
                409, "This Studio has active Hub work; wait for it to finish before removing it.")
        try:
            with startup_services.retirement_lock(modality):
                if _startup_retirement_update_active(studio_id):
                    raise HTTPException(
                        409, "This Studio has an active update; wait for it to finish before removing it.")
                from . import registry
                registry.set_studio_removed("local", studio_id, True)
                registry.set_studio_removal_complete("local", studio_id, False)
                registry.set_studio_enabled("local", studio_id, False)
                result = await asyncio.to_thread(startup_services.fully_remove_studio, modality)
                registry.set_studio_removal_complete("local", studio_id, True)
        except HTTPException:
            raise
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            raise HTTPException(409, str(exc)) from exc
        monitor.reload_registry()
        return {**result, "routing_enabled": False}
    finally:
        broker.set_maintenance(studio_id, False)


async def _require_parent_controller_service(request: Request) -> None:
    settings = control_plane.load_settings()
    parent = settings.get("parent_controller_url")
    if settings.get("role") != "agent" or not parent:
        raise HTTPException(403, {"code": "controller_source_required"})
    try:
        origin = await asyncio.to_thread(resolve_private_origin, parent)
    except (OSError, ValueError) as exc:
        raise HTTPException(403, {"code": "controller_source_invalid"}) from exc
    _require_repair_service(request, expected_source=origin.address)


@app.post("/api/hub/service/startup-services/local/{modality}/remove")
async def remove_local_studio_for_peer(request: Request, modality: str):
    """Controller-bound fleet route; the Agent removes only its local checkout."""
    await _require_parent_controller_service(request)
    return await _remove_local_studio(modality)


@app.post("/api/hub/startup-services/{machine}/{modality}/remove")
async def remove_fleet_studio(request: Request, machine: str, modality: str):
    """Fully remove one unused Studio after an explicit owner confirmation."""
    _require_exposure_owner(request)
    if modality not in startup_services.RETIRABLE_MODALITIES:
        raise HTTPException(400, "Only Music, Chat, Video, and Render may be fully removed.")
    if machine == "local":
        return await _remove_local_studio(modality)
    target = next((row for row in monitor.registry
                   if row.get("machine") == machine
                   and row.get("modality") == modality), None)
    # As on the local path, a retired family has no registry row of its own, so
    # reach the machine through any row it does have — the same fallback the
    # startup-install route already uses. Removal is about that Mac's checkout,
    # not its registration here; every id-scoped cleanup below is a no-op for a
    # row that never existed, and `forget_machine` only evicts a peer cache.
    peer = target or next((row for row in monitor.registry
                           if row.get("machine") == machine), None)
    if peer is None:
        raise HTTPException(404, f"unknown machine: {machine}")
    studio_id = target["id"] if target is not None else f"{modality}@{machine}"
    broker.set_maintenance(studio_id, True)
    try:
        if fleet_ops.studio_has_active_work(studio_id):
            raise HTTPException(
                409, "This Studio has active Hub work; wait for it to finish before removing it.")
        from . import registry
        registry.set_studio_enabled(machine, studio_id, False)
        result = await peers.remove_remote_studio(monitor._client, peer, modality)
        if not result.get("ok"):
            raise HTTPException(409, {
                "code": "routing_disabled_pending_retry",
                "message": result.get("error", "remote Studio removal failed"),
                "routing_disabled": True,
            })
        registry.remove_studio(studio_id)
        monitor.reload_registry()
        monitor.forget_studios({studio_id})
        fleet_ops.forget_studios({studio_id})
        peers.forget_machine(machine)
        return {**result, "routing_enabled": False}
    finally:
        broker.set_maintenance(studio_id, False)


@app.get("/api/hub/fleet")
def get_fleet(request: Request):
    """Fleet-token status. Reveal it only locally or to a signed-in owner."""
    token = peers.fleet_token()
    out = {"fleet_token_set": token is not None}
    owner_session = auth.valid_browser_session(
        request.cookies.get(auth.SESSION_COOKIE_NAME)
    )
    if (is_loopback(request) or owner_session) and token:
        out["token"] = token
    return out


@app.post("/api/hub/fleet")
async def set_fleet(body: dict):
    """Save locally, optionally synchronizing and verifying every peer Hub."""
    token = str(body.get("token") or "").strip()
    if not 12 <= len(token) <= 512:
        raise HTTPException(400, "fleet credential must be 12 to 512 characters")
    coordinator = _repair_coordinator_from_app()

    def local_commit(value: str) -> None:
        with coordinator.controller_mutation():
            peers.set_fleet_token(value)
            peers._cache.clear()

    sync = None
    if body.get("sync"):
        sync = await peers.sync_fleet_token(
            monitor.registry, monitor._client, token, local_commit=local_commit,
        )
    else:
        local_commit(token)
    return {"ok": True, "fleet_token_set": True, "sync": sync}


@app.get("/api/hub/memory")
async def get_fleet_memory():
    return await memory_control.inventory()


@app.put("/api/hub/memory-policy")
async def put_fleet_memory_policy(body: FleetMemoryPolicyBody):
    try:
        return await memory_control.set_mode(body.mode, body.studio_ids)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/hub/memory/release")
async def release_fleet_memory(body: FleetMemoryReleaseBody):
    try:
        return await memory_control.release(body.studio_ids)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def _managed_release_service() -> ReleaseReconciler:
    if release_reconciler is None:
        raise HTTPException(503, "Managed release reconciliation is still starting.")
    return release_reconciler


def _require_managed_release_role(request: Request, expected: str) -> dict[str, Any]:
    if not auth.valid_machine_token(request, HUB_TOKEN):
        raise HTTPException(
            401,
            "A header machine credential is required for managed release writes.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    settings = control_plane.public_settings()
    role = settings.get("role")
    if role == expected:
        return settings
    if role == "standalone":
        raise HTTPException(409, f"Configure this Hub as a {expected} first.")
    raise HTTPException(403, f"This managed release write requires the {expected} role.")


def _release_identity(settings: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": settings.get("role"),
        "site_id": settings.get("site_id"),
        "site_name": settings.get("site_name"),
        "controller_id": settings.get("controller_id"),
    }


def _schedule_release(release_id: str) -> None:
    _managed_release_service().schedule(release_id)


@app.get("/api/hub/maintenance/release-intent")
def get_release_intent():
    service = _managed_release_service()
    settings = control_plane.public_settings()
    state = service.state_snapshot()
    return {
        **_release_identity(settings),
        "desired": state["desired"],
        "activation": state["activation"],
        "jobs": list(state["jobs"].values()),
    }


@app.put("/api/hub/maintenance/release-intent")
def put_release_intent(request: Request, body: dict[str, Any]):
    settings = _require_managed_release_role(request, "controller")
    try:
        changed, manifest = _managed_release_service().replace_intent(body)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {
        "ok": True,
        "accepted": True,
        "changed": changed,
        "release_id": manifest["release_id"],
        "sequence": manifest["sequence"],
        **_release_identity(settings),
    }


@app.delete("/api/hub/maintenance/release-intent")
def withdraw_release_intent(request: Request):
    """Withdraw the desired release intent so a fresh one can be published.

    A release job that can never terminate blocks every later intent.  This is
    the operator's exit: it clears the intent, its activation, and its jobs in
    one transaction, and touches nothing else.  Withdrawing nothing succeeds as
    a no-op.  The withdrawn intent is recorded in the service log so it stays
    traceable after its state is gone.
    """
    settings = _require_managed_release_role(request, "controller")
    try:
        record = _managed_release_service().withdraw_intent()
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    _hub_log.warning(
        "release-intent-withdrawn %s",
        json.dumps({**record, **_release_identity(settings)}, sort_keys=True),
    )
    return {"ok": True, "accepted": True, **record, **_release_identity(settings)}


@app.post(
    "/api/hub/maintenance/release-intent/{release_id}/activate",
    status_code=202,
)
async def activate_release_intent(
    release_id: str, request: Request, body: ReleaseActivationBody | None = None,
):
    settings = _require_managed_release_role(request, "controller")
    service = _managed_release_service()
    try:
        job = service.activate(
            release_id,
            genstudio_run_reference=(body.genstudio_run_reference if body else None),
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    _schedule_release(release_id)
    return {
        "ok": True,
        "accepted": True,
        "job_id": job["id"],
        "release_id": release_id,
        **_release_identity(settings),
    }


@app.get("/api/hub/maintenance/release-jobs/{job_id}")
def get_release_job(job_id: str):
    try:
        return {
            **_managed_release_service().job_snapshot(job_id),
            **_release_identity(control_plane.public_settings()),
        }
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/hub/maintenance/managed-update", status_code=202)
async def admit_managed_update(request: Request, body: dict[str, Any]):
    settings = _require_managed_release_role(request, "agent")
    try:
        admission = _managed_release_service().admit_and_schedule_managed_update(body)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {**admission, **_release_identity(settings)}


@app.get("/api/hub/maintenance/managed-update/{job_id}")
def get_managed_update(job_id: str):
    try:
        return {
            **_managed_release_service().managed_update_snapshot(job_id),
            **_release_identity(control_plane.public_settings()),
        }
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/hub/maintenance/preflight")
def get_preflight():
    return fleet_ops.preflight_snapshot()


@app.post("/api/hub/maintenance/preflight")
async def run_fleet_preflight():
    return await fleet_ops.run_preflight(monitor)


@app.get("/api/hub/maintenance/studio-versions")
def get_studio_versions():
    return fleet_ops.studio_versions_snapshot(monitor)


@app.post("/api/hub/maintenance/studio-versions")
async def rescan_studio_versions():
    return await fleet_ops.scan_studio_versions(monitor)


@app.get("/api/hub/maintenance/updates")
def list_fleet_updates():
    return {"updates": fleet_ops.update_snapshot()}


@app.post("/api/hub/maintenance/updates")
async def start_fleet_updates(body: UpdateRequest):
    try:
        published = await fleet_ops.refresh_published_versions(force=True)
        selected = {
            studio.get("modality")
            for studio in monitor.registry
            if studio.get("id") in body.studio_ids
        }
        unverified = sorted(
            modality for modality in selected
            if not (published.get("versions") or {}).get(modality)
            or (published.get("errors") or {}).get(modality)
        )
        if unverified:
            raise ValueError(
                "could not freshly verify the published release for "
                + ", ".join(unverified)
                + "; retry when GitHub is reachable"
            )
        return fleet_ops.start_updates(monitor, body.studio_ids)
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.get("/api/hub/maintenance/updates/{job_id}")
def get_fleet_update(job_id: str):
    job = fleet_ops.update_snapshot(job_id)
    if not job:
        raise HTTPException(404, "unknown update")
    return job


@app.get("/api/hub/maintenance/generation-installs")
def list_generation_installs():
    return {"installs": fleet_ops.generation_install_snapshot()}


@app.post("/api/hub/maintenance/generation-installs")
async def start_generation_installs_route(body: GenerationInstallRequest):
    """Explicitly reinstall generation dependencies across sibling Studios.

    The Hub starts one trusted ``install_generation.js`` per machine and
    reports a durable job.  ``local_only`` is used by a peer Hub request so a
    primary Hub never causes recursive fleet fan-out.
    """
    try:
        return fleet_ops.start_generation_installs(
            monitor, body.studio_ids, local_only=body.local_only,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/hub/maintenance/generation-installs/{job_id}")
def get_generation_install(job_id: str):
    job = fleet_ops.generation_install_snapshot(job_id)
    if not job:
        raise HTTPException(404, "unknown generation install")
    return job


@app.get("/api/hub/maintenance/studio-update-repairs")
def list_studio_update_repairs():
    return {"repairs": fleet_ops.studio_update_repair_snapshot()}


@app.post("/api/hub/maintenance/studio-update-repairs")
async def start_studio_update_repairs_route(body: StudioUpdateRepairRequest):
    """Repair legacy Voice/Image update blockers on each target Agent locally."""
    try:
        return fleet_ops.start_studio_update_repairs(
            monitor, body.studio_ids, local_only=body.local_only,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/hub/maintenance/studio-update-repairs/{job_id}")
def get_studio_update_repair(job_id: str):
    job = fleet_ops.studio_update_repair_snapshot(job_id)
    if not job:
        raise HTTPException(404, "unknown Studio update repair")
    return job


@app.post("/api/hub/maintenance/studio-update-repairs/{job_id}/retry")
async def retry_studio_update_repair(job_id: str):
    try:
        return fleet_ops.retry_studio_update_repairs(monitor, job_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/hub/maintenance/self-update")
def self_update():
    """Compatibility entry point for older controllers.

    Use the Hub's verified updater rather than Pinokio ``update.js`` so a stale
    launcher session cannot swallow the request. New controllers use the exact
    managed-update API directly and verify the resulting commit.
    """
    before = _app_version()
    try:
        result = auto_updater.trigger_update(after_current=False)
    except UpdateError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True, "version_before": before, "state": result.get("state")}


@app.get("/api/hub/maintenance/hub-updates")
def list_hub_updates():
    return {"updates": fleet_ops.hub_update_snapshot()}


@app.post("/api/hub/maintenance/hub-updates")
async def start_hub_updates_route(body: dict):
    """Update the Studio Hub on the agent Macs remotely. Each reachable peer Hub
    self-updates and restarts; peers already at the latest version are skipped.
    Optional body {"machines": [...]}; omit to update every registered machine."""
    machines = body.get("machines")
    if machines is not None and not isinstance(machines, list):
        raise HTTPException(400, "machines must be a list of machine names")
    if _time.time() - _update_state["checked_at"] > 6 * 3600 or not _update_state["latest"]:
        _refresh_latest_version()  # make sure we know the target version to skip up-to-date peers
    try:
        return fleet_ops.start_hub_updates(
            monitor, _update_state["latest"], _update_state["commit"], machines,
        )
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.get("/api/hub/maintenance/hub-updates/{job_id}")
def get_hub_update(job_id: str):
    job = fleet_ops.hub_update_snapshot(job_id)
    if not job:
        raise HTTPException(404, "unknown hub update")
    return job


@app.get("/api/hub/maintenance/hub-versions")
def get_hub_versions():
    """Last-known Hub version per agent Mac (persisted, survives restarts)."""
    return {"latest": _update_state["latest"],
            "machines": fleet_ops.hub_versions_snapshot(monitor)}


@app.post("/api/hub/maintenance/hub-versions")
async def rescan_hub_versions():
    """Re-query every agent Mac's Hub version now and cache it. Always refreshes
    the published 'latest' too, so an explicit rescan can't compare against a
    stale target (which made a newer peer look like it needed a downgrade)."""
    _refresh_latest_version()
    machines = await fleet_ops.scan_hub_versions(monitor)
    return {"latest": _update_state["latest"], "machines": machines}


@app.post("/api/hub/registry/reload")
async def reload_registry():
    """Re-read studios.json after editing it — no restart needed."""
    coordinator = getattr(app.state, "enrollment_repair_coordinator", None)
    prepared = (
        coordinator.resolve_registry_rows(registry.load_registry())
        if coordinator is not None else None
    )
    mutation = coordinator.controller_mutation() if coordinator is not None else nullcontext()
    with mutation:
        _reload_registry_and_note_repair(prepared)
    return {"ok": True, "studios": len(monitor.registry)}


def _validated_registry_identity(body: dict) -> tuple[str, str]:
    """Accept an IPv4 address or ordinary DNS/Tailscale hostname only.

    Registry values become network destinations and stable IDs, so schemes,
    paths, whitespace and delimiter characters are never valid input.
    """
    import ipaddress
    import re

    host = str(body.get("host") or "").strip().lower()
    if not host or len(host) > 253:
        raise HTTPException(400, "host is required (LAN or Tailscale IPv4/DNS name)")
    try:
        ipaddress.IPv4Address(host)
    except ipaddress.AddressValueError:
        labels = host.rstrip(".").split(".")
        valid = (all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                     for label in labels) and all(labels))
        if not valid:
            raise HTTPException(400, "host must be an IPv4 address or DNS/Tailscale name")
        host = host.rstrip(".")
    default_machine = host.replace(".", "-")
    machine = str(body.get("machine") or default_machine).strip()
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", machine)
            or "@" in machine):
        raise HTTPException(400, "machine name must use letters, numbers, dots, dashes, or underscores")
    return host, machine


def _registration_identity(body: dict) -> tuple[str, str, str | None]:
    """Resolve an optional profile into a stable, editable machine id."""
    prepared = dict(body)
    profile_id = str(prepared.get("hardware_profile_id") or "").strip() or None
    if profile_id:
        if hardware_profiles.hardware_profile(profile_id) is None:
            raise HTTPException(400, f"unknown hardware profile {profile_id!r}")
        if not str(prepared.get("machine") or "").strip():
            prepared["machine"] = hardware_profiles.suggested_machine_id(
                profile_id, _registered_machine_ids(),
            )
    host, machine = _validated_registry_identity(prepared)
    return host, machine, profile_id


@app.delete("/api/hub/registry/machines/{machine}")
def remove_machine_route(machine: str):
    """Unregister a machine and purge its live fleet-control state."""
    from .registry import remove_machine

    if machine == "local":
        raise HTTPException(400, "the local machine's studios can't be removed")
    coordinator = _repair_coordinator_from_app()
    current = coordinator.resolve_registry_rows(list(monitor.registry))
    prepared = coordinator.resolve_registry_rows([
        row for row in monitor.registry if row.get("machine") != machine
    ])
    try:
        with coordinator.controller_mutation(machine=machine):
            coordinator._require_registry_rows_current(current)
            studio_ids = {studio["id"] for studio in monitor.registry
                          if studio.get("machine") == machine}
            removed = remove_machine(machine)
            if not removed:
                raise HTTPException(404, f"no registered studios for machine {machine!r}")
            _reload_registry_and_note_repair(prepared)
    except RepairStoreError as exc:
        _raise_registry_mutation_error(exc)
    monitor.forget_studios(studio_ids)
    peers.forget_machine(machine)
    fleet_ops.forget_machine(machine, studio_ids)
    for sid in studio_ids:
        broker.set_maintenance(sid, False)
    return {"ok": True, "removed": removed}


@app.delete("/api/hub/registry/studios/{studio_id:path}")
def remove_studio_route(studio_id: str):
    """Unregister ONE studio (e.g. a music/video studio that isn't installed on
    that machine) without removing the rest. It reappears only if it's actually
    running the next time you Refetch, or if you re-add it manually."""
    from .registry import remove_studio
    entry = next((s for s in monitor.registry if s["id"] == studio_id), None)
    if entry and entry.get("machine", "local") == "local":
        raise HTTPException(400, "the local machine's studios can't be removed")
    coordinator = _repair_coordinator_from_app()
    machine = str((entry or {}).get("machine", "local"))
    current = coordinator.resolve_registry_rows(list(monitor.registry))
    prepared = coordinator.resolve_registry_rows([
        row for row in monitor.registry if row.get("id") != studio_id
    ])
    try:
        with coordinator.controller_mutation(machine=machine):
            coordinator._require_registry_rows_current(current)
            removed = remove_studio(studio_id)
            if not removed:
                raise HTTPException(404, f"no registered studio {studio_id!r}")
            _reload_registry_and_note_repair(prepared)
    except RepairStoreError as exc:
        _raise_registry_mutation_error(exc)
    monitor.forget_studios({studio_id})
    fleet_ops.forget_studios({studio_id})
    if entry:
        peers.forget_machine(entry.get("machine", "local"))
    broker.set_maintenance(studio_id, False)
    return {"ok": True, "removed": studio_id}


@app.post("/api/hub/registry/add")
async def add_machine_manual(body: dict):
    """Pre-register a machine's studios WITHOUT probing — works while the
    machine is offline. The entries persist and turn 'up' on their own once the
    machine is reachable. `modalities` defaults to the production Image and
    Voice siblings."""
    from .registry import (FAMILY_PORTS, add_user_entries,
                           build_machine_entries)

    host, machine, profile_id = _registration_identity(body)
    modalities = body.get("modalities") or list(PRODUCTION_STUDIO_MODALITIES)
    valid = set(FAMILY_PORTS.values())
    bad = [m for m in modalities if m not in valid]
    if bad:
        raise HTTPException(400, f"unknown modalities: {bad}")
    entries = build_machine_entries(host, machine, modalities)
    coordinator = _repair_coordinator_from_app()
    registration = coordinator.resolve_enrollment_registration(machine, host)
    prepared = coordinator.resolve_registry_rows(
        _registry_with_entries(list(monitor.registry), entries)
    )
    try:
        with coordinator.controller_mutation():
            if _registry_identity_changes(list(monitor.registry), entries):
                coordinator.require_enrollment_registration_mutable(
                    machine, host, resolved=registration,
                )
            added = add_user_entries(entries)
            _reload_registry_and_note_repair(prepared)
            profile = None
            if profile_id and machine in _registered_machine_ids():
                profile = hardware_profiles.set_machine_hardware_profile(machine, profile_id)
    except RepairStoreError as exc:
        _raise_registry_mutation_error(exc)
    return {"host": host, "machine": machine, "requested": modalities,
            "registered": added,
            "hardware_profile": profile,
            "note": "saved — will show 'down' until the machine is reachable, "
                    "then activate automatically"}


@app.post("/api/hub/registry/discover")
async def discover_machine(body: dict):
    """Probe another Mac (LAN/Tailscale IP) for the studio family ports and
    register whatever answers. Each Mac only runs some studios — the registry
    reflects exactly what exists where."""
    import httpx

    from .registry import FAMILY_PORTS, MODALITY_EMOJI, add_user_entries

    host, machine, profile_id = _registration_identity(body)
    machine_was_supplied = bool(str(body.get("machine") or "").strip())
    found, detected_hardware = [], None
    async with httpx.AsyncClient() as client:
        if not profile_id:
            try:
                peer = await client.get(
                    f"http://{host}:{peers.DEFAULT_HUB_PORT}/api/hub/resources?local_only=true",
                    headers={"X-Hub-Token": peers.fleet_token()}, timeout=4.0,
                )
                if peer.is_success:
                    detected_hardware = peer.json().get("host")
                    matched = hardware_profiles.matching_hardware_profile(detected_hardware)
                    if matched:
                        profile_id = matched["id"]
                        if not machine_was_supplied:
                            machine = hardware_profiles.suggested_machine_id(
                                profile_id, _registered_machine_ids())
            except (httpx.HTTPError, ValueError):
                pass
        for port, modality in FAMILY_PORTS.items():
            try:
                r = await client.get(f"http://{host}:{port}/api/health", timeout=4.0)
                if not r.json().get("ok"):
                    continue
                v = await client.get(f"http://{host}:{port}/api/version", timeout=4.0)
                title = v.json().get("title", f"{modality} @ {machine}")
            except Exception:
                continue
            found.append({"port": port, "modality": modality, "title": title})
    proposed_entries = [{
        "id": f"{row['modality']}@{machine}",
        "title": f"{row['title']} ({machine})",
        "modality": row["modality"], "host": host, "port": row["port"],
        "machine": machine, "emoji": MODALITY_EMOJI[row["modality"]],
    } for row in found]
    coordinator = _repair_coordinator_from_app()
    registration = coordinator.resolve_enrollment_registration(machine, host)
    prepared = coordinator.resolve_registry_rows(
        _registry_with_entries(list(monitor.registry), proposed_entries)
    )
    try:
        with coordinator.controller_mutation():
            known = {(s["host"], s["port"]) for s in monitor.registry}
            entries = [
                entry for entry in proposed_entries
                if (entry["host"], entry["port"]) not in known
                or any(
                    row.get("id") == entry["id"]
                    for row in monitor.registry
                )
            ]
            if _registry_identity_changes(list(monitor.registry), entries):
                coordinator.require_enrollment_registration_mutable(
                    machine, host, resolved=registration,
                )
            added = add_user_entries(entries) if entries else 0
            if entries:
                _reload_registry_and_note_repair(prepared)
            profile = None
            if profile_id and machine in _registered_machine_ids():
                profile = hardware_profiles.set_machine_hardware_profile(machine, profile_id)
    except RepairStoreError as exc:
        _raise_registry_mutation_error(exc)
    return {"host": host, "machine": machine, "found": found,
            "registered": added,
            "hardware_profile": profile,
            "detected_hardware": detected_hardware,
            "note": None if found else
            "nothing answered — is the machine on and reachable over Tailscale?"}


# ── dashboard ──────────────────────────────────────────────────────────────
_index_gzip_cache: tuple[float, bytes] | None = None


def _index_gzipped(path: Path) -> bytes:
    """Gzip index.html once per build.

    The dashboard is a single ~540 KB page, 86% of it inline JS/CSS, and it is
    deliberately no-store — so every load pays the full size. Sites on a slow
    uplink measured ~70 KB/s, making that ~8s of pure transfer. Compressing
    cuts it ~4x. Cached by mtime because re-gzipping 540 KB on every load is
    real CPU on the 8 GB M1 boxes.
    """
    global _index_gzip_cache
    mtime = path.stat().st_mtime
    if _index_gzip_cache is None or _index_gzip_cache[0] != mtime:
        _index_gzip_cache = (mtime, gzip.compress(path.read_bytes(), 6))
    return _index_gzip_cache[1]


@app.get("/")
def index(request: Request):
    # no-store so Pinokio's embedded webview never serves a stale build after
    # an update — the #1 cause of "I don't see my changes".
    path = FRONTEND_DIR / "index.html"
    headers = {"Cache-Control": "no-store, max-age=0",
               "Vary": "Accept-Encoding"}
    if "gzip" in request.headers.get("accept-encoding", "").lower():
        return Response(
            _index_gzipped(path), media_type="text/html",
            headers={**headers, "Content-Encoding": "gzip"})
    return FileResponse(path, headers=headers)
