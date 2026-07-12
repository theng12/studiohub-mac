"""Unified asset ledger — index + link, never copy (SPEC §7).

SQLite at the launcher root (hub.db, gitignored). Two sources of truth:
- `job`: assets created through the Hub's broker/recipes — full provenance
  (prompt, model, seed, params, batch/recipe ids). The reproducibility payoff.
- `scan`: files discovered in local studios' app/output folders — basic file
  facts only; provenance fields stay null (the studios don't write sidecars).

Artifacts stay in each studio's own output folder; the ledger stores the path
plus (when known) the studio's serving URL.
"""

import json
import sqlite3
import time
import uuid
from pathlib import Path

from .control import PINOKIO_HOME
from .registry import LAUNCHER_ROOT

from .registry import DATA_DIR
DB_FILE = DATA_DIR / "hub.db"

MEDIA_EXT = {
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image",
    ".wav": "audio", ".mp3": "audio", ".flac": "audio", ".ogg": "audio",
    ".mp4": "video", ".mov": "video", ".webm": "video",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
  id TEXT PRIMARY KEY,
  created_at REAL NOT NULL,
  finished INTEGER NOT NULL DEFAULT 0,
  payload TEXT NOT NULL              -- full batch dict as JSON (write-through)
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
  duration_s REAL                 -- generation time in seconds (analytics)
);
CREATE INDEX IF NOT EXISTS idx_assets_created ON assets(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_assets_batch ON assets(batch_id);
"""


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    # Migration for DBs created before duration_s existed.
    try:
        conn.execute("ALTER TABLE assets ADD COLUMN duration_s REAL")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already present
    return conn


def save_batch(batch: dict):
    """Write-through persistence for the broker queue (SPEC: restart-safe)."""
    states = {i["state"] for i in batch["items"]}
    finished = int(not (states & {"queued", "running"}))
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO batches (id, created_at, finished, payload) "
            "VALUES (?,?,?,?)",
            (batch["id"], batch["created_at"], finished, json.dumps(batch)))


def load_unfinished_batches() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT payload FROM batches WHERE finished = 0").fetchall()
    return [json.loads(r[0]) for r in rows]


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
            f"AVG(duration_s) avg_s, MIN(duration_s) min_s, MAX(duration_s) max_s, "
            f"SUM(COALESCE(duration_s,0)) sum_s, "
            # timed_c: how many rows in this group actually carry a duration.
            # A group can now mix timed jobs + untimed scans, so the aggregate
            # avg must divide sum_s by this, NOT by the full count.
            f"SUM(CASE WHEN duration_s IS NOT NULL THEN 1 ELSE 0 END) timed_c "
            f"FROM assets WHERE {where} GROUP BY machine, op", args).fetchall()
        total = conn.execute(
            f"SELECT COUNT(*) FROM assets WHERE {where}", args).fetchone()[0]
        model_rows = conn.execute(
            f"SELECT model, ({_OP_SQL}) op, COUNT(*) c, AVG(duration_s) avg_s "
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
