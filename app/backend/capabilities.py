"""Private, read-only GenStudio capability contract for one Studio Hub site.

The snapshot composes existing monitor, registry, catalog, hardware, resource,
and scheduler state. It never reads customer jobs or emits prompts, generated
content, credentials, local cache paths, or global ownership identifiers.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone

from . import (broker, chat_jobs, hardware_profiles, memory_admission,
               model_exposure, peers, transcription_jobs)
from .registry import machine_enabled, studio_enabled
from .resources import host_stats

SCHEMA_NAME = "studiohub.site-capabilities"
SCHEMA_VERSION = 3

OPERATION_BY_MODALITY = {
    "image": "image.text_to_image",
    "music": "music.generation",
    "voice": "voice.tts",
    "transcription": "audio.transcription",
    "chat": "chat.completion",
    "video": "video.generation",
    "render": "video.render",
}

_IMMUTABLE_REVISION = re.compile(r"^(?:sha256:)?[0-9a-fA-F]{40,64}$")
_REVISION_FIELDS = (
    "runtime_revision", "model_revision", "snapshot_revision", "commit_sha", "revision",
)
_INPUT_LIMIT_FIELDS = (
    "max_text_characters", "max_input_characters", "max_prompt_characters",
    "max_input_duration_seconds", "max_audio_duration_seconds",
    "min_reference_audio_seconds", "max_reference_audio_seconds",
)
_OUTPUT_LIMIT_FIELDS = (
    "max_duration_seconds", "max_duration_s", "max_frames", "sample_rate_hz",
    "max_output_tokens", "max_tokens",
)
_SAFE_SIZE_FIELDS = ("aspect_ratio", "label", "width", "height", "tier", "default")
_SAFE_CUSTOM_FIELDS = ("min_px", "max_px", "step", "max_pixels")
_GENERATION_CONTROL_FIELDS = (
    "prompt", "aspect_ratio", "negative_prompt", "steps", "guidance", "seed",
    "batch", "image_strength", "runtime_quantization", "loras", "duration",
    "duration_seconds", "language", "speed", "voice_mode", "resolution",
    "frames", "fps", "width", "height",
)
_GENERATION_DEFAULT_FIELDS = (
    "steps", "guidance", "seed", "image_strength", "aspect_ratio", "resolution",
    "duration", "duration_seconds", "language", "speed", "voice_mode", "format",
    "frames", "fps", "width", "height", "dtype", "sample_rate_hz",
)


def _rfc3339(stamp: float) -> str:
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_scalar(value):
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:500]
    return None


def _selected(source: dict, fields: tuple[str, ...]) -> dict:
    result = {}
    for field in fields:
        if field not in source:
            continue
        value = _safe_scalar(source.get(field))
        if value is not None:
            result[field] = value
    return result


def _string_list(value, *, limit: int = 100) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item)[:200] for item in value[:limit]
            if isinstance(item, (str, int, float))]


def _immutable_runtime_revision(model: dict) -> tuple[str | None, str | None, str]:
    sources = [model]
    cache = model.get("cache")
    if isinstance(cache, dict):
        sources.append(cache)
    saw_unverified = False
    for source in sources:
        for field in _REVISION_FIELDS:
            value = source.get(field)
            if value is None:
                continue
            text = str(value).strip()
            if _IMMUTABLE_REVISION.fullmatch(text):
                return text.lower(), field, "verified_immutable"
            if text:
                saw_unverified = True
    return None, None, "reported_but_not_immutable" if saw_unverified else "not_reported"


def _voice_modes(model: dict) -> list[str]:
    capabilities = set(_string_list(model.get("capabilities")))
    repo = str(model.get("repo") or "").lower()
    modes = []
    if "voice-cloning" in capabilities:
        modes.append("reference_audio_clone")
    if "voice-design" in capabilities or "voicedesign" in repo:
        modes.append("voice_design")
    if "tts" in capabilities and not modes:
        modes.append("preset_voice")
    return modes


def _controls(model: dict, modality: str) -> dict:
    controls = {
        "capabilities": _string_list(model.get("capabilities")),
    }
    languages = _string_list(model.get("languages"))
    if languages:
        controls["languages"] = languages

    sizes = []
    for size in (model.get("sizes") or [])[:100]:
        if isinstance(size, dict):
            safe = _selected(size, _SAFE_SIZE_FIELDS)
            if safe:
                sizes.append(safe)
    if sizes:
        controls["sizes"] = sizes
        controls["aspect_ratios"] = sorted({
            str(size["aspect_ratio"]) for size in sizes if size.get("aspect_ratio")
        })
    else:
        aspect_ratios = _string_list(model.get("aspect_ratios"))
        if aspect_ratios:
            controls["aspect_ratios"] = aspect_ratios

    resolutions = _string_list(model.get("resolutions"))
    if resolutions:
        controls["resolutions"] = resolutions
    custom = model.get("custom")
    if isinstance(custom, dict):
        safe_custom = _selected(custom, _SAFE_CUSTOM_FIELDS)
        if safe_custom:
            controls["custom_dimensions"] = safe_custom
    generation = model.get("generation_profile")
    if isinstance(generation, dict):
        enabled = generation.get("controls")
        defaults = generation.get("defaults")
        if isinstance(enabled, dict):
            controls["generation_controls"] = {
                key: bool(enabled[key]) for key in _GENERATION_CONTROL_FIELDS
                if key in enabled
            }
        if isinstance(defaults, dict):
            safe_defaults = _selected(defaults, _GENERATION_DEFAULT_FIELDS)
            if safe_defaults:
                controls["defaults"] = safe_defaults
    video_defaults = model.get("video_defaults")
    if isinstance(video_defaults, dict):
        safe_defaults = _selected(video_defaults, _GENERATION_DEFAULT_FIELDS)
        if safe_defaults:
            controls["defaults"] = safe_defaults
    if modality == "voice":
        controls["voice_modes"] = _voice_modes(model)
    if modality == "chat" and model.get("verified_token_usage") is True:
        controls["verified_token_usage"] = True
    return controls


def _candidate_available_slots(candidate: dict | None) -> int | None:
    if not isinstance(candidate, dict):
        return None
    capacity = candidate.get("capacity")
    if not isinstance(capacity, dict):
        return None
    value = capacity.get("available_slots")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return max(0, value)


def _capacity_eligible(model: dict, *, online: bool, ready: bool,
                       drained: bool, maintenance: bool,
                       machine_quarantined: bool,
                       memory_capacity_eligible: bool | None) -> bool:
    """Check static model/worker gates, intentionally excluding occupancy."""
    return bool(
        online and ready and not drained and not maintenance
        and not machine_quarantined
        and model.get("runtime_compatible") is not False
        and model.get("hub_ready") is not False
        and not bool(model.get("hub_catalog_stale"))
        and not bool(model.get("hub_catalog_error"))
        and bool(model.get("hub_cached"))
        and memory_capacity_eligible is not False
        and model.get("qualified_revision_match") is not False
        and model.get("execution_ready") is not False
    )


def _catalog_reports_busy(model: dict, candidate: dict | None,
                          studio: dict, status: dict,
                          protections: dict[str, dict]) -> bool:
    """Return true only for a usable worker whose catalog reports it busy."""
    if _candidate_available_slots(candidate) != 0:
        return False
    machine = studio.get("machine", "local")
    if status.get("status") != "up" or status.get("health_recovering"):
        return False
    if (not machine_enabled(machine)
            or not studio_enabled(machine, studio.get("id"))
            or broker.in_maintenance(studio.get("id"))
            or bool((protections.get(machine) or {}).get("quarantined"))):
        return False
    if (not model.get("hub_cached")
            or model.get("hub_catalog_stale")
            or model.get("hub_catalog_error")
            or model.get("runtime_compatible") is False
            or model.get("hub_ready") is False
            or model.get("qualified_revision_match") is False
            or model.get("execution_ready") is False):
        return False
    if memory_admission.applies_to(model.get("hub_modality")):
        admission = memory_admission.describe(
            str(model.get("repo") or model.get("model_id") or "unknown"),
            model,
        )
        host_known, host = _machine_host(machine)
        if host_known and host:
            total_floor = admission.get("effective_min_total_memory_gb") or 0
            if float(host.get("total_gb") or 0) < total_floor:
                return False
    return True


def _model_capability(model: dict, studio: dict, worker: dict,
                      managed_release_reason: str | None = None) -> dict:
    modality = str(model.get("hub_modality") or studio.get("modality") or "unknown")
    operation = str(model.get("hub_operation") or OPERATION_BY_MODALITY.get(
        modality, f"{modality}.operation"))
    candidate = model.get("hub_candidate")
    candidate = candidate if isinstance(candidate, dict) else None
    repo = str(model.get("repo") or model.get("model_id") or "unknown")[:500]
    installed = bool(model.get("hub_cached"))
    runtime_compatible = model.get("runtime_compatible") is not False
    subsystem_ready = model.get("hub_ready") is not False
    qualified_revision_match = model.get("qualified_revision_match")
    if type(qualified_revision_match) is not bool:
        qualified_revision_match = None
    execution_ready = model.get("execution_ready")
    if type(execution_ready) is not bool:
        execution_ready = None
    admission = None
    memory_ready = None
    memory_capacity_eligible = None
    if memory_admission.applies_to(modality):
        admission = memory_admission.describe(repo, model)
        host_known, host = _machine_host(studio.get("machine", "local"))
        if host_known and host:
            total_floor = admission.get("effective_min_total_memory_gb") or 0
            free_floor = admission.get("effective_min_free_memory_gb") or 0
            total_memory = host.get("total_gb")
            available_memory = host.get("available_gb")
            if (isinstance(total_memory, (int, float))
                    and not isinstance(total_memory, bool)):
                memory_capacity_eligible = bool(
                    float(total_memory) >= total_floor
                )
                memory_ready = bool(
                    memory_capacity_eligible
                    and isinstance(available_memory, (int, float))
                    and not isinstance(available_memory, bool)
                    and float(available_memory) >= free_floor
                )
            else:
                # A remote worker can be reachable while its host telemetry is
                # absent. It may still report a free worker slot, but it cannot
                # enter the durable physical-capacity denominator without
                # evidence for the model's RAM floor.
                memory_capacity_eligible = not bool(total_floor)
                memory_ready = None
            admission = {
                **admission,
                "observed_total_memory_gb": total_memory,
                "observed_available_memory_gb": available_memory,
                "eligible_now": memory_ready,
            }
        else:
            total_floor = admission.get("effective_min_total_memory_gb") or 0
            memory_capacity_eligible = not bool(total_floor)
            admission = {**admission, "observed_total_memory_gb": None,
                         "observed_available_memory_gb": None,
                         "eligible_now": None}
    reported_available_slots = _candidate_available_slots(candidate)
    catalog_error = bool(str(model.get("hub_catalog_error") or "").strip())
    catalog_stale = bool(model.get("hub_catalog_stale"))
    model_ready = (
        runtime_compatible and subsystem_ready and not catalog_error
        and not catalog_stale and installed
        and memory_ready is not False
        and qualified_revision_match is not False
        and execution_ready is not False
    )
    capacity_eligible = _capacity_eligible(
        model,
        online=worker["online"],
        ready=worker["ready"],
        drained=worker["drained"],
        maintenance=worker["maintenance"],
        machine_quarantined=worker["machine_quarantined"],
        memory_capacity_eligible=memory_capacity_eligible,
    )
    available_now = bool(
        worker["available_capacity"]["slots"] and model_ready
        and (reported_available_slots is None or reported_available_slots > 0)
    )
    catalog_busy = (
        reported_available_slots is not None
        and reported_available_slots <= 0
        and model_ready
    )
    if not worker["online"]:
        reason = "worker_offline"
    elif worker["maintenance"]:
        reason = "worker_maintenance"
    elif worker["drained"]:
        reason = "worker_drained"
    elif worker["busy"]:
        reason = "worker_busy"
    elif worker["physical_machine_busy"]:
        reason = "physical_machine_busy"
    elif not worker["ready"]:
        reason = "worker_not_ready"
    elif catalog_error:
        reason = "catalog_error"
    elif catalog_stale:
        reason = "catalog_stale"
    elif qualified_revision_match is False:
        reason = "runtime_revision_mismatch"
    elif execution_ready is False:
        reason = "worker_execution_unready"
    elif not runtime_compatible:
        reason = "runtime_incompatible"
    elif not subsystem_ready:
        reason = "subsystem_unavailable"
    elif memory_ready is False and admission and (
            (admission.get("observed_total_memory_gb") or 0)
            < (admission.get("effective_min_total_memory_gb") or 0)):
        reason = "insufficient_total_memory"
    elif memory_ready is False:
        reason = "waiting_for_memory"
    elif not installed:
        reason = "model_not_installed"
    elif catalog_busy:
        reason = "worker_busy"
    else:
        reason = None
    if managed_release_reason is not None:
        available_now = False
        capacity_eligible = False
        reason = managed_release_reason

    if candidate:
        revision = candidate.get("runtime_revision")
        revision_source = "genstudio_candidate.runtime_revision"
        revision_status = "verified_immutable"
    else:
        revision, revision_source, revision_status = _immutable_runtime_revision(model)
    internal_id = (
        model.get("model_id") or model.get("repo") or "unknown"
    )
    return {
        "operation": operation,
        "internal_model_id": str(internal_id)[:500],
        "runtime_revision": revision,
        "revision_source": revision_source,
        "revision_status": revision_status,
        "provider": "local",
        "execution_lane": "local",
        "audit": ({
            "audit_id": candidate.get("audit_id"),
            "audit_status": candidate.get("audit_status"),
            "contract_hash": candidate.get("contract_hash"),
            "audited_at": candidate.get("audited_at"),
        } if candidate else None),
        "exposure": model.get("hub_exposure"),
        "input_limits": (
            candidate.get("input_limits", {}) if candidate
            else _selected(model, _INPUT_LIMIT_FIELDS)
        ),
        "output_limits": (
            candidate.get("output_limits", {}) if candidate
            else _selected(model, _OUTPUT_LIMIT_FIELDS)
        ),
        "controls": (
            candidate.get("controls", {}) if candidate
            else _controls(model, modality)
        ),
        "adapter": candidate.get("adapter", {}) if candidate else {},
        "capacity": candidate.get("capacity", {}) if candidate else {},
        "hardware": candidate.get("hardware", {}) if candidate else {},
        "catalog_observation": {
            "observed_at": (
                _rfc3339(model["hub_catalog_observed_at"])
                if isinstance(model.get("hub_catalog_observed_at"), (int, float))
                else None
            ),
            "age_seconds": model.get("hub_catalog_age_seconds"),
            "stale": catalog_stale,
        },
        "memory_admission": admission,
        "availability": {
            "supported": True,
            "approved_for_genstudio": True,
            "installed": installed,
            "runtime_compatible": runtime_compatible,
            "revision_pinning_ready": revision is not None,
            "subsystem_ready": subsystem_ready,
            "qualified_revision_match": qualified_revision_match,
            "execution_ready": execution_ready,
            "available_now": available_now,
            "capacity_eligible": capacity_eligible,
            "reason": reason,
        },
    }


def _model_supply(workers: list[dict]) -> list[dict]:
    """Aggregate approved supply strictly from detailed worker evidence."""
    grouped: dict[tuple[str, str, str | None, str | None], dict] = {}
    for worker in workers:
        for model in worker.get("models") or []:
            audit = model.get("audit") or {}
            key = (
                model["internal_model_id"], model["operation"],
                model.get("runtime_revision"), audit.get("contract_hash"),
            )
            row = grouped.setdefault(key, {
                "internal_model_id": model["internal_model_id"],
                "operation": model["operation"],
                "runtime_revision": model.get("runtime_revision"),
                "contract_hash": audit.get("contract_hash"),
                "audit_id": audit.get("audit_id"),
                "machines": [],
            })
            availability = model["availability"]
            busy = bool(
                worker["busy"] or worker["physical_machine_busy"]
                or availability.get("reason") == "worker_busy"
            )
            row["machines"].append({
                "physical_machine_id": worker["physical_machine_id"],
                "service_id": worker["service_id"],
                "hardware_profile": worker.get("hardware_profile"),
                "online": worker["online"],
                "ready": bool(availability.get("available_now")),
                "busy": busy,
                "quarantined": worker["machine_quarantined"],
                "installed": availability.get("installed"),
                "availability_reason": availability.get("reason"),
                "available_slots": int(bool(availability.get("available_now"))),
                "capacity_eligible": bool(
                    availability.get("capacity_eligible")
                ),
                "catalog_observation": model.get("catalog_observation"),
                "memory_admission": model.get("memory_admission"),
            })
    result = []
    for row in grouped.values():
        machines = row["machines"]
        unique = {machine["physical_machine_id"] for machine in machines}
        row["installed_machine_count"] = len({
            machine["physical_machine_id"] for machine in machines
            if machine["installed"] is True
        })
        row["online_machine_count"] = len({
            machine["physical_machine_id"] for machine in machines
            if machine["online"]
        })
        row["ready_machine_count"] = len({
            machine["physical_machine_id"] for machine in machines
            if machine["ready"]
        })
        row["busy_machine_count"] = len({
            machine["physical_machine_id"] for machine in machines
            if machine["busy"]
        })
        row["offline_machine_count"] = len({
            machine["physical_machine_id"] for machine in machines
            if not machine["online"]
        })
        row["quarantined_machine_count"] = len({
            machine["physical_machine_id"] for machine in machines
            if machine["quarantined"]
        })
        row["offline_or_quarantined_machine_count"] = len({
            machine["physical_machine_id"] for machine in machines
            if not machine["online"] or machine["quarantined"]
        })
        row["available_physical_slots"] = len({
            machine["physical_machine_id"] for machine in machines
            if machine["ready"] and machine["available_slots"] > 0
        })
        row["eligible_physical_slots_total"] = len({
            machine["physical_machine_id"] for machine in machines
            if machine["capacity_eligible"]
        })
        row["machine_ids"] = sorted(unique)
        observed = [
            machine["catalog_observation"].get("observed_at")
            for machine in machines
            if isinstance(machine.get("catalog_observation"), dict)
            and machine["catalog_observation"].get("observed_at")
        ]
        row["last_catalog_refresh"] = max(observed) if observed else None
        row["stale"] = any(
            bool((machine.get("catalog_observation") or {}).get("stale"))
            for machine in machines
        )
        result.append(row)
    return sorted(result, key=lambda row: (
        row["operation"], row["internal_model_id"],
        row.get("runtime_revision") or "", row.get("contract_hash") or "",
    ))


def _capacity_evidence(model_supply: list[dict]) -> dict:
    """Reduce exact model observations into deduplicated capacity evidence."""
    eligible_machines: set[str] = set()
    eligible_services: set[tuple[str, str]] = set()
    available_machines: set[str] = set()
    by_operation: dict[str, dict[str, set]] = {}
    for supply in model_supply:
        operation = supply["operation"]
        evidence = by_operation.setdefault(operation, {
            "eligible_machines": set(),
            "eligible_services": set(),
            "available_machines": set(),
        })
        for machine in supply["machines"]:
            physical_id = machine["physical_machine_id"]
            service_key = (physical_id, machine["service_id"])
            if machine["capacity_eligible"]:
                eligible_machines.add(physical_id)
                eligible_services.add(service_key)
                evidence["eligible_machines"].add(physical_id)
                evidence["eligible_services"].add(service_key)
            if machine["ready"] and machine["available_slots"] > 0:
                available_machines.add(physical_id)
                evidence["available_machines"].add(physical_id)

    operation_totals = {
        operation: {
            "eligible_physical_machine_slots_total": len(
                values["eligible_machines"]
            ),
            "eligible_worker_service_slots_total": len(
                values["eligible_services"]
            ),
            "available_physical_machine_slots": len(
                values["available_machines"]
            ),
        }
        for operation, values in by_operation.items()
    }
    return {
        "eligible_physical_machine_slots_total": len(eligible_machines),
        "eligible_worker_service_slots_total": len(eligible_services),
        "available_physical_machine_slots": len(available_machines),
        "by_operation": operation_totals,
    }


def _machine_host(machine: str) -> tuple[bool, dict | None]:
    if machine == "local":
        return True, host_stats()
    peer = peers.cached(machine) or {}
    host = peer.get("host")
    return bool(peer.get("reachable") and isinstance(host, dict)), (
        host if isinstance(host, dict) else None
    )


def _missing_release_component(managed_release: dict, component: str,
                               state: str = "pending_offline") -> dict:
    desired = managed_release.get("desired") or {}
    target = (desired.get("components") or {}).get(component) or {}
    return {
        "component": component,
        "desired_release_id": desired.get("release_id"),
        "expected_version": target.get("expected_version"),
        "expected_commit": target.get("expected_commit"),
        "observed_version": None,
        "observed_commit": None,
        "state": state,
        "next_retry": managed_release.get("next_retry"),
        "converged": False,
    }


def _machine_release_evidence(managed_release: dict | None, machine: str) -> dict | None:
    if not isinstance(managed_release, dict) or not managed_release.get("desired"):
        return None
    existing = (managed_release.get("machines") or {}).get(machine)
    if isinstance(existing, dict):
        return existing
    components = {
        name: _missing_release_component(managed_release, name)
        for name in ("hub", "image", "voice")
    }
    return {
        "desired_release_id": managed_release["desired"].get("release_id"),
        "state": "pending",
        "next_retry": managed_release.get("next_retry"),
        "converged": False,
        "components": components,
    }


def _managed_release_reason(managed_release: dict | None,
                            machine_release: dict | None,
                            component: str | None) -> str | None:
    if not isinstance(managed_release, dict) or not managed_release.get("desired"):
        return None

    def blocks(state) -> bool:
        value = str(state or "")
        return value.startswith("blocked") or value == "release_blocked"

    if blocks(managed_release.get("site_state")):
        return "managed_release_blocked"
    components = (machine_release or {}).get("components") or {}
    rows = [components.get("hub")]
    if component in {"image", "voice"}:
        rows.append(components.get(component))
    if any(
        isinstance(row, dict) and blocks(row.get("state"))
        for row in rows
    ):
        return "managed_release_blocked"
    return None


async def build_snapshot(monitor, *, app_version: str, settings: dict,
                         readiness: dict, base_capacity: dict,
                         managed_release: dict | None = None) -> dict:
    """Build schema v3 without mutating or refreshing live worker state."""
    observed = time.time()
    aggregate = monitor.cached_aggregate_catalog()
    models_by_studio: dict[str, list[dict]] = {}
    for model in aggregate.get("models") or []:
        candidate = model_exposure.candidate_summary(model)
        if candidate is None:
            continue
        studio_id = model.get("hub_studio")
        if not studio_id:
            continue
        for operation in candidate["approved_operations"]:
            exposure = model_exposure.state_for(candidate, operation)
            if exposure.get("state") != "approved":
                continue
            models_by_studio.setdefault(str(studio_id), []).append({
                **model,
                "hub_operation": operation,
                "hub_candidate": candidate,
                "hub_exposure": exposure,
            })

    studios_by_id = {studio["id"]: studio for studio in monitor.registry}
    protections = broker.machine_protection_snapshot()
    catalog_busy_studios = set()
    catalog_busy_machines = set()
    for studio_id, models in models_by_studio.items():
        studio = studios_by_id.get(studio_id)
        if not studio:
            continue
        status = monitor.status.get(studio_id, {})
        if any(_catalog_reports_busy(model, model.get("hub_candidate"),
                                     studio, status, protections)
               for model in models):
            catalog_busy_studios.add(studio_id)
            catalog_busy_machines.add(studio.get("machine", "local"))

    busy = set(broker.busy_studios()) | set(chat_jobs.busy_studios) \
        | set(transcription_jobs.busy_studios)
    busy_machines = broker.busy_machines()
    for studio in monitor.registry:
        status = monitor.status.get(studio["id"], {})
        health = status.get("health")
        health_busy = bool(status.get("health_busy"))
        if isinstance(health, dict):
            health_busy = health_busy or bool(health.get("busy"))
            generation = health.get("generation")
            if isinstance(generation, dict):
                health_busy = health_busy or bool(generation.get("busy"))
        if health_busy:
            busy.add(studio["id"])
            busy_machines.add(studio.get("machine", "local"))
    busy.update(catalog_busy_studios)
    busy_machines.update(catalog_busy_machines)
    workers = []
    for studio in monitor.registry:
        studio_id = studio["id"]
        machine = studio.get("machine", "local")
        status = monitor.status.get(studio_id, {})
        online = status.get("status") == "up"
        maintenance = broker.in_maintenance(studio_id)
        drained = (
            not machine_enabled(machine)
            or not studio_enabled(machine, studio_id)
            or maintenance
        )
        quarantined = bool((protections.get(machine) or {}).get("quarantined"))
        is_busy = studio_id in busy
        machine_busy = machine in busy_machines
        machine_release = _machine_release_evidence(managed_release, machine)
        component_release = (
            (machine_release.get("components") or {}).get(studio.get("modality"))
            if machine_release else None
        )
        release_reason = _managed_release_reason(
            managed_release, machine_release, studio.get("modality"),
        )
        ready = bool(
            online and not status.get("health_recovering")
            and not drained and not quarantined
        )
        worker = {
            "studio_type": studio.get("modality", "unknown"),
            "studio_version": status.get("app_version"),
            "service_id": studio_id,
            "physical_machine_id": machine,
            "hardware_profile": hardware_profiles.machine_hardware_profile(machine),
            "online": online,
            "ready": ready,
            "busy": is_busy,
            "physical_machine_busy": machine_busy,
            "drained": drained,
            "maintenance": maintenance,
            "machine_quarantined": quarantined,
            "managed_release": component_release,
            "last_seen_at": (
                _rfc3339(status["last_seen"]) if isinstance(status.get("last_seen"), (int, float))
                else None
            ),
            "available_capacity": {
                "slots": int(ready and not machine_busy),
                "slots_total": 1,
                "shared_by_physical_machine": True,
            },
        }
        worker["models"] = [
            _model_capability(
                model, studio, worker,
                managed_release_reason=release_reason,
            )
            for model in models_by_studio.get(studio_id, [])
        ]
        if not any(model["availability"]["available_now"] for model in worker["models"]):
            worker["available_capacity"]["slots"] = 0
        worker["supported_operations"] = sorted({
            model["operation"] for model in worker["models"]
        })
        workers.append(worker)

    machines = []
    for machine in sorted({row.get("machine", "local") for row in monitor.registry}):
        machine_workers = [row for row in workers if row["physical_machine_id"] == machine]
        peer_online, host = _machine_host(machine)
        online = peer_online or any(row["online"] for row in machine_workers)
        machine_release = _machine_release_evidence(managed_release, machine)
        machines.append({
            "physical_machine_id": machine,
            "hardware_profile": hardware_profiles.machine_hardware_profile(machine),
            "online": online,
            "enabled": machine_enabled(machine),
            "drained": bool(machine_workers) and all(row["drained"] for row in machine_workers),
            "maintenance": any(row["maintenance"] for row in machine_workers),
            "managed_release": machine_release,
            "available_capacity": {
                "worker_slots": int(any(
                    row["available_capacity"]["slots"] for row in machine_workers
                )),
                "worker_slots_total": 1,
                "available_memory_gb": (host or {}).get("available_gb"),
            },
        })

    by_operation: dict[str, dict] = {}
    for worker in workers:
        for operation in worker["supported_operations"]:
            row = by_operation.setdefault(operation, {
                "workers_total": 0, "workers_online": 0,
                "workers_ready": 0, "available_worker_slots": 0,
            })
            row["workers_total"] += 1
            row["workers_online"] += int(worker["online"])
            row["workers_ready"] += int(worker["ready"])
            row["available_worker_slots"] += int(
                worker["available_capacity"]["slots"] > 0
                and any(model["operation"] == operation
                        and model["availability"]["available_now"]
                        for model in worker["models"])
            )

    controller_drained = (
        settings.get("role") == "agent"
        or not workers
        or all(worker["drained"] for worker in workers)
    )
    model_supply = _model_supply(workers)
    capacity_evidence = _capacity_evidence(model_supply)
    for operation, totals in capacity_evidence["by_operation"].items():
        by_operation.setdefault(operation, {}).update(totals)
    controller_release = None
    if isinstance(managed_release, dict) and managed_release.get("desired"):
        controller_release = managed_release.get("controller") or _missing_release_component(
            managed_release, "hub", state="checking",
        )
    return {
        "schema": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "observed_at": _rfc3339(observed),
        "site_id": settings["site_id"],
        "controller": {
            "controller_id": settings["controller_id"],
            "role": settings["role"],
            "studiohub_version": app_version,
            "online": True,
            "ready": bool(readiness.get("ready") and not controller_drained),
            "drained": controller_drained,
            "managed_release": controller_release,
        },
        "managed_release": managed_release,
        "authority": {
            "global": "genstudio",
            "site_local_scheduler": "sqlite",
            "global_job_claiming": False,
            "postgresql": "optional_shadow_evidence_only",
        },
        "capacity": {
            "queue_depth": base_capacity.get("queue_depth", 0),
            "available_physical_machine_slots": capacity_evidence[
                "available_physical_machine_slots"
            ],
            "eligible_physical_machine_slots_total": capacity_evidence[
                "eligible_physical_machine_slots_total"
            ],
            "eligible_worker_service_slots_total": capacity_evidence[
                "eligible_worker_service_slots_total"
            ],
            "eligible_worker_services": sum(
                worker["available_capacity"]["slots"] for worker in workers
            ),
            "shared_physical_machine_slots": True,
            "by_operation": by_operation,
        },
        "model_supply": model_supply,
        "machines": machines,
        "workers": workers,
    }
