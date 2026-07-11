"""Fleet preflight and one-at-a-time drained Studio updates."""

from __future__ import annotations

import asyncio
import shutil
import time
import uuid

import httpx

from . import broker, peers
from .control import PINOKIO_HOME, control_studio, run_studio_script
from .registry import base_url

PREFLIGHT_TIMEOUT = 12.0
UPDATE_TIMEOUT = 20 * 60
DRAIN_TIMEOUT = 30 * 60

_preflight = {"ran_at": None, "status": "never", "studios": []}
_updates: dict[str, dict] = {}


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
