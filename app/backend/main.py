"""Studio Hub KH — control plane for the KH Studio family.

Phase 1 (SPEC §9): monitoring dashboard.
  - host-aware studio registry
  - health/version poller
  - unified (pass-through) model catalog
  - host + per-studio resource monitor

The /api/health and /api/version shapes intentionally mirror the sibling
studios, so the Hub itself is monitorable by the same convention.
"""

import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from .control import control_studio
from .monitor import StudioMonitor
from .registry import LAUNCHER_ROOT, base_url
from .resources import host_stats, studio_process_stats

TITLE = "Studio Hub KH"
FRONTEND_DIR = Path(__file__).resolve().parents[1] / "frontend"


def _app_version() -> str:
    try:
        return (LAUNCHER_ROOT / "VERSION").read_text().strip()
    except OSError:
        return "0.0.0"


monitor = StudioMonitor()


@asynccontextmanager
async def lifespan(app: FastAPI):
    monitor.start()
    yield
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


# ── sibling-convention endpoints (Hub is monitorable like a studio) ────────
@app.get("/api/health")
def health():
    return {"ok": True, "version": "0.1.0", "app_version": _app_version()}


@app.get("/api/version")
def version():
    return {"app_version": _app_version(), "title": TITLE}


# ── canonical hub API ──────────────────────────────────────────────────────
@app.get("/api/hub/studios")
def studios():
    """Registry + live status per studio."""
    out = []
    for s in monitor.registry:
        st = monitor.status.get(s["id"], {})
        out.append({**s, "url": base_url(s), **st})
    return {"studios": out}


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
        # `cache` is each studio's downloaded-state field (verbatim pass-through).
        models = [m for m in models if bool(m.get("cache")) == downloaded]
    if cloud is not None:
        models = [m for m in models if bool(m.get("is_cloud")) == cloud]
    return {
        "models": models,
        "count": len(models),
        "total_unfiltered": agg["total"],
        "per_studio": agg["per_studio"],
    }


@app.get("/api/hub/resources")
def hub_resources():
    """Host memory/CPU + per-studio process stats (local studios only —
    remote machines report their own resources via their own Hub later)."""
    per_studio = {}
    for s in monitor.registry:
        st = monitor.status.get(s["id"], {})
        is_local = s.get("machine", "local") == "local" and s["host"] in (
            "127.0.0.1", "localhost", "0.0.0.0",
        )
        if is_local and st.get("status") == "up":
            per_studio[s["id"]] = studio_process_stats(s["port"])
        else:
            per_studio[s["id"]] = None
    return {"host": host_stats(), "studios": per_studio, "ts": time.time()}


@app.get("/api/hub/summary")
def hub_summary():
    """One-shot payload for the dashboard poll loop."""
    return {
        "hub": {"title": TITLE, "app_version": _app_version()},
        "studios": studios()["studios"],
        "resources": hub_resources(),
    }


@app.post("/api/hub/studios/{studio_id}/{action}")
async def studio_lifecycle(studio_id: str, action: str):
    """Start or stop a local studio via Pinokio's pterm CLI. Returns
    immediately; the health poller reflects the change within seconds."""
    if action not in ("start", "stop"):
        raise HTTPException(400, "action must be 'start' or 'stop'")
    studio = next((s for s in monitor.registry if s["id"] == studio_id), None)
    if studio is None:
        raise HTTPException(404, f"unknown studio: {studio_id}")
    result = control_studio(studio, action)
    if not result["ok"]:
        raise HTTPException(409, result["error"])
    # Re-poll soon so the dashboard sees the transition quickly.
    await monitor.poll_all()
    return result


@app.post("/api/hub/registry/reload")
def reload_registry():
    """Re-read studios.json after editing it — no restart needed."""
    monitor.reload_registry()
    return {"ok": True, "studios": len(monitor.registry)}


# ── dashboard ──────────────────────────────────────────────────────────────
@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.html")
