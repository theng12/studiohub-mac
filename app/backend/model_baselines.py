"""Best-effort fleet baselines for small, operationally useful models.

Baselines are deliberately site-local.  A controller asks each registered
Studio to cache a model, but never changes GenStudio routing or customer-job
ownership.  Failures are retained as observable status and retried later; they
must never block the Hub scheduler.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx

from . import peers


WHISPER_TINY_REPO = "mlx-community/whisper-tiny"
DEFAULT_RECONCILE_SECONDS = 15 * 60


class FleetModelBaselines:
    def __init__(self, monitor, *, state_path: Path,
                 reconcile_seconds: float = DEFAULT_RECONCILE_SECONDS):
        self.monitor = monitor
        self.state_path = state_path
        self.reconcile_seconds = max(60.0, float(reconcile_seconds))
        self.enabled = True
        self.last_reconciled_at: float | None = None
        self.targets: dict[str, dict[str, Any]] = {}
        self._task: asyncio.Task | None = None
        self._lock: asyncio.Lock | None = None
        self._load()

    def _load(self) -> None:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        if isinstance(payload, dict):
            self.enabled = bool(payload.get("enabled", True))
            value = payload.get("last_reconciled_at")
            self.last_reconciled_at = float(value) if isinstance(value, (int, float)) else None
            rows = payload.get("targets")
            if isinstance(rows, dict):
                self.targets = {
                    str(key): dict(value) for key, value in rows.items()
                    if isinstance(value, dict)
                }

    def _save(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            temporary.write_text(json.dumps({
                "schema_version": 1,
                "enabled": self.enabled,
                "last_reconciled_at": self.last_reconciled_at,
                "targets": self.targets,
            }, indent=2) + "\n", encoding="utf-8")
            temporary.replace(self.state_path)
        except OSError:
            pass

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _loop(self) -> None:
        try:
            # Let the first monitor poll establish truthful reachability before
            # attempting any download on startup.
            await asyncio.sleep(30)
            while True:
                if self.enabled:
                    try:
                        await self.reconcile()
                    except Exception:
                        # A baseline is a self-healing convenience, never a
                        # scheduler dependency.  Per-target errors remain in
                        # the public snapshot for the next retry.
                        pass
                await asyncio.sleep(self.reconcile_seconds)
        except asyncio.CancelledError:
            pass

    def save_settings(self, *, enabled: bool) -> dict[str, Any]:
        self.enabled = bool(enabled)
        self._save()
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        voice_targets = [
            studio for studio in self.monitor.registry
            if studio.get("modality") == "voice"
        ]
        rows = []
        for studio in voice_targets:
            row = dict(self.targets.get(studio["id"]) or {})
            row.update({
                "studio_id": studio["id"],
                "machine": studio.get("machine", "local"),
                "reachable": self.monitor.status.get(studio["id"], {}).get("status") == "up",
            })
            row.setdefault("state", "unknown")
            rows.append(row)
        return {
            "schema_version": 1,
            "enabled": self.enabled,
            "repo": WHISPER_TINY_REPO,
            "label": "Whisper Tiny",
            "size_gb": 0.07,
            "scope": "voice-studio transcription workers only",
            "last_reconciled_at": self.last_reconciled_at,
            "reconcile_seconds": self.reconcile_seconds,
            "targets": rows,
            "summary": {
                "total": len(rows),
                "cached": sum(row.get("state") == "cached" for row in rows),
                "pending": sum(row.get("state") in {"queued", "running"} for row in rows),
                "failed": sum(row.get("state") == "error" for row in rows),
            },
        }

    async def reconcile(self) -> dict[str, Any]:
        if self._lock is None:
            self._lock = asyncio.Lock()
        if self._lock.locked():
            return self.snapshot()
        async with self._lock:
            for studio in self.monitor.registry:
                if studio.get("modality") != "voice":
                    continue
                await self._reconcile_one(studio)
            self.last_reconciled_at = time.time()
            self._save()
            return self.snapshot()

    async def _reconcile_one(self, studio: dict[str, Any]) -> None:
        studio_id = studio["id"]
        status = self.monitor.status.get(studio_id, {}).get("status")
        if status != "up":
            self.targets[studio_id] = {
                "state": "offline",
                "detail": "Voice Studio is not reachable; retrying automatically",
                "checked_at": time.time(),
            }
            return
        try:
            availability = await self.monitor.get_transcription(studio, force=True)
            model = next((row for row in (availability or {}).get("models", [])
                          if row.get("repo") == WHISPER_TINY_REPO), None)
            if isinstance(model, dict) and model.get("cached"):
                self.targets[studio_id] = {
                    "state": "cached", "detail": "Whisper Tiny is ready",
                    "checked_at": time.time(),
                }
                return
            url, headers = peers.studio_request(studio, "/api/downloads")
            async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=8.0)) as client:
                response = await client.post(
                    url, headers=headers, json={"repo": WHISPER_TINY_REPO})
                response.raise_for_status()
                payload = response.json()
            job = payload.get("job") if isinstance(payload, dict) else None
            state = str((job or {}).get("state") or "queued")
            self.targets[studio_id] = {
                "state": state,
                "detail": "Whisper Tiny download accepted",
                "job_id": (job or {}).get("id"),
                "checked_at": time.time(),
            }
        except (httpx.HTTPError, ValueError, TypeError, OSError) as exc:
            self.targets[studio_id] = {
                "state": "error",
                "detail": (str(exc).strip() or type(exc).__name__)[:220],
                "checked_at": time.time(),
            }
