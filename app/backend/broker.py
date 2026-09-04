"""Job broker + Swarm Batch — pull-based worker pools per modality.

An N-item batch is a work queue. Each UP studio of the right modality is a
worker slot (one concurrent generation each — heavy models on unified
memory). Free workers pull the next queued item, so faster machines naturally
do more and everyone finishes together; a failed item is requeued (max
MAX_TRIES). With one machine today the pool has one worker per modality —
the moment a second machine joins the registry, the same code fans out.

Memory governor: before
dispatching to a local or connected remote studio, the stricter of the
studio's catalog requirement and Hub production policy is checked against the
host's available memory; the item waits rather than OOMing the box. Render's
catalog hint bypasses the model-memory check because it runs local FFmpeg.

Params stay opaque: item params + sharedParams merge over {repo, prompt-field}
and are forwarded verbatim to the studio's own generate endpoint.
"""

import asyncio
import base64
import hashlib
import json
import logging
import math
import re
import threading
import time
import uuid
from pathlib import Path

import httpx

from . import (artifact_metadata, cloud_guard, execution_assets,
               execution_identity, ledger, peers, shared_voices)
from .memory_control import FleetMemoryControl, SUPPORTED_MODALITIES
from .peers import studio_request
from .monitor import is_cached
from .registry import base_url, machine_enabled, studio_enabled
from .resources import host_stats
from . import memory_admission

_MIME_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
             "image/webp": ".webp"}


def _ext(mime: str) -> str:
    return _MIME_EXT.get((mime or "").lower(), ".png")


def _mime_from_path(p: str) -> str:
    s = p.lower()
    if s.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if s.endswith(".webp"):
        return "image/webp"
    return "image/png"


def _studio_target_for_url(url: str) -> tuple[str, dict[str, str]]:
    studio = next((s for s in _monitor().registry if url.startswith(base_url(s))), None)
    return studio_request(studio, url) if studio else (url, {})


def _multipart_fields(body: dict) -> dict:
    """Flatten the JSON param body into the studio's img2img/edit form fields
    (strings; CSV for lora lists). Unknown fields are ignored by the studio."""
    out = {}
    for k in ("repo", "prompt", "negative_prompt", "width", "height", "steps",
              "guidance", "seed", "image_strength", "quantize"):
        v = body.get(k)
        if v is not None:
            out[k] = str(v)
    for k in ("lora_names", "lora_scales"):
        v = body.get(k)
        if isinstance(v, (list, tuple)):
            out[k] = ",".join(str(x) for x in v)
        elif v is not None:
            out[k] = str(v)
    return out


def _video_multipart_fields(body: dict) -> dict:
    """Flatten an image-to-video request for Video Studio's multipart API."""
    out = {}
    for k in ("repo", "mode", "prompt", "negative_prompt", "width", "height",
              "frames", "fps", "steps", "guidance", "seed", "duration",
              "resolution", "aspect_ratio"):
        v = body.get(k)
        if v is not None:
            out[k] = str(v)
    return out


async def _resolve_reference(client: httpx.AsyncClient, ref: dict):
    """Turn a reference_images[] entry into (bytes, mime). Supports inline b64,
    a tailnet url, or an asset_id from the Hub ledger (incl. uploaded refs).
    Raises ValueError for permanent problems."""
    if ref.get("b64"):
        raw = ref["b64"].strip()
        if raw.startswith("data:") and "," in raw:
            raw = raw.split(",", 1)[1]  # strip data URL prefix
        try:
            return base64.b64decode(raw), ref.get("mime", "image/png")
        except Exception as e:
            raise ValueError(f"invalid base64: {e}")
    if ref.get("url"):
        url, headers = _studio_target_for_url(ref["url"])
        r = await client.get(url, headers=headers, timeout=30.0)
        r.raise_for_status()
        return r.content, r.headers.get("content-type", "image/png")
    if ref.get("asset_id"):
        a = ledger.get_asset(ref["asset_id"])
        if not a:
            raise ValueError(f"asset {ref['asset_id']} not found")
        p = a.get("artifact_path")
        if p and Path(p).exists():
            return Path(p).read_bytes(), _mime_from_path(p)
        u = a.get("artifact_url")
        if u:
            url, headers = _studio_target_for_url(u)
            r = await client.get(url, headers=headers, timeout=30.0)
            r.raise_for_status()
            return r.content, r.headers.get("content-type", "image/png")
        raise ValueError("asset has no fetchable bytes")
    raise ValueError("reference needs one of b64 / url / asset_id")

# modality -> (generate endpoint, prompt field name, artifact suffix)
MODALITY = {
    "image": ("/api/generate/txt2img", "prompt", "image"),
    "music": ("/api/generate/txt2music", "prompt", "audio"),
    "voice": ("/api/generate/txt2speech", "text", "audio"),
    "video": ("/api/generate/txt2video", "prompt", "video"),
    # Render Studio accepts an immutable episode recipe and downloads every
    # referenced input before FFmpeg starts. It deliberately remains separate
    # from generative Video Studio.
    "render": ("/api/generate/render", "label", "video"),
}

# Every modality whose artifact is sound. Derived from MODALITY so a future
# audio modality inherits the priority below without a second edit.
AUDIO_MODALITIES = frozenset(
    m for m, spec in MODALITY.items() if spec[2] == "audio")
# Audio outranks image (and every other normal-priority class) on every machine
# that can actually run the audio model.
#
# This reorders only the *next* dispatch decision, which is exactly the owner's
# rule: a machine finishes the image it is already generating, then takes audio.
# Nothing is preempted and no machine is ever held idle — the scheduler makes a
# single pass over this ordering, so any worker audio cannot use (too little
# unified memory, model not downloaded, no free RAM yet) falls through to the
# image batch in the *same* pass.
#
# The memory floor is never hardcoded here. _memory_gate() already skips a Mac
# whose total unified memory is below the model's declared
# min_unified_memory_gb, so the audio-capable pool follows the model and a
# future TTS with a different footprint needs no code change. The pool is flat:
# no size is preferred within it.
AUDIO_PRIORITY = 5
MODALITY_PRIORITY = dict({"render": 0}, **{m: AUDIO_PRIORITY for m in AUDIO_MODALITIES})

MAX_TRIES = 3
MAX_INFRA_TRIES = 8
INFRA_RETRY_WINDOW_S = 30 * 60
POLL_S = 2.0
RECOVERY_WINDOW_S = 120.0  # cover a full slow M1 generation after a dropped connection
RETRY_DELAYS_S = (3.0, 10.0)
INFRA_RETRY_DELAYS_S = (5.0, 15.0, 30.0, 60.0, 120.0, 300.0, 300.0)
CAPACITY_RETRY_S = 30.0
HANDOFF_RETRY_S = 15.0
FAILED_WORKER_AVOID_S = 90.0
MACHINE_FAILURE_THRESHOLD = 2
MACHINE_COOLDOWN_S = 120.0
MAX_BATCH_ITEMS = 1000
MAX_BATCH_JSON_BYTES = 25 * 1024 * 1024
# Never begin a new local inference workload while macOS is already critically
# short of usable memory. Model-specific cold-load floors can raise this value
# through workload_policy.required_free_memory_gb(). Catalog ``size_gb`` is
# download/disk size and must never be used as a RAM estimate.
DEFAULT_MIN_FREE_MEMORY_GB = memory_admission.DEFAULT_MIN_FREE_MEMORY_GB

batches: dict[str, dict] = {}
_busy: set[str] = set()  # studio ids currently running an item for us
_maintenance: set[str] = set()  # drained by fleet maintenance/update operations
_external_machine_leases: dict[str, str] = {}
# The two auxiliary local queues share the same physical-machine lease. Keep a
# small per-machine turn token so a chat or transcription wake loop cannot win
# every newly-free lease while the other lane is waiting. This is advisory
# process-local fairness only; the durable non-preemptive lease remains the
# admission authority and a lone lane always stays work-conserving.
_EXTERNAL_LANES = frozenset({"chat", "transcription"})
_external_lane_turn: dict[str, str] = {}
# machine -> recent transport failures and an optional circuit-breaker cooldown.
# This is intentionally process-local: after a Hub restart every worker must
# answer health again before it becomes eligible, which is a clean circuit reset.
_machine_protection: dict[str, dict] = {}
_handoff_state: dict[str, dict] = {}
_wakeup = asyncio.Event()
# Sum of live-free-memory admission floors reserved by in-flight LOCAL
# dispatches. The physical-machine lease normally prevents overlap; this also
# protects the short interval between an asynchronous telemetry check and the
# lease becoming visible to another local scheduler lane.
_reserved = {"gb": 0.0}
_submit_lock = threading.Lock()
_CLIENT_REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9._:-]{8,160}")
_GENSTUDIO_VOICE_EVIDENCE_VERSION = (1, 20, 13)
_IMMUTABLE_MODEL_REVISION = re.compile(r"^(?:sha256:)?[0-9a-fA-F]{40,64}$")
_MODEL_REVISION_FIELDS = (
    "runtime_revision", "model_revision", "snapshot_revision", "commit_sha", "revision",
)


def _required_free_memory(mem: dict) -> float:
    """Return the live available-memory floor for a workload.

    ``min_free`` is an explicit runtime admission requirement. ``size`` is
    intentionally ignored because Studio catalogs use it for download/disk
    planning, not runtime unified-memory planning.
    """
    try:
        explicit = float(mem.get("min_free"))
    except (TypeError, ValueError):
        return DEFAULT_MIN_FREE_MEMORY_GB
    return max(0.0, explicit)


def _memory_gate(mem: dict, host: dict, reserved_gb: float = 0.0) -> tuple[str, str | None]:
    """Memory-governor decision for one physical Mac. Returns one of:
      ("skip", note) — this machine can't run the model at all → try another
                       studio (a bigger remote may qualify); never errors the batch
      ("wait", note) — could run, but not enough free RAM right now → defer
      ("run",  None) — clear to dispatch
    Peer Hubs provide the same host-memory snapshot for remote Macs, allowing
    the primary Hub to avoid a request that their own engine would reject."""
    min_total = mem.get("min_total")
    if min_total and host["total_gb"] < min_total:
        return ("skip", f"needs a ~{min_total}GB machine; this one has "
                        f"{host['total_gb']}GB — trying other machines")
    need_free = _required_free_memory(mem)
    effective_free = host["available_gb"] - reserved_gb
    if effective_free < need_free:
        return ("wait", f"waiting for memory: needs ~{need_free:.1f}GB, "
                        f"~{max(0.0, effective_free):.1f}GB free")
    return ("run", None)


def _local_gate(mem: dict, host: dict) -> tuple[str, str | None]:
    """Compatibility wrapper for the local unified-memory reservation."""
    return _memory_gate(mem, host, _reserved["gb"])


def _admission_requirements(model: str, entry: dict) -> dict | None:
    """Resolve the editable site policy used immediately before dispatch."""
    if entry.get("is_cloud"):
        return None
    policy = memory_admission.describe(model, entry)
    return {
        "min_total": policy["effective_min_total_memory_gb"],
        "min_free": policy["effective_min_free_memory_gb"],
    }


def _host_for_studio(studio: dict) -> dict | None:
    machine = studio.get("machine", "local")
    if machine == "local":
        return host_stats()
    peer = peers.cached(machine) or {}
    host = peer.get("host")
    return host if isinstance(host, dict) else None


async def release_idle_siblings(client: httpx.AsyncClient, target: dict) -> dict:
    """Ask idle sibling Studios on the target Mac to release resident state.

    Worker endpoints own the final safety check: queued or active direct work
    returns 409 and is never preempted. A short machine cooldown prevents three
    independent scheduler lanes from repeatedly clearing the same Mac.
    """
    from . import chat_jobs, transcription_jobs

    machine = target.get("machine", "local")
    now = time.monotonic()
    previous = _handoff_state.get(machine) or {}
    if now - float(previous.get("at") or 0.0) < HANDOFF_RETRY_S:
        return {**previous["result"], "deferred": True}
    _handoff_state[machine] = {
        "at": now,
        "result": {"attempted": 0, "released": 0,
                   "busy": ["handoff_in_progress"], "failed": []},
    }
    known_busy = set(_busy) | set(chat_jobs.busy_studios) | set(
        transcription_jobs.busy_studios)
    siblings = [
        studio for studio in _monitor().registry
        if studio.get("machine", "local") == machine
        and studio.get("id") != target.get("id")
        and studio.get("modality") in SUPPORTED_MODALITIES
    ]

    releasable = [studio["id"] for studio in siblings if studio["id"] not in known_busy]
    operation = (await FleetMemoryControl(_monitor(), client).release(releasable)
                 if releasable else {"results": []})
    results = operation["results"]
    still_loaded = {
        row["id"] for row in results if row.get("ok")
        and any((row.get("policy") or {}).get(field) for field in (
            "loaded_model", "loaded_models", "loaded_pipeline"))
    }
    result = {
        "attempted": len(siblings),
        "released": sum(row.get("ok") and row["id"] not in still_loaded
                        for row in results),
        "busy": [studio["id"] for studio in siblings if studio["id"] in known_busy]
                + [row["id"] for row in results if row.get("result") == "busy"],
        "failed": [row["id"] for row in results
                   if (not row.get("ok") and row.get("result") != "busy")
                   or row["id"] in still_loaded],
        "deferred": False,
    }
    _handoff_state[machine] = {"at": now, "result": result}
    logging.getLogger("studiohub.broker").info(
        "memory handoff machine=%s target=%s released=%d busy=%s failed=%s",
        machine, target.get("id"), result["released"], result["busy"], result["failed"])
    return result


async def prepare_machine_memory(client: httpx.AsyncClient, studio: dict,
                                 model: str, entry: dict) -> tuple[str, str | None]:
    """Run admission, coordinated idle handoff, then one fresh admission check."""
    mem = _admission_requirements(model, entry)
    if mem is None:
        return "run", None
    is_local = studio.get("machine", "local") == "local"
    host = _host_for_studio(studio)
    if not host:
        return "run", None  # the worker's own MemoryGuard remains authoritative
    decision, note = _memory_gate(mem, host, _reserved["gb"] if is_local else 0.0)
    if decision != "wait":
        return decision, note

    handoff = await release_idle_siblings(client, studio)
    if handoff["busy"]:
        return "wait", "waiting for active sibling work on this machine"
    if not handoff["released"]:
        return decision, note
    if not is_local:
        peers.invalidate(studio.get("machine", "local"))
        await peers.refresh(_monitor().registry, client)
    refreshed = _host_for_studio(studio)
    if not refreshed:
        return "wait", "idle sibling memory released; waiting for fresh RAM telemetry"
    decision, note = _memory_gate(
        mem, refreshed, _reserved["gb"] if is_local else 0.0)
    if decision == "wait":
        note = f"released {handoff['released']} idle sibling(s); {note}"
    return decision, note


def memory_admission_dispatchable(studio: dict, model: str, entry: dict) -> bool:
    """Return whether current telemetry can admit this model without a wait.

    This is a cache/telemetry-only probe for the shared Chat/transcription
    turn arbiter. It deliberately does not release siblings or contact peers;
    the owning scheduler still performs ``prepare_machine_memory`` before its
    durable lease. Unknown telemetry remains dispatchable so the worker's own
    MemoryGuard remains authoritative, with the memory-failure turn fallback
    preventing the opposite lane from being stranded.
    """
    mem = _admission_requirements(model, entry)
    if mem is None:
        return True
    host = _host_for_studio(studio)
    if not isinstance(host, dict):
        return True
    try:
        total = float(host.get("total_gb"))
        available = float(host.get("available_gb"))
    except (TypeError, ValueError):
        return True
    if total <= 0 or available < 0:
        return True
    normalized_host = {"total_gb": total, "available_gb": available}
    reserved = _reserved["gb"] if studio.get("machine", "local") == "local" else 0.0
    decision, _note = _memory_gate(mem, normalized_host, reserved)
    return decision == "run"


def _studio_total_memory_gb(studio: dict) -> float:
    """Sortable physical-memory capacity; unknown evidence stays eligible."""
    host = _host_for_studio(studio)
    try:
        total = float((host or {}).get("total_gb"))
    except (TypeError, ValueError):
        return float("inf")
    return total if total > 0 else float("inf")


# Apple chip generation out of the marketing brand string ("Apple M4 Pro").
_CHIP_GENERATION = re.compile(r"\bM([1-9])\b")


def _studio_speed_score(studio: dict) -> float:
    """Fastest-first ranking for image workers. A PROXY, not a measurement.

    Nothing in the fleet measures image throughput: Image Studio's /api/health
    publishes readiness and memory, never seconds-per-image. The one existing
    speed signal, Render Studio's ``render_score``, is itself this same proxy
    (``generation * 100 + memory_gb``), so this mirrors its shape rather than
    inventing a scale.

    Chip generation dominates and is therefore weighted far above RAM. The
    owner's measured figures: M4/16 GB ~50-60 s/image, M2/8 GB ~90 s,
    M1/8 GB ~110-120 s — a 1.6-2.1x spread that RAM alone does not explain,
    since an M2/16 and an M4/16 have equal memory and unequal throughput.

    Replace this whole function the day Image Studio reports a real
    seconds-per-image figure in health; until then the proxy stands.

    This ranks only. It never admits or excludes: the memory floor still comes
    from the model's ``min_unified_memory_gb`` via _memory_gate(), and a worker
    with no hardware telemetry scores 0, sorts last, and stays eligible.
    """
    host = _host_for_studio(studio) or {}
    match = _CHIP_GENERATION.search(str(host.get("chip") or "").upper())
    generation = int(match.group(1)) if match else 0
    try:
        memory_gb = max(0.0, float(host.get("total_gb") or 0.0))
    except (TypeError, ValueError):
        memory_gb = 0.0
    return generation * 100 + memory_gb


def _batch_memory_constraint_gb(batch: dict) -> float:
    """Highest cache-observed admission floor for a queued exact model.

    This is a ranking hint, never an admission decision. It reads the
    monitor's durable last-good catalogue only, so queue inspection cannot
    contact workers. The per-worker catalogue, cache, immutable revision, and
    live-memory checks still decide whether dispatch may actually occur.
    """
    model = str(batch.get("model") or "")
    modality = str(batch.get("modality") or "")
    if not model:
        return 0.0
    floors: list[float] = []
    for match in _monitor().scheduling_catalog_entries(model, modality=modality):
        entry = match["entry"]
        if entry.get("is_cloud") or not memory_admission.applies_to(modality):
            continue
        requirements = _admission_requirements(model, entry)
        try:
            minimum = float((requirements or {}).get("min_total"))
        except (TypeError, ValueError):
            continue
        if minimum > 0:
            floors.append(minimum)
    return max(floors, default=0.0)


def _protection(machine: str) -> dict:
    return _machine_protection.setdefault(machine, {
        "failures": 0, "cooldown_until": None, "reason": None,
        "last_failure_at": None, "last_success_at": None,
    })


def _machine_blocked(machine: str, now: float | None = None) -> bool:
    until = (_machine_protection.get(machine) or {}).get("cooldown_until") or 0
    return until > (time.time() if now is None else now)


def machine_protection_snapshot() -> dict[str, dict]:
    """Public, secret-free circuit state for Resources and operator diagnostics."""
    now = time.time()
    out = {}
    for machine, state in _machine_protection.items():
        until = state.get("cooldown_until") or 0
        out[machine] = {
            **state,
            "quarantined": until > now,
            "retry_in_s": round(max(0.0, until - now), 1),
        }
    return out


def machine_is_quarantined(machine: str) -> bool:
    """Whether a machine is temporarily excluded after transport failures."""
    return _machine_blocked(machine)


def mark_external_machine_failure(studio: dict, message: str) -> None:
    """Record a failed non-broker worker request against the shared circuit."""
    _mark_machine_failure(studio, message)


def mark_external_machine_success(studio: dict) -> None:
    """Clear the shared circuit after a successful non-broker worker request."""
    _mark_machine_success(studio)


def _mark_machine_failure(studio: dict, message: str) -> None:
    machine = studio.get("machine", "local")
    state = _protection(machine)
    was_blocked = _machine_blocked(machine)
    state["failures"] = int(state.get("failures") or 0) + 1
    state["last_failure_at"] = time.time()
    state["reason"] = message[:240]
    if state["failures"] < MACHINE_FAILURE_THRESHOLD:
        return
    state["cooldown_until"] = time.time() + MACHINE_COOLDOWN_S
    if not was_blocked:
        from . import alerts
        alerts.emit(
            "machine_quarantined",
            f"{machine} paused for {round(MACHINE_COOLDOWN_S)}s after repeated connection failures",
            {"machine": machine, "failures": state["failures"], "reason": state["reason"]},
        )


def _mark_machine_success(studio: dict) -> None:
    machine = studio.get("machine", "local")
    state = _machine_protection.get(machine)
    if not state:
        return
    had_cooldown = state.get("cooldown_until") is not None
    had_failures = bool(state.get("failures"))
    state.update(failures=0, cooldown_until=None, reason=None,
                 last_success_at=time.time())
    if had_cooldown:
        from . import alerts
        alerts.emit("machine_recovered", f"{machine} passed a worker request and rejoined the pool",
                    {"machine": machine})
    elif not had_failures:
        _machine_protection.pop(machine, None)


def _is_capacity_failure(message: str) -> bool:
    value = message.lower()
    return any(token in value for token in (
        "memoryguarderror", "memory guard paused", "waiting for memory",
        "not enough memory", "insufficient memory",
    ))


def _is_transport_failure(exc: BaseException, message: str) -> bool:
    if isinstance(exc, (httpx.TransportError, httpx.TimeoutException)):
        return True
    if getattr(exc, "status_code", None) in {502, 503, 504}:
        return True
    value = message.lower().strip()
    return any(token in value for token in (
        "readerror", "read error", "connection dropped", "connection reset",
        "connection refused", "server disconnected", "remote protocol error",
        "timed out", "timeout", "network is unreachable", "broken pipe",
    ))


def _item_allows_studio(item: dict, studio: dict, now: float) -> bool:
    avoided = item.get("avoid_machines") or {}
    return float(avoided.get(studio.get("machine", "local"), 0) or 0) <= now


def _monitor():
    from .main import monitor
    return monitor


async def _catalog_entry(studio: dict, model: str) -> dict | None:
    """The studio's own catalog entry for a model, kept verbatim.
    Carries cache state for model-aware dispatch and capability facts for the
    governor: min_unified_memory_gb = 'needs ≥N GB TOTAL machine'. size_gb
    is download/disk metadata and is never treated as runtime RAM."""
    catalog = await _monitor().scheduling_catalog(studio)
    for m in (catalog or {}).get("models", []):
        if m.get("repo") == model or model in (m.get("aliases") or []):
            return m
    return None


def _normalized_immutable_revision(value: object) -> str | None:
    text = str(value or "").strip()
    if not _IMMUTABLE_MODEL_REVISION.fullmatch(text):
        return None
    return text.removeprefix("sha256:").lower()


def _catalog_immutable_revision(entry: dict) -> str | None:
    """Return only an immutable worker-owned cached model revision."""
    sources = [entry]
    if isinstance(entry.get("cache"), dict):
        sources.append(entry["cache"])
    for source in sources:
        for field in _MODEL_REVISION_FIELDS:
            revision = _normalized_immutable_revision(source.get(field))
            if revision is not None:
                return revision
    return None


def _catalog_matches_genstudio_revision(batch: dict, entry: dict) -> bool:
    """Fence a GenStudio attempt to the exact model snapshot it assigned.

    Legacy callers without GenStudio execution identity retain their existing
    dispatch behavior. A supplied but mutable/invalid expected revision cannot
    be satisfied by inventing evidence at Studio Hub.
    """
    execution = batch.get("genstudio_execution")
    if not isinstance(execution, dict) or not execution.get("model_revision"):
        return True
    expected = _normalized_immutable_revision(execution.get("model_revision"))
    actual = _catalog_immutable_revision(entry)
    return expected is not None and actual == expected


def _expire_genstudio_batch(batch: dict) -> bool:
    """Fence unfinished local work after GenStudio's renewable lease expires."""
    if not execution_identity.lease_expired(batch.get("genstudio_execution")):
        return False
    unfinished = [
        item for item in batch.get("items") or []
        if item.get("state") in {"queued", "running"}
    ]
    # A lease protects ownership while work is still executing. Once every
    # item is terminal, expiring that lease must not rewrite a completed batch
    # as cancelled or make a successfully generated artifact look failed in
    # operator history.
    if not unfinished:
        return False
    first_expiry = not batch.get("lease_expired")
    batch["cancelled"] = True
    batch["lease_expired"] = True
    batch["governor_note"] = "GenStudio execution lease expired"
    now = time.time()
    for item in unfinished:
        item["state"] = "cancelled"
        item["error"] = "GenStudio execution lease expired"
        item["finished_at"] = now
        item["retry_at"] = None
    if first_expiry:
        from . import alerts
        execution = batch.get("genstudio_execution") or {}
        alerts.emit(
            "genstudio_lease_expired",
            f"GenStudio lease expired with {len(unfinished)} unfinished item(s)",
            {
                "batch_id": batch.get("id"),
                "modality": batch.get("modality"),
                "genstudio_job_id": execution.get("genstudio_job_id"),
                "genstudio_attempt_id": execution.get("genstudio_attempt_id"),
                "unfinished_items": len(unfinished),
            },
        )
    return True


def renew_execution_lease(renewal: dict) -> bool:
    """Apply a validated renewal to the matching in-memory/durable batch."""
    batch_id = renewal.get("local_batch_id")
    batch = batches.get(batch_id) if batch_id else None
    if batch is None and batch_id:
        batch = ledger.load_batch(batch_id)
    if batch is None:
        batch = next(
            (
                candidate
                for candidate in batches.values()
                if (candidate.get("genstudio_execution") or {}).get(
                    "genstudio_attempt_id"
                )
                == renewal.get("genstudio_attempt_id")
            ),
            None,
        )
    if batch is None:
        return False
    evidence = dict(batch.get("genstudio_execution") or {})
    evidence["lease_expires_at"] = renewal["lease_expires_at"]
    batch["genstudio_execution"] = evidence
    batches[batch["id"]] = batch
    ledger.save_batch(batch)
    _wakeup.set()
    return True


def restore_batches():
    """Reload unfinished batches from hub.db after a Hub restart. Items that
    were mid-flight ('running') go back to 'queued' — their studio-side job is
    orphaned but the work is simply redone (generation is idempotent-enough;
    the ledger keys on the new artifact)."""
    for b in ledger.load_unfinished_batches():
        if _expire_genstudio_batch(b):
            batches[b["id"]] = b
            ledger.save_batch(b)
            continue
        for it in b["items"]:
            if it["state"] == "running":
                it["state"] = "queued"
                it["studio"] = None
                it["studio_job_id"] = None
        batches[b["id"]] = b
        ledger.save_batch(b)
    if batches:
        _wakeup.set()
    return len(batches)


def submit_batch(envelope: dict) -> dict:
    with _submit_lock:
        return _submit_batch_locked(envelope)


def _submit_batch_locked(envelope: dict) -> dict:
    modality = envelope.get("modality")
    if modality not in MODALITY:
        return {"error": f"modality must be one of {sorted(MODALITY)}"}
    items_in = envelope.get("items") or []
    if not items_in:
        return {"error": "items must be a non-empty list"}
    if len(items_in) > MAX_BATCH_ITEMS:
        return {"error": f"items is limited to {MAX_BATCH_ITEMS} per batch"}
    try:
        if len(json.dumps(envelope, separators=(",", ":")).encode()) > MAX_BATCH_JSON_BYTES:
            return {"error": "batch payload exceeds the 25 MB limit"}
    except (TypeError, ValueError):
        return {"error": "batch payload must be valid JSON"}
    if not envelope.get("model"):
        return {"error": "model (repo) is required"}
    # Refuse at the door: cloud work is never queued, never accepted
    # and then failed. This runs before execution_identity.prepare so a refused
    # submission also leaves no fence or idempotency record behind.
    refusal = cloud_guard.refusal(
        envelope, model=envelope["model"], modality=modality,
    )
    if refusal:
        return {"error": refusal, "code": cloud_guard.REFUSAL_CODE}
    routing = str(envelope.get("routing") or "pool")
    if routing not in {"pool", "remote"} and not (
        routing.startswith("studio:") and routing.split(":", 1)[1]
    ):
        return {"error": "routing must be pool, remote, or studio:<id>"}
    try:
        prepared = execution_identity.prepare(envelope)
    except execution_identity.ExecutionIdentityError as exc:
        return {"error": str(exc)}
    envelope = prepared.envelope
    client_request_id = envelope.get("clientRequestId")
    if client_request_id is not None:
        if (
            not isinstance(client_request_id, str)
            or not _CLIENT_REQUEST_ID_PATTERN.fullmatch(client_request_id)
        ):
            return {
                "error": "clientRequestId must be 8-160 safe letters, digits, or ._:-"
            }
        fingerprint_payload = {
            key: value for key, value in envelope.items() if key != "clientRequestId"
        }
        # GenStudio may reassign the exact attempt with a newer externally
        # issued fence. Ownership transport is not generation payload, so it
        # must not turn an otherwise exact idempotent replay into a conflict.
        if isinstance(fingerprint_payload.get("genstudio_execution"), dict):
            fingerprint_payload["genstudio_execution"] = {
                key: value
                for key, value in fingerprint_payload["genstudio_execution"].items()
                if key != "fencing_token"
            }
        request_fingerprint = hashlib.sha256(
            json.dumps(
                fingerprint_payload, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        existing = next(
            (
                batch
                for batch in batches.values()
                if batch.get("client_request_id") == client_request_id
            ),
            None,
        ) or ledger.load_batch_by_client_request_id(client_request_id)
        if existing is not None:
            if existing.get("request_fingerprint") != request_fingerprint:
                return {"error": "clientRequestId was already used for a different batch"}
            current_execution = existing.get("genstudio_execution") or {}
            if (
                prepared.evidence
                and current_execution
                and prepared.evidence["fencing_token"]
                    > int(current_execution.get("fencing_token") or 0)
            ):
                # Preserve the newest GenStudio-issued fence as evidence while
                # returning the same local batch and creating no new work.
                existing["genstudio_execution"] = prepared.evidence
                if existing["id"] in batches:
                    batches[existing["id"]]["genstudio_execution"] = prepared.evidence
                ledger.save_batch(existing)
            execution_identity.bind_local_batch(prepared.evidence, existing["id"])
            return {
                "batch_id": existing["id"],
                "items": len(existing.get("items") or []),
                "replayed": True,
            }
    else:
        request_fingerprint = None
    batch_id = uuid.uuid4().hex[:10]
    batches[batch_id] = {
        "id": batch_id,
        "client_request_id": client_request_id,
        "request_fingerprint": request_fingerprint,
        # Non-authoritative identity assigned by GenStudio. Studio Hub only
        # uses it to fence and evidence this one site-local execution.
        "genstudio_execution": prepared.evidence,
        "modality": modality,
        "model": envelope["model"],
        "shared_params": envelope.get("sharedParams") or {},
        "routing": routing,
        "label": envelope.get("label"),        # who submitted (e.g. "storystudio")
        "webhook": envelope.get("webhook"),    # POSTed the summary on completion
        "item_webhook": envelope.get("itemWebhook"),  # POSTed per item as each finishes
        "webhook_sent": False,
        "created_at": time.time(),
        "cancelled": False,
        "items": [{
            "index": i,
            "prompt": it.get("prompt") or it.get("text") or "",
            "seed": it.get("seed"),
            "params": it.get("params") or {},
            "state": "queued",       # queued|running|done|error|cancelled
            "tries": 0,
            "studio": None,
            "studio_job_id": None,
            "artifact_path": None,
            "artifact_url": None,
            "asset_id": None,
            "error": None,
            "retry_at": None,
        } for i, it in enumerate(items_in)],
    }
    ledger.save_batch(batches[batch_id])
    execution_identity.bind_local_batch(prepared.evidence, batch_id)
    _wakeup.set()
    return {"batch_id": batch_id, "items": len(items_in), "replayed": False}


def busy_studios() -> set:
    """Studio ids currently running a batch item (i.e. 'generating')."""
    return set(_busy)


def busy_machines() -> set[str]:
    """Physical machines holding a non-preemptive heavy-work lease."""
    by_id = {s["id"]: s for s in _monitor().registry}
    return set(_external_machine_leases) | {
        by_id[sid].get("machine", "local")
        for sid in _busy
        if sid in by_id
    }


def acquire_external_machine(machine: str, owner: str) -> bool:
    """Atomically reserve a physical machine for another heavy-work queue."""
    if machine in busy_machines():
        return False
    # Episode renders are the fleet's highest-priority queued work. Never
    # interrupt a running job, but once a render is waiting, reserve an eligible
    # render Mac so a Chat/transcription poller cannot win the next-free race.
    if not owner.startswith("render:") and _pending_render_for_machine(machine):
        return False
    _external_machine_leases[machine] = owner
    _wakeup.set()
    return True


def external_dispatch_allowed(
        machine: str, lane: str, *, other_lane_has_work: bool) -> bool:
    """Apply the shared chat/transcription turn token for one physical Mac.

    The token is consulted only while both lanes have queued work. If the
    other lane is empty, the caller may use a free machine immediately even if
    the previous token points elsewhere. ``acquire_external_machine`` still
    provides the atomic lease and is the final authority before dispatch.
    """
    if lane not in _EXTERNAL_LANES:
        raise ValueError(f"unsupported external dispatch lane: {lane}")
    if not other_lane_has_work:
        return True
    return _external_lane_turn.get(machine) in {None, lane}


def note_external_dispatch(machine: str, lane: str) -> None:
    """Pass the next shared-machine turn to the opposite auxiliary lane."""
    if lane not in _EXTERNAL_LANES:
        raise ValueError(f"unsupported external dispatch lane: {lane}")
    other = next(iter(_EXTERNAL_LANES - {lane}))
    _external_lane_turn[machine] = other


def note_external_memory_block(machine: str, lane: str) -> None:
    """Pass a blocked lane's shared-machine turn to its eligible opposite."""
    if lane not in _EXTERNAL_LANES:
        raise ValueError(f"unsupported external dispatch lane: {lane}")
    other = next(iter(_EXTERNAL_LANES - {lane}))
    _external_lane_turn[machine] = other
    _wakeup.set()


def _pending_render_for_machine(machine: str) -> bool:
    if not any(
        b.get("modality") == "render" and not b.get("cancelled")
        and any(item.get("state") == "queued" for item in b.get("items", []))
        for b in batches.values()
    ):
        return False
    mon = _monitor()
    return any(
        studio.get("modality") == "render"
        and studio.get("machine", "local") == machine
        and machine_enabled(machine)
        and studio_enabled(machine, studio["id"])
        and mon.status.get(studio["id"], {}).get("status") == "up"
        for studio in mon.registry
    )


def release_external_machine(machine: str, owner: str) -> None:
    if _external_machine_leases.get(machine) == owner:
        del _external_machine_leases[machine]
        _wakeup.set()


def set_maintenance(studio_id: str, enabled: bool):
    if enabled:
        _maintenance.add(studio_id)
    else:
        _maintenance.discard(studio_id)
        _wakeup.set()


def in_maintenance(studio_id: str) -> bool:
    return studio_id in _maintenance


def _recent_avg(modality: str, model: str, limit: int = 50) -> float | None:
    """Average completed-item duration for this (modality, model) across ALL
    batches — so even a 1-item batch gets an ETA from the model's track record,
    not just from its own (nonexistent) completed siblings."""
    durs = []
    for b in batches.values():
        if b["modality"] != modality or b["model"] != model:
            continue
        for i in b["items"]:
            runtime = i.get("runtime_s", i.get("duration_s"))
            if i.get("state") == "done" and isinstance(runtime, (int, float)):
                durs.append(runtime)
    durs = durs[-limit:]
    return round(sum(durs) / len(durs), 1) if durs else None


def batch_summary(b: dict) -> dict:
    items = b["items"]
    states = [i["state"] for i in items]
    now = time.time()
    # ETA basis: this batch's own completed items if any, else the model's recent
    # average across every batch (so single-item jobs still get an estimate).
    done_durs = [i.get("runtime_s", i.get("duration_s")) for i in items
                 if i.get("state") == "done"
                 and isinstance(i.get("runtime_s", i.get("duration_s")), (int, float))]
    avg_s = (round(sum(done_durs) / len(done_durs), 1) if done_durs
             else _recent_avg(b["modality"], b["model"]))
    # Per-item live detail for whatever is running right now (machine tag + progress).
    # Keep batch-level timing separately: the Jobs page needs to answer both
    # "how long has this been processing?" and "has anything moved recently?".
    running_items = []
    started_at = []
    activity_at = [b.get("created_at", now), b.get("last_dispatched_at", 0)]
    for i in items:
        run_started = i.get("run_started")
        if isinstance(run_started, (int, float)):
            started_at.append(run_started)
            activity_at.append(run_started)
        for key in ("last_progress_at", "finished_at"):
            value = i.get(key)
            if isinstance(value, (int, float)):
                activity_at.append(value)
        # Batches saved before explicit terminal timestamps still have enough
        # information to estimate their most recent completed item.
        runtime = i.get("runtime_s", i.get("duration_s"))
        if (i.get("state") == "done" and isinstance(run_started, (int, float))
                and isinstance(runtime, (int, float))):
            activity_at.append(run_started + runtime)
        if i.get("state") != "running":
            continue
        sid = i.get("studio") or ""
        machine = sid.split("@", 1)[1] if "@" in sid else "local"
        started = run_started
        elapsed = round(now - started, 1) if started else None
        running_items.append({
            "index": i.get("index"),
            "studio": sid,                 # e.g. "image@macmini-m1-01" or "image"
            "machine": machine,            # "macmini-m1-01" or "local"
            "progress": i.get("progress"),  # 0..1 or None
            "chunk_index": i.get("chunk_index"),
            "chunk_total": i.get("chunk_total"),
            "elapsed_s": elapsed,
        })
    retrying = [i for i in items if i["state"] == "queued"
                and (i.get("retry_at") or 0) > now]
    active = bool(states.count("queued") or states.count("running"))
    processing_started_at = min(started_at) if started_at else None
    last_activity_at = max(activity_at)
    # A missing worker progress report is normal for some local MLX models, so
    # do not call a job stuck merely because a single poll had no percentage.
    # Fifteen minutes, or five times its measured per-item average, is a useful
    # conservative warning threshold rather than an automatic cancellation.
    stalled_after_s = max(15 * 60, round((avg_s or 0) * 5))
    no_progress_s = round(max(0, now - last_activity_at), 1) if active else None
    return {
        "id": b["id"], "modality": b["modality"], "model": b["model"],
        "created_at": b["created_at"], "finished_at": b.get("finished_at"),
        "cancelled": b["cancelled"],
        "routing": b.get("routing", "pool"),
        "governor_note": b.get("governor_note"),
        "label": b.get("label"),
        "total": len(states),
        "queued": states.count("queued") - len(retrying),
        "retrying": len(retrying),
        "next_retry_at": min((i["retry_at"] for i in retrying), default=None),
        "running": states.count("running"),
        "done": states.count("done"),
        "error": states.count("error"),
        "cancelled_items": states.count("cancelled"),
        "avg_s": avg_s,
        "running_items": running_items,
        "processing_started_at": processing_started_at,
        "processing_elapsed_s": (round(max(0, now - processing_started_at), 1)
                                 if active and processing_started_at else None),
        "last_activity_at": last_activity_at,
        "no_progress_s": no_progress_s,
        "stalled_after_s": stalled_after_s,
        "stalled": bool(active and no_progress_s is not None
                        and no_progress_s >= stalled_after_s),
    }


async def _signal_worker_cancel(client: httpx.AsyncClient, item: dict) -> bool:
    """Ask the exact Studio worker to stop its active generation job."""
    studio_id = item.get("studio")
    job_id = item.get("studio_job_id")
    if not studio_id or not job_id:
        return False
    studio = next((s for s in _monitor().registry if s["id"] == studio_id), None)
    if not studio:
        item["cancel_error"] = "worker is no longer registered"
        return False
    try:
        url, headers = studio_request(studio, f"/api/generate/jobs/{job_id}")
        response = await client.delete(
            url, headers=headers, timeout=15.0)
        # A 404 means the worker no longer has active work under this id. The
        # broker poll will reconcile whether it completed just before cancel.
        if response.status_code in (200, 404):
            item["cancel_signal_sent_at"] = time.time()
            item.pop("cancel_error", None)
            return True
        item["cancel_error"] = f"worker returned HTTP {response.status_code}"
    except httpx.HTTPError as e:
        item["cancel_error"] = str(e) or type(e).__name__
    return False


async def cancel_batch(batch_id: str, client: httpx.AsyncClient | None = None) -> dict | None:
    """Cancel queued work and immediately signal every known running worker."""
    b = batches.get(batch_id)
    if b is None:
        return None
    b["cancelled"] = True
    queued_cancelled = 0
    for it in b["items"]:
        if it["state"] == "queued":
            it["state"] = "cancelled"
            it["error"] = "Cancelled by user"
            it["retry_at"] = None
            queued_cancelled += 1
    ledger.save_batch(b)
    _wakeup.set()

    running = [it for it in b["items"] if it["state"] == "running"]
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient()
    try:
        signals = await asyncio.gather(
            *(_signal_worker_cancel(client, it) for it in running)) if running else []
        for it in b["items"]:
            if it["state"] == "cancelled":
                await _post_item_webhook(client, b, it)
        await _maybe_finish(client, b)
    finally:
        if owns_client:
            await client.aclose()
    return {
        "batch": b,
        "queued_cancelled": queued_cancelled,
        "running_signalled": sum(1 for sent in signals if sent),
        "running_pending": sum(1 for sent in signals if not sent),
    }


async def cancel_batches(modality: str | None = None) -> dict:
    """Cancel every active batch, optionally limited to one modality."""
    targets = [
        b["id"] for b in batches.values()
        if (modality is None or b.get("modality") == modality)
        and any(it.get("state") in ("queued", "running") for it in b.get("items", []))
    ]
    results = []
    async with httpx.AsyncClient() as client:
        for batch_id in targets:
            result = await cancel_batch(batch_id, client)
            if result:
                results.append(result)
    return {
        "batches_cancelled": len(results),
        "queued_cancelled": sum(r["queued_cancelled"] for r in results),
        "running_signalled": sum(r["running_signalled"] for r in results),
        "running_pending": sum(r["running_pending"] for r in results),
    }


def clear_finished_batches(modality: str | None = None,
                           batch_id: str | None = None) -> dict:
    """Remove terminal job history without deleting any generated assets."""
    known = {b["id"]: b for b in ledger.load_finished_batches()}
    known.update({b["id"]: b for b in batches.values()})
    selected = []
    for candidate_id, b in known.items():
        if batch_id is not None and candidate_id != batch_id:
            continue
        if modality is not None and b.get("modality") != modality:
            continue
        if any(it.get("state") in ("queued", "running") for it in b.get("items", [])):
            continue
        selected.append(candidate_id)
    for candidate_id in selected:
        batches.pop(candidate_id, None)
    ledger.delete_batches(selected)
    return {"cleared": len(selected), "batch_ids": selected}


async def _post_item_webhook(client: httpx.AsyncClient, b: dict, item: dict):
    """POST a single item to the client's per-item webhook the moment it reaches a
    terminal state — lets a client submit ALL scenes as one batch yet still
    receive each result as it finishes (instead of waiting for the whole batch).
    Fires at most once per item; skipped for retry-requeued items."""
    url = b.get("item_webhook")
    if not url or item.get("_item_notified"):
        return
    if item["state"] not in ("done", "error", "cancelled"):
        return
    item["_item_notified"] = True
    sid = item.get("studio") or ""
    safe_item = public_item(b, item)
    try:
        await client.post(url, json={
            "batch_id": b["id"], "label": b.get("label"),
            "index": item["index"], "state": item["state"],
            "studio": sid,
            "machine": safe_item.get("machine") or (
                sid.split("@", 1)[1] if "@" in sid else "local"
            ),
            "worker_id": safe_item.get("worker_id"),
            "model_revision": safe_item.get("model_revision"),
            "runtime_revision": safe_item.get("runtime_revision"),
            "artifact_url": hub_artifact_url(b, item),
            "asset_id": item.get("asset_id"),
            "runtime_s": item.get("runtime_s", item.get("duration_s")),
            "duration_s": item.get("runtime_s", item.get("duration_s")),  # legacy alias
            "execution_started_at": safe_item.get("execution_started_at"),
            "terminal_result": terminal_result(b, item),
            "error_code": safe_item.get("error_code"),
            "error": safe_item.get("error"),
            "resource_usage": safe_item.get("resource_usage"),
            # running batch tally so the client can show n/N without a poll
            "done": sum(1 for i in b["items"] if i["state"] == "done"),
            "total": len(b["items"]),
        }, timeout=10.0)
    except httpx.HTTPError:
        pass  # client unreachable — the item is still in the batch/poll + ledger


def hub_artifact_url(b: dict, item: dict) -> str | None:
    """Stable Hub-relative identity; never expose a worker-local path."""
    if item.get("state") != "done":
        return None
    return f"/api/hub/jobs/{b['id']}/items/{item['index']}/artifact"


def terminal_result(b: dict, item: dict) -> dict | None:
    """Safe result envelope for customer-facing consumers such as GenStudio."""
    state = item.get("state")
    if state != "done":
        return None
    evidence = {
        "runtime_s": item.get("runtime_s", item.get("duration_s")),
        # Kept only for callers that predate runtime_s. It is runtime, never
        # decoded media duration.
        "duration_s": item.get("runtime_s", item.get("duration_s")),
        "model_revision": item.get("model_revision"),
        "runtime_revision": item.get("runtime_revision"),
        "worker_id": item.get("worker_id"),
        "machine_id": item.get("machine"),
        "resource_usage": item.get("resource_usage"),
    }
    return {
        "status": "succeeded",
        "asset_id": item.get("asset_id"),
        "artifact_url": hub_artifact_url(b, item),
        "media_type": item.get("media_type"),
        "format": item.get("format"),
        "bytes": item.get("bytes"),
        "sha256": item.get("sha256"),
        "audio_duration_s": item.get("audio_duration_s"),
        "audio_duration_ms": item.get("audio_duration_ms"),
        "sample_rate_hz": item.get("sample_rate_hz"),
        "channels": item.get("channels"),
        "voice_revision": item.get("voice_revision"),
        "voice_library_id": item.get("voice_library_id"),
        "preset_speaker": item.get("preset_speaker"),
        "reference_audio_sha256": item.get("reference_audio_sha256"),
        "reference_source_sha256": item.get("reference_source_sha256"),
        "reference_preparation_revision": item.get("reference_preparation_revision"),
        "reference_duration_s": item.get("reference_duration_s"),
        "long_form_strategy": item.get("long_form_strategy"),
        "chunk_total": item.get("chunk_total"),
        "width": item.get("width"),
        "height": item.get("height"),
        "steps": item.get("steps"),
        "resolved_seed": item.get("resolved_seed"),
        **evidence,
    }


def public_item(b: dict, item: dict) -> dict:
    """Return a public job item without worker-local paths or worker URLs."""
    result = {k: v for k, v in item.items()
              if k not in {"artifact_path", "worker_artifact_url"}}
    result["execution_started_at"] = item.get("execution_started_at")
    if item.get("state") == "done":
        result["artifact_url"] = hub_artifact_url(b, item)
        result["terminal_result"] = terminal_result(b, item)
    elif item.get("state") in {"error", "cancelled"}:
        if result.get("error_code") is not None:
            result["error_code"] = _sanitize_public_error_code(result["error_code"])
        if result.get("error") is not None:
            result["error"] = _sanitize_public_error(result["error"])
    return result


_PUBLIC_AUTHORIZATION_RE = re.compile(
    r'''(?i)["']?\bauthorization\b["']?\s*[:=]\s*["']?'''
    r'''(?:bearer\s+)?[^"'\s,;}]+["']?'''
)
_PUBLIC_ERROR_CODE_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_PUBLIC_BEARER_RE = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_PUBLIC_CREDENTIAL_RE = re.compile(
    r'''(?i)["']?\b(x-hub-token|api[_-]?key|token|secret|password)\b'''
    r'''["']?\s*[:=]\s*["']?[^"'\s,;}]+["']?'''
)
_PUBLIC_FILE_URI_RE = re.compile(
    r'''(?i)file:///(?:Users|Volumes|Library|Applications|System|Network|private|tmp|var|opt|home|usr|etc|dev|bin|sbin|cores)/[^\s,;}\]"']+'''
)
_PUBLIC_LOCAL_PATH_RE = re.compile(
    r'''(?<![A-Za-z0-9:])/(?:Users|Volumes|Library|Applications|System|Network|private|tmp|var|opt|home|usr|etc|dev|bin|sbin|cores)/[^\s,;}\]"']+'''
)
_PUBLIC_HOME_PATH_RE = re.compile(r'''(?<![A-Za-z0-9])~/[^\s,;}\]"']+''')


def _sanitize_public_error(value: object) -> str:
    """Bound worker detail and remove common credentials and local Mac paths."""
    text = str(value)
    text = _PUBLIC_AUTHORIZATION_RE.sub("Authorization=[redacted]", text)
    text = _PUBLIC_BEARER_RE.sub("Bearer [redacted]", text)
    text = _PUBLIC_CREDENTIAL_RE.sub(
        lambda match: f"{match.group(1)}=[redacted]", text,
    )
    text = _PUBLIC_FILE_URI_RE.sub("[local path]", text)
    text = _PUBLIC_LOCAL_PATH_RE.sub("[local path]", text)
    text = _PUBLIC_HOME_PATH_RE.sub("[local path]", text)
    return text[:1000]


def _sanitize_public_error_code(value: object) -> str:
    code = str(value).strip()[:128]
    return code if _PUBLIC_ERROR_CODE_RE.fullmatch(code) else "WORKER_TERMINAL_ERROR"


async def _cache_voice_artifact_metadata(client: httpx.AsyncClient, item: dict,
                                         studio: dict, worker_url: str,
                                         expected_bytes, expected_sha256) -> None:
    """Fetch and decode a terminal voice artifact once, then persist its facts.

    A metadata failure never retries an already-completed generation: the
    result remains available but is marked unverified and therefore unbillable.
    """
    try:
        url, headers = studio_request(studio, worker_url)
        # Long-form 40k narration can produce a 100–160 MB PCM WAV. Give an
        # authenticated fleet transfer enough time on slower Tailscale links;
        # generation remains protected by the renewable GenStudio lease.
        response = await client.get(url, headers=headers, timeout=600.0)
        response.raise_for_status()
        metadata = artifact_metadata.wav_metadata(response.content)
        if expected_bytes is not None and int(expected_bytes) != metadata["bytes"]:
            raise ValueError("worker byte count does not match downloaded artifact")
        if expected_sha256 and str(expected_sha256).lower() != metadata["sha256"]:
            raise ValueError("worker checksum does not match downloaded artifact")
    except (ValueError, httpx.HTTPError) as exc:
        item["artifact_metadata_error"] = str(exc)
        return
    item.update(metadata)
    item.pop("artifact_metadata_error", None)


async def _record_worker_success(client: httpx.AsyncClient, b: dict, item: dict,
                                 studio: dict, job: dict, body: dict,
                                 t_start: float):
    """Adopt a completed worker job into the Hub ledger.

    This is deliberately separate from the normal poll loop: if the network
    drops after the worker finishes, recovery can record the already-produced
    artifact instead of submitting a duplicate generation.
    """
    if _expire_genstudio_batch(b):
        await _signal_worker_cancel(client, item)
        return
    _persist_worker_execution_started_at(b, item, job)
    if item.get("state") == "done" and item.get("asset_id"):
        return  # terminal polling/recovery is idempotent
    _record_worker_resource_usage(item, job)
    item["artifact_path"] = job.get("output_path")
    worker_url = (
        f"{base_url(studio)}{job['output_url']}" if job.get("output_url") else None)
    item["worker_artifact_url"] = worker_url
    # Kept internally for old ledger/reference behavior; API readers are
    # normalized by public_item(), which returns the stable Hub proxy URL.
    item["artifact_url"] = worker_url
    item["encoder"] = job.get("encoder")
    _record_worker_identity(item, studio, job)
    item["voice_revision"] = job.get("voice_revision")
    if b["modality"] == "image":
        for field in ("width", "height", "steps"):
            value = job.get(field)
            if type(value) is int and value >= 0:
                item[field] = value
        resolved_seed = job.get("resolved_seed")
        if type(resolved_seed) is int:
            item["resolved_seed"] = resolved_seed
            # Completed public items historically expose ``seed``. Make that
            # existing field reproducible when the worker resolved a random seed.
            item["seed"] = resolved_seed
    if b["modality"] == "voice":
        item["voice_library_id"] = body.get("voice_library_id")
        item["preset_speaker"] = body.get("preset_speaker")
        for field in (
            "reference_audio_sha256", "reference_source_sha256",
            "reference_preparation_revision", "reference_duration_s",
            "long_form_strategy", "chunk_total",
        ):
            item[field] = job.get(field)
    runtime = job.get("runtime_s", job.get("generation_seconds", job.get("duration_seconds")))
    try:
        runtime = float(runtime) if runtime is not None else round(time.time() - t_start, 2)
    except (TypeError, ValueError):
        runtime = round(time.time() - t_start, 2)
    item["runtime_s"] = runtime
    item["duration_s"] = runtime  # compatibility: historic duration_s meant runtime
    if b["modality"] == "voice" and worker_url:
        await _cache_voice_artifact_metadata(
            client, item, studio, worker_url, job.get("bytes"), job.get("sha256"))
    else:
        item["sha256"] = job.get("sha256")
        item["bytes"] = job.get("bytes")
        item["media_type"] = artifact_metadata.trusted_media_type(
            job.get("media_type") or job.get("content_type"), b["modality"])
    item["finished_at"] = time.time()
    item["last_progress_at"] = item["finished_at"]
    item["asset_id"] = ledger.record_asset(
        source="job", modality=b["modality"], studio=studio["id"],
        machine=studio.get("machine", "local"), model=b["model"],
        seed=job.get("resolved_seed") or item["seed"], prompt=item["prompt"],
        params=body, artifact_path=item["artifact_path"],
        artifact_url=worker_url, batch_id=b["id"],
        item_index=item["index"], duration_s=runtime, runtime_s=runtime,
    )
    # Publish terminal state only after metadata, revision evidence, and the
    # stable asset identity are complete. Pollers must never observe a partial
    # "done" result while the Hub is still finalizing the worker artifact.
    item["state"] = "done"


async def _recover_worker_job(client, b: dict, item: dict, studio: dict,
                              body: dict, t_start: float) -> bool:
    """Reconcile a worker job after a transport failure.

    A generation request is not safely retryable once the worker has accepted
    it. Keep the Hub lease while reconnecting and poll the original job for a
    bounded window. Return True only after adopting a completed result.
    """
    job_id = item.get("studio_job_id")
    if not job_id or b.get("cancelled"):
        return False
    deadline = time.monotonic() + RECOVERY_WINDOW_S
    delay = 1.0
    while time.monotonic() < deadline:
        if _expire_genstudio_batch(b):
            await _signal_worker_cancel(client, item)
            return False
        try:
            url, headers = studio_request(studio, f"/api/generate/jobs/{job_id}")
            jr = await client.get(
                url, headers=headers, timeout=10.0)
            if jr.status_code >= 400:
                return False  # 404/4xx means the worker no longer has the job
            job = jr.json().get("job") or {}
            _persist_worker_execution_started_at(b, item, job)
            _record_worker_resource_usage(item, job)
            state = job.get("state")
            if state in ("queued", "running"):
                _record_worker_progress(item, job.get("progress"))
                await asyncio.sleep(POLL_S)
                continue
            if state == "done" and not job.get("error"):
                await _record_worker_success(client, b, item, studio, job, body, t_start)
                _mark_machine_success(studio)
                return True
            return False  # the original job genuinely failed or was cancelled
        except Exception:
            # Tailscale/Wi-Fi and a busy worker can briefly drop the HTTP
            # connection. Back off while retaining the same worker lease.
            await asyncio.sleep(delay)
            delay = min(delay * 2, 8.0)
    return False


async def _maybe_finish(client: httpx.AsyncClient, b: dict):
    """Persist state; when the batch just reached a terminal state, alert on any
    failures and fire the client's webhook (Story Studio et al) once."""
    ledger.save_batch(b)
    if b.get("done_notified"):
        return
    states = {i["state"] for i in b["items"]}
    if states & {"queued", "running"}:
        return  # not terminal yet
    if not b.get("finished_at"):
        b["finished_at"] = time.time()
    b["done_notified"] = True
    ledger.save_batch(b)
    summary = batch_summary(b)
    if summary["error"]:
        from . import alerts
        alerts.emit("batch_failed",
                    f"batch {b['id']} ({b['modality']}/{b['model']}): "
                    f"{summary['error']}/{summary['total']} items failed",
                    {"batch_id": b["id"], **{k: summary[k] for k in
                                             ("done", "error", "total")}})
    if b.get("webhook") and not b.get("webhook_sent"):
        b["webhook_sent"] = True
        try:
            await client.post(b["webhook"], json={
                **summary,
                "items": [public_item(b, it) for it in b["items"]],
            }, timeout=10.0)
        except httpx.HTTPError:
            pass  # client unreachable — batch state is still queryable


def _eligible_studios(modality: str, routing: str) -> list[dict]:
    mon = _monitor()
    out = []
    leased_machines = busy_machines()
    for s in mon.registry:
        if routing.startswith("studio:") and s["id"] != routing.split(":", 1)[1]:
            continue
        machine = s.get("machine", "local")
        # A remote render deliberately keeps the Hub Mac as the control plane.
        # It waits for an external Render Studio rather than quietly consuming
        # the Hub machine's CPU / Media Engine as a fallback.
        if routing == "remote" and machine == "local":
            continue
        if (s["modality"] != modality or s["id"] in _busy
                or s["id"] in _maintenance or machine in leased_machines):
            continue
        # a machine the operator has disabled stays monitored but takes no jobs
        if not machine_enabled(s.get("machine", "local")):
            continue
        # App-specific pauses are scheduler-only: current work finishes and the
        # Studio remains online for monitoring, lifecycle control, and updates.
        if not studio_enabled(machine, s["id"]):
            continue
        if _machine_blocked(machine):
            continue
        if mon.status.get(s["id"], {}).get("status") == "up":
            out.append(s)
    if modality == "render":
        # Render workers publish a normalized score in /api/health. M4 16 GB
        # machines rank above older/smaller Macs, while every healthy worker
        # remains an eligible fallback.
        out.sort(key=lambda s: (
            -float((mon.status.get(s["id"], {}).get("health") or {})
                   .get("render_score", 0)),
            s["id"],
        ))
    elif modality == "image":
        # Fastest-free-worker placement. Image work is the fleet's slowest
        # per-item class and the spread between machines is large, so sending
        # an image to the smallest free Mac cost 1.6-2.1x the wall clock for
        # no remaining benefit — see _studio_speed_score() for the figures.
        #
        # This deliberately reverses the best-fit rule below. Best-fit existed
        # to keep high-memory workers free for audio without reserving them;
        # 2.11.0 replaced that with audio taking the next free capable worker
        # outright, so best-fit became a tax on every image for a benefit
        # already delivered elsewhere.
        #
        # Preference only, never a filter: every healthy worker stays in the
        # list, so a slow Mac still takes the job when it is the only one free.
        out.sort(key=lambda studio: (-_studio_speed_score(studio), studio["id"]))
    else:
        # Audio (and video) keep best-fit placement, unchanged. Audio must
        # never gain a preference for high-memory machines — the owner measured
        # 24 GB as *slower* for TTS — so any Mac clearing the model's declared
        # floor is an equal audio candidate and the fastest-first rule above is
        # deliberately image-scoped rather than global.
        #
        # Best-fit also still fills an idle small Mac before a large one for a
        # flexible workload, while every larger worker remains an eligible
        # fallback when demand exceeds the smaller tier's capacity. Unknown
        # telemetry sorts last but is not blocked; the worker's own admission
        # guard remains authoritative.
        out.sort(key=lambda studio: (
            _studio_total_memory_gb(studio), studio["id"],
        ))
    return out


def _shared_voice_allows_studio(batch: dict, item: dict, studio: dict) -> bool:
    """Keep Hub-owned clones on workers where their stable id is synchronized.

    Unknown ids retain the legacy behavior because they may be direct-only
    Voice Studio library entries. Hub-owned ids with no successful targets
    wait in queue until the background synchronizer heals one worker.
    """
    if batch.get("modality") != "voice":
        return True
    params = dict(batch.get("shared_params") or {})
    params.update(item.get("params") or {})
    voice_id = str(params.get("voice_library_id") or "").strip()
    if not voice_id:
        return True
    synced = shared_voices.synced_studio_ids(voice_id)
    return synced is None or studio.get("id") in synced


def _version_tuple(value: object) -> tuple[int, int, int] | None:
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", str(value or ""))
    return tuple(map(int, match.groups())) if match else None


def _supports_genstudio_voice_evidence(batch: dict, studio: dict) -> bool:
    """Keep customer jobs off workers that cannot report immutable revisions."""
    if (
        batch.get("modality") != "voice"
        or not str(batch.get("label") or "").startswith("genstudio-kh:")
    ):
        return True
    status = _monitor().status.get(studio.get("id"), {})
    version = status.get("app_version") or (status.get("health") or {}).get(
        "app_version"
    )
    parsed = _version_tuple(version)
    return parsed is not None and parsed >= _GENSTUDIO_VOICE_EVIDENCE_VERSION


def _queued_batches() -> list[dict]:
    """Queued work in modality, scarcity, then fair-turn order.

    Audio sorts ahead of image (see MODALITY_PRIORITY), so a worker that just
    freed up is offered to queued audio before queued image. Because the
    scheduler makes one pass over this list, this never idles a worker: audio
    that cannot use a given Mac leaves it to the image batch immediately.

    Within one normal-priority local-inference class, a workload that needs
    more total memory receives the next compatible high-memory worker before a
    flexible workload does. This changes only the next dispatch decision:
    running work is never preempted, and lower-memory work remains
    work-conserving when no constrained job is waiting.
    """
    return sorted(
        batches.values(),
        key=lambda b: (MODALITY_PRIORITY.get(b["modality"], 10),
                       -_batch_memory_constraint_gb(b),
                       b.get("last_dispatched_at", 0),
                       b.get("created_at", 0)),
    )


def _image_batch_waiting(now: float) -> dict | None:
    """A dispatchable image batch, for the audio-priority decision log.

    Read from the live queue, never inferred: this is the same `queued` test the
    scheduler itself applies, so the log line only claims audio went first when
    an image job really was ready to take that worker.
    """
    for batch in batches.values():
        if batch["modality"] != "image" or batch.get("cancelled"):
            continue
        if any(i["state"] == "queued" and (i.get("retry_at") or 0) <= now
               for i in batch["items"]):
            return batch
    return None


def _log_audio_priority(batch: dict, item: dict, studio: dict, now: float) -> None:
    """One line whenever audio takes a worker ahead of ready image work."""
    if batch["modality"] not in AUDIO_MODALITIES:
        return
    waiting = _image_batch_waiting(now)
    if waiting is None:
        return
    logging.getLogger("studiohub.broker").info(
        "audio priority machine=%s studio=%s audio=%s/%s#%d image-waiting=%s/%s "
        "— audio takes this worker first; the image job keeps every other worker",
        studio.get("machine", "local"), studio["id"],
        batch["id"], batch["model"], item["index"],
        waiting["id"], waiting["model"])


async def _dispatch_loop():
    """The scheduler: match queued items to free studios, forever."""
    client = httpx.AsyncClient(timeout=httpx.Timeout(connect=5, read=60, write=30, pool=5))
    while True:
        try:
            assigned = False
            for b in _queued_batches():
                if _expire_genstudio_batch(b):
                    ledger.save_batch(b)
                    continue
                if b["cancelled"]:
                    continue
                now = time.time()
                queued = [i for i in b["items"] if i["state"] == "queued"
                          and (i.get("retry_at") or 0) <= now]
                if not queued:
                    continue
                eligible = _eligible_studios(b["modality"], b["routing"])
                evidence_eligible = [
                    studio
                    for studio in eligible
                    if _supports_genstudio_voice_evidence(b, studio)
                ]
                if eligible and not evidence_eligible:
                    b["governor_note"] = (
                        "Waiting for Voice Studio 1.20.13 or newer so GenStudio "
                        "receives immutable model and voice revision evidence"
                    )
                eligible = evidence_eligible
                if not eligible and b.get("routing") == "remote":
                    b["governor_note"] = (
                        "Waiting for an online remote worker; this Hub Mac is intentionally excluded"
                    )
                for studio in eligible:
                    if not queued:
                        break
                    compatible_index = next(
                        (index for index, candidate in enumerate(queued)
                         if _shared_voice_allows_studio(b, candidate, studio)
                         and _item_allows_studio(candidate, studio, now)),
                        None,
                    )
                    if compatible_index is None:
                        b["governor_note"] = (
                            "Waiting for a Voice Studio where the selected "
                            "shared voice is synchronized"
                        )
                        continue
                    # ── model-aware dispatch (heterogeneous machines) ──
                    # A studio only gets work for models it actually has. This
                    # is what lets a 3-model Mac and a 1-model Mac share pools.
                    entry = await _catalog_entry(studio, b["model"])
                    if entry is None:
                        b["governor_note"] = (
                            f"'{b['model']}' not in {studio['id']}'s catalog")
                        continue
                    if not _catalog_matches_genstudio_revision(b, entry):
                        b["governor_note"] = (
                            "Waiting for a worker with the exact immutable model "
                            "revision assigned by GenStudio"
                        )
                        continue
                    if not entry.get("is_cloud") and not is_cached(entry):
                        b["governor_note"] = (
                            f"'{b['model']}' not downloaded on {studio['id']} "
                            f"— broadcast the download or try another machine")
                        continue
                    # ── fleet memory governor ──
                    # Peer Hubs report their own host snapshots, so we can
                    # avoid submitting work that their MemoryGuard would
                    # reject. Missing/stale telemetry does not strand work:
                    # the worker's own guard remains the final authority.
                    is_local = studio.get("machine", "local") == "local"
                    # A worker catalog is one input. Measured fleet defaults
                    # and visible owner overrides form the effective site-local
                    # admission policy used immediately before dispatch.
                    mem = _admission_requirements(b["model"], entry)
                    reserve = 0.0
                    if mem is not None:
                        decision, note = await prepare_machine_memory(
                            client, studio, b["model"], entry)
                        if decision != "run":
                            # skip → another (maybe bigger/remote) studio may take
                            # it; wait → defer. NEVER errors the whole batch.
                            b["governor_note"] = f"{studio['id']}: {note}"
                            continue
                        reserve = _required_free_memory(mem) if is_local else 0.0
                    # Eligibility was calculated before the catalog/memory awaits.
                    # Recheck the physical lease so a transcription that claimed
                    # this Mac in the meantime never overlaps generation/render.
                    if studio.get("machine", "local") in busy_machines():
                        continue
                    b["governor_note"] = None
                    item = queued.pop(compatible_index)
                    _clear_worker_attempt_evidence(item)
                    item["state"] = "running"
                    item["retry_at"] = None
                    item["studio"] = studio["id"]
                    # Never let a later dispatch reconcile a previous attempt's
                    # worker id if the new POST loses its response.
                    item["studio_job_id"] = None
                    item["tries"] += 1
                    item["_reserved"] = reserve
                    _reserved["gb"] += reserve
                    _busy.add(studio["id"])
                    b["last_dispatched_at"] = time.time()
                    _log_audio_priority(b, item, studio, now)
                    asyncio.create_task(_run_item(client, b, item, studio))
                    assigned = True
            _wakeup.clear()
            try:  # idle until new work or a worker frees up (or 3s heartbeat)
                await asyncio.wait_for(_wakeup.wait(), timeout=3.0 if not assigned else 0.1)
            except asyncio.TimeoutError:
                pass
        except Exception:
            logging.getLogger("studiohub.broker").exception(
                "dispatch loop error (continuing)")
            await asyncio.sleep(3)  # the scheduler must never die


def _worker_http_error(response) -> RuntimeError:
    try:
        detail = (response.json().get("detail")
                  if "json" in response.headers.get("content-type", "")
                  else response.text)
    except (AttributeError, ValueError):
        detail = response.text or "worker request failed"
    error = RuntimeError(f"HTTP {response.status_code}: {detail}")
    error.status_code = response.status_code
    error.retryable = (response.status_code in {408, 425, 429}
                       or response.status_code >= 500)
    return error


def _worker_terminal_error(message: str) -> RuntimeError:
    error = RuntimeError(message)
    error.retryable = True
    return error


def _record_worker_progress(item: dict, progress) -> None:
    """Keep a real activity timestamp without treating an unchanged poll as progress."""
    if not isinstance(progress, (int, float)):
        return
    value = max(0.0, min(1.0, float(progress)))
    previous = item.get("progress")
    item["progress"] = value
    if previous is None or value > previous + 0.001:
        item["last_progress_at"] = time.time()


def _record_worker_execution_started_at(item: dict, job: object) -> bool:
    """Record the first trustworthy worker-side execution-start proof."""
    if item.get("execution_started_at") is not None:
        return False
    if not isinstance(job, dict) or job.get("state") not in (
            "running", "done", "error", "cancelled"):
        return False
    started_at = job.get("started_at")
    if type(started_at) not in (int, float):
        return False
    try:
        if not math.isfinite(float(started_at)) or started_at <= 0:
            return False
    except OverflowError:
        return False
    item["execution_started_at"] = started_at
    return True


def _persist_worker_execution_started_at(b: dict, item: dict, job: object) -> None:
    """Write through newly observed worker execution proof before more polling."""
    if _record_worker_execution_started_at(item, job):
        ledger.save_batch(b)


_RESOURCE_USAGE_FIELDS = {
    "sampling": {
        "interval_seconds", "samples", "started_at", "finished_at",
    },
    "host": {
        "total_gb", "available_gb_start", "minimum_available_gb",
        "available_gb_end", "maximum_used_gb", "maximum_used_percent",
        "pressure_level_start", "peak_pressure_level", "peak_pressure_raw",
        "pressure_level_end", "swap_used_gb_start", "maximum_swap_used_gb",
        "swap_used_gb_end", "swap_used_delta_gb", "swap_in_delta_bytes",
        "swap_out_delta_bytes",
    },
    "worker": {
        "rss_gb_start", "peak_rss_gb", "rss_gb_end", "peak_process_count",
    },
    "mlx": {
        "supported", "active_gb_start", "peak_active_gb", "peak_cache_gb",
        "reported_peak_gb", "active_gb_end", "cache_gb_end",
    },
    "outcome": {
        "state", "memory_failure", "restart_scheduled", "model_retained",
    },
}
_RESOURCE_USAGE_SCHEMAS = {
    "imagestudio.resource-telemetry",
    "voicestudio.resource-telemetry",
}
_RESOURCE_TEXT_FIELDS = {
    "host": {"pressure_level_start", "peak_pressure_level", "pressure_level_end"},
    "outcome": {"state"},
}
_RESOURCE_BOOL_FIELDS = {
    "mlx": {"supported"},
    "outcome": {"memory_failure", "restart_scheduled", "model_retained"},
}
_RESOURCE_INTEGER_FIELDS = {
    "sampling": {"samples"},
    "worker": {"peak_process_count"},
    "host": {"peak_pressure_raw", "swap_in_delta_bytes", "swap_out_delta_bytes"},
}
_PRESSURE_LEVELS = frozenset({
    "normal", "warning", "urgent", "critical", "unknown", "unavailable",
})
_OUTCOME_STATES = frozenset({"done", "error", "cancelled"})
_ATTEMPT_EVIDENCE_FIELDS = (
    "machine", "worker_id", "model_revision", "runtime_revision",
    "resource_usage", "error_code", "runtime_s", "duration_s", "finished_at",
)


def _clear_worker_attempt_evidence(item: dict) -> None:
    """A retry must report only the worker that owns the new attempt."""
    for field in _ATTEMPT_EVIDENCE_FIELDS:
        item.pop(field, None)
    item["error"] = None


def _record_worker_identity(item: dict, studio: dict, job: dict) -> None:
    """Keep bounded worker identity for both successful and failed attempts."""
    item["machine"] = str(studio.get("machine") or "local")[:500]
    worker_id = job.get("worker_id")
    item["worker_id"] = (
        str(worker_id).strip()[:500] if worker_id is not None
        else str(studio.get("id") or "unknown")[:500]
    )
    for field in ("model_revision", "runtime_revision"):
        value = job.get(field)
        if isinstance(value, str) and value.strip():
            item[field] = value.strip()[:500]


def _record_worker_failure(item: dict, studio: dict, job: dict, t_start: float) -> None:
    """Retain the authenticated worker evidence needed to diagnose a failure."""
    _record_worker_execution_started_at(item, job)
    _record_worker_identity(item, studio, job)
    _record_worker_resource_usage(item, job)
    code = job.get("error_code")
    if code is not None:
        item["error_code"] = _sanitize_public_error_code(code)
    runtime = job.get("runtime_s", job.get("generation_seconds", job.get("duration_seconds")))
    try:
        runtime = float(runtime) if runtime is not None else round(time.time() - t_start, 2)
    except (TypeError, ValueError):
        runtime = round(time.time() - t_start, 2)
    if not math.isfinite(runtime) or runtime < 0:
        runtime = max(0.0, round(time.time() - t_start, 2))
    item["runtime_s"] = runtime
    item["duration_s"] = runtime


def _record_worker_resource_usage(item: dict, job: dict) -> None:
    """Retain only the versioned, worker-produced telemetry contract.

    Worker payloads are authenticated but still cross a service boundary.  A
    small whitelist prevents an accidental future worker field (paths, command
    lines, environment values) from becoming customer-visible through the Hub.
    """
    raw = job.get("resource_usage")
    if not isinstance(raw, dict):
        raw = job.get("resource_telemetry")
    if not isinstance(raw, dict):
        return
    schema = raw.get("schema")
    if schema not in _RESOURCE_USAGE_SCHEMAS:
        return
    if type(raw.get("schema_version")) is not int or raw.get("schema_version") != 1:
        return
    clean = {
        "schema": schema,
        "schema_version": 1,
    }
    for section, fields in _RESOURCE_USAGE_FIELDS.items():
        values = raw.get(section)
        if not isinstance(values, dict):
            continue
        sanitized = {}
        for key, value in values.items():
            if key not in fields:
                continue
            if value is None:
                sanitized[key] = None
            elif key in _RESOURCE_BOOL_FIELDS.get(section, set()):
                if isinstance(value, bool):
                    sanitized[key] = value
            elif key in _RESOURCE_INTEGER_FIELDS.get(section, set()):
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    sanitized[key] = value
            elif key in _RESOURCE_TEXT_FIELDS.get(section, set()):
                if isinstance(value, str) and (
                    (section == "host" and value in _PRESSURE_LEVELS)
                    or (section == "outcome" and value in _OUTCOME_STATES)
                ):
                    sanitized[key] = value
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                numeric = float(value)
                if math.isfinite(numeric) and (
                    numeric >= 0 or key == "swap_used_delta_gb"
                ):
                    sanitized[key] = value
        clean[section] = sanitized
    item["resource_usage"] = clean


async def _run_item(client: httpx.AsyncClient, b: dict, item: dict, studio: dict):
    endpoint, prompt_field, artifact_kind = MODALITY[b["modality"]]
    t_start = time.time()  # wall-clock fallback for generation duration
    item["run_started"] = t_start   # surfaced live for elapsed/ETA in the UI
    item["progress"] = None         # 0..1 as reported by the studio while running
    item["last_progress_at"] = t_start
    body = dict(b["shared_params"])
    body.update(item["params"])
    body["repo"] = b["model"]
    body[prompt_field] = item["prompt"]
    if b["modality"] == "voice":
        body["client_request_id"] = f"studiohub:{b['id']}:{item['index']}"
    if item["seed"] is not None:
        body["seed"] = item["seed"]
    # Reference-image jobs use multipart. Image Studio accepts ``image`` on
    # img2img/edit; Video Studio accepts ``file`` on video2video with
    # mode=img2video. One exact source image is used per stable scene id.
    refs = body.pop("reference_images", None) if b["modality"] in ("image", "video") else None
    voice_reference_asset_id = (
        str(body.pop("voice_reference_asset_id", "") or "").strip()
        if b["modality"] == "voice" else ""
    )
    ref_mode = body.pop("ref_mode", None)
    body.pop("reference_images", None)  # never forward references as JSON
    try:
        if _expire_genstudio_batch(b):
            item["state"] = "cancelled"
            item["error"] = "GenStudio execution lease expired"
            return
        if voice_reference_asset_id:
            if body.get("voice_library_id"):
                item["state"] = "error"
                item["error_code"] = "VOICE_REFERENCE_CONFLICT"
                item["error"] = "Use either voice_library_id or voice_reference_asset_id, not both."
                return
            try:
                reference, reference_path = execution_assets.resolve_voice_reference(
                    voice_reference_asset_id
                )
            except execution_assets.ExecutionAssetError as exc:
                item["state"] = "error"
                item["error_code"] = exc.code
                item["error"] = exc.detail
                return
            if reference.get("transcript"):
                body["ref_transcript"] = reference["transcript"]
            url, headers = studio_request(
                studio, "/api/generate/txt2speech/reference"
            )
            r = await client.post(
                url,
                data={
                    "request_json": json.dumps(body, separators=(",", ":")),
                    "transcript_segments_json": json.dumps(
                        reference.get("transcript_segments") or [],
                        separators=(",", ":"),
                    ),
                    "source_sha256": reference["sha256"],
                    "reference_expires_at": str(reference["expires_at"]),
                },
                files={
                    "audio": (
                        f"reference{reference['audio_extension']}",
                        reference_path.read_bytes(),
                        reference["media_type"],
                    )
                },
                headers=headers,
                timeout=120.0,
            )
            item["voice_reference_asset_id"] = voice_reference_asset_id
        elif refs:
            entry = await _catalog_entry(studio, b["model"])
            caps = (entry or {}).get("capabilities") or []
            if b["modality"] == "video":
                mode = ref_mode or "img2video"
                supported_modes = ("img2video",)
            else:
                mode = ref_mode or ("img2img" if "img2img" in caps
                                    else ("edit" if "edit" in caps else None))
                supported_modes = ("img2img", "edit")
            if not mode or mode not in supported_modes or mode not in caps:
                item["state"] = "error"
                item["error"] = (f"model {b['model']} does not support reference "
                                 f"images (needs {'img2video' if b['modality'] == 'video' else 'img2img/edit'} capability)")
                return  # terminal — the finally block cleans up
            try:
                img_bytes, mime = await _resolve_reference(client, refs[0])
            except (ValueError, httpx.HTTPError) as e:
                item["state"] = "error"
                item["error"] = f"reference image could not be loaded: {e}"
                return
            if b["modality"] == "video":
                body["mode"] = "img2video"
                url, headers = studio_request(studio, "/api/generate/video2video")
                r = await client.post(
                    url,
                    data=_video_multipart_fields(body),
                    files={"file": (f"reference{_ext(mime)}", img_bytes, mime)},
                    headers=headers)
            else:
                url, headers = studio_request(studio, f"/api/generate/{mode}")
                r = await client.post(
                    url,
                    data=_multipart_fields(body),
                    files={"image": (f"reference{_ext(mime)}", img_bytes, mime)},
                    headers=headers)
        else:
            url, headers = studio_request(studio, endpoint)
            r = await client.post(url, json=body, headers=headers)
        if r.status_code >= 400 and voice_reference_asset_id:
            try:
                worker_detail = r.json().get("detail")
            except (ValueError, AttributeError):
                worker_detail = None
            if isinstance(worker_detail, dict) and worker_detail.get("code"):
                item["state"] = "error"
                item["error_code"] = str(worker_detail["code"])
                item["error"] = str(
                    worker_detail.get("detail") or "Voice reference preparation failed."
                )
                return
        if r.status_code >= 400:
            raise _worker_http_error(r)
        job = r.json()["job"]
        _persist_worker_execution_started_at(b, item, job)
        _record_worker_identity(item, studio, job)
        _record_worker_resource_usage(item, job)
        item["studio_job_id"] = job["id"]
        # Keep the Hub's ownership fact after this finished batch is pruned;
        # the optional Studio reporter must not have to win a poll race.
        try:
            ledger.record_activity_ownership(
                machine=str(studio.get("machine") or "local"),
                studio=str(studio.get("id") or ""), job_id=str(job["id"]),
                model=str(b.get("model") or "") or None,
            )
        except Exception:
            # Optional telemetry must never retry a worker that already
            # accepted customer work; studio_job_id remains the live fallback.
            logging.getLogger("studiohub.broker").exception(
                "Could not record optional activity ownership"
            )
        if _expire_genstudio_batch(b) or b["cancelled"]:
            await _signal_worker_cancel(client, item)
            item["state"] = "cancelled"
            item["error"] = (
                "GenStudio execution lease expired"
                if b.get("lease_expired")
                else "Cancelled by user"
            )
            return
        # poll the studio's async job until terminal
        while True:
            await asyncio.sleep(POLL_S)
            if _expire_genstudio_batch(b) or b["cancelled"]:
                await _signal_worker_cancel(client, item)
                item["state"] = "cancelled"
                item["error"] = (
                    "GenStudio execution lease expired"
                    if b.get("lease_expired")
                    else "Cancelled by user"
                )
                return
            url, headers = studio_request(studio, f"/api/generate/jobs/{job['id']}")
            jr = await client.get(
                url, headers=headers)
            if jr.status_code >= 400:
                raise _worker_http_error(jr)
            j = jr.json()["job"]
            _persist_worker_execution_started_at(b, item, j)
            _record_worker_identity(item, studio, j)
            _record_worker_resource_usage(item, j)
            state = j.get("state")
            if state in ("queued", "running"):
                _record_worker_progress(item, j.get("progress"))
                item["chunk_index"] = j.get("chunk_index")
                item["chunk_total"] = j.get("chunk_total")
                continue
            if j.get("error") or state in ("error", "cancelled"):
                _record_worker_failure(item, studio, j, t_start)
                raise _worker_terminal_error(
                    j.get("error") or f"studio job {state}"
                )
            # terminal + no error = success
            await _record_worker_success(client, b, item, studio, j, body, t_start)
            _mark_machine_success(studio)
            return
    except Exception as e:
        # The worker may have completed even though this status request lost
        # its connection. Reconcile the original job before considering a
        # retry; otherwise one image can be generated twice or reported as a
        # false failure.
        if b["cancelled"]:
            item["state"] = "cancelled"
            item["error"] = "Cancelled by user"
            return
        if await _recover_worker_job(client, b, item, studio, body, t_start):
            return
        message = str(e) or type(e).__name__
        retryable = getattr(e, "retryable", True)
        item["last_progress_at"] = time.time()
        now = time.time()
        if retryable and _is_capacity_failure(message) and not b["cancelled"]:
            # Memory pressure is a capacity wait, not a consumed generation
            # attempt. Avoid this Mac briefly so another healthy worker can
            # steal the item; if none can, keep it queued until memory clears.
            handoff = await release_idle_siblings(client, studio)
            item["tries"] = max(0, int(item.get("tries") or 0) - 1)
            item["state"] = "queued"
            item["error"] = f"waiting for capacity: {message}"
            item["retry_at"] = now + (3.0 if handoff["released"] else CAPACITY_RETRY_S)
            item.setdefault("capacity_wait_started_at", now)
            item.setdefault("avoid_machines", {})[
                studio.get("machine", "local")
            ] = now + FAILED_WORKER_AVOID_S
        elif retryable and _is_transport_failure(e, message) and not b["cancelled"]:
            # Infrastructure failures get a longer bounded healing window than
            # genuine generation errors. The stable worker job id was already
            # reconciled above, so retrying cannot duplicate a known result.
            _mark_machine_failure(studio, message)
            failures = int(item.get("infra_failures") or 0) + 1
            started = float(item.get("infra_failure_started_at") or now)
            item["infra_failures"] = failures
            item["infra_failure_started_at"] = started
            item.setdefault("avoid_machines", {})[
                studio.get("machine", "local")
            ] = now + FAILED_WORKER_AVOID_S
            within_window = now - started < INFRA_RETRY_WINDOW_S
            if failures < MAX_INFRA_TRIES and within_window:
                item["state"] = "queued"
                item["error"] = (
                    f"connection failure {failures}/{MAX_INFRA_TRIES}; "
                    f"recovering automatically: {message}"
                )
                delay_index = min(failures - 1, len(INFRA_RETRY_DELAYS_S) - 1)
                item["retry_at"] = now + INFRA_RETRY_DELAYS_S[delay_index]
            else:
                item["state"] = "error"
                item["error"] = f"connection recovery exhausted: {message}"
                item["retry_at"] = None
        elif retryable and item["tries"] < MAX_TRIES and not b["cancelled"]:
            item["state"] = "queued"  # work-stealing retry, possibly elsewhere
            item["error"] = f"try {item['tries']} failed: {message}"
            delay_index = min(item["tries"] - 1, len(RETRY_DELAYS_S) - 1)
            item["retry_at"] = time.time() + RETRY_DELAYS_S[delay_index]
        else:
            item["state"] = "error"
            item["error"] = message
            item["retry_at"] = None
    finally:
        if item["state"] in ("done", "error", "cancelled"):
            item.setdefault("machine", str(studio.get("machine") or "local")[:500])
            item.setdefault("worker_id", str(studio.get("id") or "unknown")[:500])
            if item["state"] == "error":
                item.setdefault("error_code", "WORKER_TERMINAL_ERROR")
            runtime = round(time.time() - t_start, 2)
            item.setdefault("runtime_s", runtime)
            item.setdefault("duration_s", item["runtime_s"])
            item.setdefault("finished_at", time.time())
            item["last_progress_at"] = item["finished_at"]
        _busy.discard(studio["id"])
        _reserved["gb"] = max(0.0, _reserved["gb"] - item.get("_reserved", 0.0))
        item["_reserved"] = 0.0
        await _post_item_webhook(client, b, item)   # per-scene result → client
        await _maybe_finish(client, b)
        _wakeup.set()


def start_dispatcher():
    asyncio.create_task(_dispatch_loop())


def wake_dispatcher() -> None:
    """Re-evaluate queued work after an operator changes admission policy."""
    _wakeup.set()
