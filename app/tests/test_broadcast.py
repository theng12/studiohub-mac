import pytest

from backend import broadcast


class _FakeResp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._p = payload or {}
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._p


class _FakeClient:
    def __init__(self):
        self.calls = []

    async def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append((url, json))
        return _FakeResp(200, {"job": {"id": "j1"}})


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


def test_broadcast_hf_token_endpoint_requires_token(authed):
    assert authed.post("/api/hub/broadcast/hf-token", json={}).status_code == 400
    # never echoes the token back
    r = authed.post("/api/hub/broadcast/hf-token", json={"token": "hf_x", "studios": []})
    assert "token" not in r.json()
