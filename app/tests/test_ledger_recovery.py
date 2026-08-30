import sqlite3

import pytest

from backend import ledger


def test_prepare_database_quarantines_malformed_ledger_and_preserves_sidecars(
    tmp_path, monkeypatch
):
    database = tmp_path / "hub.db"
    database.write_bytes(b"this is not a sqlite database")
    (tmp_path / "hub.db-wal").write_bytes(b"damaged wal")
    (tmp_path / "hub.db-shm").write_bytes(b"damaged shm")
    monkeypatch.setattr(ledger, "DB_FILE", database)

    recovery = ledger.prepare_database()

    assert recovery is not None
    assert (recovery / "hub.db").read_bytes() == b"this is not a sqlite database"
    assert (recovery / "hub.db-wal").read_bytes() == b"damaged wal"
    assert (recovery / "hub.db-shm").read_bytes() == b"damaged shm"
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA quick_check(1)").fetchone()[0] == "ok"
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert {"batches", "batch_requests", "assets"} <= tables


def test_prepare_database_leaves_healthy_ledger_untouched(tmp_path, monkeypatch):
    database = tmp_path / "hub.db"
    monkeypatch.setattr(ledger, "DB_FILE", database)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE preserved (value TEXT)")
        connection.execute("INSERT INTO preserved VALUES ('keep me')")
    inode = database.stat().st_ino

    assert ledger.prepare_database() is None

    assert database.stat().st_ino == inode
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM preserved").fetchone()[0] == "keep me"
    assert not (tmp_path / ".database-recovery").exists()


def test_prepare_database_restores_original_if_clean_rebuild_fails(
    tmp_path, monkeypatch
):
    database = tmp_path / "hub.db"
    original = b"recoverable corrupt bytes"
    database.write_bytes(original)
    monkeypatch.setattr(ledger, "DB_FILE", database)
    checks = 0

    def fail_both_checks():
        nonlocal checks
        checks += 1
        if checks == 1:
            raise sqlite3.DatabaseError("file is not a database")
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(ledger, "_validate_database", fail_both_checks)

    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        ledger.prepare_database()

    assert database.read_bytes() == original
