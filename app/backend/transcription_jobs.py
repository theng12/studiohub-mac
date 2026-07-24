"""Persistent, fleet-wide Whisper transcription batches."""

import asyncio
import hashlib
import json
import re
import shutil
import sqlite3
import time
import uuid
from pathlib import Path

import httpx
from fastapi import HTTPException, UploadFile

from . import broker, execution_identity, ledger
from .peers import studio_request
from .registry import DATA_DIR, machine_enabled, studio_enabled

ROOT = DATA_DIR / "transcription_jobs"
SETTINGS_FILE = DATA_DIR / "transcription_settings.json"
CHUNK_BYTES = 1024 * 1024
MAX_FILE_BYTES = 2 * 1024 * 1024 * 1024
MAX_BATCH_BYTES = 20 * 1024 * 1024 * 1024
MAX_FILES = 500
MAX_TRIES = 3
RETENTION_CHOICES = {1, 3, 7, 15, 30, 90}
ALLOWED_EXTENSIONS = {
    ".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus",
    ".webm", ".mp4", ".mov",
}
TRANSIENT_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
TERMINAL_STATES = {"done", "error", "cancelled"}

batches: dict[str, dict] = {}
busy_studios: set[str] = set()
_item_tasks: dict[tuple[str, int], asyncio.Task] = {}
_dispatcher_task: asyncio.Task | None = None
_cleanup_task: asyncio.Task | None = None
_wakeup = asyncio.Event()
_shutting_down = False

_SCHEMA = """
CREATE TABLE IF NOT EXISTS transcription_batches (
  id TEXT PRIMARY KEY,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  finished INTEGER NOT NULL DEFAULT 0,
  idempotency_key TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transcription_created
  ON transcription_batches(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_transcription_idempotency
  ON transcription_batches(idempotency_key, finished);
"""


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(ledger.DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def _save(batch: dict) -> None:
    batch["updated_at"] = time.time()
    finished = int(all(i["state"] in TERMINAL_STATES for i in batch["items"]))
    if finished and not batch.get("finished_at"):
        batch["finished_at"] = batch["updated_at"]
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO transcription_batches "
            "(id,created_at,updated_at,finished,idempotency_key,payload) "
            "VALUES (?,?,?,?,?,?)",
            (batch["id"], batch["created_at"], batch["updated_at"], finished,
             batch["idempotency_key"], json.dumps(batch)),
        )
    from . import control_plane
    control_plane.queue_shadow_job("transcription", batch)


def _load_rows(where: str = "", params: tuple = ()) -> list[dict]:
    sql = "SELECT payload FROM transcription_batches"
    if where:
        sql += " WHERE " + where
    sql += " ORDER BY created_at DESC"
    with _conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [json.loads(row[0]) for row in rows]


def get_batch(batch_id: str) -> dict | None:
    if batch_id in batches:
        return batches[batch_id]
    rows = _load_rows("id = ?", (batch_id,))
    return rows[0] if rows else None


def _safe_identifier(value: str, field: str, max_length: int = 120) -> str:
    value = (value or "").strip()
    if not value or len(value) > max_length or value in {".", ".."}:
        raise HTTPException(400, f"invalid {field}")
    if "/" in value or "\\" in value or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._ -]*", value):
        raise HTTPException(400, f"invalid {field}")
    return value


def _safe_filename(value: str) -> str:
    value = (value or "").strip()
    if (
        not value
        or len(value) > 240
        or value in {".", ".."}
        or Path(value).name != value
        or "/" in value
        or "\\" in value
        or "\x00" in value
        or any(ord(char) < 32 for char in value)
    ):
        raise HTTPException(400, "invalid filename")
    # A filename is display metadata only: uploaded bytes are stored under a
    # generated path below. Keep path traversal protections while accepting
    # ordinary punctuation such as commas and parentheses from user files.
    return value


def _safe_model(value: str) -> str:
    value = (value or "").strip()
    if (not value or len(value) > 240 or value.startswith("/") or "\\" in value
            or ".." in value.split("/")
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", value)):
        raise HTTPException(400, "invalid model")
    return value


def _idempotency_key(project: str | None, episode: str | None, model: str,
                     item_ids: list[str], filenames: list[str]) -> str:
    canonical = json.dumps({
        "project": project or "", "episode": episode or "", "model": model,
        "items": list(zip(item_ids, filenames)),
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _active_duplicate(key: str) -> dict | None:
    rows = _load_rows("idempotency_key = ? AND finished = 0", (key,))
    return rows[0] if rows else None


async def create_batch(files: list[UploadFile], item_ids: list[str], model: str,
                       language: str | None, word_timestamps: bool, label: str | None,
                       project: str | None, episode: str | None,
                       genstudio_execution_json: str | None = None,
                       deduplicate: bool = True) -> tuple[dict, bool]:
    if not files or len(files) > MAX_FILES:
        raise HTTPException(400, f"files must contain 1 to {MAX_FILES} uploads")
    if len(item_ids) != len(files):
        raise HTTPException(400, "item_ids must contain one stable ID per file")
    model = _safe_model(model)
    clean_ids = [_safe_identifier(v, "item_id") for v in item_ids]
    if len(set(clean_ids)) != len(clean_ids):
        raise HTTPException(400, "item_ids must be unique within a batch")
    filenames = [_safe_filename(f.filename or "") for f in files]
    for filename in filenames:
        if Path(filename).suffix.lower() not in ALLOWED_EXTENSIONS:
            raise HTTPException(415, f"unsupported audio/video type: {Path(filename).suffix or 'none'}")
    clean_project = _safe_identifier(project, "project") if project else None
    clean_episode = _safe_identifier(episode, "episode") if episode else None
    clean_label = _safe_identifier(label, "label") if label else None
    execution = None
    if genstudio_execution_json:
        try:
            execution = json.loads(genstudio_execution_json)
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                400, "genstudio_execution must be valid JSON"
            ) from exc
        if not isinstance(execution, dict):
            raise HTTPException(400, "genstudio_execution must be a JSON object")
    envelope = {
        "modality": "transcription",
        "model": model,
        "language": language or None,
        "word_timestamps": bool(word_timestamps),
        "project": clean_project,
        "episode": clean_episode,
        "items": [
            {"item_id": item_id, "filename": filename}
            for item_id, filename in zip(clean_ids, filenames)
        ],
    }
    if execution is not None:
        envelope["genstudio_execution"] = execution
    try:
        prepared = execution_identity.prepare(envelope)
    except execution_identity.ExecutionIdentityError as exc:
        raise HTTPException(409, str(exc)) from exc
    if prepared.replay_batch_id:
        replay = get_batch(prepared.replay_batch_id)
        if replay is not None:
            for upload in files:
                await upload.close()
            batches.setdefault(replay["id"], replay)
            return replay, True
    key = _idempotency_key(clean_project, clean_episode, model, clean_ids, filenames)
    duplicate = (
        _active_duplicate(key)
        if deduplicate and prepared.evidence is None
        else None
    )
    if duplicate:
        for upload in files:
            await upload.close()
        batches.setdefault(duplicate["id"], duplicate)
        return duplicate, True

    batch_id = uuid.uuid4().hex[:12]
    batch_dir = ROOT / batch_id
    input_dir = batch_dir / "input"
    output_dir = batch_dir / "output"
    total_bytes = 0
    items = []
    try:
        input_dir.mkdir(parents=True, exist_ok=False)
        output_dir.mkdir()
        for index, (upload, item_id, filename) in enumerate(zip(files, clean_ids, filenames)):
            suffix = Path(filename).suffix.lower()
            stored = input_dir / f"{index:04d}-{hashlib.sha256(item_id.encode()).hexdigest()[:12]}{suffix}"
            file_bytes = 0
            with stored.open("wb") as handle:
                while chunk := await upload.read(CHUNK_BYTES):
                    file_bytes += len(chunk)
                    total_bytes += len(chunk)
                    if file_bytes > MAX_FILE_BYTES:
                        raise HTTPException(413, f"{filename} exceeds the per-file upload limit")
                    if total_bytes > MAX_BATCH_BYTES:
                        raise HTTPException(413, "batch exceeds the total upload limit")
                    handle.write(chunk)
            if file_bytes == 0:
                raise HTTPException(400, f"{filename} is empty")
            items.append({
                "index": index, "item_id": item_id, "filename": filename,
                "input_path": str(stored), "bytes": file_bytes, "state": "queued",
                "tries": 0, "studio": None, "studio_task_id": None,
                "duration_seconds": None, "media_duration_seconds": None,
                "artifact_path": None, "error": None, "metadata": None,
                # A local failure must not immediately reselect the same Mac.
                # The shared broker circuit and this per-item avoidance window
                # let another Voice Studio recover the work first.
                "avoid_machines": {}, "attempt_history": [],
            })
    except Exception:
        shutil.rmtree(batch_dir, ignore_errors=True)
        raise
    finally:
        for upload in files:
            await upload.close()

    now = time.time()
    batch = {
        "id": batch_id, "idempotency_key": key, "created_at": now,
        "updated_at": now, "finished_at": None, "cancelled": False,
        "label": clean_label, "project": clean_project, "episode": clean_episode,
        "model": model, "language": language or None,
        "word_timestamps": bool(word_timestamps), "items": items,
        "genstudio_execution": prepared.evidence,
    }
    batches[batch_id] = batch
    _save(batch)
    execution_identity.bind_local_batch(prepared.evidence, batch_id)
    _wakeup.set()
    return batch, False


def summary(batch: dict, include_items: bool = True,
            include_metadata: bool = False) -> dict:
    states = [item["state"] for item in batch["items"]]
    counts = {state: states.count(state) for state in ("queued", "running", "done", "error", "cancelled")}
    if counts["queued"] or counts["running"]:
        status = "running" if counts["running"] else "queued"
    elif counts["cancelled"] == len(states):
        status = "cancelled"
    elif counts["error"] and counts["done"]:
        status = "partial"
    elif counts["error"]:
        status = "error"
    elif counts["cancelled"]:
        status = "partial" if counts["done"] else "cancelled"
    else:
        status = "done"
    out = {
        "id": batch["id"], "status": status, "label": batch.get("label"),
        "project": batch.get("project"), "episode": batch.get("episode"),
        "model": batch["model"], "language": batch.get("language"),
        "word_timestamps": batch.get("word_timestamps", False),
        "created_at": batch["created_at"], "updated_at": batch.get("updated_at"),
        "finished_at": batch.get("finished_at"), "total": len(states), **counts,
        "duration_seconds": round(sum(i.get("duration_seconds") or 0 for i in batch["items"]), 2),
        "storage_cleaned": bool(batch.get("storage_cleaned_at")),
    }
    execution = batch.get("genstudio_execution")
    if isinstance(execution, dict):
        out["genstudio_execution"] = {
            "genstudio_job_id": execution.get("genstudio_job_id"),
            "genstudio_attempt_id": execution.get("genstudio_attempt_id"),
            "fencing_token": execution.get("fencing_token"),
            "lease_expires_at": execution.get("lease_expires_at"),
        }
    if include_items:
        out["items"] = [{
            "index": item["index"], "item_id": item["item_id"],
            "filename": item["filename"], "state": item["state"],
            "studio": item.get("studio"), "studio_task_id": item.get("studio_task_id"),
            "duration_seconds": item.get("duration_seconds"),
            "media_duration_seconds": item.get("media_duration_seconds"),
            "artifact_url": (f"/api/hub/transcription/jobs/{batch['id']}/items/"
                             f"{item['index']}/artifact" if item["state"] == "done"
                             and not batch.get("storage_cleaned_at") else None),
            "error": item.get("error"), "tries": item["tries"],
            **({"metadata": item.get("metadata")} if include_metadata else {}),
        } for item in batch["items"]]
    return out


def list_batches() -> list[dict]:
    persisted = {b["id"]: b for b in _load_rows()}
    persisted.update(batches)
    return [summary(b, include_items=True) for b in sorted(
        persisted.values(), key=lambda row: row["created_at"], reverse=True)]


def active_assignments() -> dict[str, dict]:
    """Current transcription lease details keyed by Voice Studio id."""
    active = {}
    for batch in batches.values():
        for item in batch["items"]:
            studio = item.get("studio")
            if item["state"] == "running" and studio:
                active[studio] = {
                    "kind": "transcription", "batch_id": batch["id"],
                    "project": batch.get("project"), "episode": batch.get("episode"),
                    "item_id": item["item_id"], "attempt": item["tries"],
                    "max_attempts": MAX_TRIES, "started_at": item.get("started_at"),
                }
    return active


async def _eligible_studios(monitor, model: str, item: dict | None = None) -> list[dict]:
    eligible = []
    now = time.time()
    avoided = (item or {}).get("avoid_machines") or {}
    for studio in monitor.registry:
        if (studio.get("modality") != "voice" or studio["id"] in busy_studios
                or broker.in_maintenance(studio["id"])):
            continue
        machine = studio.get("machine", "local")
        if (monitor.status.get(studio["id"], {}).get("status") != "up"
                or not machine_enabled(machine)
                or not studio_enabled(machine, studio["id"])
                or machine in broker.busy_machines()
                or broker.machine_is_quarantined(machine)
                or float(avoided.get(machine, 0) or 0) > now):
            continue
        availability = await monitor.get_transcription(studio)
        models = (availability or {}).get("models", [])
        if (availability or {}).get("available") and any(
                m.get("repo") == model and m.get("cached", True) for m in models):
            eligible.append(studio)
    return eligible


def _expire_genstudio_batch(batch: dict) -> bool:
    if not execution_identity.lease_expired(batch.get("genstudio_execution")):
        return False
    batch["cancelled"] = True
    batch["lease_expired"] = True
    now = time.time()
    for item in batch.get("items") or []:
        if item.get("state") in {"queued", "running"}:
            item.update(
                state="cancelled",
                error="GenStudio execution lease expired",
                finished_at=now,
            )
    return True


def renew_execution_lease(renewal: dict) -> bool:
    batch_id = renewal.get("local_batch_id")
    batch = batches.get(batch_id) if batch_id else None
    if batch is None and batch_id:
        batch = get_batch(batch_id)
    if batch is None:
        batch = next(
            (
                candidate
                for candidate in batches.values()
                if (candidate.get("genstudio_execution") or {}).get(
                    "genstudio_attempt_id"
                )
                == renewal.get("genstudio_attempt_id")
            ),
            None,
        )
    if batch is None:
        return False
    evidence = dict(batch.get("genstudio_execution") or {})
    evidence["lease_expires_at"] = renewal["lease_expires_at"]
    batch["genstudio_execution"] = evidence
    batches[batch["id"]] = batch
    _save(batch)
    _wakeup.set()
    return True


async def dispatch_once(monitor) -> int:
    assigned = 0
    while True:
        made_progress = False
        active = sorted(
            (batch for batch in batches.values() if not batch.get("cancelled")),
            key=lambda batch: (batch.get("last_dispatched_at", 0), batch["created_at"]),
        )
        for batch in active:
            if _expire_genstudio_batch(batch):
                _save(batch)
                continue
            item = next((i for i in batch["items"] if i["state"] == "queued"), None)
            if not item:
                continue
            for studio in await _eligible_studios(monitor, batch["model"], item):
                machine = studio.get("machine", "local")
                owner = f"transcription:{batch['id']}:{item['index']}"
                if not broker.acquire_external_machine(machine, owner):
                    continue
                item.update(state="running", studio=studio["id"], error=None,
                            started_at=time.time(), tries=item["tries"] + 1)
                batch["last_dispatched_at"] = time.time()
                busy_studios.add(studio["id"])
                _save(batch)
                task = asyncio.create_task(_run_item(monitor, batch, item, studio, owner))
                _item_tasks[(batch["id"], item["index"])] = task
                assigned += 1
                made_progress = True
                break
        if not made_progress:
            break
    return assigned


async def _run_item(monitor, batch: dict, item: dict, studio: dict, owner: str) -> None:
    started = time.time()
    try:
        if _expire_genstudio_batch(batch):
            return
        input_path = Path(item["input_path"])
        if not input_path.is_file():
            raise FileNotFoundError("uploaded audio is missing")
        data = {
            "model": batch["model"],
            "word_timestamps": str(bool(batch.get("word_timestamps"))).lower(),
        }
        if batch.get("language"):
            data["language"] = batch["language"]
        url, headers = studio_request(studio, "/api/transcribe")
        with input_path.open("rb") as handle:
            response = await monitor._client.post(
                url, data=data,
                files={"file": (item["filename"], handle, "application/octet-stream")},
                headers=headers, timeout=300.0,
            )
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail")
            except Exception:
                detail = getattr(response, "text", "")
            error = RuntimeError(f"HTTP {response.status_code}: {detail or 'transcription failed'}")
            error.transient = response.status_code in TRANSIENT_STATUS
            raise error
        payload = response.json()
        if _expire_genstudio_batch(batch):
            return
        srt = str(payload.get("srt") or "")
        if not srt.strip():
            raise ValueError("Voice Studio returned an empty SRT artifact")
        artifact = ROOT / batch["id"] / "output" / f"{item['index']:04d}.srt"
        temporary = artifact.with_suffix(".srt.part")
        temporary.write_text(srt, encoding="utf-8")
        temporary.replace(artifact)
        if not artifact.is_file() or artifact.stat().st_size == 0:
            raise ValueError("SRT artifact verification failed")
        item.update(
            state="done", artifact_path=str(artifact), error=None,
            studio_task_id=payload.get("studio_task_id") or payload.get("task_id"),
            duration_seconds=round(float(payload.get("elapsed_seconds") or time.time() - started), 2),
            media_duration_seconds=payload.get("duration"),
            metadata={k: v for k, v in payload.items() if k != "srt"},
        )
        broker.mark_external_machine_success(studio)
    except asyncio.CancelledError:
        if _shutting_down and not batch.get("cancelled"):
            item.update(state="queued", studio=None, studio_task_id=None,
                        error="interrupted by Hub shutdown", interrupted=True)
        else:
            item.update(state="cancelled", error="cancelled")
    except Exception as exc:
        transient = isinstance(exc, (httpx.HTTPError, OSError)) or getattr(exc, "transient", False)
        message = str(exc) or type(exc).__name__
        machine = studio.get("machine", "local")
        history = item.setdefault("attempt_history", [])
        history.append({
            "studio": studio.get("id"), "machine": machine,
            "error": message[:240], "at": round(time.time(), 3),
        })
        del history[:-8]
        if transient:
            # Availability is a cache of a lightweight endpoint. A real
            # transcription connection failure is stronger evidence, so force
            # a fresh check and route the next attempt to a different machine.
            monitor._transcribe_cache.pop(studio["id"], None)
            broker.mark_external_machine_failure(studio, message)
            item.setdefault("avoid_machines", {})[machine] = time.time() + broker.FAILED_WORKER_AVOID_S
        if transient and item["tries"] < MAX_TRIES and not batch.get("cancelled"):
            item.update(state="queued", studio=None, studio_task_id=None,
                        error=f"try {item['tries']} failed on {machine}: {message}")
        else:
            attempted = ", ".join(
                f"{entry.get('machine')} ({entry.get('error')})"
                for entry in history
            )
            item.update(
                state="error",
                error=f"{message}. Workers attempted: {attempted}" if attempted else message,
            )
    finally:
        busy_studios.discard(studio["id"])
        broker.release_external_machine(studio.get("machine", "local"), owner)
        _item_tasks.pop((batch["id"], item["index"]), None)
        _save(batch)
        _wakeup.set()


async def _dispatch_loop(monitor) -> None:
    while True:
        try:
            assigned = await dispatch_once(monitor)
            _wakeup.clear()
            try:
                await asyncio.wait_for(_wakeup.wait(), timeout=0.1 if assigned else 2.0)
            except asyncio.TimeoutError:
                pass
        except asyncio.CancelledError:
            return
        except Exception:
            await asyncio.sleep(2)


def restore_batches() -> int:
    restored = 0
    for batch in _load_rows("finished = 0"):
        if _expire_genstudio_batch(batch):
            batches[batch["id"]] = batch
            _save(batch)
            restored += 1
            continue
        for item in batch["items"]:
            if item["state"] == "running":
                item.update(state="queued", studio=None, studio_task_id=None,
                            error="interrupted by Hub restart", interrupted=True)
            if item["state"] == "queued" and not Path(item["input_path"]).is_file():
                item.update(state="error", error="uploaded audio is missing")
        batches[batch["id"]] = batch
        _save(batch)
        restored += 1
    if restored:
        _wakeup.set()
    return restored


def start_dispatcher(monitor) -> None:
    global _dispatcher_task, _cleanup_task, _shutting_down
    _shutting_down = False
    loop = asyncio.get_running_loop()
    if _dispatcher_task is None or _dispatcher_task.done() or _dispatcher_task.get_loop() is not loop:
        _dispatcher_task = asyncio.create_task(_dispatch_loop(monitor))
    if _cleanup_task is None or _cleanup_task.done() or _cleanup_task.get_loop() is not loop:
        _cleanup_task = asyncio.create_task(_cleanup_loop())


async def stop() -> None:
    global _dispatcher_task, _cleanup_task, _shutting_down
    _shutting_down = True
    tasks = [t for t in (_dispatcher_task, _cleanup_task, *_item_tasks.values()) if t and not t.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _dispatcher_task = _cleanup_task = None


async def cancel_batch(batch_id: str) -> dict | None:
    batch = get_batch(batch_id)
    if not batch:
        return None
    batch["cancelled"] = True
    for item in batch["items"]:
        if item["state"] == "queued":
            item.update(state="cancelled", error="cancelled")
        elif item["state"] == "running":
            task = _item_tasks.get((batch_id, item["index"]))
            if task:
                task.cancel()
    batches[batch_id] = batch
    _save(batch)
    _wakeup.set()
    return batch


def retry_batch(batch_id: str) -> tuple[dict | None, int]:
    batch = get_batch(batch_id)
    if not batch:
        return None, 0
    retried = 0
    if batch.get("storage_cleaned_at"):
        return batch, 0
    for item in batch["items"]:
        if item["state"] == "error" or item.get("interrupted"):
            item.update(state="queued", error=None, studio=None, studio_task_id=None,
                        interrupted=False, tries=0)
            retried += 1
    if retried:
        batch["cancelled"] = False
        batch["finished_at"] = None
        batches[batch_id] = batch
        _save(batch)
        _wakeup.set()
    return batch, retried


def settings() -> dict:
    try:
        saved = json.loads(SETTINGS_FILE.read_text())
    except (OSError, ValueError, TypeError):
        saved = {}
    value = saved.get("retention_days", 30)
    value = value if value in RETENTION_CHOICES else 30
    try:
        policy_version = int(saved.get("policy_version", 1))
    except (TypeError, ValueError):
        policy_version = 1
    if policy_version < 2 and value == 3:
        value = 30
        set_retention(value)
    return {"retention_days": value}


def set_retention(days: int) -> dict:
    if days not in RETENTION_CHOICES:
        raise HTTPException(400, f"retention_days must be one of {sorted(RETENTION_CHOICES)}")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(
        json.dumps({"retention_days": days, "policy_version": 2}),
        encoding="utf-8",
    )
    return {"retention_days": days}


def cleanup(batch_id: str | None = None, expired_only: bool = True) -> dict:
    now = time.time()
    cutoff = now - settings()["retention_days"] * 86400
    candidates = [get_batch(batch_id)] if batch_id else _load_rows()
    cleaned = 0
    reclaimed = 0
    for batch in candidates:
        if not batch or batch.get("storage_cleaned_at"):
            continue
        if any(item["state"] not in TERMINAL_STATES for item in batch["items"]):
            continue
        if expired_only and (batch.get("finished_at") or batch["updated_at"]) > cutoff:
            continue
        batch_dir = ROOT / batch["id"]
        if batch_dir.exists():
            reclaimed += sum(p.stat().st_size for p in batch_dir.rglob("*") if p.is_file())
            shutil.rmtree(batch_dir)
        batch["storage_cleaned_at"] = now
        for item in batch["items"]:
            item["input_path"] = None
            item["artifact_path"] = None
        batches.pop(batch["id"], None)
        _save(batch)
        cleaned += 1
    return {"cleaned": cleaned, "reclaimed_bytes": reclaimed}


def _remove_terminal_batch(batch: dict) -> int:
    """Delete one terminal batch and its Hub-local input/output directory."""
    batch_dir = ROOT / batch["id"]
    reclaimed = 0
    if batch_dir.exists():
        try:
            reclaimed = sum(p.stat().st_size for p in batch_dir.rglob("*") if p.is_file())
        except OSError:
            reclaimed = 0
        shutil.rmtree(batch_dir, ignore_errors=True)
    batches.pop(batch["id"], None)
    for item in batch.get("items", []):
        _item_tasks.pop((batch["id"], item.get("index")), None)
    with _conn() as conn:
        conn.execute("DELETE FROM transcription_batches WHERE id = ?", (batch["id"],))
    return reclaimed


def remove_batch(batch_id: str) -> dict | None:
    """Permanently clear one completed/cancelled batch and its local files.

    Active work is intentionally not clearable: cancel it first, then clear
    it once every chapter has reached a terminal state.
    """
    batch = get_batch(batch_id)
    if not batch or any(item["state"] not in TERMINAL_STATES for item in batch["items"]):
        return None
    return {"removed": batch_id, "reclaimed_bytes": _remove_terminal_batch(batch)}


def clear_terminal() -> dict:
    """Permanently clear all terminal transcription history and local files."""
    cleared = reclaimed = 0
    for batch in _load_rows():
        if any(item["state"] not in TERMINAL_STATES for item in batch["items"]):
            continue
        reclaimed += _remove_terminal_batch(batch)
        cleared += 1
    return {"cleared": cleared, "reclaimed_bytes": reclaimed}


async def _cleanup_loop() -> None:
    while True:
        # Fleet policy controls both age and capacity cleanup. Importing here
        # avoids a module cycle during app startup.
        from . import job_storage
        if job_storage.status()["enabled"]:
            job_storage.enforce_budget()
        await asyncio.sleep(3600)


def statistics() -> dict:
    rows = _load_rows()
    items = [item for batch in rows for item in batch["items"]]
    return {
        "batches": len(rows), "items": len(items),
        "done": sum(i["state"] == "done" for i in items),
        "error": sum(i["state"] == "error" for i in items),
        "duration_seconds": round(sum(i.get("duration_seconds") or 0 for i in items), 2),
    }


def reset_for_tests() -> None:
    global _dispatcher_task, _cleanup_task, _shutting_down
    for task in [t for t in (_dispatcher_task, _cleanup_task, *_item_tasks.values()) if t and not t.done()]:
        task.cancel()
    batches.clear()
    busy_studios.clear()
    _item_tasks.clear()
    _dispatcher_task = _cleanup_task = None
    _shutting_down = False
    broker._external_machine_leases.clear()
