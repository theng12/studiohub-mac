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
import time
import uuid
from pathlib import Path

import httpx

from . import ledger
from .monitor import is_cached
from .registry import base_url
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
        r = await client.get(ref["url"], timeout=30.0)
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
            r = await client.get(u, timeout=30.0)
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
}

MAX_TRIES = 3
POLL_S = 2.0
MEMORY_HEADROOM_GB = 1.0  # keep at least this much free beyond the model's need

batches: dict[str, dict] = {}
_busy: set[str] = set()  # studio ids currently running an item for us
_wakeup = asyncio.Event()


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


def batch_summary(b: dict) -> dict:
    states = [i["state"] for i in b["items"]]
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


async def _maybe_finish(client: httpx.AsyncClient, b: dict):
    """Persist state; when the batch just reached a terminal state, fire the
    client's webhook (Story Studio et al) once, best-effort."""
    ledger.save_batch(b)
    if b.get("webhook") and not b.get("webhook_sent"):
        states = {i["state"] for i in b["items"]}
        if not (states & {"queued", "running"}):
            b["webhook_sent"] = True
            ledger.save_batch(b)
            try:
                await client.post(b["webhook"], json={
                    **batch_summary(b),
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
    for s in mon.registry:
        if routing.startswith("studio:") and s["id"] != routing.split(":", 1)[1]:
            continue
        if s["modality"] != modality or s["id"] in _busy:
            continue
        if mon.status.get(s["id"], {}).get("status") == "up":
            out.append(s)
    return out


async def _dispatch_loop():
    """The scheduler: match queued items to free studios, forever."""
    client = httpx.AsyncClient(timeout=httpx.Timeout(connect=5, read=60, write=30, pool=5))
    while True:
        try:
            assigned = False
            for b in list(batches.values()):
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
                    # ── memory governor (local models only) ──
                    mem = None if entry.get("is_cloud") else {
                        "min_total": entry.get("min_unified_memory_gb"),
                        "size": entry.get("size_gb")}
                    if mem is not None and studio.get("machine", "local") == "local":
                        host = host_stats()
                        if mem["min_total"] and host["total_gb"] < mem["min_total"]:
                            # machine can never run this — fail fast, don't wait
                            for it in queued:
                                it["state"] = "error"
                                it["error"] = (f"model needs a {mem['min_total']}GB machine; "
                                               f"host has {host['total_gb']}GB")
                            queued.clear()
                            ledger.save_batch(b)
                            break
                        need_free = (mem["size"] or 0) + MEMORY_HEADROOM_GB
                        if host["available_gb"] < need_free:
                            b["governor_note"] = (
                                f"waiting for memory: needs ~{need_free:.1f}GB free, "
                                f"{host['available_gb']:.1f}GB available")
                            continue  # defer rather than OOM
                    b["governor_note"] = None
                    item = queued.pop(0)
                    item["state"] = "running"
                    item["studio"] = studio["id"]
                    item["tries"] += 1
                    _busy.add(studio["id"])
                    asyncio.create_task(_run_item(client, b, item, studio))
                    assigned = True
            _wakeup.clear()
            try:  # idle until new work or a worker frees up (or 3s heartbeat)
                await asyncio.wait_for(_wakeup.wait(), timeout=3.0 if not assigned else 0.1)
            except asyncio.TimeoutError:
                pass
        except Exception:
            await asyncio.sleep(3)  # the scheduler must never die


async def _run_item(client: httpx.AsyncClient, b: dict, item: dict, studio: dict):
    endpoint, prompt_field, artifact_kind = MODALITY[b["modality"]]
    t_start = time.time()  # wall-clock fallback for generation duration
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
                files={"image": (f"reference{_ext(mime)}", img_bytes, mime)})
        else:
            r = await client.post(f"{base_url(studio)}{endpoint}", json=body)
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
                f"{base_url(studio)}/api/generate/jobs/{job['id']}")
            j = jr.json()["job"]
            state = j.get("state")
            if state in ("queued", "running"):
                if b["cancelled"]:
                    await client.delete(
                        f"{base_url(studio)}/api/generate/jobs/{job['id']}")
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
        await _maybe_finish(client, b)
        _wakeup.set()


def start_dispatcher():
    asyncio.create_task(_dispatch_loop())
