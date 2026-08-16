import json
import stat
import asyncio
import hashlib
import os
import socket
import sys
import textwrap
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from backend import (
    auth, broadcast, control_plane, enrollment, fleet_ops, hardware_profiles,
    job_storage, ledger, model_exposure, peers, registry,
    release_reconciliation,
)
from backend.controller_settings_lock import SettingsWriterBusy, settings_writer_lock
from backend.enrollment_repair_executor import RepairExecutor
from backend.enrollment_repair_transport import (
    OriginInvalid,
    PinnedJSONConnection,
    PinnedTransportError,
    ResolvedOrigin,
    resolve_private_origin,
)


ADDRESS = "100.70.0.1"
ORIGIN = "http://controller.test:47873"
REQUEST_ID = "request-000000000000000000000001"
TARGET = "registered-target"
TICKET = "t" * 43
FLEET_TOKEN = "fleet-secret"


class Clock:
    def __init__(self, value=1_000.0):
        self.value = value

    def __call__(self):
        return self.value


class FakeConnection:
    def __init__(self, *, status=200, response=None, error=None, direct_peer=ADDRESS):
        self.status = status
        self.response = response or {}
        self.error = error
        self.direct_peer = direct_peer
        self.local_address = "100.70.0.2"
        self.calls = []

    async def request_json(self, method, path, *, headers, body, timeout):
        self.calls.append((method, path, headers, body, timeout))
        if self.error:
            raise self.error
        return self.status, self.response


def settings(role="standalone", parent=None):
    return {
        "version": 2,
        "role": role,
        "site_id": "damaged-site",
        "site_name": "Damaged site",
        "controller_id": "damaged-id",
        "database_mode": "off",
        "parent_controller_url": parent,
        "preserved": {"nested": True},
    }


def dispatch(**changes):
    value = {
        "schema": "studiohub.enrollment-repair-dispatch",
        "schema_version": 1,
        "request_id": REQUEST_ID,
        "target_machine_id": TARGET,
        "ticket": TICKET,
        "redemption_expires_at": 1_120.0,
        "controller_url": ORIGIN,
        "controller": {
            "site_id": "controller-site",
            "site_name": "Controller site",
            "controller_id": "controller-hub",
        },
    }
    value.update(changes)
    return value


def claim(payload=None):
    payload = payload or dispatch()
    return {
        "schema": "studiohub.enrollment-repair-claim",
        "schema_version": 1,
        "request_id": payload["request_id"],
        "target_machine_id": payload["target_machine_id"],
        "role": "agent",
        "site_id": payload["controller"]["site_id"],
        "site_name": payload["controller"]["site_name"],
        "controller_id": payload["target_machine_id"],
    }


def address_resolver(addresses=(ADDRESS,)):
    def resolve(host, port, *args, **kwargs):
        assert host in {"controller.test", "8.8.8.8"}
        return [
            (2, 1, 6, "", (address, port))
            for address in addresses
        ]

    return resolve


def origin_resolver(addresses=(ADDRESS,)):
    return lambda value: resolve_private_origin(
        value, resolver=address_resolver(addresses)
    )


def factory_for(connection, opened=None):
    @asynccontextmanager
    async def factory(origin):
        if opened is not None:
            opened.append(origin)
        yield connection

    return factory


def make_executor(tmp_path, monkeypatch, connection, *, clock=None, resolver=None, opened=None):
    monkeypatch.setenv("STUDIOHUB_FLEET_TOKEN", FLEET_TOKEN)
    settings_path = tmp_path / "controller_settings.json"
    journal_path = tmp_path / ".enrollment_repair_journal.json"
    return (
        RepairExecutor(
            journal_path=journal_path,
            settings_path=settings_path,
            clock=clock or Clock(),
            origin_resolver=resolver or origin_resolver(),
            connection_factory=factory_for(connection, opened),
        ),
        settings_path,
        journal_path,
    )


class SimulatedCrash(BaseException):
    pass


@pytest.mark.asyncio
async def test_wrong_role_and_missing_parent_are_repairable(tmp_path, monkeypatch):
    for role in ("standalone", "controller"):
        case = tmp_path / role
        case.mkdir()
        connection = FakeConnection(response=claim())
        executor, settings_path, journal_path = make_executor(case, monkeypatch, connection)
        original = json.dumps(settings(role=role, parent=None), sort_keys=True).encode()
        settings_path.write_bytes(original)

        result = await executor.apply(dispatch(), direct_source=ADDRESS)

        assert result["state"] == "complete"
        assert len(connection.calls) == 1
        observed = connection.calls[0][3]["observed_identity"]
        assert observed["role"] == role
        assert observed["parent_controller_url"] is None
        journal = json.loads(journal_path.read_text())
        assert journal["audit"]["saved_parent"] == {
            "present": False,
            "status": "missing",
            "matches_dispatch": False,
        }


@pytest.mark.asyncio
async def test_wrong_saved_parent_is_optional_audit_evidence(tmp_path, monkeypatch):
    stale = "http://100.70.0.99:47873"
    connection = FakeConnection(response=claim())
    opened = []
    executor, settings_path, journal_path = make_executor(
        tmp_path, monkeypatch, connection, opened=opened
    )
    settings_path.write_text(json.dumps(settings(role="agent", parent=stale)))

    result = await executor.apply(dispatch(), direct_source=ADDRESS)

    assert result["state"] == "complete"
    assert [item.origin for item in opened] == [ORIGIN]
    journal = json.loads(journal_path.read_text())
    assert journal["audit"]["saved_parent"] == {
        "present": True,
        "status": "valid",
        "origin": stale,
        "matches_dispatch": False,
    }
    assert connection.calls[0][3]["observed_identity"]["parent_controller_url"] == stale


@pytest.mark.asyncio
async def test_callback_origin_must_resolve_to_direct_dispatch_peer(tmp_path, monkeypatch):
    good_connection = FakeConnection(response=claim())
    good, settings_path, _ = make_executor(
        tmp_path / "good", monkeypatch, good_connection
    )
    settings_path.parent.mkdir()
    settings_path.write_text(json.dumps(settings()))
    assert (await good.apply(dispatch(), direct_source=ADDRESS))["state"] == "complete"

    bad_connection = FakeConnection(response=claim())
    bad, settings_path, journal_path = make_executor(
        tmp_path / "bad", monkeypatch, bad_connection
    )
    settings_path.parent.mkdir()
    settings_path.write_text(json.dumps(settings()))
    result = await bad.apply(dispatch(), direct_source="100.70.0.2")

    assert result == {
        "request_id": REQUEST_ID,
        "state": "needs_review",
        "error_code": "callback_source_mismatch",
    }
    assert bad_connection.calls == []
    assert not journal_path.exists()


@pytest.mark.asyncio
async def test_public_multi_address_credentialed_path_query_and_fragment_origins_fail(
    tmp_path, monkeypatch
):
    cases = (
        ("http://8.8.8.8:47873", ("8.8.8.8",)),
        (ORIGIN, (ADDRESS, "100.70.0.2")),
        ("http://user:secret@controller.test:47873", (ADDRESS,)),
        (ORIGIN + "/redeem", (ADDRESS,)),
        (ORIGIN + "?ticket=secret", (ADDRESS,)),
        (ORIGIN + "#fragment", (ADDRESS,)),
    )
    for index, (value, addresses) in enumerate(cases):
        case = tmp_path / str(index)
        case.mkdir()
        connection = FakeConnection(response=claim())
        executor, settings_path, journal_path = make_executor(
            case, monkeypatch, connection, resolver=origin_resolver(addresses)
        )
        original = json.dumps(settings(), sort_keys=True).encode()
        settings_path.write_bytes(original)

        result = await executor.apply(
            dispatch(controller_url=value), direct_source=ADDRESS
        )

        assert result == {
            "request_id": REQUEST_ID,
            "state": "needs_review",
            "error_code": "callback_url_invalid",
        }
        assert connection.calls == []
        assert settings_path.read_bytes() == original
        assert not journal_path.exists()


@pytest.mark.asyncio
async def test_redirect_and_proxy_environment_are_never_used(tmp_path, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    connection = FakeConnection(
        status=302, response={"location": "http://attacker.test/claim"}
    )
    opened = []
    executor, settings_path, journal_path = make_executor(
        tmp_path, monkeypatch, connection, opened=opened
    )
    settings_path.write_text(json.dumps(settings()))

    result = await executor.apply(dispatch(), direct_source=ADDRESS)

    assert result == {
        "request_id": REQUEST_ID,
        "state": "needs_review",
        "error_code": "callback_url_invalid",
    }
    assert len(connection.calls) == 1
    assert [item.address for item in opened] == [ADDRESS]
    journal = json.loads(journal_path.read_text())
    assert journal["state"] == "needs_review"
    assert "ticket" not in journal


@pytest.mark.asyncio
async def test_lost_response_before_claim_save_expires_to_never_applied(
    tmp_path, monkeypatch
):
    clock = Clock()
    connection = FakeConnection(error=TimeoutError("lost response " + TICKET))
    executor, settings_path, journal_path = make_executor(
        tmp_path, monkeypatch, connection, clock=clock
    )
    original = json.dumps(settings(), sort_keys=True).encode()
    settings_path.write_bytes(original)

    result = await executor.apply(dispatch(), direct_source=ADDRESS)

    assert result == {
        "request_id": REQUEST_ID,
        "state": "redemption_attempted",
        "outcome": "unknown",
        "error_code": "transport_unavailable",
    }
    journal = json.loads(journal_path.read_text())
    assert journal["state"] == "redemption_attempted"
    assert journal["outcome"] == "unknown"
    assert journal["ticket"] == TICKET
    assert stat.S_IMODE(journal_path.stat().st_mode) == 0o600
    assert settings_path.read_bytes() == original
    assert TICKET not in json.dumps(result)

    clock.value = dispatch()["redemption_expires_at"]
    status_result = executor.status(REQUEST_ID, direct_source=ADDRESS)

    assert status_result == {"request_id": REQUEST_ID, "state": "never_applied"}
    expired = json.loads(journal_path.read_text())
    assert expired["state"] == "never_applied"
    assert "ticket" not in expired
    assert "claim" not in expired
    assert settings_path.read_bytes() == original


@pytest.mark.asyncio
async def test_identical_dispatch_adopts_and_different_unresolved_request_conflicts(
    tmp_path, monkeypatch
):
    clock = Clock()
    connection = FakeConnection(error=TimeoutError("lost"))
    executor, settings_path, journal_path = make_executor(
        tmp_path, monkeypatch, connection, clock=clock
    )
    settings_path.write_text(json.dumps(settings()))
    await executor.apply(dispatch(), direct_source=ADDRESS)
    first_journal = journal_path.read_bytes()

    adopted = await executor.apply(dispatch(), direct_source=ADDRESS)
    conflict = await executor.apply(
        dispatch(request_id="request-000000000000000000000002"),
        direct_source=ADDRESS,
    )

    assert adopted["state"] == "redemption_attempted"
    assert len(connection.calls) == 2
    assert conflict == {
        "request_id": "request-000000000000000000000002",
        "state": "needs_review",
        "error_code": "request_conflict",
    }
    assert json.loads(journal_path.read_text())["request_id"] == REQUEST_ID
    assert first_journal != b""


@pytest.mark.asyncio
async def test_environment_locked_and_runtime_database_mode_fail_before_redemption(
    tmp_path, monkeypatch
):
    cases = (
        ("STUDIOHUB_ROLE", " ", "environment_locked"),
        ("STUDIOHUB_DATABASE_MODE", "shadow", "database_mode_unsafe"),
    )
    for index, (name, value, error_code) in enumerate(cases):
        monkeypatch.delenv("STUDIOHUB_ROLE", raising=False)
        monkeypatch.delenv("STUDIOHUB_DATABASE_MODE", raising=False)
        monkeypatch.setenv(name, value)
        case = tmp_path / str(index)
        case.mkdir()
        connection = FakeConnection(response=claim())
        executor, settings_path, journal_path = make_executor(
            case, monkeypatch, connection
        )
        monkeypatch.setenv(name, value)
        settings_path.write_text(json.dumps(settings()))

        result = await executor.apply(dispatch(), direct_source=ADDRESS)

        assert result["error_code"] == error_code
        assert connection.calls == []
        assert not journal_path.exists()


@pytest.mark.asyncio
async def test_boolean_schema_version_and_non_urlsafe_ticket_are_invalid_dispatches(
    tmp_path, monkeypatch
):
    cases = (
        dispatch(schema_version=True),
        dispatch(ticket="!" * 43),
    )
    for index, payload in enumerate(cases):
        case = tmp_path / str(index)
        case.mkdir()
        connection = FakeConnection(response=claim(payload))
        executor, settings_path, journal_path = make_executor(
            case, monkeypatch, connection
        )
        settings_path.write_text(json.dumps(settings()))

        result = await executor.apply(payload, direct_source=ADDRESS)

        assert result["error_code"] == "dispatch_invalid"
        assert connection.calls == []
        assert not journal_path.exists()


@pytest.mark.asyncio
async def test_concurrent_identical_apply_cannot_overwrite_durable_claim(
    tmp_path, monkeypatch
):
    class RacingConnection(FakeConnection):
        def __init__(self):
            super().__init__(response=claim())
            self.started = asyncio.Event()
            self.count = 0

        async def request_json(self, method, path, *, headers, body, timeout):
            self.calls.append((method, path, headers, body, timeout))
            self.count += 1
            if self.count == 1:
                self.started.set()
                await asyncio.sleep(0)
                return 200, claim()
            await self.started.wait()
            await asyncio.sleep(0.02)
            return 409, {}

    connection = RacingConnection()
    executor, settings_path, journal_path = make_executor(
        tmp_path, monkeypatch, connection
    )
    settings_path.write_text(json.dumps(settings()))

    first, second = await asyncio.gather(
        executor.apply(dispatch(), direct_source=ADDRESS),
        executor.apply(dispatch(), direct_source=ADDRESS),
    )

    assert first["state"] == second["state"] == "complete"
    assert len(connection.calls) == 1
    journal = json.loads(journal_path.read_text())
    assert journal["state"] == "complete"
    assert "ticket" not in journal and "claim" not in journal


@pytest.mark.asyncio
async def test_pinned_transport_uses_one_address_original_host_and_connection_close(
    monkeypatch,
):
    from backend import enrollment_repair_transport as transport

    events = {}

    class Socket:
        def settimeout(self, value):
            events["timeout"] = value

        def getpeername(self):
            return (ADDRESS, 47873)

        def getsockname(self):
            return ("100.70.0.2", 50000)

        def close(self):
            events["socket_closed"] = True

    class Response:
        status = 200

        def getheader(self, name):
            return "11" if name == "Content-Length" else None

        def read(self, size):
            events["read_bound"] = size
            return b'{"ok":true}'

    class HTTPConnection:
        def __init__(self, host, port, timeout):
            events["http_target"] = (host, port, timeout)
            self.sock = None

        def request(self, method, path, *, body, headers, encode_chunked):
            events["request"] = (method, path, body, headers, encode_chunked)

        def getresponse(self):
            return Response()

        def close(self):
            self.sock.close()

    def create_connection(target, timeout):
        events["socket_target"] = (target, timeout)
        return Socket()

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setattr(transport.socket, "create_connection", create_connection)
    monkeypatch.setattr(transport.http.client, "HTTPConnection", HTTPConnection)
    origin = ResolvedOrigin(ORIGIN, ADDRESS, "controller.test:47873")
    connection = PinnedJSONConnection(origin)

    await connection.connect(timeout=5.0)

    assert events["socket_target"][0] == (ADDRESS, 47873)
    assert connection.direct_peer == ADDRESS
    assert connection.local_address == "100.70.0.2"
    assert "request" not in events
    status_code, response = await connection.request_json(
        "POST", "/redeem", headers={"X-Hub-Token": FLEET_TOKEN},
        body={"request_id": REQUEST_ID}, timeout=5.0,
    )

    assert (status_code, response) == (200, {"ok": True})
    assert events["socket_target"][0] == (ADDRESS, 47873)
    assert 0 < events["socket_target"][1] <= 5.0
    headers = events["request"][3]
    assert headers["Host"] == "controller.test:47873"
    assert headers["Connection"] == "close"
    assert "Forwarded" not in headers and "X-Forwarded-For" not in headers
    assert connection.direct_peer == ADDRESS
    assert connection.local_address == "100.70.0.2"
    assert events["socket_closed"] is True


@pytest.mark.asyncio
async def test_pinned_transport_enforces_response_bound_without_secret_exception(
    monkeypatch,
):
    from backend import enrollment_repair_transport as transport

    class Socket:
        def settimeout(self, value):
            pass

        def getpeername(self):
            return (ADDRESS, 47873)

        def getsockname(self):
            return ("100.70.0.2", 50000)

        def close(self):
            pass

    class Response:
        status = 200

        def getheader(self, name):
            return str(300_000) if name == "Content-Length" else None

    class HTTPConnection:
        def __init__(self, host, port, timeout):
            self.sock = None

        def request(self, method, path, *, body, headers, encode_chunked):
            pass

        def getresponse(self):
            return Response()

        def close(self):
            self.sock.close()

    monkeypatch.setattr(
        transport.socket, "create_connection", lambda target, timeout: Socket()
    )
    monkeypatch.setattr(transport.http.client, "HTTPConnection", HTTPConnection)
    connection = PinnedJSONConnection(
        ResolvedOrigin(ORIGIN, ADDRESS, "controller.test:47873")
    )

    with pytest.raises(PinnedTransportError) as error:
        await connection.request_json(
            "POST", "/redeem", headers={"X-Hub-Token": FLEET_TOKEN},
            body={"ticket": TICKET}, timeout=5.0,
        )

    assert str(error.value) == "transport_response_too_large"
    assert FLEET_TOKEN not in repr(error.value)
    assert TICKET not in repr(error.value)


@pytest.mark.asyncio
async def test_prepared_transport_reuses_connect_deadline_without_writing_secrets(
    monkeypatch,
):
    from backend import enrollment_repair_transport as transport

    events = {}
    monotonic = Clock(0.0)

    class Socket:
        def settimeout(self, value):
            events["timeout"] = value

        def getpeername(self):
            return (ADDRESS, 47873)

        def getsockname(self):
            return ("100.70.0.2", 50000)

        def shutdown(self, how):
            events["shutdown"] = how

        def close(self):
            events["socket_closed"] = True

    class HTTPConnection:
        def __init__(self, host, port, timeout):
            self.sock = None

        def request(self, method, path, *, body, headers, encode_chunked):
            events["request"] = (method, path, body, headers, encode_chunked)

        def getresponse(self):
            return Response()

        def close(self):
            self.sock.close()

    class Response:
        status = 200

        def getheader(self, name):
            return "11" if name == "Content-Length" else None

        def read(self, size):
            return b'{"ok":true}'

    monkeypatch.setattr(transport.time, "monotonic", monotonic)
    monkeypatch.setattr(
        transport.socket, "create_connection", lambda target, timeout: Socket()
    )
    monkeypatch.setattr(transport.http.client, "HTTPConnection", HTTPConnection)
    connection = PinnedJSONConnection(
        ResolvedOrigin(ORIGIN, ADDRESS, "controller.test:47873")
    )

    await connection.connect(timeout=0.2)
    monotonic.value = 0.21

    with pytest.raises(PinnedTransportError, match="transport_timeout") as error:
        await connection.request_json(
            "POST", "/redeem", headers={"X-Hub-Token": FLEET_TOKEN},
            body={"ticket": TICKET}, timeout=0.2,
        )

    assert "request" not in events
    assert events["socket_closed"] is True
    assert FLEET_TOKEN not in repr(error.value)
    assert TICKET not in repr(error.value)


@pytest.mark.asyncio
async def test_redemption_attempt_journal_fsyncs_file_and_directory(
    tmp_path, monkeypatch
):
    from backend import enrollment_repair_executor as executor_module

    synced = []
    real_fsync = executor_module.os.fsync

    def fsync(descriptor):
        mode = executor_module.os.fstat(descriptor).st_mode
        synced.append("directory" if stat.S_ISDIR(mode) else "file")
        real_fsync(descriptor)

    monkeypatch.setattr(executor_module.os, "fsync", fsync)
    connection = FakeConnection(error=TimeoutError("lost"))
    executor, settings_path, _journal_path = make_executor(
        tmp_path, monkeypatch, connection
    )
    settings_path.write_text(json.dumps(settings()))

    await executor.apply(dispatch(), direct_source=ADDRESS)

    assert synced.count("file") >= 2
    assert synced.count("directory") >= 2


@pytest.mark.asyncio
async def test_first_durable_ticket_state_is_outcome_unknown_and_restart_retries(
    tmp_path, monkeypatch
):
    first_connection = FakeConnection(response=claim())
    executor, settings_path, journal_path = make_executor(
        tmp_path, monkeypatch, first_connection
    )
    settings_path.write_text(json.dumps(settings()))
    real_save = executor._save_journal
    saves = 0

    def crash_after_first_save(value):
        nonlocal saves
        real_save(value)
        saves += 1
        if saves == 1:
            raise RuntimeError("simulated process loss")

    executor._save_journal = crash_after_first_save

    with pytest.raises(RuntimeError, match="simulated process loss"):
        await executor.apply(dispatch(), direct_source=ADDRESS)

    stranded = json.loads(journal_path.read_text())
    assert stranded["state"] == "redemption_attempted"
    assert stranded["outcome"] == "unknown"
    assert stranded["ticket"] == TICKET
    assert "claim" not in stranded
    assert first_connection.calls == []

    retry_connection = FakeConnection(response=claim())
    restarted = RepairExecutor(
        journal_path=journal_path,
        settings_path=settings_path,
        clock=Clock(),
        origin_resolver=origin_resolver(),
        connection_factory=factory_for(retry_connection),
    )
    adopted = await restarted.apply(dispatch(), direct_source=ADDRESS)

    assert adopted["request_id"] == REQUEST_ID
    assert adopted["state"] == "complete"
    assert len(retry_connection.calls) == 1
    completed = json.loads(journal_path.read_text())
    assert completed["state"] == "complete"
    assert "ticket" not in completed and "claim" not in completed


@pytest.mark.asyncio
async def test_feature_disable_allows_delayed_issued_dispatch_and_rejects_unissued_ticket(
    tmp_path, monkeypatch,
):
    delayed_dir = tmp_path / "delayed-issued"
    delayed_dir.mkdir()
    delayed_connection = FakeConnection(response=claim())
    delayed, delayed_settings, delayed_journal = make_executor(
        delayed_dir, monkeypatch, delayed_connection
    )
    original = json.dumps(settings(), sort_keys=True).encode()
    delayed_settings.write_bytes(original)

    completed = await delayed.apply(dispatch(), direct_source=ADDRESS)

    assert completed["state"] == "complete"
    assert len(delayed_connection.calls) == 1
    repaired = json.loads(delayed_settings.read_text())
    assert repaired == {
        **settings(),
        "role": "agent",
        "site_id": "controller-site",
        "site_name": "Controller site",
        "controller_id": TARGET,
        "parent_controller_url": ORIGIN,
    }
    assert json.loads(delayed_journal.read_text())["state"] == "complete"

    unissued_dir = tmp_path / "fabricated-unissued"
    unissued_dir.mkdir()
    unissued_connection = FakeConnection(
        status=410, response={"code": "ticket_invalid"},
    )
    unissued, unissued_settings, unissued_journal = make_executor(
        unissued_dir, monkeypatch, unissued_connection
    )
    unissued_settings.write_bytes(original)

    rejected = await unissued.apply(
        dispatch(ticket="f" * 43), direct_source=ADDRESS,
    )

    assert rejected["state"] == "redemption_attempted"
    assert rejected["error_code"] == "transport_unavailable"
    assert len(unissued_connection.calls) == 1
    assert unissued_settings.read_bytes() == original
    rejected_journal = json.loads(unissued_journal.read_text())
    assert rejected_journal["state"] == "redemption_attempted"
    assert rejected_journal["outcome"] == "unknown"
    assert "claim" not in rejected_journal
    assert "apply_started_at" not in rejected_journal

    existing_dir = tmp_path / "existing-issued"
    existing_dir.mkdir()
    connection = FakeConnection(error=TimeoutError("lost response"))
    clock = Clock()
    existing, existing_settings, existing_journal = make_executor(
        existing_dir, monkeypatch, connection, clock=clock
    )
    existing_settings.write_bytes(original)
    first = await existing.apply(dispatch(), direct_source=ADDRESS)
    assert first["state"] == "redemption_attempted"
    assert existing_journal.exists()

    adopted = await existing.apply(dispatch(), direct_source=ADDRESS)

    assert adopted["state"] == "redemption_attempted"
    assert adopted["error_code"] == "transport_unavailable"
    assert len(connection.calls) == 2
    assert existing_settings.read_bytes() == original

    clock.value = dispatch()["redemption_expires_at"]
    assert existing.status(REQUEST_ID, direct_source=ADDRESS)["state"] == "never_applied"
    terminal_journal = existing_journal.read_bytes()
    clock.value = 10**12
    assert existing.status(REQUEST_ID, direct_source=ADDRESS)["state"] == "never_applied"
    assert existing_journal.read_bytes() == terminal_journal
    assert existing_settings.read_bytes() == original


@pytest.mark.asyncio
async def test_invalid_accepted_ticket_journal_cannot_adopt_or_redeem(
    tmp_path, monkeypatch
):
    connection = FakeConnection(response=claim())
    executor, settings_path, journal_path = make_executor(
        tmp_path, monkeypatch, connection
    )
    settings_path.write_text(json.dumps(settings()))
    journal_path.write_text(json.dumps({
        "state": "accepted",
        "outcome": "unknown",
        "request_id": REQUEST_ID,
        "ticket": TICKET,
        "redemption_expires_at": 1_120.0,
        "controller_address": ADDRESS,
        "dispatch_digest": "0" * 64,
    }))

    result = await executor.apply(dispatch(), direct_source=ADDRESS)

    assert result["error_code"] == "journal_invalid"
    assert connection.calls == []


@pytest.mark.asyncio
async def test_two_executor_instances_share_one_claim_transition(tmp_path, monkeypatch):
    class YieldingConnection(FakeConnection):
        async def request_json(self, method, path, *, headers, body, timeout):
            self.calls.append((method, path, headers, body, timeout))
            await asyncio.sleep(0.02)
            return 200, claim()

    connection = YieldingConnection(response=claim())
    first, settings_path, journal_path = make_executor(
        tmp_path, monkeypatch, connection
    )
    second = RepairExecutor(
        journal_path=journal_path,
        settings_path=settings_path,
        clock=Clock(),
        origin_resolver=origin_resolver(),
        connection_factory=factory_for(connection),
    )
    settings_path.write_text(json.dumps(settings()))

    results = await asyncio.gather(
        first.apply(dispatch(), direct_source=ADDRESS),
        second.apply(dispatch(), direct_source=ADDRESS),
    )

    assert [result["state"] for result in results] == ["complete", "complete"]
    assert len(connection.calls) == 1
    journal = json.loads(journal_path.read_text())
    assert journal["state"] == "complete"
    assert "ticket" not in journal
    lock_path = journal_path.with_name(f"{journal_path.name}.lock")
    assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_status_expiry_cannot_overwrite_or_preempt_live_claim_transition(
    tmp_path, monkeypatch
):
    class PausedConnection(FakeConnection):
        def __init__(self):
            super().__init__(response=claim())
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def request_json(self, method, path, *, headers, body, timeout):
            self.calls.append((method, path, headers, body, timeout))
            self.started.set()
            await self.release.wait()
            return 200, claim()

    clock = Clock()
    connection = PausedConnection()
    executor, settings_path, journal_path = make_executor(
        tmp_path, monkeypatch, connection, clock=clock
    )
    settings_path.write_text(json.dumps(settings()))
    apply_task = asyncio.create_task(executor.apply(dispatch(), direct_source=ADDRESS))
    await connection.started.wait()
    clock.value = dispatch()["redemption_expires_at"]
    status_task = asyncio.create_task(asyncio.to_thread(
        executor.status, REQUEST_ID, direct_source=ADDRESS
    ))
    await asyncio.sleep(0.02)
    connection.release.set()

    applied, status_result = await asyncio.gather(apply_task, status_task)

    assert applied["request_id"] == REQUEST_ID
    assert applied["state"] == "complete"
    assert status_result["state"] in {"redemption_attempted", "applying", "complete"}
    journal = json.loads(journal_path.read_text())
    assert journal["state"] == "complete"
    assert "ticket" not in journal


@pytest.mark.asyncio
@pytest.mark.parametrize("same_request", [True, False])
async def test_subprocesses_share_journal_lock_for_adoption_and_conflict(
    tmp_path, monkeypatch, same_request
):
    monkeypatch.setenv("STUDIOHUB_FLEET_TOKEN", FLEET_TOKEN)
    settings_path = tmp_path / "controller_settings.json"
    journal_path = tmp_path / ".enrollment_repair_journal.json"
    ready_path = tmp_path / "ready"
    callbacks_path = tmp_path / "callbacks"
    settings_path.write_text(json.dumps(settings()))
    second_request = REQUEST_ID if same_request else "request-000000000000000000000002"
    script = textwrap.dedent(
        """
        import asyncio, json, os, sys, time
        from contextlib import asynccontextmanager
        from pathlib import Path
        from backend.enrollment_repair_executor import RepairExecutor
        from backend.enrollment_repair_transport import ResolvedOrigin

        journal, settings, ready, callbacks = map(Path, sys.argv[1:5])
        request_id = sys.argv[5]
        payload = {
            "schema": "studiohub.enrollment-repair-dispatch",
            "schema_version": 1,
            "request_id": request_id,
            "target_machine_id": "registered-target",
            "ticket": "t" * 43,
            "redemption_expires_at": 1120.0,
            "controller_url": "http://controller.test:47873",
            "controller": {
                "site_id": "controller-site",
                "site_name": "Controller site",
                "controller_id": "controller-hub",
            },
        }

        class Connection:
            direct_peer = "100.70.0.1"
            local_address = "100.70.0.2"
            async def request_json(self, method, path, *, headers, body, timeout):
                with callbacks.open("a", encoding="utf-8") as stream:
                    stream.write(request_id + "\\n")
                return 200, {
                    "schema": "studiohub.enrollment-repair-claim",
                    "schema_version": 1,
                    "request_id": request_id,
                    "target_machine_id": "registered-target",
                    "role": "agent",
                    "site_id": "controller-site",
                    "site_name": "Controller site",
                    "controller_id": "registered-target",
                }

        @asynccontextmanager
        async def factory(origin):
            yield Connection()

        executor = RepairExecutor(
            journal_path=journal,
            settings_path=settings,
            clock=lambda: 1000.0,
            origin_resolver=lambda value: ResolvedOrigin(
                "http://controller.test:47873", "100.70.0.1", "controller.test:47873"
            ),
            connection_factory=factory,
        )
        original_load = executor._load_journal
        first_load = True
        def synchronized_load():
            global first_load
            value = original_load()
            if first_load and value is None:
                first_load = False
                with ready.open("a", encoding="utf-8") as stream:
                    stream.write(str(os.getpid()) + "\\n")
                deadline = time.monotonic() + 0.2
                while time.monotonic() < deadline:
                    try:
                        if len(ready.read_text().splitlines()) >= 2:
                            break
                    except FileNotFoundError:
                        pass
                    time.sleep(0.005)
            return value
        executor._load_journal = synchronized_load
        print(json.dumps(asyncio.run(executor.apply(payload, direct_source="100.70.0.1"))))
        """
    )
    environment = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        "STUDIOHUB_FLEET_TOKEN": FLEET_TOKEN,
    }

    async def run(request_id):
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", script,
            str(journal_path), str(settings_path), str(ready_path),
            str(callbacks_path), request_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=5)
        assert process.returncode == 0, stderr.decode()
        return json.loads(stdout)

    results = await asyncio.gather(run(REQUEST_ID), run(second_request))

    callbacks = callbacks_path.read_text().splitlines()
    assert len(callbacks) == 1
    assert sum(result["state"] == "complete" for result in results) >= 1
    if same_request:
        assert {result["state"] for result in results} == {"complete"}
    else:
        conflict = next(result for result in results if result["state"] != "complete")
        assert conflict["error_code"] == "request_conflict"
    journal = json.loads(journal_path.read_text())
    assert journal["state"] == "complete"
    assert "ticket" not in journal


@pytest.mark.asyncio
async def test_apply_changes_only_five_identity_parent_fields(tmp_path, monkeypatch):
    from backend import enrollment_repair_executor as executor_module

    connection = FakeConnection(response=claim())
    executor, settings_path, journal_path = make_executor(
        tmp_path, monkeypatch, connection
    )
    preimage = {
        **settings(role="controller", parent="http://100.70.0.99:47873"),
        "owner_password_hash": "preserved-hash",
        "update_schedule": {"enabled": True, "hour": 3},
    }
    settings_path.write_text(json.dumps(preimage, indent=4) + "\n")
    reloads = []
    monkeypatch.setattr(
        control_plane, "reload_settings_cache", lambda: reloads.append(True)
    )
    settings_replacements = []
    real_replace = executor_module.os.replace

    def replace(source, destination):
        if Path(destination) == settings_path:
            assert stat.S_IMODE(Path(source).stat().st_mode) == 0o600
            settings_replacements.append((Path(source), Path(destination)))
        real_replace(source, destination)

    monkeypatch.setattr(executor_module.os, "replace", replace)

    result = await executor.apply(dispatch(), direct_source=ADDRESS)

    applied = json.loads(settings_path.read_text())
    changed = {
        key for key in preimage.keys() | applied.keys()
        if preimage.get(key) != applied.get(key)
    }
    assert changed == {
        "role", "site_id", "site_name", "controller_id", "parent_controller_url",
    }
    assert applied == {
        **preimage,
        "role": "agent",
        "site_id": "controller-site",
        "site_name": "Controller site",
        "controller_id": TARGET,
        "parent_controller_url": ORIGIN,
    }
    assert stat.S_IMODE(settings_path.stat().st_mode) == 0o600
    assert len(settings_replacements) == 1
    assert result == {
        "request_id": REQUEST_ID,
        "state": "complete",
        "identity": {
            "role": "agent",
            "site_id": "controller-site",
            "site_name": "Controller site",
            "controller_id": TARGET,
        },
        "applied_at": 1_000.0,
    }
    assert reloads == [True]
    terminal = json.loads(journal_path.read_text())
    assert terminal["state"] == "complete"
    assert "ticket" not in terminal and "claim" not in terminal


@pytest.mark.asyncio
async def test_fleet_token_files_are_byte_identical_after_every_outcome(
    tmp_path, monkeypatch, reset
):
    peers.FLEET_TOKEN_FILE.write_bytes(b"file-fleet-token\n")
    peers.SHARED_STUDIO_TOKEN_FILE.write_bytes(b"shared-studio-token\n")
    os.chmod(peers.FLEET_TOKEN_FILE, 0o640)
    os.chmod(peers.SHARED_STUDIO_TOKEN_FILE, 0o604)
    token_snapshot = {
        path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
        for path in (peers.FLEET_TOKEN_FILE, peers.SHARED_STUDIO_TOKEN_FILE)
    }

    async def run_case(name, connection, *, clock=None, checkpoint=None):
        case = tmp_path / name
        case.mkdir()
        executor, settings_path, _journal_path = make_executor(
            case, monkeypatch, connection, clock=clock
        )
        settings_path.write_text(json.dumps(settings()))
        if checkpoint is not None:
            def inject(site):
                if site == checkpoint:
                    raise RuntimeError(f"injected {site}")
            executor._checkpoint = inject
        result = await executor.apply(dispatch(), direct_source=ADDRESS)
        assert {
            path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
            for path in (peers.FLEET_TOKEN_FILE, peers.SHARED_STUDIO_TOKEN_FILE)
        } == token_snapshot
        return executor, result

    _complete, completed = await run_case(
        "complete", FakeConnection(response=claim())
    )
    assert completed["state"] == "complete"

    clock = Clock()
    never, unknown = await run_case(
        "never", FakeConnection(error=TimeoutError("lost")), clock=clock
    )
    assert unknown["state"] == "redemption_attempted"
    clock.value = dispatch()["redemption_expires_at"]
    assert never.status(REQUEST_ID, direct_source=ADDRESS)["state"] == "never_applied"

    bad_claim = {**claim(), "unexpected": "value"}
    _review, reviewed = await run_case(
        "review", FakeConnection(response=bad_claim)
    )
    assert reviewed["state"] == "needs_review"

    for site, expected_state in (
        ("stage_write", "never_applied"),
        ("file_fsync", "never_applied"),
        ("pre_replace_recheck", "never_applied"),
        ("replace", "never_applied"),
        ("directory_fsync", "complete"),
        ("read_back", "complete"),
    ):
        _failed, result = await run_case(
            site, FakeConnection(response=claim()), checkpoint=site
        )
        assert result["state"] == expected_state

    assert {
        path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
        for path in (peers.FLEET_TOKEN_FILE, peers.SHARED_STUDIO_TOKEN_FILE)
    } == token_snapshot


@pytest.mark.asyncio
async def test_repair_outcomes_preserve_owner_registry_workload_model_update_and_release_state(
    tmp_path, monkeypatch, reset,
):
    preserved_paths = {
        "owner_password": auth.PASSWORD_FILE,
        "owner_sessions": auth.SESSIONS_FILE,
        "hub_token": auth.TOKEN_FILE,
        "fleet_token": peers.FLEET_TOKEN_FILE,
        "shared_fleet_token": peers.SHARED_STUDIO_TOKEN_FILE,
        "permanent_code": enrollment.ENROLLMENT_CODE_FILE,
        "permanent_code_db": enrollment.DB_FILE,
        "registry_endpoints": registry.REGISTRY_FILE,
        "labels": registry.LABELS_FILE,
        "profile": hardware_profiles.MACHINE_PROFILES_FILE,
        "off_flags": registry.FLAGS_FILE,
        "jobs_artifacts": ledger.DB_FILE,
        "job_storage": job_storage.SETTINGS_FILE,
        "models": model_exposure.STATE_FILE,
        "model_cache": registry.DATA_DIR / "catalog_state.json",
        "updater_state": fleet_ops._STATE_FILE,
        "update_history": broadcast.DOWNLOADS_FILE,
        "managed_release": release_reconciliation.STATE_FILE,
    }
    for index, (name, path) in enumerate(preserved_paths.items()):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"task13:{name}:sentinel\n".encode())
        os.chmod(path, 0o600 + (index % 4) * 0o10)

    def digest():
        return {
            name: (
                hashlib.sha256(path.read_bytes()).hexdigest(),
                stat.S_IMODE(path.stat().st_mode),
            )
            for name, path in preserved_paths.items()
        }

    before = digest()

    complete_dir = tmp_path / "complete-preservation"
    complete_dir.mkdir()
    completed, complete_settings, _ = make_executor(
        complete_dir, monkeypatch, FakeConnection(response=claim())
    )
    complete_settings.write_text(json.dumps(settings()))
    assert (await completed.apply(dispatch(), direct_source=ADDRESS))["state"] == "complete"
    assert digest() == before

    review_dir = tmp_path / "review-preservation"
    review_dir.mkdir()
    reviewed, review_settings, _ = make_executor(
        review_dir, monkeypatch,
        FakeConnection(response={**claim(), "unexpected": "forbidden"}),
    )
    review_settings.write_text(json.dumps(settings()))
    assert (await reviewed.apply(dispatch(), direct_source=ADDRESS))["state"] == "needs_review"
    assert digest() == before

    expiry_dir = tmp_path / "expiry-preservation"
    expiry_dir.mkdir()
    clock = Clock()
    expired, expiry_settings, _ = make_executor(
        expiry_dir, monkeypatch,
        FakeConnection(error=TimeoutError("lost response")), clock=clock,
    )
    expiry_settings.write_text(json.dumps(settings()))
    assert (await expired.apply(dispatch(), direct_source=ADDRESS))["state"] == "redemption_attempted"
    clock.value = dispatch()["redemption_expires_at"]
    assert expired.status(REQUEST_ID, direct_source=ADDRESS)["state"] == "never_applied"
    assert digest() == before


@pytest.mark.asyncio
async def test_crash_before_replace_recovers_never_applied_without_write(
    tmp_path, monkeypatch
):
    executor, settings_path, journal_path = make_executor(
        tmp_path, monkeypatch, FakeConnection(response=claim())
    )
    preimage = json.dumps(settings(), sort_keys=True).encode()
    settings_path.write_bytes(preimage)

    def crash(site):
        if site == "replace":
            raise SimulatedCrash("before replace")

    executor._checkpoint = crash
    with pytest.raises(SimulatedCrash, match="before replace"):
        await executor.apply(dispatch(), direct_source=ADDRESS)

    applying = json.loads(journal_path.read_text())
    assert applying["state"] == "applying"
    assert "ticket" not in applying and applying["claim"] == claim()
    assert settings_path.read_bytes() == preimage

    restarted = RepairExecutor(
        journal_path=journal_path,
        settings_path=settings_path,
        clock=Clock(),
        origin_resolver=origin_resolver(),
        connection_factory=factory_for(FakeConnection()),
    )
    restarted._stage_settings_file = lambda *_args: pytest.fail("recovery staged settings")
    restarted._replace_settings_file = lambda *_args: pytest.fail("recovery replaced settings")

    assert restarted.recover() == {
        "request_id": REQUEST_ID,
        "state": "never_applied",
    }
    assert settings_path.read_bytes() == preimage
    terminal = json.loads(journal_path.read_text())
    assert terminal["state"] == "never_applied"
    assert "ticket" not in terminal and "claim" not in terminal


@pytest.mark.asyncio
async def test_recovery_waits_for_settings_writer_before_classifying(
    tmp_path, monkeypatch
):
    executor, settings_path, journal_path = make_executor(
        tmp_path, monkeypatch, FakeConnection(response=claim())
    )
    settings_path.write_text(json.dumps(settings()))

    def crash(site):
        if site == "replace":
            raise SimulatedCrash("before replace")

    executor._checkpoint = crash
    with pytest.raises(SimulatedCrash):
        await executor.apply(dispatch(), direct_source=ADDRESS)

    restarted = RepairExecutor(
        journal_path=journal_path,
        settings_path=settings_path,
        clock=Clock(),
        origin_resolver=origin_resolver(),
        connection_factory=factory_for(FakeConnection()),
    )
    recovered = []
    finished = threading.Event()

    def recover():
        recovered.append(restarted.recover())
        finished.set()

    with settings_writer_lock():
        worker = threading.Thread(target=recover)
        worker.start()
        assert not finished.wait(0.05)
    worker.join(2)

    assert finished.is_set()
    assert recovered == [{"request_id": REQUEST_ID, "state": "never_applied"}]


@pytest.mark.asyncio
async def test_crash_after_verified_replace_recovers_complete_without_write(
    tmp_path, monkeypatch
):
    executor, settings_path, journal_path = make_executor(
        tmp_path, monkeypatch, FakeConnection(response=claim())
    )
    settings_path.write_text(json.dumps(settings()))

    def crash(site):
        if site == "after_read_back":
            raise SimulatedCrash("after verified replace")

    executor._checkpoint = crash
    with pytest.raises(SimulatedCrash, match="after verified replace"):
        await executor.apply(dispatch(), direct_source=ADDRESS)

    staged = settings_path.read_bytes()
    applying = json.loads(journal_path.read_text())
    assert applying["state"] == "applying"
    assert applying["settings_staged_sha256"] == hashlib.sha256(staged).hexdigest()

    restarted = RepairExecutor(
        journal_path=journal_path,
        settings_path=settings_path,
        clock=Clock(),
        origin_resolver=origin_resolver(),
        connection_factory=factory_for(FakeConnection()),
    )
    restarted._stage_settings_file = lambda *_args: pytest.fail("recovery staged settings")
    restarted._replace_settings_file = lambda *_args: pytest.fail("recovery replaced settings")

    recovered = restarted.recover()

    assert recovered["state"] == "complete"
    assert recovered["identity"] == {
        "role": "agent",
        "site_id": "controller-site",
        "site_name": "Controller site",
        "controller_id": TARGET,
    }
    assert settings_path.read_bytes() == staged
    terminal = json.loads(journal_path.read_text())
    assert terminal["state"] == "complete"
    assert "ticket" not in terminal and "claim" not in terminal


@pytest.mark.asyncio
async def test_third_state_before_replace_or_on_restart_is_needs_review_without_overwrite(
    tmp_path, monkeypatch
):
    live = tmp_path / "live"
    live.mkdir()
    executor, settings_path, _journal_path = make_executor(
        live, monkeypatch, FakeConnection(response=claim())
    )
    settings_path.write_text(json.dumps(settings()))
    live_third = b'{"manual":"live-third-state"}\n'

    def edit_before_recheck(site):
        if site == "pre_replace_recheck":
            settings_path.write_bytes(live_third)

    executor._checkpoint = edit_before_recheck
    live_result = await executor.apply(dispatch(), direct_source=ADDRESS)

    assert live_result == {
        "request_id": REQUEST_ID,
        "state": "needs_review",
        "error_code": "settings_preimage_changed",
    }
    assert settings_path.read_bytes() == live_third

    restart = tmp_path / "restart"
    restart.mkdir()
    executor, settings_path, journal_path = make_executor(
        restart, monkeypatch, FakeConnection(response=claim())
    )
    settings_path.write_text(json.dumps(settings()))

    def crash(site):
        if site == "replace":
            raise SimulatedCrash("stranded apply")

    executor._checkpoint = crash
    with pytest.raises(SimulatedCrash):
        await executor.apply(dispatch(), direct_source=ADDRESS)
    restart_third = b'{"manual":"restart-third-state"}\n'
    settings_path.write_bytes(restart_third)

    restarted = RepairExecutor(
        journal_path=journal_path,
        settings_path=settings_path,
        clock=Clock(),
        origin_resolver=origin_resolver(),
        connection_factory=factory_for(FakeConnection()),
    )
    assert restarted.recover() == {
        "request_id": REQUEST_ID,
        "state": "needs_review",
        "error_code": "settings_state_ambiguous",
    }
    assert settings_path.read_bytes() == restart_third


@pytest.mark.asyncio
async def test_concurrent_settings_and_join_writers_return_busy(
    tmp_path, monkeypatch
):
    executor, settings_path, journal_path = make_executor(
        tmp_path, monkeypatch, FakeConnection(response=claim())
    )
    settings_path.write_text(json.dumps(settings()))
    paused = threading.Event()
    resume = threading.Event()

    def pause(site):
        if site == "replace":
            paused.set()
            assert resume.wait(3)

    executor._checkpoint = pause
    result = []
    errors = []

    def repair():
        try:
            result.append(asyncio.run(executor.apply(dispatch(), direct_source=ADDRESS)))
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=repair)
    worker.start()
    assert paused.wait(3)
    applying = json.loads(journal_path.read_text())
    assert applying["state"] == "applying"
    assert "ticket" not in applying and applying["claim"] == claim()

    monkeypatch.delenv("STUDIOHUB_FLEET_TOKEN", raising=False)
    monkeypatch.setattr(hardware_profiles, "hardware_profile", lambda _value: {})
    writers = (
        lambda: control_plane.save_settings({
            "role": "controller",
            "site_id": "other-site",
            "site_name": "Other Site",
            "controller_id": "other-controller",
            "database_mode": "off",
        }),
        lambda: enrollment.configure_new_controller(
            "Other Site", "other-site", "test-profile"
        ),
        lambda: enrollment.configure_joined_agent(
            "http://100.70.0.2:47873", "test-profile", {
                "site_id": "other-site",
                "site_name": "Other Site",
                "controller_id": "other-controller",
                "fleet_token": "other-fleet-token",
            }
        ),
    )
    for writer in writers:
        with pytest.raises(SettingsWriterBusy, match="settings_writer_busy"):
            writer()

    resume.set()
    worker.join(3)

    assert not worker.is_alive() and errors == []
    assert result[0]["state"] == "complete"
    assert json.loads(journal_path.read_text())["state"] == "complete"
    with settings_writer_lock():
        pass


@pytest.mark.parametrize(
    "address",
    ["10.1.2.3", "172.16.0.1", "192.168.1.1", "100.64.0.1", "fd00::1"],
)
def test_explicit_private_and_tailscale_ranges_are_accepted(address):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    resolved = resolve_private_origin(
        "http://controller.test:47873",
        resolver=lambda *args, **kwargs: [(family, 1, 6, "", (address, 47873))],
    )
    assert resolved.address == address


@pytest.mark.parametrize(
    "address",
    [
        "198.18.0.1", "192.0.2.1", "198.51.100.1", "203.0.113.1",
        "192.88.99.1", "8.8.8.8", "127.0.0.1", "169.254.1.1",
        "224.0.0.1", "2001:db8::1", "64:ff9b::1", "::1", "fe80::1",
    ],
)
def test_special_use_documentation_benchmark_and_public_ranges_are_rejected(address):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    with pytest.raises(OriginInvalid, match="callback_url_invalid"):
        resolve_private_origin(
            "http://controller.test:47873",
            resolver=lambda *args, **kwargs: [(family, 1, 6, "", (address, 47873))],
        )


@pytest.mark.asyncio
async def test_resolver_and_transport_share_one_absolute_deadline(tmp_path, monkeypatch):
    from backend import enrollment_repair_executor as executor_module

    monkeypatch.setattr(executor_module, "CALLBACK_TIMEOUT_SECONDS", 0.03, raising=False)
    connection = FakeConnection(response=claim())

    def slow_resolver(value):
        time.sleep(0.12)
        return ResolvedOrigin(ORIGIN, ADDRESS, "controller.test:47873")

    executor, settings_path, journal_path = make_executor(
        tmp_path, monkeypatch, connection, resolver=slow_resolver
    )
    settings_path.write_text(json.dumps(settings()))

    started = time.monotonic()
    result = await executor.apply(dispatch(), direct_source=ADDRESS)
    elapsed = time.monotonic() - started

    assert elapsed < 0.08
    assert result["error_code"] == "transport_unavailable"
    assert connection.calls == []
    assert not journal_path.exists()
    assert TICKET not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("slow_stage", ["headers", "body"])
async def test_pinned_transport_total_deadline_bounds_slow_headers_and_body(
    monkeypatch, slow_stage
):
    from backend import enrollment_repair_transport as transport

    class Socket:
        def settimeout(self, value):
            pass
        def getpeername(self):
            return (ADDRESS, 47873)
        def getsockname(self):
            return ("100.70.0.2", 50000)
        def shutdown(self, how):
            pass
        def close(self):
            pass

    class Response:
        status = 200
        def getheader(self, name):
            return "11" if name == "Content-Length" else None
        def read(self, size):
            if slow_stage == "body":
                time.sleep(0.12)
            return b'{"ok":true}'

    class HTTPConnection:
        def __init__(self, host, port, timeout):
            self.sock = None
        def request(self, method, path, *, body, headers, encode_chunked):
            pass
        def getresponse(self):
            if slow_stage == "headers":
                time.sleep(0.12)
            return Response()
        def close(self):
            self.sock.close()

    monkeypatch.setattr(transport.socket, "create_connection", lambda *args, **kwargs: Socket())
    monkeypatch.setattr(transport.http.client, "HTTPConnection", HTTPConnection)
    connection = PinnedJSONConnection(
        ResolvedOrigin(ORIGIN, ADDRESS, "controller.test:47873")
    )

    started = time.monotonic()
    with pytest.raises(PinnedTransportError, match="transport_timeout") as error:
        await connection.request_json(
            "POST", "/redeem", headers={"X-Hub-Token": FLEET_TOKEN},
            body={"ticket": TICKET}, timeout=0.03,
        )
    elapsed = time.monotonic() - started

    assert elapsed < 0.08
    assert FLEET_TOKEN not in repr(error.value)
    assert TICKET not in repr(error.value)
