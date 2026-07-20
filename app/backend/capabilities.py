"""Private, read-only GenStudio capability contract for one Studio Hub site.

The snapshot composes existing monitor, registry, catalog, hardware, resource,
and scheduler state. It never reads customer jobs or emits prompts, generated
content, credentials, local cache paths, or global ownership identifiers.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone

from . import broker, chat_jobs, hardware_profiles, peers, transcription_jobs
from .monitor import is_cloud_lane
from .registry import machine_enabled, studio_enabled
from .resources import host_stats

SCHEMA_NAME = "studiohub.site-capabilities"
SCHEMA_VERSION = 1

OPERATION_BY_MODALITY = {
    "image": "image.generation",
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


def _voice_modes(model: dict, cloud: bool) -> list[str]:
    capabilities = set(_string_list(model.get("capabilities")))
    repo = str(model.get("repo") or "").lower()
    modes = []
    if cloud:
        modes.append("provider_voice_id")
    if "voice-cloning" in capabilities:
        modes.append("reference_audio_clone")
    if "voice-design" in capabilities or "voicedesign" in repo:
        modes.append("voice_design")
    if "tts" in capabilities and not modes:
        modes.append("preset_voice")
    return modes


def _controls(model: dict, modality: str, cloud: bool) -> dict:
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
        controls["voice_modes"] = _voice_modes(model, cloud)
    return controls


def _provider_health(monitor, studio: dict, provider: str | None) -> bool | None:
    if not provider:
        return None
    machine = studio.get("machine", "local")
    if machine == "local":
        health = monitor.provider_health(studio["id"])
    else:
        peer = peers.cached(machine) or {}
        service = ((peer.get("studios") or {}).get(studio.get("modality")) or {})
        health = service.get("cloud_providers") or {}
    if health.get("stale"):
        return False
    row = next((item for item in (health.get("providers") or [])
                if item.get("key") == provider), None)
    return bool(row and row.get("enabled") and row.get("live"))


def _model_capability(model: dict, studio: dict, worker: dict, monitor) -> dict:
    modality = str(model.get("hub_modality") or studio.get("modality") or "unknown")
    operation = OPERATION_BY_MODALITY.get(modality, f"{modality}.operation")
    repo = str(model.get("repo") or model.get("model_id") or "unknown")[:500]
    cloud = (
        is_cloud_lane(model.get("is_cloud"), modality)
        or model.get("kind") == "cloud"
        or repo.startswith("provider:")
    )
    provider = str(model.get("provider") or model.get("cloud_provider") or "").strip() or None
    if provider is None and repo.startswith("provider:"):
        provider = repo.split(":", 2)[1] or None
    provider_ready = None
    if cloud:
        explicit = [model.get(key) for key in ("cloud_credentials_ok", "key_set")
                    if key in model]
        provider_ready = all(bool(value) for value in explicit) if explicit else None
        if modality == "voice":
            reported = _provider_health(monitor, studio, provider)
            provider_ready = reported if reported is not None else provider_ready

    installed = None if cloud else bool(model.get("hub_cached"))
    runtime_compatible = model.get("runtime_compatible") is not False
    subsystem_ready = model.get("hub_ready") is not False
    model_ready = runtime_compatible and subsystem_ready and (
        provider_ready is True if cloud else installed is True
    )
    available_now = bool(worker["available_capacity"]["slots"] and model_ready)
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
    elif not runtime_compatible:
        reason = "runtime_incompatible"
    elif not subsystem_ready:
        reason = "subsystem_unavailable"
    elif cloud and provider_ready is not True:
        reason = "provider_unavailable_or_unverified"
    elif not cloud and installed is not True:
        reason = "model_not_installed"
    else:
        reason = None

    revision, revision_source, revision_status = _immutable_runtime_revision(model)
    internal_id = (
        model.get("model_id") or model.get("cloud_model_id")
        or model.get("provider_model") or model.get("repo") or "unknown"
    )
    return {
        "operation": operation,
        "internal_model_id": str(internal_id)[:500],
        "runtime_revision": revision,
        "revision_source": revision_source,
        "revision_status": revision_status,
        "provider": provider or ("local" if not cloud else None),
        "execution_lane": "cloud" if cloud else "local",
        "input_limits": _selected(model, _INPUT_LIMIT_FIELDS),
        "output_limits": _selected(model, _OUTPUT_LIMIT_FIELDS),
        "controls": _controls(model, modality, cloud),
        "availability": {
            "supported": True,
            "installed": installed,
            "runtime_compatible": runtime_compatible,
            "revision_pinning_ready": revision is not None,
            "subsystem_ready": subsystem_ready,
            "provider_ready": provider_ready,
            "available_now": available_now,
            "reason": reason,
        },
    }


def _machine_host(machine: str) -> tuple[bool, dict | None]:
    if machine == "local":
        return True, host_stats()
    peer = peers.cached(machine) or {}
    host = peer.get("host")
    return bool(peer.get("reachable") and isinstance(host, dict)), (
        host if isinstance(host, dict) else None
    )


async def build_snapshot(monitor, *, app_version: str, settings: dict,
                         readiness: dict, base_capacity: dict) -> dict:
    """Build schema v1 without mutating or refreshing live worker state."""
    observed = time.time()
    aggregate = await monitor.aggregate_catalog(force=False)
    models_by_studio: dict[str, list[dict]] = {}
    for model in aggregate.get("models") or []:
        studio_id = model.get("hub_studio")
        if studio_id:
            models_by_studio.setdefault(str(studio_id), []).append(model)

    busy = set(broker.busy_studios()) | set(chat_jobs.busy_studios) \
        | set(transcription_jobs.busy_studios)
    busy_machines = broker.busy_machines()
    protections = broker.machine_protection_snapshot()
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
            _model_capability(model, studio, worker, monitor)
            for model in models_by_studio.get(studio_id, [])
        ]
        if not any(model["availability"]["available_now"] for model in worker["models"]):
            worker["available_capacity"]["slots"] = 0
        worker["supported_operations"] = sorted({
            model["operation"] for model in worker["models"]
        } or {OPERATION_BY_MODALITY.get(
            studio.get("modality"), f"{studio.get('modality', 'unknown')}.operation")})
        workers.append(worker)

    machines = []
    for machine in sorted({row.get("machine", "local") for row in monitor.registry}):
        machine_workers = [row for row in workers if row["physical_machine_id"] == machine]
        peer_online, host = _machine_host(machine)
        online = peer_online or any(row["online"] for row in machine_workers)
        machines.append({
            "physical_machine_id": machine,
            "hardware_profile": hardware_profiles.machine_hardware_profile(machine),
            "online": online,
            "enabled": machine_enabled(machine),
            "drained": bool(machine_workers) and all(row["drained"] for row in machine_workers),
            "maintenance": any(row["maintenance"] for row in machine_workers),
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
    available_machine_slots = sum(
        machine["available_capacity"]["worker_slots"] for machine in machines
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
        },
        "authority": {
            "global": "genstudio",
            "site_local_scheduler": "sqlite",
            "global_job_claiming": False,
            "postgresql": "optional_shadow_evidence_only",
        },
        "capacity": {
            "queue_depth": base_capacity.get("queue_depth", 0),
            "available_physical_machine_slots": available_machine_slots,
            "eligible_worker_services": sum(
                worker["available_capacity"]["slots"] for worker in workers
            ),
            "shared_physical_machine_slots": True,
            "by_operation": by_operation,
        },
        "machines": machines,
        "workers": workers,
    }
