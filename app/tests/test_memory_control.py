from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from backend.memory_control import DEFAULT_MODE, FleetMemoryControl


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class FakeMonitor:
    def __init__(self, responses):
        self.registry = [
            {"id": "image", "title": "Image Studio KH", "modality": "image",
             "host": "127.0.0.1", "port": 47868, "machine": "local"},
            {"id": "voice@renderbox", "title": "Voice Studio KH", "modality": "voice",
             "host": "10.0.0.8", "port": 47870, "hub_port": 47873,
             "machine": "renderbox"},
            {"id": "render", "title": "Render Studio KH", "modality": "render",
             "host": "127.0.0.1", "port": 47874, "machine": "local"},
        ]
        self.status = {"image": {"status": "up"},
                       "voice@renderbox": {"status": "up"},
                       "render": {"status": "up"}}
        self._client = FakeClient(responses)


def response(status, data=None):
    return httpx.Response(status, json=data or {})


def test_inventory_uses_authenticated_direct_and_peer_routes():
    monitor = FakeMonitor([
        response(200, {"mode": "performance", "loaded_model": "flux",
                       "process_title": "Image Studio Mac"}),
        response(200, {"mode": "balanced", "loaded_models": ["qwen-tts"],
                       "process_title": "Voice Studio Mac"}),
    ])
    control = FleetMemoryControl(monitor)

    data = asyncio.run(control.inventory())

    assert data["default_mode"] == DEFAULT_MODE == "performance"
    assert [row["id"] for row in data["studios"]] == ["image", "voice@renderbox"]
    assert data["summary"] == {"total": 2, "ready": 2, "offline": 0,
                               "update_required": 0}
    direct, remote = monitor._client.calls
    assert direct[1] == "http://127.0.0.1:47868/api/memory-policy"
    assert direct[2]["headers"]["X-Studio-Token"]
    assert remote[1] == "http://10.0.0.8:47873/studio/voice/api/memory-policy"
    assert remote[2]["headers"]["X-Hub-Token"]


def test_inventory_explains_offline_and_old_studios():
    monitor = FakeMonitor([response(404)])
    monitor.status["voice@renderbox"]["status"] = "down"

    data = asyncio.run(FleetMemoryControl(monitor).inventory())

    assert data["studios"][0]["state"] == "update_required"
    assert "Run Update" in data["studios"][0]["detail"]
    assert data["studios"][1]["state"] == "offline"
    assert len(monitor._client.calls) == 1


def test_policy_fanout_keeps_success_when_another_studio_is_busy():
    monitor = FakeMonitor([
        response(200, {"mode": "memory_saver"}),
        response(409, {"detail": "Voice generation is active; memory was not released"}),
    ])

    result = asyncio.run(FleetMemoryControl(monitor).set_mode("memory_saver"))

    assert result["ok"] is False
    assert result["succeeded"] == 1 and result["failed"] == 1
    assert [row["result"] for row in result["results"]] == ["updated", "busy"]
    assert monitor._client.calls[0][2]["json"] == {"mode": "memory_saver"}


def test_manual_release_reports_each_studio_independently():
    monitor = FakeMonitor([
        response(200, {"mode": "performance", "last_release_reason": "manual"}),
        httpx.ConnectError("sleeping"),
    ])

    result = asyncio.run(FleetMemoryControl(monitor).release(["image", "voice@renderbox"]))

    assert result["succeeded"] == 1 and result["failed"] == 1
    assert result["results"][0]["result"] == "released"
    assert result["results"][1]["result"] == "offline"


def test_memory_routes_validate_and_expose_friendly_hub_title(authed, monkeypatch):
    from backend import main

    async def inventory():
        return {"default_mode": "performance", "options": [], "studios": [],
                "summary": {"total": 0, "ready": 0, "offline": 0,
                            "update_required": 0}}

    async def set_mode(mode, studio_ids):
        assert mode == "balanced" and studio_ids == ["image"]
        return {"ok": True, "action": "set_mode", "mode": mode,
                "selected": 1, "succeeded": 1, "failed": 0, "results": []}

    async def release(studio_ids):
        assert studio_ids == ["image"]
        return {"ok": True, "action": "release", "selected": 1,
                "succeeded": 1, "failed": 0, "results": []}

    monkeypatch.setattr(main.memory_control, "inventory", inventory)
    monkeypatch.setattr(main.memory_control, "set_mode", set_mode)
    monkeypatch.setattr(main.memory_control, "release", release)

    assert authed.get("/api/hub/memory").json()["default_mode"] == "performance"
    assert authed.put("/api/hub/memory-policy", json={
        "mode": "balanced", "studio_ids": ["image"],
    }).json()["succeeded"] == 1
    assert authed.post("/api/hub/memory/release", json={
        "studio_ids": ["image"],
    }).json()["succeeded"] == 1
    assert authed.get("/api/health").json()["process_title"] == "Studio Hub Mac"


def test_whats_new_is_read_from_the_current_changelog(authed):
    data = authed.get("/api/releases").json()
    assert data["current_version"]
    assert data["releases"]
    assert data["releases"][0]["version"] == data["current_version"]
    assert data["releases"][0]["details"]


def test_memory_dashboard_preserves_an_unsaved_mode_draft():
    dashboard = (Path(__file__).parents[1] / "frontend" / "index.html").read_text()
    assert 'data-tab="memory"' in dashboard
    assert 'id="tab-memory"' in dashboard
    assert 'data-mode="performance"' in dashboard
    assert 'data-mode="balanced"' in dashboard
    assert 'data-mode="memory_saver"' in dashboard
    assert 'data-mode="immediate"' in dashboard
    assert 'let memoryModeDraft = "performance";' in dashboard
    assert 'if (!memorySelectionInitialized)' in dashboard
    assert 'setMemoryModeDraft(memoryModeDraft);' in dashboard
    assert 'if (btn.dataset.tab === "memory") loadFleetMemory();' in dashboard
    assert 'if (!document.hidden && vis("memory") && !memoryBusy) loadFleetMemory();' in dashboard
    assert 'api("/api/releases")' in dashboard
