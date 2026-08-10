import httpx
import pytest

from backend import broadcast


class _FakeResp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._p = payload or {}
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeClient:
    def __init__(self):
        self.calls = []

    async def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append((url, json))
        return _FakeResp(200, {"job": {"id": "j1"}})


class _DownloadStatusClient:
    async def get(self, url, headers=None, timeout=None):
        if url.endswith("/api/downloads"):
            return _FakeResp(200, {"jobs": [{
                "id": "j1", "state": "running", "percent": 42.5,
                "bytes_observed": 425, "bytes_total": 1000,
                "speed_bps": 50, "eta_seconds": 11,
            }]})
        raise AssertionError(url)

    async def delete(self, url, headers=None, timeout=None):
        assert url.endswith("/api/downloads/j1")
        return _FakeResp(200, {"job": {"id": "j1", "state": "cancelling"}})


class _UnavailableDownloadStatusClient:
    async def get(self, url, headers=None, timeout=None):
        raise httpx.ConnectError("worker is sleeping")


@pytest.mark.asyncio
async def test_broadcast_download_fans_out_to_each_studio():
    studios = [
        {"id": "chat", "host": "127.0.0.1", "port": 47871, "modality": "chat"},
        {"id": "chat@mac-b", "host": "10.0.0.2", "port": 47871, "modality": "chat"},
    ]
    c = _FakeClient()
    out = await broadcast.broadcast_download(c, studios, "mlx-community/Qwen3-4B-Instruct-2507-4bit")
    assert len(c.calls) == 2
    assert all(u.endswith("/api/downloads") for u, _ in c.calls)
    assert all(j["repo"] == "mlx-community/Qwen3-4B-Instruct-2507-4bit" for _, j in c.calls)
    assert out["chat"]["ok"] and out["chat"]["job"] == "j1"
    assert out["chat@mac-b"]["ok"]


@pytest.mark.asyncio
async def test_fleet_download_progress_is_durable_and_cancellable(reset):
    studios = [{
        "id": "image@mac-b", "host": "10.0.0.2", "port": 47868,
        "machine": "mac-b", "modality": "image",
    }]
    run = broadcast.record_download(
        "org/model", studios, {"image@mac-b": {"ok": True, "job": "j1"}})

    refreshed = await broadcast.refresh_downloads(_DownloadStatusClient(), studios)
    item = refreshed[0]["items"][0]
    assert item["percent"] == 42.5 and item["bytes_observed"] == 425
    assert broadcast.DOWNLOADS_FILE.exists()

    cancelled = await broadcast.cancel_download(
        _DownloadStatusClient(), run["id"], "image@mac-b", studios)
    assert cancelled["items"][0]["state"] == "cancelling"


@pytest.mark.asyncio
async def test_unreachable_worker_preserves_last_known_download_progress(reset):
    studios = [{
        "id": "image@mac-b", "host": "10.0.0.2", "port": 47868,
        "machine": "mac-b", "modality": "image",
    }]
    broadcast.record_download(
        "org/model", studios, {"image@mac-b": {"ok": True, "job": "j1"}})
    observed = await broadcast.refresh_downloads(_DownloadStatusClient(), studios)
    before = observed[0]["items"][0]
    assert before["percent"] == 42.5 and before["bytes_observed"] == 425

    unavailable = await broadcast.refresh_downloads(
        _UnavailableDownloadStatusClient(), studios)
    after = unavailable[0]["items"][0]

    assert after["state"] == "running"
    assert after["percent"] == 42.5
    assert after["bytes_observed"] == 425 and after["bytes_total"] == 1000
    assert after["speed_bps"] == 50 and after["eta_seconds"] == 11
    assert after["reachable"] is False
    assert after["detail"].startswith("worker unavailable")


def test_broadcast_download_endpoint_returns_tracked_run(authed, monkeypatch):
    from backend import main

    async def fanout(client, studios, repo, token=None):
        return {studio["id"]: {"ok": True, "job": "tracked"} for studio in studios}

    monkeypatch.setattr(main.broadcast, "broadcast_download", fanout)
    response = authed.post("/api/hub/broadcast/download", json={
        "repo": "org/model", "studios": ["image"],
    })
    assert response.status_code == 200
    assert response.json()["download"]["items"][0]["job_id"] == "tracked"


def test_broadcast_download_endpoint_requires_repo(authed):
    assert authed.post("/api/hub/broadcast/download", json={}).status_code == 400


class _CapSettingsClient:
    def __init__(self, status=200):
        self.calls = []
        self._status = status

    async def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append((url, json))
        return _FakeResp(self._status, {})


@pytest.mark.asyncio
async def test_broadcast_hf_token_partial_update_to_settings():
    studios = [
        {"id": "chat", "host": "127.0.0.1", "port": 47871, "modality": "chat"},
        {"id": "image@mac-b", "host": "10.0.0.2", "port": 47868, "modality": "image"},
    ]
    c = _CapSettingsClient()
    out = await broadcast.broadcast_hf_token(c, studios, "hf_secret")
    assert len(c.calls) == 2
    # only hf_token is sent → partial update, other keys preserved
    assert all(u.endswith("/api/settings") and j == {"hf_token": "hf_secret"} for u, j in c.calls)
    assert out["chat"]["ok"] and out["image@mac-b"]["ok"]


@pytest.mark.asyncio
async def test_broadcast_hf_token_reports_missing_settings():
    render = [{"id": "render", "host": "10.0.0.2", "port": 47874, "modality": "render"}]
    c = _CapSettingsClient(status=404)
    out = await broadcast.broadcast_hf_token(c, render, "hf_secret")
    assert out["render"]["ok"] is False and out["render"]["status"] == 404


def test_broadcast_hf_token_endpoint_requires_token(authed, monkeypatch):
    from backend import hf_credentials, main

    assert authed.post("/api/hub/broadcast/hf-token", json={}).status_code == 400
    async def valid(_token):
        return {}
    monkeypatch.setattr(main, "_validate_hf_token", valid)
    monkeypatch.setattr(hf_credentials, "save_token", lambda _token: {})
    monkeypatch.setattr(hf_credentials, "get_token", lambda: "hf_x")
    monkeypatch.setattr(hf_credentials, "record_delivery", lambda results: {"results": results})
    # never echoes the token back
    r = authed.post("/api/hub/broadcast/hf-token", json={"token": "hf_x", "studios": []})
    assert r.status_code == 200
    assert "token" not in r.json()
