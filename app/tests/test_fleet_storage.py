from pathlib import Path

import pytest

from backend import fleet_storage


GIB = 1024 ** 3


class Response:
    def __init__(self, value, status=200):
        self.value = value
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise ValueError(f"HTTP {self.status_code}")

    def json(self):
        return self.value


class StorageClient:
    def __init__(self, stores):
        self.stores = stores
        self.calls = []

    @staticmethod
    def _app(url):
        return {47868: "image", 47870: "voice", 47871: "chat"}.get(
            int(url.split(":")[2].split("/")[0]), "unknown")

    async def get(self, url, **kwargs):
        app = self._app(url)
        self.calls.append(("GET", app, None))
        return Response(dict(self.stores[app]))

    async def put(self, url, json=None, **kwargs):
        app = self._app(url)
        self.calls.append(("PUT", app, json))
        self.stores[app].update(json or {})
        return Response(dict(self.stores[app]))

    async def post(self, url, json=None, **kwargs):
        app = self._app(url)
        self.calls.append(("POST", app, json))
        target = (json or {}).get("target_bytes")
        if target is not None:
            self.stores[app]["used_bytes"] = min(
                self.stores[app]["used_bytes"], target)
        return Response(dict(self.stores[app]))


class Monitor:
    def __init__(self, client):
        self._client = client
        self.registry = [
            {"id": "image", "title": "Image Studio", "modality": "image",
             "host": "127.0.0.1", "port": 47868, "machine": "local"},
            {"id": "voice", "title": "Voice Studio", "modality": "voice",
             "host": "127.0.0.1", "port": 47870, "machine": "local"},
            {"id": "chat", "title": "Chat Studio", "modality": "chat",
             "host": "127.0.0.1", "port": 47871, "machine": "local"},
        ]


class PeerClient:
    def __init__(self):
        self.calls = []

    async def post(self, url, json=None, **kwargs):
        self.calls.append((url, json, kwargs))
        return Response({"machines": [{
            "machine": "local", "used_bytes": 2 * GIB,
            "max_bytes": 80 * GIB, "over_limit": False,
            "reclaimed_bytes": GIB, "stores": [], "errors": 0,
        }]})


@pytest.mark.asyncio
async def test_combined_machine_cap_shrinks_largest_disposable_store(reset, monkeypatch):
    stores = {
        "image": {"used_bytes": 60 * GIB, "supported": True,
                  "scope": "generated images"},
        "voice": {"used_bytes": 40 * GIB, "supported": True,
                  "scope": "generated audio"},
        "chat": {"used_bytes": 0, "supported": False,
                 "scope": "chat history protected"},
    }
    client = StorageClient(stores)
    monitor = Monitor(client)
    monkeypatch.setattr(fleet_storage.job_storage, "status", lambda: {
        "used_bytes": 0, "supported": True, "scope": "Hub transcription"})
    monkeypatch.setattr(fleet_storage.job_storage, "save", lambda *args: {
        "used_bytes": 0, "supported": True, "scope": "Hub transcription"})
    monkeypatch.setattr(fleet_storage.job_storage, "enforce_budget", lambda *args: {
        "used_bytes": 0, "reclaimed_bytes": 0})

    result = await fleet_storage.local_status(
        monitor, apply_policy=True, cleanup=True)

    machine = result["machines"][0]
    assert machine["used_bytes"] == 80 * GIB
    assert machine["over_limit"] is False
    assert ("POST", "image", {"target_bytes": 40 * GIB}) in client.calls
    assert next(row for row in machine["stores"] if row["app"] == "chat")["supported"] is False


@pytest.mark.asyncio
async def test_protected_store_that_cannot_shrink_remains_visible_as_over_limit(reset, monkeypatch):
    stores = {
        "image": {"used_bytes": 90 * GIB, "supported": True},
        "voice": {"used_bytes": 0, "supported": True},
        "chat": {"used_bytes": 0, "supported": False},
    }
    client = StorageClient(stores)

    async def no_shrink(url, json=None, **kwargs):
        app = client._app(url)
        client.calls.append(("POST", app, json))
        return Response(dict(client.stores[app]))

    client.post = no_shrink
    monkeypatch.setattr(fleet_storage.job_storage, "status", lambda: {"used_bytes": 0})
    monkeypatch.setattr(fleet_storage.job_storage, "save", lambda *args: {"used_bytes": 0})
    monkeypatch.setattr(fleet_storage.job_storage, "enforce_budget", lambda *args: {
        "used_bytes": 0, "reclaimed_bytes": 0})

    result = await fleet_storage.local_status(
        Monitor(client), apply_policy=True, cleanup=True)
    assert result["machines"][0]["over_limit"] is True
    assert result["machines"][0]["used_bytes"] == 90 * GIB


def test_policy_defaults_and_round_trip(reset):
    assert fleet_storage.read_policy() == {
        "enabled": True, "retention_days": 3, "max_gb": 80.0}
    saved = fleet_storage.save_policy(True, 1, 64)
    assert saved == {"enabled": True, "retention_days": 1, "max_gb": 64.0}
    assert fleet_storage.read_policy() == saved


@pytest.mark.asyncio
async def test_peer_cleanup_uses_cleanup_endpoint_and_local_only(reset):
    client = PeerClient()
    studio = {"host": "100.64.0.8", "machine": "mac-b", "hub_token": "secret"}
    report = await fleet_storage._peer_call(client, "mac-b", studio, "post", {})
    url, body, kwargs = client.calls[0]
    assert url == "http://100.64.0.8:47873/api/hub/storage-policy/cleanup?local_only=true"
    assert body == {}
    assert kwargs["headers"] == {"X-Hub-Token": "secret"}
    assert report["machine"] == "mac-b" and report["used_bytes"] == 2 * GIB


def test_dashboard_has_modern_fleet_storage_controls():
    dashboard = Path(__file__).parents[1] / "frontend" / "index.html"
    text = dashboard.read_text()
    assert 'id="fleet-storage-save"' in text
    assert '>Save to fleet</button>' in text
    assert '>Check &amp; clean now</button>' in text
    assert "loadFleetStorage()" in text
    assert "optional Studio store" in text
    assert "this Mac will self-enforce when it reconnects" in text
    assert 'class="storage-machine${offline ? " offline" : ""}' in text
