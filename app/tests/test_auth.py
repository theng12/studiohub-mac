from types import SimpleNamespace

from backend import auth


def _req(host="1.2.3.4", headers=None, query=""):
    from starlette.datastructures import Headers, QueryParams
    return SimpleNamespace(
        client=SimpleNamespace(host=host),
        headers=Headers(headers or {}),
        query_params=QueryParams(query),
    )


def test_presented_token_forms():
    assert auth.presented_token(_req(headers={"authorization": "Bearer abc"})) == "abc"
    assert auth.presented_token(_req(headers={"x-hub-token": "def"})) == "def"
    assert auth.presented_token(_req(query="token=ghi")) == "ghi"
    assert auth.presented_token(_req()) is None


def test_is_loopback():
    assert auth.is_loopback(_req(host="127.0.0.1"))
    assert auth.is_loopback(_req(host="::1"))
    assert not auth.is_loopback(_req(host="192.168.0.5"))


def test_remote_requires_token(client):
    # public paths are open
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/version").status_code == 200
    # protected paths reject non-loopback without a token
    assert client.get("/api/hub/health").status_code == 401


def test_remote_with_token_ok(authed):
    assert authed.get("/api/hub/health").status_code == 200


def test_fleet_token_accepted(app, token):
    from starlette.testclient import TestClient
    from backend import peers
    peers.set_fleet_token("fleet-secret")
    c = TestClient(app, headers={"X-Hub-Token": "fleet-secret"})
    assert c.get("/api/hub/health").status_code == 200
    bad = TestClient(app, headers={"X-Hub-Token": "wrong"})
    assert bad.get("/api/hub/health").status_code == 401
