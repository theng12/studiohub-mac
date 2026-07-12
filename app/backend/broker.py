"""Job broker + Swarm Batch — pull-based worker pools per modality (SPEC §5).

An N-item batch is a work queue. Each UP studio of the right modality is a
worker slot (one concurrent generation each — heavy models on unified
memory). Free workers pull the next queued item, so faster machines naturally
do more and everyone finishes together; a failed item is requeued (max
MAX_TRIES). With one machine today the pool has one worker per modality —
the moment a second machine joins the registry, the same code fans out.

Memory governor (local models only, SPEC §7 two-lane decision): before
dispatching to a LOCAL studio, the model's min_unified_memory_gb (from that
studio's own catalog) is checked against the host's available memory; the item
waits rather than OOMing the box. Cloud models bypass the check.

Params stay opaque: item params + sharedParams merge over {repo, prompt-field}
and are forwarded verbatim to the studio's own generate endpoint.
"""

import asyncio
import base64
import logging
import time
import uuid
from pathlib import Path

import httpx

from . import ledger
from .peers import studio_headers
from .monitor import is_cached
from .registry import base_url, machine_enabled
from .resources import host_stats

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


def _studio_headers_for_url(url: str) -> dict[str, str]:
    studio = next((s for s in _monitor().registry if url.startswith(base_url(s))), None)
    return studio_headers(studio) if studio else {}


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
        r = await client.get(ref["url"], headers=_studio_headers_for_url(ref["url"]), timeout=30.0)
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
            r = await client.get(u, headers=_studio_headers_for_url(u), timeout=30.0)
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
POLL_S = 2.0
MEMORY_HEADROOM_GB = 1.0  # keep at least this much free beyond the model's need

batches: dict[str, dict] = {}
_busy: set[str] = set()  # studio ids currently running an item for us
_maintenance: set[str] = set()  # drained by fleet maintenance/update operations
_wakeup = asyncio.Event()
# Sum of size_gb reserved by in-flight LOCAL dispatches. The memory governor
# subtracts this from free RAM so two concurrent local dispatches (e.g. image +
# voice at once) don't both read the same free-RAM snapshot and OOM together.
_reserved = {"gb": 0.0}


def _local_gate(mem: dict, host: dict) -> tuple[str, str | None]:
    """Memory-governor decision for a LOCAL studio. Returns one of:
      ("skip", note) — this machine can't run the model at all → try another
                       studio (a bigger remote may qualify); never errors the batch
      ("wait", note) — could run, but not enough free RAM right now → defer
      ("run",  None) — clear to dispatch
    Remote studios bypass this entirely (paced by their own backend)."""
    min_total = mem.get("min_total")
    if min_total and host["total_gb"] < min_total:
        return ("skip", f"needs a ~{min_total}GB machine; this one has "
                        f"{host['total_gb']}GB — trying other machines")
    need_free = (mem.get("size") or 0) + MEMORY_HEADROOM_GB
    effective_free = host["available_gb"] - _reserved["gb"]
    if effective_free < need_free:
        return ("wait", f"waiting for memory: needs ~{need_free:.1f}GB, "
                        f"~{max(0.0, effective_free):.1f}GB free")
    return ("run", None)


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
    if not envelope.get("model"):
        return {"error": "model (repo) is required"}
    batch_id = uuid.uuid4().hex[:10]
    batches[batch_id] = {
        "id": batch_id,
        "modality": modality,
        "model": envelope["model"],
        "shared_params": envelope.get("sharedParams") or {},
        "routing": envelope.get("routing", "pool"),
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
    return {
        by_id[sid].get("machine", "local")
        for sid in _busy
        if sid in by_id
    }


def set_maintenance(studio_id: str, enabled: bool):
    if enabled:
        _maintenance.add(studio_id)
    else:
        _maintenance.discard(studio_id)
        _wakeup.set()


def _recent_avg(modality: str, model: str, limit: int = 50) -> float | None:
    """Average completed-item duration for this (modality, model) across ALL
    batches — so even a 1-item batch gets an ETA from the model's track record,
    not just from its own (nonexistent) completed siblings."""
    durs = []
    for b in batches.values():
        if b["modality"] != modality or b["model"] != model:
            continue
        for i in b["items"]:
            if i.get("state") == "done" and isinstance(i.get("duration_s"), (int, float)):
                durs.append(i["duration_s"])
    durs = durs[-limit:]
    return round(sum(durs) / len(durs), 1) if durs else None


def batch_summary(b: dict) -> dict:
    items = b["items"]
    states = [i["state"] for i in items]
    now = time.time()
    # ETA basis: this batch's own completed items if any, else the model's recent
    # average across every batch (so single-item jobs still get an estimate).
    done_durs = [i["duration_s"] for i in items
                 if i.get("state") == "done" and isinstance(i.get("duration_s"), (int, float))]
    avg_s = (round(sum(done_durs) / len(done_durs), 1) if done_durs
             else _recent_avg(b["modality"], b["model"]))
    # per-item live detail for whatever is running right now (machine tag + progress)
    running_items = []
    for i in items:
        if i.get("state") != "running":
            continue
        sid = i.get("studio") or ""
        machine = sid.split("@", 1)[1] if "@" in sid else "local"
        started = i.get("run_started")
        elapsed = round(now - started, 1) if started else None
        running_items.append({
            "index": i.get("index"),
            "studio": sid,                 # e.g. "image@macmini-m1-01" or "image"
            "machine": machine,            # "macmini-m1-01" or "local"
            "progress": i.get("progress"),  # 0..1 or None
            "elapsed_s": elapsed,
        })
    return {
        "id": b["id"], "modality": b["modality"], "model": b["model"],
        "created_at": b["created_at"], "cancelled": b["cancelled"],
        "governor_note": b.get("governor_note"),
        "label": b.get("label"),
        "total": len(states),
        "queued": states.count("queued"),
        "running": states.count("running"),
        "done": states.count("done"),
        "error": states.count("error"),
        "cancelled_items": states.count("cancelled"),
        "avg_s": avg_s,
        "running_items": running_items,
    }


def cancel_batch(batch_id: str) -> bool:
    b = batches.get(batch_id)
    if b is None:
        return False
    b["cancelled"] = True
    for it in b["items"]:
        if it["state"] == "queued":
            it["state"] = "cancelled"
    ledger.save_batch(b)
    return True


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
            "artifact_url": item.get("artifact_url"),
            "artifact_path": item.get("artifact_path"),
            "asset_id": item.get("asset_id"),
            "duration_s": item.get("duration_s"),
            "error": item.get("error"),
            # running batch tally so the client can show n/N without a poll
            "done": sum(1 for i in b["items"] if i["state"] == "done"),
            "total": len(b["items"]),
        }, timeout=10.0)
    except httpx.HTTPError:
        pass  # client unreachable — the item is still in the batch/poll + ledger


async def _maybe_finish(client: httpx.AsyncClient, b: dict):
    """Persist state; when the batch just reached a terminal state, alert on any
    failures and fire the client's webhook (Story Studio et al) once."""
    ledger.save_batch(b)
    if b.get("done_notified"):
        return
    states = {i["state"] for i in b["items"]}
    if states & {"queued", "running"}:
        return  # not terminal yet
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
                "items": [{k: it[k] for k in
                           ("index", "state", "artifact_url",
                            "artifact_path", "asset_id", "error")}
                          for it in b["items"]],
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
        if (s["modality"] != modality or s["id"] in _busy
                or s["id"] in _maintenance or machine in leased_machines):
            continue
        # a machine the operator has disabled stays monitored but takes no jobs
        if not machine_enabled(s.get("machine", "local")):
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


def _queued_batches() -> list[dict]:
    """Queued work in priority/FIFO order; running work is never preempted."""
    return sorted(
        batches.values(),
        key=lambda b: (MODALITY_PRIORITY.get(b["modality"], 10),
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
                queued = [i for i in b["items"] if i["state"] == "queued"]
                if not queued:
                    continue
                for studio in _eligible_studios(b["modality"], b["routing"]):
                    if not queued:
                        break
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
                    # ── memory governor (LOCAL studios only; remotes bypass) ──
                    is_local = studio.get("machine", "local") == "local"
                    mem = None if entry.get("is_cloud") else {
                        "min_total": entry.get("min_unified_memory_gb"),
                        "size": entry.get("size_gb")}
                    reserve = 0.0
                    if mem is not None and is_local:
                        decision, note = _local_gate(mem, host_stats())
                        if decision != "run":
                            # skip → another (maybe bigger/remote) studio may take
                            # it; wait → defer. NEVER errors the whole batch.
                            b["governor_note"] = f"{studio['id']}: {note}"
                            continue
                        reserve = mem.get("size") or 0.0
                    b["governor_note"] = None
                    item = queued.pop(0)
                    item["state"] = "running"
                    item["studio"] = studio["id"]
                    item["tries"] += 1
                    item["_reserved"] = reserve
                    _reserved["gb"] += reserve
                    _busy.add(studio["id"])
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


async def _run_item(client: httpx.AsyncClient, b: dict, item: dict, studio: dict):
    endpoint, prompt_field, artifact_kind = MODALITY[b["modality"]]
    t_start = time.time()  # wall-clock fallback for generation duration
    item["run_started"] = t_start   # surfaced live for elapsed/ETA in the UI
    item["progress"] = None         # 0..1 as reported by the studio while running
    body = dict(b["shared_params"])
    body.update(item["params"])
    body["repo"] = b["model"]
    body[prompt_field] = item["prompt"]
    if item["seed"] is not None:
        body["seed"] = item["seed"]
    # Reference-image (img2img / edit) — image modality only. References live in
    # the item params; forward reference_images[0] as multipart to the studio's
    # img2img/edit route (single-ref: extra references are ignored — the client
    # pre-selects its primary anchor).
    refs = body.pop("reference_images", None) if b["modality"] == "image" else None
    ref_mode = body.pop("ref_mode", None)
    body.pop("reference_images", None)  # never forward as JSON on the txt2img path
    try:
        if refs:
            entry = await _catalog_entry(studio, b["model"])
            caps = (entry or {}).get("capabilities") or []
            mode = ref_mode or ("img2img" if "img2img" in caps
                                else ("edit" if "edit" in caps else None))
            if not mode or mode not in caps:
                item["state"] = "error"
                item["error"] = (f"model {b['model']} does not support reference "
                                 f"images (needs img2img/edit capability)")
                return  # terminal — the finally block cleans up
            try:
                img_bytes, mime = await _resolve_reference(client, refs[0])
            except (ValueError, httpx.HTTPError) as e:
                item["state"] = "error"
                item["error"] = f"reference image could not be loaded: {e}"
                return
            r = await client.post(
                f"{base_url(studio)}/api/generate/{mode}",
                data=_multipart_fields(body),
                files={"image": (f"reference{_ext(mime)}", img_bytes, mime)},
                headers=studio_headers(studio))
        else:
            r = await client.post(f"{base_url(studio)}{endpoint}", json=body,
                                  headers=studio_headers(studio))
        if r.status_code >= 400:
            detail = (r.json().get("detail")
                      if "json" in r.headers.get("content-type", "") else r.text)
            raise RuntimeError(f"HTTP {r.status_code}: {detail}")
        job = r.json()["job"]
        item["studio_job_id"] = job["id"]
        # poll the studio's async job until terminal
        while True:
            await asyncio.sleep(POLL_S)
            jr = await client.get(
                f"{base_url(studio)}/api/generate/jobs/{job['id']}",
                headers=studio_headers(studio))
            j = jr.json()["job"]
            state = j.get("state")
            if state in ("queued", "running"):
                p = j.get("progress")
                if isinstance(p, (int, float)):
                    item["progress"] = max(0.0, min(1.0, float(p)))
                if b["cancelled"]:
                    await client.delete(
                        f"{base_url(studio)}/api/generate/jobs/{job['id']}",
                        headers=studio_headers(studio))
                    item["state"] = "cancelled"
                    return
                continue
            if j.get("error") or state in ("error", "cancelled"):
                raise RuntimeError(j.get("error") or f"studio job {state}")
            # terminal + no error = success
            item["artifact_path"] = j.get("output_path")
            item["artifact_url"] = (
                f"{base_url(studio)}{j['output_url']}" if j.get("output_url") else None)
            item["state"] = "done"
            item["sha256"] = j.get("sha256")
            item["bytes"] = j.get("bytes")
            item["encoder"] = j.get("encoder")
            # Prefer the studio's own generation time; fall back to wall-clock.
            duration = j.get("duration_seconds")
            if duration is None:
                duration = round(time.time() - t_start, 2)
            item["duration_s"] = duration
            item["asset_id"] = ledger.record_asset(
                source="job", modality=b["modality"], studio=studio["id"],
                machine=studio.get("machine", "local"), model=b["model"],
                seed=j.get("resolved_seed") or item["seed"],
                prompt=item["prompt"], params=body,
                artifact_path=item["artifact_path"],
                artifact_url=item["artifact_url"],
                batch_id=b["id"], item_index=item["index"],
                duration_s=duration,
            )
            return
    except Exception as e:
        if item["tries"] < MAX_TRIES and not b["cancelled"]:
            item["state"] = "queued"  # work-stealing retry, possibly elsewhere
            item["error"] = f"try {item['tries']} failed: {e}"
        else:
            item["state"] = "error"
            item["error"] = str(e)
    finally:
        _busy.discard(studio["id"])
        _reserved["gb"] = max(0.0, _reserved["gb"] - item.get("_reserved", 0.0))
        item["_reserved"] = 0.0
        await _post_item_webhook(client, b, item)   # per-scene result → client
        await _maybe_finish(client, b)
        _wakeup.set()


def start_dispatcher():
    asyncio.create_task(_dispatch_loop())
