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


def test_fleet_token_roundtrip(reset):
    generated = peers.fleet_token()
    assert generated
    peers.set_fleet_token("secret")
    assert peers.fleet_token() == "secret"
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
async def test_refresh_success_caches_host(reset):
    resp = FakeResp(data={"host": {"total_gb": 64}, "studios": {"image": {"rss_gb": 3}}})
    await peers.refresh(REMOTE, FakeGet(resp=resp))
    c = peers.cached("mac-b")
    assert c["reachable"] and c["host"]["total_gb"] == 64
    assert c["studios"]["image"]["rss_gb"] == 3


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


def test_remote_studio_requests_use_connected_peer_hub(reset):
    studio = REMOTE[0]
    peers.set_fleet_token("shared-secret")
    direct_url, direct_headers = peers.studio_request(studio, "/api/catalog")
    assert direct_url == "http://100.1.1.1:47868/api/catalog"
    assert direct_headers == {"X-Studio-Token": "shared-secret"}

    peers._cache["mac-b"] = (1.0, {"status": "connected", "reachable": True})
    peer_url, peer_headers = peers.studio_request(studio, "/api/catalog")
    assert peer_url == "http://100.1.1.1:47873/studio/image/api/catalog"
    assert peer_headers == {"X-Hub-Token": "shared-secret"}
