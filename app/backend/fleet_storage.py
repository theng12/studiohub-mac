"""Fleet-wide local-backup storage protection.

Every Studio owns the rules for deleting its outputs. This coordinator only
pushes a common policy, asks those protected APIs to clean, and applies one
combined cap per physical Mac. Peer Hubs enforce their own Mac locally so the
system keeps working even when the primary Hub or dashboard is closed.
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
from fastapi import HTTPException

from . import job_storage, peers
from .registry import DATA_DIR, label_for

SETTINGS_FILE = DATA_DIR / "fleet_storage_policy.json"
DEFAULT_POLICY = {"enabled": True, "retention_days": 3, "max_gb": 80.0}
RETENTION_CHOICES = {1, 3, 7, 15, 30, 90}
CHECK_INTERVAL_SECONDS = 60 * 60
REQUEST_TIMEOUT_SECONDS = 12.0
PEER_TIMEOUT_SECONDS = 30.0

_task: asyncio.Task | None = None
_lock: asyncio.Lock | None = None
_lock_loop = None
_last_local: dict | None = None


def read_policy() -> dict:
    try:
        saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        saved = {}
    enabled = saved.get("enabled", DEFAULT_POLICY["enabled"])
    retention = saved.get("retention_days", DEFAULT_POLICY["retention_days"])
    maximum = saved.get("max_gb", DEFAULT_POLICY["max_gb"])
    if not isinstance(enabled, bool):
        enabled = DEFAULT_POLICY["enabled"]
    if not isinstance(retention, int) or retention not in RETENTION_CHOICES:
        retention = DEFAULT_POLICY["retention_days"]
    if not isinstance(maximum, (int, float)) or isinstance(maximum, bool) or not 1 <= maximum <= 1000:
        maximum = DEFAULT_POLICY["max_gb"]
    return {"enabled": enabled, "retention_days": retention, "max_gb": float(maximum)}


def save_policy(enabled: object, retention_days: object, max_gb: object) -> dict:
    if not isinstance(enabled, bool):
        raise HTTPException(400, "enabled must be true or false")
    if not isinstance(retention_days, int) or retention_days not in RETENTION_CHOICES:
        raise HTTPException(400, "retention_days must be 1, 3, 7, 15, 30, or 90")
    if (not isinstance(max_gb, (int, float)) or isinstance(max_gb, bool)
            or not 1 <= float(max_gb) <= 1000):
        raise HTTPException(400, "max_gb must be between 1 and 1000")
    value = {"enabled": enabled, "retention_days": retention_days,
             "max_gb": float(max_gb)}
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    partial = SETTINGS_FILE.with_suffix(".json.tmp")
    partial.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    partial.replace(SETTINGS_FILE)
    return value


def _payload(policy: dict) -> dict:
    return {"enabled": policy["enabled"],
            "retention_days": policy["retention_days"],
            "max_gb": policy["max_gb"]}


async def _studio_call(client, studio: dict, method: str, path: str,
                       body: dict | None = None) -> dict:
    url, headers = peers.studio_request(studio, path)
    try:
        caller = getattr(client, method.lower())
        kwargs = {"headers": headers, "timeout": REQUEST_TIMEOUT_SECONDS}
        if body is not None:
            kwargs["json"] = body
        response = await caller(url, **kwargs)
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise ValueError("storage policy returned a non-object response")
        return value
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise RuntimeError(str(exc)) from exc


def _store_row(studio: dict, value: dict | None = None,
               error: str | None = None) -> dict:
    value = value or {}
    return {
        "id": studio["id"], "app": studio.get("modality", "unknown"),
        "title": studio.get("title", studio["id"]),
        "supported": bool(value.get("supported", True)) if not error else False,
        "reachable": error is None,
        "used_bytes": max(0, int(value.get("used_bytes") or 0)),
        "count": max(0, int(value.get("count") or 0)),
        "scope": value.get("scope"), "error": error,
        "over_limit": bool(value.get("over_limit", False)),
    }


async def _worker_status(client, studio: dict, policy: dict,
                         apply_policy: bool, cleanup: bool) -> dict:
    try:
        value = None
        if apply_policy:
            value = await _studio_call(client, studio, "put", "/api/storage-policy",
                                       _payload(policy))
        if cleanup and policy["enabled"]:
            value = await _studio_call(client, studio, "post",
                                       "/api/storage-policy/cleanup", {})
        if value is None:
            value = await _studio_call(client, studio, "get", "/api/storage-policy")
        return _store_row(studio, value=value)
    except RuntimeError as exc:
        return _store_row(studio, error=str(exc))


async def _shrink_worker(client, studio: dict, target_bytes: int) -> dict:
    try:
        value = await _studio_call(client, studio, "post",
                                   "/api/storage-policy/cleanup",
                                   {"target_bytes": max(0, int(target_bytes))})
        return _store_row(studio, value=value)
    except RuntimeError as exc:
        return _store_row(studio, error=str(exc))


def _hub_row(value: dict) -> dict:
    return {
        "id": "studiohub", "app": "hub", "title": "Studio Hub KH",
        "supported": True, "reachable": True,
        "used_bytes": max(0, int(value.get("used_bytes") or 0)),
        "count": max(0, int(value.get("cleared") or 0)),
        "scope": value.get("scope", "Hub-local transcription files"),
        "error": None, "over_limit": bool(value.get("over_limit", False)),
    }


def _report(machine: str, stores: list[dict], policy: dict,
            reclaimed_bytes: int = 0) -> dict:
    used = sum(row["used_bytes"] for row in stores
               if row["supported"] and row["reachable"])
    maximum = round(float(policy["max_gb"]) * 1024 ** 3)
    return {
        "machine": machine, "machine_label": label_for(machine),
        "checked_at": time.time(), "used_bytes": used, "max_bytes": maximum,
        "over_limit": bool(policy["enabled"] and used > maximum),
        "reclaimed_bytes": reclaimed_bytes, "stores": stores,
        "errors": sum(1 for row in stores if not row["reachable"]),
    }


async def local_status(monitor, *, apply_policy: bool = False,
                       cleanup: bool = False) -> dict:
    """Inspect or enforce this Hub's physical Mac only."""
    global _lock, _lock_loop, _last_local
    loop = asyncio.get_running_loop()
    if _lock is None or _lock_loop is not loop:
        _lock = asyncio.Lock()
        _lock_loop = loop
    async with _lock:
        policy = read_policy()
        local_studios = []
        seen_endpoints = set()
        for studio in monitor.registry:
            if studio.get("machine", "local") != "local":
                continue
            endpoint = (studio.get("host"), studio.get("port"),
                        studio.get("modality"))
            if endpoint in seen_endpoints:
                continue
            seen_endpoints.add(endpoint)
            local_studios.append(studio)
        rows = await asyncio.gather(*(
            _worker_status(monitor._client, studio, policy, apply_policy, cleanup)
            for studio in local_studios
        ))
        hub_before = job_storage.status()
        if apply_policy:
            hub_before = job_storage.save(policy["enabled"], policy["max_gb"],
                                          policy["retention_days"])
        reclaimed = 0
        if cleanup and policy["enabled"]:
            hub_cleaned = job_storage.enforce_budget()
            reclaimed += int(hub_cleaned.get("reclaimed_bytes") or 0)
            hub_before = {**job_storage.status(), **hub_cleaned}
        stores = [*rows, _hub_row(hub_before)]

        maximum = round(float(policy["max_gb"]) * 1024 ** 3)
        total = sum(row["used_bytes"] for row in stores
                    if row["supported"] and row["reachable"])
        if cleanup and policy["enabled"] and total > maximum:
            by_id = {studio["id"]: studio for studio in local_studios}
            # Largest disposable store first minimizes cross-app churn. If a
            # store is mostly active/protected, move on instead of looping.
            for current in sorted(stores, key=lambda row: row["used_bytes"], reverse=True):
                excess = total - maximum
                if excess <= 0:
                    break
                if not current["supported"] or not current["reachable"] or current["used_bytes"] <= 0:
                    continue
                target = max(0, current["used_bytes"] - excess)
                before = current["used_bytes"]
                if current["id"] == "studiohub":
                    value = job_storage.enforce_budget(target)
                    replacement = _hub_row({**job_storage.status(), **value})
                    reclaimed += int(value.get("reclaimed_bytes") or 0)
                else:
                    replacement = await _shrink_worker(
                        monitor._client, by_id[current["id"]], target)
                current.update(replacement)
                total -= max(0, before - current["used_bytes"])

        _last_local = _report("local", stores, policy, reclaimed)
        return {"policy": policy, "machines": [_last_local]}


def _peer_request(studio: dict, path: str) -> tuple[str, dict]:
    token = studio.get("hub_token") or peers.fleet_token()
    headers = {"X-Hub-Token": token} if token else {}
    url = f"http://{studio['host']}:{studio.get('hub_port', peers.DEFAULT_HUB_PORT)}{path}"
    return url, headers


async def _peer_call(client, machine: str, studio: dict, method: str,
                     body: dict | None = None) -> dict:
    endpoint = ("/api/hub/storage-policy/cleanup" if method.lower() == "post"
                else "/api/hub/storage-policy")
    url, headers = _peer_request(
        studio, f"{endpoint}?local_only=true")
    try:
        caller = getattr(client, method.lower())
        kwargs = {"headers": headers, "timeout": PEER_TIMEOUT_SECONDS}
        if body is not None:
            kwargs["json"] = body
        response = await caller(url, **kwargs)
        response.raise_for_status()
        value = response.json()
        report = (value.get("machines") or [None])[0]
        if not isinstance(report, dict):
            raise ValueError("peer Hub returned no local storage report")
        report = {**report, "machine": machine,
                  "machine_label": label_for(machine)}
        return report
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        return {
            "machine": machine, "machine_label": label_for(machine),
            "checked_at": time.time(), "used_bytes": 0,
            "max_bytes": round(read_policy()["max_gb"] * 1024 ** 3),
            "over_limit": False, "reclaimed_bytes": 0, "stores": [],
            "errors": 1, "error": str(exc),
        }


def _remote_samples(monitor) -> dict[str, dict]:
    samples = {}
    for studio in monitor.registry:
        machine = studio.get("machine", "local")
        if machine != "local":
            samples.setdefault(machine, studio)
    return samples


async def fleet_status(monitor) -> dict:
    remotes = _remote_samples(monitor)
    local, *rows = await asyncio.gather(
        local_status(monitor),
        *(_peer_call(monitor._client, machine, studio, "get")
          for machine, studio in remotes.items()),
    )
    return {"policy": read_policy(), "machines": [*local["machines"], *rows]}


async def save_fleet(monitor, enabled: object, retention_days: object,
    max_gb: object, *, local_only: bool = False) -> dict:
    policy = save_policy(enabled, retention_days, max_gb)
    if local_only:
        return await local_status(monitor, apply_policy=True)
    remotes = _remote_samples(monitor)
    local, *rows = await asyncio.gather(
        local_status(monitor, apply_policy=True),
        *(_peer_call(monitor._client, machine, studio, "put", _payload(policy))
          for machine, studio in remotes.items()),
    )
    return {"policy": policy, "machines": [*local["machines"], *rows]}


async def cleanup_fleet(monitor, *, local_only: bool = False) -> dict:
    if local_only:
        return await local_status(monitor, apply_policy=True, cleanup=True)
    remotes = _remote_samples(monitor)
    local, *rows = await asyncio.gather(
        local_status(monitor, apply_policy=True, cleanup=True),
        *(_peer_call(monitor._client, machine, studio, "post", {})
          for machine, studio in remotes.items()),
    )
    return {"policy": read_policy(), "machines": [*local["machines"], *rows]}


async def _loop(monitor) -> None:
    while True:
        try:
            await local_status(monitor, apply_policy=True, cleanup=True)
        except Exception:
            # A single unavailable Studio must never stop future self-healing.
            pass
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


def start(monitor) -> None:
    global _task
    loop = asyncio.get_running_loop()
    if _task is None or _task.done() or _task.get_loop() is not loop:
        _task = asyncio.create_task(_loop(monitor))


async def stop() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
        await asyncio.gather(_task, return_exceptions=True)
    _task = None
