import httpx
import pytest

from backend import peers


REMOTE = [{"id": "image@mac-b", "modality": "image", "host": "100.1.1.1",
           "port": 47868, "machine": "mac-b"}]


class FakeGet:
    def __init__(self, exc=None, resp=None):
        self.exc, self.resp = exc, resp

    async def get(self, url, headers=None, timeout=None):
        if self.exc:
            raise self.exc
        return self.resp


class FakeResp:
    def __init__(self, status=200, data=None):
        self.status_code = status
        self._data = data or {}
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._data


class FakeSyncClient:
    def __init__(self):
        self.calls = []

    async def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append(("POST", url, headers, json))
        return FakeResp(200)

    async def get(self, url, headers=None, timeout=None):
        self.calls.append(("GET", url, headers, None))
        return FakeResp(200, {"host": {}})


class FakeStartupClient:
    def __init__(self, status=200, data=None):
        self.status = status
        self.data = data or {}
        self.calls = []

    async def get(self, url, headers=None, timeout=None):
        self.calls.append(("GET", url, headers, timeout))
        return FakeResp(self.status, self.data)

    async def post(self, url, headers=None, timeout=None):
        self.calls.append(("POST", url, headers, timeout))
        return FakeResp(self.status, self.data)


def test_fleet_token_roundtrip(reset):
    generated = peers.fleet_token()
    assert generated
    peers.set_fleet_token("secret")
    assert peers.fleet_token() == "secret"
    assert peers.SHARED_STUDIO_TOKEN_FILE.read_text().strip() == "secret"
    assert peers.SHARED_STUDIO_TOKEN_FILE.stat().st_mode & 0o777 == 0o600
    peers.set_fleet_token("")
    assert peers.fleet_token() not in {None, "", "secret"}


def test_remote_machines_grouping():
    reg = REMOTE + [{"id": "image", "machine": "local", "host": "127.0.0.1", "port": 47868}]
    grouped = peers._remote_machines(reg)
    assert set(grouped) == {"mac-b"}  # local excluded


@pytest.mark.asyncio
async def test_refresh_offline_peer_is_graceful(reset):
    client = FakeGet(exc=httpx.ConnectError("down"))
    await peers.refresh(REMOTE, client)  # must not raise
    c = peers.cached("mac-b")
    assert c is not None and c["reachable"] is False


@pytest.mark.asyncio
async def test_peer_unreachable_and_recovery_alerts_are_debounced(reset):
    from backend import alerts

    client = FakeGet(exc=httpx.ConnectError("down"))
    for _ in range(peers.PEER_FAILURES_TO_ALERT):
        peers._cache.clear()
        await peers.refresh(REMOTE, client)
    events = [
        event for event in alerts.recent(20)
        if event["kind"] == "agent_unreachable"
    ]
    assert len(events) == 1

    peers._cache.clear()
    await peers.refresh(
        REMOTE,
        FakeGet(resp=FakeResp(data={"host": {}, "studios": {}})),
    )
    assert alerts.recent(1)[0]["kind"] == "agent_recovered"


@pytest.mark.asyncio
async def test_refresh_success_caches_host(reset):
    resp = FakeResp(data={
        "host": {"total_gb": 64},
        "studios": {
            "image": {"rss_gb": 3},
            "voice": {"cloud_providers": {
                "supported": True,
                "providers": [{"key": "genaipro", "live": True}],
            }},
        },
    })
    await peers.refresh(REMOTE, FakeGet(resp=resp))
    c = peers.cached("mac-b")
    assert c["reachable"] and c["host"]["total_gb"] == 64
    assert c["studios"]["image"]["rss_gb"] == 3
    assert c["studios"]["voice"]["cloud_providers"]["providers"][0]["key"] == "genaipro"


@pytest.mark.asyncio
async def test_refresh_inflight_guard(reset):
    peers._inflight["v"] = True
    try:
        await peers.refresh(REMOTE, FakeGet(exc=httpx.ConnectError("x")))
        assert peers.cached("mac-b") is None  # guard skipped the whole sweep
    finally:
        peers._inflight["v"] = False


def test_studio_headers_use_per_studio_override(reset):
    assert peers.studio_headers({"studio_token": "one"}) == {"X-Studio-Token": "one"}


def test_remote_studio_requests_always_use_peer_hub(reset):
    studio = REMOTE[0]
    peers.set_fleet_token("shared-secret")
    peer_url, peer_headers = peers.studio_request(studio, "/api/catalog")
    assert peer_url == "http://100.1.1.1:47873/studio/image/api/catalog"
    assert peer_headers == {"X-Hub-Token": "shared-secret"}


@pytest.mark.asyncio
async def test_fleet_token_sync_uses_old_token_then_verifies_new(reset):
    peers.set_fleet_token("old-shared-secret")
    client = FakeSyncClient()
    result = await peers.sync_fleet_token(REMOTE, client, "new-shared-secret")
    assert result["verified"] == 1 and result["manual"] == 0 and result["pending"] == 0
    assert client.calls[0][2] == {"X-Hub-Token": "old-shared-secret"}
    assert client.calls[0][3] == {"token": "new-shared-secret", "sync": False}
    assert client.calls[1][2] == {"X-Hub-Token": "new-shared-secret"}
    assert peers.fleet_token() == "new-shared-secret"


@pytest.mark.asyncio
async def test_remote_startup_audit_uses_authenticated_peer_hub(reset):
    peers.set_fleet_token("shared-secret")
    client = FakeStartupClient(data={
        "schema_version": 1, "services": [{"modality": "image", "installed": True}],
    })
    result = await peers.startup_services_status(REMOTE, client)

    assert result["mac-b"]["reachable"] is True
    assert result["mac-b"]["services"][0]["installed"] is True
    method, url, headers, timeout = client.calls[0]
    assert method == "GET" and url.endswith("/api/hub/startup-services?local_only=true")
    assert headers == {"X-Hub-Token": "shared-secret"}
    assert timeout == peers.PEER_TIMEOUT_S


@pytest.mark.asyncio
async def test_old_peer_reports_update_needed_for_startup_audit(reset):
    peers.set_fleet_token("shared-secret")
    result = await peers.startup_services_status(REMOTE, FakeStartupClient(status=404))
    assert result["mac-b"]["supported"] is False
    assert "Update" in result["mac-b"]["detail"]


@pytest.mark.asyncio
async def test_remote_startup_install_targets_peer_local_machine(reset):
    peers.set_fleet_token("shared-secret")
    client = FakeStartupClient(data={"ok": True, "changed": True})
    result = await peers.install_remote_startup_service(client, REMOTE[0], "voice")
    assert result["ok"] is True
    method, url, headers, timeout = client.calls[0]
    assert method == "POST"
    assert url.endswith("/api/hub/startup-services/local/voice/install")
    assert headers == {"X-Hub-Token": "shared-secret"}
    assert timeout == 260.0
