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

DB_FILE = LAUNCHER_ROOT / "hub.db"

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
  recipe_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_assets_created ON assets(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_assets_batch ON assets(batch_id);
"""


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
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


def query_assets(q: str | None = None, modality: str | None = None,
                 studio: str | None = None, batch_id: str | None = None,
                 limit: int = 100) -> list[dict]:
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
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(min(limit, 500))
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute(sql, args)]
    for r in rows:
        r["params"] = json.loads(r.pop("params_json") or "{}")
        r["exists"] = bool(r["artifact_path"]) and Path(r["artifact_path"]).exists()
    return rows
