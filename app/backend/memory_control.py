"""Fleet controller for the sibling Studios' opt-in model-memory policies."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from . import peers
from .registry import label_for


SUPPORTED_MODALITIES = {"image", "chat", "video", "music", "voice"}
MODES = {
    "performance": {"label": "Performance", "idle_seconds": None},
    "balanced": {"label": "Balanced", "idle_seconds": 600},
    "memory_saver": {"label": "Memory Saver", "idle_seconds": 120},
    "immediate": {"label": "Immediate", "idle_seconds": 0},
}
DEFAULT_MODE = "performance"
REQUEST_TIMEOUT = httpx.Timeout(15.0, connect=5.0)


def _error_detail(response: httpx.Response) -> str:
    try:
        value = response.json()
        if isinstance(value, dict):
            return str(value.get("detail") or value.get("message") or "").strip()
    except (ValueError, TypeError):
        pass
    return response.text.strip()[:300] or f"HTTP {response.status_code}"


class FleetMemoryControl:
    """Read and change memory policy without owning any model state itself."""

    def __init__(self, monitor):
        self.monitor = monitor

    def targets(self) -> list[dict[str, Any]]:
        return sorted(
            [studio for studio in self.monitor.registry
             if studio.get("modality") in SUPPORTED_MODALITIES],
            key=lambda row: (
                str(row.get("machine", "local")),
                str(row.get("modality", "")),
                str(row.get("id", "")),
            ),
        )

    def _selected(self, studio_ids: list[str] | None) -> list[dict[str, Any]]:
        targets = self.targets()
        if studio_ids is None:
            return targets
        if not studio_ids:
            raise ValueError("select at least one Studio")
        requested = set(studio_ids)
        known = {studio["id"] for studio in targets}
        unknown = sorted(requested - known)
        if unknown:
            raise ValueError("unknown memory-control studio(s): " + ", ".join(unknown))
        return [studio for studio in targets if studio["id"] in requested]

    async def _request(self, studio: dict[str, Any], method: str, path: str,
                       payload: dict[str, Any] | None = None) -> httpx.Response:
        url, headers = peers.studio_request(studio, path)
        return await self.monitor._client.request(
            method, url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT,
        )

    def _base_row(self, studio: dict[str, Any]) -> dict[str, Any]:
        state = self.monitor.status.get(studio["id"], {})
        return {
            "id": studio["id"],
            "title": studio.get("title", studio["id"]),
            "modality": studio.get("modality"),
            "machine": studio.get("machine", "local"),
            "machine_label": studio.get("machine_label") or label_for(
                studio.get("machine", "local")),
            "url": peers.studio_request(studio, "/")[0].rsplit("/", 1)[0],
            "health": state.get("status", "unknown"),
        }

    async def _status_one(self, studio: dict[str, Any]) -> dict[str, Any]:
        row = self._base_row(studio)
        if row["health"] == "down":
            return {**row, "state": "offline", "supported": None,
                    "detail": "Studio is offline"}
        try:
            response = await self._request(studio, "GET", "/api/memory-policy")
        except (httpx.HTTPError, OSError) as exc:
            return {**row, "state": "offline", "supported": None,
                    "detail": f"Could not reach Studio: {exc}"}
        if response.status_code == 404:
            return {**row, "state": "update_required", "supported": False,
                    "detail": "Run Update on this Studio to add memory controls"}
        if not response.is_success:
            return {**row, "state": "error", "supported": None,
                    "detail": _error_detail(response)}
        try:
            policy = response.json()
        except ValueError:
            return {**row, "state": "error", "supported": None,
                    "detail": "Studio returned an invalid memory-policy response"}
        if not isinstance(policy, dict) or policy.get("mode") not in MODES:
            return {**row, "state": "error", "supported": None,
                    "detail": "Studio returned an invalid memory policy"}
        return {**row, "state": "ready", "supported": True, "detail": None,
                "policy": policy}

    async def inventory(self) -> dict[str, Any]:
        studios = await asyncio.gather(*(self._status_one(s) for s in self.targets()))
        return {
            "default_mode": DEFAULT_MODE,
            "options": [{"mode": mode, **value} for mode, value in MODES.items()],
            "studios": studios,
            "summary": {
                "total": len(studios),
                "ready": sum(row["state"] == "ready" for row in studios),
                "offline": sum(row["state"] == "offline" for row in studios),
                "update_required": sum(row["state"] == "update_required" for row in studios),
            },
        }

    async def _change_one(self, studio: dict[str, Any], method: str, path: str,
                          payload: dict[str, Any] | None, success: str) -> dict[str, Any]:
        row = self._base_row(studio)
        try:
            response = await self._request(studio, method, path, payload)
        except (httpx.HTTPError, OSError) as exc:
            return {**row, "ok": False, "result": "offline",
                    "detail": f"Could not reach Studio: {exc}"}
        if response.status_code == 404:
            return {**row, "ok": False, "result": "update_required",
                    "detail": "Run Update on this Studio to add memory controls"}
        if response.status_code == 409:
            return {**row, "ok": False, "result": "busy",
                    "detail": _error_detail(response)}
        if not response.is_success:
            return {**row, "ok": False, "result": "error",
                    "detail": _error_detail(response)}
        try:
            policy = response.json()
        except ValueError:
            policy = {}
        return {**row, "ok": True, "result": success, "detail": None,
                "policy": policy if isinstance(policy, dict) else {}}

    @staticmethod
    def _operation(results: list[dict[str, Any]], action: str) -> dict[str, Any]:
        succeeded = sum(bool(row.get("ok")) for row in results)
        return {
            "ok": succeeded == len(results),
            "action": action,
            "selected": len(results),
            "succeeded": succeeded,
            "failed": len(results) - succeeded,
            "results": results,
        }

    async def set_mode(self, mode: str, studio_ids: list[str] | None = None) -> dict[str, Any]:
        if mode not in MODES:
            raise ValueError("mode must be one of: " + ", ".join(MODES))
        targets = self._selected(studio_ids)
        results = await asyncio.gather(*(
            self._change_one(studio, "PUT", "/api/memory-policy", {"mode": mode}, "updated")
            for studio in targets
        ))
        operation = self._operation(results, "set_mode")
        operation["mode"] = mode
        return operation

    async def release(self, studio_ids: list[str] | None = None) -> dict[str, Any]:
        targets = self._selected(studio_ids)
        results = await asyncio.gather(*(
            self._change_one(studio, "POST", "/api/memory/release", None, "released")
            for studio in targets
        ))
        return self._operation(results, "release")
