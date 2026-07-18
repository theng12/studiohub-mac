"""Fleet version discovery and one-at-a-time drained Studio updates."""

from __future__ import annotations

import asyncio
import re
import shutil
import time
import uuid

import httpx

import json

from . import broker, peers
from .control import (PINOKIO_HOME, control_studio, resolve_app_dir,
                      run_hub_script, run_studio_script)
from .registry import DATA_DIR, base_url
from .resources import host_stats

# Cached fleet versions persisted to disk, so the last-known versions survive a
# Hub restart and never just "disappear" from the dashboard.
_STATE_FILE = DATA_DIR / "fleet_versions.json"

PREFLIGHT_TIMEOUT = 12.0
UPDATE_TIMEOUT = 20 * 60
UPDATE_START_TIMEOUT = 3 * 60
DRAIN_TIMEOUT = 30 * 60

# Studio Hub watches the repositories itself instead of waiting for each
# Studio's scheduled updater. Branch-named raw URLs can be stale after a push,
# so discovery resolves main's commit first and reads immutable commit content.
PUBLISHED_REPOSITORIES = {
    "hub": "theng12/studiohub-mac",
    "voice": "theng12/voicestudio-mac",
    "chat": "theng12/chatstudio-mac",
    "image": "theng12/imagestudio-mac",
    "music": "theng12/musicstudio-mac",
    "video": "theng12/videostudio-mac",
    "render": "theng12/renderstudio-mac",
}
PUBLISHED_REFRESH_SECONDS = 60.0

_published_versions: dict[str, str] = {}
_published_refs: dict[str, str] = {}
_published_checked_at = 0.0
_published_errors: dict[str, str] = {}
_published_task: asyncio.Task | None = None
_published_lock: asyncio.Lock | None = None

_preflight = {"ran_at": None, "status": "never", "studios": []}
_studio_versions = {"checked_at": None, "studios": []}
_updates: dict[str, dict] = {}
_hub_updates: dict[str, dict] = {}
# machine -> {version, checked_at, host, reachable}: last-known peer Hub versions
_hub_versions: dict[str, dict] = {}


def _save_state() -> None:
    try:
        _STATE_FILE.write_text(json.dumps(
            {"studio_versions": _studio_versions,
             "hub_versions": _hub_versions,
             "updates": _updates,
             "hub_updates": _hub_updates}, indent=2) + "\n")
    except OSError:
        pass


def _load_state() -> None:
    global _preflight, _studio_versions, _hub_versions, _updates, _hub_updates
    try:
        d = json.loads(_STATE_FILE.read_text())
        if isinstance(d.get("studio_versions"), dict):
            _studio_versions = d["studio_versions"]
        # One-time migration from the old preflight persistence. Only retain
        # the version fields; the health/model/disk checklist is intentionally
        # no longer part of fleet update control.
        if isinstance(d.get("preflight"), dict):
            _preflight = d["preflight"]
            if not _studio_versions.get("studios"):
                _studio_versions = {
                    "checked_at": d["preflight"].get("ran_at"),
                    "studios": [{k: row.get(k) for k in (
                        "id", "title", "modality", "machine", "host",
                        "version", "latest_version", "version_status",
                        "version_detail", "update_available", "reachable",
                    )} for row in d["preflight"].get("studios", [])],
                }
        if isinstance(d.get("hub_versions"), dict):
            _hub_versions = d["hub_versions"]
        if isinstance(d.get("updates"), dict):
            _updates = d["updates"]
        if isinstance(d.get("hub_updates"), dict):
            _hub_updates = d["hub_updates"]
        # Preserve interrupted operations for diagnosis instead of letting them
        # vanish or remain falsely "running" forever after a Hub restart.
        for job in [*_updates.values(), *_hub_updates.values()]:
            if job.get("status") not in {"queued", "running"}:
                continue
            job.update(status="failed", finished_at=time.time(),
                       restart_interrupted=True)
            for item in job.get("items", []):
                if item.get("status") not in {"complete", "current", "failed"}:
                    item.update(
                        status="failed", finished_at=time.time(),
                        detail="Primary Hub restarted during this operation; retry remotely from this Hub",
                    )
    except (OSError, ValueError):
        pass


_load_state()


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


def _version_key(value: object) -> tuple[int, int, int] | None:
    """Comparable release triplet used by every Studio's update-status API."""
    try:
        parts = [int(part) for part in str(value).strip().lstrip("v").split(".")[:3]]
    except (TypeError, ValueError):
        return None
    if not parts:
        return None
    return tuple((parts + [0, 0, 0])[:3])


def published_version_snapshot() -> dict:
    return {
        "versions": dict(_published_versions),
        "checked_at": _published_checked_at or None,
        "errors": dict(_published_errors),
        "refresh_seconds": PUBLISHED_REFRESH_SECONDS,
    }


async def refresh_published_versions(*, force: bool = False) -> dict:
    """Refresh the canonical GitHub VERSION files, retaining last-known values.

    This is deliberately independent from every Studio's own updater cache.
    One unreachable repository cannot erase or delay the other app versions.
    """
    global _published_checked_at, _published_errors, _published_lock
    now = time.time()
    if not force and now - _published_checked_at < PUBLISHED_REFRESH_SECONDS:
        return published_version_snapshot()
    if _published_lock is None:
        _published_lock = asyncio.Lock()
    async with _published_lock:
        now = time.time()
        if not force and now - _published_checked_at < PUBLISHED_REFRESH_SECONDS:
            return published_version_snapshot()
        results: dict[str, str] = {}
        errors: dict[str, str] = {}
        async with httpx.AsyncClient(
            timeout=6.0,
            headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
        ) as client:
            async def fetch(modality: str, repository: str) -> tuple[str, tuple[str, str] | Exception]:
                try:
                    refs = await client.get(
                        f"https://github.com/{repository}.git/info/refs",
                        params={"service": "git-upload-pack"},
                    )
                    refs.raise_for_status()
                    match = re.search(
                        rb"([0-9a-f]{40}) refs/heads/main(?:\x00|\n)", refs.content)
                    if not match:
                        raise ValueError("main branch ref was not advertised")
                    commit = match.group(1).decode("ascii")
                    if (_published_refs.get(modality) == commit
                            and modality in _published_versions):
                        return modality, (_published_versions[modality], commit)
                    response = await client.get(
                        f"https://raw.githubusercontent.com/{repository}/{commit}/VERSION")
                    response.raise_for_status()
                    version = response.text.strip()
                    if _version_key(version) is None:
                        raise ValueError("invalid VERSION response")
                    return modality, (version, commit)
                except Exception as exc:
                    return modality, exc

            fetched = await asyncio.gather(*(
                fetch(modality, repository)
                for modality, repository in PUBLISHED_REPOSITORIES.items()
            ))
        for modality, value in fetched:
            if isinstance(value, Exception):
                errors[modality] = (str(value).strip() or type(value).__name__)[:160]
            else:
                version, commit = value
                results[modality] = version
                _published_refs[modality] = commit
        _published_versions.update(results)
        _published_errors = errors
        _published_checked_at = time.time()
        return published_version_snapshot()


async def _published_version_loop() -> None:
    try:
        while True:
            try:
                await refresh_published_versions(force=True)
            except Exception as exc:
                print(f"[updates] GitHub VERSION refresh failed: {exc}", flush=True)
            await asyncio.sleep(PUBLISHED_REFRESH_SECONDS)
    except asyncio.CancelledError:
        pass


def start_published_version_monitor() -> None:
    global _published_task
    if _published_task is None or _published_task.done():
        _published_task = asyncio.create_task(_published_version_loop())


async def stop_published_version_monitor() -> None:
    global _published_task
    if _published_task is not None:
        _published_task.cancel()
        try:
            await _published_task
        except asyncio.CancelledError:
            pass
        _published_task = None


def _apply_version_status(row: dict, update: dict | None) -> None:
    """Record what is running, what is published, and the honest relationship.

    `update_available: false` without a `latest_version` is deliberately NOT
    treated as current: sibling Studios fetch the published VERSION in a
    background thread, so their first response can contain a temporary null.
    """
    update = update or {}
    current = update.get("app_version") or row.get("version")
    latest = update.get("latest_version")
    row["version"] = str(current) if current else None
    row["latest_version"] = str(latest) if latest else None
    current_key = _version_key(current)
    latest_key = _version_key(latest)

    if current_key is not None and latest_key is not None:
        if current_key >= latest_key:
            row["version_status"] = "current"
            row["update_available"] = False
            relation = "matches" if current_key == latest_key else "is newer than"
            row["version_detail"] = (
                f"Running v{current} {relation} latest published v{latest}"
            )
        else:
            row["version_status"] = "update_available"
            row["update_available"] = True
            row["version_detail"] = f"Running v{current}; latest published v{latest}"
    elif update.get("update_available") is True:
        row["version_status"] = "update_available"
        row["update_available"] = True
        row["version_detail"] = (
            f"Running v{current or 'unknown'}; the Studio reports an update is available"
        )
    else:
        row["version_status"] = "unknown"
        row["update_available"] = None
        if current:
            row["version_detail"] = (
                f"Running v{current}; latest published version could not be verified"
            )
        else:
            row["version_detail"] = "Running and latest published versions could not be verified"


async def _fetch_update_status(client: httpx.AsyncClient, studio: dict,
                               headers: dict[str, str]) -> dict | None:
    """Read a Studio's public update contract, allowing its async refresh time.

    Current Studios populate `latest_version` in a background thread. A short,
    bounded retry makes a user-initiated rescan return the resolved comparison
    instead of persisting the first transient null response.
    """
    last: dict | None = None
    for attempt, delay in enumerate((0.0, 0.4, 0.8)):
        if delay:
            await asyncio.sleep(delay)
        try:
            response = await client.get(
                f"{base_url(studio)}/api/update-status", headers=headers)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                return None
            last = data
            if data.get("latest_version") or data.get("update_available") is True:
                return data
        except (httpx.HTTPError, ValueError):
            if attempt == 2:
                return last
    return last


async def run_preflight(monitor) -> dict:
    global _preflight
    # A user-initiated scan always bypasses the one-minute background cache.
    await refresh_published_versions(force=True)
    rows = await asyncio.gather(*(_preflight_one(monitor, s) for s in monitor.registry))
    _apply_canonical_published_versions(rows)
    status = "fail" if any(r["status"] == "fail" for r in rows) else (
        "warn" if any(r["status"] == "warn" for r in rows) else "pass")
    _preflight = {"ran_at": time.time(), "status": status, "studios": rows}
    _save_state()
    return _preflight


async def _studio_version_one(studio: dict) -> dict:
    """Read only the version information needed by the update UI."""
    sid = studio["id"]
    row = {
        "id": sid,
        "title": studio.get("title", sid),
        "modality": studio.get("modality", ""),
        "machine": studio.get("machine", "local"),
        "host": studio.get("host"),
        "version": None,
        "latest_version": None,
        "version_status": "unknown",
        "version_detail": "Version scan has not completed",
        "update_available": None,
        "reachable": False,
    }
    headers = peers.studio_headers(studio)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(PREFLIGHT_TIMEOUT)) as client:
            try:
                response = await client.get(f"{base_url(studio)}/api/version")
                response.raise_for_status()
                row["version"] = response.json().get("app_version")
                row["reachable"] = True
            except (httpx.HTTPError, ValueError):
                pass
            update_status = await _fetch_update_status(client, studio, headers)
            _apply_version_status(row, update_status)
            if update_status:
                row["reachable"] = True
    except (httpx.HTTPError, ValueError):
        _apply_version_status(row, None)
    return row


async def scan_studio_versions(monitor) -> dict:
    """Refresh running/latest versions without the retired fleet preflight."""
    global _studio_versions
    await refresh_published_versions(force=True)
    rows = await asyncio.gather(*(
        _studio_version_one(studio) for studio in monitor.registry
    ))
    _apply_canonical_published_versions(rows)
    _studio_versions = {"checked_at": time.time(), "studios": rows}
    _save_state()
    return _studio_versions


def studio_versions_snapshot(monitor=None) -> dict:
    """Return saved versions, excluding machines no longer in the registry."""
    rows = list(_studio_versions.get("studios") or [])
    if monitor is not None:
        known = {studio["id"] for studio in monitor.registry}
        rows = [row for row in rows if row.get("id") in known]
    if rows:
        _apply_canonical_published_versions(rows)
    return {"checked_at": _studio_versions.get("checked_at"), "studios": rows}


def forget_studios(studio_ids: set[str]) -> None:
    """Purge removed Studios from live update-control persistence."""
    global _studio_versions, _preflight
    if not studio_ids:
        return
    _studio_versions = {
        **_studio_versions,
        "studios": [row for row in _studio_versions.get("studios", [])
                    if row.get("id") not in studio_ids],
    }
    _preflight = {
        **_preflight,
        "studios": [row for row in _preflight.get("studios", [])
                    if row.get("id") not in studio_ids],
    }
    _save_state()


def forget_machine(machine: str, studio_ids: set[str] | None = None) -> None:
    """Purge a removed machine from every live fleet-control cache."""
    global _hub_versions
    ids = set(studio_ids or ())
    if not ids:
        ids = {row.get("id") for row in _studio_versions.get("studios", [])
               if row.get("machine") == machine and row.get("id")}
    forget_studios(ids)
    _hub_versions.pop(machine, None)
    _save_state()


def _apply_canonical_published_versions(rows: list[dict],
                                        published: dict[str, str] | None = None) -> None:
    """Compare every worker to one published version per Studio app.

    A worker's ``/api/update-status`` cache is intentionally local and may be
    several hours old.  That must not make an older worker look current just
    because it has not refreshed yet.  Prefer the local Studio's published
    value for each modality, falling back to the highest verified value from a
    reachable worker when the local Studio is unavailable.
    """
    # source priority: GitHub > local Studio > remote Studio when versions are
    # equal. A numerically newer verified value always wins, which also keeps a
    # just-built worker from ever looking like it should downgrade.
    targets: dict[str, tuple[tuple[int, int, int], str, int]] = {}
    for modality, latest in (published or _published_versions).items():
        key = _version_key(latest)
        if key is not None:
            targets[modality] = (key, str(latest), 2)
    for row in rows:
        modality = str(row.get("modality") or "")
        latest = row.get("latest_version")
        key = _version_key(latest)
        if not modality or key is None:
            continue
        priority = 1 if row.get("machine") == "local" else 0
        previous = targets.get(modality)
        if previous is None or key > previous[0] or (
            key == previous[0] and priority > previous[2]
        ):
            targets[modality] = (key, str(latest), priority)

    for row in rows:
        target = targets.get(str(row.get("modality") or ""))
        if target is None:
            continue
        _, latest, _ = target
        # Keep the running version discovered from /api/version, but apply the
        # fleet-wide published target to this individual machine.
        _apply_version_status(row, {"app_version": row.get("version"),
                                    "latest_version": latest})
        check = next((item for item in row.get("checks", [])
                      if item.get("name") == "version"), None)
        if check is not None:
            check.update(status="pass" if row["version_status"] == "current" else "warn",
                         detail=row["version_detail"])
        checks = row.get("checks") or []
        if checks:
            row["status"] = "fail" if any(c.get("status") == "fail" for c in checks) else (
                "warn" if any(c.get("status") == "warn" for c in checks) else "pass"
            )


async def scan_hub_versions(monitor) -> dict:
    """Query each remote machine's Hub /api/version and cache the result with a
    timestamp. Unreachable machines keep their last-known version (so it never
    disappears) and are just marked not-currently-reachable."""
    hosts = _remote_hosts(monitor)

    async def one(machine: str, host: str):
        url = f"http://{host}:{peers.DEFAULT_HUB_PORT}/api/version"
        prev = _hub_versions.get(machine, {})
        try:
            async with httpx.AsyncClient(timeout=6.0) as client:
                r = await client.get(url)
                version = r.json().get("app_version")
            _hub_versions[machine] = {"version": version, "host": host,
                                      "checked_at": time.time(), "reachable": True}
        except (httpx.HTTPError, ValueError):
            _hub_versions[machine] = {"version": prev.get("version"), "host": host,
                                      "checked_at": time.time(), "reachable": False}

    await asyncio.gather(*(one(m, h) for m, h in hosts.items()))
    for machine in set(_hub_versions) - set(hosts):
        _hub_versions.pop(machine, None)
    _save_state()
    return _hub_versions


def hub_versions_snapshot(monitor=None) -> dict:
    if monitor is None:
        return _hub_versions
    known = set(_remote_hosts(monitor))
    return {machine: row for machine, row in _hub_versions.items()
            if machine in known}


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
           "modality": studio.get("modality", ""),
           "machine": studio.get("machine", "local"), "checks": checks,
           "version": None, "latest_version": None,
           "version_status": "unknown", "version_detail": "Version scan has not completed",
           "update_available": None}
    headers = peers.studio_headers(studio)
    timeout = httpx.Timeout(PREFLIGHT_TIMEOUT)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            try:  # public endpoint — show the version even if auth is stale
                vr = await client.get(f"{base_url(studio)}/api/version")
                row["version"] = vr.json().get("app_version")
            except (httpx.HTTPError, ValueError):
                pass
            update_status = await _fetch_update_status(client, studio, headers)
            _apply_version_status(row, update_status)
            checks.append({
                "name": "version",
                "status": "pass" if row["version_status"] == "current" else "warn",
                "detail": row["version_detail"],
            })
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
        if e.response.status_code in {401, 403}:
            # A stale/mismatched fleet token does NOT block updating: the update
            # runs via the machine's own Hub (not the studio's API) and RESTARTS
            # the studio, which reloads the token — i.e. updating fixes this. So
            # it's a warning, not a hard block.
            checks.append({"name": "fleet authentication", "status": "warn",
                           "detail": f"HTTP {e.response.status_code} — studio rejected the fleet "
                                     "token (needs a restart to reload it; updating does that)"})
        else:
            checks.append({"name": "API contract", "status": "fail",
                           "detail": f"HTTP {e.response.status_code}"})
    except (httpx.HTTPError, ValueError) as e:
        detail = str(e).strip() or type(e).__name__
        checks.append({"name": "API contract", "status": "fail", "detail": detail[:160]})

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
    rows = _preflight.get("studios") or []
    if rows:
        _apply_canonical_published_versions(rows)
        _preflight["status"] = "fail" if any(r.get("status") == "fail" for r in rows) else (
            "warn" if any(r.get("status") == "warn" for r in rows) else "pass"
        )
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
    _save_state()
    asyncio.create_task(_run_updates(monitor, job))
    return job


async def _run_updates(monitor, job: dict):
    job["status"] = "running"
    _save_state()
    for item in job["items"]:
        studio = next(s for s in monitor.registry if s["id"] == item["studio"])
        try:
            await _update_one(monitor, studio, item)
        except Exception as e:
            item.update(status="failed", detail=str(e)[:240], finished_at=time.time())
        _save_state()
    final_status = "failed" if any(i["status"] == "failed" for i in job["items"]) else "complete"
    # Refresh the source-of-truth rows before exposing a terminal job. This
    # guarantees the next UI poll replaces action history ("Updated") with the
    # real running-vs-published comparison, including for remote Studios.
    try:
        await scan_studio_versions(monitor)
    except Exception as exc:
        job["refresh_warning"] = f"post-update version scan failed: {str(exc)[:160]}"
    job["status"] = final_status
    job["finished_at"] = time.time()
    _save_state()


def _active_studio_leases() -> set[str]:
    from . import chat_jobs, transcription_jobs
    return (broker.busy_studios() | set(chat_jobs.busy_studios)
            | set(transcription_jobs.busy_studios))


async def _update_one(monitor, studio: dict, item: dict):
    sid = studio["id"]
    item.update(status="draining", detail="waiting for active work to finish", started_at=time.time())
    broker.set_maintenance(sid, True)
    try:
        deadline = time.monotonic() + DRAIN_TIMEOUT
        while sid in _active_studio_leases():
            if time.monotonic() >= deadline:
                raise RuntimeError("drain timed out; update was not started")
            await asyncio.sleep(2)
        if studio.get("machine", "local") != "local":
            await _update_remote(studio, item)
            return
        app_dir = resolve_app_dir(studio)
        if app_dir is None:
            raise RuntimeError(f"Pinokio app folder not found for {studio['id']}")
        version_file = app_dir / "VERSION"
        item["from_version"] = (monitor.status.get(sid, {}).get("app_version")
                                or monitor.status.get(sid, {}).get("health", {}).get("app_version"))
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
    restart_deadline = time.monotonic() + UPDATE_START_TIMEOUT
    headers = peers.studio_headers(studio)
    saw_unavailable = False
    async with httpx.AsyncClient(timeout=5.0) as client:
        while time.monotonic() < deadline:
            await asyncio.sleep(3)
            try:
                r = await client.get(f"{base_url(studio)}/api/health", headers=headers)
                data = r.json()
                version = str(data.get("app_version", "unknown"))
                app_dir = resolve_app_dir(studio)
                if app_dir:
                    try:
                        item["expected_version"] = (app_dir / "VERSION").read_text().strip()
                    except OSError:
                        pass
                expected = item.get("expected_version")
                version_loaded = bool(expected and version.startswith(expected))
                restarted_or_advanced = saw_unavailable or version != str(item.get("from_version"))
                if r.status_code == 200 and data.get("ok") and version_loaded and restarted_or_advanced:
                    item.update(status="complete", detail=f"healthy on v{version}",
                                finished_at=time.time())
                    return
                if not saw_unavailable and time.monotonic() >= restart_deadline:
                    raise RuntimeError(
                        "update command did not restart the Studio within 3 minutes; "
                        "check its update status/log instead of waiting indefinitely"
                    )
            except (httpx.HTTPError, ValueError):
                saw_unavailable = True
        raise RuntimeError("Studio did not return healthy before the update timeout")


async def _update_remote(studio: dict, item: dict):
    url = f"http://{studio['host']}:{studio.get('hub_port', peers.DEFAULT_HUB_PORT)}"
    headers = {"X-Hub-Token": peers.fleet_token() or ""}
    local_id = studio["modality"]
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(f"{url}/api/hub/maintenance/updates", headers=headers,
                              json={"studio_ids": [local_id]})
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail") or r.text
            except (ValueError, AttributeError):
                detail = r.text or f"HTTP {r.status_code}"
            raise RuntimeError(str(detail)[:240])
        remote_id = r.json()["id"]
        item["remote_job_id"] = remote_id
        _save_state()
        deadline = time.monotonic() + UPDATE_TIMEOUT
        while time.monotonic() < deadline:
            await asyncio.sleep(4)
            try:
                status = await client.get(f"{url}/api/hub/maintenance/updates/{remote_id}", headers=headers)
                status.raise_for_status()
                data = status.json()
            except (httpx.TransportError, ValueError) as exc:
                # A Studio restart or a busy peer can drop one status response.
                # The remote Hub still owns the update job, so reconnect to that
                # same job instead of turning a transient transport error into a
                # false failure (or starting the update twice).
                item.update(status="checking", detail=f"connection dropped; reconnecting ({type(exc).__name__})")
                continue
            remote_item = data["items"][0]
            item.update(status=remote_item["status"], detail=remote_item["detail"])
            for key in ("from_version", "expected_version", "to_version"):
                if key in remote_item:
                    item[key] = remote_item[key]
            _save_state()
            remote_started = float(remote_item.get("started_at") or 0)
            if (remote_item.get("status") == "updating" and remote_started
                    and time.time() - remote_started >= UPDATE_START_TIMEOUT):
                raise RuntimeError(
                    "remote update command did not restart the Studio within 3 minutes; "
                    "check that Mac's updater status/log"
                )
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
    _save_state()
    asyncio.create_task(_run_hub_updates(job))
    return job


async def _run_hub_updates(job: dict):
    job["status"] = "running"
    _save_state()
    # peers are independent Macs → update them concurrently; each self-restarts
    await asyncio.gather(*(_update_hub_one(item, job.get("latest")) for item in job["items"]))
    job["status"] = "failed" if any(i["status"] == "failed" for i in job["items"]) else "complete"
    job["finished_at"] = time.time()
    _save_state()


async def _update_hub_one(item: dict, latest: str | None):
    host = item["host"]
    url = f"http://{host}:{peers.DEFAULT_HUB_PORT}"
    headers = {"X-Hub-Token": peers.fleet_token() or ""}
    item.update(status="checking", detail="checking the Hub", started_at=time.time())
    _save_state()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=8.0)) as client:
            cur = ""
            for attempt, delay in enumerate((0, 3, 10, 20), start=1):
                if delay:
                    item.update(status="checking",
                                detail=f"Hub is slow or unreachable; retrying ({attempt}/4)")
                    _save_state()
                    await asyncio.sleep(delay)
                try:
                    v = await client.get(f"{url}/api/version")
                    v.raise_for_status()
                    cur = str(v.json().get("app_version") or "")
                    break
                except (httpx.HTTPError, ValueError):
                    continue
            if not cur:
                raise RuntimeError("Hub unreachable after 4 attempts on :47873 — it may be asleep, "
                                   "offline, blocked by the firewall, or not running")
            item["from_version"] = cur
            if latest and cur == latest:
                item.update(status="current", to_version=cur,
                            detail=f"already on v{cur}", finished_at=time.time())
                return
            item.update(status="updating", detail="pulling latest + restarting")
            _save_state()
            r = await client.post(f"{url}/api/hub/maintenance/self-update", headers=headers)
            if r.status_code == 401:
                raise RuntimeError("remote Hub rejected the fleet token")
            if r.status_code == 404:
                # This peer predates remote self-update (added in 1.25.4). It has
                # no endpoint to receive the command — a one-time manual update
                # seeds the capability, then future updates are remote.
                raise RuntimeError(f"Hub v{cur} is too old for remote update — "
                                   "update it once from the Pinokio sidebar on that "
                                   "Mac (then it's remote from here on)")
            r.raise_for_status()
    except Exception as e:
        item.update(status="failed", detail=str(e)[:240], finished_at=time.time())
        _save_state()
        return
    # the peer now runs update.js and restarts — wait for it to come back
    item.update(status="restarting", detail="waiting for the Hub to come back online")
    _save_state()
    deadline = time.monotonic() + UPDATE_TIMEOUT
    restart_deadline = time.monotonic() + UPDATE_START_TIMEOUT
    saw_down = False
    frm = item.get("from_version")

    def _record(ver: str, status: str, detail: str):
        item.update(status=status, to_version=ver, detail=detail, finished_at=time.time())
        _hub_versions[item["machine"]] = {"version": ver, "host": host,
                                          "checked_at": time.time(), "reachable": True}
        _save_state()

    async with httpx.AsyncClient(timeout=5.0) as client:
        while time.monotonic() < deadline:
            await asyncio.sleep(4)
            try:
                v = await client.get(f"{url}/api/version")
                ver = str(v.json().get("app_version") or "")
                if frm and ver != frm:
                    _record(ver, "complete", f"updated to v{ver}")
                    return
                if saw_down:
                    # It restarted but came back on the SAME version — the update
                    # did not actually apply (git pull / deps likely failed on that
                    # Mac). Report it honestly instead of a misleading "complete".
                    _record(ver, "failed",
                            f"restarted but still on v{ver} — update didn't apply "
                            "(git pull or deps failed on that Mac; update it from "
                            "its Pinokio sidebar and check its logs)")
                    return
                if time.monotonic() >= restart_deadline:
                    item.update(
                        status="failed",
                        detail=(f"still on v{ver or frm or '?'} — update command did not "
                                "restart the Hub within 3 minutes; check its updater status/log"),
                        finished_at=time.time(),
                    )
                    _save_state()
                    return
            except (httpx.HTTPError, ValueError):
                saw_down = True  # it went down to restart — expected
    item.update(status="failed",
                detail=f"still on v{frm or '?'} — the Hub didn't come back on a new "
                       "version before the timeout (the update may have failed on that Mac)",
                finished_at=time.time())
    _save_state()


def hub_update_snapshot(job_id: str | None = None):
    if job_id:
        return _hub_updates.get(job_id)
    return sorted(_hub_updates.values(), key=lambda j: j["created_at"], reverse=True)[:20]


def hub_update_blockers() -> list[str]:
    """Active Hub-owned work that must survive an automatic Hub update."""
    reasons: list[str] = []
    if any(job["status"] in {"queued", "running"} for job in _updates.values()):
        reasons.append("a rolling Studio update is active")
    if any(job["status"] in {"queued", "running"} for job in _hub_updates.values()):
        reasons.append("an agent Hub update is active")
    if _active_studio_leases():
        reasons.append("a fleet worker owns an active lease")
    generation_active = any(
        item.get("state") not in {"done", "error", "cancelled"}
        for batch in broker.batches.values() if not batch.get("cancelled")
        for item in batch.get("items", [])
    )
    if generation_active:
        reasons.append("a generation batch is queued or running")
    from . import chat_jobs, transcription_jobs
    if any(batch.get("status") in {"queued", "running"} for batch in chat_jobs.list_batches()):
        reasons.append("a Chat batch is queued or running")
    if any(batch.get("status") in {"queued", "running"} for batch in transcription_jobs.list_batches()):
        reasons.append("a transcription batch is queued or running")
    return reasons
