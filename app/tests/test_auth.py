from types import SimpleNamespace

from backend import auth
import stat


def _req(host="1.2.3.4", headers=None, query=""):
    from starlette.datastructures import Headers, QueryParams
    return SimpleNamespace(
        client=SimpleNamespace(host=host),
        headers=Headers(headers or {}),
        query_params=QueryParams(query),
        cookies={},
    )


def test_presented_token_forms():
    assert auth.presented_token(_req(headers={"authorization": "Bearer abc"})) == "abc"
    assert auth.presented_token(_req(headers={"x-hub-token": "def"})) == "def"
    cookie = _req()
    cookie.cookies[auth.COOKIE_NAME] = "ghi"
    assert auth.presented_token(cookie) == "ghi"
    assert auth.presented_token(_req(query="token=leaks-in-url")) is None
    assert auth.presented_token(_req()) is None


def test_is_loopback():
    assert auth.is_loopback(_req(host="127.0.0.1"))
    assert auth.is_loopback(_req(host="::1"))
    assert not auth.is_loopback(_req(host="192.168.0.5"))


def test_is_tailscale():
    assert auth.is_tailscale(_req(host="100.66.3.3"))
    assert not auth.is_tailscale(_req(host="192.168.0.5"))


def test_remote_requires_token(client):
    # public paths are open
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/version").status_code == 200
    # protected paths reject non-loopback without a token
    assert client.get("/api/hub/health").status_code == 401
    assert client.post("/api/auto-update/check").status_code == 401
    assert client.post("/api/hub/auto-updates/check-all").status_code == 401


def test_remote_with_token_ok(authed):
    response = authed.get("/api/hub/health")
    assert response.status_code == 200
    assert auth.COOKIE_NAME in response.cookies


def test_fleet_token_accepted(app, token):
    from starlette.testclient import TestClient
    from backend import peers
    peers.set_fleet_token("fleet-secret")
    c = TestClient(app, headers={"X-Hub-Token": "fleet-secret"})
    assert c.get("/api/hub/health").status_code == 200
    bad = TestClient(app, headers={"X-Hub-Token": "wrong"})
    assert bad.get("/api/hub/health").status_code == 401


def test_cross_origin_browser_write_is_rejected(authed):
    r = authed.post("/api/hub/registry/reload", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert authed.post("/api/hub/registry/reload").status_code == 200
    same = authed.post("/api/hub/registry/reload", headers={"Origin": "http://testserver"})
    assert same.status_code == 200


def test_hub_token_permissions_are_private(reset):
    auth.TOKEN_FILE.unlink(missing_ok=True)
    assert auth.load_token()
    assert stat.S_IMODE(auth.TOKEN_FILE.stat().st_mode) == 0o600


def test_owner_password_is_hashed_and_revokes_existing_sessions(reset):
    auth.set_owner_password("correct horse battery staple")
    assert auth.password_configured()
    assert auth.verify_owner_password("correct horse battery staple")
    assert not auth.verify_owner_password("not the owner password")
    assert "correct horse battery staple" not in auth.PASSWORD_FILE.read_text()
    assert stat.S_IMODE(auth.PASSWORD_FILE.stat().st_mode) == 0o600
    session = auth.create_browser_session()
    assert auth.valid_browser_session(session)
    auth.set_owner_password("an entirely new owner password")
    assert not auth.valid_browser_session(session)


def test_tailscale_password_login_creates_remembered_session(app):
    from starlette.testclient import TestClient
    client = TestClient(app, client=("100.66.3.3", 50000))
    auth.set_owner_password("correct horse battery staple")
    bad = client.post("/api/auth/login", json={"password": "wrong password"})
    assert bad.status_code == 401
    signed_in = client.post("/api/auth/login", json={"password": "correct horse battery staple"})
    assert signed_in.status_code == 200
    assert auth.SESSION_COOKIE_NAME in signed_in.cookies
    assert client.get("/api/hub/health").status_code == 200
    logged_out = client.post("/api/auth/logout")
    assert logged_out.status_code == 200
    assert client.get("/api/hub/health").status_code == 401


def test_lan_password_login_is_rejected(client):
    auth.set_owner_password("correct horse battery staple")
    denied = client.post("/api/auth/login", json={"password": "correct horse battery staple"})
    assert denied.status_code == 403


def test_owner_password_setup_is_loopback_only(client):
    denied = client.post("/api/auth/setup", json={"password": "correct horse battery staple"})
    assert denied.status_code == 401
