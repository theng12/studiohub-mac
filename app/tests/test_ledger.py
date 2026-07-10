from backend import ledger


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


def test_stats_counts_and_speed(reset):
    for i in range(3):
        ledger.record_asset(source="job", modality="image", studio="image",
                            machine="local", model="a/b", duration_s=2.0)
    ledger.record_asset(source="job", modality="image", studio="image@mac-b",
                        machine="mac-b", model="a/b", duration_s=8.0)
    # scanned assets must NOT count toward generation stats
    ledger.record_asset(source="scan", modality="image", machine="local",
                        artifact_path="/tmp/scanned.png")
    s = ledger.stats()
    assert s["total"] == 4
    assert s["by_machine"]["local"]["count"] == 3
    assert s["by_machine"]["local"]["avg_s"] == 2.0
    assert s["by_machine"]["mac-b"]["count"] == 1
    assert s["by_modality"]["image"]["count"] == 4
    assert s["by_model"]["a/b"]["count"] == 4


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
