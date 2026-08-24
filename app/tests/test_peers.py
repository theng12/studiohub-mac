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


def test_current_fleet_token_missing_returns_none_without_creating_files(reset):
    for path in (peers.FLEET_TOKEN_FILE, peers.SHARED_STUDIO_TOKEN_FILE):
        path.unlink(missing_ok=True)

    assert peers.current_fleet_token() is None
    assert peers.current_fleet_token() is None
    assert not peers.FLEET_TOKEN_FILE.exists()
    assert not peers.SHARED_STUDIO_TOKEN_FILE.exists()


def test_empty_current_fleet_token_is_read_only(reset):
    peers.FLEET_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    peers.FLEET_TOKEN_FILE.write_bytes(b" \n\t")
    peers.FLEET_TOKEN_FILE.chmod(0o640)
    before_bytes = peers.FLEET_TOKEN_FILE.read_bytes()
    before_mode = peers.FLEET_TOKEN_FILE.stat().st_mode

    assert peers.current_fleet_token() is None
    assert peers.current_fleet_token() is None
    assert peers.FLEET_TOKEN_FILE.read_bytes() == before_bytes
    assert peers.FLEET_TOKEN_FILE.stat().st_mode == before_mode
    assert not peers.SHARED_STUDIO_TOKEN_FILE.exists()


def test_current_fleet_token_reads_existing_or_environment_value_without_side_effects(
    reset, monkeypatch,
):
    peers.FLEET_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    peers.FLEET_TOKEN_FILE.write_bytes(b"  file-secret  \n")
    peers.FLEET_TOKEN_FILE.chmod(0o640)
    before_bytes = peers.FLEET_TOKEN_FILE.read_bytes()
    before_mode = peers.FLEET_TOKEN_FILE.stat().st_mode

    monkeypatch.delenv("STUDIOHUB_FLEET_TOKEN", raising=False)
    assert peers.current_fleet_token() == "file-secret"
    assert peers.FLEET_TOKEN_FILE.read_bytes() == before_bytes
    assert peers.FLEET_TOKEN_FILE.stat().st_mode == before_mode

    monkeypatch.setenv("STUDIOHUB_FLEET_TOKEN", "  env-secret  ")
    assert peers.current_fleet_token() == "env-secret"
    assert peers.FLEET_TOKEN_FILE.read_bytes() == before_bytes
    assert peers.FLEET_TOKEN_FILE.stat().st_mode == before_mode


def test_legacy_fleet_token_generation_is_unchanged_for_nonrepair_callers(reset):
    assert peers.fleet_token()
    assert peers.FLEET_TOKEN_FILE.exists()
    assert peers.SHARED_STUDIO_TOKEN_FILE.exists()


@pytest.mark.asyncio
async def test_sync_fleet_token_invokes_local_commit_once_after_network_work(reset):
    peers.set_fleet_token("old-shared-secret")
    events = []

    class OrderedSyncClient(FakeSyncClient):
        async def post(self, url, headers=None, json=None, timeout=None):
            events.append(("network", "POST", url))
            return await super().post(url, headers=headers, json=json, timeout=timeout)

        async def get(self, url, headers=None, timeout=None):
            events.append(("network", "GET", url))
            return await super().get(url, headers=headers, timeout=timeout)

    client = OrderedSyncClient()

    def local_commit(token):
        events.append(("commit", token))

    result = await peers.sync_fleet_token(
        REMOTE, client, "new-shared-secret", local_commit=local_commit,
    )

    assert result["verified"] == 1
    assert [event[0] for event in events] == ["network", "network", "commit"]
    assert events[-1] == ("commit", "new-shared-secret")
    assert sum(event[0] == "commit" for event in events) == 1


def test_fleet_sync_network_io_precedes_short_local_token_commit_lock(
    authed, monkeypatch,
):
    from contextlib import contextmanager
    from backend import main

    events = []

    class Coordinator:
        @contextmanager
        def controller_mutation(self, *, identity=False, machine=None):
            events.append("lock_enter")
            try:
                yield
            finally:
                events.append("lock_exit")

    main.app.state.enrollment_repair_coordinator = Coordinator()

    async def sync(_registry, _client, token, *, local_commit):
        events.extend(["remote_update", "remote_verify"])
        local_commit(token)
        return {"total": 1, "verified": 1, "manual": 0, "pending": 0, "machines": {}}

    monkeypatch.setattr(peers, "sync_fleet_token", sync)
    response = authed.post("/api/hub/fleet", json={
        "token": "new-shared-fleet-token", "sync": True,
    })

    assert response.status_code == 200
    assert events == ["remote_update", "remote_verify", "lock_enter", "lock_exit"]
    assert peers.current_fleet_token() == "new-shared-fleet-token"


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
async def test_peer_recovery_schedules_only_the_recovered_managed_target(reset, monkeypatch):
    class Reconciler:
        def __init__(self):
            self.machines = []

        def wake_peer(self, machine):
            self.machines.append(machine)

    reconciler = Reconciler()
    monkeypatch.setattr(peers, "release_reconciler", reconciler, raising=False)
    peers._peer_alert_state["mac-b"] = {"failures": 3, "alerted": True}

    await peers.refresh(
        REMOTE,
        FakeGet(resp=FakeResp(data={"host": {}, "studios": {}})),
    )

    assert reconciler.machines == ["mac-b"]


@pytest.mark.asyncio
async def test_peer_recovery_before_alert_threshold_still_schedules_target(reset, monkeypatch):
    class Reconciler:
        def __init__(self):
            self.machines = []

        def wake_peer(self, machine):
            self.machines.append(machine)

    reconciler = Reconciler()
    monkeypatch.setattr(peers, "release_reconciler", reconciler, raising=False)
    await peers.refresh(REMOTE, FakeGet(exc=httpx.ConnectError("down")))
    assert peers._peer_alert_state["mac-b"]["alerted"] is False

    peers._cache.clear()
    await peers.refresh(
        REMOTE,
        FakeGet(resp=FakeResp(data={"host": {}, "studios": {}})),
    )

    assert reconciler.machines == ["mac-b"]


@pytest.mark.asyncio
async def test_peer_recovery_scheduler_failure_does_not_hide_reachable_peer(reset, monkeypatch):
    class Reconciler:
        def wake_peer(self, _machine):
            raise OSError("state unavailable")

    monkeypatch.setattr(peers, "release_reconciler", Reconciler(), raising=False)
    peers._peer_alert_state["mac-b"] = {"failures": 3, "alerted": True}

    await peers.refresh(
        REMOTE,
        FakeGet(resp=FakeResp(data={"host": {}, "studios": {}})),
    )

    assert peers.cached("mac-b")["reachable"] is True


@pytest.mark.asyncio
async def test_refresh_success_caches_host(reset):
    resp = FakeResp(data={
        "host": {"total_gb": 64},
        "studios": {
            "image": {"rss_gb": 3},
            # a peer's per-studio stats are passed through verbatim, including
            # nested structures this Hub version does not itself produce
            "voice": {"rss_gb": 2, "proxy": {"https": True, "port": 47869}},
        },
    })
    await peers.refresh(REMOTE, FakeGet(resp=resp))
    c = peers.cached("mac-b")
    assert c["reachable"] and c["host"]["total_gb"] == 64
    assert c["studios"]["image"]["rss_gb"] == 3
    assert c["studios"]["voice"]["proxy"]["port"] == 47869


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


@pytest.mark.asyncio
async def test_remote_startup_retire_targets_peer_local_machine(reset):
    peers.set_fleet_token("shared-secret")
    client = FakeStartupClient(data={"ok": True, "changed": True})

    result = await peers.retire_remote_startup_service(
        client, REMOTE[0], "music",
    )

    assert result["ok"] is True
    method, url, headers, timeout = client.calls[0]
    assert method == "POST"
    assert url.endswith("/api/hub/service/startup-services/local/music/retire")
    assert headers == {"X-Hub-Token": "shared-secret"}
    assert timeout == 260.0


@pytest.mark.asyncio
async def test_remote_update_repair_requires_exact_capability_and_targets_peer_local_studio(reset):
    peers.set_fleet_token("shared-secret")

    class Client:
        def __init__(self): self.calls = []
        async def get(self, url, headers=None, timeout=None):
            self.calls.append(("GET", url, headers, None))
            return FakeResp(200, {"studio_update_repair_schema": 1})
        async def post(self, url, headers=None, json=None, timeout=None):
            self.calls.append(("POST", url, headers, json))
            return FakeResp(200, {"id": "repair-1", "status": "queued", "items": []})

    client = Client()
    result = await peers.start_remote_studio_update_repair(client, REMOTE[0])

    assert result["ok"] is True and result["job"]["id"] == "repair-1"
    assert client.calls[0][0] == "GET" and client.calls[0][1].endswith("/api/version")
    assert client.calls[1][0] == "POST"
    assert client.calls[1][1].endswith("/api/hub/maintenance/studio-update-repairs")
    assert client.calls[1][2] == {"X-Hub-Token": "shared-secret"}
    assert client.calls[1][3] == {"studio_ids": ["image"], "local_only": True}


@pytest.mark.asyncio
async def test_remote_update_repair_old_or_unreachable_hub_is_retryable(reset):
    peers.set_fleet_token("shared-secret")

    class OldClient:
        async def get(self, *_args, **_kwargs):
            return FakeResp(200, {"studio_update_repair_schema": 0})
        async def post(self, *_args, **_kwargs):
            raise AssertionError("old Hub must not receive repair request")

    class OfflineClient:
        async def get(self, *_args, **_kwargs):
            raise httpx.ConnectError("offline")

    old = await peers.start_remote_studio_update_repair(OldClient(), REMOTE[0])
    offline = await peers.start_remote_studio_update_repair(OfflineClient(), REMOTE[0])

    assert old["ok"] is False and old["retryable"] is True
    assert "update the Agent Hub" in old["error"]
    assert offline["ok"] is False and offline["retryable"] is True
