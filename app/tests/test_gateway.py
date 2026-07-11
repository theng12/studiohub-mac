import httpx
from starlette.testclient import TestClient

from backend import gateway


class FakeResp:
    def __init__(self, status=200, headers=None, chunks=(b"body",)):
        self.status_code = status
        self.headers = httpx.Headers(headers or {})
        self._chunks = chunks
        self.closed = False

    async def aiter_raw(self):
        for c in self._chunks:
            yield c

    async def aclose(self):
        self.closed = True


class FakeClient:
    """Stands in for gateway._client: records the built request, returns a
    FakeResp (or raises), and tracks close()."""
    def __init__(self, resp=None, exc=None):
        self.resp = resp
        self.exc = exc
        self.captured = {}

    def build_request(self, method, url, headers=None, params=None, content=None):
        self.captured = {"method": method, "url": url, "headers": headers or {}}
        return object()

    async def send(self, req, stream=False):
        if self.exc:
            raise self.exc
        return self.resp


def _authed(app, token):
    return TestClient(app, headers={"X-Hub-Token": token})


def test_gateway_routes_to_correct_studio(app, token, monkeypatch):
    fc = FakeClient(resp=FakeResp())
    monkeypatch.setattr(gateway, "_client", fc)
    r = _authed(app, token).get("/studio/image/api/catalog")
    assert r.status_code == 200 and r.content == b"body"
    assert fc.captured["url"] == "http://127.0.0.1:47868/api/catalog"


def test_gateway_strips_hub_token_from_upstream(app, token, monkeypatch):
    fc = FakeClient(resp=FakeResp())
    monkeypatch.setattr(gateway, "_client", fc)
    _authed(app, token).get("/studio/chat/v1/models")
    hdrs = {k.lower() for k in fc.captured["headers"]}
    assert "x-hub-token" not in hdrs and "authorization" not in hdrs
    assert "x-studio-token" in hdrs


def test_gateway_unknown_studio_404(authed):
    assert authed.get("/studio/nope/api/x").status_code == 404


def test_gateway_unreachable_502(app, token, monkeypatch):
    monkeypatch.setattr(gateway, "_client", FakeClient(exc=httpx.ConnectError("boom")))
    assert _authed(app, token).get("/studio/image/api/x").status_code == 502


def test_gateway_closes_upstream_response(app, token, monkeypatch):
    # regression for the connection leak: upstream response MUST be closed.
    fc = FakeClient(resp=FakeResp())
    monkeypatch.setattr(gateway, "_client", fc)
    _authed(app, token).get("/studio/image/api/health")
    assert fc.resp.closed is True
