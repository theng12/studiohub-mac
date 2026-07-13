"""Studio Hub KH — control plane for the KH Studio family.

Phase 1 (SPEC §9): monitoring dashboard.
  - host-aware studio registry
  - health/version poller
  - unified (pass-through) model catalog
  - host + per-studio resource monitor

The /api/health and /api/version shapes intentionally mirror the sibling
studios, so the Hub itself is monitorable by the same convention.
"""

import asyncio
import hashlib
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import (alerts, broadcast, broker, chat_jobs, fleet_ops, gateway, ledger,
               metrics, peers, recipes, transcription_jobs)
from .auth import is_loopback, load_token, make_middleware
from .control import control_studio
from .monitor import StudioMonitor
from .registry import DATA_DIR, LAUNCHER_ROOT, base_url
from .resources import host_stats, studio_process_stats

TITLE = "Studio Hub KH"
FRONTEND_DIR = Path(__file__).resolve().parents[1] / "frontend"


class UpdateRequest(BaseModel):
    studio_ids: list[str] = Field(min_length=1, max_length=100)

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


def _app_version() -> str:
    """Version of the code loaded by this process, not a later disk checkout."""
    return APP_VERSION


monitor = StudioMonitor()


@asynccontextmanager
async def lifespan(app: FastAPI):
    monitor.start()
    restored = broker.restore_batches()
    if restored:
        print(f"[hub] resumed {restored} unfinished batch(es) from hub.db")
    broker.start_dispatcher()
    transcription_restored = transcription_jobs.restore_batches()
    if transcription_restored:
        print(f"[hub] resumed {transcription_restored} transcription batch(es) from hub.db")
    transcription_jobs.start_dispatcher(monitor)
    chat_restored = chat_jobs.restore_batches()
    if chat_restored:
        print(f"[hub] resumed {chat_restored} Chat batch(es) from hub.db")
    chat_jobs.start_dispatcher(monitor)
    yield
    await chat_jobs.stop()
    await transcription_jobs.stop()
    await monitor.stop()


app = FastAPI(title=TITLE, lifespan=lifespan)

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

# Unified gateway: {HUB}/studio/{id}/{path} -> the right studio.
app.include_router(gateway.router)


# ── sibling-convention endpoints (Hub is monitorable like a studio) ────────
@app.get("/api/health")
def health():
    return {"ok": True, "version": "0.1.0", "app_version": _app_version()}


# ── Update auto-check (surfaced by the web-UI banner; mirrors the studios) ──
import threading as _threading
import time as _time
import urllib.request as _urlreq

_UPDATE_REPO = "theng12/studiohub-mac"
_update_state = {"checked_at": 0.0, "latest": None}


def _parse_ver(v):
    try:
        return tuple(int(x) for x in str(v).strip().lstrip("v").split(".")[:3])
    except Exception:
        return (0,)


def _refresh_latest_version():
    try:
        url = f"https://raw.githubusercontent.com/{_UPDATE_REPO}/main/VERSION"
        with _urlreq.urlopen(url, timeout=5) as r:
            _update_state["latest"] = r.read().decode("utf-8").strip()
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
    return {"app_version": _app_version(), "title": TITLE}


# ── canonical hub API ──────────────────────────────────────────────────────
@app.get("/api/hub/studios")
def studios():
    """Registry + live status per studio."""
    from .registry import load_labels

    labels = load_labels()
    out = []
    for s in monitor.registry:
        st = monitor.status.get(s["id"], {})
        machine = s.get("machine", "local")
        out.append({**s, "url": base_url(s),
                    "machine_label": labels.get(machine, machine), **st})
    return {"studios": out}


@app.post("/api/hub/registry/machines/{machine}/name")
def rename_machine(machine: str, body: dict):
    """Set a friendly display name for a machine (the underlying key is
    unchanged, so control/routing keep working). Works for 'local' too.
    An empty name clears the alias."""
    from .registry import set_label
    set_label(machine, body.get("name", ""))
    return {"ok": True, "machine": machine, "name": body.get("name") or machine}


@app.post("/api/hub/registry/machines/{machine}/enabled")
def set_machine_enabled_ep(machine: str, body: dict):
    """Enable/disable a machine in the fleet. A disabled machine stays
    registered and monitored but the broker sends it no jobs — use it to quiesce
    a machine before updating/restarting it. Body: {"enabled": <bool>}."""
    from .registry import set_machine_enabled
    enabled = bool(body.get("enabled", True))
    set_machine_enabled(machine, enabled)
    return {"ok": True, "machine": machine, "enabled": enabled}


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
    cloud: bool | None = Query(None, description="true=cloud lane, false=local lane"),
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
    # lanes counted before the cloud filter so both are always reported
    lanes = {"local": sum(1 for m in models if not m.get("is_cloud")),
             "cloud": sum(1 for m in models if m.get("is_cloud"))}
    if cloud is not None:
        models = [m for m in models if bool(m.get("is_cloud")) == cloud]
    return {
        "models": models,
        "count": len(models),
        "lanes": lanes,
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
    machines = {"local": {"host": host_stats(), "reachable": True,
                          "enabled": machine_enabled("local")}}
    per_studio = {}
    for s in monitor.registry:
        machine = s.get("machine", "local")
        st = monitor.status.get(s["id"], {})
        if machine == "local":
            per_studio[s["id"]] = (
                studio_process_stats(s["port"]) if st.get("status") == "up" else None)
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
            }
            per_studio[s["id"]] = (
                (peer.get("studios", {}) or {}).get(s["modality"]) if peer else None)
    return {"host": machines["local"]["host"], "machines": machines,
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
        "resources": hub_resources(local_only=False),
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
    /api/hub/summary polling if the stream drops. Auth: loopback exempt, remote
    passes ?token= (EventSource can't set headers)."""
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
    if is_loopback(request):
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
    async with httpx.AsyncClient() as client:
        results = await broadcast.broadcast_download(
            client, _pick_studios(body.get("studios")), repo, body.get("token"))
    return {"repo": repo, "results": results}


@app.post("/api/hub/broadcast/hf-token")
async def hub_broadcast_hf_token(body: dict):
    """Set one Hugging Face token on every studio (partial settings update).
    The token is passed through to each studio and never stored in the Hub."""
    token = (body.get("token") or "").strip()
    if not token:
        raise HTTPException(400, "token is required")
    import httpx
    async with httpx.AsyncClient() as client:
        results = await broadcast.broadcast_hf_token(
            client, _pick_studios(body.get("studios")), token)
    return {"results": results}  # NB: never echo the token back


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
@app.post("/api/hub/jobs")
def hub_submit_jobs(envelope: dict):
    result = broker.submit_batch(envelope)
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


@app.get("/api/hub/jobs")
def hub_list_jobs():
    return {"batches": [broker.batch_summary(b)
                        for b in sorted(broker.batches.values(),
                                        key=lambda x: -x["created_at"])]}


@app.get("/api/hub/jobs/{batch_id}")
def hub_get_batch(batch_id: str):
    b = broker.batches.get(batch_id) or ledger.load_batch(batch_id)
    if b is None:
        raise HTTPException(404, "unknown batch")
    return {**broker.batch_summary(b), "items": b["items"]}


@app.get("/api/hub/jobs/{batch_id}/items/{item_index}/artifact")
def hub_proxy_job_artifact(batch_id: str, item_index: int):
    """Stream a worker artifact through Hub so clients need only Hub auth."""
    b = broker.batches.get(batch_id) or ledger.load_batch(batch_id)
    if not b:
        raise HTTPException(404, "unknown batch")
    item = next((i for i in b["items"] if i.get("index") == item_index), None)
    if not item or item.get("state") != "done" or not item.get("artifact_url"):
        raise HTTPException(404, "artifact is not available")
    studio = next((s for s in monitor.registry if s["id"] == item.get("studio")), None)
    if not studio:
        raise HTTPException(503, "render worker is no longer registered")

    async def stream():
        import httpx
        from .peers import studio_headers
        async with httpx.AsyncClient(follow_redirects=True) as client:
            async with client.stream(
                "GET", item["artifact_url"], headers=studio_headers(studio),
                timeout=None,
            ) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes(1024 * 1024):
                    yield chunk

    headers = {}
    if item.get("bytes"):
        headers["Content-Length"] = str(item["bytes"])
    if item.get("sha256"):
        headers["X-Content-SHA256"] = item["sha256"]
    return StreamingResponse(stream(), media_type="video/mp4", headers=headers)


@app.post("/api/hub/jobs/{batch_id}/items/{item_index}/ack")
async def hub_ack_job_artifact(batch_id: str, item_index: int):
    """Start worker retention only after the main machine verifies receipt."""
    import httpx
    from .peers import studio_headers
    b = broker.batches.get(batch_id) or ledger.load_batch(batch_id)
    if not b:
        raise HTTPException(404, "unknown batch")
    item = next((i for i in b["items"] if i.get("index") == item_index), None)
    studio = next((s for s in monitor.registry if s["id"] == (item or {}).get("studio")), None)
    if not item or not studio or not item.get("studio_job_id"):
        raise HTTPException(404, "worker job is not available")
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{base_url(studio)}/api/generate/jobs/{item['studio_job_id']}/ack",
            headers=studio_headers(studio), timeout=15.0)
    if response.status_code >= 400:
        raise HTTPException(502, "render worker did not acknowledge receipt")
    item["receipt_acked_at"] = time.time()
    ledger.save_batch(b)
    return {"ok": True}


@app.delete("/api/hub/jobs/{batch_id}")
def hub_cancel_batch(batch_id: str):
    if not broker.cancel_batch(batch_id):
        raise HTTPException(404, "unknown batch")
    return {"ok": True}


# ── asset ledger ───────────────────────────────────────────────────────────
@app.get("/api/hub/assets")
def hub_assets(q: str | None = None, modality: str | None = None,
               studio: str | None = None, batch_id: str | None = None,
               sort: str = Query("newest", pattern="^(newest|oldest|name|type|studio|model)$"),
               limit: int = Query(100, ge=1, le=500)):
    return {"assets": ledger.query_assets(q, modality, studio, batch_id, limit, sort)}


@app.get("/api/hub/models")
async def hub_models(modality: str | None = None, q: str | None = None,
                     downloaded: bool | None = None, cloud: bool | None = None,
                     force: bool = False):
    """Deduped-by-repo model list with per-machine availability (Models tab).
    Reports local vs cloud as distinct lanes (never one merged number)."""
    rows = await monitor.models_by_repo(force=force)
    if modality:
        rows = [r for r in rows if r["modality"] == modality]
    if q:
        needle = q.lower()
        rows = [r for r in rows
                if needle in r["repo"].lower() or needle in r["label"].lower()]
    if downloaded is not None:
        rows = [r for r in rows if r["downloaded"] == downloaded]
    # Lane + per-provider counts are computed BEFORE the cloud filter so the UI
    # can always show both lanes even while viewing one.
    lanes = {"local": sum(1 for r in rows if not r["is_cloud"]),
             "cloud": sum(1 for r in rows if r["is_cloud"])}
    providers: dict[str, int] = {}
    for r in rows:
        if r["is_cloud"]:
            p = r.get("provider") or "cloud"
            providers[p] = providers.get(p, 0) + 1
    if cloud is not None:
        rows = [r for r in rows if r["is_cloud"] == cloud]
    return {"models": rows, "count": len(rows), "lanes": lanes, "providers": providers}


@app.get("/api/hub/transcription")
async def hub_transcription(force: bool = False):
    """Fleet-wide Whisper availability with per-machine cache status."""
    return await monitor.transcription_inventory(force=force)


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
):
    """Spool an episode upload and immediately enqueue its chapters."""
    batch, duplicate = await transcription_jobs.create_batch(
        files, item_ids, model, language, word_timestamps, label, project, episode)
    transcription_jobs.start_dispatcher(monitor)
    result = {"batch_id": batch["id"], "items": len(batch["items"]),
              "queued": sum(i["state"] == "queued" for i in batch["items"])}
    if duplicate:
        result["duplicate"] = True
    return result


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


@app.post("/api/hub/transcribe")
async def hub_transcribe(
    file: UploadFile = File(...),
    model: str = Form(...),
    language: str | None = Form(None),
    word_timestamps: bool = Form(False),
):
    """Backward-compatible one-file request, implemented through the queue."""
    item_id = Path(file.filename or "audio").stem or "audio"
    batch, _ = await transcription_jobs.create_batch(
        [file], [item_id], model, language, word_timestamps,
        "single-file-api", None, None, deduplicate=False)
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
    return {**(item.get("metadata") or {}), "srt": artifact.read_text(encoding="utf-8")}


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


@app.post("/api/hub/render-assets")
async def hub_render_asset_upload(request: Request):
    """Stream one immutable render input to the Hub and return its digest."""
    original = request.headers.get("x-file-name", "asset.bin")
    ext = Path(original).suffix.lower()
    if ext not in _RENDER_EXTENSIONS:
        raise HTTPException(415, f"unsupported render asset type: {ext or '(none)'}")
    declared = request.headers.get("content-length")
    if declared and int(declared) > _MAX_RENDER_UPLOAD_BYTES:
        raise HTTPException(413, "render asset exceeds 20 GB")
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
        final = _RENDER_UPLOADS / f"{asset_id}{ext}"
        partial.replace(final)
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    return {"asset_id": asset_id, "bytes": total, "sha256": sha256,
            "path": f"/api/hub/render-assets/{asset_id}"}


def _render_asset_path(asset_id: str) -> Path | None:
    if not asset_id.isalnum():
        return None
    return next((p for p in _RENDER_UPLOADS.glob(f"{asset_id}.*")
                 if p.is_file() and not p.name.endswith(".partial")), None)


@app.get("/api/hub/render-assets/{asset_id}")
def hub_render_asset_download(asset_id: str):
    path = _render_asset_path(asset_id)
    if not path:
        raise HTTPException(404, "render asset not found")
    return FileResponse(path, filename=path.name)


@app.delete("/api/hub/render-assets/{asset_id}")
def hub_render_asset_delete(asset_id: str):
    path = _render_asset_path(asset_id)
    if not path:
        raise HTTPException(404, "render asset not found")
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
    lane: str = Query("all", pattern="^(all|local|cloud)$",
                      description="all | local | cloud (cloud-provider generations)"),
):
    """Generation analytics: per-machine / operation-type / model counts +
    speed, plus a time-bucketed throughput series (bucket sized to the window).
    Counts span every source by default; `source`, `modality`, `machine`, and
    `lane` (local vs cloud) narrow the view (and the throughput chart) to match.
    `by_lane` in the response always reports both lanes for the current window."""
    since = time.time() - hours * 3600 if hours else None
    bucket = 300 if hours == 1 else (3600 if hours == 24 else 86400)
    result = ledger.stats(since_s=since, source=source, op=modality, machine=machine, lane=lane)
    result["timeline"] = ledger.timeline(since, bucket, source=source, op=modality,
                                          machine=machine, lane=lane)
    result["filters"] = {"source": source, "modality": modality, "machine": machine,
                         "lane": lane, "hours": hours}
    return result


# ── recipes + director ─────────────────────────────────────────────────────
@app.post("/api/hub/recipes/run")
async def hub_run_recipe(body: dict):
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


@app.get("/api/hub/fleet")
def get_fleet(request: Request):
    """Fleet-token status. The token itself is revealed only to loopback."""
    token = peers.fleet_token()
    out = {"fleet_token_set": token is not None}
    if is_loopback(request) and token:
        out["token"] = token
    return out


@app.post("/api/hub/fleet")
async def set_fleet(body: dict):
    """Save locally, optionally synchronizing and verifying every peer Hub."""
    token = str(body.get("token") or "").strip()
    if not 12 <= len(token) <= 512:
        raise HTTPException(400, "fleet credential must be 12 to 512 characters")
    sync = None
    if body.get("sync"):
        sync = await peers.sync_fleet_token(monitor.registry, monitor._client, token)
    else:
        peers.set_fleet_token(token)
        peers._cache.clear()
    return {"ok": True, "fleet_token_set": True, "sync": sync}


@app.get("/api/hub/maintenance/preflight")
def get_preflight():
    return fleet_ops.preflight_snapshot()


@app.post("/api/hub/maintenance/preflight")
async def run_fleet_preflight():
    return await fleet_ops.run_preflight(monitor)


@app.get("/api/hub/maintenance/updates")
def list_fleet_updates():
    return {"updates": fleet_ops.update_snapshot()}


@app.post("/api/hub/maintenance/updates")
async def start_fleet_updates(body: UpdateRequest):
    try:
        return fleet_ops.start_updates(monitor, body.studio_ids)
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.get("/api/hub/maintenance/updates/{job_id}")
def get_fleet_update(job_id: str):
    job = fleet_ops.update_snapshot(job_id)
    if not job:
        raise HTTPException(404, "unknown update")
    return job


@app.post("/api/hub/maintenance/self-update")
def self_update():
    """Run THIS Hub's own update.js (git pull + restart). Loopback can call it
    (the sidebar Update does the same), and the primary Hub calls it on a peer
    over the fleet (authenticated by the fleet token) to update the Studio Hub on
    an agent Mac. The Hub goes down briefly; its startup service brings it back."""
    from .control import run_hub_script
    before = _app_version()
    result = run_hub_script("update.js")
    if not result["ok"]:
        raise HTTPException(409, result["error"])
    return {"ok": True, "version_before": before, "ref": result.get("ref")}


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
        return fleet_ops.start_hub_updates(monitor, _update_state["latest"], machines)
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
            "machines": fleet_ops.hub_versions_snapshot()}


@app.post("/api/hub/maintenance/hub-versions")
async def rescan_hub_versions():
    """Re-query every agent Mac's Hub version now and cache it. Always refreshes
    the published 'latest' too, so an explicit rescan can't compare against a
    stale target (which made a newer peer look like it needed a downgrade)."""
    _refresh_latest_version()
    machines = await fleet_ops.scan_hub_versions(monitor)
    return {"latest": _update_state["latest"], "machines": machines}


@app.post("/api/hub/registry/reload")
def reload_registry():
    """Re-read studios.json after editing it — no restart needed."""
    monitor.reload_registry()
    return {"ok": True, "studios": len(monitor.registry)}


@app.delete("/api/hub/registry/machines/{machine}")
def remove_machine_route(machine: str):
    """Unregister a previously discovered machine's studios."""
    from .registry import remove_machine

    if machine == "local":
        raise HTTPException(400, "the local machine's studios can't be removed")
    removed = remove_machine(machine)
    if not removed:
        raise HTTPException(404, f"no registered studios for machine {machine!r}")
    # Drop them from live status too, not just the file.
    monitor.reload_registry()
    monitor.registry = [s for s in monitor.registry
                        if s.get("machine") != machine]
    for sid in list(monitor.status):
        if not any(s["id"] == sid for s in monitor.registry):
            del monitor.status[sid]
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
    removed = remove_studio(studio_id)
    if not removed:
        raise HTTPException(404, f"no registered studio {studio_id!r}")
    monitor.reload_registry()
    monitor.registry = [s for s in monitor.registry if s["id"] != studio_id]
    monitor.status.pop(studio_id, None)
    return {"ok": True, "removed": studio_id}


@app.post("/api/hub/registry/add")
def add_machine_manual(body: dict):
    """Pre-register a machine's studios WITHOUT probing — works while the
    machine is offline. The entries persist and turn 'up' on their own once the
    machine is reachable. `modalities` defaults to all five."""
    from .registry import (FAMILY_PORTS, add_user_entries,
                           build_machine_entries)

    host = body.get("host")
    if not host:
        raise HTTPException(400, "host is required")
    machine = body.get("machine") or host.replace(".", "-")
    modalities = body.get("modalities") or list(FAMILY_PORTS.values())
    valid = set(FAMILY_PORTS.values())
    bad = [m for m in modalities if m not in valid]
    if bad:
        raise HTTPException(400, f"unknown modalities: {bad}")
    entries = build_machine_entries(host, machine, modalities)
    added = add_user_entries(entries)
    monitor.reload_registry()
    return {"host": host, "machine": machine, "requested": modalities,
            "registered": added,
            "note": "saved — will show 'down' until the machine is reachable, "
                    "then activate automatically"}


@app.post("/api/hub/registry/discover")
async def discover_machine(body: dict):
    """Probe another Mac (LAN/Tailscale IP) for the studio family ports and
    register whatever answers. Each Mac only runs some studios — the registry
    reflects exactly what exists where."""
    import httpx

    from .registry import FAMILY_PORTS, MODALITY_EMOJI, add_user_entries

    host = body.get("host")
    if not host:
        raise HTTPException(400, "host is required (LAN or Tailscale IP)")
    machine = body.get("machine") or host.replace(".", "-")
    known = {(s["host"], s["port"]) for s in monitor.registry}
    found, entries = [], []
    async with httpx.AsyncClient() as client:
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
            if (host, port) in known:
                continue  # already registered (e.g. this Hub's own locals)
            entries.append({
                "id": f"{modality}@{machine}", "title": f"{title} ({machine})",
                "modality": modality, "host": host, "port": port,
                "machine": machine, "emoji": MODALITY_EMOJI[modality],
            })
    added = add_user_entries(entries) if entries else 0
    if added:
        monitor.reload_registry()
    return {"host": host, "machine": machine, "found": found,
            "registered": added,
            "note": None if found else
            "nothing answered — is the machine on and reachable over Tailscale?"}


# ── dashboard ──────────────────────────────────────────────────────────────
@app.get("/")
def index():
    # no-store so Pinokio's embedded webview never serves a stale build after
    # an update — the #1 cause of "I don't see my changes".
    return FileResponse(
        FRONTEND_DIR / "index.html",
        headers={"Cache-Control": "no-store, max-age=0"})
