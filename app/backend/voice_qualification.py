"""Controller-owned, deliberately narrow Voice Studio qualification attempts.

This is not a customer queue and cannot approve or expose a model.  It is an
owner-operated evidence collector for an explicit qualification set.  Every
attempt is written to the local Hub SQLite ledger before a remote worker is
contacted.  A lost submit, poll, or cancellation response is *uncertain* and
is never automatically resubmitted.

Remote work travels through :func:`peers.studio_request`, which routes a
registered remote Studio through its own authenticated Studio Hub. A
controller's local Voice Studio is default-denied, but can be explicitly
allowed for a non-brain controller after fencing its physical machine ID.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from . import broker, control_plane, execution_assets, ledger, peers
from .peers import studio_request
from .resources import host_stats


MIN_VOICE_STUDIO_VERSION = (1, 27, 0)
PEER_MEMORY_MAX_AGE_S = 30.0
CLIENT_REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{8,160}")
MACHINE_ID = re.compile(r"[A-Za-z0-9._:-]{1,120}")
IMMUTABLE_REVISION = re.compile(r"^(?:sha256:)?[0-9a-fA-F]{40,64}$")
ALLOWED_MODELS = frozenset({
    "mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-8bit",
    "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-8bit",
    "mlx-community/chatterbox-4bit",
    "mlx-community/VoxCPM2-4bit",
    "mlx-community/VibeVoice-Realtime-0.5B-4bit",
    "mlx-community/OmniVoice-bfloat16",
})
ALLOWED_CASES = frozenset({"short", "long_form", "cancellation"})
MODEL_OPERATIONS = {
    "mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-8bit": frozenset({"preset_tts"}),
    "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-8bit": frozenset({"voice_clone"}),
    "mlx-community/chatterbox-4bit": frozenset({"voice_clone"}),
    "mlx-community/VoxCPM2-4bit": frozenset({
        "auto_tts", "voice_clone", "voice_design", "voice_clone_with_design",
    }),
    "mlx-community/VibeVoice-Realtime-0.5B-4bit": frozenset({"preset_tts"}),
    "mlx-community/OmniVoice-bfloat16": frozenset({
        "voice_clone", "voice_design", "voice_clone_with_design",
    }),
}
DEFAULT_OPERATIONS = {
    model: next(iter(operations)) if len(operations) == 1 else None
    for model, operations in MODEL_OPERATIONS.items()
}
REFERENCE_OPERATIONS = frozenset({"voice_clone", "voice_clone_with_design"})
DESIGN_OPERATIONS = frozenset({"voice_design", "voice_clone_with_design"})
LONG_FORM_READY_MODELS = frozenset({
    "mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-8bit",
    "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-8bit",
    "mlx-community/chatterbox-4bit",
    "mlx-community/VoxCPM2-4bit",
    "mlx-community/VibeVoice-Realtime-0.5B-4bit",
})
WAVE_2_MODELS = frozenset({
    "mlx-community/VoxCPM2-4bit",
    "mlx-community/VibeVoice-Realtime-0.5B-4bit",
    "mlx-community/OmniVoice-bfloat16",
})
TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled", "uncertain"})
QWEN_CUSTOMVOICE_SPEAKERS = frozenset({
    "Ryan", "Aiden", "Serena", "Vivian", "Uncle_Fu", "Dylan", "Eric",
    "Ono_Anna", "Sohee",
})
CHATTERBOX_LANGUAGE_CODES = frozenset({
    "ar", "da", "de", "el", "en", "es", "fi", "fr", "he", "hi",
    "it", "ja", "ko", "ms", "nl", "no", "pl", "pt", "ru", "sv",
    "sw", "tr", "zh",
})
VIBEVOICE_VOICE_IDS = frozenset({
    "en-Carter_man", "en-Davis_man", "en-Emma_woman", "en-Frank_man",
    "en-Grace_woman", "en-Mike_man", "in-Samuel_man",
    "de-Spk0_man", "de-Spk1_woman", "fr-Spk0_man", "fr-Spk1_woman",
    "it-Spk0_woman", "it-Spk1_man", "jp-Spk0_man", "jp-Spk1_woman",
    "kr-Spk0_woman", "kr-Spk1_man", "nl-Spk0_man", "nl-Spk1_woman",
    "pl-Spk0_man", "pl-Spk1_woman", "pt-Spk0_woman", "pt-Spk1_man",
    "sp-Spk0_woman", "sp-Spk1_man",
})
_CREATION_LOCK = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS voice_qualification_attempts (
  id TEXT PRIMARY KEY,
  client_request_id TEXT NOT NULL UNIQUE,
  request_fingerprint TEXT NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  state TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_voice_qualification_updated
  ON voice_qualification_attempts(updated_at DESC);
"""

_RESOURCE_USAGE_FIELDS = {
    "sampling": {"interval_seconds", "samples", "started_at", "finished_at"},
    "host": {
        "total_gb", "available_gb_start", "minimum_available_gb",
        "available_gb_end", "maximum_used_gb", "maximum_used_percent",
        "pressure_level_start", "peak_pressure_level", "peak_pressure_raw",
        "pressure_level_end", "swap_used_gb_start", "maximum_swap_used_gb",
        "swap_used_gb_end", "swap_used_delta_gb", "swap_in_delta_bytes",
        "swap_out_delta_bytes",
    },
    "worker": {"rss_gb_start", "peak_rss_gb", "rss_gb_end", "peak_process_count"},
    "mlx": {"supported", "active_gb_start", "peak_active_gb", "peak_cache_gb",
            "reported_peak_gb", "active_gb_end", "cache_gb_end"},
    "outcome": {"state", "memory_failure", "restart_scheduled", "model_retained"},
}


class QualificationError(ValueError):
    """A sanitized local admission or state error."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True)
class Preflight:
    studio: dict[str, Any]
    model: dict[str, Any]
    target: dict[str, Any]
    reference: dict[str, Any] | None = None
    reference_audio: bytes | None = None


def _conn() -> sqlite3.Connection:
    connection = sqlite3.connect(ledger.DB_FILE, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.executescript(_SCHEMA)
    return connection


def _save(attempt: dict[str, Any]) -> None:
    now = time.time()
    attempt["updated_at"] = now
    with _conn() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO voice_qualification_attempts "
            "(id, client_request_id, request_fingerprint, created_at, updated_at, state, payload) "
            "VALUES (?,?,?,?,?,?,?)",
            (attempt["id"], attempt["client_request_id"],
             attempt["request_fingerprint"], attempt["created_at"], now,
             attempt["state"], json.dumps(attempt, separators=(",", ":"))),
        )


def get(attempt_id: str) -> dict[str, Any] | None:
    with _conn() as connection:
        row = connection.execute(
            "SELECT payload FROM voice_qualification_attempts WHERE id = ?", (attempt_id,),
        ).fetchone()
    return json.loads(row["payload"]) if row else None


def get_by_client_request_id(client_request_id: str) -> dict[str, Any] | None:
    with _conn() as connection:
        row = connection.execute(
            "SELECT payload FROM voice_qualification_attempts WHERE client_request_id = ?",
            (client_request_id,),
        ).fetchone()
    return json.loads(row["payload"]) if row else None


def list_attempts(limit: int = 100) -> list[dict[str, Any]]:
    with _conn() as connection:
        rows = connection.execute(
            "SELECT payload FROM voice_qualification_attempts "
            "ORDER BY updated_at DESC LIMIT ?", (max(1, min(int(limit), 500)),),
        ).fetchall()
    return [json.loads(row["payload"]) for row in rows]


def _version(value: object) -> tuple[int, int, int] | None:
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", str(value or ""))
    return tuple(map(int, match.groups())) if match else None


def _immutable_revision(entry: dict[str, Any]) -> str | None:
    for source in (entry, entry.get("cache") if isinstance(entry.get("cache"), dict) else {}):
        for field in ("runtime_revision", "model_revision", "snapshot_revision", "commit_sha", "revision"):
            value = str(source.get(field) or "").strip()
            if IMMUTABLE_REVISION.fullmatch(value):
                return value.removeprefix("sha256:").lower()
    return None


def _safe_number(value: object) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if numeric >= 0 else None


def _peer_age(machine: str) -> float | None:
    # Peer snapshots are owned by peers.py.  Keep its small cache private while
    # enforcing freshness here; this feature never opens a direct worker URL.
    entry = peers._cache.get(machine)  # noqa: SLF001 - same controller boundary
    if not entry:
        return None
    return max(0.0, time.time() - float(entry[0]))


def _catalog_entry(monitor, studio_id: str, model_id: str) -> dict[str, Any] | None:
    for item in (monitor.cached_aggregate_catalog().get("models") or []):
        if item.get("hub_studio") == studio_id and item.get("repo") == model_id:
            return item
    return None


def _tier_matches(total_gb: float, tier_gb: int) -> bool:
    """Match marketed unified-memory tiers to decimal psutil totals.

    Apple 8/16/24 GiB machines report roughly 8.6/17.2/25.8 decimal GB, so
    rounding the latter would incorrectly turn a genuine 8 GB machine into 9.
    """
    return float(tier_gb) <= total_gb <= float(tier_gb) * 1.10


def _active_attempt_on_machine(machine: str) -> bool:
    return any(
        item.get("state") in {"prepared", "submitting", "running", "cancel_requested"}
        and (item.get("target") or {}).get("machine_id") == machine
        for item in list_attempts(500)
    )


def _excluded_machine_ids(request: dict[str, Any]) -> tuple[str, ...]:
    """Validate and canonicalize the physical machines an owner has fenced."""
    raw = request.get("excluded_machine_ids") or []
    if not isinstance(raw, list) or len(raw) > 100:
        raise QualificationError(
            "EXCLUDED_MACHINE_IDS_INVALID",
            "excluded_machine_ids must be a list of at most 100 physical machine IDs.",
        )
    values = []
    for item in raw:
        value = str(item or "").strip()
        if not MACHINE_ID.fullmatch(value):
            raise QualificationError(
                "EXCLUDED_MACHINE_IDS_INVALID",
                "Every excluded machine must use a safe physical machine ID.",
            )
        values.append(value)
    return tuple(sorted(set(values)))


def _qwen_speaker_ids(raw: object) -> frozenset[str] | None:
    if not isinstance(raw, list):
        return None
    speaker_ids = set()
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            return None
        speaker_ids.add(item["id"])
    return frozenset(speaker_ids)


def _voice_ids(raw: object) -> frozenset[str] | None:
    if not isinstance(raw, list):
        return None
    values = set()
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            return None
        values.add(item["id"])
    return frozenset(values)


def _operation(request: dict[str, Any], model_id: str) -> str:
    value = str(request.get("operation") or "").strip()
    if not value:
        value = DEFAULT_OPERATIONS.get(model_id) or ""
    if value not in MODEL_OPERATIONS.get(model_id, frozenset()):
        raise QualificationError(
            "QUALIFICATION_OPERATION_INVALID",
            "Choose an operation explicitly supported by this qualification model.",
        )
    return value


async def _validate_model_controls(
    *, studio: dict[str, Any], model_id: str, entry: dict[str, Any],
    operation: str, params: dict[str, Any], client: httpx.AsyncClient,
) -> None:
    """Verify exact, worker-reported controls before any job is admitted."""
    if model_id.endswith("CustomVoice-8bit"):
        requested = str(params.get("preset_speaker") or "").strip()
        if not requested:
            raise QualificationError("PRESET_SPEAKER_REQUIRED", "Qwen CustomVoice qualification requires a preset speaker.")
        try:
            url, headers = studio_request(studio, "/api/generate/availability")
            response = await client.get(url, headers=headers, timeout=30.0)
            availability = response.json() if response.status_code < 400 else None
        except (httpx.HTTPError, ValueError, AttributeError):
            availability = None
        speaker_ids = _qwen_speaker_ids(
            availability.get("qwen3_preset_speakers") if isinstance(availability, dict) else None
        )
        if speaker_ids != QWEN_CUSTOMVOICE_SPEAKERS:
            raise QualificationError(
                "QWEN_CUSTOMVOICE_ROSTER_MISMATCH",
                "The worker did not prove the exact Qwen CustomVoice nine-speaker roster.",
            )
        if requested not in QWEN_CUSTOMVOICE_SPEAKERS:
            raise QualificationError("PRESET_SPEAKER_INVALID", "Choose one of the worker's verified Qwen preset speakers.")
        return

    if model_id == "mlx-community/chatterbox-4bit":
        support = entry.get("language_support")
        codes = support.get("codes") if isinstance(support, dict) else None
        exact = (
            isinstance(codes, list)
            and frozenset(str(code) for code in codes) == CHATTERBOX_LANGUAGE_CODES
            and support.get("enumeration_status") == "exact"
            and support.get("input_selection") == "required"
            and support.get("runtime_enforced") is True
        )
        if not exact:
            raise QualificationError(
                "CHATTERBOX_LANGUAGE_ROSTER_MISMATCH",
                "The worker did not prove Chatterbox's exact 23-language contract.",
            )
        language = str(params.get("language") or "").strip().lower()
        if language not in CHATTERBOX_LANGUAGE_CODES:
            raise QualificationError("CHATTERBOX_LANGUAGE_REQUIRED", "Choose a verified Chatterbox language code.")

    if model_id == "mlx-community/VibeVoice-Realtime-0.5B-4bit":
        try:
            url, headers = studio_request(studio, "/api/generate/availability")
            response = await client.get(url, headers=headers, timeout=30.0)
            availability = response.json() if response.status_code < 400 else None
        except (httpx.HTTPError, ValueError, AttributeError):
            availability = None
        reported = _voice_ids(
            availability.get("vibevoice_voices") if isinstance(availability, dict) else None
        )
        if reported != VIBEVOICE_VOICE_IDS:
            raise QualificationError(
                "VIBEVOICE_ROSTER_MISMATCH",
                "The worker did not prove the exact 25-voice VibeVoice checkpoint roster.",
            )
        requested = str(params.get("voice") or "").strip()
        if requested not in VIBEVOICE_VOICE_IDS:
            raise QualificationError(
                "VIBEVOICE_PRESET_REQUIRED",
                "Choose one of the worker's verified VibeVoice presets.",
            )

    if operation in DESIGN_OPERATIONS:
        prompt = str(params.get("voice_design_prompt") or params.get("instruct") or "").strip()
        if not prompt:
            raise QualificationError(
                "VOICE_DESIGN_PROMPT_REQUIRED",
                "This qualification operation requires a voice-design prompt.",
            )


async def _preflight(monitor, request: dict[str, Any], client: httpx.AsyncClient) -> Preflight:
    settings = control_plane.public_settings()
    if settings.get("role") != "controller":
        raise QualificationError("CONTROLLER_REQUIRED", "Run qualification from a controller Hub.")
    model_id = str(request.get("model") or "")
    if model_id not in ALLOWED_MODELS:
        raise QualificationError("MODEL_NOT_IN_QUALIFICATION_SET", "This model is not in the approved qualification set.")
    operation = _operation(request, model_id)
    case_type = str(request.get("case_type") or "")
    if case_type not in ALLOWED_CASES:
        raise QualificationError("QUALIFICATION_CASE_INVALID", "Use short, long_form, or cancellation.")
    text = str(request.get("text") or "")
    if not text.strip() or len(text) > 40_000:
        raise QualificationError("QUALIFICATION_TEXT_INVALID", "Qualification text must contain 1 to 40,000 characters.")
    if case_type == "long_form" and len(text) != 40_000:
        raise QualificationError("LONG_FORM_TEXT_REQUIRED", "Long-form qualification requires exactly 40,000 characters.")
    if case_type == "long_form" and model_id not in LONG_FORM_READY_MODELS:
        raise QualificationError(
            "LONG_FORM_ADAPTER_NOT_READY",
            "This model must pass short-form stability before its adapter-managed long-form test is enabled.",
        )

    studio_id = str(request.get("target_studio_id") or "")
    studio = next((candidate for candidate in monitor.registry if candidate.get("id") == studio_id), None)
    if not studio or studio.get("modality") != "voice":
        raise QualificationError("VOICE_WORKER_NOT_FOUND", "Choose a registered Voice Studio worker.")
    registry_machine = str(studio.get("machine") or "local")
    controller_local = registry_machine == "local"
    if controller_local and request.get("allow_controller_local") is not True:
        raise QualificationError(
            "LOCAL_TARGET_FORBIDDEN",
            "Controller-local qualification requires explicit allow_controller_local approval.",
        )
    physical_machine = (
        str(settings.get("controller_id") or "").strip()
        if controller_local else registry_machine
    )
    if not MACHINE_ID.fullmatch(physical_machine):
        raise QualificationError("PHYSICAL_MACHINE_ID_INVALID", "The selected worker has no safe physical machine identity.")
    excluded_machine_ids = _excluded_machine_ids(request)
    if physical_machine in excluded_machine_ids:
        raise QualificationError("TARGET_MACHINE_EXCLUDED", "The selected physical machine is explicitly excluded from qualification.")
    if monitor.status.get(studio_id, {}).get("status") != "up":
        raise QualificationError("WORKER_NOT_IDLE", "The selected Voice Studio is not online and idle.")
    if studio_id in broker.busy_studios() or registry_machine in broker.busy_machines():
        raise QualificationError("WORKER_NOT_IDLE", "The selected machine has active work.")
    if _active_attempt_on_machine(physical_machine):
        raise QualificationError("WORKER_NOT_IDLE", "The selected physical machine already has a qualification attempt in progress.")
    if broker.machine_is_quarantined(registry_machine):
        raise QualificationError("MACHINE_QUARANTINED", "The selected machine is temporarily quarantined.")

    version = _version(monitor.status.get(studio_id, {}).get("app_version") or
                       (monitor.status.get(studio_id, {}).get("health") or {}).get("app_version"))
    if version is None or version < MIN_VOICE_STUDIO_VERSION:
        raise QualificationError("VOICE_STUDIO_VERSION_TOO_OLD", "Voice Studio 1.27.0 or newer is required for qualification.")
    if model_id in WAVE_2_MODELS and version < (1, 27, 9):
        raise QualificationError(
            "VOICE_STUDIO_VERSION_TOO_OLD",
            "Voice Studio 1.27.9 or newer is required for Wave 2 qualification.",
        )

    entry = _catalog_entry(monitor, studio_id, model_id)
    if not entry or not entry.get("hub_cached"):
        raise QualificationError("MODEL_NOT_CACHED", "The selected model is not fully cached on this remote worker.")
    if entry.get("hub_catalog_stale") or entry.get("runtime_compatible") is False or entry.get("hub_ready") is False:
        raise QualificationError("MODEL_RUNTIME_NOT_READY", "The selected model is not runtime-ready on this worker.")
    revision = _immutable_revision(entry)
    if revision is None:
        raise QualificationError("MODEL_REVISION_MISSING", "The worker did not report an immutable model revision.")

    if controller_local:
        host = host_stats()
        age = 0.0
    else:
        snapshot = peers.cached(registry_machine) or {}
        age = _peer_age(registry_machine)
        host = snapshot.get("host") if isinstance(snapshot.get("host"), dict) else None
        if not snapshot.get("reachable") or not snapshot.get("auth") or host is None or age is None or age > PEER_MEMORY_MAX_AGE_S:
            raise QualificationError("REMOTE_MEMORY_STALE", "The remote machine lacks a fresh authenticated memory snapshot.")
    total_gb, available_gb = _safe_number(host.get("total_gb")), _safe_number(host.get("available_gb"))
    requested_tier = int(request.get("machine_tier_gb") or 0)
    if requested_tier not in {8, 16, 24} or total_gb is None or not _tier_matches(total_gb, requested_tier):
        raise QualificationError("MACHINE_TIER_MISMATCH", "The selected worker does not match the requested 8, 16, or 24 GB tier.")
    min_total = _safe_number(entry.get("min_unified_memory_gb")) or 0.0
    min_free = _safe_number(entry.get("min_free_memory_gb")) or 2.0
    if total_gb < min_total or available_gb is None or available_gb < min_free:
        raise QualificationError("INSUFFICIENT_LIVE_MEMORY", "The selected worker does not meet the model's live memory admission floor.")

    params = request.get("params") if isinstance(request.get("params"), dict) else {}
    reference_asset_id = str(request.get("voice_reference_asset_id") or "").strip()
    await _validate_model_controls(
        studio=studio, model_id=model_id, entry=entry, operation=operation,
        params=params, client=client,
    )
    reference = None
    reference_audio = None
    if operation in REFERENCE_OPERATIONS and not reference_asset_id:
        raise QualificationError("REFERENCE_ASSET_REQUIRED", "Clone qualification requires a staged private voice reference asset.")
    if reference_asset_id:
        try:
            reference, reference_path = execution_assets.resolve_voice_reference(reference_asset_id)
            reference_audio = reference_path.read_bytes()
        except (OSError, execution_assets.ExecutionAssetError) as exc:
            raise QualificationError(
                "REFERENCE_ASSET_UNAVAILABLE",
                "The private voice reference is unavailable. Upload it again before qualification.",
            ) from exc
        if operation in REFERENCE_OPERATIONS and not str(reference.get("transcript") or "").strip():
            raise QualificationError(
                "REFERENCE_TRANSCRIPT_REQUIRED",
                "Clone qualification requires the exact transcript for its private reference clip.",
            )

    # The Hub's own queue is not the only possible source of activity. Verify
    # the selected sibling has no live generation jobs using the same
    # authenticated Hub-to-worker proxy used for submit/poll/cancel.
    try:
        url, headers = studio_request(studio, "/api/generate/jobs")
        response = await client.get(url, headers=headers, timeout=30.0)
        jobs = response.json().get("jobs") if response.status_code < 400 else None
    except (httpx.HTTPError, ValueError, AttributeError):
        jobs = None
    if not isinstance(jobs, list):
        raise QualificationError("WORKER_IDLE_UNKNOWN", "The selected Voice Studio did not provide a safe idle-job snapshot.")
    if any(str(item.get("state") or "").lower() in {"queued", "running"}
           for item in jobs if isinstance(item, dict)):
        raise QualificationError("WORKER_NOT_IDLE", "The selected Voice Studio has active generation work.")

    return Preflight(studio=studio, model=entry, target={
        "site_id": settings["site_id"],
        "studio_id": studio_id,
        "machine_id": physical_machine,
        "registry_machine_id": registry_machine,
        "execution_path": "controller_local" if controller_local else "remote_agent",
        "excluded_machine_ids": list(excluded_machine_ids),
        "machine_tier_gb": requested_tier,
        "observed_total_memory_gb": total_gb,
        "observed_available_memory_gb": available_gb,
        "memory_snapshot_age_seconds": round(age, 3),
        "minimum_total_memory_gb": min_total,
        "minimum_free_memory_gb": min_free,
        "voice_studio_version": ".".join(map(str, version)),
        "model_id": model_id,
        "operation": operation,
        "runtime_revision": revision,
    }, reference=reference, reference_audio=reference_audio)


def _fingerprint(request: dict[str, Any]) -> str:
    # The reference id and exact request are intentional idempotency material;
    # the original audio bytes and worker endpoints are never persisted here.
    safe = {
        "target_studio_id": request.get("target_studio_id"),
        "machine_tier_gb": request.get("machine_tier_gb"), "model": request.get("model"),
        "operation": request.get("operation"),
        "case_type": request.get("case_type"), "text": request.get("text"),
        "params": request.get("params") or {},
        "voice_reference_asset_id": request.get("voice_reference_asset_id"),
        "allow_controller_local": request.get("allow_controller_local") is True,
        "excluded_machine_ids": list(_excluded_machine_ids(request)),
    }
    return hashlib.sha256(json.dumps(safe, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _public(attempt: dict[str, Any]) -> dict[str, Any]:
    """Return only operational facts.  No text, endpoints, credentials, or raw worker errors."""
    return {
        key: attempt.get(key) for key in (
            "id", "client_request_id", "created_at", "updated_at", "state", "case_type",
            "model", "operation", "target", "worker_job_id", "progress", "cancel_requested_at",
            "terminal_evidence", "review_reason",
        )
    }


def _sanitize_resource_usage(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or raw.get("schema") != "voicestudio.resource-telemetry" or raw.get("schema_version") != 1:
        return None
    clean: dict[str, Any] = {"schema": raw["schema"], "schema_version": 1}
    for section, allowed in _RESOURCE_USAGE_FIELDS.items():
        values = raw.get(section)
        if isinstance(values, dict):
            clean[section] = {key: value for key, value in values.items()
                              if key in allowed and (value is None or isinstance(value, (str, int, float, bool)))}
    return clean


def _terminal_evidence(job: dict[str, Any]) -> dict[str, Any]:
    artifact = {key: job.get(key) for key in (
        "integration_name", "integration_version", "internal_model_id",
        "model_revision", "runtime_revision", "voice_library_id", "voice_revision",
        "reference_audio_sha256", "reference_source_sha256",
        "reference_preparation_revision", "reference_duration_s",
        "long_form_strategy", "chunk_total", "runtime_s", "media_type", "format",
        "bytes", "sha256", "audio_duration_s", "audio_duration_ms",
        "sample_rate_hz", "channels",
    ) if isinstance(job.get(key), (str, int, float, bool))}
    evidence: dict[str, Any] = {"artifact": artifact}
    resources = _sanitize_resource_usage(job.get("resource_usage"))
    if resources is not None:
        evidence["resource_usage"] = resources
    return evidence


def _mark_uncertain(attempt: dict[str, Any], code: str) -> dict[str, Any]:
    attempt.update(state="uncertain", review_reason=code)
    _save(attempt)
    return _public(attempt)


async def submit(monitor, request: dict[str, Any], client: httpx.AsyncClient) -> dict[str, Any]:
    client_request_id = str(request.get("client_request_id") or "")
    if not CLIENT_REQUEST_ID.fullmatch(client_request_id):
        raise QualificationError("CLIENT_REQUEST_ID_INVALID", "client_request_id must be 8-160 safe characters.")
    fingerprint = _fingerprint(request)
    with _CREATION_LOCK:
        existing = get_by_client_request_id(client_request_id)
        if existing:
            if existing.get("request_fingerprint") != fingerprint:
                raise QualificationError("CLIENT_REQUEST_ID_CONFLICT", "client_request_id was already used for a different qualification request.")
            return {**_public(existing), "replayed": True}

    preflight = await _preflight(monitor, request, client)
    with _CREATION_LOCK:
        # A concurrent caller may have completed the same preflight while this
        # one waited on the remote idle snapshot.  Reuse it, never double-post.
        existing = get_by_client_request_id(client_request_id)
        if existing:
            if existing.get("request_fingerprint") != fingerprint:
                raise QualificationError("CLIENT_REQUEST_ID_CONFLICT", "client_request_id was already used for a different qualification request.")
            return {**_public(existing), "replayed": True}
        if _active_attempt_on_machine(preflight.target["machine_id"]):
            raise QualificationError("WORKER_NOT_IDLE", "The selected physical machine already has a qualification attempt in progress.")
        attempt = {
            "id": f"vq-{uuid.uuid4().hex[:16]}", "client_request_id": client_request_id,
            "request_fingerprint": fingerprint, "created_at": time.time(), "state": "prepared",
            "case_type": request["case_type"], "model": request["model"],
            "operation": preflight.target["operation"], "target": preflight.target,
            "worker_job_id": None, "progress": None, "terminal_evidence": None,
            "review_reason": None,
        }
        _save(attempt)  # Durable before the remote worker can possibly accept.
        attempt["state"] = "submitting"
        _save(attempt)

    try:
        payload = dict(request.get("params") or {})
        payload.update(repo=request["model"], text=request["text"], client_request_id=client_request_id)
        if preflight.reference is not None and preflight.reference_audio is not None:
            reference = preflight.reference
            if reference.get("transcript"):
                payload["ref_transcript"] = reference["transcript"]
            url, headers = studio_request(preflight.studio, "/api/generate/txt2speech/reference")
            response = await client.post(url, headers=headers, timeout=120.0, data={
                "request_json": json.dumps(payload, separators=(",", ":")),
                "transcript_segments_json": json.dumps(reference.get("transcript_segments") or [], separators=(",", ":")),
                "source_sha256": reference["sha256"], "reference_expires_at": str(reference["expires_at"]),
            }, files={"audio": (f"reference{reference['audio_extension']}", preflight.reference_audio, reference["media_type"])})
        else:
            url, headers = studio_request(preflight.studio, "/api/generate/txt2speech")
            response = await client.post(url, headers=headers, timeout=120.0, json=payload)
    except (TypeError, ValueError):
        attempt.update(state="failed", review_reason="QUALIFICATION_PAYLOAD_INVALID")
        _save(attempt)
        return _public(attempt)
    except httpx.HTTPError:
        broker.mark_external_machine_failure(preflight.studio, "qualification submit transport failed")
        return _mark_uncertain(attempt, "SUBMIT_RESPONSE_UNKNOWN")

    if response.status_code >= 400:
        attempt.update(state="failed", review_reason="WORKER_REJECTED_BEFORE_ACCEPTANCE")
        _save(attempt)
        return _public(attempt)
    try:
        worker_job = response.json().get("job")
        worker_job_id = str(worker_job.get("id") or "") if isinstance(worker_job, dict) else ""
    except (ValueError, AttributeError):
        worker_job_id = ""
    if not worker_job_id:
        return _mark_uncertain(attempt, "SUBMIT_RESPONSE_MALFORMED")
    attempt.update(state="running", worker_job_id=worker_job_id)
    _save(attempt)
    broker.mark_external_machine_success(preflight.studio)
    return _public(attempt)


async def poll(monitor, attempt_id: str, client: httpx.AsyncClient) -> dict[str, Any]:
    attempt = get(attempt_id)
    if attempt is None:
        raise QualificationError("ATTEMPT_NOT_FOUND", "Unknown qualification attempt.")
    if attempt.get("state") in TERMINAL_STATES:
        return _public(attempt)
    studio = next((item for item in monitor.registry if item.get("id") == attempt.get("target", {}).get("studio_id")), None)
    if not studio or not attempt.get("worker_job_id"):
        return _mark_uncertain(attempt, "WORKER_IDENTITY_UNAVAILABLE")
    try:
        url, headers = studio_request(studio, f"/api/generate/jobs/{attempt['worker_job_id']}")
        response = await client.get(url, headers=headers, timeout=30.0)
    except httpx.HTTPError:
        broker.mark_external_machine_failure(studio, "qualification poll transport failed")
        return _mark_uncertain(attempt, "POLL_RESPONSE_UNKNOWN")
    if response.status_code >= 400:
        return _mark_uncertain(attempt, "POLL_RESPONSE_UNKNOWN")
    try:
        job = response.json().get("job")
    except (ValueError, AttributeError):
        job = None
    if not isinstance(job, dict):
        return _mark_uncertain(attempt, "POLL_RESPONSE_MALFORMED")
    state = str(job.get("state") or "").lower()
    progress = job.get("progress")
    if isinstance(progress, (int, float)):
        attempt["progress"] = max(0.0, min(1.0, float(progress)))
    if state in {"queued", "running"}:
        attempt["state"] = "cancel_requested" if attempt.get("cancel_requested_at") else "running"
        _save(attempt)
        return _public(attempt)
    if state == "done":
        reported_revision = str(job.get("model_revision") or "").removeprefix("sha256:").lower()
        if reported_revision and reported_revision != attempt["target"]["runtime_revision"]:
            return _mark_uncertain(attempt, "MODEL_REVISION_CHANGED")
        attempt.update(state="succeeded", terminal_evidence=_terminal_evidence(job), review_reason=None)
    elif state == "cancelled":
        attempt.update(state="cancelled", terminal_evidence=_terminal_evidence(job), review_reason=None)
    else:
        attempt.update(state="failed", terminal_evidence=_terminal_evidence(job), review_reason="WORKER_TERMINAL_FAILURE")
    _save(attempt)
    broker.mark_external_machine_success(studio)
    return _public(attempt)


async def cancel(monitor, attempt_id: str, client: httpx.AsyncClient) -> dict[str, Any]:
    attempt = get(attempt_id)
    if attempt is None:
        raise QualificationError("ATTEMPT_NOT_FOUND", "Unknown qualification attempt.")
    if attempt.get("state") in TERMINAL_STATES:
        return _public(attempt)
    attempt["cancel_requested_at"] = time.time()
    attempt["state"] = "cancel_requested"
    _save(attempt)  # Intent is durable before a remote cancellation can race completion.
    studio = next((item for item in monitor.registry if item.get("id") == attempt.get("target", {}).get("studio_id")), None)
    if not studio or not attempt.get("worker_job_id"):
        return _mark_uncertain(attempt, "CANCEL_WORKER_IDENTITY_UNAVAILABLE")
    try:
        url, headers = studio_request(studio, f"/api/generate/jobs/{attempt['worker_job_id']}")
        response = await client.delete(url, headers=headers, timeout=30.0)
    except httpx.HTTPError:
        broker.mark_external_machine_failure(studio, "qualification cancellation transport failed")
        return _mark_uncertain(attempt, "CANCEL_RESPONSE_UNKNOWN")
    if response.status_code >= 400:
        return _mark_uncertain(attempt, "CANCEL_RESPONSE_UNKNOWN")
    broker.mark_external_machine_success(studio)
    return _public(attempt)


def reset_for_tests() -> None:
    """No in-memory scheduler exists; tests remove hub.db through conftest."""
    return None
