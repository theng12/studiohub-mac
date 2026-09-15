import pytest


def test_local_recovery_requires_full_image_stall_and_resets_on_meaningful_stage(tmp_path):
    from backend.automatic_recovery import AutomaticRecoveryState

    state = AutomaticRecoveryState(tmp_path / "recovery.json")
    image = {
        "id": "image-job-1", "state": "running", "model": "local/image",
        "started_at": 0.0, "progress": 0.05, "current_step": 1,
        "total_steps": 20,
    }

    first = state.observe("image", "image", image, now=100.0)
    early = None
    for observed_at in [*range(400, 3700, 300), 3699]:
        early = state.observe("image", "image", image, now=float(observed_at))
    image["current_step"] = 2
    advanced = state.observe("image", "image", image, now=3700.0)
    eligible = None
    for observed_at in range(4000, 7301, 300):
        eligible = state.observe("image", "image", image, now=float(observed_at))

    assert first["eligible"] is False and early["eligible"] is False
    assert advanced["eligible"] is False
    assert eligible["eligible"] is True
    assert eligible["stall_after_s"] == 3600.0
    assert eligible["last_meaningful_at"] == 3700.0


def test_local_recovery_voice_chunk_advance_is_progress_even_if_percent_is_unchanged(tmp_path):
    from backend.automatic_recovery import AutomaticRecoveryState

    state = AutomaticRecoveryState(tmp_path / "recovery.json")
    voice = {
        "id": "voice-job-1", "state": "running", "model": "local/voice",
        "started_at": 0.0, "progress": 0.4, "chunk_index": 1,
        "chunk_total": 8,
    }
    state.observe("voice", "voice", voice, now=100.0)
    for observed_at in range(400, 7300, 300):
        state.observe("voice", "voice", voice, now=float(observed_at))
    voice["chunk_index"] = 2

    result = state.observe("voice", "voice", voice, now=7300.0)

    assert result["eligible"] is False
    assert result["last_meaningful_at"] == 7300.0


def test_voice_stall_window_stays_conservative_without_full_workload_identity(tmp_path):
    from backend.automatic_recovery import AutomaticRecoveryState

    state = AutomaticRecoveryState(tmp_path / "recovery.json")
    voice = {
        "id": "voice-job-1", "state": "running", "model": "local/voice",
        "started_at": 0.0, "progress": 0.4, "chunk_index": 1,
        "chunk_total": 20,
    }
    result = state.observe("voice", "voice", voice, now=100.0)

    assert result["stall_after_s"] == 7200.0


@pytest.mark.parametrize("payload", [
    "[]", "{broken", '{"schema_version":1}',
    '{"schema_version":1,"jobs":{"voice":"bad"}}',
])
def test_corrupt_recovery_state_fails_closed_across_future_state_writes(tmp_path, payload):
    from backend.automatic_recovery import AutomaticRecoveryState

    path = tmp_path / "recovery.json"
    path.write_text(payload)
    state = AutomaticRecoveryState(path)
    job = {
        "id": "voice-job-1", "state": "running", "model": "local/voice",
        "started_at": 0.0, "progress": 0.1, "chunk_index": 1, "chunk_total": 2,
    }

    result = None
    for observed_at in range(100, 10_001, 300):
        result = state.observe("voice", "voice", job, now=float(observed_at))

    assert result["eligible"] is False
    assert AutomaticRecoveryState(path).snapshot()["state_healthy"] is False


def test_observation_does_not_erase_manual_recovery_phase(tmp_path):
    from backend.automatic_recovery import AutomaticRecoveryState

    state = AutomaticRecoveryState(tmp_path / "recovery.json")
    job = {
        "id": "image-job-1", "state": "running", "model": "local/image",
        "started_at": 0.0, "progress": 0.1, "current_step": 1, "total_steps": 20,
    }
    state.observe("image", "image", job, now=100.0)
    state.mark("image", "image-job-1", "manual_action_required", "managed service missing", now=200.0)

    observed = state.observe("image", "image", job, now=10_000.0)

    row = state.snapshot(now=300.0)["jobs"][0]
    assert row["phase"] == "manual_action_required"
    assert row["reason"] == "managed service missing"
    assert observed["eligible"] is False


def test_hub_restart_or_long_poll_gap_resets_the_observed_stall_window(tmp_path):
    from backend.automatic_recovery import AutomaticRecoveryState

    path = tmp_path / "recovery.json"
    job = {
        "id": "image-job-1", "state": "running", "model": "local/image",
        "started_at": 0.0, "progress": 0.1, "current_step": 1, "total_steps": 20,
    }
    state = AutomaticRecoveryState(path)
    state.observe("image", "image", job, now=100.0)

    after_gap = state.observe("image", "image", job, now=10_000.0)
    after_restart = AutomaticRecoveryState(path).observe(
        "image", "image", job, now=20_000.0,
    )

    assert after_gap["eligible"] is False
    assert after_gap["last_meaningful_at"] == 10_000.0
    assert after_restart["eligible"] is False
    assert after_restart["last_meaningful_at"] == 20_000.0


def test_hub_restart_turns_interrupted_recovery_into_visible_manual_state(tmp_path):
    from backend.automatic_recovery import AutomaticRecoveryState

    path = tmp_path / "recovery.json"
    state = AutomaticRecoveryState(path)
    state.mark("voice", "voice-job-1", "cancel_requested", "cancelling", now=100.0)

    row = AutomaticRecoveryState(path).snapshot(now=200.0)["jobs"][0]

    assert row["phase"] == "manual_action_required"
    assert "restarted during local recovery" in row["reason"]


def test_unchanged_percent_without_meaningful_stage_evidence_never_qualifies(tmp_path):
    from backend.automatic_recovery import AutomaticRecoveryState

    state = AutomaticRecoveryState(tmp_path / "recovery.json")
    job = {
        "id": "voice-job-1", "state": "running", "model": "local/voice",
        "started_at": 0.0, "progress": 0.4,
        "chunk_index": None, "chunk_total": None,
    }
    result = None
    for observed_at in range(100, 10_001, 300):
        result = state.observe("voice", "voice", job, now=float(observed_at))

    assert result["eligible"] is False
    assert result["stage_evidence"] is False


@pytest.mark.asyncio
async def test_automatic_reconcile_adopts_exact_terminal_voice_job(reset, monkeypatch):
    from backend import broker, main

    submitted = broker.submit_batch({
        "modality": "voice", "model": "local/voice",
        "items": [{"text": "recover"}],
    })
    batch = broker.batches[submitted["batch_id"]]
    item = batch["items"][0]
    item.update(state="uncertain", studio="voice@remote", studio_job_id="voice-job-1")
    main.monitor.registry = [{
        "id": "voice@remote", "modality": "voice", "machine": "remote",
        "host": "100.64.0.2", "port": 47870,
    }]
    calls = []

    async def reconcile(_client, _batch, target, _studio):
        calls.append(target["studio_job_id"])
        target["state"] = "done"
        return "done"

    monkeypatch.setattr(main, "_reconcile_voice_recovery_job", reconcile)

    await main._automatic_reconcile_batches(object())

    assert calls == ["voice-job-1"]
    assert item["state"] == "done"


@pytest.mark.asyncio
@pytest.mark.parametrize("download_state,active_operation,expected", [
    ("done", "speech", True),
    ("running", "speech", False),
    ("done", "transcription", False),
])
async def test_exact_cancel_requires_fresh_worker_activity_and_no_download(
    reset, download_state, active_operation, expected,
):
    from backend import main

    studio = {
        "id": "voice", "modality": "voice", "machine": "local",
        "host": "127.0.0.1", "port": 47870,
    }

    class Response:
        status_code = 200
        def __init__(self, payload): self._payload = payload
        def json(self): return self._payload

    class Client:
        async def get(self, url, **_kwargs):
            if url.endswith("/api/downloads"):
                return Response({"jobs": [{"id": "download-1", "state": download_state}]})
            return Response({
                "schema": "kh-studio.activity.v1", "studio": "voice", "observed_at": 100.0,
                "active": {
                    "id": "voice-job-1", "state": "running", "model": "local/voice",
                    "operation": active_operation, "source": "direct", "origin": "api",
                    "progress": 0.4, "started_at": 1.0, "updated_at": 100.0,
                },
                "latest": None,
            })

    result = await main._worker_allows_exact_cancel(
        Client(), studio, "voice-job-1",
    )

    assert result is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["missing", "uncertain", "unknown"])
async def test_missing_or_uncertain_exact_job_is_not_a_confirmed_exit(
    reset, monkeypatch, state,
):
    from backend import main

    async def read(*_args):
        return state, None

    monkeypatch.setattr(main, "_read_exact_generation_job", read)

    confirmed = await main._wait_for_exact_job_exit(
        object(), {"id": "voice"}, "voice-job-1", 0,
    )

    assert confirmed is False


@pytest.mark.asyncio
async def test_local_hang_recovery_cancels_exact_job_and_restores_drain_after_terminal(
    reset, monkeypatch,
):
    from backend import broker, main

    studio = {
        "id": "voice", "modality": "voice", "machine": "local",
        "host": "127.0.0.1", "port": 47870, "app": "voicestudio-mac.git",
    }
    active = {
        "id": "voice-job-1", "state": "running", "model": "local/voice",
        "origin": "api", "started_at": 1.0, "progress": 0.4,
        "chunk_index": 2, "chunk_total": 8,
    }
    exact_states = iter(["running", "running", "cancelled"])
    events = []

    class Response:
        status_code = 200
        def __init__(self, payload): self._payload = payload
        def json(self): return self._payload

    class Client:
        async def get(self, url, **_kwargs):
            if url.endswith("/api/generate/jobs"):
                return Response({"jobs": [{"id": "voice-job-1", "state": "running"}]})
            state = next(exact_states)
            events.append(("get", state))
            return Response({"job": {**active, "state": state}})
        async def delete(self, _url, **_kwargs):
            events.append(("delete", "voice-job-1"))
            return Response({"job": {**active, "state": "cancel_requested"}})

    monkeypatch.setattr(
        main.automatic_recovery, "observe",
        lambda *_args, **_kwargs: {"eligible": True, "stall_after_s": 900.0},
    )
    monkeypatch.setattr(main.automatic_recovery, "mark", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "_worker_allows_exact_cancel", lambda *_args: _true())
    monkeypatch.setattr(main, "AUTO_RECOVERY_CANCEL_GRACE_S", 0)
    monkeypatch.setattr(main.asyncio, "sleep", lambda _seconds: _no_wait())

    await main._automatic_local_recovery(studio, active, Client(), now=50_000.0)

    assert events == [
        ("get", "running"), ("get", "running"),
        ("delete", "voice-job-1"), ("get", "cancelled"),
    ]
    assert broker.in_maintenance("voice") is False


@pytest.mark.asyncio
async def test_unconfirmed_cancel_stays_manual_and_keeps_the_studio_drained(
    reset, monkeypatch,
):
    from backend import broker, main

    studio = {"id": "image", "modality": "image", "machine": "local",
              "host": "127.0.0.1", "port": 47868}
    active = {"id": "image-job-1", "state": "running", "model": "local/image"}
    exact_reads = iter([("running", dict(active)), ("running", dict(active))])
    phases = []

    async def read(*_args):
        return next(exact_reads)

    class Client:
        async def delete(self, *_args, **_kwargs):
            return type("Response", (), {"status_code": 200})()

    monkeypatch.setattr(main, "_read_exact_generation_job", read)
    monkeypatch.setattr(main, "_other_active_generation_jobs", lambda *_args: _false())
    monkeypatch.setattr(main, "_worker_allows_exact_cancel", lambda *_args: _true())
    monkeypatch.setattr(main, "_wait_for_exact_job_exit", lambda *_args: _false())
    monkeypatch.setattr(main.automatic_recovery, "observe", lambda *_args, **_kwargs: {"eligible": True})
    monkeypatch.setattr(
        main.automatic_recovery, "mark",
        lambda _studio, _job, phase, reason, **_kwargs: phases.append((phase, reason)),
    )
    monkeypatch.setattr(main, "AUTO_RECOVERY_CANCEL_GRACE_S", 0)

    result = await main._automatic_local_recovery(studio, active, Client(), now=50_000.0)

    assert result is False
    assert phases[-1][0] == "manual_action_required"
    assert "remains drained" in phases[-1][1]
    assert broker.in_maintenance("image") is True
    broker.set_maintenance("image", False)


@pytest.mark.asyncio
async def test_reconciliation_cursor_reaches_uncertain_jobs_after_first_hundred(reset, monkeypatch):
    from backend import broker, main

    main._automatic_reconcile_cursor = 0
    main.monitor.registry = [{"id": "voice", "modality": "voice"}]
    for index in range(150):
        broker.batches[str(index)] = {
            "id": str(index), "modality": "voice",
            "items": [{"index": 0, "state": "uncertain", "studio": "voice",
                       "studio_job_id": f"job-{index}"}],
        }
    calls = []

    async def reconcile(_client, _batch, item, _studio):
        calls.append(item["studio_job_id"])
        return "active"

    monkeypatch.setattr(main, "_reconcile_voice_recovery_job", reconcile)

    for _ in range(150):
        await main._automatic_reconcile_batches(object())

    assert calls[:2] == ["job-0", "job-1"]
    assert calls[100:102] == ["job-100", "job-101"]
    assert "job-149" in calls


@pytest.mark.asyncio
async def test_slow_voice_artifact_reconciliation_does_not_starve_local_watchdog(
    reset, monkeypatch,
):
    import asyncio
    from backend import main

    reconciliation_started = asyncio.Event()
    release_reconciliation = asyncio.Event()
    local_checked = asyncio.Event()

    async def slow_reconcile(_client):
        reconciliation_started.set()
        await release_reconciliation.wait()

    async def local_pass(_client):
        local_checked.set()

    class ClientContext:
        async def __aenter__(self): return object()
        async def __aexit__(self, *_args): return None

    monkeypatch.setattr(main, "_automatic_reconcile_batches", slow_reconcile)
    monkeypatch.setattr(main, "_automatic_local_recovery_pass", local_pass)
    monkeypatch.setattr(main.httpx, "AsyncClient", ClientContext)

    reconcile_task = asyncio.create_task(main._automatic_reconcile_loop())
    local_task = asyncio.create_task(main._automatic_local_recovery_loop())
    try:
        await asyncio.wait_for(reconciliation_started.wait(), timeout=0.5)
        await asyncio.wait_for(local_checked.wait(), timeout=0.5)
        assert reconcile_task.done() is False
    finally:
        reconcile_task.cancel()
        local_task.cancel()
        await asyncio.gather(reconcile_task, local_task, return_exceptions=True)


def test_recovery_status_and_model_recheck_routes(authed, monkeypatch):
    from backend import broker, main

    models = [{"studio": "voice", "model": "local/voice", "blocked": True}]
    auto = {"state": "watching", "reason": "safe"}
    calls = []

    async def recheck(studio, model):
        calls.append((studio, model))
        return {"ok": True, "detail": "ready"}

    monkeypatch.setattr(broker, "model_protection_snapshot", lambda: models)
    monkeypatch.setattr(broker, "recheck_model_protection", recheck)
    monkeypatch.setattr(main.automatic_recovery, "snapshot", lambda: auto)

    status = authed.get("/api/hub/recovery")
    result = authed.post(
        "/api/hub/recovery/models/recheck",
        json={"studio": "voice", "model": "local/voice"},
    )

    assert status.json() == {"models": models, "auto": auto}
    assert result.status_code == 200 and result.json()["ok"] is True
    assert calls == [("voice", "local/voice")]


def test_model_recheck_conflict_is_409(authed, monkeypatch):
    from backend import broker

    async def blocked(_studio, _model):
        raise ValueError("readiness is still blocked")

    monkeypatch.setattr(broker, "recheck_model_protection", blocked)

    response = authed.post(
        "/api/hub/recovery/models/recheck",
        json={"studio": "voice", "model": "local/voice"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "readiness is still blocked"


async def _no_wait():
    return None


async def _false():
    return False


async def _true():
    return True
