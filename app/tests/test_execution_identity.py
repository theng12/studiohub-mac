import sqlite3

from backend import broker, control_plane, execution_identity, ledger


def _configure_site():
    control_plane.save_settings({
        "role": "controller",
        "site_id": "site-a",
        "site_name": "Site A",
        "controller_id": "controller-a",
        "database_mode": "off",
    })


def _envelope(*, token=7, text="Hello", idempotency_key="stable-attempt-key",
              job_id="job-100", attempt_id="attempt-200"):
    return {
        "modality": "voice",
        "model": "org/qwen-tts",
        "items": [{"text": text}],
        "genstudio_execution": {
            "genstudio_job_id": job_id,
            "genstudio_attempt_id": attempt_id,
            "idempotency_key": idempotency_key,
            "fencing_token": token,
            "site_id": "site-a",
            "operation": "tts",
            "model_revision": "model-sha-1",
            "voice_revision": "voice-sha-2",
        },
    }


def test_external_genstudio_identity_and_token_are_preserved_as_evidence(reset):
    _configure_site()

    submitted = broker.submit_batch(_envelope(token=41))
    batch = broker.batches[submitted["batch_id"]]
    evidence = batch["genstudio_execution"]

    assert evidence["genstudio_job_id"] == "job-100"
    assert evidence["genstudio_attempt_id"] == "attempt-200"
    assert evidence["fencing_token"] == 41
    assert evidence["site_id"] == "site-a"
    assert evidence["operation"] == "tts"
    assert evidence["model_revision"] == "model-sha-1"
    assert evidence["voice_revision"] == "voice-sha-2"
    assert evidence["authority"] == "genstudio"
    assert len(evidence["idempotency_hash"]) == 64
    assert "stable-attempt-key" not in str(batch)
    assert ledger.load_batch(batch["id"])["genstudio_execution"] == evidence


def test_top_level_genstudio_identity_form_is_accepted(reset):
    _configure_site()
    envelope = _envelope(token=12)
    identity = envelope.pop("genstudio_execution")
    envelope.update(identity)

    submitted = broker.submit_batch(envelope)
    evidence = broker.batches[submitted["batch_id"]]["genstudio_execution"]

    assert evidence["genstudio_job_id"] == "job-100"
    assert evidence["fencing_token"] == 12


def test_exact_idempotent_replay_is_safe_and_does_not_duplicate_work(reset):
    _configure_site()
    envelope = _envelope()

    first = broker.submit_batch(envelope)
    replay = broker.submit_batch({**envelope, "clientRequestId": "caller-cannot-bypass-idem"})

    assert replay == {"batch_id": first["batch_id"], "items": 1, "replayed": True}
    assert len(broker.batches) == 1


def test_conflicting_idempotent_replay_is_rejected(reset):
    _configure_site()
    first = broker.submit_batch(_envelope(text="Original"))
    conflict = broker.submit_batch(_envelope(text="Changed"))

    assert "batch_id" in first
    assert "different GenStudio assignment" in conflict["error"]
    assert len(broker.batches) == 1


def test_newer_external_fence_is_preserved_and_older_fence_is_rejected(reset):
    _configure_site()
    first = broker.submit_batch(_envelope(token=7))
    newer = broker.submit_batch(_envelope(token=9))
    stale = broker.submit_batch(_envelope(token=8))

    assert newer == {"batch_id": first["batch_id"], "items": 1, "replayed": True}
    assert broker.batches[first["batch_id"]]["genstudio_execution"]["fencing_token"] == 9
    assert "stale GenStudio fencing token 8" in stale["error"]
    assert len(broker.batches) == 1


def test_hub_never_generates_or_increments_global_fencing_tokens(reset):
    _configure_site()
    broker.submit_batch(_envelope(token=113))

    with sqlite3.connect(execution_identity.DB_FILE) as connection:
        job_token = connection.execute(
            "SELECT highest_fencing_token FROM genstudio_fences WHERE genstudio_job_id=?",
            ("job-100",),
        ).fetchone()[0]
        attempt_token = connection.execute(
            "SELECT highest_fencing_token FROM genstudio_attempt_fences "
            "WHERE genstudio_attempt_id=?",
            ("attempt-200",),
        ).fetchone()[0]

    assert job_token == 113
    assert attempt_token == 113


def test_attempt_scope_rejects_an_older_fence_even_under_another_job(reset):
    _configure_site()
    accepted = broker.submit_batch(_envelope(token=20))
    stale = broker.submit_batch(_envelope(
        token=19, job_id="job-other", idempotency_key="other-key"))

    assert "batch_id" in accepted
    assert "attempt 'attempt-200' has already observed 20" in stale["error"]


def test_assignment_for_another_site_is_rejected_before_local_dispatch(reset):
    _configure_site()
    envelope = _envelope()
    envelope["genstudio_execution"]["site_id"] = "site-b"

    result = broker.submit_batch(envelope)

    assert "assigned site 'site-b'" in result["error"]
    assert broker.batches == {}


def test_existing_direct_story_studio_and_genstudio_requests_are_unchanged(reset):
    direct = {
        "clientRequestId": "genstudio:legacy-job:attempt-1",
        "modality": "voice",
        "model": "org/qwen-tts",
        "label": "genstudio-kh:story-studio-kh",
        "items": [{"text": "Established adapter request"}],
    }

    first = broker.submit_batch(direct)
    replay = broker.submit_batch(direct)
    batch = broker.batches[first["batch_id"]]

    assert replay["batch_id"] == first["batch_id"]
    assert replay["replayed"] is True
    assert batch["client_request_id"] == direct["clientRequestId"]
    assert batch["genstudio_execution"] is None
