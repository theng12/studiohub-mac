"""Safe, optional cap for files owned by Hub job queues.

Only Hub-local transcription spools are eligible today.  Image and voice
generation artifacts live on the worker Macs and are deliberately never
deleted from the coordinator.  This module therefore cannot accidentally
remove a worker's output or a shared voice reference.
"""

import json
from pathlib import Path

from fastapi import HTTPException

from .registry import DATA_DIR

SETTINGS_FILE = DATA_DIR / "job_storage_settings.json"
DEFAULT_MAX_BYTES = 5 * 1024 ** 3
MIN_MAX_BYTES = 1 * 1024 ** 3
MAX_MAX_BYTES = 100 * 1024 ** 3


def _read() -> dict:
    try:
        saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        saved = {}
    maximum = saved.get("max_bytes", DEFAULT_MAX_BYTES)
    if not isinstance(maximum, int) or not MIN_MAX_BYTES <= maximum <= MAX_MAX_BYTES:
        maximum = DEFAULT_MAX_BYTES
    return {"enabled": bool(saved.get("enabled", False)), "max_bytes": maximum}


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
    return {**value, "used_bytes": used,
            "over_limit": value["enabled"] and used > value["max_bytes"],
            "scope": "Hub-local transcription input and subtitle files"}


def save(enabled: object, max_gb: object) -> dict:
    if not isinstance(enabled, bool):
        raise HTTPException(400, "enabled must be true or false")
    try:
        max_bytes = round(float(max_gb) * 1024 ** 3)
    except (TypeError, ValueError):
        raise HTTPException(400, "max_gb must be a number")
    if not MIN_MAX_BYTES <= max_bytes <= MAX_MAX_BYTES:
        raise HTTPException(400, "max_gb must be between 1 and 100")
    value = {"enabled": enabled, "max_bytes": max_bytes}
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(value), encoding="utf-8")
    return status()


def enforce_budget() -> dict:
    """Drop oldest terminal transcription batches until under the enabled cap."""
    from . import transcription_jobs

    value = _read()
    before = storage_bytes()
    result = {"enabled": value["enabled"], "max_bytes": value["max_bytes"],
              "used_before_bytes": before, "used_bytes": before,
              "cleared": 0, "reclaimed_bytes": 0}
    if not value["enabled"] or before <= value["max_bytes"]:
        return result
    candidates = sorted(
        transcription_jobs.list_batches(), key=lambda batch: batch.get("finished_at")
        or batch.get("updated_at") or batch.get("created_at") or 0)
    for batch in candidates:
        if storage_bytes() <= value["max_bytes"]:
            break
        removed = transcription_jobs.remove_batch(batch["id"])
        if removed:
            result["cleared"] += 1
            result["reclaimed_bytes"] += removed["reclaimed_bytes"]
    result["used_bytes"] = storage_bytes()
    return result
