"""Unified asset ledger — index + link, never copy.

SQLite at the launcher root (hub.db, gitignored). Two sources of truth:
- `job`: assets created through the Hub's broker/recipes — full provenance
  (prompt, model, seed, params, batch/recipe ids). The reproducibility payoff.
- `scan`: files discovered in local studios' app/output folders — basic file
  facts only; provenance fields stay null (the studios don't write sidecars).

Artifacts stay in each studio's own output folder; the ledger stores the path
plus (when known) the studio's serving URL.
"""

import json
import contextlib
import shutil
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from .control import PINOKIO_HOME
from .registry import LAUNCHER_ROOT

from .registry import DATA_DIR
DB_FILE = DATA_DIR / "hub.db"
_PREPARE_LOCK = threading.Lock()

MEDIA_EXT = {
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image",
    ".wav": "audio", ".mp3": "audio", ".flac": "audio", ".ogg": "audio",
    ".mp4": "video", ".mov": "video", ".webm": "video",
}
_VALID_ORIGINS = frozenset({"hub", "local_ui", "api", "unknown"})
_MAX_ORIGIN_DEVICE = 160

_SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
  id TEXT PRIMARY KEY,
  created_at REAL NOT NULL,
  finished INTEGER NOT NULL DEFAULT 0,
  payload TEXT NOT NULL              -- full batch dict as JSON (write-through)
);
CREATE TABLE IF NOT EXISTS batch_requests (
  client_request_id TEXT PRIMARY KEY,
  request_fingerprint TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  item_count INTEGER NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS assets (
  id TEXT PRIMARY KEY,
  created_at REAL NOT NULL,
  source TEXT NOT NULL,            -- 'job' | 'scan'
  modality TEXT,
  studio TEXT,
  machine TEXT,
  model TEXT,
  seed INTEGER,
  prompt TEXT,
  params_json TEXT,
  artifact_path TEXT UNIQUE,
  artifact_url TEXT,
  batch_id TEXT,
  item_index INTEGER,
  recipe_id TEXT,
  duration_s REAL,                -- legacy alias for runtime_s
  runtime_s REAL                  -- generation/processing time in seconds
);
CREATE INDEX IF NOT EXISTS idx_assets_created ON assets(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_assets_batch ON assets(batch_id);
CREATE TABLE IF NOT EXISTS activity_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  machine TEXT NOT NULL,
  studio TEXT NOT NULL,
  job_id TEXT NOT NULL,
  state TEXT NOT NULL,
  model TEXT,
  source TEXT NOT NULL,
  origin TEXT,
  origin_device TEXT,
  progress REAL,
  started_at REAL,
  finished_at REAL,
  runtime_s REAL,
  error_code TEXT,
  observed_at REAL NOT NULL,      -- immutable controller receipt time
  activity_received_at REAL NOT NULL, -- latest controller receipt/order
  reported_at REAL,
  hub_owned INTEGER NOT NULL DEFAULT 0,
  UNIQUE(machine, studio, job_id, state)
);
CREATE INDEX IF NOT EXISTS idx_activity_events_machine_time
  ON activity_events(machine, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_activity_events_comparable
  ON activity_events(studio, model, machine, finished_at DESC);
CREATE TABLE IF NOT EXISTS machine_state_transitions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  machine TEXT NOT NULL,
  reachable INTEGER NOT NULL,
  working INTEGER NOT NULL,
  state TEXT NOT NULL DEFAULT 'unknown',
  state_since REAL,
  observed_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_machine_state_transitions_machine_time
  ON machine_state_transitions(machine, observed_at ASC);
"""


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    try:
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)
        # Migrations for DBs created before newer columns existed.
        for ddl in ("ALTER TABLE assets ADD COLUMN duration_s REAL",
                    "ALTER TABLE assets ADD COLUMN runtime_s REAL",
                    "ALTER TABLE activity_events ADD COLUMN activity_received_at REAL",
                    "ALTER TABLE activity_events ADD COLUMN reported_at REAL",
                    "ALTER TABLE activity_events ADD COLUMN hub_owned INTEGER NOT NULL DEFAULT 0",
                    "ALTER TABLE activity_events ADD COLUMN origin TEXT",
                    "ALTER TABLE activity_events ADD COLUMN origin_device TEXT",
                    "ALTER TABLE machine_state_transitions ADD COLUMN state TEXT NOT NULL DEFAULT 'unknown'",
                    "ALTER TABLE machine_state_transitions ADD COLUMN state_since REAL"):
            try:
                conn.execute(ddl)
                conn.commit()
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
        return conn
    except BaseException:
        conn.close()
        raise


@contextlib.contextmanager
def activity_transaction():
    """Reuse one prepared SQLite connection for a complete observer cycle."""
    conn = _conn()
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def record_activity_event(*, machine: str, studio: str, job_id: str, state: str,
                          model: str | None, source: str, progress: float | None = None,
                          origin: str | None = None, origin_device: str | None = None,
                          started_at: float | None = None,
                          finished_at: float | None = None,
                          runtime_s: float | None = None,
                          error_code: str | None = None,
                          activity_received_at: float | None = None,
                          reported_at: float | None = None,
                          hub_owned: bool = False,
                          observed_at: float | None = None,
                          conn: sqlite3.Connection | None = None) -> bool:
    """Insert one meaningful Studio job transition, or refresh its evidence.

    The unique transition identity deliberately excludes observation time and
    progress: five-second health polling must not inflate counts merely because
    a worker is still running. The latest bounded scalar evidence remains
    useful for an operator-facing live board.
    """
    observed_at = float(time.time() if observed_at is None else observed_at)
    activity_received_at = float(
        observed_at if activity_received_at is None else activity_received_at
    )
    reported_at = float(observed_at if reported_at is None else reported_at)
    hub_owned = bool(hub_owned or source == "job")
    origin = origin if origin in _VALID_ORIGINS else "unknown"
    origin_device = origin_device.strip()[:_MAX_ORIGIN_DEVICE] if isinstance(origin_device, str) else None
    origin_device = origin_device or None
    owns_connection = conn is None
    if conn is None:
        conn = _conn()
    try:
        existing = conn.execute(
            "SELECT activity_received_at, hub_owned, origin_device FROM activity_events WHERE "
            "machine = ? AND studio = ? AND job_id = ? AND state = ?",
            (machine, studio, job_id, state),
        ).fetchone()
        if existing is None:
            conn.execute(
                """INSERT INTO activity_events (
                    machine, studio, job_id, state, model, source, origin, origin_device, progress,
                    started_at, finished_at, runtime_s, error_code, observed_at,
                    activity_received_at, reported_at, hub_owned
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (machine, studio, job_id, state, model, "job" if hub_owned else source,
                 "hub" if hub_owned else origin, origin_device, progress, started_at,
                 finished_at, runtime_s, error_code, observed_at, activity_received_at, reported_at,
                 int(hub_owned)),
            )
            changed = True
        else:
            previous_received = existing["activity_received_at"]
            is_newer = previous_received is None or activity_received_at >= previous_received
            if is_newer:
                conn.execute(
                    """UPDATE activity_events SET model = ?, source = ?, origin = ?, origin_device = ?, progress = ?,
                       started_at = ?, finished_at = ?, runtime_s = ?, error_code = ?,
                       activity_received_at = ?, reported_at = ?, hub_owned = ?
                       WHERE machine = ? AND studio = ? AND job_id = ? AND state = ?""",
                    (model, "job" if hub_owned or existing["hub_owned"] else source,
                     "hub" if hub_owned or existing["hub_owned"] else origin,
                     origin_device if hub_owned or not existing["hub_owned"] else existing["origin_device"],
                     progress, started_at, finished_at, runtime_s, error_code,
                     activity_received_at, reported_at, int(hub_owned or existing["hub_owned"]),
                     machine, studio, job_id, state),
                )
                changed = True
            elif hub_owned and not existing["hub_owned"]:
                conn.execute(
                    "UPDATE activity_events SET hub_owned = 1, source = 'job', origin = 'hub', origin_device = ? "
                    "WHERE machine = ? AND studio = ? AND job_id = ? AND state = ?",
                    (origin_device, machine, studio, job_id, state),
                )
                changed = True
            else:
                changed = False
        if owns_connection:
            conn.commit()
        return changed
    finally:
        if owns_connection:
            conn.close()


def activity_events(*, machine: str | None = None,
                    since_s: float | None = None,
                    conn: sqlite3.Connection | None = None) -> list[dict]:
    where, args = ["1=1"], []
    if machine:
        where.append("machine = ?")
        args.append(machine)
    if since_s is not None:
        where.append("observed_at >= ?")
        args.append(float(since_s))
    owns_connection = conn is None
    if conn is None:
        conn = _conn()
    try:
        rows = conn.execute(
            "SELECT machine, studio, job_id, state, model, source, origin, origin_device, progress, "
            "started_at, finished_at, runtime_s, error_code, observed_at, activity_received_at, reported_at, hub_owned "
            f"FROM activity_events WHERE {' AND '.join(where)} "
            "ORDER BY observed_at DESC, id DESC", args,
        ).fetchall()
    finally:
        if owns_connection:
            conn.close()
    return [dict(row) for row in rows]


def activity_job_is_hub_owned(machine: str, studio: str, job_id: str,
                              *, conn: sqlite3.Connection | None = None) -> bool:
    """Whether an earlier broker observation established job ownership."""
    owns_connection = conn is None
    if conn is None:
        conn = _conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM activity_events WHERE machine = ? AND studio = ? "
            "AND job_id = ? AND (hub_owned = 1 OR source = 'job') LIMIT 1",
            (machine, studio, job_id),
        ).fetchone()
    finally:
        if owns_connection:
            conn.close()
    return row is not None


def record_activity_ownership(*, machine: str, studio: str, job_id: str,
                              model: str | None, observed_at: float | None = None) -> None:
    """Durably bind a worker job ID to this Hub before any reporter poll."""
    receipt = float(time.time() if observed_at is None else observed_at)
    record_activity_event(
        machine=machine, studio=studio, job_id=job_id, state="running",
        model=model, source="job", hub_owned=True, reported_at=receipt,
        activity_received_at=receipt, observed_at=receipt,
    )


def record_machine_state(*, machine: str, reachable: bool, working: bool,
                         state: str = "unknown", observed_at: float | None = None,
                         conn: sqlite3.Connection | None = None) -> bool:
    """Store only actual machine reachability/working transitions."""
    observed_at = float(time.time() if observed_at is None else observed_at)
    owns_connection = conn is None
    if conn is None:
        conn = _conn()
    try:
        previous = conn.execute(
            "SELECT reachable, working, state FROM machine_state_transitions "
            "WHERE machine = ? ORDER BY observed_at DESC, id DESC LIMIT 1",
            (machine,),
        ).fetchone()
        current = (int(bool(reachable)), int(bool(working)))
        if previous and (previous["reachable"], previous["working"], previous["state"]) == (*current, state):
            return False
        conn.execute(
            "INSERT INTO machine_state_transitions "
            "(machine, reachable, working, state, state_since, observed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (machine, *current, state, observed_at, observed_at),
        )
        if owns_connection:
            conn.commit()
    finally:
        if owns_connection:
            conn.close()
    return True


def machine_state_transitions(machine: str, *, before_s: float | None = None) -> list[dict]:
    where, args = ["machine = ?"], [machine]
    if before_s is not None:
        where.append("observed_at <= ?")
        args.append(float(before_s))
    with _conn() as conn:
        rows = conn.execute(
            "SELECT reachable, working, state, state_since, observed_at FROM machine_state_transitions "
            f"WHERE {' AND '.join(where)} ORDER BY observed_at ASC, id ASC", args,
        ).fetchall()
    return [dict(row) for row in rows]


def prune_activity(before_s: float, *, conn: sqlite3.Connection | None = None) -> None:
    """Keep the operational ledger deliberately bounded to the approved 30 days."""
    owns_connection = conn is None
    if conn is None:
        conn = _conn()
    try:
        conn.execute("DELETE FROM activity_events WHERE observed_at < ?", (before_s,))
        machines = conn.execute(
            "SELECT DISTINCT machine FROM machine_state_transitions WHERE observed_at < ?",
            (before_s,),
        ).fetchall()
        for item in machines:
            machine = item["machine"]
            predecessor = conn.execute(
                "SELECT id FROM machine_state_transitions "
                "WHERE machine = ? AND observed_at < ? ORDER BY observed_at DESC, id DESC LIMIT 1",
                (machine, before_s),
            ).fetchone()
            if predecessor:
                conn.execute(
                    "DELETE FROM machine_state_transitions WHERE machine = ? "
                    "AND observed_at < ? AND id != ?",
                    (machine, before_s, predecessor["id"]),
                )
            else:
                conn.execute(
                    "DELETE FROM machine_state_transitions WHERE machine = ? AND observed_at < ?",
                    (machine, before_s),
                )
        if owns_connection:
            conn.commit()
    finally:
        if owns_connection:
            conn.close()


def _is_corruption_error(exc: sqlite3.DatabaseError) -> bool:
    if getattr(exc, "sqlite_errorname", None) in {"SQLITE_CORRUPT", "SQLITE_NOTADB"}:
        return True
    message = str(exc).lower()
    return "database disk image is malformed" in message or "file is not a database" in message


def _database_family() -> tuple[Path, ...]:
    return tuple(
        Path(f"{DB_FILE}{suffix}")
        for suffix in ("", "-wal", "-shm", "-journal")
    )


def _validate_database() -> None:
    with _conn() as conn:
        result = conn.execute("PRAGMA quick_check(1)").fetchone()
    if result is None or result[0] != "ok":
        detail = result[0] if result else "no result"
        raise sqlite3.DatabaseError(
            f"database disk image is malformed: quick_check reported {detail}"
        )


def prepare_database() -> Path | None:
    """Validate the shared ledger before background work begins.

    A corrupt ledger must not keep the whole Hub offline. Preserve the complete
    SQLite file family for manual recovery, then create a clean ledger. Other
    machine state (enrollment, settings, voices, models, and outputs) lives in
    separate files and is never touched here.
    """
    with _PREPARE_LOCK:
        recovery_root = DB_FILE.parent / ".database-recovery"
        recovery: Path | None = None
        recovery_name = (
            f"hub-db-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:8]}"
        )
        # SQLite may discard an unusable WAL while opening a damaged main file,
        # so snapshot only the small sidecars before inspection. The main DB is
        # moved (not copied) if corruption is confirmed, avoiding a 100+ MB copy
        # on every healthy startup.
        for source in _database_family()[1:]:
            if not source.exists():
                continue
            if recovery is None:
                recovery = recovery_root / recovery_name
                recovery.mkdir(parents=True)
            shutil.copy2(source, recovery / source.name)
        try:
            _validate_database()
        except sqlite3.DatabaseError as exc:
            if not _is_corruption_error(exc):
                if recovery is not None:
                    shutil.rmtree(recovery)
                    try:
                        recovery_root.rmdir()
                    except OSError:
                        pass
                raise
        else:
            if recovery is not None:
                shutil.rmtree(recovery)
                try:
                    recovery_root.rmdir()
                except OSError:
                    pass
            return None

        if recovery is None:
            recovery = recovery_root / recovery_name
            recovery.mkdir(parents=True)
        try:
            for source in _database_family():
                if not source.exists():
                    continue
                target = recovery / source.name
                if target.exists():
                    target = recovery / f"post-probe-{source.name}"
                source.replace(target)
            _validate_database()
        except BaseException:
            # Never trade the preserved ledger for a failed rebuild. Keep any
            # newly-created files alongside it, then restore byte-for-byte
            # copies of the originals.
            for fresh in _database_family():
                if fresh.exists():
                    fresh.replace(recovery / f"fresh-{fresh.name}")
            for source in _database_family():
                original = recovery / source.name
                if original.exists():
                    shutil.copy2(original, source)
            raise
        return recovery


def save_batch(batch: dict):
    """Write-through persistence for the restart-safe broker queue."""
    states = {i["state"] for i in batch["items"]}
    finished = int(not (states & {"queued", "running"}))
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO batches (id, created_at, finished, payload) "
            "VALUES (?,?,?,?)",
            (batch["id"], batch["created_at"], finished, json.dumps(batch)))
        if batch.get("client_request_id") and batch.get("request_fingerprint"):
            conn.execute(
                "INSERT OR IGNORE INTO batch_requests "
                "(client_request_id, request_fingerprint, batch_id, item_count, created_at) "
                "VALUES (?,?,?,?,?)",
                (
                    batch["client_request_id"],
                    batch["request_fingerprint"],
                    batch["id"],
                    len(batch.get("items") or []),
                    batch["created_at"],
                ),
            )
    # Optional PostgreSQL observability is fire-and-forget execution evidence.
    # SQLite remains authoritative and a database outage can never fail the
    # local job save or influence dispatch.
    from . import control_plane
    control_plane.queue_shadow_job("generation", batch)


def load_unfinished_batches() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT payload FROM batches WHERE finished = 0").fetchall()
    return [json.loads(r[0]) for r in rows]


def load_finished_batches() -> list[dict]:
    """Load terminal broker batches for operator-requested history cleanup."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT payload FROM batches WHERE finished = 1").fetchall()
    return [json.loads(r[0]) for r in rows]


def delete_batches(batch_ids: list[str]) -> int:
    """Delete broker batch history. Asset records are managed separately."""
    ids = list(dict.fromkeys(batch_ids))
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    with _conn() as conn:
        cursor = conn.execute(
            f"DELETE FROM batches WHERE id IN ({placeholders})", ids)
    return cursor.rowcount


def remove_job_assets(batch_ids: list[str]) -> dict:
    """Remove Hub-owned asset records for finished batches.

    Worker output normally remains on the worker that produced it.  The Hub
    must never unlink an arbitrary path returned by a worker, so this only
    removes a file when it is physically inside this Hub's ``DATA_DIR``.  The
    ledger row is still removed in either case, which keeps a cleared job out
    of the Assets view without reaching into another Mac.
    """
    ids = list(dict.fromkeys(batch_ids))
    if not ids:
        return {"assets_removed": 0, "files_removed": 0, "reclaimed_bytes": 0}
    placeholders = ",".join("?" for _ in ids)
    with _conn() as conn:
        rows = [dict(row) for row in conn.execute(
            f"SELECT id, artifact_path FROM assets "
            f"WHERE source = 'job' AND batch_id IN ({placeholders})", ids)]
        conn.execute(
            f"DELETE FROM assets WHERE source = 'job' AND batch_id IN ({placeholders})", ids)

    root = DATA_DIR.resolve()
    files_removed = reclaimed = 0
    for row in rows:
        raw_path = row.get("artifact_path")
        if not raw_path:
            continue
        try:
            path = Path(raw_path).resolve()
            safe = path.is_relative_to(root)
        except (OSError, ValueError):
            safe = False
        if not safe or not path.is_file():
            continue
        try:
            reclaimed += path.stat().st_size
            path.unlink()
            files_removed += 1
        except OSError:
            pass
    return {"assets_removed": len(rows), "files_removed": files_removed,
            "reclaimed_bytes": reclaimed}


def get_asset(asset_id: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    return dict(row) if row else None


def load_batch(batch_id: str) -> dict | None:
    """Fetch any persisted batch — lets clients query batches that finished
    before a Hub restart (they're no longer in broker memory)."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT payload FROM batches WHERE id = ?", (batch_id,)).fetchone()
    return json.loads(row[0]) if row else None


def load_batch_by_client_request_id(client_request_id: str) -> dict | None:
    """Find an idempotent submission across unfinished and finished history."""
    with _conn() as conn:
        request = conn.execute(
            "SELECT request_fingerprint, batch_id, item_count, created_at "
            "FROM batch_requests WHERE client_request_id = ?",
            (client_request_id,),
        ).fetchone()
        if request is None:
            return None
        row = conn.execute(
            "SELECT payload FROM batches WHERE id = ?", (request["batch_id"],)
        ).fetchone()
    if row is not None:
        try:
            return json.loads(row[0])
        except (TypeError, json.JSONDecodeError):
            pass
    return {
        "id": request["batch_id"],
        "client_request_id": client_request_id,
        "request_fingerprint": request["request_fingerprint"],
        "items": [None] * int(request["item_count"]),
        "created_at": request["created_at"],
    }


def record_asset(**fields) -> str:
    asset_id = fields.pop("id", None) or uuid.uuid4().hex[:12]
    row = {
        "id": asset_id, "created_at": fields.pop("created_at", time.time()),
        "source": fields.pop("source", "job"),
        "modality": fields.pop("modality", None),
        "studio": fields.pop("studio", None),
        "machine": fields.pop("machine", "local"),
        "model": fields.pop("model", None),
        "seed": fields.pop("seed", None),
        "prompt": fields.pop("prompt", None),
        "params_json": json.dumps(fields.pop("params", None) or {}),
        "artifact_path": fields.pop("artifact_path", None),
        "artifact_url": fields.pop("artifact_url", None),
        "batch_id": fields.pop("batch_id", None),
        "item_index": fields.pop("item_index", None),
        "recipe_id": fields.pop("recipe_id", None),
        "duration_s": fields.pop("duration_s", None),
        "runtime_s": fields.pop("runtime_s", None),
    }
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO assets ({}) VALUES ({})".format(
                ",".join(row), ",".join("?" * len(row))),
            list(row.values()),
        )
    return asset_id


def scan_outputs(registry: list[dict]) -> dict:
    """Index files under each local studio's app/output. Idempotent —
    artifact_path is unique, existing rows are left alone."""
    added, seen = 0, 0
    with _conn() as conn:
        known = {r[0] for r in conn.execute(
            "SELECT artifact_path FROM assets WHERE artifact_path IS NOT NULL")}
        for s in registry:
            if s.get("machine", "local") != "local" or not s.get("app"):
                continue
            out_dir = PINOKIO_HOME / "api" / s["app"] / "app" / "output"
            if not out_dir.exists():
                continue
            for f in out_dir.rglob("*"):
                if not f.is_file() or f.suffix.lower() not in MEDIA_EXT:
                    continue
                seen += 1
                path = str(f)
                if path in known:
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO assets "
                    "(id, created_at, source, modality, studio, machine, "
                    " params_json, artifact_path) VALUES (?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex[:12], f.stat().st_mtime, "scan",
                     MEDIA_EXT[f.suffix.lower()], s["id"], "local", "{}", path),
                )
                added += 1
    return {"scanned": seen, "added": added}


# ── Operation type ──
# The user-facing "operation type" (image / voice / music / video / chat).
# Prefer the STUDIO that produced the asset (machine suffix stripped), because
# scanned files tag `modality` as the coarse MEDIA type (image/audio/video) —
# which can't tell TTS (voice) from music. The studio can. Fall back to the
# media modality when no studio is recorded (e.g. uploads).
_OP_SQL = (
    "CASE WHEN studio IS NOT NULL AND studio != '' THEN "
    " CASE WHEN instr(studio,'@')>0 THEN substr(studio,1,instr(studio,'@')-1) "
    " ELSE studio END "
    "ELSE COALESCE(modality,'unknown') END"
)


def _stats_where(source: str | None, op: str | None, machine: str | None,
                 since_s: float | None) -> tuple[str, list]:
    """Build the shared WHERE clause + args for stats/timeline.

    source: 'all' (default) | 'job' (Hub-dispatched) | 'direct' (scans + uploads
    made straight in a studio). op/machine narrow to one operation type / one
    machine. All values are parameterized (or fixed literals) — no injection."""
    where = ["1=1"]
    args: list = []
    if source == "job":
        where.append("source = 'job'")
    elif source == "direct":
        where.append("source != 'job'")
    # 'all' / None → every source
    if since_s:
        where.append("created_at >= ?"); args.append(since_s)
    if op:
        where.append(f"({_OP_SQL}) = ?"); args.append(op)
    if machine:
        where.append("machine = ?"); args.append(machine)
    return " AND ".join(where), args


def stats(since_s: float | None = None, source: str = "all",
          op: str | None = None, machine: str | None = None) -> dict:
    """Generation analytics from the ledger. Counts span every source by
    default (Hub jobs + direct-in-studio scans + uploads); pass source='job'
    or 'direct' to split them. Groups by operation type (see _OP_SQL) so voice
    and music are distinct even for scanned audio. Timing/model stats only
    reflect assets that carry a duration/model (Hub jobs) — SQL AVG ignores
    the nulls. Also returns the full option lists (`available_*`) for the
    filter UI, computed independent of the op/machine filters."""
    where, args = _stats_where(source, op, machine, since_s)
    with _conn() as conn:
        cells = conn.execute(
            f"SELECT machine, ({_OP_SQL}) op, COUNT(*) c, "
            f"AVG(COALESCE(runtime_s,duration_s)) avg_s, "
            f"MIN(COALESCE(runtime_s,duration_s)) min_s, "
            f"MAX(COALESCE(runtime_s,duration_s)) max_s, "
            f"SUM(COALESCE(runtime_s,duration_s,0)) sum_s, "
            # timed_c: how many rows in this group actually carry a duration.
            # A group can now mix timed jobs + untimed scans, so the aggregate
            # avg must divide sum_s by this, NOT by the full count.
            f"SUM(CASE WHEN COALESCE(runtime_s,duration_s) IS NOT NULL THEN 1 ELSE 0 END) timed_c "
            f"FROM assets WHERE {where} GROUP BY machine, op", args).fetchall()
        total = conn.execute(
            f"SELECT COUNT(*) FROM assets WHERE {where}", args).fetchone()[0]
        model_rows = conn.execute(
            f"SELECT model, ({_OP_SQL}) op, COUNT(*) c, "
            f"AVG(COALESCE(runtime_s,duration_s)) avg_s "
            f"FROM assets WHERE {where} GROUP BY model", args).fetchall()
        src_rows = conn.execute(
            f"SELECT source, COUNT(*) c FROM assets WHERE {where} GROUP BY source",
            args).fetchall()
        # Option lists for the filter chips — narrowed by source+window only, so
        # picking an op/machine filter doesn't make the other options vanish.
        aw, aargs = _stats_where(source, None, None, since_s)
        avail_ops = conn.execute(
            f"SELECT DISTINCT ({_OP_SQL}) op FROM assets WHERE {aw}", aargs).fetchall()
        avail_mach = conn.execute(
            f"SELECT DISTINCT machine FROM assets WHERE {aw}", aargs).fetchall()

    def _round(x):
        return round(x, 2) if x is not None else None

    by_machine: dict = {}
    by_modality: dict = {}
    matrix = []
    for r in cells:
        m = r["machine"] or "unknown"
        modality = r["op"] or "unknown"
        matrix.append({"machine": m, "modality": modality, "count": r["c"],
                       "avg_s": _round(r["avg_s"]), "min_s": _round(r["min_s"]),
                       "max_s": _round(r["max_s"])})
        bm = by_machine.setdefault(m, {"count": 0, "sum_s": 0.0, "timed": 0,
                                       "modalities": {}})
        bm["count"] += r["c"]
        bm["sum_s"] += r["sum_s"] or 0
        bm["timed"] += r["timed_c"] or 0
        bm["modalities"][modality] = bm["modalities"].get(modality, 0) + r["c"]
        md = by_modality.setdefault(modality, {"count": 0, "sum_s": 0.0, "timed": 0,
                                               "machines": {}})
        md["count"] += r["c"]
        md["sum_s"] += r["sum_s"] or 0
        md["timed"] += r["timed_c"] or 0
        md["machines"][m] = md["machines"].get(m, 0) + r["c"]
    for d in (*by_machine.values(), *by_modality.values()):
        d["avg_s"] = _round(d["sum_s"] / d["timed"]) if d["timed"] else None
        d.pop("sum_s", None)
        d.pop("timed", None)
    by_model = {r["model"]: {"count": r["c"], "avg_s": _round(r["avg_s"]),
                             "modality": r["op"]}
                for r in model_rows if r["model"]}
    by_source = {r["source"] or "unknown": r["c"] for r in src_rows}
    return {"total": total, "by_machine": by_machine, "by_modality": by_modality,
            "by_model": by_model, "matrix": matrix, "by_source": by_source,
            "available_modalities": sorted(r["op"] for r in avail_ops if r["op"]),
            "available_machines": sorted(r["machine"] for r in avail_mach if r["machine"])}


def timeline(since_s: float | None, bucket_s: int, source: str = "all",
             op: str | None = None, machine: str | None = None) -> dict:
    """Generations bucketed over time, split by operation type — for a
    throughput chart. Honours the same source/op/machine filters as stats()."""
    where, args = _stats_where(source, op, machine, since_s)
    bucket_s = int(bucket_s)
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT CAST(created_at / {bucket_s} AS INTEGER) b, ({_OP_SQL}) op, COUNT(*) c "
            f"FROM assets WHERE {where} GROUP BY b, op ORDER BY b", args).fetchall()
    if not rows:
        return {"bucket_s": bucket_s, "buckets": [], "series": {}}
    bmin = rows[0]["b"]
    bmax = max(r["b"] for r in rows)
    n = min(bmax - bmin + 1, 400)  # safety cap
    series: dict = {}
    for r in rows:
        idx = r["b"] - bmin
        if idx >= n:
            continue
        series.setdefault(r["op"] or "unknown", [0] * n)[idx] += r["c"]
    buckets = [(bmin + i) * bucket_s for i in range(n)]
    return {"bucket_s": bucket_s, "buckets": buckets, "series": series}


def query_assets(q: str | None = None, modality: str | None = None,
                 studio: str | None = None, batch_id: str | None = None,
                 limit: int = 100, sort: str = "newest") -> list[dict]:
    sql = "SELECT * FROM assets WHERE 1=1"
    args: list = []
    if q:
        sql += " AND (prompt LIKE ? OR model LIKE ? OR artifact_path LIKE ?)"
        args += [f"%{q}%"] * 3
    if modality:
        sql += " AND modality = ?"
        args.append(modality)
    if studio:
        sql += " AND studio = ?"
        args.append(studio)
    if batch_id:
        sql += " AND batch_id = ?"
        args.append(batch_id)
    order_by = {
        "newest": "created_at DESC",
        "oldest": "created_at ASC",
        "name": "LOWER(COALESCE(artifact_path, id)) ASC, created_at DESC",
        "type": "LOWER(COALESCE(modality, '')) ASC, created_at DESC",
        "studio": "LOWER(COALESCE(studio, '')) ASC, created_at DESC",
        "model": "LOWER(COALESCE(model, '')) ASC, created_at DESC",
    }.get(sort, "created_at DESC")
    sql += f" ORDER BY {order_by} LIMIT ?"
    args.append(min(limit, 500))
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute(sql, args)]
    for r in rows:
        r["params"] = json.loads(r.pop("params_json") or "{}")
        r["exists"] = bool(r["artifact_path"]) and Path(r["artifact_path"]).exists()
    return rows
