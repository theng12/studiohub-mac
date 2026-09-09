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
    assert client.post("/api/hub/maintenance/restart", json={}).status_code == 401


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


def test_owner_password_accepts_a_single_character(reset):
    auth.set_owner_password("1")
    assert auth.verify_owner_password("1")


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


def test_owner_password_setup_refuses_an_unauthenticated_remote_request(client):
    denied = client.post("/api/auth/setup", json={"password": "correct horse battery staple"})
    assert denied.status_code == 401


def _tailscale(app):
    from starlette.testclient import TestClient
    return TestClient(app, client=("100.66.3.3", 50000))


def test_default_password_signs_in_only_while_no_password_is_stored(app):
    """An Agent nobody could type a password on is still its owner's Hub."""
    client = _tailscale(app)
    assert auth.password_mode() == "default"

    wrong = client.post("/api/auth/login", json={"password": "1234567"})
    assert wrong.status_code == 401

    signed_in = client.post("/api/auth/login", json={"password": "123456"})
    assert signed_in.status_code == 200
    assert signed_in.json()["password_mode"] == "default"
    assert auth.SESSION_COOKIE_NAME in signed_in.cookies
    assert client.get("/api/hub/health").status_code == 200

    auth.set_owner_password("an owner chosen password")
    refused = _tailscale(app).post("/api/auth/login", json={"password": "123456"})
    assert refused.status_code == 401
    assert auth.password_mode() == "custom"


def test_default_password_is_refused_from_a_lan_address(client):
    denied = client.post("/api/auth/login", json={"password": "123456"})
    assert denied.status_code == 403
    assert not auth.password_configured()


def test_default_password_respects_the_existing_failure_throttle(app):
    client = _tailscale(app)
    for _ in range(auth._MAX_LOGIN_FAILURES):
        assert client.post("/api/auth/login", json={"password": "nope"}).status_code == 401
    throttled = client.post("/api/auth/login", json={"password": "123456"})
    assert throttled.status_code == 429


def test_auth_status_reports_the_password_mode(client):
    assert client.get("/api/auth/status").json()["password_mode"] == "default"
    auth.set_owner_password("an owner chosen password")
    assert client.get("/api/auth/status").json()["password_mode"] == "custom"
    assert auth.install_password_verifier(auth.owner_password_verifier()) is False
    auth.PASSWORD_FILE.unlink()
    salt, digest = "ab" * 16, "cd" * 64
    assert auth.install_password_verifier(
        {"version": 1, "salt": salt, "digest": digest}
    ) is True
    status = client.get("/api/auth/status").json()
    assert status["password_mode"] == "inherited"
    assert status["password_configured"] is True


def test_owner_session_may_change_the_password_over_tailscale(app):
    client = _tailscale(app)
    assert client.post("/api/auth/login", json={"password": "123456"}).status_code == 200

    saved = client.post("/api/auth/setup", json={"password": "an owner chosen password"})
    assert saved.status_code == 200
    assert saved.json()["password_mode"] == "custom"
    assert auth.verify_owner_password("an owner chosen password")


def test_setup_without_an_owner_session_is_still_refused(app, token):
    from starlette.testclient import TestClient
    # The middleware refuses a bare remote request before the route is reached.
    bare = _tailscale(app).post("/api/auth/setup", json={"password": "x"})
    assert bare.status_code == 401
    # A machine credential authenticates the request but is not an owner session.
    machine = TestClient(app, client=("100.66.3.3", 50000),
                         headers={"X-Hub-Token": token})
    refused = machine.post("/api/auth/setup", json={"password": "x"})
    assert refused.status_code == 403
    assert not auth.password_configured()


def test_password_verifier_validation_rejects_malformed_records(reset):
    auth.set_owner_password("an owner chosen password")
    verifier = auth.owner_password_verifier()
    assert set(verifier) == {"version", "salt", "digest"}
    assert auth.validated_password_verifier(verifier) == verifier

    for broken in (
        None, {}, {"version": 2, "salt": "ab" * 16, "digest": "cd" * 64},
        {"version": 1, "salt": "zz" * 16, "digest": "cd" * 64},
        {"version": 1, "salt": "abc", "digest": "cd" * 64},
        {"version": 1, "salt": "ab" * 16, "digest": "cd" * 600},
        {"version": 1, "salt": "AB" * 16, "digest": "cd" * 64},
    ):
        assert auth.validated_password_verifier(broken) is None


def test_an_inherited_password_is_replaceable_but_a_custom_one_is_not(reset):
    inherited = {"version": 1, "salt": "ab" * 16, "digest": "cd" * 64}
    replacement = {"version": 1, "salt": "12" * 16, "digest": "34" * 64}

    assert auth.install_password_verifier(inherited) is True
    assert auth.password_mode() == "inherited"
    assert auth.install_password_verifier(replacement) is True

    auth.set_owner_password("an owner chosen password")
    assert auth.password_mode() == "custom"
    assert auth.install_password_verifier(replacement) is False
    assert auth.verify_owner_password("an owner chosen password")


def test_dashboard_warns_about_the_default_password_and_names_the_fleet_token():
    from pathlib import Path

    dashboard = (Path(__file__).parents[1] / "frontend" / "index.html").read_text()

    # The banner lives above the tab strip, so it is on every page.
    header_end = dashboard.index("</header>")
    assert 0 < dashboard.index('id="default-password-banner"') < dashboard.index("<nav>")
    assert header_end < dashboard.index('id="default-password-banner"')
    assert "This Hub is using the default password. Change it now." in dashboard
    assert "dismissDefaultPasswordBanner" in dashboard
    assert 'authInfo?.password_mode === "default"' in dashboard

    # An Agent's owner is told the two things that actually work on an Agent.
    assert ("Enrolled Agent? Paste the fleet token shown on your Controller's "
            "Remote → Machine mode page.") in dashboard
    assert "sign in with the default password 123456 if this Hub was never given one" in dashboard
    assert 'authInfo?.role === "agent" ? "Fleet token" : "Hub token"' in dashboard


def test_dashboard_explains_a_refused_repair_with_the_earlier_outcome():
    from pathlib import Path

    dashboard = (Path(__file__).parents[1] / "frontend" / "index.html").read_text()

    assert "request_conflict: [\"needs_review\"" in dashboard
    assert "function priorRepairNote(request)" in dashboard
    assert "evidence.prior_repair_state" in dashboard
    assert "An earlier repair${when ? ` on ${when}` : \"\"} ended ${state}" in dashboard
