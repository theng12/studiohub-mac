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
    return {
        "schema": "kh-studio.activity.v1", "observed_at": 100.0,
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
