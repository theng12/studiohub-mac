"""Safe cap for disposable files owned by Hub job queues.

Only Hub-local transcription spools are eligible today. Image and voice
generation artifacts live on the worker Macs and shared voice references live
outside this scope. The fleet storage coordinator combines this usage with the
worker Studios' own protected cleanup APIs.
"""

import json
from pathlib import Path

from fastapi import HTTPException

from .registry import DATA_DIR

SETTINGS_FILE = DATA_DIR / "job_storage_settings.json"
DEFAULT_MAX_BYTES = 80 * 1024 ** 3
DEFAULT_RETENTION_DAYS = 3
MIN_MAX_BYTES = 1 * 1024 ** 3
MAX_MAX_BYTES = 1000 * 1024 ** 3


def _read() -> dict:
    try:
        saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        saved = {}
    maximum = saved.get("max_bytes", DEFAULT_MAX_BYTES)
    if not isinstance(maximum, int) or not MIN_MAX_BYTES <= maximum <= MAX_MAX_BYTES:
        maximum = DEFAULT_MAX_BYTES
    retention = saved.get("retention_days", DEFAULT_RETENTION_DAYS)
    if not isinstance(retention, int) or retention not in {1, 3, 7, 15, 30, 90}:
        retention = DEFAULT_RETENTION_DAYS
    return {
        "enabled": bool(saved.get("enabled", True)),
        "max_bytes": maximum,
        "retention_days": retention,
    }


def _bytes_under(root: Path) -> int:
    if not root.exists():
        return 0
    total = 0
    for path in root.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            pass
    return total


def storage_bytes() -> int:
    from . import transcription_jobs
    return _bytes_under(transcription_jobs.ROOT)


def status() -> dict:
    value = _read()
    used = storage_bytes()
    return {**value, "supported": True, "used_bytes": used,
            "over_limit": value["enabled"] and used > value["max_bytes"],
            "scope": "Hub-local transcription input and subtitle files"}


def save(enabled: object, max_gb: object, retention_days: object = None) -> dict:
    if not isinstance(enabled, bool):
        raise HTTPException(400, "enabled must be true or false")
    try:
        max_bytes = round(float(max_gb) * 1024 ** 3)
    except (TypeError, ValueError):
        raise HTTPException(400, "max_gb must be a number")
    if not MIN_MAX_BYTES <= max_bytes <= MAX_MAX_BYTES:
        raise HTTPException(400, "max_gb must be between 1 and 1000")
    if retention_days is None:
        retention_days = _read()["retention_days"]
    if not isinstance(retention_days, int) or retention_days not in {1, 3, 7, 15, 30, 90}:
        raise HTTPException(400, "retention_days must be 1, 3, 7, 15, 30, or 90")
    value = {"enabled": enabled, "max_bytes": max_bytes,
             "retention_days": retention_days}
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    partial = SETTINGS_FILE.with_suffix(".json.tmp")
    partial.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    partial.replace(SETTINGS_FILE)
    # Keep the transcription queue's existing retention endpoint in sync.
    from . import transcription_jobs
    transcription_jobs.set_retention(retention_days)
    return status()


def enforce_budget(target_bytes: int | None = None) -> dict:
    """Clean expired files, then oldest terminal files until under ``target``.

    Batch metadata remains in the Jobs UI; only its local input/output files are
    removed. Active transcription work is never eligible.
    """
    from . import transcription_jobs

    value = _read()
    before = storage_bytes()
    maximum = max(0, int(target_bytes)) if target_bytes is not None else value["max_bytes"]
    result = {"enabled": value["enabled"], "max_bytes": maximum,
              "used_before_bytes": before, "used_bytes": before,
              "cleared": 0, "reclaimed_bytes": 0}
    if not value["enabled"] and target_bytes is None:
        return result
    if target_bytes is None:
        aged = transcription_jobs.cleanup()
        result["cleared"] += aged["cleaned"]
        result["reclaimed_bytes"] += aged["reclaimed_bytes"]
    if storage_bytes() <= maximum:
        result["used_bytes"] = storage_bytes()
        return result
    candidates = sorted(
        transcription_jobs.list_batches(), key=lambda batch: batch.get("finished_at")
        or batch.get("updated_at") or batch.get("created_at") or 0)
    for batch in candidates:
        if storage_bytes() <= maximum:
            break
        cleaned = transcription_jobs.cleanup(batch["id"], expired_only=False)
        if cleaned["cleaned"]:
            result["cleared"] += 1
            result["reclaimed_bytes"] += cleaned["reclaimed_bytes"]
    result["used_bytes"] = storage_bytes()
    result["over_limit"] = result["used_bytes"] > maximum
    return result
