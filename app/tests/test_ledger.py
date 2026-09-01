from backend import ledger
from backend.registry import DATA_DIR


def test_record_and_get_asset(reset):
    aid = ledger.record_asset(source="job", modality="image", studio="image",
                              machine="local", model="a/b", seed=42,
                              prompt="hi", artifact_path="/tmp/x.png",
                              duration_s=1.5)
    got = ledger.get_asset(aid)
    assert got["modality"] == "image"
    assert got["seed"] == 42
    assert got["duration_s"] == 1.5


def test_query_assets_filters(reset):
    ledger.record_asset(source="job", modality="image", studio="image",
                        machine="local", model="a/b", prompt="cat")
    ledger.record_asset(source="job", modality="voice", studio="voice",
                        machine="local", model="c/d", prompt="dog")
    assert len(ledger.query_assets(modality="image")) == 1
    assert len(ledger.query_assets(q="dog")) == 1
    assert len(ledger.query_assets()) == 2


def test_query_assets_sorting(reset):
    ledger.record_asset(source="job", modality="voice", studio="z-studio",
                        machine="local", model="z/model", prompt="later",
                        artifact_path="/tmp/z.wav", created_at=20)
    ledger.record_asset(source="job", modality="image", studio="a-studio",
                        machine="local", model="a/model", prompt="earlier",
                        artifact_path="/tmp/a.png", created_at=10)
    assert [a["created_at"] for a in ledger.query_assets(sort="newest")] == [20, 10]
    assert [a["created_at"] for a in ledger.query_assets(sort="oldest")] == [10, 20]
    assert ledger.query_assets(sort="name")[0]["artifact_path"] == "/tmp/a.png"
    assert ledger.query_assets(sort="type")[0]["modality"] == "image"
    assert ledger.query_assets(sort="studio")[0]["studio"] == "a-studio"
    assert ledger.query_assets(sort="model")[0]["model"] == "a/model"


def test_stats_counts_and_speed(reset):
    for i in range(3):
        ledger.record_asset(source="job", modality="image", studio="image",
                            machine="local", model="a/b", duration_s=2.0)
    ledger.record_asset(source="job", modality="image", studio="image@mac-b",
                        machine="mac-b", model="a/b", duration_s=8.0)
    # a direct-in-studio scan (no timing/model) — now COUNTED by default
    ledger.record_asset(source="scan", modality="image", studio="image",
                        machine="local", artifact_path="/tmp/scanned.png")

    # default: all sources counted
    s = ledger.stats()
    assert s["total"] == 5
    assert s["by_source"] == {"job": 4, "scan": 1}
    assert s["by_machine"]["local"]["count"] == 4          # 3 jobs + 1 scan
    assert s["by_machine"]["local"]["avg_s"] == 2.0        # scan has no timing
    assert s["by_machine"]["mac-b"]["count"] == 1
    assert s["by_modality"]["image"]["count"] == 5
    assert s["by_model"]["a/b"]["count"] == 4               # scan has no model

    # source filters
    assert ledger.stats(source="job")["total"] == 4
    assert ledger.stats(source="direct")["total"] == 1
    # op + machine filters
    assert ledger.stats(op="image")["total"] == 5
    assert ledger.stats(machine="mac-b")["total"] == 1


def test_stats_op_splits_voice_and_music(reset):
    # Scanned audio tags modality='audio' but studio tells voice vs music apart.
    ledger.record_asset(source="job", modality="voice", studio="voice",
                        machine="local", model="tts/x", duration_s=1.0)
    ledger.record_asset(source="scan", modality="audio", studio="music",
                        machine="local", artifact_path="/tmp/song.wav")
    s = ledger.stats()
    assert s["by_modality"]["voice"]["count"] == 1
    assert s["by_modality"]["music"]["count"] == 1
    assert "audio" not in s["by_modality"]                  # coarse type replaced by op
    assert set(s["available_modalities"]) == {
        "image", "voice", "transcription", "music",
    }


def test_stats_adds_completed_transcriptions_without_double_counting_live_states(reset):
    import time

    now = time.time()
    ledger.record_asset(
        source="job", modality="voice", studio="voice@mac-a",
        machine="mac-a", model="org/qwen", runtime_s=4.0, created_at=now - 5,
    )
    ledger.record_activity_event(
        machine="mac-a", studio="voice@mac-a", job_id="subtitle-done",
        state="done", operation="transcription", model="org/whisper",
        source="direct", origin="api", started_at=now - 16,
        finished_at=now - 4, runtime_s=12.0, observed_at=now - 4,
    )
    for state in ("running", "error"):
        ledger.record_activity_event(
            machine="mac-a", studio="voice@mac-a", job_id=f"subtitle-{state}",
            state=state, operation="transcription", model="org/whisper",
            source="direct", origin="api", observed_at=now - 3,
        )

    result = ledger.stats()

    assert result["total"] == 2
    assert result["by_modality"]["voice"]["count"] == 1
    assert result["by_modality"]["transcription"]["count"] == 1
    assert result["by_machine"]["mac-a"]["modalities"] == {
        "image": 0, "voice": 1, "transcription": 1,
    }
    assert result["by_model"]["org/whisper"] == {
        "count": 1, "avg_s": 12.0, "modality": "transcription",
    }
    assert any(
        cell["machine"] == "mac-a"
        and cell["modality"] == "transcription"
        and cell["count"] == 1
        for cell in result["matrix"]
    )
    assert ledger.stats(source="direct")["total"] == 1
    assert ledger.stats(source="job")["total"] == 1
    assert ledger.stats(op="transcription")["total"] == 1
    assert ledger.stats(since_s=now - 4.5)["total"] == 1

    timeline = ledger.timeline(since_s=now - 60, bucket_s=60)
    assert sum(timeline["series"]["voice"]) == 1
    assert sum(timeline["series"]["transcription"]) == 1


def test_stats_keeps_core_operation_filters_available_without_history(reset):
    result = ledger.stats()

    assert result["available_modalities"] == ["image", "voice", "transcription"]


def test_stats_preserves_same_model_used_for_voice_and_transcription(reset):
    ledger.record_asset(
        source="job", modality="voice", studio="voice@mac-a",
        machine="mac-a", model="shared/model", runtime_s=4.0,
    )
    ledger.record_activity_event(
        machine="mac-a", studio="voice@mac-a", job_id="shared-subtitle",
        state="done", operation="transcription", model="shared/model",
        source="job", origin="hub", runtime_s=9.0,
    )

    rows = ledger.stats()["model_rows"]

    assert rows == [
        {"model": "shared/model", "count": 1, "avg_s": 9.0,
         "modality": "transcription"},
        {"model": "shared/model", "count": 1, "avg_s": 4.0,
         "modality": "voice"},
    ]


def test_timeline_buckets(reset):
    import time
    now = time.time()
    for _ in range(2):
        ledger.record_asset(source="job", modality="image", machine="local",
                            model="a/b", created_at=now, duration_s=1)
    tl = ledger.timeline(since_s=None, bucket_s=3600)
    assert sum(tl["series"]["image"]) == 2
    assert tl["bucket_s"] == 3600


def test_batch_persistence_roundtrip(reset):
    b = {"id": "b1", "created_at": 1.0, "items": [
        {"state": "queued"}, {"state": "running"}]}
    ledger.save_batch(b)
    assert [x["id"] for x in ledger.load_unfinished_batches()] == ["b1"]
    # once terminal, it's no longer 'unfinished'
    b["items"] = [{"state": "done"}, {"state": "error"}]
    ledger.save_batch(b)
    assert ledger.load_unfinished_batches() == []
    assert ledger.load_batch("b1")["id"] == "b1"  # still queryable


def test_duration_migration_on_old_db(reset):
    # simulate the REAL pre-duration_s schema (all columns that existed before,
    # minus duration_s). _conn() must ALTER-add duration_s transparently.
    import sqlite3
    conn = sqlite3.connect(ledger.DB_FILE)
    conn.execute("""CREATE TABLE assets (
        id TEXT PRIMARY KEY, created_at REAL NOT NULL, source TEXT NOT NULL,
        modality TEXT, studio TEXT, machine TEXT, model TEXT, seed INTEGER,
        prompt TEXT, params_json TEXT, artifact_path TEXT UNIQUE,
        artifact_url TEXT, batch_id TEXT, item_index INTEGER, recipe_id TEXT)""")
    conn.commit()
    conn.close()
    aid = ledger.record_asset(source="job", modality="image", machine="local",
                              duration_s=3.0)
    assert ledger.get_asset(aid)["duration_s"] == 3.0


def test_activity_origin_migration_roundtrip_and_bounded_device(reset):
    # Simulate the production schema before optional origin scalars existed.
    import sqlite3

    conn = sqlite3.connect(ledger.DB_FILE)
    conn.execute("""CREATE TABLE activity_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, machine TEXT NOT NULL,
        studio TEXT NOT NULL, job_id TEXT NOT NULL, state TEXT NOT NULL,
        model TEXT, source TEXT NOT NULL, progress REAL, started_at REAL,
        finished_at REAL, runtime_s REAL, error_code TEXT, observed_at REAL NOT NULL,
        activity_received_at REAL NOT NULL, reported_at REAL,
        hub_owned INTEGER NOT NULL DEFAULT 0,
        UNIQUE(machine, studio, job_id, state))""")
    conn.commit()
    conn.close()

    ledger.record_activity_event(
        machine="mac-a", studio="image@mac-a", job_id="job-1", state="done",
        model="org/model", source="direct", origin="spoof", origin_device="x" * 170,
        finished_at=100.0, observed_at=100.0,
    )
    with ledger._conn() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(activity_events)")}
    assert {"origin", "origin_device"} <= columns
    row = ledger.activity_events(machine="mac-a")[0]
    assert row["origin"] == "unknown"
    assert row["origin_device"] == "x" * 160


def test_clearing_job_assets_only_unlinks_files_owned_by_this_hub(reset):
    local = DATA_DIR / "owned.png"
    local.write_bytes(b"owned")
    outside = DATA_DIR.parent / "outside.png"
    outside.write_bytes(b"outside")
    ledger.record_asset(source="job", batch_id="finished", artifact_path=str(local))
    ledger.record_asset(source="job", batch_id="finished", artifact_path=str(outside))

    result = ledger.remove_job_assets(["finished"])
    assert result == {"assets_removed": 2, "files_removed": 1, "reclaimed_bytes": 5}
    assert not local.exists()
    assert outside.read_bytes() == b"outside"
    outside.unlink()
