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

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from . import broadcast, broker, gateway, ledger, metrics, peers, recipes
from .auth import is_loopback, load_token, make_middleware
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
    restored = broker.restore_batches()
    if restored:
        print(f"[hub] resumed {restored} unfinished batch(es) from hub.db")
    broker.start_dispatcher()
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

# Token auth: loopback is exempt; remote clients need the Hub token.
HUB_TOKEN = load_token()
app.middleware("http")(make_middleware(HUB_TOKEN))

# Unified gateway: {HUB}/studio/{id}/{path} -> the right studio.
app.include_router(gateway.router)


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
    if cloud is not None:
        models = [m for m in models if bool(m.get("is_cloud")) == cloud]
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
    machines = {"local": {"host": host_stats(), "reachable": True}}
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
            }
            per_studio[s["id"]] = (
                (peer.get("studios", {}) or {}).get(s["modality"]) if peer else None)
    return {"host": machines["local"]["host"], "machines": machines,
            "studios": per_studio, "fleet_token_set": peers.fleet_token() is not None,
            "ts": time.time()}


@app.get("/api/hub/summary")
def hub_summary():
    """One-shot payload for the dashboard poll loop."""
    busy = broker.busy_studios()
    studio_list = studios()["studios"]
    for s in studio_list:
        s["busy"] = s["id"] in busy  # 'generating' when up + busy
    return {
        "hub": {"title": TITLE, "app_version": _app_version()},
        "studios": studio_list,
        "resources": hub_resources(),
        "watchdog": metrics.watchdog_status(),
        "jobs": [broker.batch_summary(b) for b in broker.batches.values()],
    }


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


@app.delete("/api/hub/jobs/{batch_id}")
def hub_cancel_batch(batch_id: str):
    if not broker.cancel_batch(batch_id):
        raise HTTPException(404, "unknown batch")
    return {"ok": True}


# ── asset ledger ───────────────────────────────────────────────────────────
@app.get("/api/hub/assets")
def hub_assets(q: str | None = None, modality: str | None = None,
               studio: str | None = None, batch_id: str | None = None,
               limit: int = Query(100, ge=1, le=500)):
    return {"assets": ledger.query_assets(q, modality, studio, batch_id, limit)}


@app.get("/api/hub/models")
async def hub_models(modality: str | None = None, q: str | None = None,
                     downloaded: bool | None = None, force: bool = False):
    """Deduped-by-repo model list with per-machine availability (Models tab)."""
    rows = await monitor.models_by_repo(force=force)
    if modality:
        rows = [r for r in rows if r["modality"] == modality]
    if q:
        needle = q.lower()
        rows = [r for r in rows
                if needle in r["repo"].lower() or needle in r["label"].lower()]
    if downloaded is not None:
        rows = [r for r in rows if r["downloaded"] == downloaded]
    return {"models": rows, "count": len(rows)}


@app.post("/api/hub/assets/scan")
def hub_assets_scan():
    return ledger.scan_outputs(monitor.registry)


@app.get("/api/hub/stats")
def hub_stats(hours: int | None = Query(None, ge=1, description="limit to last N hours")):
    """Generation analytics: per-machine and per-modality counts + speed."""
    since = time.time() - hours * 3600 if hours else None
    return ledger.stats(since_s=since)


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


@app.post("/api/hub/studios/{studio_id}/{action}")
async def studio_lifecycle(studio_id: str, action: str):
    """Start or stop a local studio via Pinokio's pterm CLI. Returns
    immediately; the health poller reflects the change within seconds."""
    if action not in ("start", "stop"):
        raise HTTPException(400, "action must be 'start' or 'stop'")
    studio = next((s for s in monitor.registry if s["id"] == studio_id), None)
    if studio is None:
        raise HTTPException(404, f"unknown studio: {studio_id}")
    if studio.get("machine", "local") == "local":
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
def set_fleet(body: dict):
    """Set the shared fleet token (paste the SAME value on every Mac's Hub)."""
    token = body.get("token", "")
    peers.set_fleet_token(token)
    return {"ok": True, "fleet_token_set": bool(token.strip())}


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
