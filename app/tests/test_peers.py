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
    assert peers.fleet_token() is None
    peers.set_fleet_token("secret")
    assert peers.fleet_token() == "secret"
    peers.set_fleet_token("")
    assert peers.fleet_token() is None


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


@pytest.mark.asyncio
async def test_control_remote_needs_token(reset):
    async with httpx.AsyncClient() as c:
        r = await peers.control_remote(c, REMOTE[0], "start")
    assert r["ok"] is False and "fleet token" in r["error"]
