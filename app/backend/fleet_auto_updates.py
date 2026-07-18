"""Read-only fleet updater inventory plus staggered, health-gated orchestration."""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from . import fleet_ops, peers
from .registry import base_url


TERMINAL_ITEM_STATES = {"complete", "current", "scheduled", "failed"}
APP_ORDER = {"hub": 0, "voice": 1, "chat": 2, "image": 3, "music": 4,
             "video": 5, "render": 6}


def _version_key(value: object) -> tuple[int, int, int] | None:
    try:
        parts = [int(part) for part in str(value).strip().lstrip("v").split(".")[:3]]
    except (TypeError, ValueError):
        return None
    return tuple((parts + [0, 0, 0])[:3]) if parts else None


def _latest_version(*values: object) -> str | None:
    """Choose the newest valid version, never a stale downgrade."""
    candidates = [(key, str(value)) for value in values
                  if (key := _version_key(value)) is not None]
    return max(candidates, default=(None, None))[1]


class FleetAutoUpdates:
    """Coordinate fixed registered targets without touching their repositories."""

    def __init__(self, monitor, hub_updater, *, stagger_seconds: float = 3.0,
                 poll_seconds: float = 3.0, update_timeout: float = 20 * 60,
                 state_path: Path | None = None):
        self.monitor = monitor
        self.hub_updater = hub_updater
        self.stagger_seconds = stagger_seconds
        self.poll_seconds = poll_seconds
        self.update_timeout = update_timeout
        self.state_path = state_path
        self._jobs: dict[str, dict[str, Any]] = {}
        self._load_jobs()

    def _load_jobs(self) -> None:
        if self.state_path is None:
            return
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            jobs = value.get("jobs") if isinstance(value, dict) else None
            if isinstance(jobs, list):
                self._jobs = {
                    str(job["id"]): job for job in jobs[-50:]
                    if isinstance(job, dict) and job.get("id")
                }
        except (OSError, ValueError, TypeError):
            return

    def _persist(self) -> None:
        if self.state_path is None:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            saved = sorted(self._jobs.values(), key=lambda job: job["created_at"], reverse=True)[:50]
            temporary.write_text(json.dumps({"jobs": saved}, indent=2) + "\n",
                                 encoding="utf-8")
            temporary.replace(self.state_path)
        except OSError:
            pass

    def resume_pending(self) -> int:
        """Resume the last durable rolling job after an unexpected Hub restart."""
        known = {target["id"]: target for target in self.targets()
                 if target["kind"] == "studio"}
        resumed = 0
        for job in sorted(self._jobs.values(), key=lambda row: row["created_at"]):
            if job.get("status") not in {"queued", "running"}:
                continue
            for item in job.get("items", []):
                if item.get("status") not in TERMINAL_ITEM_STATES:
                    item.update(status="queued",
                                detail="Hub restarted; safely resuming this update")
            job["status"] = "queued"
            job["finished_at"] = None
            asyncio.create_task(self._run_updates(job, known))
            resumed += 1
            break
        self._persist()
        return resumed

    def targets(self) -> list[dict[str, Any]]:
        targets = [{
            "id": "hub@local", "kind": "hub", "modality": "hub",
            "title": "Studio Hub KH", "machine": "local", "url": "",
            "settings_url": "/#updates",
        }]
        registry = list(self.monitor.registry)
        for modality in ("voice", "chat", "image", "music", "video", "render"):
            candidates = [studio for studio in registry
                          if str(studio.get("modality") or "") == modality]
            if not candidates:
                continue
            # This view represents the six repositories in this release, not
            # every remote worker registered for production. Prefer the fixed
            # canonical local row; remote agent-Hub maintenance remains in
            # Remote where machine versions and reachability belong.
            studio = min(candidates, key=lambda row: (
                0 if row.get("id") == modality else 1,
                0 if row.get("machine") == "local" else 1,
                str(row.get("id") or ""),
            ))
            root = base_url(studio)
            suffix = "" if modality == "video" else (
                "/#automatic-updates" if modality == "render" else "/#/settings"
            )
            targets.append({
                "id": studio["id"], "kind": "studio", "modality": modality,
                "title": studio.get("title", studio["id"]),
                "machine": studio.get("machine", "local"), "url": root,
                "settings_url": root + suffix, "studio": studio,
            })
        return sorted(targets, key=lambda row: (
            APP_ORDER.get(row["modality"], 99), str(row["machine"]), str(row["id"])
        ))

    def _target(self, target_id: str) -> dict[str, Any]:
        target = next((row for row in self.targets() if row["id"] == target_id), None)
        if target is None:
            raise ValueError("unknown automatic-update target")
        return target

    async def _request(self, target: dict[str, Any], method: str, path: str,
                       payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if target["kind"] == "hub":
            if path.endswith("/status"):
                return self.hub_updater.public_status()
            if path.endswith("/readiness"):
                return self.hub_updater.readiness_status()
            if path.endswith("/settings"):
                return self.hub_updater.save_settings(payload or {})
            if path.endswith("/check"):
                return self.hub_updater.trigger_check()
            if path.endswith("/update"):
                return self.hub_updater.trigger_update(after_current=bool((payload or {}).get("after_current")))
            if path == "/api/health":
                return {"ok": True, "app_version": self.hub_updater.installed_version()}
            raise ValueError("unsupported local updater operation")
        headers = peers.studio_headers(target["studio"])
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=8.0)) as client:
            response = await client.request(method, target["url"] + path,
                                            headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("invalid updater response")
            return data

    async def _request_resilient(self, target: dict[str, Any], method: str, path: str,
                                 payload: dict[str, Any] | None = None,
                                 item: dict[str, Any] | None = None) -> dict[str, Any]:
        """Retry short transport failures without repeating permanent HTTP errors."""
        last: Exception | None = None
        for attempt in range(1, 5):
            try:
                return await self._request(target, method, path, payload)
            except (httpx.TransportError, httpx.TimeoutException, OSError) as exc:
                last = exc
                if item is not None:
                    reconnects = int(item.get("reconnects") or 0) + 1
                    item.update(status="checking", reconnects=reconnects,
                                detail=f"Mac did not respond; retrying ({attempt}/4)")
                    self._persist()
                if attempt < 4:
                    await asyncio.sleep(min(max(self.poll_seconds, 0.05) * (2 ** (attempt - 1)), 5.0))
        assert last is not None
        raise last

    async def _status_one(self, target: dict[str, Any]) -> dict[str, Any]:
        healthy = True if target["kind"] == "hub" else (
            self.monitor.status.get(target["id"], {}).get("status") == "up"
        )
        base = {key: target[key] for key in (
            "id", "kind", "modality", "title", "machine", "settings_url"
        )}
        try:
            status = await self._request(target, "GET", "/api/auto-update/status")
            # The updater's persisted last-check record may predate a manual
            # Pinokio update.  The public Studio version contract tracks the
            # published VERSION separately; use the newest verified value so
            # the Updates tab can never render a fake downgrade.
            release = {}
            if target["kind"] == "studio":
                try:
                    release = await self._request(target, "GET", "/api/update-status")
                except (httpx.HTTPError, ValueError, OSError):
                    pass
            settings = status.get("settings") or {}
            installed = release.get("app_version") or status.get("installed_version")
            latest = _latest_version(
                status.get("latest_version"),
                release.get("latest_version"),
                fleet_ops.published_version_snapshot()["versions"].get(target["modality"]),
            )
            installed_key = _version_key(installed)
            latest_key = _version_key(latest)
            return {
                **base, "supported": True, "healthy": healthy,
                "installed_version": installed,
                "latest_version": latest,
                "mode": settings.get("mode", "off"),
                "frequency": settings.get("frequency", "daily"),
                "maintenance_hour": settings.get("maintenance_hour"),
                "last_checked": status.get("last_checked"),
                "next_check": status.get("next_check"),
                "update_available": (
                    latest_key > installed_key if latest_key is not None and installed_key is not None
                    else bool(status.get("update_available"))
                ),
                "state": status.get("state", "idle"),
                "defer_reason": status.get("defer_reason"),
                "last_update_result": status.get("last_update_result"),
                "scheduler_installed": bool((status.get("scheduler") or {}).get("installed")),
            }
        except (httpx.HTTPError, ValueError, OSError) as exc:
            return {**base, "supported": False, "healthy": healthy, "mode": "off",
                    "state": "unavailable", "update_available": False,
                    "error": str(exc)[:180]}

    async def snapshot(self) -> dict[str, Any]:
        published = await fleet_ops.refresh_published_versions()
        rows = await asyncio.gather(*(self._status_one(target) for target in self.targets()))
        active = next((job for job in self._jobs.values()
                       if job["status"] in {"queued", "running"}), None)
        return {"apps": rows, "job": active or self.latest_job(), "checked_at": time.time(),
                "github_checked_at": published["checked_at"],
                "github_errors": published["errors"]}

    async def check_all(self) -> dict[str, Any]:
        published = await fleet_ops.refresh_published_versions(force=True)
        results = []
        for target in self.targets():
            try:
                await self._request(target, "POST", "/api/auto-update/check", {})
                results.append({"id": target["id"], "ok": True})
            except (httpx.HTTPError, ValueError, OSError) as exc:
                results.append({"id": target["id"], "ok": False, "error": str(exc)[:180]})
            await asyncio.sleep(0.05)
        return {"results": results, "started_at": time.time(),
                "github_versions": published["versions"],
                "github_errors": published["errors"]}

    async def set_mode(self, target_id: str, mode: str) -> dict[str, Any]:
        if mode not in {"off", "notify", "auto"}:
            raise ValueError("mode must be off, notify, or auto")
        target = self._target(target_id)
        status = await self._request(target, "GET", "/api/auto-update/status")
        settings = dict(status.get("settings") or {})
        settings.update(mode=mode)
        settings.setdefault("frequency", "daily")
        settings.setdefault("maintenance_hour", APP_ORDER.get(target["modality"], 1))
        settings.setdefault("idle_only", True)
        return await self._request(target, "POST", "/api/auto-update/settings", settings)

    def start_idle_updates(self, target_ids: list[str] | None = None) -> dict[str, Any]:
        active = next((job for job in self._jobs.values()
                       if job["status"] in {"queued", "running"}), None)
        if active:
            raise ValueError("an automatic fleet update is already running")
        known = {target["id"]: target for target in self.targets() if target["kind"] == "studio"}
        ids = list(dict.fromkeys(target_ids or known.keys()))
        if any(not isinstance(value, str) or value not in known for value in ids):
            raise ValueError("choose only known sibling Studio targets")
        if not ids:
            raise ValueError("no sibling Studios selected")
        if len(self._jobs) >= 50:
            done = sorted((job for job in self._jobs.values()
                           if job["status"] not in {"queued", "running"}),
                          key=lambda job: job["created_at"])
            for old in done[:max(1, len(self._jobs) - 49)]:
                self._jobs.pop(old["id"], None)
        job = {"id": uuid.uuid4().hex[:10], "status": "queued",
               "created_at": time.time(), "finished_at": None,
               "items": [{"target": value, "status": "queued", "detail": "waiting"}
                         for value in ids]}
        self._jobs[job["id"]] = job
        self._persist()
        asyncio.create_task(self._run_updates(job, known))
        return job

    def retry_failed(self, job_id: str) -> dict[str, Any]:
        job = self._jobs.get(job_id)
        if job is None:
            raise ValueError("automatic fleet update not found")
        targets = [item["target"] for item in job.get("items", [])
                   if item.get("status") == "failed"]
        if not targets:
            raise ValueError("this update has no failed apps to retry")
        return self.start_idle_updates(targets)

    async def _run_updates(self, job: dict[str, Any], known: dict[str, dict[str, Any]]) -> None:
        job["status"] = "running"
        self._persist()
        for index, item in enumerate(job["items"]):
            if item.get("status") in TERMINAL_ITEM_STATES:
                continue
            try:
                target = known.get(item["target"])
                if target is None:
                    raise RuntimeError("app is no longer registered on this Hub")
                await self._update_one(target, item)
            except Exception as exc:
                item.update(status="failed", detail=str(exc)[:220], finished_at=time.time())
            self._persist()
            if index + 1 < len(job["items"]):
                await asyncio.sleep(self.stagger_seconds)
        job["status"] = "failed" if any(
            item["status"] == "failed" for item in job["items"]) else "complete"
        job["finished_at"] = time.time()
        self._persist()

    async def _update_one(self, target: dict[str, Any], item: dict[str, Any]) -> None:
        item.update(status="checking", detail="refreshing GitHub and app update state",
                    started_at=time.time())
        self._persist()
        published = await fleet_ops.refresh_published_versions(force=True)
        expected = published["versions"].get(target["modality"])
        try:
            existing = await self._request_resilient(
                target, "GET", "/api/auto-update/status", item=item)
        except (httpx.TransportError, httpx.TimeoutException, OSError, ValueError):
            existing = {}
        if existing.get("state") in {"updating", "restarting"}:
            item.update(status="checking", detail="reconnected to the update already running")
            self._persist()
            await self._wait_for_completion(target, item, expected)
            return
        if existing.get("state") == "deferred" and existing.get("pending_manual"):
            item.update(status="scheduled",
                        detail=existing.get("defer_reason") or "queued on the app until it is idle",
                        finished_at=time.time())
            self._persist()
            return
        await self._request_resilient(
            target, "POST", "/api/auto-update/check", {}, item=item)
        check_deadline = time.monotonic() + 45.0
        while True:
            status = await self._request_resilient(
                target, "GET", "/api/auto-update/status", item=item)
            if status.get("state") != "checking" or time.monotonic() >= check_deadline:
                break
            await asyncio.sleep(min(self.poll_seconds, 0.5))
        installed = status.get("installed_version")
        centrally_available = bool(
            _version_key(expected) is not None
            and _version_key(installed) is not None
            and _version_key(expected) > _version_key(installed)
        )
        if not status.get("update_available") and not centrally_available:
            item.update(status="current", detail="already current", finished_at=time.time())
            return
        readiness = await self._request_resilient(
            target, "GET", "/api/auto-update/readiness", item=item)
        if not readiness.get("idle"):
            detail = "; ".join(readiness.get("reasons") or ["active work"])
            # The app's own durable scheduler keeps retrying this request after
            # its current work finishes, even if this Hub or browser restarts.
            await self._request_resilient(
                target, "POST", "/api/auto-update/update",
                {"after_current": True}, item=item)
            item.update(status="scheduled",
                        detail=f"queued until idle: {detail}"[:220], finished_at=time.time())
            self._persist()
            return
        item.update(status="updating", detail="updater started")
        self._persist()
        await self._request_resilient(
            target, "POST", "/api/auto-update/update", {}, item=item)
        await self._wait_for_completion(target, item, expected)

    async def _wait_for_completion(self, target: dict[str, Any], item: dict[str, Any],
                                   expected: str | None) -> None:
        deadline = time.monotonic() + self.update_timeout
        last_error = ""
        reconnects = int(item.get("reconnects") or 0)
        while time.monotonic() < deadline:
            await asyncio.sleep(self.poll_seconds)
            try:
                current = await self._request(target, "GET", "/api/auto-update/status")
                state = current.get("state")
                if state == "failed":
                    raise RuntimeError(current.get("last_update_result") or "update failed")
                if state == "deferred":
                    item.update(status="scheduled",
                                detail=current.get("defer_reason") or "queued on the app until it is idle",
                                finished_at=time.time())
                    self._persist()
                    return
                if state == "succeeded":
                    health = await self._request(target, "GET", "/api/health")
                    running = health.get("app_version", current.get("installed_version"))
                    reached_expected = not expected or (
                        _version_key(running) is not None
                        and _version_key(expected) is not None
                        and _version_key(running) >= _version_key(expected)
                    )
                    if health.get("ok") and reached_expected:
                        item.update(status="complete",
                                    detail=f"healthy on v{running or '?'}",
                                    finished_at=time.time())
                        self._persist()
                        return
            except (httpx.TransportError, httpx.TimeoutException, OSError, ValueError) as exc:
                last_error = type(exc).__name__
                reconnects += 1
                item.update(status="checking", reconnects=reconnects,
                            detail=f"connection dropped; reconnecting ({last_error})")
                self._persist()
        raise RuntimeError(f"update did not become healthy before timeout{': ' + last_error if last_error else ''}")

    def jobs(self) -> list[dict[str, Any]]:
        return sorted(self._jobs.values(), key=lambda job: job["created_at"], reverse=True)[:20]

    def latest_job(self) -> dict[str, Any] | None:
        jobs = self.jobs()
        return jobs[0] if jobs else None

    def job(self, job_id: str) -> dict[str, Any] | None:
        return self._jobs.get(job_id)
