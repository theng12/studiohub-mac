"""Fleet preflight and one-at-a-time drained Studio updates."""

from __future__ import annotations

import asyncio
import shutil
import time
import uuid

import httpx

from . import broker, peers
from .control import PINOKIO_HOME, control_studio, run_hub_script, run_studio_script
from .registry import base_url
from .resources import host_stats

PREFLIGHT_TIMEOUT = 12.0
UPDATE_TIMEOUT = 20 * 60
DRAIN_TIMEOUT = 30 * 60

_preflight = {"ran_at": None, "status": "never", "studios": []}
_updates: dict[str, dict] = {}
_hub_updates: dict[str, dict] = {}


def _downloaded(catalog: dict) -> tuple[int, int]:
    models = catalog.get("models") or []
    ready = sum(1 for m in models if m.get("is_cloud") or
                (m.get("cache") or {}).get("state") == "cached")
    return len(models), ready


def _diag_state(data: dict) -> str:
    if data.get("ok") is False or data.get("available") is False:
        return "warn"
    engines = data.get("engines")
    if isinstance(engines, dict) and engines:
        values = list(engines.values())
        if not any(bool(v.get("available") or v.get("ready")) for v in values if isinstance(v, dict)):
            return "warn"
    return "pass"


async def run_preflight(monitor) -> dict:
    global _preflight
    rows = await asyncio.gather(*(_preflight_one(monitor, s) for s in monitor.registry))
    status = "fail" if any(r["status"] == "fail" for r in rows) else (
        "warn" if any(r["status"] == "warn" for r in rows) else "pass")
    _preflight = {"ran_at": time.time(), "status": status, "studios": rows}
    return _preflight


async def _preflight_one(monitor, studio: dict) -> dict:
    sid = studio["id"]
    checks = []
    health = monitor.status.get(sid, {})
    checks.append({"name": "health", "status": "pass" if health.get("status") == "up" else "fail",
                   "detail": health.get("status", "unknown")})
    same_port = [s["id"] for s in monitor.registry
                 if s.get("host") == studio.get("host") and s.get("port") == studio.get("port")]
    checks.append({"name": "port", "status": "pass" if len(same_port) == 1 else "fail",
                   "detail": f"{studio.get('host')}:{studio.get('port')}" if len(same_port) == 1
                   else "conflicts with " + ", ".join(same_port)})
    row = {"id": sid, "title": studio.get("title", sid),
           "machine": studio.get("machine", "local"), "checks": checks}
    headers = peers.studio_headers(studio)
    timeout = httpx.Timeout(PREFLIGHT_TIMEOUT)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            cap_r = await client.get(f"{base_url(studio)}/api/capabilities")
            cap_r.raise_for_status()
            cap = cap_r.json()
            valid = cap.get("schema_version") == 1 and cap.get("studio", {}).get("modality") == studio["modality"]
            checks.append({"name": "capability contract", "status": "pass" if valid else "fail",
                           "detail": ", ".join(cap.get("operations") or []) or "no operations"})
            cat_r = await client.get(f"{base_url(studio)}/api/catalog", headers=headers)
            cat_r.raise_for_status()
            total, ready = _downloaded(cat_r.json())
            checks.append({"name": "fleet authentication", "status": "pass", "detail": "accepted"})
            checks.append({"name": "models", "status": "pass" if ready else "warn",
                           "detail": f"{ready}/{total} ready"})
            diag_path = cap.get("diagnostics_endpoint")
            if diag_path:
                diag_r = await client.get(f"{base_url(studio)}{diag_path}", headers=headers)
                diag_r.raise_for_status()
                ds = _diag_state(diag_r.json())
                checks.append({"name": "generation engine", "status": ds,
                               "detail": "ready" if ds == "pass" else "needs install or repair"})
    except httpx.HTTPStatusError as e:
        name = "fleet authentication" if e.response.status_code in {401, 403} else "API contract"
        checks.append({"name": name, "status": "fail", "detail": f"HTTP {e.response.status_code}"})
    except (httpx.HTTPError, ValueError) as e:
        checks.append({"name": "API contract", "status": "fail", "detail": str(e)[:160]})

    if studio.get("machine", "local") == "local" and studio.get("app"):
        app_dir = PINOKIO_HOME / "api" / studio["app"]
        script_ok = (app_dir / "update.js").exists()
        checks.append({"name": "update workflow", "status": "pass" if script_ok else "fail",
                       "detail": "update.js ready" if script_ok else "update.js missing"})
        if app_dir.exists():
            free_gb = shutil.disk_usage(app_dir).free / 1_000_000_000
            checks.append({"name": "disk space", "status": "pass" if free_gb >= 5 else "warn",
                           "detail": f"{free_gb:.1f} GB free"})
        memory = host_stats()
        available = memory.get("available_gb", 0)
        checks.append({"name": "memory", "status": "pass" if available >= 2 else "warn",
                       "detail": f"{available} GB free of {memory.get('total_gb', '?')} GB"})
    elif studio.get("machine"):
        peer = peers.cached(studio["machine"])
        memory = (peer or {}).get("host")
        if memory:
            available = memory.get("available_gb", 0)
            checks.append({"name": "memory", "status": "pass" if available >= 2 else "warn",
                           "detail": f"{available} GB free of {memory.get('total_gb', '?')} GB"})
    row["capabilities"] = next((c["detail"] for c in checks if c["name"] == "capability contract"), "")
    row["status"] = "fail" if any(c["status"] == "fail" for c in checks) else (
        "warn" if any(c["status"] == "warn" for c in checks) else "pass")
    return row


def preflight_snapshot() -> dict:
    return _preflight


def start_updates(monitor, studio_ids: list[str]) -> dict:
    active = next((j for j in _updates.values() if j["status"] in {"queued", "running"}), None)
    if active:
        raise ValueError(f"update {active['id']} is already running")
    if any(not isinstance(sid, str) or not sid or len(sid) > 200 for sid in studio_ids):
        raise ValueError("studio ids must be non-empty strings under 200 characters")
    known = {s["id"] for s in monitor.registry}
    ids = list(dict.fromkeys(studio_ids))
    missing = [sid for sid in ids if sid not in known]
    if not ids or missing:
        raise ValueError("choose at least one known studio" if not ids else f"unknown studios: {', '.join(missing)}")
    if len(_updates) >= 100:
        oldest_done = sorted((j for j in _updates.values() if j["status"] not in {"queued", "running"}),
                             key=lambda j: j["created_at"])
        for old in oldest_done[:max(1, len(_updates) - 99)]:
            _updates.pop(old["id"], None)
    job_id = uuid.uuid4().hex[:10]
    job = {"id": job_id, "status": "queued", "created_at": time.time(),
           "finished_at": None, "items": [{"studio": sid, "status": "queued", "detail": "waiting"} for sid in ids]}
    _updates[job_id] = job
    asyncio.create_task(_run_updates(monitor, job))
    return job


async def _run_updates(monitor, job: dict):
    job["status"] = "running"
    for item in job["items"]:
        studio = next(s for s in monitor.registry if s["id"] == item["studio"])
        try:
            await _update_one(monitor, studio, item)
        except Exception as e:
            item.update(status="failed", detail=str(e)[:240], finished_at=time.time())
    job["status"] = "failed" if any(i["status"] == "failed" for i in job["items"]) else "complete"
    job["finished_at"] = time.time()


async def _update_one(monitor, studio: dict, item: dict):
    sid = studio["id"]
    item.update(status="draining", detail="waiting for active work to finish", started_at=time.time())
    broker.set_maintenance(sid, True)
    try:
        deadline = time.monotonic() + DRAIN_TIMEOUT
        while sid in broker.busy_studios():
            if time.monotonic() >= deadline:
                raise RuntimeError("drain timed out; update was not started")
            await asyncio.sleep(2)
        if studio.get("machine", "local") != "local":
            await _update_remote(studio, item)
            return
        version_file = PINOKIO_HOME / "api" / studio["app"] / "VERSION"
        try:
            item["expected_version"] = version_file.read_text().strip()
        except OSError:
            item["expected_version"] = None
        item.update(status="updating", detail="running the Studio's update.js")
        result = run_studio_script(studio, "update.js")
        if not result.get("ok"):
            raise RuntimeError(result.get("error", "could not start update"))
        await _wait_for_healthy(studio, item)
    except Exception:
        if studio.get("machine", "local") == "local":
            control_studio(studio, "start")
        raise
    finally:
        broker.set_maintenance(sid, False)


async def _wait_for_healthy(studio: dict, item: dict):
    deadline = time.monotonic() + UPDATE_TIMEOUT
    headers = peers.studio_headers(studio)
    saw_unavailable = False
    async with httpx.AsyncClient(timeout=5.0) as client:
        while time.monotonic() < deadline:
            await asyncio.sleep(3)
            try:
                r = await client.get(f"{base_url(studio)}/api/health", headers=headers)
                data = r.json()
                version = str(data.get("app_version", "unknown"))
                expected = item.get("expected_version")
                version_loaded = bool(expected and version.startswith(expected))
                if r.status_code == 200 and data.get("ok") and (saw_unavailable or version_loaded):
                    item.update(status="complete", detail=f"healthy on v{version}",
                                finished_at=time.time())
                    return
            except (httpx.HTTPError, ValueError):
                saw_unavailable = True
        raise RuntimeError("Studio did not return healthy before the update timeout")


async def _update_remote(studio: dict, item: dict):
    url = f"http://{studio['host']}:{studio.get('hub_port', peers.DEFAULT_HUB_PORT)}"
    headers = {"X-Hub-Token": peers.fleet_token() or ""}
    local_id = studio["modality"]
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(f"{url}/api/hub/maintenance/updates", headers=headers,
                              json={"studio_ids": [local_id]})
        r.raise_for_status()
        remote_id = r.json()["id"]
        deadline = time.monotonic() + UPDATE_TIMEOUT
        while time.monotonic() < deadline:
            await asyncio.sleep(4)
            status = await client.get(f"{url}/api/hub/maintenance/updates/{remote_id}", headers=headers)
            status.raise_for_status()
            data = status.json()
            remote_item = data["items"][0]
            item.update(status=remote_item["status"], detail=remote_item["detail"])
            if data["status"] in {"complete", "failed"}:
                if data["status"] == "failed":
                    raise RuntimeError(remote_item["detail"])
                item["finished_at"] = time.time()
                return
        raise RuntimeError("remote Hub update timed out")


def update_snapshot(job_id: str | None = None):
    if job_id:
        return _updates.get(job_id)
    return sorted(_updates.values(), key=lambda j: j["created_at"], reverse=True)[:20]


# ── Fleet Hub self-update ───────────────────────────────────────────────────
# The studio updates above cover studios (registry entries). The HUB itself is
# not a studio, so to update the Studio Hub on the agent Macs the primary tells
# each peer Hub to run ITS OWN update.js and then waits for it to restart (each
# peer's launchd startup service brings it back). Peers already accept the fleet
# token, and already expose /api/hub/maintenance/self-update.

def _remote_hosts(monitor) -> dict[str, str]:
    """machine -> host for every non-local registered machine (its Hub is at
    http://host:47873)."""
    out: dict[str, str] = {}
    for s in monitor.registry:
        m = s.get("machine", "local")
        if m != "local" and m not in out:
            out[m] = s["host"]
    return out


def start_hub_updates(monitor, latest: str | None, machines: list[str] | None = None) -> dict:
    active = next((j for j in _hub_updates.values() if j["status"] in {"queued", "running"}), None)
    if active:
        raise ValueError(f"a fleet Hub update ({active['id']}) is already running")
    hosts = _remote_hosts(monitor)
    if machines is not None:
        if any(not isinstance(m, str) or not m or len(m) > 200 for m in machines):
            raise ValueError("machine names must be non-empty strings under 200 characters")
        wanted = list(dict.fromkeys(machines))
        missing = [m for m in wanted if m not in hosts]
        if missing:
            raise ValueError(f"unknown machines: {', '.join(missing)}")
        targets = [(m, hosts[m]) for m in wanted]
    else:
        targets = list(hosts.items())
    if not targets:
        raise ValueError("no remote machines registered to update")
    if len(_hub_updates) >= 50:
        for old in sorted((j for j in _hub_updates.values() if j["status"] not in {"queued", "running"}),
                          key=lambda j: j["created_at"])[:max(1, len(_hub_updates) - 49)]:
            _hub_updates.pop(old["id"], None)
    job = {"id": uuid.uuid4().hex[:10], "kind": "hub", "status": "queued",
           "created_at": time.time(), "finished_at": None, "latest": latest,
           "items": [{"machine": m, "host": h, "status": "queued",
                      "detail": "waiting", "from_version": None, "to_version": None}
                     for m, h in targets]}
    _hub_updates[job["id"]] = job
    asyncio.create_task(_run_hub_updates(job))
    return job


async def _run_hub_updates(job: dict):
    job["status"] = "running"
    # peers are independent Macs → update them concurrently; each self-restarts
    await asyncio.gather(*(_update_hub_one(item, job.get("latest")) for item in job["items"]))
    job["status"] = "failed" if any(i["status"] == "failed" for i in job["items"]) else "complete"
    job["finished_at"] = time.time()


async def _update_hub_one(item: dict, latest: str | None):
    host = item["host"]
    url = f"http://{host}:{peers.DEFAULT_HUB_PORT}"
    headers = {"X-Hub-Token": peers.fleet_token() or ""}
    item.update(status="checking", detail="checking the Hub", started_at=time.time())
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            try:
                v = await client.get(f"{url}/api/version")
                cur = str(v.json().get("app_version") or "")
            except (httpx.HTTPError, ValueError):
                raise RuntimeError("Hub unreachable on :47873 — run the Hub there, "
                                   "open the firewall, and set the same fleet token")
            item["from_version"] = cur
            if latest and cur == latest:
                item.update(status="current", to_version=cur,
                            detail=f"already on v{cur}", finished_at=time.time())
                return
            item.update(status="updating", detail="pulling latest + restarting")
            r = await client.post(f"{url}/api/hub/maintenance/self-update", headers=headers)
            if r.status_code == 401:
                raise RuntimeError("remote Hub rejected the fleet token")
            r.raise_for_status()
    except Exception as e:
        item.update(status="failed", detail=str(e)[:240], finished_at=time.time())
        return
    # the peer now runs update.js and restarts — wait for it to come back
    item.update(status="restarting", detail="waiting for the Hub to come back online")
    deadline = time.monotonic() + UPDATE_TIMEOUT
    saw_down = False
    async with httpx.AsyncClient(timeout=5.0) as client:
        while time.monotonic() < deadline:
            await asyncio.sleep(4)
            try:
                v = await client.get(f"{url}/api/version")
                ver = str(v.json().get("app_version") or "")
                if saw_down or (item["from_version"] and ver != item["from_version"]):
                    item.update(status="complete", to_version=ver,
                                detail=f"back online on v{ver}", finished_at=time.time())
                    return
            except (httpx.HTTPError, ValueError):
                saw_down = True  # it went down to restart — expected
    item.update(status="failed", detail="Hub did not come back before the timeout",
                finished_at=time.time())


def hub_update_snapshot(job_id: str | None = None):
    if job_id:
        return _hub_updates.get(job_id)
    return sorted(_hub_updates.values(), key=lambda j: j["created_at"], reverse=True)[:20]
