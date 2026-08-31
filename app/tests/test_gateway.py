import asyncio

import httpx
import pytest
from starlette.requests import ClientDisconnect, Request
from starlette.testclient import TestClient

from backend import gateway


SAFE_FLEET_JOB_HEADERS = {
    "cache-control": "no-store, private, max-age=0",
    "pragma": "no-cache",
    "x-content-type-options": "nosniff",
}


def assert_safe_fleet_job_headers(response):
    for name, value in SAFE_FLEET_JOB_HEADERS.items():
        assert response.headers[name] == value


class FakeResp:
    def __init__(self, status=200, headers=None, chunks=(b"body",), stream_error=None):
        self.status_code = status
        self.headers = httpx.Headers(headers or {})
        self._chunks = chunks
        self._stream_error = stream_error
        self.closed = False

    async def aiter_raw(self):
        for c in self._chunks:
            yield c
        if self._stream_error:
            raise self._stream_error

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
    assert not SAFE_FLEET_JOB_HEADERS.keys() & r.headers.keys()


def test_gateway_strips_hub_token_from_upstream(app, token, monitor, monkeypatch):
    monitor.registry.append({
        "id": "chat", "title": "Chat Studio KH", "modality": "chat",
        "machine": "local", "host": "127.0.0.1", "port": 47871,
    })
    fc = FakeClient(resp=FakeResp())
    monkeypatch.setattr(gateway, "_client", fc)
    _authed(app, token).get("/studio/chat/v1/models")
    hdrs = {k.lower() for k in fc.captured["headers"]}
    assert "x-hub-token" not in hdrs and "authorization" not in hdrs
    assert "x-studio-token" in hdrs


def test_gateway_contains_studio_cookies_and_browser_credentials(
    app, token, monitor, monkeypatch,
):
    monitor.registry[0]["studio_token"] = "resolved-studio-secret"
    fc = FakeClient(resp=FakeResp(headers={
        "set-cookie": "kh_studio_token=fleet-secret; HttpOnly",
        "set-cookie2": "kh_studio_token=fleet-secret; Version=1",
        "cache-control": "no-store, private, max-age=0",
        "pragma": "no-cache",
        "x-content-type-options": "nosniff",
        "x-worker-evidence": "preserved",
        "content-type": "audio/wav",
        "content-range": "bytes 0-3/4",
    }))
    monkeypatch.setattr(gateway, "_client", fc)

    response = TestClient(app, client=("127.0.0.1", 50000), headers={
        "Authorization": "Bearer browser-secret",
        "X-Hub-Token": token,
        "X-Studio-Token": "browser-studio-secret",
        "Cookie": "kh_hub_token=browser-cookie",
    }).get("/studio/image/api/fleet/jobs/job-1/details")

    assert "set-cookie" not in response.headers
    assert "set-cookie2" not in response.headers
    assert response.headers["cache-control"] == "no-store, private, max-age=0"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-worker-evidence"] == "preserved"
    assert response.headers["content-type"] == "audio/wav"
    assert response.headers["content-range"] == "bytes 0-3/4"
    upstream_headers = {k.lower(): v for k, v in fc.captured["headers"].items()}
    assert "authorization" not in upstream_headers
    assert "x-hub-token" not in upstream_headers
    assert "cookie" not in upstream_headers
    assert upstream_headers["x-studio-token"] == "resolved-studio-secret"


def test_gateway_unknown_studio_404(authed):
    assert authed.get("/studio/nope/api/x").status_code == 404


def test_gateway_unreachable_502(app, token, monkeypatch):
    monkeypatch.setattr(gateway, "_client", FakeClient(exc=httpx.ConnectError("boom")))
    assert _authed(app, token).get("/studio/image/api/x").status_code == 502


def test_fleet_job_gateway_generated_errors_are_private(app, token, monkeypatch):
    remote = TestClient(app, client=("203.0.113.5", 50000))
    unauthorized = remote.get("/studio/image/api/fleet/jobs/job-1/details")
    unknown = _authed(app, token).get(
        "/studio/missing/api/fleet/jobs/job-1/details",
    )
    monkeypatch.setattr(
        gateway, "_client", FakeClient(exc=httpx.ConnectError("boom")),
    )
    unreachable = _authed(app, token).get(
        "/studio/image/api/fleet/jobs/job-1/details",
    )

    assert [unauthorized.status_code, unknown.status_code, unreachable.status_code] == [
        401, 404, 502,
    ]
    for response in (unauthorized, unknown, unreachable):
        assert_safe_fleet_job_headers(response)


def test_fleet_job_gateway_adds_safe_headers_to_legacy_upstream_errors(
    app, token, monkeypatch,
):
    fc = FakeClient(resp=FakeResp(status=404, headers={"x-worker-error": "legacy"}))
    monkeypatch.setattr(gateway, "_client", fc)

    response = _authed(app, token).get(
        "/studio/image/api/fleet/jobs/missing/details",
    )

    assert response.status_code == 404
    assert response.headers["x-worker-error"] == "legacy"
    assert_safe_fleet_job_headers(response)


def test_gateway_closes_upstream_response(app, token, monkeypatch):
    # regression for the connection leak: upstream response MUST be closed.
    fc = FakeClient(resp=FakeResp())
    monkeypatch.setattr(gateway, "_client", fc)
    _authed(app, token).get("/studio/image/api/health")
    assert fc.resp.closed is True


def test_gateway_closes_upstream_response_after_media_stream_error(
    app, token, monkeypatch,
):
    fc = FakeClient(resp=FakeResp(stream_error=RuntimeError("media stream failed")))
    monkeypatch.setattr(gateway, "_client", fc)

    with pytest.raises(RuntimeError, match="media stream failed"):
        _authed(app, token).get("/studio/image/api/fleet/jobs/job-1/media/opaque")

    assert fc.resp.closed is True


def test_gateway_response_closes_upstream_before_asgi_send_disconnect_returns(
    monitor, monkeypatch,
):
    fc = FakeClient(resp=FakeResp(chunks=(b"media",)))
    monkeypatch.setattr(gateway, "_client", fc)
    path = "/studio/image/api/fleet/jobs/job-1/media/opaque"
    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": b"",
        "headers": [(b"host", b"testserver")], "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80), "root_path": "", "extensions": {},
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            raise OSError("client disconnected")

    async def request():
        response = await gateway.proxy(
            "image", "api/fleet/jobs/job-1/media/opaque", Request(scope, receive),
        )
        with pytest.raises(ClientDisconnect):
            await response(scope, receive, send)
        assert fc.resp.closed is True

    asyncio.run(request())
