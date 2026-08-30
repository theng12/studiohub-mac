"""Contract and state tests for the compact fleet-activity ledger."""

from backend import ledger


def _studio(machine="mac-a", modality="image"):
    return {
        "id": f"{modality}@{machine}", "machine": machine,
        "modality": modality, "host": "127.0.0.1", "port": 47868,
    }


def _status(snapshot=None, *, state="up", support="available"):
    value = {"status": state, "last_checked": 100.0}
    if snapshot is not None:
        value["activity"] = snapshot
    value["activity_support"] = support
    return value


def _snapshot(*, active=None, latest=None, studio="image"):
    observed_at = max(
        [100.0, *[
            value for job in (active, latest) if job
            for value in (job.get("updated_at"), job.get("finished_at"))
            if value is not None
        ]]
    )
    return {
        "schema": "kh-studio.activity.v1", "observed_at": observed_at,
        "studio": studio, "active": active, "latest": latest,
    }


def _job(job_id="job-1", *, state="running", model="org/model", source="direct",
         progress=0.5, started_at=90.0, finished_at=None, runtime_s=None):
    value = {
        "id": job_id, "state": state, "model": model, "source": source,
        "progress": progress, "created_at": 80.0, "started_at": started_at,
        "updated_at": 100.0,
    }
    if finished_at is not None:
        value["finished_at"] = finished_at
    if runtime_s is not None:
        value["runtime_s"] = runtime_s
    return value


def test_activity_contract_ignores_malformed_snapshot_and_job_values(reset):
    from backend import activity

    assert activity.validate_snapshot({"schema": "wrong"}) is None
    invalid = _snapshot(active=_job(state="running", progress=1.2))
    assert activity.validate_snapshot(invalid) is None
    invalid = _snapshot(active=_job(state="invented"))
    assert activity.validate_snapshot(invalid) is None
    invalid = _snapshot(active=_job(model="x" * 600))
    assert activity.validate_snapshot(invalid) is None


def test_activity_contract_allowlists_bounded_origin_scalars(reset):
    from backend import activity

    legacy = _snapshot(active=_job())
    valid = _snapshot(active={
        **_job(), "origin": "local_ui", "origin_device": "  Mac Mini  ",
    })
    assert activity.validate_snapshot(legacy)["active"]["origin"] == "unknown"
    validated = activity.validate_snapshot(valid)["active"]
    assert validated["origin"] == "local_ui"
    assert validated["origin_device"] == "Mac Mini"
    assert activity.validate_snapshot({
        **valid, "active": {**valid["active"], "origin": "spoof"},
    }) is None
    assert activity.validate_snapshot({
        **valid, "active": {**valid["active"], "origin_device": "x" * 161},
    }) is None

    projected = activity.validate_snapshot(_snapshot(active={
        **_job(), "prompt": "private", "path": "/private", "handle": "opaque",
    }))["active"]
    assert not {"prompt", "path", "handle"} & projected.keys()


def test_broker_origin_overrides_worker_claim_and_persisted_ownership(reset, monkeypatch):
    from backend import activity, control_plane

    monkeypatch.setattr(control_plane, "load_settings", lambda: {"site_name": "PPS"})
    studio = _studio()
    statuses = {studio["id"]: _status(_snapshot(active={
        **_job("broker-job"), "origin": "api", "origin_device": "worker claim",
    }))}
    batches = {"batch": {"model": "broker/model", "items": [{
        "studio": studio["id"], "studio_job_id": "broker-job", "state": "running",
    }]}}
    row = activity._observed_jobs([studio], statuses, batches, 100.0)[0]
    assert row["origin"] == "hub"
    assert row["origin_device"] == "Studio Hub KH · PPS"
    assert row["source"] == "job"

    non_owned = activity._observed_jobs([studio], {
        studio["id"]: _status(_snapshot(active={**_job("api-job"), "origin": "api"})),
    }, {}, 100.0)[0]
    assert non_owned["origin"] == "api"
    assert non_owned.get("origin_device") is None

    ledger.record_activity_ownership(machine="mac-a", studio=studio["id"],
                                     job_id="owned-before-poll", model="broker/model",
                                     observed_at=100.0)
    owned_before_poll = activity._observed_jobs([studio], {
        studio["id"]: _status(_snapshot(active={
            **_job("owned-before-poll"), "origin": "api",
        })),
    }, {}, 101.0)[0]
    assert owned_before_poll["origin"] == "hub"
    assert owned_before_poll["origin_device"] == "Studio Hub KH · PPS"


def test_activity_observation_is_idempotent_and_broker_owns_matching_job(reset):
    from backend import activity

    studio = _studio()
    statuses = {studio["id"]: _status(_snapshot(active=_job()))}
    batches = {
        "batch-1": {"model": "broker/model", "items": [{
            "studio": studio["id"], "studio_job_id": "job-1", "state": "running",
        }]},
    }
    activity.observe_poll([studio], statuses, batches, now=100.0)
    activity.observe_poll([studio], statuses, batches, now=105.0)
    rows = ledger.activity_events(machine="mac-a")
    assert len(rows) == 1
    assert rows[0]["source"] == "job"
    assert rows[0]["model"] == "broker/model"

    # Batch cleanup can happen just before the worker's terminal snapshot. Its
    # previously recorded studio_job_id still owns that terminal evidence.
    statuses[studio["id"]]["activity"] = _snapshot(latest=_job(
        state="done", finished_at=110.0, runtime_s=20.0,
    ))
    activity.observe_poll([studio], statuses, {}, now=110.0)
    done = next(row for row in ledger.activity_events(machine="mac-a") if row["state"] == "done")
    assert done["source"] == "job"


def test_activity_observation_uses_one_ledger_connection_per_poll(reset, monkeypatch):
    """A five-second poll must not reopen and migrate SQLite for each row."""
    from backend import activity

    calls = 0
    original = ledger._conn

    def counted_connection():
        nonlocal calls
        calls += 1
        return original()

    monkeypatch.setattr(ledger, "_conn", counted_connection)
    studio = _studio()
    activity.observe_poll(
        [studio], {studio["id"]: _status(_snapshot(active=_job()))}, {}, now=100.0,
    )

    assert calls == 1


def test_machine_state_distinguishes_recent_completion_from_long_idle(reset):
    from backend import activity

    studio = _studio()
    ledger.record_activity_event(
        machine="mac-a", studio=studio["id"], job_id="one", state="done",
        model="org/model", source="direct", started_at=100.0, finished_at=110.0,
        runtime_s=10.0, observed_at=110.0,
    )
    statuses = {studio["id"]: _status(_snapshot(latest=_job(
        "one", state="done", finished_at=110.0, runtime_s=10.0,
    )))}
    recent = activity.fleet_snapshot([studio], statuses, {}, since_s=0.0, now=110.0 + 14 * 60)
    old = activity.fleet_snapshot([studio], statuses, {}, since_s=0.0, now=110.0 + 2 * 3600)
    assert recent["machines"][0]["state"] == "just_finished"
    assert old["machines"][0]["state"] == "long_idle"


def test_machine_state_precedence_and_success_clears_terminal_attention(reset):
    from backend import activity

    studio = _studio()
    statuses = {studio["id"]: _status(_snapshot(active=_job()))}
    ledger.record_activity_event(machine="mac-a", studio=studio["id"], job_id="bad",
                                 state="error", model="org/model", source="direct",
                                 finished_at=90.0, observed_at=90.0)
    activity.observe_poll([studio], statuses, {}, now=100.0)
    assert activity.fleet_snapshot([studio], statuses, {}, since_s=0.0, now=100.0)["machines"][0]["state"] == "needs_attention"

    ledger.record_activity_event(machine="mac-a", studio=studio["id"], job_id="good",
                                 state="done", model="org/model", source="direct",
                                 started_at=95.0, finished_at=101.0, runtime_s=6.0,
                                 observed_at=101.0)
    statuses[studio["id"]]["activity"] = _snapshot(latest=_job(
        "good", state="done", finished_at=101.0, runtime_s=6.0,
    ))
    activity.observe_poll([studio], statuses, {}, now=101.0)
    assert activity.fleet_snapshot([studio], statuses, {}, since_s=0.0, now=101.0)["machines"][0]["state"] == "just_finished"

    statuses[studio["id"]]["status"] = "down"
    assert activity.fleet_snapshot([studio], statuses, {}, since_s=0.0, now=101.0)["machines"][0]["state"] == "offline"


def test_legacy_and_temporarily_failed_reporter_are_limitations_not_health_failures(reset):
    from backend import activity

    studio = _studio()
    statuses = {studio["id"]: _status(None, support="unavailable")}
    machine = activity.fleet_snapshot([studio], statuses, {}, since_s=0.0, now=100.0)["machines"][0]
    assert machine["state"] == "unknown"
    assert machine["limitation"] == "Direct activity unavailable"

    statuses[studio["id"]]["activity_support"] = "error"
    statuses[studio["id"]]["activity_error"] = "reporter request failed"
    machine = activity.fleet_snapshot([studio], statuses, {}, since_s=0.0, now=100.0)["machines"][0]
    assert machine["state"] == "unknown"
    assert machine["limitation"] == "Activity reporter temporarily unavailable"


def test_activity_prunes_old_rows_and_utilization_uses_only_reachable_time(reset):
    from backend import activity

    studio = _studio()
    old = 100.0 - 31 * 86400
    ledger.record_activity_event(machine="mac-a", studio=studio["id"], job_id="old",
                                 state="done", model="org/model", source="direct",
                                 finished_at=old, observed_at=old)
    ledger.record_machine_state(machine="mac-a", reachable=True, working=True, observed_at=0.0)
    ledger.record_machine_state(machine="mac-a", reachable=True, working=False, observed_at=10.0)
    ledger.record_machine_state(machine="mac-a", reachable=False, working=False, observed_at=20.0)
    activity.observe_poll([studio], {studio["id"]: _status()}, {}, now=100.0)
    assert ledger.activity_events(machine="mac-a") == []
    machine = activity.fleet_snapshot([studio], {studio["id"]: _status()}, {}, since_s=0.0, now=30.0)["machines"][0]
    assert machine["utilization"] == {"ratio": 0.5, "evidence": "complete"}


def test_utilization_counts_the_last_reachable_interval_through_now(reset):
    from backend import activity

    studio = _studio("mac-b")
    ledger.record_machine_state(machine="mac-b", reachable=True, working=True, observed_at=0.0)
    ledger.record_machine_state(machine="mac-b", reachable=True, working=False, observed_at=10.0)
    machine = activity.fleet_snapshot(
        [studio], {studio["id"]: _status()}, {}, since_s=0.0, now=20.0,
    )["machines"][0]
    assert machine["utilization"] == {"ratio": 0.5, "evidence": "complete"}


def test_comparable_medians_require_three_samples_on_two_machines(reset):
    from backend import activity

    studios = [_studio("mac-a"), _studio("mac-b")]
    statuses = {studio["id"]: _status() for studio in studios}
    for machine, values in {"mac-a": [10, 11, 12], "mac-b": [20, 21]}.items():
        for index, runtime in enumerate(values):
            ledger.record_activity_event(
                machine=machine, studio=f"image@{machine}", job_id=f"{machine}-{index}",
                state="done", model="org/model", source="direct", started_at=50 + index,
                finished_at=50 + index + runtime, runtime_s=runtime, observed_at=90 + index,
            )
    snapshot = activity.fleet_snapshot(studios, statuses, {}, since_s=0.0, now=100.0)
    assert snapshot["machines"][0]["median_runtime_s"] == 11.0
    assert snapshot["machines"][0]["relative_performance"] is None

    ledger.record_activity_event(machine="mac-b", studio="image@mac-b", job_id="mac-b-3",
                                 state="done", model="org/model", source="direct",
                                 started_at=55.0, finished_at=77.0, runtime_s=22.0,
                                 observed_at=95.0)
    snapshot = activity.fleet_snapshot(studios, statuses, {}, since_s=0.0, now=100.0)
    by_machine = {row["machine"]: row for row in snapshot["machines"]}
    assert by_machine["mac-a"]["relative_performance"]["percent_faster"] > 0
    assert by_machine["mac-b"]["relative_performance"]["percent_faster"] < 0


def test_retained_active_snapshot_is_not_live_after_reporter_failure_or_offline(reset):
    from backend import activity

    studio = _studio()
    statuses = {studio["id"]: _status(_snapshot(active=_job()), support="available")}
    activity.observe_poll([studio], statuses, {}, now=100.0)
    statuses[studio["id"]]["activity_support"] = "error"
    statuses[studio["id"]]["activity_error"] = "reporter request failed"
    assert activity.fleet_snapshot([studio], statuses, {}, since_s=0.0, now=105.0)["machines"][0]["state"] != "working"
    statuses[studio["id"]]["status"] = "down"
    assert activity.fleet_snapshot([studio], statuses, {}, since_s=0.0, now=110.0)["machines"][0]["state"] == "offline"


def test_terminal_receipt_is_immutable_and_older_evidence_cannot_regress_it(reset):
    from backend import activity

    studio = _studio()
    snapshot = _snapshot(latest=_job("done", state="done", progress=1.0,
                                     finished_at=100.0, runtime_s=10.0))
    statuses = {studio["id"]: _status(snapshot)}
    activity.observe_poll([studio], statuses, {}, now=100.0)
    for now in (101.0, 100.0 + 31 * 86400):
        activity.observe_poll([studio], statuses, {}, now=now)
    assert ledger.activity_events(machine="mac-a") == []

    ledger.record_activity_event(machine="mac-a", studio=studio["id"], job_id="order",
                                 state="running", model="new/model", source="direct",
                                 progress=0.9, activity_received_at=200.0,
                                 reported_at=200.0, observed_at=200.0)
    ledger.record_activity_event(machine="mac-a", studio=studio["id"], job_id="order",
                                 state="running", model="old/model", source="direct",
                                 progress=0.1, activity_received_at=100.0,
                                 reported_at=100.0, observed_at=210.0)
    row = next(row for row in ledger.activity_events(machine="mac-a") if row["job_id"] == "order")
    assert row["observed_at"] == 200.0
    assert row["reported_at"] == 200.0
    assert row["model"] == "new/model" and row["progress"] == 0.9


def test_operational_state_onsets_are_durable_for_empty_cancelled_offline_and_attention(reset):
    from backend import activity

    studio = _studio()
    statuses = {studio["id"]: _status(_snapshot(), support="available")}
    activity.observe_poll([studio], statuses, {}, now=100.0)
    assert activity.fleet_snapshot([studio], statuses, {}, since_s=0.0, now=110.0)["machines"][0]["state"] == "ready"

    statuses[studio["id"]]["activity"] = _snapshot(latest=_job(
        "cancel", state="cancelled", finished_at=120.0,
    ))
    activity.observe_poll([studio], statuses, {}, now=120.0)
    assert activity.fleet_snapshot([studio], statuses, {}, since_s=0.0, now=121.0)["machines"][0]["state"] == "ready"
    activity.observe_poll([studio], statuses, {}, now=120.0 + 2 * 3600)
    assert activity.fleet_snapshot([studio], statuses, {}, since_s=0.0, now=120.0 + 2 * 3600)["machines"][0]["state"] == "long_idle"

    statuses[studio["id"]]["status"] = "down"
    activity.observe_poll([studio], statuses, {}, now=9000.0)
    assert activity.fleet_snapshot([studio], statuses, {}, since_s=0.0, now=9010.0)["machines"][0]["state_duration_s"] == 10.0
    activity.observe_poll([studio], statuses, {}, now=9020.0)
    assert activity.fleet_snapshot([studio], statuses, {}, since_s=0.0, now=9030.0)["machines"][0]["state_duration_s"] == 30.0

    statuses[studio["id"]]["status"] = "degraded"
    activity.observe_poll([studio], statuses, {}, now=9040.0)
    assert activity.fleet_snapshot([studio], statuses, {}, since_s=0.0, now=9050.0)["machines"][0]["state"] == "needs_attention"
    activity.observe_poll([studio], statuses, {}, now=9060.0)
    assert activity.fleet_snapshot([studio], statuses, {}, since_s=0.0, now=9070.0)["machines"][0]["state_duration_s"] == 30.0


def test_prune_retains_predecessor_and_counts_degraded_as_reachable(reset):
    from backend import activity

    studio = _studio()
    ledger.record_machine_state(machine="mac-a", reachable=True, working=True,
                                state="working", observed_at=0.0)
    ledger.prune_activity(100.0)
    transition = ledger.machine_state_transitions("mac-a")[0]
    assert transition["observed_at"] == 0.0
    assert transition["state_since"] == 0.0
    machine = activity.fleet_snapshot(
        [studio], {studio["id"]: _status(state="degraded", support="available")},
        {}, since_s=100.0, now=110.0,
    )["machines"][0]
    assert machine["state"] == "needs_attention"
    assert machine["utilization"] == {"ratio": 1.0, "evidence": "complete"}


def test_snapshot_roles_modality_and_timestamp_order_are_enforced(reset):
    from backend import activity

    assert activity.validate_snapshot(_snapshot(active=_job(state="done", finished_at=100.0))) is None
    assert activity.validate_snapshot(_snapshot(latest=_job(state="running"))) is None
    assert activity.validate_snapshot(_snapshot(latest=_job(
        state="done", started_at=100.0, finished_at=90.0, runtime_s=1.0,
    ))) is None
    assert activity.validate_snapshot(_snapshot(studio="image"), expected_studio="voice") is None


def test_broker_only_activity_uses_injected_now_and_persisted_ownership_survives_cleanup(reset):
    from backend import activity

    studio = _studio()
    batches = {"batch": {"model": "broker/model", "items": [{
        "studio": studio["id"], "studio_job_id": "broker-job", "state": "running",
    }]}}
    activity.observe_poll([studio], {studio["id"]: _status()}, batches, now=123.0)
    row = next(row for row in ledger.activity_events(machine="mac-a") if row["job_id"] == "broker-job")
    assert row["observed_at"] == 123.0

    ledger.record_activity_ownership(machine="mac-a", studio=studio["id"],
                                     job_id="cleaned", model="broker/model", observed_at=130.0)
    statuses = {studio["id"]: _status(_snapshot(latest=_job(
        "cleaned", state="done", finished_at=130.0, runtime_s=2.0,
    )))}
    activity.observe_poll([studio], statuses, {}, now=131.0)
    done = next(row for row in ledger.activity_events(machine="mac-a")
                if row["job_id"] == "cleaned" and row["state"] == "done")
    assert done["source"] == "job"


def test_unknown_or_mixed_reporter_support_is_visible_partial_evidence(reset):
    from backend import activity

    studios = [_studio("mac-a", "image"), _studio("mac-a", "voice")]
    statuses = {
        studios[0]["id"]: _status(_snapshot(studio="image"), support="available"),
        studios[1]["id"]: _status(_snapshot(studio="voice"), support=None),
    }
    mixed = activity.fleet_snapshot(studios, statuses, {}, since_s=0.0, now=100.0)["machines"][0]
    assert mixed["limitation"] == "Direct activity partially unavailable"
    assert mixed["utilization"]["evidence"] == "partial"
    statuses[studios[0]["id"]]["activity_support"] = None
    unknown = activity.fleet_snapshot(studios, statuses, {}, since_s=0.0, now=100.0)["machines"][0]
    assert unknown["limitation"] == "Activity evidence pending"


def test_state_duration_keeps_a_zero_epoch_transition(reset):
    from backend import activity

    studio = _studio()
    ledger.record_machine_state(machine="mac-a", reachable=True, working=True,
                                state="working", observed_at=0.0)
    batches = {"batch": {"model": "broker/model", "items": [{
        "studio": studio["id"], "studio_job_id": "current", "state": "running",
    }]}}
    machine = activity.fleet_snapshot(
        [studio], {studio["id"]: _status()}, batches, since_s=0.0, now=10.0,
    )["machines"][0]
    assert machine["state"] == "working"
    assert machine["state_duration_s"] == 10.0


def test_controller_receipt_handles_clock_skew_rollback_and_old_terminal(reset):
    from backend import activity

    studio = _studio()
    ahead = _snapshot(active=_job("ahead", progress=0.2))
    ahead["active"].update(created_at=105.0, started_at=105.0, updated_at=110.0)
    ahead["observed_at"] = 110.0
    ahead_status = {studio["id"]: _status(ahead)}
    ahead_status[studio["id"]]["activity_received_at"] = 105.0
    assert [row["id"] for row in activity._observed_jobs(
        [studio], ahead_status, {}, 105.0
    )] == ["ahead"]

    active = _snapshot(active=_job("live", progress=0.4))
    active["active"].update(created_at=0.0, started_at=0.0, updated_at=1.0)
    active["observed_at"] = 1.0  # Worker is far behind, receipt is current.
    statuses = {studio["id"]: _status(active)}
    statuses[studio["id"]]["activity_received_at"] = 100.0
    activity.observe_poll([studio], statuses, {}, now=105.0)
    assert activity.fleet_snapshot([studio], statuses, {}, since_s=0.0, now=105.0)["machines"][0]["state"] == "working"

    rollback = _snapshot(active=_job("live", progress=0.9))
    rollback["active"].update(created_at=0.0, started_at=0.0, updated_at=2.0)
    rollback["observed_at"] = 2.0
    statuses[studio["id"]].update(activity=rollback, activity_received_at=106.0)
    activity.observe_poll([studio], statuses, {}, now=106.0)
    live = next(row for row in ledger.activity_events(machine="mac-a") if row["job_id"] == "live")
    assert live["progress"] == 0.9 and live["activity_received_at"] == 106.0

    future = _snapshot(latest=_job("future", state="done", finished_at=10000.0))
    future["observed_at"] = 10000.0
    statuses[studio["id"]].update(activity=future, activity_received_at=107.0)
    activity.observe_poll([studio], statuses, {}, now=107.0)
    assert not any(row["job_id"] == "future" for row in ledger.activity_events(machine="mac-a"))

    terminal = _snapshot(latest=_job("old", state="done", finished_at=110.0))
    terminal["observed_at"] = 110.0
    statuses[studio["id"]].update(activity=terminal, activity_received_at=110.0)
    activity.observe_poll([studio], statuses, {}, now=110.0)
    boundary = 110.0 + activity.RETENTION_S + 5.0
    terminal["observed_at"] = boundary
    statuses[studio["id"]]["activity_received_at"] = boundary
    activity.observe_poll([studio], statuses, {}, now=boundary)
    activity.observe_poll([studio], statuses, {}, now=boundary + 5.0)
    assert not any(row["job_id"] == "old" for row in ledger.activity_events(machine="mac-a"))
    later = 110.0 + activity.RETENTION_S + activity.REPORTER_CLOCK_SKEW_S + 5.0
    terminal["observed_at"] = later
    statuses[studio["id"]]["activity_received_at"] = later
    activity.observe_poll([studio], statuses, {}, now=later)
    assert not any(row["job_id"] == "old" for row in ledger.activity_events(machine="mac-a"))


def test_broker_active_work_precedes_offline_but_retained_reporter_does_not(reset):
    from backend import activity

    studio = _studio()
    statuses = {studio["id"]: _status(_snapshot(active=_job()), state="down")}
    statuses[studio["id"]]["activity_received_at"] = 100.0
    assert activity.fleet_snapshot([studio], statuses, {}, since_s=0.0, now=100.0)["machines"][0]["state"] == "offline"
    batches = {"batch": {"model": "broker/model", "items": [{
        "studio": studio["id"], "studio_job_id": "broker", "state": "running",
    }]}}
    assert activity.fleet_snapshot([studio], statuses, batches, since_s=0.0, now=100.0)["machines"][0]["state"] == "working"


def test_validator_rejects_missing_start_order_and_future_terminal(reset):
    from backend import activity

    no_start = _snapshot(latest=_job("order", state="done", started_at=None,
                                     finished_at=90.0))
    no_start["latest"]["created_at"] = 100.0
    assert activity.validate_snapshot(no_start) is None
    active = _snapshot(active=_job("active", started_at=None))
    active["active"]["updated_at"] = 90.0
    active["active"]["created_at"] = 100.0
    assert activity.validate_snapshot(active) is None
    future = _snapshot(latest=_job("future", state="done", finished_at=101.0))
    future["observed_at"] = 100.0
    assert activity.validate_snapshot(future) is None
    after_finish = _snapshot(latest=_job("late-update", state="done", finished_at=100.0))
    after_finish["latest"]["updated_at"] = 101.0
    after_finish["observed_at"] = 101.0
    assert activity.validate_snapshot(after_finish) is None


def test_prune_keeps_one_stable_predecessor_without_rewrite_churn(reset):
    ledger.record_machine_state(machine="mac-a", reachable=True, working=False,
                                state="ready", observed_at=0.0)
    ledger.prune_activity(100.0)
    first = ledger.machine_state_transitions("mac-a")
    ledger.prune_activity(105.0)
    assert ledger.machine_state_transitions("mac-a") == first


def test_out_of_policy_clock_skew_is_partial_unknown_evidence(reset):
    from backend import activity

    studio = _studio()
    for observed_at in (1.0, 2000.0):
        snapshot = _snapshot(active=_job("skew", started_at=0.0))
        snapshot["active"].update(created_at=0.0, updated_at=0.0)
        snapshot["observed_at"] = observed_at
        statuses = {studio["id"]: _status(snapshot)}
        statuses[studio["id"]]["activity_received_at"] = 1000.0
        machine = activity.fleet_snapshot([studio], statuses, {}, since_s=0.0, now=1000.0)["machines"][0]
        assert machine["state"] == "unknown"
        assert machine["limitation"] == "Activity reporter clock skew exceeds policy"
        assert machine["utilization"]["evidence"] == "partial"
