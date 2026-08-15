import asyncio
import hashlib
import json
import math
import os
import threading
from copy import deepcopy

import pytest


COMPONENTS = {
    "hub": {
        "repository": "theng12/studiohub-mac",
        "version": "2.8.0",
        "commit": "a" * 40,
    },
    "image": {
        "repository": "theng12/imagestudio-mac",
        "version": "1.30.1",
        "commit": "b" * 40,
        "installed_only": True,
    },
    "voice": {
        "repository": "theng12/voicestudio-mac",
        "version": "1.27.9",
        "commit": "c" * 40,
        "installed_only": True,
    },
}


def _manifest(sequence=12, **changes):
    value = {
        "schema": "genstudio.studio-fleet-release-intent",
        "schema_version": 1,
        "sequence": sequence,
        "created_at": "2026-08-15T00:00:00Z",
        "components": deepcopy(COMPONENTS),
        **changes,
    }
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    value["release_id"] = "sha256:" + hashlib.sha256(canonical).hexdigest()
    return value


class Clock:
    def __init__(self, value=1_700_000_000.0):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class Monitor:
    registry = [
        {"id": "image", "modality": "image", "machine": "local", "host": "127.0.0.1"},
        {"id": "voice", "modality": "voice", "machine": "local", "host": "127.0.0.1"},
        {"id": "image@mac-a", "modality": "image", "machine": "mac-a", "host": "10.0.0.2"},
        {"id": "voice@mac-a", "modality": "voice", "machine": "mac-a", "host": "10.0.0.2"},
    ]


def _service(tmp_path, clock=None, peer_reader=None, **options):
    from backend.release_reconciliation import ReleaseReconciler

    return ReleaseReconciler(
        Monitor(),
        state_path=tmp_path / "release_reconciliation.json",
        clock=clock or Clock(),
        peer_reader=peer_reader or (lambda machine: None),
        **options,
    )


def _raw_state(tmp_path):
    return json.loads((tmp_path / "release_reconciliation.json").read_text())


def _record_current(service, job_id, machine, component):
    target = COMPONENTS[component]
    return service.record_component(
        job_id,
        machine,
        component,
        state="current",
        observed_version=target["version"],
        observed_commit=target["commit"],
    )


def _ack_catalog_for_test(service, job_id):
    """Test-only durable acknowledgement after a simulated idempotent request."""
    now = service._clock()

    def mutate(state):
        catalog = state["jobs"][job_id]["catalog"]
        catalog.update(
            state="acknowledged",
            attempt=max(1, catalog["attempt"]),
            next_retry=None,
            requested_at=catalog["requested_at"] or now,
            acknowledged_at=now,
        )

    service._write(mutate)


def _converge(service, job_id):
    for machine in ("local", "mac-a"):
        for component in ("hub", "image", "voice"):
            _record_current(service, job_id, machine, component)
    _ack_catalog_for_test(service, job_id)


def test_intent_is_canonical_atomic_and_idempotent(tmp_path):
    service = _service(tmp_path)
    manifest = _manifest()

    changed, saved = service.replace_intent(manifest)
    assert changed is True
    assert saved == manifest
    state_path = tmp_path / "release_reconciliation.json"
    first_bytes = state_path.read_bytes()
    assert list(tmp_path.glob(".release_reconciliation.json.*.tmp")) == []

    assert service.replace_intent(deepcopy(manifest))[0] is False
    assert state_path.read_bytes() == first_bytes

    tampered = deepcopy(manifest)
    tampered["components"]["hub"]["commit"] = "d" * 40
    with pytest.raises(ValueError, match="release_id"):
        service.replace_intent(tampered)
    assert state_path.read_bytes() == first_bytes


def test_intent_rejects_lower_sequence_before_mutating_state(tmp_path):
    service = _service(tmp_path)
    service.replace_intent(_manifest(sequence=12))
    first_bytes = (tmp_path / "release_reconciliation.json").read_bytes()

    with pytest.raises(ValueError, match="sequence"):
        service.replace_intent(_manifest(sequence=11))

    assert (tmp_path / "release_reconciliation.json").read_bytes() == first_bytes


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda value: value.update(created_at="15 August 2026"), "created_at"),
        (lambda value: value["components"]["hub"].update(version="2.8"), "version"),
        (lambda value: value["components"]["voice"].update(commit="A" * 40), "commit"),
        (lambda value: value["components"]["image"].update(repository="someone/fork"), "repository"),
        (lambda value: value["components"].update(chat=deepcopy(COMPONENTS["hub"])), "components"),
        (lambda value: value["components"]["image"].update(installed_only=False), "installed_only"),
    ],
)
def test_intent_rejects_malformed_or_unknown_component_targets(tmp_path, mutate, message):
    value = _manifest()
    value.pop("release_id")
    mutate(value)
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    value["release_id"] = "sha256:" + hashlib.sha256(canonical).hexdigest()

    with pytest.raises(ValueError, match=message):
        _service(tmp_path).replace_intent(value)
    assert not (tmp_path / "release_reconciliation.json").exists()


def test_activation_persists_deterministic_operation_and_remote_child_ids(tmp_path):
    first = _service(tmp_path)
    manifest = _manifest()
    first.replace_intent(manifest)

    job = first.activate(manifest["release_id"], genstudio_run_reference="run-42")
    replay = first.activate(manifest["release_id"], genstudio_run_reference="run-42")
    assert replay["id"] == job["id"]
    assert replay["machines"]["mac-a"]["operation_id"].startswith("managed-")
    assert len(replay["machines"]["mac-a"]["operation_id"]) <= 128
    assert replay["machines"]["mac-a"]["operation_id"] == job["machines"]["mac-a"]["operation_id"]

    assert first.persist_remote_job(job["id"], "mac-a", "agent-job-9") is True
    assert first.persist_remote_job(job["id"], "mac-a", "agent-job-9") is False
    with pytest.raises(ValueError, match="agent job"):
        first.persist_remote_job(job["id"], "mac-a", "different-agent-job")

    restarted = _service(tmp_path)
    snapshot = restarted.job_snapshot(job["id"])
    assert snapshot["machines"]["mac-a"]["agent_job_id"] == "agent-job-9"
    assert snapshot["machines"]["mac-a"]["operation_id"] == job["machines"]["mac-a"]["operation_id"]
    assert list(restarted.state_snapshot()["jobs"]) == [job["id"]]


def test_restart_adopts_nonterminal_job_not_fleet_ops_history(tmp_path):
    legacy = {
        "hub_updates": {
            "legacy-job": {"id": "legacy-job", "status": "running", "next_retry": 1},
        },
    }
    (tmp_path / "fleet_versions.json").write_text(json.dumps(legacy))
    assert _service(tmp_path).resume_pending() == 0

    first = _service(tmp_path)
    manifest = _manifest()
    first.replace_intent(manifest)
    job = first.activate(manifest["release_id"], genstudio_run_reference=None)
    first.persist_remote_job(job["id"], "mac-a", "agent-job")

    second = _service(tmp_path)
    assert second.resume_pending() == 1
    assert second.resume_pending() == 0
    assert second.job_snapshot(job["id"])["machines"]["mac-a"]["agent_job_id"] == "agent-job"
    assert len(second.state_snapshot()["jobs"]) == 1


@pytest.mark.parametrize(
    "boundary",
    [
        "intent_receipt",
        "activation",
        "between_components",
        "remote_hub_restart",
        "lost_post_response",
        "poll_transport_loss",
        "controller_hub_last",
    ],
)
def test_restart_matrix_adopts_one_release_job_at_every_boundary(tmp_path, boundary):
    manifest = _manifest()
    before = _service(tmp_path)
    before.replace_intent(manifest)

    if boundary == "intent_receipt":
        before = _service(tmp_path)
    job = before.activate(manifest["release_id"], genstudio_run_reference=None)
    job_id = job["id"]

    if boundary == "between_components":
        _record_current(before, job_id, "mac-a", "hub")
        before.record_component(job_id, "mac-a", "image", state="checking")
    elif boundary == "remote_hub_restart":
        before.persist_remote_job(job_id, "mac-a", "agent-job")
        before.record_component(job_id, "mac-a", "hub", state="restarting")
    elif boundary == "lost_post_response":
        before.record_component(job_id, "mac-a", "hub", state="updating")
    elif boundary == "poll_transport_loss":
        before.persist_remote_job(job_id, "mac-a", "agent-job")
        before.record_component(job_id, "mac-a", "hub", state="verifying")
    elif boundary == "controller_hub_last":
        for machine in ("mac-a", "local"):
            for component in ("image", "voice"):
                _record_current(before, job_id, machine, component)
        _record_current(before, job_id, "mac-a", "hub")
        before.record_component(job_id, "local", "hub", state="restarting")

    resumed = _service(tmp_path)
    assert resumed.resume_pending() == 1
    snapshot = resumed.job_snapshot(job_id)
    assert snapshot["id"] == job_id
    assert len(resumed.state_snapshot()["jobs"]) == 1
    if boundary in {"remote_hub_restart", "poll_transport_loss"}:
        assert snapshot["machines"]["mac-a"]["agent_job_id"] == "agent-job"
    if boundary == "controller_hub_last":
        assert snapshot["machines"]["local"]["components"]["hub"]["state"] == "restarting"


def test_degraded_pending_work_survives_restart_and_due_scan(tmp_path):
    clock = Clock()
    first = _service(tmp_path, clock=clock)
    manifest = _manifest()
    first.replace_intent(manifest)
    job = first.activate(manifest["release_id"], genstudio_run_reference=None)
    first.record_component(job["id"], "mac-a", "voice", state="pending_offline")

    degraded = first.job_snapshot(job["id"])
    assert degraded["state"] == "degraded"
    assert degraded["finished_at"] is None
    assert degraded["next_retry"] == clock() + 60
    assert degraded["machines"]["mac-a"]["components"]["voice"]["attempt"] == 1

    second = _service(tmp_path, clock=clock)
    assert second.resume_due() == 0
    clock.advance(60)
    assert second.resume_due() == 1
    due = second.job_snapshot(job["id"])
    assert due["machines"]["mac-a"]["components"]["voice"]["state"] == "checking"
    assert due["state"] == "running"

    second.record_component(job["id"], "mac-a", "voice", state="retryable_failure")
    retried = second.job_snapshot(job["id"])
    assert retried["machines"]["mac-a"]["components"]["voice"]["attempt"] == 2
    assert retried["next_retry"] == clock() + 300


def test_retry_backoff_is_deterministic_and_caps_at_one_day():
    from backend.release_reconciliation import DUE_SCAN_INTERVAL_SECONDS, retry_delay

    assert [retry_delay(attempt) for attempt in range(1, 9)] == [
        60, 300, 900, 3600, 14400, 86400, 86400, 86400,
    ]
    assert DUE_SCAN_INTERVAL_SECONDS == 900
    with pytest.raises(ValueError, match="attempt"):
        retry_delay(0)


def test_state_and_public_snapshots_redact_secrets_paths_commands_and_host_details(tmp_path):
    unsafe = {
        "reachable": False,
        "status": "unreachable",
        "host": "10.0.0.2",
        "token": "fleet-secret",
        "checkout_path": "/Users/operator/private/repo",
        "command": "git reset --hard",
    }
    service = _service(tmp_path, peer_reader=lambda machine: unsafe)
    manifest = _manifest()
    service.replace_intent(manifest)
    job = service.activate(manifest["release_id"], genstudio_run_reference="run-safe")
    service.record_component(
        job["id"], "mac-a", "hub", state="retryable_failure",
        error_code="transport_unavailable",
    )

    raw = (tmp_path / "release_reconciliation.json").read_text()
    public = json.dumps(service.job_snapshot(job["id"]), sort_keys=True)
    for forbidden in (
        "fleet-secret", "token=abc", "/Users/operator", "git reset --hard",
        "10.0.0.2", "checkout_path", '"command"',
    ):
        assert forbidden not in raw
        assert forbidden not in public
    assert service.job_snapshot(job["id"])["machines"]["mac-a"]["host_evidence"] == {
        "reachable": False,
        "status": "unreachable",
    }


@pytest.mark.parametrize(
    "hostile",
    [
        "api_key=sk-live-value",
        "git -C /opt/studio fetch failed",
        "https://user:password@example.test/update",
        "host=10.0.0.2",
        "customer prompt contents",
    ],
)
def test_arbitrary_error_text_is_not_an_accepted_persistence_input(tmp_path, hostile):
    service = _service(tmp_path)
    manifest = _manifest()
    service.replace_intent(manifest)
    job = service.activate(manifest["release_id"], genstudio_run_reference=None)

    with pytest.raises(TypeError):
        service.record_component(
            job["id"], "mac-a", "hub", state="retryable_failure", last_error=hostile,
        )
    raw = (tmp_path / "release_reconciliation.json").read_text()
    assert hostile not in raw


def test_error_code_produces_only_reconciler_owned_safe_detail(tmp_path):
    service = _service(tmp_path)
    manifest = _manifest()
    service.replace_intent(manifest)
    job = service.activate(manifest["release_id"], genstudio_run_reference=None)

    row = service.record_component(
        job["id"], "mac-a", "hub", state="auth_blocked", error_code="auth_rejected",
    )

    assert row["error_code"] == "auth_rejected"
    assert row["detail"] == "managed update authentication was rejected"
    with pytest.raises(ValueError, match="error code"):
        service.record_component(
            job["id"], "mac-a", "hub", state="retryable_failure",
            error_code="git -C /opt/studio failed",
        )


def test_error_code_cannot_be_attached_to_nonerror_state(tmp_path):
    service = _service(tmp_path)
    manifest = _manifest()
    service.replace_intent(manifest)
    job = service.activate(manifest["release_id"], genstudio_run_reference=None)

    with pytest.raises(ValueError, match="error code"):
        service.record_component(
            job["id"], "mac-a", "hub", state="current",
            observed_version="2.8.0", observed_commit="a" * 40,
            error_code="auth_rejected",
        )


@pytest.mark.parametrize("value", ["agent/job", "agent job", "x" * 129])
def test_durable_identifiers_reject_paths_commands_and_unbounded_values(tmp_path, value):
    service = _service(tmp_path)
    manifest = _manifest()
    service.replace_intent(manifest)
    job = service.activate(manifest["release_id"], genstudio_run_reference=None)

    with pytest.raises(ValueError, match="agent_job_id"):
        service.persist_remote_job(job["id"], "mac-a", value)


def test_complete_and_blocked_release_are_the_only_terminal_states(tmp_path):
    service = _service(tmp_path)
    manifest = _manifest()
    service.replace_intent(manifest)
    job = service.activate(manifest["release_id"], genstudio_run_reference=None)

    service.record_component(job["id"], "mac-a", "voice", state="pending_offline")
    service.persist_job(job["id"], state="degraded")
    assert service.job_snapshot(job["id"])["finished_at"] is None
    service.persist_job(job["id"], state="blocked_release")
    assert service.job_snapshot(job["id"])["finished_at"] is not None
    assert service.resume_pending() == 0


def test_pending_component_prevents_false_complete_terminalization(tmp_path):
    service = _service(tmp_path)
    manifest = _manifest()
    service.replace_intent(manifest)
    job = service.activate(manifest["release_id"], genstudio_run_reference=None)
    service.record_component(job["id"], "mac-a", "voice", state="auth_blocked")

    with pytest.raises(ValueError, match="converged"):
        service.persist_job(job["id"], state="complete")
    saved = service.job_snapshot(job["id"])
    assert saved["state"] == "degraded" and saved["finished_at"] is None


def test_terminal_release_job_rejects_later_component_mutation(tmp_path):
    service = _service(tmp_path)
    manifest = _manifest()
    service.replace_intent(manifest)
    job = service.activate(manifest["release_id"], genstudio_run_reference=None)
    service.persist_job(job["id"], state="blocked_release")
    terminal = service.job_snapshot(job["id"])

    with pytest.raises(ValueError, match="terminal"):
        service.record_component(job["id"], "mac-a", "hub", state="retryable_failure")
    assert service.job_snapshot(job["id"]) == terminal


def test_terminal_release_job_rejects_later_child_job_mutation(tmp_path):
    service = _service(tmp_path)
    manifest = _manifest()
    service.replace_intent(manifest)
    job = service.activate(manifest["release_id"], genstudio_run_reference=None)
    service.persist_job(job["id"], state="blocked_release")
    terminal = service.job_snapshot(job["id"])

    with pytest.raises(ValueError, match="terminal"):
        service.persist_remote_job(job["id"], "mac-a", "late-child")
    assert service.job_snapshot(job["id"]) == terminal


@pytest.mark.parametrize("inflight", ["checking", "updating", "restarting", "verifying"])
def test_inflight_installed_component_prevents_completion(tmp_path, inflight):
    service = _service(tmp_path)
    manifest = _manifest()
    service.replace_intent(manifest)
    job = service.activate(manifest["release_id"], genstudio_run_reference=None)
    service.record_component(job["id"], "mac-a", "hub", state=inflight)

    with pytest.raises(ValueError, match="converged"):
        service.persist_job(job["id"], state="complete")
    assert service.job_snapshot(job["id"])["finished_at"] is None


@pytest.mark.parametrize(
    "version, commit",
    [
        (None, None),
        ("2.8.1", "a" * 40),
        ("2.8.0", "d" * 40),
    ],
)
def test_current_requires_exact_observed_attestation(tmp_path, version, commit):
    service = _service(tmp_path)
    manifest = _manifest()
    service.replace_intent(manifest)
    job = service.activate(manifest["release_id"], genstudio_run_reference=None)

    with pytest.raises(ValueError, match="exact observed"):
        service.record_component(
            job["id"], "mac-a", "hub", state="current",
            observed_version=version, observed_commit=commit,
        )


def test_complete_is_derived_only_after_all_installed_rows_are_exact_current(tmp_path):
    service = _service(tmp_path)
    manifest = _manifest()
    service.replace_intent(manifest)
    job = service.activate(manifest["release_id"], genstudio_run_reference=None)
    _converge(service, job["id"])

    complete = service.persist_job(job["id"], state="complete")

    assert complete["state"] == "complete"
    assert complete["finished_at"] is not None
    assert {row["state"] for machine in complete["machines"].values()
            for row in machine["components"].values()} == {"current"}


class SparseMonitor:
    registry = [
        {"id": "image", "modality": "image", "machine": "local", "host": "127.0.0.1"},
    ]


def test_not_installed_component_can_complete_without_attestation(tmp_path):
    from backend.release_reconciliation import ReleaseReconciler

    service = ReleaseReconciler(
        SparseMonitor(), state_path=tmp_path / "release_reconciliation.json",
        clock=Clock(), peer_reader=lambda machine: None,
    )
    manifest = _manifest()
    service.replace_intent(manifest)
    job = service.activate(manifest["release_id"], genstudio_run_reference=None)
    _record_current(service, job["id"], "local", "hub")
    _record_current(service, job["id"], "local", "image")
    _ack_catalog_for_test(service, job["id"])

    assert service.persist_job(job["id"], state="complete")["state"] == "complete"
    assert service.job_snapshot(job["id"])["machines"]["local"]["components"]["voice"]["state"] == "not_installed"


@pytest.mark.parametrize(
    "retry_at",
    [object(), "tomorrow", True, math.nan, math.inf, -1, 1_699_999_999.0],
)
def test_retry_time_is_finite_numeric_and_not_in_the_past_before_mutation(tmp_path, retry_at):
    service = _service(tmp_path)
    manifest = _manifest()
    service.replace_intent(manifest)
    job = service.activate(manifest["release_id"], genstudio_run_reference=None)
    before = service.job_snapshot(job["id"])

    with pytest.raises((TypeError, ValueError), match="next_retry"):
        service.record_component(
            job["id"], "mac-a", "voice", state="pending_offline", next_retry=retry_at,
        )
    assert service.job_snapshot(job["id"]) == before


def test_retry_time_is_rejected_for_nonretryable_state(tmp_path):
    service = _service(tmp_path)
    manifest = _manifest()
    service.replace_intent(manifest)
    job = service.activate(manifest["release_id"], genstudio_run_reference=None)
    before = service.job_snapshot(job["id"])

    with pytest.raises(ValueError, match="next_retry"):
        service.record_component(
            job["id"], "mac-a", "voice", state="checking", next_retry=1_700_000_060,
        )
    assert service.job_snapshot(job["id"]) == before


def test_failed_intent_write_does_not_change_live_or_disk_state(tmp_path, monkeypatch):
    from backend import release_reconciliation

    service = _service(tmp_path)
    manifest = _manifest()
    monkeypatch.setattr(release_reconciliation, "_atomic_json", lambda *args: (_ for _ in ()).throw(OSError("disk")))

    with pytest.raises(OSError, match="disk"):
        service.replace_intent(manifest)
    assert service.intent_snapshot() is None
    assert not (tmp_path / "release_reconciliation.json").exists()


def test_failed_child_write_does_not_create_false_adoption(tmp_path, monkeypatch):
    from backend import release_reconciliation

    service = _service(tmp_path)
    manifest = _manifest()
    service.replace_intent(manifest)
    job = service.activate(manifest["release_id"], genstudio_run_reference=None)
    durable = (tmp_path / "release_reconciliation.json").read_bytes()
    monkeypatch.setattr(release_reconciliation, "_atomic_json", lambda *args: (_ for _ in ()).throw(OSError("disk")))

    with pytest.raises(OSError, match="disk"):
        service.persist_remote_job(job["id"], "mac-a", "agent-job")
    assert service.job_snapshot(job["id"])["machines"]["mac-a"]["agent_job_id"] is None
    assert (tmp_path / "release_reconciliation.json").read_bytes() == durable


def test_failed_retry_write_does_not_increment_attempt_in_memory(tmp_path, monkeypatch):
    from backend import release_reconciliation

    service = _service(tmp_path)
    manifest = _manifest()
    service.replace_intent(manifest)
    job = service.activate(manifest["release_id"], genstudio_run_reference=None)
    before = service.job_snapshot(job["id"])
    monkeypatch.setattr(release_reconciliation, "_atomic_json", lambda *args: (_ for _ in ()).throw(OSError("disk")))

    with pytest.raises(OSError, match="disk"):
        service.record_component(job["id"], "mac-a", "voice", state="pending_offline")
    assert service.job_snapshot(job["id"]) == before


def test_failed_terminal_write_keeps_job_adoptable(tmp_path, monkeypatch):
    from backend import release_reconciliation

    service = _service(tmp_path)
    manifest = _manifest()
    service.replace_intent(manifest)
    job = service.activate(manifest["release_id"], genstudio_run_reference=None)
    _converge(service, job["id"])
    monkeypatch.setattr(release_reconciliation, "_atomic_json", lambda *args: (_ for _ in ()).throw(OSError("disk")))

    with pytest.raises(OSError, match="disk"):
        service.persist_job(job["id"], state="complete")
    assert service.job_snapshot(job["id"])["state"] == "running"
    monkeypatch.undo()
    assert service.resume_pending() == 1


def test_two_instances_preserve_component_and_child_updates(tmp_path):
    first = _service(tmp_path)
    manifest = _manifest()
    first.replace_intent(manifest)
    job = first.activate(manifest["release_id"], genstudio_run_reference=None)
    second = _service(tmp_path)

    _record_current(first, job["id"], "mac-a", "hub")
    second.persist_remote_job(job["id"], "mac-a", "agent-job")

    restarted = _service(tmp_path).job_snapshot(job["id"])
    assert restarted["machines"]["mac-a"]["components"]["hub"]["state"] == "current"
    assert restarted["machines"]["mac-a"]["agent_job_id"] == "agent-job"


def test_higher_sequence_intent_is_rejected_while_prior_job_is_nonterminal(tmp_path):
    service = _service(tmp_path)
    manifest = _manifest(sequence=12)
    service.replace_intent(manifest)
    job = service.activate(manifest["release_id"], genstudio_run_reference=None)

    with pytest.raises(ValueError, match="nonterminal"):
        service.replace_intent(_manifest(sequence=13))
    assert service.intent_snapshot()["manifest"]["release_id"] == manifest["release_id"]

    _converge(service, job["id"])
    service.persist_job(job["id"], state="complete")
    assert service.replace_intent(_manifest(sequence=13))[0] is True


def test_resume_adopts_only_current_activation_job(tmp_path):
    first = _service(tmp_path)
    old_manifest = _manifest(sequence=12)
    first.replace_intent(old_manifest)
    old_job = first.activate(old_manifest["release_id"], genstudio_run_reference=None)
    _converge(first, old_job["id"])
    first.persist_job(old_job["id"], state="complete")
    current_manifest = _manifest(sequence=13)
    first.replace_intent(current_manifest)
    current_job = first.activate(current_manifest["release_id"], genstudio_run_reference=None)

    state = _raw_state(tmp_path)
    state["jobs"][old_job["id"]].update(state="running", finished_at=None)
    (tmp_path / "release_reconciliation.json").write_text(json.dumps(state))
    os.chmod(tmp_path / "release_reconciliation.json", 0o600)

    restarted = _service(tmp_path)
    assert restarted.resume_pending() == 1
    assert restarted.job_snapshot(current_job["id"])["state"] == "running"


def test_two_threads_claim_one_adoption_lease(tmp_path):
    service = _service(
        tmp_path,
        owner_id="owner-a",
        pid=101,
        owner_alive=lambda _pid: True,
    )
    manifest = _manifest()
    service.replace_intent(manifest)
    service.activate(manifest["release_id"], genstudio_run_reference=None)
    barrier = threading.Barrier(3)
    results = []

    def claim():
        barrier.wait()
        results.append(service.resume_pending())

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert sorted(results) == [0, 1]


def test_two_instances_in_one_process_claim_one_adoption_lease(tmp_path):
    first = _service(tmp_path, owner_id="owner-a", pid=os.getpid())
    manifest = _manifest()
    first.replace_intent(manifest)
    first.activate(manifest["release_id"], genstudio_run_reference=None)
    second = _service(tmp_path, owner_id="owner-b", pid=os.getpid())
    barrier = threading.Barrier(3)
    results = []

    def claim(service):
        barrier.wait()
        results.append(service.resume_pending())

    threads = [threading.Thread(target=claim, args=(service,)) for service in (first, second)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert sorted(results) == [0, 1]


def test_overlapping_processes_claim_one_durable_adoption_lease(tmp_path):
    alive = lambda _pid: True
    first = _service(tmp_path, owner_id="process-a", pid=101, owner_alive=alive)
    manifest = _manifest()
    first.replace_intent(manifest)
    job = first.activate(manifest["release_id"], genstudio_run_reference=None)
    second = _service(tmp_path, owner_id="process-b", pid=202, owner_alive=alive)

    assert first.resume_pending() == 1
    assert second.resume_pending() == 0
    lease = _raw_state(tmp_path)["jobs"][job["id"]]["adoption_lease"]
    assert lease["owner_id"] == "process-a"
    assert lease["pid"] == 101


def test_dead_owner_can_be_taken_over_without_changing_activation_anchor(tmp_path):
    first = _service(
        tmp_path,
        owner_id="process-a",
        pid=101,
        owner_alive=lambda _pid: True,
    )
    manifest = _manifest()
    first.replace_intent(manifest)
    job = first.activate(manifest["release_id"], genstudio_run_reference=None)
    assert first.resume_pending() == 1

    second = _service(
        tmp_path,
        owner_id="process-b",
        pid=202,
        owner_alive=lambda pid: pid != 101,
    )
    assert second.resume_pending() == 1
    raw = _raw_state(tmp_path)
    assert raw["activation"]["job_id"] == job["id"]
    assert raw["activation"]["release_id"] == manifest["release_id"]
    assert raw["jobs"][job["id"]]["adoption_lease"]["owner_id"] == "process-b"


def test_expired_adoption_lease_can_be_taken_over(tmp_path):
    clock = Clock()
    first = _service(
        tmp_path, clock=clock, owner_id="process-a", pid=101,
        owner_alive=lambda _pid: True, lease_seconds=30,
    )
    manifest = _manifest()
    first.replace_intent(manifest)
    job = first.activate(manifest["release_id"], genstudio_run_reference=None)
    assert first.resume_pending() == 1

    clock.advance(31)
    second = _service(
        tmp_path, clock=clock, owner_id="process-b", pid=202,
        owner_alive=lambda _pid: True, lease_seconds=30,
    )
    assert second.resume_pending() == 1
    assert _raw_state(tmp_path)["jobs"][job["id"]]["adoption_lease"]["owner_id"] == "process-b"


def test_expired_adoption_lease_can_be_reclaimed_by_same_owner_id(tmp_path):
    clock = Clock()
    first = _service(
        tmp_path, clock=clock, owner_id="stable-owner", pid=101,
        owner_alive=lambda _pid: True, lease_seconds=30,
    )
    manifest = _manifest()
    first.replace_intent(manifest)
    job = first.activate(manifest["release_id"], genstudio_run_reference=None)
    assert first.resume_pending() == 1

    clock.advance(31)
    restarted = _service(
        tmp_path, clock=clock, owner_id="stable-owner", pid=101,
        owner_alive=lambda _pid: True, lease_seconds=30,
    )

    assert restarted.resume_pending() == 1
    lease = _raw_state(tmp_path)["jobs"][job["id"]]["adoption_lease"]
    assert lease["owner_id"] == "stable-owner"
    assert lease["acquired_at"] == clock()


def test_adoption_heartbeat_and_explicit_release_are_durable(tmp_path):
    clock = Clock()
    first = _service(
        tmp_path, clock=clock, owner_id="process-a", pid=101,
        owner_alive=lambda _pid: True, lease_seconds=30,
    )
    manifest = _manifest()
    first.replace_intent(manifest)
    job = first.activate(manifest["release_id"], genstudio_run_reference=None)
    assert first.resume_pending() == 1
    initial_expiry = _raw_state(tmp_path)["jobs"][job["id"]]["adoption_lease"]["expires_at"]

    clock.advance(10)
    assert first.refresh_adoption(job["id"]) is True
    assert _raw_state(tmp_path)["jobs"][job["id"]]["adoption_lease"]["expires_at"] > initial_expiry
    assert first.release_adoption(job["id"]) is True
    assert _raw_state(tmp_path)["jobs"][job["id"]]["adoption_lease"] is None

    second = _service(
        tmp_path, clock=clock, owner_id="process-b", pid=202,
        owner_alive=lambda _pid: True, lease_seconds=30,
    )
    assert second.resume_pending() == 1


def test_terminal_job_clears_lease_and_public_snapshots_hide_process_details(tmp_path):
    service = _service(tmp_path, owner_id="private-owner", pid=4242)
    manifest = _manifest()
    service.replace_intent(manifest)
    job = service.activate(manifest["release_id"], genstudio_run_reference=None)
    assert service.resume_pending() == 1

    public = json.dumps(service.state_snapshot(), sort_keys=True)
    assert "private-owner" not in public
    assert "4242" not in public
    assert '"pid"' not in public

    _converge(service, job["id"])
    service.persist_job(job["id"], state="complete")
    assert _raw_state(tmp_path)["jobs"][job["id"]]["adoption_lease"] is None


@pytest.mark.parametrize(
    "options",
    [
        {"owner_id": ""},
        {"owner_id": "x" * 129},
        {"pid": True},
        {"pid": 0},
        {"lease_seconds": 0},
        {"lease_seconds": math.inf},
        {"owner_alive": None},
    ],
)
def test_adoption_owner_inputs_are_validated_before_state_mutation(tmp_path, options):
    with pytest.raises(ValueError):
        _service(tmp_path, **options)
    assert not (tmp_path / "release_reconciliation.json").exists()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda state: state["desired"]["manifest"]["components"]["hub"].update(commit="d" * 40),
        lambda state: state["activation"].update(job_id="release-wrong"),
        lambda state: next(iter(state["jobs"].values())).update(state="invented"),
        lambda state: next(iter(state["jobs"].values()))["machines"]["mac-a"].update(operation_id="managed-wrong"),
        lambda state: next(iter(state["jobs"].values()))["machines"]["mac-a"].update(state="current"),
        lambda state: next(iter(state["jobs"].values()))["machines"]["mac-a"]["components"]["hub"].update(detail="api_key=sk-live"),
        lambda state: next(iter(state["jobs"].values())).update(created_at=math.nan),
    ],
)
def test_hostile_nested_durable_state_fails_closed(tmp_path, mutate):
    service = _service(tmp_path)
    manifest = _manifest()
    service.replace_intent(manifest)
    service.activate(manifest["release_id"], genstudio_run_reference=None)
    state = _raw_state(tmp_path)
    mutate(state)
    path = tmp_path / "release_reconciliation.json"
    path.write_text(json.dumps(state))
    os.chmod(path, 0o600)

    with pytest.raises(ValueError, match="durable release reconciliation state"):
        _service(tmp_path)


@pytest.mark.parametrize(
    "lease",
    [
        {"owner_id": "/private/owner", "pid": 101, "acquired_at": 1.0, "heartbeat_at": 1.0, "expires_at": 2.0},
        {"owner_id": "owner-a", "pid": True, "acquired_at": 1.0, "heartbeat_at": 1.0, "expires_at": 2.0},
        {"owner_id": "owner-a", "pid": 101, "acquired_at": math.nan, "heartbeat_at": 1.0, "expires_at": 2.0},
        {"owner_id": "owner-a", "pid": 101, "acquired_at": 2.0, "heartbeat_at": 1.0, "expires_at": 3.0},
        {"owner_id": "owner-a", "pid": 101, "acquired_at": 1.0, "heartbeat_at": 2.0, "expires_at": 2.0},
    ],
)
def test_hostile_adoption_lease_fails_closed(tmp_path, lease):
    service = _service(tmp_path)
    manifest = _manifest()
    service.replace_intent(manifest)
    job = service.activate(manifest["release_id"], genstudio_run_reference=None)
    state = _raw_state(tmp_path)
    state["jobs"][job["id"]]["adoption_lease"] = lease
    path = tmp_path / "release_reconciliation.json"
    path.write_text(json.dumps(state))
    os.chmod(path, 0o600)

    with pytest.raises(ValueError, match="durable release reconciliation state"):
        _service(tmp_path)


def test_terminal_job_with_adoption_lease_fails_closed(tmp_path):
    service = _service(tmp_path, owner_id="owner-a", pid=101)
    manifest = _manifest()
    service.replace_intent(manifest)
    job = service.activate(manifest["release_id"], genstudio_run_reference=None)
    assert service.resume_pending() == 1
    state = _raw_state(tmp_path)
    state["jobs"][job["id"]].update(state="blocked_release", finished_at=Clock()())
    path = tmp_path / "release_reconciliation.json"
    path.write_text(json.dumps(state))
    os.chmod(path, 0o600)

    with pytest.raises(ValueError, match="durable release reconciliation state"):
        _service(tmp_path)


def test_existing_state_file_mode_is_enforced_on_load(tmp_path):
    service = _service(tmp_path)
    manifest = _manifest()
    service.replace_intent(manifest)
    path = tmp_path / "release_reconciliation.json"
    os.chmod(path, 0o644)

    _service(tmp_path)

    assert path.stat().st_mode & 0o777 == 0o600


def test_persistent_lock_file_uses_ignored_state_filename_prefix(tmp_path):
    service = _service(tmp_path)

    assert service.lock_path.name == "release_reconciliation.json.lock"


def test_directory_fsync_failure_is_propagated(tmp_path, monkeypatch):
    from backend import release_reconciliation

    real_fsync = release_reconciliation.os.fsync
    calls = 0

    def fail_directory_fsync(descriptor):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("directory durability failed")
        return real_fsync(descriptor)

    monkeypatch.setattr(release_reconciliation.os, "fsync", fail_directory_fsync)
    with pytest.raises(OSError, match="directory durability"):
        release_reconciliation._atomic_json(tmp_path / "state.json", {"ok": True})


def test_corrupt_durable_state_fails_closed_without_replacement(tmp_path):
    path = tmp_path / "release_reconciliation.json"
    path.write_text('{"schema_version": 1, "desired":')

    with pytest.raises(ValueError, match="durable release reconciliation state"):
        _service(tmp_path)
    assert path.read_text() == '{"schema_version": 1, "desired":'


class ExecutionMonitor:
    registry = [
        {"id": "image", "modality": "image", "machine": "local", "host": "127.0.0.1"},
        {"id": "voice", "modality": "voice", "machine": "local", "host": "127.0.0.1"},
        {"id": "image@mac-b", "modality": "image", "machine": "mac-b", "host": "10.0.0.3"},
        {"id": "voice@mac-b", "modality": "voice", "machine": "mac-b", "host": "10.0.0.3"},
        {"id": "image@mac-a", "modality": "image", "machine": "mac-a", "host": "10.0.0.2"},
        {"id": "voice@mac-a", "modality": "voice", "machine": "mac-a", "host": "10.0.0.2"},
    ]


def _execution_service(tmp_path, **options):
    from backend.release_reconciliation import ReleaseReconciler

    peer_state = options.pop("peer_state", {
        "mac-a": {"reachable": True, "auth": True, "status": "connected"},
        "mac-b": {"reachable": True, "auth": True, "status": "connected"},
    })
    service = ReleaseReconciler(
        ExecutionMonitor(),
        state_path=tmp_path / "release_reconciliation.json",
        clock=options.pop("clock", Clock()),
        peer_reader=lambda machine: peer_state.get(machine),
        lease_seconds=options.pop("lease_seconds", 30),
        heartbeat_seconds=options.pop("heartbeat_seconds", 5),
        **options,
    )
    manifest = _manifest()
    service.replace_intent(manifest)
    service.activate(manifest["release_id"], genstudio_run_reference=None)
    return service, manifest


def _exact_result(component, state="current", **extra):
    target = COMPONENTS[component]
    return {
        "component": component,
        "state": state,
        "observed_version": target["version"] if state == "current" else None,
        "observed_commit": target["commit"] if state == "current" else None,
        **extra,
    }


@pytest.mark.asyncio
async def test_canary_agents_are_serial_then_controller_is_last(tmp_path):
    events = []

    async def remote_bundle(machine, _body, _existing_job_id):
        for component in ("hub", "image", "voice"):
            events.append(f"{machine}:{component}")
        return {
            "job_id": f"agent-{machine}",
            "components": [_exact_result(name) for name in ("hub", "image", "voice")],
        }

    async def local_components(_monitor, _manifest, *, operation_id):
        del operation_id
        events.extend(("local:image", "local:voice"))
        return [_exact_result("image"), _exact_result("voice")]

    async def local_hub(_target, _operation_id):
        events.append("local:hub")
        return _exact_result("hub")

    service, manifest = _execution_service(
        tmp_path,
        remote_bundle_runner=remote_bundle,
        component_runner=local_components,
        hub_runner=local_hub,
    )

    job = await service.run(manifest["release_id"])

    assert events == [
        "mac-a:hub", "mac-a:image", "mac-a:voice",
        "mac-b:hub", "mac-b:image", "mac-b:voice",
        "local:image", "local:voice", "local:hub",
    ]
    assert job["state"] == "complete"


@pytest.mark.asyncio
async def test_disabled_machine_is_excluded_from_release_inventory_and_execution(
    tmp_path, monkeypatch,
):
    from backend import registry

    monkeypatch.setattr(
        registry, "machine_enabled", lambda machine: machine != "mac-b",
    )
    calls = []

    async def remote_bundle(machine, _body, _existing_job_id):
        calls.append(machine)
        return {
            "job_id": f"agent-{machine}",
            "components": [_exact_result(name) for name in ("hub", "image", "voice")],
        }

    service, manifest = _execution_service(
        tmp_path,
        remote_bundle_runner=remote_bundle,
        component_runner=lambda *_args, **_kwargs: [
            _exact_result("image"), _exact_result("voice"),
        ],
        hub_runner=lambda *_args, **_kwargs: _exact_result("hub"),
    )

    job = await service.run(manifest["release_id"])

    assert set(job["machines"]) == {"local", "mac-a"}
    assert calls == ["mac-a"]
    assert job["state"] == "complete"


def test_studio_level_off_does_not_exclude_enabled_machine_from_release_inventory(
    tmp_path, monkeypatch,
):
    from backend import registry

    monkeypatch.setattr(registry, "machine_enabled", lambda _machine: True)
    monkeypatch.setattr(registry, "studio_enabled", lambda *_args: False)
    service, _manifest_value = _execution_service(tmp_path)
    job_id = service.state_snapshot()["activation"]["job_id"]

    inventory = service.job_snapshot(job_id)["machines"]

    assert inventory["mac-a"]["components"]["image"]["installed"] is True
    assert inventory["mac-a"]["components"]["voice"]["installed"] is True


@pytest.mark.asyncio
async def test_reenabled_machine_is_deterministically_supplemented(
    tmp_path, monkeypatch,
):
    from backend import registry

    disabled = {"mac-b"}
    monkeypatch.setattr(
        registry, "machine_enabled", lambda machine: machine not in disabled,
    )
    calls = []

    async def remote_bundle(machine, _body, _existing_job_id):
        calls.append(machine)
        return {
            "job_id": f"agent-{machine}",
            "components": [_exact_result(name) for name in ("hub", "image", "voice")],
        }

    service, manifest = _execution_service(
        tmp_path,
        remote_bundle_runner=remote_bundle,
        component_runner=lambda *_args, **_kwargs: [
            _exact_result("image"), _exact_result("voice"),
        ],
        hub_runner=lambda *_args, **_kwargs: _exact_result("hub"),
    )
    base = await service.run(manifest["release_id"])
    assert base["state"] == "complete"
    assert "mac-b" not in base["machines"]

    calls.clear()
    disabled.clear()
    assert service.reconcile_registry() == 1
    supplemented = await service.run(manifest["release_id"])

    assert supplemented["supersedes_job_id"] == base["id"]
    assert supplemented["machines"]["mac-b"]["state"] == "current"
    assert calls == ["mac-b"]


@pytest.mark.asyncio
async def test_machine_disabled_after_activation_is_durably_excluded_then_rejoins(
    tmp_path, monkeypatch,
):
    from backend import registry

    disabled = set()
    monkeypatch.setattr(
        registry, "machine_enabled", lambda machine: machine not in disabled,
    )
    calls = []

    async def remote_bundle(machine, _body, _existing_job_id):
        calls.append(machine)
        if machine == "mac-a":
            disabled.add("mac-b")
        return {
            "job_id": f"agent-{machine}",
            "components": [_exact_result(name) for name in ("hub", "image", "voice")],
        }

    service, manifest = _execution_service(
        tmp_path,
        remote_bundle_runner=remote_bundle,
        component_runner=lambda *_args, **_kwargs: [
            _exact_result("image"), _exact_result("voice"),
        ],
        hub_runner=lambda *_args, **_kwargs: _exact_result("hub"),
    )
    activated_job_id = service.state_snapshot()["activation"]["job_id"]
    assert "mac-b" in service.job_snapshot(activated_job_id)["machines"]

    base = await service.run(manifest["release_id"])

    assert calls == ["mac-a"]
    assert base["state"] == "complete"
    assert base["machines"]["mac-b"]["state"] == "excluded"
    assert {
        row["state"] for row in base["machines"]["mac-b"]["components"].values()
    } == {"excluded_disabled"}

    disabled.clear()
    assert service.reconcile_registry() == 1
    supplemented = await service.run(manifest["release_id"])

    assert supplemented["supersedes_job_id"] == base["id"]
    assert supplemented["machines"]["mac-b"]["state"] == "current"
    assert calls == ["mac-a", "mac-b"]


@pytest.mark.asyncio
async def test_post_activation_exclusion_is_not_reported_as_canary(
    tmp_path, monkeypatch,
):
    from backend import registry

    disabled = set()
    monkeypatch.setattr(
        registry, "machine_enabled", lambda machine: machine not in disabled,
    )
    calls = []

    async def remote_bundle(machine, _body, _existing_job_id):
        calls.append(machine)
        return {
            "job_id": f"agent-{machine}",
            "components": [_exact_result(name) for name in ("hub", "image", "voice")],
        }

    service, manifest = _execution_service(
        tmp_path,
        remote_bundle_runner=remote_bundle,
        component_runner=lambda *_args, **_kwargs: [
            _exact_result("image"), _exact_result("voice"),
        ],
        hub_runner=lambda *_args, **_kwargs: _exact_result("hub"),
    )
    disabled.add("mac-a")

    await service.run(manifest["release_id"])

    assert calls == ["mac-b"]
    assert service.capability_evidence()["canary_machine_id"] == "mac-b"


@pytest.mark.asyncio
async def test_reenable_during_active_lease_runs_immediate_post_pass_supplement(
    tmp_path, monkeypatch,
):
    from backend import registry

    disabled = {"mac-b"}
    monkeypatch.setattr(
        registry, "machine_enabled", lambda machine: machine not in disabled,
    )
    mac_a_started = asyncio.Event()
    allow_mac_a = asyncio.Event()
    calls = []

    async def remote_bundle(machine, _body, _existing_job_id):
        calls.append(machine)
        if machine == "mac-a":
            mac_a_started.set()
            await allow_mac_a.wait()
        return {
            "job_id": f"agent-{machine}",
            "components": [_exact_result(name) for name in ("hub", "image", "voice")],
        }

    service, manifest = _execution_service(
        tmp_path,
        remote_bundle_runner=remote_bundle,
        component_runner=lambda *_args, **_kwargs: [
            _exact_result("image"), _exact_result("voice"),
        ],
        hub_runner=lambda *_args, **_kwargs: _exact_result("hub"),
    )
    initial_job_id = service.state_snapshot()["activation"]["job_id"]
    assert "mac-b" not in service.job_snapshot(initial_job_id)["machines"]

    base_task = service.schedule(manifest["release_id"])
    await mac_a_started.wait()
    disabled.clear()

    assert service.wake_registry() == 0
    allow_mac_a.set()
    base = await base_task
    assert base["state"] == "complete"

    async def supplemented():
        while True:
            active = service.state_snapshot()["activation"]
            job = service.job_snapshot(active["job_id"])
            if job["id"] != base["id"] and job["state"] == "complete":
                return job
            await asyncio.sleep(0)

    supplemental = await asyncio.wait_for(supplemented(), timeout=2)

    assert supplemental["release_id"] == base["release_id"]
    assert supplemental["supersedes_job_id"] == base["id"]
    assert supplemental["machines"]["mac-b"]["state"] == "current"
    assert calls == ["mac-a", "mac-b"]


@pytest.mark.asyncio
async def test_foreign_lease_registry_wake_waits_then_runs_once(
    tmp_path, monkeypatch,
):
    from backend import registry
    from backend.release_reconciliation import ReleaseReconciler

    disabled = {"mac-b"}
    monkeypatch.setattr(
        registry, "machine_enabled", lambda machine: machine not in disabled,
    )
    state_path = tmp_path / "release_reconciliation.json"
    calls = []

    async def remote_bundle(machine, _body, _existing_job_id):
        calls.append(machine)
        return {
            "job_id": f"agent-{machine}",
            "components": [_exact_result(name) for name in ("hub", "image", "voice")],
        }

    options = {
        "state_path": state_path,
        "clock": Clock(),
        "peer_reader": lambda _machine: {
            "reachable": True, "auth": True, "status": "connected",
        },
        "lease_seconds": 30,
        "heartbeat_seconds": 5,
        "poll_seconds": 0.001,
        "owner_alive": lambda _pid: True,
        "remote_bundle_runner": remote_bundle,
        "component_runner": lambda *_args, **_kwargs: [
            _exact_result("image"), _exact_result("voice"),
        ],
        "hub_runner": lambda *_args, **_kwargs: _exact_result("hub"),
    }
    owner_a = ReleaseReconciler(
        ExecutionMonitor(), owner_id="owner-a", pid=101, **options,
    )
    manifest = _manifest()
    owner_a.replace_intent(manifest)
    owner_a.activate(manifest["release_id"], genstudio_run_reference=None)
    assert owner_a.resume_pending() == 1

    owner_b = ReleaseReconciler(
        ExecutionMonitor(), owner_id="owner-b", pid=202, **options,
    )
    disabled.clear()

    assert owner_b.wake_registry() == 0
    wake = owner_b._registry_recovery_task
    assert wake is not None
    assert owner_b.wake_registry() == 0
    assert owner_b._registry_recovery_task is wake
    await asyncio.sleep(0.01)
    assert wake.done() is False
    assert owner_b.reconcile_registry() == 0

    assert owner_a.release_adoption() is True
    await asyncio.wait_for(asyncio.shield(wake), timeout=2)

    active = owner_b.state_snapshot()["activation"]
    completed = owner_b.job_snapshot(active["job_id"])
    assert completed["release_id"] == manifest["release_id"]
    assert completed["supplement_generation"] == 1
    assert completed["machines"]["mac-b"]["state"] == "current"
    assert completed["state"] == "complete"
    assert calls == ["mac-a", "mac-b"]
    assert owner_b._registry_recovery_task is None


@pytest.mark.asyncio
async def test_target_local_pending_does_not_block_later_machine(tmp_path):
    events = []

    async def remote_bundle(machine, _body, _existing_job_id):
        events.append(machine)
        if machine == "mac-a":
            return {
                "job_id": "agent-mac-a",
                "components": [
                    _exact_result("hub", "pending_offline", error_code="offline"),
                    _exact_result("image", "pending_busy", error_code="busy"),
                    _exact_result("voice", "auth_blocked", error_code="auth_rejected"),
                ],
            }
        return {
            "job_id": "agent-mac-b",
            "components": [_exact_result(name) for name in ("hub", "image", "voice")],
        }

    async def local_components(_monitor, _manifest, *, operation_id):
        del operation_id
        return [_exact_result("image"), _exact_result("voice")]

    async def local_hub(_target, _operation_id):
        return _exact_result("hub")

    service, manifest = _execution_service(
        tmp_path,
        remote_bundle_runner=remote_bundle,
        component_runner=local_components,
        hub_runner=local_hub,
    )

    job = await service.run(manifest["release_id"])

    assert events == ["mac-a", "mac-b"]
    assert job["state"] == "degraded"
    assert job["finished_at"] is None
    assert job["machines"]["mac-a"]["components"]["hub"]["next_retry"] is not None
    assert job["machines"]["mac-b"]["components"]["hub"]["state"] == "current"


@pytest.mark.asyncio
async def test_second_distinct_clean_health_failure_blocks_remaining_fanout(tmp_path):
    calls = []

    async def remote_bundle(machine, _body, _existing_job_id):
        calls.append(machine)
        return {
            "job_id": f"agent-{machine}",
            "components": [
                _exact_result(
                    "hub", "retryable_failure",
                    error_code="clean_checkout_health_failure",
                ),
                _exact_result("image"),
                _exact_result("voice"),
            ],
        }

    service, manifest = _execution_service(
        tmp_path,
        remote_bundle_runner=remote_bundle,
        component_runner=lambda *_args, **_kwargs: pytest.fail("local bundle must not run"),
        hub_runner=lambda *_args, **_kwargs: pytest.fail("local Hub must not run"),
    )

    job = await service.run(manifest["release_id"])

    assert calls == ["mac-a", "mac-b"]
    assert job["state"] == "blocked_release"
    remaining = job["machines"]["local"]["components"].values()
    assert all(row["state"] in {"not_installed", "release_blocked"} for row in remaining)


@pytest.mark.asyncio
async def test_real_sibling_adapter_clean_failures_block_before_controller(
    tmp_path, monkeypatch,
):
    from backend import fleet_auto_updates

    class Response:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

        def raise_for_status(self):
            return None

    class Client:
        polls = {}

        def __init__(self, *args, **kwargs):
            del args, kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            del args

        async def post(self, *args, **kwargs):
            del args, kwargs
            return Response({"state": "updating"})

        async def get(self, url, **kwargs):
            del kwargs
            key = url.split(":47868", 1)[0]
            self.polls[key] = self.polls.get(key, 0) + 1
            if self.polls[key] == 1:
                return Response({
                    "state": "idle",
                    "capabilities": {"managed_exact_commit": True},
                })
            return Response({
                "state": "failed",
                "capabilities": {"managed_exact_commit": True},
                "details": [
                    "The loaded app does not attest to the requested commit and version."
                ],
            })

    monkeypatch.setattr(fleet_auto_updates.httpx, "AsyncClient", Client)
    calls = []

    async def remote_bundle(machine, body, _existing_job_id):
        calls.append(machine)
        host = "10.0.0.2" if machine == "mac-a" else "10.0.0.3"
        monitor = type("OneImage", (), {"registry": [{
            "id": f"image@{machine}", "title": "Image", "modality": "image",
            "machine": machine, "host": host, "port": 47868,
        }]})()
        image = await fleet_auto_updates.run_managed_components(
            monitor, body, operation_id=body["operation_id"],
            poll_seconds=0, update_timeout=1,
        )
        return {
            "job_id": f"agent-{machine}",
            "components": [
                _exact_result("hub"), image[0], _exact_result("voice"),
            ],
        }

    service, manifest = _execution_service(
        tmp_path,
        remote_bundle_runner=remote_bundle,
        component_runner=lambda *_args, **_kwargs: pytest.fail(
            "controller must not run after the second clean failure"
        ),
        hub_runner=lambda *_args, **_kwargs: pytest.fail(
            "controller Hub must not run after the second clean failure"
        ),
    )

    job = await service.run(manifest["release_id"])

    assert calls == ["mac-a", "mac-b"]
    assert job["state"] == "blocked_release"
    assert [
        job["machines"][machine]["components"]["image"]["error_code"]
        for machine in ("mac-a", "mac-b")
    ] == ["clean_checkout_health_failure"] * 2


@pytest.mark.asyncio
async def test_wrong_exact_commit_blocks_release_before_later_fanout(tmp_path):
    calls = []

    async def remote_bundle(machine, _body, _existing_job_id):
        calls.append(machine)
        return {
            "job_id": f"agent-{machine}",
            "components": [
                _exact_result("hub", observed_commit="d" * 40),
                _exact_result("image"),
                _exact_result("voice"),
            ],
        }

    service, manifest = _execution_service(
        tmp_path,
        remote_bundle_runner=remote_bundle,
        component_runner=lambda *_args, **_kwargs: pytest.fail("local bundle must not run"),
        hub_runner=lambda *_args, **_kwargs: pytest.fail("local Hub must not run"),
    )

    job = await service.run(manifest["release_id"])

    assert calls == ["mac-a"]
    assert job["state"] == "blocked_release"


@pytest.mark.asyncio
async def test_executor_heartbeats_and_stops_when_lease_is_lost(tmp_path):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def remote_bundle(_machine, _body, _existing_job_id):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    service, manifest = _execution_service(
        tmp_path,
        remote_bundle_runner=remote_bundle,
        heartbeat_seconds=0.01,
    )
    original_refresh = service.refresh_adoption
    refreshes = 0

    def lose_after_first(job_id=None, *, generation=None):
        nonlocal refreshes
        refreshes += 1
        return original_refresh(job_id, generation=generation) if refreshes == 1 else False

    service.refresh_adoption = lose_after_first
    task = asyncio.create_task(service.run(manifest["release_id"]))
    await started.wait()

    with pytest.raises(RuntimeError, match="lease"):
        await asyncio.wait_for(task, timeout=1)
    assert cancelled.is_set()
    assert refreshes >= 2


@pytest.mark.asyncio
async def test_same_owner_concurrent_run_adopts_one_controller_execution(tmp_path):
    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def remote_bundle(machine, _body, _existing_job_id):
        nonlocal calls
        calls += 1
        if machine == "mac-a":
            started.set()
            await release.wait()
        return {
            "job_id": f"agent-{machine}",
            "components": [_exact_result(name) for name in ("hub", "image", "voice")],
        }

    async def local_components(_monitor, _manifest, *, operation_id):
        del operation_id
        return [_exact_result("image"), _exact_result("voice")]

    async def local_hub(_target, _operation_id):
        return _exact_result("hub")

    service, manifest = _execution_service(
        tmp_path,
        remote_bundle_runner=remote_bundle,
        component_runner=local_components,
        hub_runner=local_hub,
    )
    first = asyncio.create_task(service.run(manifest["release_id"]))
    await started.wait()
    second = asyncio.create_task(service.run(manifest["release_id"]))
    await asyncio.sleep(0)
    release.set()

    one, two = await asyncio.gather(first, second)

    assert one == two
    assert calls == 2  # one call for each of the two remote machines


@pytest.mark.asyncio
async def test_lost_remote_admission_response_replays_once_and_persists_before_poll(
    tmp_path, monkeypatch,
):
    import httpx
    from backend import release_reconciliation

    service, manifest = _execution_service(tmp_path, poll_seconds=0.001)
    job_id = service.state_snapshot()["activation"]["job_id"]
    assert service.resume_pending() == 1
    fence = _raw_state(tmp_path)["jobs"][job_id]["adoption_lease"]["generation"]
    expected_child = release_reconciliation._agent_job_id(
        service.job_snapshot(job_id)["machines"]["mac-a"]["operation_id"]
    )

    class Response:
        def __init__(self, data):
            self.data = data
            self.status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return self.data

    class Client:
        posts = []
        executions = 0
        persisted_before_poll = False

        def __init__(self, *args, **kwargs):
            del args, kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            del args

        async def post(self, _url, *, headers, json):
            del headers
            self.posts.append(deepcopy(json))
            if len(self.posts) == 1:
                type(self).executions += 1
                raise httpx.ReadError("response lost")
            return Response({"job_id": expected_child, "adopted": True})

        async def get(self, _url, *, headers):
            del headers
            type(self).persisted_before_poll = (
                service.job_snapshot(job_id)["machines"]["mac-a"]["agent_job_id"] == expected_child
            )
            return Response({
                "state": "complete",
                "components": [_exact_result(name) for name in ("hub", "image", "voice")],
            })

    monkeypatch.setattr(release_reconciliation.httpx, "AsyncClient", Client)

    blocked = await service._run_remote_machine(job_id, "mac-a", manifest, fence=fence)

    assert blocked is False
    assert Client.posts[0] == Client.posts[1]
    assert Client.executions == 1
    assert Client.persisted_before_poll is True
    assert Client.posts[0]["operation_id"] == service.job_snapshot(job_id)["machines"]["mac-a"]["operation_id"]


@pytest.mark.asyncio
async def test_controller_hub_restart_attests_without_replaying_and_requests_catalog(tmp_path):
    hub_calls = []
    catalog_calls = []

    async def remote_bundle(machine, _body, _existing_job_id):
        return {
            "job_id": f"agent-{machine}",
            "components": [_exact_result(name) for name in ("hub", "image", "voice")],
        }

    async def local_components(_monitor, _manifest, *, operation_id):
        del operation_id
        return [_exact_result("image"), _exact_result("voice")]

    async def restarting_hub(_target, _operation_id):
        hub_calls.append("update")
        return _exact_result("hub", "restarting")

    first, manifest = _execution_service(
        tmp_path,
        remote_bundle_runner=remote_bundle,
        component_runner=local_components,
        hub_runner=restarting_hub,
    )
    first_job = await first.run(manifest["release_id"])
    assert first_job["machines"]["local"]["components"]["hub"]["state"] == "restarting"

    restarted = type(first)(
        ExecutionMonitor(),
        state_path=tmp_path / "release_reconciliation.json",
        clock=first._clock,
        peer_reader=first._peer_reader,
        remote_bundle_runner=remote_bundle,
        component_runner=local_components,
        hub_runner=restarting_hub,
        loaded_version=COMPONENTS["hub"]["version"],
        loaded_commit=COMPONENTS["hub"]["commit"],
        catalog_requester=lambda _operation_id: catalog_calls.append("catalog"),
        lease_seconds=30,
        heartbeat_seconds=5,
    )

    current = await restarted.run(manifest["release_id"])

    assert current["state"] == "complete"
    assert hub_calls == ["update"]
    assert catalog_calls == ["catalog"]
    assert current["catalog"]["requested_at"] is not None


def test_peer_recovery_marks_only_known_target_due_now(tmp_path):
    clock = Clock()
    service, manifest = _execution_service(tmp_path, clock=clock)
    job_id = service.activate(manifest["release_id"], genstudio_run_reference=None)["id"]
    service.record_component(job_id, "mac-a", "hub", state="pending_offline")
    service.record_component(job_id, "mac-b", "hub", state="pending_offline")

    assert service.note_peer_recovered("unknown") == 0
    assert service.note_peer_recovered("mac-a") == 1
    job = service.job_snapshot(job_id)
    assert job["machines"]["mac-a"]["components"]["hub"]["next_retry"] == clock()
    assert job["machines"]["mac-b"]["components"]["hub"]["next_retry"] > clock()


@pytest.mark.asyncio
async def test_agent_admission_is_durable_idempotent_and_executes_one_serial_bundle(tmp_path):
    events = []

    async def hub_runner(_target, _operation_id):
        events.append("hub")
        await asyncio.sleep(0)
        return _exact_result("hub")

    async def component_runner(_monitor, _manifest, *, operation_id):
        del operation_id
        events.extend(("image", "voice"))
        return [_exact_result("image"), _exact_result("voice")]

    service, manifest = _execution_service(
        tmp_path,
        hub_runner=hub_runner,
        component_runner=component_runner,
    )
    body = {
        "release_id": manifest["release_id"],
        "operation_id": service.job_snapshot(
            service.state_snapshot()["activation"]["job_id"]
        )["machines"]["mac-a"]["operation_id"],
        "components": deepcopy(manifest["components"]),
    }

    first = service.admit_managed_update(body)
    assert first["adopted"] is False
    assert service.managed_update_snapshot(first["job_id"])["state"] == "queued"
    restarted = type(service)(
        ExecutionMonitor(),
        state_path=tmp_path / "release_reconciliation.json",
        clock=service._clock,
        peer_reader=service._peer_reader,
        hub_runner=hub_runner,
        component_runner=component_runner,
        lease_seconds=30,
        heartbeat_seconds=5,
    )
    replay = restarted.admit_managed_update(deepcopy(body))
    assert replay == {"job_id": first["job_id"], "adopted": True}

    one, two = await asyncio.gather(
        restarted.run_managed_update(first["job_id"]),
        restarted.run_managed_update(first["job_id"]),
    )

    assert one == two
    assert one["state"] == "complete"
    assert events == ["hub", "image", "voice"]


@pytest.mark.asyncio
async def test_two_agent_instances_claim_one_durable_child_execution(tmp_path):
    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def hub_runner(_target, _operation_id):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return _exact_result("hub")

    async def component_runner(_monitor, _manifest, *, operation_id):
        del operation_id
        return [_exact_result("image"), _exact_result("voice")]

    first, manifest = _execution_service(
        tmp_path,
        owner_id="agent-owner-a",
        pid=101,
        owner_alive=lambda _pid: True,
        hub_runner=hub_runner,
        component_runner=component_runner,
    )
    body = {
        "release_id": manifest["release_id"],
        "operation_id": first.job_snapshot(
            first.state_snapshot()["activation"]["job_id"]
        )["machines"]["mac-a"]["operation_id"],
        "components": deepcopy(manifest["components"]),
    }
    child = first.admit_managed_update(body)
    second = type(first)(
        ExecutionMonitor(),
        state_path=tmp_path / "release_reconciliation.json",
        clock=first._clock,
        peer_reader=first._peer_reader,
        owner_id="agent-owner-b",
        pid=202,
        owner_alive=lambda _pid: True,
        hub_runner=hub_runner,
        component_runner=component_runner,
        lease_seconds=30,
        heartbeat_seconds=5,
    )

    running = asyncio.create_task(first.run_managed_update(child["job_id"]))
    await started.wait()
    adopted = await second.run_managed_update(child["job_id"])
    assert adopted["state"] == "running"
    release.set()
    completed = await running

    assert completed["state"] == "complete"
    assert calls == 1


@pytest.mark.asyncio
async def test_agent_accepts_task3_managed_hub_item_result_shape(tmp_path):
    async def hub_runner(target, _operation_id):
        return {
            "status": "complete",
            "to_version": target["version"],
            "target_commit": target["commit"],
        }

    async def component_runner(_monitor, _manifest, *, operation_id):
        del operation_id
        return [_exact_result("image"), _exact_result("voice")]

    service, manifest = _execution_service(
        tmp_path,
        hub_runner=hub_runner,
        component_runner=component_runner,
    )
    body = {
        "release_id": manifest["release_id"],
        "operation_id": service.job_snapshot(
            service.state_snapshot()["activation"]["job_id"]
        )["machines"]["mac-a"]["operation_id"],
        "components": deepcopy(manifest["components"]),
    }
    child = service.admit_managed_update(body)

    result = await service.run_managed_update(child["job_id"])

    assert result["state"] == "complete"


def test_agent_same_operation_rejects_changed_exact_bundle(tmp_path):
    service, manifest = _execution_service(tmp_path)
    body = {
        "release_id": manifest["release_id"],
        "operation_id": service.job_snapshot(
            service.state_snapshot()["activation"]["job_id"]
        )["machines"]["mac-a"]["operation_id"],
        "components": deepcopy(manifest["components"]),
    }
    service.admit_managed_update(body)
    changed = deepcopy(body)
    changed["components"]["voice"]["commit"] = "d" * 40

    with pytest.raises(ValueError, match="operation"):
        service.admit_managed_update(changed)


@pytest.mark.asyncio
async def test_expired_site_owner_is_fenced_after_takeover(tmp_path):
    clock = Clock()
    old_started = asyncio.Event()
    release_old = asyncio.Event()
    old_calls = []
    new_calls = []

    async def old_remote(machine, _body, _child):
        old_calls.append(machine)
        old_started.set()
        await release_old.wait()
        return {"job_id": f"agent-{machine}", "components": [
            _exact_result(name) for name in ("hub", "image", "voice")
        ]}

    async def new_remote(machine, _body, _child):
        new_calls.append(machine)
        return {"job_id": f"agent-{machine}", "components": [
            _exact_result(name) for name in ("hub", "image", "voice")
        ]}

    old, manifest = _execution_service(
        tmp_path, clock=clock, owner_id="old-site", pid=101,
        owner_alive=lambda _pid: True, lease_seconds=30, heartbeat_seconds=5,
        remote_bundle_runner=old_remote,
    )
    old_task = asyncio.create_task(old.run(manifest["release_id"]))
    await old_started.wait()
    clock.advance(31)
    new = type(old)(
        ExecutionMonitor(), state_path=old.state_path, clock=clock,
        peer_reader=old._peer_reader, owner_id="new-site", pid=202,
        owner_alive=lambda _pid: True, lease_seconds=30, heartbeat_seconds=5,
        remote_bundle_runner=new_remote,
        component_runner=lambda *_args, **_kwargs: [
            _exact_result("image"), _exact_result("voice")
        ],
        hub_runner=lambda *_args: _exact_result("hub"),
    )
    current = await new.run(manifest["release_id"])
    release_old.set()

    with pytest.raises(RuntimeError, match="lease"):
        await old_task
    assert current["state"] == "complete"
    assert old_calls == ["mac-a"]
    assert new_calls == ["mac-a", "mac-b"]


@pytest.mark.asyncio
async def test_expired_agent_owner_is_fenced_after_takeover(tmp_path):
    clock = Clock()
    old_started = asyncio.Event()
    release_old = asyncio.Event()
    old_components = []

    async def old_hub(_target, _operation_id):
        old_started.set()
        await release_old.wait()
        return _exact_result("hub")

    async def old_component(*_args, **_kwargs):
        old_components.append("ran")
        return [_exact_result("image"), _exact_result("voice")]

    old, manifest = _execution_service(
        tmp_path, clock=clock, owner_id="old-agent", pid=101,
        owner_alive=lambda _pid: True, lease_seconds=30, heartbeat_seconds=5,
        hub_runner=old_hub, component_runner=old_component,
    )
    body = {
        "release_id": manifest["release_id"],
        "operation_id": old.job_snapshot(old.state_snapshot()["activation"]["job_id"])
        ["machines"]["mac-a"]["operation_id"],
        "components": deepcopy(manifest["components"]),
    }
    child = old.admit_managed_update(body)
    old_task = asyncio.create_task(old.run_managed_update(child["job_id"]))
    await old_started.wait()
    clock.advance(31)
    new = type(old)(
        ExecutionMonitor(), state_path=old.state_path, clock=clock,
        peer_reader=old._peer_reader, owner_id="new-agent", pid=202,
        owner_alive=lambda _pid: True, lease_seconds=30, heartbeat_seconds=5,
        hub_runner=lambda *_args: _exact_result("hub"),
        component_runner=lambda *_args, **_kwargs: [
            _exact_result("image"), _exact_result("voice")
        ],
    )
    current = await new.run_managed_update(child["job_id"])
    release_old.set()

    with pytest.raises(RuntimeError, match="lease"):
        await old_task
    assert current["state"] == "complete"
    assert old_components == []


@pytest.mark.parametrize("error_code", sorted(set([
    "offline", "busy", "auth_rejected", "updater_unavailable",
    "transport_unavailable", "health_mismatch", "update_refused",
    "identity_mismatch", "invalid_evidence", "unknown_failure",
    "manifest_mismatch", "sha_mismatch",
])))
@pytest.mark.asyncio
async def test_nonapproved_agent_block_codes_remain_target_local(tmp_path, error_code):
    calls = []

    async def remote(machine, _body, _child):
        calls.append(machine)
        hub = (_exact_result("hub", "release_blocked", error_code=error_code)
               if machine == "mac-a" else _exact_result("hub"))
        return {"job_id": f"agent-{machine}", "components": [
            hub, _exact_result("image"), _exact_result("voice"),
        ]}

    service, manifest = _execution_service(
        tmp_path, remote_bundle_runner=remote,
        component_runner=lambda *_args, **_kwargs: [
            _exact_result("image"), _exact_result("voice")
        ],
        hub_runner=lambda *_args: _exact_result("hub"),
    )
    job = await service.run(manifest["release_id"])

    assert calls == ["mac-a", "mac-b"]
    assert job["state"] == "degraded"
    assert job["machines"]["mac-a"]["components"]["hub"]["state"] == "retryable_failure"


@pytest.mark.asyncio
async def test_due_scan_during_active_pass_preserves_future_retry(tmp_path):
    clock = Clock()
    later_started = asyncio.Event()
    release_later = asyncio.Event()

    async def remote(machine, _body, _child):
        if machine == "mac-a":
            return {"job_id": "agent-mac-a", "components": [
                _exact_result(name, "pending_offline", error_code="offline")
                for name in ("hub", "image", "voice")
            ]}
        later_started.set()
        await release_later.wait()
        return {"job_id": "agent-mac-b", "components": [
            _exact_result(name) for name in ("hub", "image", "voice")
        ]}

    service, manifest = _execution_service(
        tmp_path, clock=clock, remote_bundle_runner=remote,
        component_runner=lambda *_args, **_kwargs: [],
        lease_seconds=300, heartbeat_seconds=30,
    )
    task = asyncio.create_task(service.run(manifest["release_id"]))
    await later_started.wait()
    clock.advance(61)
    assert service.resume_due() == 3
    held = service.job_snapshot(service.state_snapshot()["activation"]["job_id"])
    assert held["machines"]["mac-a"]["components"]["hub"]["state"] == "pending_offline"
    assert held["machines"]["mac-a"]["components"]["hub"]["next_retry"] <= clock()
    release_later.set()
    await task


@pytest.mark.asyncio
async def test_peer_recovery_during_active_pass_preserves_future_retry(tmp_path):
    later_started = asyncio.Event()
    release_later = asyncio.Event()

    async def remote(machine, _body, _child):
        if machine == "mac-a":
            return {"job_id": "agent-mac-a", "components": [
                _exact_result(name, "pending_offline", error_code="offline")
                for name in ("hub", "image", "voice")
            ]}
        later_started.set()
        await release_later.wait()
        return {"job_id": "agent-mac-b", "components": [
            _exact_result(name) for name in ("hub", "image", "voice")
        ]}

    service, manifest = _execution_service(
        tmp_path, remote_bundle_runner=remote,
        component_runner=lambda *_args, **_kwargs: [],
    )
    task = asyncio.create_task(service.run(manifest["release_id"]))
    await later_started.wait()
    assert service.note_peer_recovered("mac-a") == 3
    held = service.job_snapshot(service.state_snapshot()["activation"]["job_id"])
    assert held["machines"]["mac-a"]["components"]["hub"]["state"] == "pending_offline"
    assert held["machines"]["mac-a"]["components"]["hub"]["next_retry"] == service._clock()
    release_later.set()
    await task


@pytest.mark.parametrize(
    "case", ["partial", "unknown", "duplicate", "installed_mismatch", "missing_attestation",
             "dict_shape"],
)
@pytest.mark.asyncio
async def test_malformed_terminal_child_evidence_is_target_local(tmp_path, case):
    calls = []

    async def remote(machine, _body, _child):
        calls.append(machine)
        rows = [_exact_result(name) for name in ("hub", "image", "voice")]
        if machine == "mac-a":
            if case == "partial":
                rows.pop()
            elif case == "unknown":
                rows[-1]["component"] = "chat"
            elif case == "duplicate":
                rows[-1]["component"] = "image"
            elif case == "installed_mismatch":
                rows[-1] = {"component": "voice", "state": "not_installed"}
            elif case == "dict_shape":
                rows = {row["component"]: row for row in rows}
            else:
                rows[0].pop("observed_version")
                rows[0].pop("observed_commit")
        return {"job_id": f"agent-{machine}", "state": "complete", "components": rows}

    service, manifest = _execution_service(
        tmp_path, remote_bundle_runner=remote,
        component_runner=lambda *_args, **_kwargs: [
            _exact_result("image"), _exact_result("voice")
        ],
        hub_runner=lambda *_args: _exact_result("hub"),
    )
    job = await service.run(manifest["release_id"])

    assert calls == ["mac-a", "mac-b"]
    assert job["state"] == "degraded"
    assert job["machines"]["mac-a"]["components"]["voice"]["error_code"] == "invalid_evidence"


@pytest.mark.asyncio
async def test_terminal_evidence_for_uninstalled_component_is_rejected(tmp_path):
    from backend.release_reconciliation import ReleaseReconciler

    registry = [
        row for row in ExecutionMonitor.registry
        if row["id"] != "voice@mac-a"
    ]
    monitor = type("NoRemoteVoiceMonitor", (), {"registry": registry})()
    calls = []

    async def remote(machine, _body, _child):
        calls.append(machine)
        return {"job_id": f"agent-{machine}", "state": "complete", "components": [
            _exact_result(name) for name in ("hub", "image", "voice")
        ]}

    service = ReleaseReconciler(
        monitor, state_path=tmp_path / "release_reconciliation.json",
        clock=Clock(), peer_reader=lambda machine: {
            "reachable": True, "auth": True, "status": "connected",
        } if machine != "local" else None,
        remote_bundle_runner=remote,
        component_runner=lambda *_args, **_kwargs: [
            _exact_result("image"), _exact_result("voice")
        ],
        hub_runner=lambda *_args: _exact_result("hub"),
        lease_seconds=30, heartbeat_seconds=5,
    )
    manifest = _manifest()
    service.replace_intent(manifest)
    service.activate(manifest["release_id"], genstudio_run_reference=None)
    job = await service.run(manifest["release_id"])

    assert calls == ["mac-a", "mac-b"]
    assert job["machines"]["mac-a"]["components"]["hub"]["error_code"] == "invalid_evidence"


@pytest.mark.asyncio
async def test_degraded_remote_retry_readmits_same_child_once(tmp_path, monkeypatch):
    from backend import release_reconciliation

    clock = Clock()
    service, manifest = _execution_service(
        tmp_path, clock=clock, poll_seconds=0.001,
        component_runner=lambda *_args, **_kwargs: [
            _exact_result("image"), _exact_result("voice")
        ],
        hub_runner=lambda *_args: _exact_result("hub"),
    )

    class Response:
        status_code = 200

        def __init__(self, value):
            self.value = value

        def raise_for_status(self):
            return None

        def json(self):
            return self.value

    class Client:
        posts = {}
        bodies = {}

        def __init__(self, *args, **kwargs):
            del args, kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            del args

        async def post(self, url, *, headers, json):
            del headers
            operation = json["operation_id"]
            self.posts[operation] = self.posts.get(operation, 0) + 1
            self.bodies.setdefault(operation, []).append(deepcopy(json))
            child = release_reconciliation._agent_job_id(operation)
            return Response({
                "job_id": child,
                "adopted": self.posts[operation] > 1,
            })

        async def get(self, url, *, headers):
            del headers
            machine_a = "10.0.0.2" in url
            operation = service.job_snapshot(
                service.state_snapshot()["activation"]["job_id"]
            )["machines"]["mac-a"]["operation_id"]
            degraded = machine_a and self.posts.get(operation) == 1
            rows = [
                _exact_result(
                    name,
                    "retryable_failure" if degraded else "current",
                    **({"error_code": "unknown_failure"} if degraded else {}),
                )
                for name in ("hub", "image", "voice")
            ]
            return Response({"state": "degraded" if degraded else "complete", "components": rows})

    monkeypatch.setattr(release_reconciliation.httpx, "AsyncClient", Client)
    first = await service.run(manifest["release_id"])
    operation = first["machines"]["mac-a"]["operation_id"]
    child = first["machines"]["mac-a"]["agent_job_id"]
    assert first["state"] == "degraded"

    clock.advance(61)
    assert service.resume_due() == 3
    current = await service.run(manifest["release_id"])

    assert current["state"] == "complete"
    assert current["machines"]["mac-a"]["agent_job_id"] == child
    assert Client.posts[operation] == 2
    assert Client.bodies[operation][0] == Client.bodies[operation][1]


@pytest.mark.asyncio
async def test_remote_replay_requires_adopted_acknowledgement(tmp_path, monkeypatch):
    from backend import release_reconciliation

    service, manifest = _execution_service(tmp_path)
    job_id = service.state_snapshot()["activation"]["job_id"]
    machine = service.job_snapshot(job_id)["machines"]["mac-a"]
    child = release_reconciliation._agent_job_id(machine["operation_id"])

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"job_id": child, "adopted": False}

    class Client:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            del args

        async def post(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(release_reconciliation.httpx, "AsyncClient", Client)
    body = {
        "release_id": manifest["release_id"],
        "operation_id": machine["operation_id"],
        "components": deepcopy(manifest["components"]),
    }

    with pytest.raises(ValueError, match="not adopted"):
        await service._request_remote_bundle("mac-a", body, child)


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", [
    {"role": "controller", "site_id": "site-a", "controller_id": "controller-a"},
    {"role": "agent", "site_id": "site-b", "controller_id": "controller-a"},
    {"role": "agent", "site_id": "site-a", "controller_id": "controller-a"},
    {"role": "agent", "site_id": "site-a", "controller_id": "mac-z"},
    {"role": "agent", "site_id": "site-a"},
])
async def test_remote_admission_identity_mismatch_is_quarantined(
    tmp_path, monkeypatch, identity,
):
    from backend import release_reconciliation

    service, manifest = _execution_service(
        tmp_path,
        identity_reader=lambda: {
            "role": "controller", "site_id": "site-a", "controller_id": "controller-a",
        },
    )
    job_id = service.state_snapshot()["activation"]["job_id"]
    machine = service.job_snapshot(job_id)["machines"]["mac-a"]
    child = release_reconciliation._agent_job_id(machine["operation_id"])

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"job_id": child, "adopted": False, **identity}

    class Client:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            del args

        async def post(self, *_args, **_kwargs):
            return Response()

        async def get(self, *_args, **_kwargs):
            pytest.fail("identity mismatch must stop before polling")

    monkeypatch.setattr(release_reconciliation.httpx, "AsyncClient", Client)
    body = {
        "release_id": manifest["release_id"],
        "operation_id": machine["operation_id"],
        "components": deepcopy(manifest["components"]),
    }

    result = await service._request_remote_bundle("mac-a", body, None)

    assert result == {
        "state": "auth_blocked",
        "error_code": "identity_mismatch",
    }


@pytest.mark.asyncio
async def test_remote_admission_and_poll_accept_real_joined_agent_identity(
    tmp_path, monkeypatch, reset,
):
    from backend import control_plane, enrollment, release_reconciliation

    monkeypatch.setattr(enrollment, "suggested_local_hub_id", lambda _profile: "mac-a")
    joined = enrollment.configure_joined_agent(
        "http://100.70.0.2:47873",
        "mac-mini-m2-8gb",
        {
            "site_id": "terranash-kts",
            "site_name": "KTS",
            "controller_id": "terranash-0200",
            "fleet_token": "site-fleet-token-123",
        },
    )["settings"]
    assert joined["role"] == "agent"
    assert joined["controller_id"] == "mac-a"
    control_plane.save_settings({
        "role": "controller",
        "site_id": "terranash-kts",
        "site_name": "KTS",
        "controller_id": "terranash-0200",
    })
    service, manifest = _execution_service(
        tmp_path,
        identity_reader=control_plane.public_settings,
    )
    job_id = service.state_snapshot()["activation"]["job_id"]
    machine = service.job_snapshot(job_id)["machines"]["mac-a"]
    child = release_reconciliation._agent_job_id(machine["operation_id"])

    class Response:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class Client:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            del args

        async def post(self, *_args, **_kwargs):
            return Response({
                "job_id": child,
                "adopted": False,
                "role": joined["role"],
                "site_id": joined["site_id"],
                "controller_id": joined["controller_id"],
            })

        async def get(self, *_args, **_kwargs):
            return Response({
                "job_id": child,
                "state": "complete",
                "components": [
                    _exact_result(name) for name in ("hub", "image", "voice")
                ],
                "role": joined["role"],
                "site_id": joined["site_id"],
                "controller_id": joined["controller_id"],
            })

    monkeypatch.setattr(release_reconciliation.httpx, "AsyncClient", Client)
    body = {
        "release_id": manifest["release_id"],
        "operation_id": machine["operation_id"],
        "components": deepcopy(manifest["components"]),
    }

    result = await service._request_remote_bundle("mac-a", body, None)

    assert result["state"] == "complete"
    assert result["job_id"] == child


@pytest.mark.asyncio
async def test_remote_poll_identity_change_is_quarantined_after_child_persist(
    tmp_path, monkeypatch,
):
    from backend import release_reconciliation

    service, manifest = _execution_service(
        tmp_path,
        identity_reader=lambda: {
            "role": "controller", "site_id": "site-a", "controller_id": "controller-a",
        },
    )
    job_id = service.state_snapshot()["activation"]["job_id"]
    machine = service.job_snapshot(job_id)["machines"]["mac-a"]
    child = release_reconciliation._agent_job_id(machine["operation_id"])
    persisted = []

    class Response:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class Client:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            del args

        async def post(self, *_args, **_kwargs):
            return Response({
                "job_id": child, "adopted": False,
                "role": "agent", "site_id": "site-a", "controller_id": "mac-a",
            })

        async def get(self, *_args, **_kwargs):
            return Response({
                "job_id": child, "state": "complete", "components": {},
                "role": "agent", "site_id": "site-a", "controller_id": "controller-a",
            })

    monkeypatch.setattr(release_reconciliation.httpx, "AsyncClient", Client)
    body = {
        "release_id": manifest["release_id"],
        "operation_id": machine["operation_id"],
        "components": deepcopy(manifest["components"]),
    }

    result = await service._request_remote_bundle(
        "mac-a", body, None, persist_child=persisted.append,
    )

    assert persisted == [child]
    assert result == {
        "job_id": child,
        "state": "auth_blocked",
        "error_code": "identity_mismatch",
    }


@pytest.mark.asyncio
async def test_registry_growth_supplements_active_degraded_job_and_installed_component(
    tmp_path, monkeypatch,
):
    from backend import main
    from backend.release_reconciliation import ReleaseReconciler

    monitor = type("GrowingMonitor", (), {})()
    monitor.registry = [
        {"id": "image", "modality": "image", "machine": "local", "host": "127.0.0.1"},
        {"id": "voice", "modality": "voice", "machine": "local", "host": "127.0.0.1"},
        {"id": "image@mac-a", "modality": "image", "machine": "mac-a", "host": "10.0.0.2"},
    ]
    calls = []

    async def remote(machine, _body, _child):
        calls.append(machine)
        return {
            "job_id": f"agent-{machine}",
            "components": [_exact_result(name) for name in ("hub", "image", "voice")],
        }

    service = ReleaseReconciler(
        monitor,
        state_path=tmp_path / "release_reconciliation.json",
        clock=Clock(),
        peer_reader=lambda machine: {
            "reachable": True, "auth": True, "status": "connected",
        } if machine != "local" else None,
        remote_bundle_runner=remote,
        component_runner=lambda *_args, **_kwargs: [
            _exact_result("image"), _exact_result("voice"),
        ],
        hub_runner=lambda *_args, **_kwargs: _exact_result("hub"),
        lease_seconds=30,
        heartbeat_seconds=5,
    )
    manifest = _manifest()
    service.replace_intent(manifest)
    original = service.activate(manifest["release_id"], genstudio_run_reference=None)
    old_operation = original["machines"]["mac-a"]["operation_id"]
    service.persist_remote_job(original["id"], "mac-a", "agent-mac-a")
    service.record_component(
        original["id"], "mac-a", "image",
        state="pending_offline", error_code="offline",
    )
    monitor.registry.extend([
        {"id": "voice@mac-a", "modality": "voice", "machine": "mac-a", "host": "10.0.0.2"},
        {"id": "image@mac-c", "modality": "image", "machine": "mac-c", "host": "10.0.0.4"},
        {"id": "voice@mac-c", "modality": "voice", "machine": "mac-c", "host": "10.0.0.4"},
    ])

    monkeypatch.setattr(main, "release_reconciler", service)
    assert main._reconcile_managed_registry() == 2
    assert main._reconcile_managed_registry() == 0
    await asyncio.gather(*tuple(service._lifecycle_tasks))
    job = service.job_snapshot(original["id"])

    assert job["id"] == original["id"]
    assert job["state"] == "complete"
    assert job["supplement_generation"] == 1
    assert job["machines"]["mac-a"]["components"]["voice"]["installed"] is True
    assert job["machines"]["mac-a"]["operation_id"] != old_operation
    assert job["machines"]["mac-c"]["components"]["hub"]["expected_commit"] == "a" * 40
    assert calls == ["mac-a", "mac-c"]


@pytest.mark.asyncio
async def test_registry_growth_after_completion_creates_immutable_supplemental_job(
    tmp_path, monkeypatch,
):
    from backend import main
    from backend.release_reconciliation import ReleaseReconciler

    monitor = type("GrowingMonitor", (), {})()
    monitor.registry = [
        {"id": "image", "modality": "image", "machine": "local", "host": "127.0.0.1"},
        {"id": "voice", "modality": "voice", "machine": "local", "host": "127.0.0.1"},
        {"id": "image@mac-a", "modality": "image", "machine": "mac-a", "host": "10.0.0.2"},
        {"id": "voice@mac-a", "modality": "voice", "machine": "mac-a", "host": "10.0.0.2"},
    ]
    calls = []

    async def remote(machine, _body, _child):
        calls.append(machine)
        return {
            "job_id": f"agent-{machine}",
            "components": [_exact_result(name) for name in ("hub", "image", "voice")],
        }

    service = ReleaseReconciler(
        monitor,
        state_path=tmp_path / "release_reconciliation.json",
        clock=Clock(),
        peer_reader=lambda machine: {
            "reachable": True, "auth": True, "status": "connected",
        } if machine != "local" else None,
        remote_bundle_runner=remote,
        component_runner=lambda *_args, **_kwargs: [
            _exact_result("image"), _exact_result("voice"),
        ],
        hub_runner=lambda *_args, **_kwargs: _exact_result("hub"),
        lease_seconds=30,
        heartbeat_seconds=5,
    )
    manifest = _manifest()
    service.replace_intent(manifest)
    base = service.activate(manifest["release_id"], genstudio_run_reference="run-growth")
    _converge(service, base["id"])
    first = service.persist_job(base["id"], state="complete")
    assert first["state"] == "complete"
    base_snapshot = service.job_snapshot(base["id"])
    calls.clear()
    monitor.registry.extend([
        {"id": "image@mac-d", "modality": "image", "machine": "mac-d", "host": "10.0.0.5"},
        {"id": "voice@mac-d", "modality": "voice", "machine": "mac-d", "host": "10.0.0.5"},
    ])

    monkeypatch.setattr(main, "release_reconciler", service)
    assert main._reconcile_managed_registry() == 1
    assert main._reconcile_managed_registry() == 0
    await asyncio.gather(*tuple(service._lifecycle_tasks))
    supplement_id = service.state_snapshot()["activation"]["job_id"]
    supplement = service.job_snapshot(supplement_id)

    assert service.job_snapshot(base["id"]) == base_snapshot
    assert supplement["id"] != base["id"]
    assert supplement["supersedes_job_id"] == base["id"]
    assert supplement["supplement_generation"] == 1
    assert supplement["state"] == "complete"
    assert supplement["machines"]["mac-d"]["components"]["voice"]["state"] == "current"
    assert service.state_snapshot()["activation"]["job_id"] == supplement["id"]
    assert calls == ["mac-d"]


@pytest.mark.asyncio
async def test_agent_replay_schedules_one_retry_of_same_durable_child(tmp_path):
    hub_calls = 0

    async def hub(_target, _operation_id):
        nonlocal hub_calls
        hub_calls += 1
        if hub_calls == 1:
            return _exact_result(
                "hub", "retryable_failure", error_code="unknown_failure",
            )
        return _exact_result("hub")

    service, manifest = _execution_service(
        tmp_path, hub_runner=hub,
        component_runner=lambda *_args, **_kwargs: [
            _exact_result("image"), _exact_result("voice")
        ],
    )
    job_id = service.state_snapshot()["activation"]["job_id"]
    body = {
        "release_id": manifest["release_id"],
        "operation_id": service.job_snapshot(job_id)["machines"]["mac-a"]["operation_id"],
        "components": deepcopy(manifest["components"]),
    }
    first = service.admit_and_schedule_managed_update(body)
    await service.run_managed_update(first["job_id"])
    assert service.managed_update_snapshot(first["job_id"])["state"] == "degraded"

    replay = service.admit_and_schedule_managed_update(deepcopy(body))
    current = await service.run_managed_update(first["job_id"])

    assert replay == {"job_id": first["job_id"], "adopted": True}
    assert current["state"] == "complete"
    assert hub_calls == 2


@pytest.mark.asyncio
async def test_agent_target_local_block_and_missing_attestation_remain_reclaimable(tmp_path):
    calls = 0

    async def hub(_target, _operation_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _exact_result(
                "hub", "release_blocked", error_code="update_refused",
            )
        if calls == 2:
            return {"component": "hub", "state": "current"}
        return _exact_result("hub")

    service, manifest = _execution_service(
        tmp_path, hub_runner=hub,
        component_runner=lambda *_args, **_kwargs: [
            _exact_result("image"), _exact_result("voice")
        ],
    )
    site_job = service.state_snapshot()["activation"]["job_id"]
    body = {
        "release_id": manifest["release_id"],
        "operation_id": service.job_snapshot(site_job)["machines"]["mac-a"]["operation_id"],
        "components": deepcopy(manifest["components"]),
    }
    child = service.admit_and_schedule_managed_update(body)
    first = await service.run_managed_update(child["job_id"])
    assert first["state"] == "degraded"
    assert first["components"][0]["error_code"] == "update_refused"

    service.admit_and_schedule_managed_update(body)
    second = await service.run_managed_update(child["job_id"])
    assert second["state"] == "degraded"
    assert second["components"][0]["error_code"] == "invalid_evidence"

    service.admit_and_schedule_managed_update(body)
    current = await service.run_managed_update(child["job_id"])
    assert current["state"] == "complete"
    assert calls == 3


@pytest.mark.parametrize("local_result", [
    {"component": "image", "state": "current"},
    {"component": "image", "state": "release_blocked", "error_code": "update_refused"},
])
@pytest.mark.asyncio
async def test_controller_local_unvalidated_results_remain_retryable(tmp_path, local_result):
    async def remote(machine, _body, _child):
        return {"job_id": f"agent-{machine}", "components": [
            _exact_result(name) for name in ("hub", "image", "voice")
        ]}

    service, manifest = _execution_service(
        tmp_path, remote_bundle_runner=remote,
        component_runner=lambda *_args, **_kwargs: [
            local_result, _exact_result("voice"),
        ],
        hub_runner=lambda *_args: _exact_result("hub"),
    )
    job = await service.run(manifest["release_id"])

    assert job["state"] == "degraded"
    image = job["machines"]["local"]["components"]["image"]
    assert image["state"] == "retryable_failure"
    assert image["error_code"] == (
        "invalid_evidence" if local_result["state"] == "current" else "update_refused"
    )


def test_persist_complete_requires_real_catalog_acknowledgement(tmp_path):
    service = _service(tmp_path)
    manifest = _manifest()
    service.replace_intent(manifest)
    job = service.activate(manifest["release_id"], genstudio_run_reference=None)
    for machine in ("local", "mac-a"):
        for component in ("hub", "image", "voice"):
            _record_current(service, job["id"], machine, component)

    with pytest.raises(ValueError, match="catalog"):
        service.persist_job(job["id"], state="complete")
    assert service.job_snapshot(job["id"])["catalog"]["state"] == "pending"


@pytest.mark.asyncio
async def test_clean_failure_history_survives_recovery_and_blocks_second_machine(tmp_path):
    clock = Clock()
    calls = {"mac-a": 0, "mac-b": 0}

    async def remote(machine, _body, _child):
        calls[machine] += 1
        if machine == "mac-a" and calls[machine] == 1:
            hub = _exact_result(
                "hub", "retryable_failure",
                error_code="clean_checkout_health_failure",
            )
        elif machine == "mac-b" and calls[machine] < 3:
            return {"job_id": "agent-mac-b", "components": [
                _exact_result(name, "pending_offline", error_code="offline")
                for name in ("hub", "image", "voice")
            ]}
        elif machine == "mac-b":
            hub = _exact_result(
                "hub", "retryable_failure",
                error_code="clean_checkout_health_failure",
            )
        else:
            hub = _exact_result("hub")
        return {"job_id": f"agent-{machine}", "components": [
            hub, _exact_result("image"), _exact_result("voice"),
        ]}

    service, manifest = _execution_service(
        tmp_path, clock=clock, remote_bundle_runner=remote,
        component_runner=lambda *_args, **_kwargs: [
            _exact_result("image"), _exact_result("voice")
        ],
        hub_runner=lambda *_args: _exact_result("hub"),
    )
    first = await service.run(manifest["release_id"])
    assert first["state"] == "degraded"

    clock.advance(61)
    assert service.resume_due() == 4
    recovered = await service.run(manifest["release_id"])
    assert recovered["state"] == "degraded"
    job_id = service.state_snapshot()["activation"]["job_id"]
    assert _raw_state(tmp_path)["jobs"][job_id]["clean_failure_machines"] == ["mac-a"]

    clock.advance(301)
    assert service.resume_due() == 3
    blocked = await service.run(manifest["release_id"])

    assert blocked["state"] == "blocked_release"
    assert _raw_state(tmp_path)["jobs"][job_id]["clean_failure_machines"] == ["mac-a", "mac-b"]
    assert "clean_failure_machines" not in service.job_snapshot(job_id)


@pytest.mark.parametrize("history", ["mac-a", ["unknown"], ["mac-a", "mac-a"]])
def test_clean_failure_history_is_deeply_validated(tmp_path, history):
    service = _service(tmp_path)
    manifest = _manifest()
    service.replace_intent(manifest)
    job = service.activate(manifest["release_id"], genstudio_run_reference=None)
    state = _raw_state(tmp_path)
    state["jobs"][job["id"]]["clean_failure_machines"] = history
    path = tmp_path / "release_reconciliation.json"
    path.write_text(json.dumps(state))
    os.chmod(path, 0o600)

    with pytest.raises(ValueError, match="durable release reconciliation state"):
        _service(tmp_path)


@pytest.mark.parametrize("agent_result, expected_state", [
    (_exact_result("hub", observed_commit="d" * 40), "blocked_release"),
    (
        _exact_result("hub", "release_blocked", error_code="update_refused"),
        "degraded",
    ),
])
@pytest.mark.asyncio
async def test_agent_to_controller_propagates_only_validated_blocks(
    tmp_path, agent_result, expected_state,
):
    agent_services = {}
    controller_calls = []

    def agent_for(machine, manifest):
        from backend.release_reconciliation import ReleaseReconciler

        if machine not in agent_services:
            async def hub(_target, _operation_id):
                return agent_result if machine == "mac-a" else _exact_result("hub")

            agent_services[machine] = ReleaseReconciler(
                ExecutionMonitor(),
                state_path=tmp_path / machine / "release_reconciliation.json",
                clock=Clock(), peer_reader=lambda _machine: None,
                hub_runner=hub,
                component_runner=lambda *_args, **_kwargs: [
                    _exact_result("image"), _exact_result("voice")
                ],
                lease_seconds=30, heartbeat_seconds=5,
            )
        return agent_services[machine]

    async def remote(machine, body, _child):
        controller_calls.append(machine)
        agent = agent_for(machine, body)
        admission = agent.admit_and_schedule_managed_update(body)
        return await agent.run_managed_update(admission["job_id"])

    controller, manifest = _execution_service(
        tmp_path / "controller", remote_bundle_runner=remote,
        component_runner=lambda *_args, **_kwargs: [
            _exact_result("image"), _exact_result("voice")
        ],
        hub_runner=lambda *_args: _exact_result("hub"),
    )
    job = await controller.run(manifest["release_id"])

    assert job["state"] == expected_state
    assert controller_calls == (
        ["mac-a"] if expected_state == "blocked_release" else ["mac-a", "mac-b"]
    )


@pytest.mark.parametrize("second_machine", [None, "mac-b"])
def test_clean_failure_item_history_and_block_commit_atomically(
    tmp_path, monkeypatch, second_machine,
):
    from backend import release_reconciliation

    service, _manifest_value = _execution_service(tmp_path)
    job_id = service.state_snapshot()["activation"]["job_id"]
    assert service.resume_pending() == 1
    fence = _raw_state(tmp_path)["jobs"][job_id]["adoption_lease"]["generation"]
    if second_machine is not None:
        service._record_result(
            job_id, "mac-a",
            _exact_result(
                "hub", "retryable_failure",
                error_code="clean_checkout_health_failure",
            ),
            fence=fence,
        )

    real_atomic = release_reconciliation._atomic_json
    writes = []

    def capture(path, payload):
        writes.append(deepcopy(payload))
        real_atomic(path, payload)

    monkeypatch.setattr(release_reconciliation, "_atomic_json", capture)
    machine = second_machine or "mac-a"
    blocked = service._record_result(
        job_id, machine,
        _exact_result(
            "hub", "retryable_failure",
            error_code="clean_checkout_health_failure",
        ),
        fence=fence,
    )

    assert len(writes) == 1
    durable_job = writes[0]["jobs"][job_id]
    assert durable_job["machines"][machine]["components"]["hub"]["error_code"] == (
        "clean_checkout_health_failure"
    )
    expected_history = ["mac-a"] if second_machine is None else ["mac-a", "mac-b"]
    assert durable_job["clean_failure_machines"] == expected_history
    assert blocked is (second_machine is not None)
    if blocked:
        assert durable_job["state"] == "blocked_release"


def test_exact_current_ignores_stale_clean_failure_code(tmp_path):
    service, _manifest_value = _execution_service(tmp_path)
    job_id = service.state_snapshot()["activation"]["job_id"]
    assert service.resume_pending() == 1
    fence = _raw_state(tmp_path)["jobs"][job_id]["adoption_lease"]["generation"]

    blocked = service._record_result(
        job_id, "mac-a",
        _exact_result("hub", error_code="clean_checkout_health_failure"),
        fence=fence,
    )

    job = service.job_snapshot(job_id)
    assert blocked is False
    assert job["machines"]["mac-a"]["components"]["hub"]["state"] == "current"
    assert job["machines"]["mac-a"]["components"]["hub"]["error_code"] is None
    assert _raw_state(tmp_path)["jobs"][job_id]["clean_failure_machines"] == []


@pytest.mark.asyncio
async def test_stale_current_clean_code_plus_one_real_failure_does_not_block(tmp_path):
    async def remote(machine, _body, _child):
        hub = (
            _exact_result("hub", error_code="clean_checkout_health_failure")
            if machine == "mac-a"
            else _exact_result(
                "hub", "retryable_failure",
                error_code="clean_checkout_health_failure",
            )
        )
        return {"job_id": f"agent-{machine}", "components": [
            hub, _exact_result("image"), _exact_result("voice"),
        ]}

    service, manifest = _execution_service(
        tmp_path, remote_bundle_runner=remote,
        component_runner=lambda *_args, **_kwargs: [
            _exact_result("image"), _exact_result("voice")
        ],
        hub_runner=lambda *_args: _exact_result("hub"),
    )
    job = await service.run(manifest["release_id"])
    job_id = service.state_snapshot()["activation"]["job_id"]

    assert job["state"] == "degraded"
    assert job["machines"]["mac-a"]["components"]["hub"]["state"] == "current"
    assert _raw_state(tmp_path)["jobs"][job_id]["clean_failure_machines"] == ["mac-b"]


@pytest.mark.asyncio
async def test_catalog_failure_retries_same_operation_without_hub_replay(tmp_path):
    clock = Clock()
    catalog_operations = []
    hub_calls = []

    async def remote(machine, _body, _child):
        return {"job_id": f"agent-{machine}", "components": [
            _exact_result(name) for name in ("hub", "image", "voice")
        ]}

    async def catalog(operation_id):
        catalog_operations.append(operation_id)
        if len(catalog_operations) == 1:
            raise TimeoutError("response outcome unknown")

    service, manifest = _execution_service(
        tmp_path, clock=clock, remote_bundle_runner=remote,
        component_runner=lambda *_args, **_kwargs: [
            _exact_result("image"), _exact_result("voice")
        ],
        hub_runner=lambda *_args: hub_calls.append("hub") or _exact_result("hub"),
        catalog_requester=catalog,
    )
    first = await service.run(manifest["release_id"])
    assert first["state"] == "degraded"
    assert first["catalog"]["state"] == "retryable_failure"

    clock.advance(61)
    assert service.resume_due() == 1
    restarted = type(service)(
        ExecutionMonitor(), state_path=service.state_path, clock=clock,
        peer_reader=service._peer_reader, remote_bundle_runner=remote,
        component_runner=service._component_runner, hub_runner=service._hub_runner,
        catalog_requester=catalog, loaded_version=COMPONENTS["hub"]["version"],
        loaded_commit=COMPONENTS["hub"]["commit"], lease_seconds=30,
        heartbeat_seconds=5,
    )
    current = await restarted.run(manifest["release_id"])

    assert current["state"] == "complete"
    assert catalog_operations[0] == catalog_operations[1]
    assert current["catalog"]["state"] == "acknowledged"
    assert hub_calls == ["hub"]


@pytest.mark.asyncio
async def test_catalog_ack_persistence_ambiguity_replays_same_operation(tmp_path):
    operations = []
    hub_calls = []

    async def remote(machine, _body, _child):
        return {"job_id": f"agent-{machine}", "components": [
            _exact_result(name) for name in ("hub", "image", "voice")
        ]}

    async def catalog(operation_id):
        operations.append(operation_id)

    service, manifest = _execution_service(
        tmp_path, remote_bundle_runner=remote,
        component_runner=lambda *_args, **_kwargs: [
            _exact_result("image"), _exact_result("voice")
        ],
        hub_runner=lambda *_args: hub_calls.append("hub") or _exact_result("hub"),
        catalog_requester=catalog,
    )
    original_write = service._write
    failed = False

    def lose_ack(mutate, **kwargs):
        nonlocal failed
        if operations and not failed:
            failed = True
            raise OSError("catalog acknowledgement persistence lost")
        return original_write(mutate, **kwargs)

    service._write = lose_ack
    with pytest.raises(OSError, match="acknowledgement"):
        await service.run(manifest["release_id"])
    job_id = service.state_snapshot()["activation"]["job_id"]
    assert service.job_snapshot(job_id)["catalog"]["state"] == "requesting"

    restarted = type(service)(
        ExecutionMonitor(), state_path=service.state_path, clock=service._clock,
        peer_reader=service._peer_reader, remote_bundle_runner=remote,
        component_runner=service._component_runner, hub_runner=service._hub_runner,
        catalog_requester=catalog, loaded_version=COMPONENTS["hub"]["version"],
        loaded_commit=COMPONENTS["hub"]["commit"], lease_seconds=30,
        heartbeat_seconds=5,
    )
    current = await restarted.run(manifest["release_id"])

    assert current["state"] == "complete"
    assert operations[0] == operations[1]
    assert hub_calls == ["hub"]
