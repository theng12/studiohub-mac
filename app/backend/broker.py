"""Job broker + Swarm Batch — pull-based worker pools per modality (SPEC §5).

An N-item batch is a work queue. Each UP studio of the right modality is a
worker slot (one concurrent generation each — heavy models on unified
memory). Free workers pull the next queued item, so faster machines naturally
do more and everyone finishes together; a failed item is requeued (max
MAX_TRIES). With one machine today the pool has one worker per modality —
the moment a second machine joins the registry, the same code fans out.

Memory governor (local models only, SPEC §7 two-lane decision): before
dispatching to a local or connected remote studio, the stricter of the
studio's catalog requirement and Hub production policy is checked against the
host's available memory; the item waits rather than OOMing the box. Cloud
models bypass the check.

Params stay opaque: item params + sharedParams merge over {repo, prompt-field}
and are forwarded verbatim to the studio's own generate endpoint.
"""

import asyncio
import base64
import json
import logging
import time
import uuid
from pathlib import Path

import httpx

from . import artifact_metadata, ledger, peers, shared_voices
from .peers import studio_request
from .monitor import is_cached, is_cloud_lane
from .registry import base_url, machine_enabled
from .resources import host_stats
from .workload_policy import required_total_memory_gb

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

MODALITY_PRIORITY = {"render": 0}

MAX_TRIES = 3
MAX_INFRA_TRIES = 8
INFRA_RETRY_WINDOW_S = 30 * 60
POLL_S = 2.0
RECOVERY_WINDOW_S = 120.0  # cover a full slow M1 generation after a dropped connection
RETRY_DELAYS_S = (3.0, 10.0)
INFRA_RETRY_DELAYS_S = (5.0, 15.0, 30.0, 60.0, 120.0, 300.0, 300.0)
CAPACITY_RETRY_S = 30.0
FAILED_WORKER_AVOID_S = 90.0
MACHINE_FAILURE_THRESHOLD = 2
MACHINE_COOLDOWN_S = 120.0
MAX_BATCH_ITEMS = 1000
MAX_BATCH_JSON_BYTES = 25 * 1024 * 1024
MEMORY_HEADROOM_GB = 1.0  # keep at least this much free beyond the model's need

batches: dict[str, dict] = {}
_busy: set[str] = set()  # studio ids currently running an item for us
_maintenance: set[str] = set()  # drained by fleet maintenance/update operations
_external_machine_leases: dict[str, str] = {}
# machine -> recent transport failures and an optional circuit-breaker cooldown.
# This is intentionally process-local: after a Hub restart every worker must
# answer health again before it becomes eligible, which is a clean circuit reset.
_machine_protection: dict[str, dict] = {}
_wakeup = asyncio.Event()
# Sum of size_gb reserved by in-flight LOCAL dispatches. The memory governor
# subtracts this from free RAM so two concurrent local dispatches (e.g. image +
# voice at once) don't both read the same free-RAM snapshot and OOM together.
_reserved = {"gb": 0.0}


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
    need_free = (mem.get("size") or 0) + MEMORY_HEADROOM_GB
    effective_free = host["available_gb"] - reserved_gb
    if effective_free < need_free:
        return ("wait", f"waiting for memory: needs ~{need_free:.1f}GB, "
                        f"~{max(0.0, effective_free):.1f}GB free")
    return ("run", None)


def _local_gate(mem: dict, host: dict) -> tuple[str, str | None]:
    """Compatibility wrapper for the local unified-memory reservation."""
    return _memory_gate(mem, host, _reserved["gb"])


def _host_for_studio(studio: dict) -> dict | None:
    machine = studio.get("machine", "local")
    if machine == "local":
        return host_stats()
    peer = peers.cached(machine) or {}
    host = peer.get("host")
    return host if isinstance(host, dict) else None


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
    """The studio's own catalog entry for a model (verbatim, per SPEC §6.2).
    Carries cache state for model-aware dispatch and memory facts for the
    governor: min_unified_memory_gb = 'needs ≥N GB TOTAL machine' (capability),
    size_gb ≈ incremental footprint (what must fit in FREE memory)."""
    catalog = await _monitor().get_catalog(studio)
    for m in (catalog or {}).get("models", []):
        if m.get("repo") == model or model in (m.get("aliases") or []):
            return m
    return None


def restore_batches():
    """Reload unfinished batches from hub.db after a Hub restart. Items that
    were mid-flight ('running') go back to 'queued' — their studio-side job is
    orphaned but the work is simply redone (generation is idempotent-enough;
    the ledger keys on the new artifact)."""
    for b in ledger.load_unfinished_batches():
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
    routing = str(envelope.get("routing") or "pool")
    if routing not in {"pool", "remote"} and not (
        routing.startswith("studio:") and routing.split(":", 1)[1]
    ):
        return {"error": "routing must be pool, remote, or studio:<id>"}
    batch_id = uuid.uuid4().hex[:10]
    batches[batch_id] = {
        "id": batch_id,
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
    _wakeup.set()
    return {"batch_id": batch_id, "items": len(items_in)}


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
    try:
        await client.post(url, json={
            "batch_id": b["id"], "label": b.get("label"),
            "index": item["index"], "state": item["state"],
            "studio": sid, "machine": sid.split("@", 1)[1] if "@" in sid else "local",
            "artifact_url": hub_artifact_url(b, item),
            "asset_id": item.get("asset_id"),
            "runtime_s": item.get("runtime_s", item.get("duration_s")),
            "duration_s": item.get("runtime_s", item.get("duration_s")),  # legacy alias
            "terminal_result": terminal_result(b, item),
            "error": item.get("error"),
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
    if item.get("state") != "done":
        return None
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
        "runtime_s": item.get("runtime_s", item.get("duration_s")),
        # Kept only for callers that predate runtime_s. It is runtime, never
        # decoded media duration.
        "duration_s": item.get("runtime_s", item.get("duration_s")),
    }


def public_item(b: dict, item: dict) -> dict:
    """Return a public job item without worker-local paths or worker URLs."""
    result = {k: v for k, v in item.items()
              if k not in {"artifact_path", "worker_artifact_url"}}
    if item.get("state") == "done":
        result["artifact_url"] = hub_artifact_url(b, item)
        result["terminal_result"] = terminal_result(b, item)
    return result


async def _cache_voice_artifact_metadata(client: httpx.AsyncClient, item: dict,
                                         studio: dict, worker_url: str,
                                         expected_bytes, expected_sha256) -> None:
    """Fetch and decode a terminal voice artifact once, then persist its facts.

    A metadata failure never retries an already-completed generation: the
    result remains available but is marked unverified and therefore unbillable.
    """
    try:
        url, headers = studio_request(studio, worker_url)
        response = await client.get(url, headers=headers, timeout=60.0)
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
    if item.get("state") == "done" and item.get("asset_id"):
        return  # terminal polling/recovery is idempotent
    item["artifact_path"] = job.get("output_path")
    worker_url = (
        f"{base_url(studio)}{job['output_url']}" if job.get("output_url") else None)
    item["worker_artifact_url"] = worker_url
    # Kept internally for old ledger/reference behavior; API readers are
    # normalized by public_item(), which returns the stable Hub proxy URL.
    item["artifact_url"] = worker_url
    item["state"] = "done"
    item["encoder"] = job.get("encoder")
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
        is_cloud=item.get("is_cloud", False),
    )


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
        try:
            url, headers = studio_request(studio, f"/api/generate/jobs/{job_id}")
            jr = await client.get(
                url, headers=headers, timeout=10.0)
            if jr.status_code >= 400:
                return False  # 404/4xx means the worker no longer has the job
            job = jr.json().get("job") or {}
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


def _uses_local_elevenlabs_gateway(modality: str, model: str) -> bool:
    """ElevenLabs secrets and account quotas live only on the Hub Mac.

    Remote Voice Studios remain available for local TTS engines. Cloud
    ElevenLabs batches always wait for the local Voice Studio gateway, which
    owns account selection, per-account voice IDs, and safe paid-call recovery.
    """
    return modality == "voice" and str(model).startswith("provider:elevenlabs:")


def _eligible_studios(modality: str, routing: str, model: str = "") -> list[dict]:
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
        if _uses_local_elevenlabs_gateway(modality, model) and machine != "local":
            continue
        if (s["modality"] != modality or s["id"] in _busy
                or s["id"] in _maintenance or machine in leased_machines):
            continue
        # a machine the operator has disabled stays monitored but takes no jobs
        if not machine_enabled(s.get("machine", "local")):
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


def _queued_batches() -> list[dict]:
    """Queued work in priority/fair-turn order; running work is never preempted."""
    return sorted(
        batches.values(),
        key=lambda b: (MODALITY_PRIORITY.get(b["modality"], 10),
                       b.get("last_dispatched_at", 0),
                       b.get("created_at", 0)),
    )


async def _dispatch_loop():
    """The scheduler: match queued items to free studios, forever."""
    client = httpx.AsyncClient(timeout=httpx.Timeout(connect=5, read=60, write=30, pool=5))
    while True:
        try:
            assigned = False
            for b in _queued_batches():
                if b["cancelled"]:
                    continue
                now = time.time()
                queued = [i for i in b["items"] if i["state"] == "queued"
                          and (i.get("retry_at") or 0) <= now]
                if not queued:
                    continue
                eligible = _eligible_studios(
                    b["modality"], b["routing"], b.get("model", ""),
                )
                if not eligible and b.get("routing") == "remote":
                    b["governor_note"] = (
                        "Waiting for an online remote worker; this Hub Mac is intentionally excluded"
                    )
                elif not eligible and _uses_local_elevenlabs_gateway(
                    b["modality"], b.get("model", ""),
                ):
                    b["governor_note"] = (
                        "Waiting for the local Voice Studio ElevenLabs gateway "
                        "on this Hub Mac"
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
                    mem = None if entry.get("is_cloud") else {
                        # A worker catalog is a technical capability, while
                        # this is the customer-service admission policy.  For
                        # example, Qwen3 standard voice is intentionally kept
                        # on 16 GB Macs; 8 GB Macs remain free for image work.
                        "min_total": required_total_memory_gb(b["model"], entry),
                        "size": entry.get("size_gb")}
                    reserve = 0.0
                    host = _host_for_studio(studio) if mem is not None else None
                    if mem is not None and host:
                        decision, note = _memory_gate(
                            mem, host, _reserved["gb"] if is_local else 0.0)
                        if decision != "run":
                            # skip → another (maybe bigger/remote) studio may take
                            # it; wait → defer. NEVER errors the whole batch.
                            b["governor_note"] = f"{studio['id']}: {note}"
                            continue
                        reserve = mem.get("size") or 0.0
                    # Eligibility was calculated before the catalog/memory awaits.
                    # Recheck the physical lease so a transcription that claimed
                    # this Mac in the meantime never overlaps generation/render.
                    if studio.get("machine", "local") in busy_machines():
                        continue
                    b["governor_note"] = None
                    item = queued.pop(compatible_index)
                    item["state"] = "running"
                    item["retry_at"] = None
                    item["studio"] = studio["id"]
                    # Never let a later dispatch reconcile a previous attempt's
                    # worker id if the new POST loses its response.
                    item["studio_job_id"] = None
                    item["tries"] += 1
                    item["is_cloud"] = is_cloud_lane(entry.get("is_cloud"), b["modality"])
                    item["_reserved"] = reserve
                    _reserved["gb"] += reserve
                    _busy.add(studio["id"])
                    b["last_dispatched_at"] = time.time()
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
    # Voice Studio uses this exact class prefix when a paid ElevenLabs call may
    # have completed but could not be uniquely recovered. Retrying the batch
    # would risk a duplicate charge, so the Hub must preserve the terminal state.
    error.retryable = not message.startswith("ProviderResultUncertain:")
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
    ref_mode = body.pop("ref_mode", None)
    body.pop("reference_images", None)  # never forward references as JSON
    try:
        if refs:
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
        if r.status_code >= 400:
            raise _worker_http_error(r)
        job = r.json()["job"]
        item["studio_job_id"] = job["id"]
        if b["cancelled"]:
            await _signal_worker_cancel(client, item)
            item["state"] = "cancelled"
            item["error"] = "Cancelled by user"
            return
        # poll the studio's async job until terminal
        while True:
            await asyncio.sleep(POLL_S)
            if b["cancelled"]:
                await _signal_worker_cancel(client, item)
                item["state"] = "cancelled"
                item["error"] = "Cancelled by user"
                return
            url, headers = studio_request(studio, f"/api/generate/jobs/{job['id']}")
            jr = await client.get(
                url, headers=headers)
            if jr.status_code >= 400:
                raise _worker_http_error(jr)
            j = jr.json()["job"]
            state = j.get("state")
            if state in ("queued", "running"):
                _record_worker_progress(item, j.get("progress"))
                continue
            if j.get("error") or state in ("error", "cancelled"):
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
            item["tries"] = max(0, int(item.get("tries") or 0) - 1)
            item["state"] = "queued"
            item["error"] = f"waiting for capacity: {message}"
            item["retry_at"] = now + CAPACITY_RETRY_S
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
