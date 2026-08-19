"""Read-only fleet updater inventory plus staggered, health-gated orchestration."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
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
MANAGED_COMPONENT_REPOSITORIES = {
    "image": "theng12/imagestudio-mac",
    "voice": "theng12/voicestudio-mac",
}
_MANAGED_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_MANAGED_VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_CLEAN_CHECKOUT_HEALTH_DETAILS = {
    "The loaded app does not attest to the requested commit and version.",
    "The updated app did not attest to the expected commit and version.",
}


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


def managed_targets(monitor, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Return every installed Image/Voice target with its frozen release tuple.

    ``targets()`` intentionally deduplicates the normal automatic-update
    inventory. Managed release work must retain every installed instance.
    """
    components = manifest.get("components") if isinstance(manifest, dict) else None
    if not isinstance(components, dict):
        raise ValueError("managed release manifest must contain components")
    frozen: dict[str, dict[str, str]] = {}
    for modality, repository in MANAGED_COMPONENT_REPOSITORIES.items():
        component = components.get(modality)
        if not isinstance(component, dict):
            raise ValueError(f"managed release is missing {modality}")
        version = component.get("version")
        commit = component.get("commit")
        if (component.get("repository") != repository
                or component.get("installed_only") is not True
                or not isinstance(version, str) or not _MANAGED_VERSION_RE.fullmatch(version)
                or not isinstance(commit, str) or not _MANAGED_COMMIT_RE.fullmatch(commit)):
            raise ValueError(f"managed {modality} target is invalid")
        frozen[modality] = {
            "repository": repository,
            "target_version": version,
            "target_commit": commit,
        }
    rows = []
    for studio in monitor.registry:
        modality = str(studio.get("modality") or "")
        if modality not in frozen:
            continue
        root = base_url(studio)
        rows.append({
            "id": studio["id"], "kind": "studio", "modality": modality,
            "title": studio.get("title", studio["id"]),
            "machine": studio.get("machine", "local"), "url": root,
            "settings_url": root + "/#/settings", "studio": studio,
            **frozen[modality],
        })
    return sorted(rows, key=lambda row: (str(row["machine"]), row["modality"], row["id"]))


def _managed_operation_id(seed: str, target: dict[str, Any]) -> str:
    if not isinstance(seed, str) or not seed or len(seed) > 128:
        raise ValueError("managed operation_id must be a bounded non-empty string")
    identity = "\0".join(str(target[key]) for key in (
        "machine", "modality", "repository", "target_version", "target_commit",
    ))
    return "managed-" + hashlib.sha256(f"{seed}\0{identity}".encode()).hexdigest()


def _managed_result(target: dict[str, Any], state: str, detail: str, *,
                    next_retry: float | None = None,
                    error_code: str | None = None) -> dict[str, Any]:
    result = {
        **{key: target[key] for key in (
            "id", "machine", "modality", "repository", "target_version",
            "target_commit", "operation_id",
        )},
        "state": state,
        "detail": str(detail).replace("\n", " ")[:220],
        "next_retry": next_retry,
    }
    if error_code is not None:
        result["error_code"] = error_code
    return result


def _retryable_component_result(target: dict[str, Any], detail: str, *,
                                error_code: str | None = None) -> dict[str, Any]:
    return _managed_result(target, "retryable_failure", detail,
                           next_retry=time.time() + 60, error_code=error_code)


def managed_failure_code(status: object) -> str | None:
    """Normalize only updater-owned, stable failure evidence.

    Image and Voice 1.30.1/2.3.0 predate the structured field, so their two
    exact health-verification messages remain a deliberately closed legacy
    mapping. Arbitrary remote error text never becomes release-block evidence.
    """
    if not isinstance(status, dict):
        return None
    if status.get("failure_class") == "clean_checkout_health_failure":
        return "clean_checkout_health_failure"
    details = status.get("details")
    if isinstance(details, list) and any(
        isinstance(detail, str) and detail in _CLEAN_CHECKOUT_HEALTH_DETAILS
        for detail in details
    ):
        return "clean_checkout_health_failure"
    return None


def _auth_component_result(target: dict[str, Any], detail: str) -> dict[str, Any]:
    return _managed_result(target, "auth_blocked", detail, next_retry=time.time() + 60)


def _component_http_result(target: dict[str, Any], exc: httpx.HTTPStatusError) -> dict[str, Any]:
    code = exc.response.status_code
    if code in {401, 403}:
        return _auth_component_result(target, f"component updater authentication rejected (HTTP {code})")
    return _retryable_component_result(target, f"component updater unavailable or refused update (HTTP {code})")


def _json_object(response: httpx.Response) -> dict[str, Any]:
    value = response.json()
    if not isinstance(value, dict):
        raise ValueError("response JSON must be an object")
    return value


async def _run_managed_component(target: dict[str, Any], *, poll_seconds: float,
                                 update_timeout: float) -> dict[str, Any]:
    """Run one exact sibling update without touching ordinary update controls."""
    headers = peers.studio_headers(target["studio"])
    payload = {"after_current": True, "target_commit": target["target_commit"],
               "target_version": target["target_version"],
               "operation_id": target["operation_id"]}
    deadline = time.monotonic() + update_timeout
    transport_errors = (httpx.TransportError, httpx.TimeoutException)
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=8.0)) as client:
        # Capability may only be observed after an old sibling has recovered;
        # transport loss is therefore retryable rather than a fake capability
        # decision.
        while time.monotonic() < deadline:
            try:
                status = await client.get(target["url"] + "/api/auto-update/status", headers=headers)
                status.raise_for_status()
                state = _json_object(status)
                if (state.get("capabilities") or {}).get("managed_exact_commit") is not True:
                    return _retryable_component_result(
                        target, "exact component updater unavailable: managed_exact_commit is required",
                    )
                break
            except transport_errors:
                await asyncio.sleep(poll_seconds)
            except httpx.HTTPStatusError as exc:
                return _component_http_result(target, exc)
            except (ValueError, TypeError, AttributeError):
                return _retryable_component_result(target, "remote updater returned invalid status")
        else:
            return _retryable_component_result(target, "component unavailable before managed update")

        # The sibling admits this operation durably before replying. Replaying
        # this unchanged body after a dropped response adopts that admission.
        admitted = False
        while time.monotonic() < deadline:
            try:
                response = await client.post(target["url"] + "/api/auto-update/update",
                                             headers=headers, json=payload)
                response.raise_for_status()
                _json_object(response)
                admitted = True
                break
            except transport_errors:
                await asyncio.sleep(poll_seconds)
            except httpx.HTTPStatusError as exc:
                return _component_http_result(target, exc)
            except (ValueError, TypeError, AttributeError):
                return _retryable_component_result(target, "component returned invalid managed-update response")
        if not admitted:
            return _retryable_component_result(target, "component did not acknowledge managed update")

        while time.monotonic() < deadline:
            try:
                status = await client.get(target["url"] + "/api/auto-update/status", headers=headers)
                status.raise_for_status()
                state = _json_object(status)
                if (state.get("capabilities") or {}).get("managed_exact_commit") is not True:
                    return _retryable_component_result(
                        target, "exact component updater unavailable: managed_exact_commit is required",
                    )
                if state.get("state") == "failed":
                    failure_code = managed_failure_code(state)
                    return _retryable_component_result(
                        target,
                        "clean checkout health verification failed"
                        if failure_code else "exact component updater reported failure",
                        error_code=failure_code,
                    )
                if state.get("state") != "succeeded":
                    await asyncio.sleep(poll_seconds)
                    continue
                health = await client.get(target["url"] + "/api/health", headers=headers)
                health.raise_for_status()
                observed = _json_object(health)
                version = str(observed.get("app_version") or "")
                commit = str(observed.get("app_commit") or "")
                if (observed.get("ok") is True and version == target["target_version"]
                        and commit == target["target_commit"]):
                    return _managed_result(target, "current",
                                           f"healthy on exact v{version} commit {commit}")
                return _retryable_component_result(
                    target, "exact component health attestation mismatch",
                )
            except transport_errors:
                await asyncio.sleep(poll_seconds)
            except httpx.HTTPStatusError as exc:
                return _component_http_result(target, exc)
            except (ValueError, TypeError, AttributeError):
                return _retryable_component_result(target, "component returned invalid managed-update evidence")
    return _retryable_component_result(target, "component did not become healthy before deadline")


async def run_managed_components(monitor, manifest: dict[str, Any], *, operation_id: str,
                                 poll_seconds: float = 3.0,
                                 update_timeout: float = 20 * 60) -> list[dict[str, Any]]:
    """Execute installed Image/Voice exact targets serially for a later reconciler."""
    targets = [dict(target, operation_id=_managed_operation_id(operation_id, target))
               for target in managed_targets(monitor, manifest)]
    results = []
    for target in targets:
        try:
            results.append(await _run_managed_component(
                target, poll_seconds=poll_seconds, update_timeout=update_timeout,
            ))
        except Exception:
            # A malformed or locally broken sibling must not prevent the next
            # installed target from supplying its independent exact evidence.
            results.append(_retryable_component_result(
                target, "component runner could not obtain managed-update evidence",
            ))
    return results


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

    async def retirement_status(self, target_id: str) -> dict[str, Any]:
        """Read the sibling's own updater state before startup retirement."""
        target = self._target(target_id)
        return await self._request(target, "GET", "/api/auto-update/status")

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
        fleet_ops.finish_fleet_job(job)
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
