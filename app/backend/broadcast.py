"""Config / model broadcaster — push one thing to many studios at once.

- Model downloads: fan out to each studio's own `POST /api/downloads`
  (identical schema across the family: {repo, token?}). Works for local AND
  remote registry entries since it goes over HTTP.
- Environment variables: rewrite the KEY=value line in each local studio's
  ENVIRONMENT file (append if missing). File-level by necessity — that's where
  Pinokio reads env from. Local studios only, and the studio must be
  restarted for the change to take effect (we report that back).
"""

import asyncio
import json
import re
import time
import uuid

import httpx

from .peers import studio_request
from .control import PINOKIO_HOME
from .registry import DATA_DIR

# Guard the env broadcaster against writing outside a studio's own folder.
_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
DOWNLOADS_FILE = DATA_DIR / "fleet_downloads.json"
_ACTIVE_DOWNLOAD_STATES = {"queued", "running", "paused", "cancelling"}


def _load_downloads() -> dict[str, dict]:
    try:
        payload = json.loads(DOWNLOADS_FILE.read_text())
        rows = payload.get("downloads") if isinstance(payload, dict) else None
        if isinstance(rows, list):
            return {str(row["id"]): row for row in rows[-50:]
                    if isinstance(row, dict) and row.get("id")}
    except (OSError, ValueError, TypeError):
        pass
    return {}


_downloads: dict[str, dict] = _load_downloads()


def _save_downloads() -> None:
    try:
        DOWNLOADS_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = DOWNLOADS_FILE.with_suffix(".json.tmp")
        rows = sorted(_downloads.values(), key=lambda row: row["created_at"])[-50:]
        temporary.write_text(json.dumps({"downloads": rows}, indent=2) + "\n")
        temporary.replace(DOWNLOADS_FILE)
    except OSError:
        pass


def _rollup_download(run: dict) -> None:
    items = run.get("items") or []
    active = [item for item in items if item.get("state") in _ACTIVE_DOWNLOAD_STATES]
    done = [item for item in items if item.get("state") == "done"]
    failed = [item for item in items if item.get("state") in {"error", "cancelled"}]
    if active:
        state = "running"
    elif failed and done:
        state = "complete_with_errors"
    elif failed:
        state = "failed"
    else:
        state = "done"
    run.update(
        state=state,
        completed_count=len(done),
        failed_count=len(failed),
        active_count=len(active),
        finished_at=None if active else (run.get("finished_at") or time.time()),
    )


def record_download(repo: str, studios: list[dict], results: dict) -> dict:
    """Persist the worker job IDs returned by one fleet download fan-out."""
    now = time.time()
    run = {
        "id": uuid.uuid4().hex[:10], "repo": repo, "created_at": now,
        "finished_at": None, "state": "running", "items": [],
    }
    by_id = {studio["id"]: studio for studio in studios}
    for studio_id, result in results.items():
        studio = by_id[studio_id]
        accepted = bool(result.get("ok"))
        run["items"].append({
            "studio": studio_id,
            "machine": studio.get("machine", "local"),
            "modality": studio.get("modality"),
            "host": studio.get("host"),
            "port": studio.get("port"),
            "job_id": result.get("job"),
            "state": "queued" if accepted else "error",
            "detail": ("accepted by Studio" if accepted else
                       result.get("detail") or result.get("error") or
                       f"HTTP {result.get('status', '?')}"),
            "percent": None,
            "bytes_observed": 0,
            "bytes_total": 0,
            "speed_bps": 0,
            "eta_seconds": None,
            "reachable": accepted,
            "updated_at": now,
        })
    _rollup_download(run)
    _downloads[run["id"]] = run
    _save_downloads()
    return run


def download_history() -> list[dict]:
    return sorted(_downloads.values(), key=lambda row: row["created_at"], reverse=True)


async def refresh_downloads(client: httpx.AsyncClient, registry: list[dict]) -> list[dict]:
    """Refresh active worker downloads while preserving offline progress."""
    live = {studio["id"]: studio for studio in registry}
    targets = [(run, item) for run in _downloads.values()
               for item in run.get("items") or []
               if item.get("state") in _ACTIVE_DOWNLOAD_STATES]

    async def refresh_one(run: dict, item: dict) -> None:
        studio = live.get(item["studio"]) or {
            "id": item["studio"], "host": item.get("host"),
            "port": item.get("port"), "machine": item.get("machine"),
        }
        try:
            url, headers = studio_request(studio, "/api/downloads")
            response = await client.get(url, headers=headers, timeout=8.0)
            response.raise_for_status()
            jobs = response.json().get("jobs") or []
            worker = next((row for row in jobs
                           if str(row.get("id")) == str(item.get("job_id"))), None)
            if worker is not None:
                for key in ("state", "error", "percent", "bytes_done",
                            "bytes_partial", "bytes_observed", "bytes_total",
                            "speed_bps", "eta_seconds", "started_at", "finished_at"):
                    if key in worker:
                        item[key] = worker[key]
                item["detail"] = worker.get("error") or worker.get("state")
                item["reachable"] = True
            else:
                cache_url, cache_headers = studio_request(
                    studio, f"/api/cache/{run['repo']}")
                cached = await client.get(cache_url, headers=cache_headers, timeout=8.0)
                cached.raise_for_status()
                cache = cached.json()
                if cache.get("state") == "cached":
                    item.update(state="done", percent=100.0,
                                bytes_observed=cache.get("bytes_complete") or 0,
                                detail="model cache verified", reachable=True,
                                finished_at=time.time())
                elif time.time() - run["created_at"] >= 30:
                    item.update(
                        state="error", reachable=True, finished_at=time.time(),
                        bytes_observed=(cache.get("bytes_complete") or 0)
                                       + (cache.get("bytes_incomplete") or 0),
                        detail="worker lost the download job; partial cache is preserved and can resume",
                    )
            item["updated_at"] = time.time()
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            # A sleeping or dropped Mac is reduced capacity, not proof that
            # its resumable download failed. Keep the last known progress.
            item.update(reachable=False, detail=f"worker unavailable · {str(exc)[:140]}",
                        updated_at=time.time())

    if targets:
        await asyncio.gather(*(refresh_one(run, item) for run, item in targets))
    for run in _downloads.values():
        _rollup_download(run)
    if targets:
        _save_downloads()
    return download_history()


async def cancel_download(client: httpx.AsyncClient, run_id: str,
                          studio_id: str, registry: list[dict]) -> dict:
    run = _downloads.get(run_id)
    if run is None:
        raise ValueError("fleet download not found")
    item = next((row for row in run.get("items") or []
                 if row.get("studio") == studio_id), None)
    if item is None:
        raise ValueError("download target not found")
    if item.get("state") not in _ACTIVE_DOWNLOAD_STATES or not item.get("job_id"):
        raise ValueError("download is not active")
    studio = next((row for row in registry if row["id"] == studio_id), None) or {
        "id": studio_id, "host": item.get("host"), "port": item.get("port"),
        "machine": item.get("machine"),
    }
    url, headers = studio_request(studio, f"/api/downloads/{item['job_id']}")
    response = await client.delete(url, headers=headers, timeout=8.0)
    response.raise_for_status()
    worker = response.json().get("job") or {}
    item.update(state=worker.get("state") or "cancelling",
                detail="cancellation requested", reachable=True, updated_at=time.time())
    _rollup_download(run)
    _save_downloads()
    return run


async def broadcast_download(
    client: httpx.AsyncClient, studios: list[dict], repo: str,
    token: str | None = None,
) -> dict:
    async def start_one(s: dict) -> tuple[str, dict]:
        try:
            body = {"repo": repo}
            if token:
                body["token"] = token
            url, headers = studio_request(s, "/api/downloads")
            r = await client.post(
                url, json=body, headers=headers, timeout=15.0)
            payload = r.json() if r.status_code < 500 else {}
            result = {
                "ok": r.status_code < 400,
                "status": r.status_code,
                "detail": payload.get("detail"),
                "job": (payload.get("job") or {}).get("id"),
            }
        except httpx.HTTPError as e:
            result = {"ok": False, "error": str(e)}
        return s["id"], result

    # Independent Macs must not wait behind a sleeping node. All workers get
    # the request concurrently and report their own result.
    return dict(await asyncio.gather(*(start_one(studio) for studio in studios)))


async def broadcast_hf_token(
    client: httpx.AsyncClient, studios: list[dict], token: str,
) -> dict:
    """Set the Hugging Face token on many studios at once via each studio's own
    `POST /api/settings` (a partial update — only `hf_token` is sent, so other
    sibling settings are preserved). The token is passed through,
    never stored in the Hub. Studios without a settings endpoint (e.g. Render)
    report a clean failure rather than blocking the rest."""
    results = {}
    for s in studios:
        try:
            url, headers = studio_request(s, "/api/settings")
            r = await client.post(
                url, json={"hf_token": token}, headers=headers, timeout=15.0)
            if r.status_code == 404 or r.status_code == 405:
                results[s["id"]] = {"ok": False, "status": r.status_code,
                                    "detail": "no settings endpoint (studio can't hold a token)"}
                continue
            payload = r.json() if r.status_code < 500 and r.headers.get(
                "content-type", "").startswith("application/json") else {}
            results[s["id"]] = {
                "ok": r.status_code < 400,
                "status": r.status_code,
                "detail": payload.get("detail") if isinstance(payload, dict) else None,
            }
        except httpx.HTTPError as e:
            results[s["id"]] = {"ok": False, "error": str(e)}
    return results


def broadcast_env(studios: list[dict], key: str, value: str) -> dict:
    if not _KEY_RE.match(key):
        return {"error": f"invalid env key: {key!r} (UPPER_SNAKE_CASE only)"}
    results = {}
    for s in studios:
        if s.get("machine", "local") != "local" or not s.get("app"):
            results[s["id"]] = {"ok": False, "error": "local studios only"}
            continue
        env_file = PINOKIO_HOME / "api" / s["app"] / "ENVIRONMENT"
        try:
            lines = env_file.read_text().splitlines() if env_file.exists() else []
            pattern = re.compile(rf"^{re.escape(key)}=")
            replaced = False
            for i, line in enumerate(lines):
                if pattern.match(line):
                    lines[i] = f"{key}={value}"
                    replaced = True
                    break
            if not replaced:
                lines.append(f"{key}={value}")
            env_file.write_text("\n".join(lines) + "\n")
            results[s["id"]] = {"ok": True, "action": "replaced" if replaced else "appended"}
        except OSError as e:
            results[s["id"]] = {"ok": False, "error": str(e)}
    return {
        "results": results,
        "note": "restart each studio for the change to take effect",
    }
