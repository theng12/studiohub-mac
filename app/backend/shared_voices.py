"""Hub-owned reference voices synchronized to every Voice Studio Mac."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .peers import studio_request
from .registry import DATA_DIR

ROOT = DATA_DIR / "shared_voices"
METADATA = "metadata.json"
TRANSCRIPT = "transcript.txt"
SYNC_STATE = "sync.json"
REFERENCE = "reference"
MAX_BYTES = 25_000_000
MAX_TRANSCRIPT_CHARS = 200_000
ALLOWED_EXTENSIONS = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".opus", ".aac"}
ALLOWED_LICENSES = {"self-owned", "public-domain", "permission", "unknown"}
ALLOWED_GENDERS = {"m", "f", "n"}
TRANSIENT_STATUS = {408, 425, 429, 500, 502, 503, 504}

_tasks: dict[str, asyncio.Task] = {}
_reconciler_task: asyncio.Task | None = None
_sync_lock = asyncio.Lock()


class SharedVoiceConflict(ValueError):
    pass


def _voice_id(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{12}", value or ""):
        raise ValueError("voice ID must be exactly 12 lowercase hex characters")
    return value


def _voice_dir(voice_id: str) -> Path:
    return ROOT / _voice_id(voice_id)


def _atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _read_json(path: Path, default: dict | None = None) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else (default or {})
    except (OSError, json.JSONDecodeError):
        return default or {}


def _validate(
    *, audio_bytes: bytes | None, filename: str | None, name: str,
    language: str, gender: str, license: str, source_url: str | None,
    transcript: str | None, permission_acknowledged: bool,
) -> str | None:
    if not name or not name.strip() or len(name.strip()) > 200:
        raise ValueError("name is required and must be at most 200 characters")
    if not language or not language.strip() or len(language.strip()) > 40:
        raise ValueError("language is required and must be at most 40 characters")
    if gender not in ALLOWED_GENDERS:
        raise ValueError(f"gender must be one of {sorted(ALLOWED_GENDERS)}")
    if license not in ALLOWED_LICENSES:
        raise ValueError(f"license must be one of {sorted(ALLOWED_LICENSES)}")
    if not permission_acknowledged:
        raise ValueError("confirm that you have permission to clone and distribute this voice")
    if len(transcript or "") > MAX_TRANSCRIPT_CHARS:
        raise ValueError("transcript is too long")
    if source_url:
        parsed = urlparse(source_url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("source URL must be a complete http(s) URL")
    if audio_bytes is None:
        return None
    if not audio_bytes:
        raise ValueError("audio file is empty")
    if len(audio_bytes) > MAX_BYTES:
        raise ValueError("audio exceeds the 25 MB shared-voice limit")
    if not filename or Path(filename).name != filename:
        raise ValueError("invalid audio filename")
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError(f"unsupported audio type {ext or '(none)'}")
    return ext


def _load(voice_id: str) -> dict | None:
    directory = _voice_dir(voice_id)
    if directory.is_symlink() or not directory.is_dir():
        return None
    metadata = _read_json(directory / METADATA)
    if metadata.get("id") != voice_id:
        return None
    metadata["transcript"] = (
        (directory / TRANSCRIPT).read_text(encoding="utf-8")
        if (directory / TRANSCRIPT).is_file() and not (directory / TRANSCRIPT).is_symlink()
        else ""
    )
    return metadata


def audio_path(voice_id: str) -> Path | None:
    voice = _load(voice_id)
    if not voice:
        return None
    path = _voice_dir(voice_id) / f"{REFERENCE}{voice['audio_extension']}"
    return path if path.is_file() and not path.is_symlink() else None


def create(
    *, audio_bytes: bytes, filename: str, name: str, language: str,
    gender: str, license: str, notes: str = "", source_url: str | None = None,
    transcript: str | None = None, permission_acknowledged: bool = False,
) -> dict:
    ext = _validate(
        audio_bytes=audio_bytes, filename=filename, name=name, language=language,
        gender=gender, license=license, source_url=source_url,
        transcript=transcript, permission_acknowledged=permission_acknowledged,
    )
    ROOT.mkdir(parents=True, exist_ok=True)
    if ROOT.is_symlink():
        raise ValueError("shared voice root may not be a symbolic link")
    voice_id = uuid.uuid4().hex[:12]
    while (ROOT / voice_id).exists():
        voice_id = uuid.uuid4().hex[:12]
    target = ROOT / voice_id
    temporary = ROOT / f".{voice_id}.{uuid.uuid4().hex}.tmp"
    temporary.mkdir(mode=0o700, exist_ok=False)
    now = time.time()
    digest = hashlib.sha256(audio_bytes).hexdigest()
    metadata = {
        "id": voice_id, "name": name.strip(), "language": language.strip(),
        "gender": gender, "license": license, "notes": (notes or "").strip(),
        "source_url": (source_url or "").strip() or None,
        "permission_acknowledged": True, "audio_extension": ext,
        "audio_sha256": digest, "created_at": now, "updated_at": now,
        "has_transcript": bool((transcript or "").strip()),
    }
    try:
        (temporary / f"{REFERENCE}{ext}").write_bytes(audio_bytes)
        if (transcript or "").strip():
            (temporary / TRANSCRIPT).write_text(transcript.strip(), encoding="utf-8")
        _atomic_json(temporary / METADATA, metadata)
        _atomic_json(temporary / SYNC_STATE, {"targets": {}, "updated_at": now})
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
    return _load(voice_id) or metadata


def update(voice_id: str, changes: dict) -> dict:
    voice = _load(voice_id)
    if not voice:
        raise KeyError(voice_id)
    allowed = {"name", "language", "gender", "license", "notes", "source_url", "transcript"}
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError(f"unsupported fields: {', '.join(sorted(unknown))}")
    merged = {**voice, **changes}
    _validate(
        audio_bytes=None, filename=None, name=str(merged.get("name") or ""),
        language=str(merged.get("language") or ""), gender=str(merged.get("gender") or ""),
        license=str(merged.get("license") or ""), source_url=merged.get("source_url"),
        transcript=merged.get("transcript"), permission_acknowledged=True,
    )
    directory = _voice_dir(voice_id)
    transcript = str(merged.pop("transcript", "") or "").strip()
    transcript_path = directory / TRANSCRIPT
    if transcript_path.is_symlink():
        raise SharedVoiceConflict("transcript path is unsafe")
    if transcript:
        transcript_path.write_text(transcript, encoding="utf-8")
    elif transcript_path.exists():
        transcript_path.unlink()
    merged["has_transcript"] = bool(transcript)
    merged["updated_at"] = time.time()
    _atomic_json(directory / METADATA, merged)
    return _load(voice_id) or merged


def _targets(monitor) -> list[dict]:
    by_machine: dict[str, dict] = {}
    for studio in monitor.registry:
        if studio.get("modality") != "voice":
            continue
        machine = studio.get("machine", "local")
        by_machine.setdefault(machine, studio)
    return list(by_machine.values())


def _state(voice_id: str) -> dict:
    return _read_json(_voice_dir(voice_id) / SYNC_STATE, {"targets": {}})


def _save_state(voice_id: str, state: dict) -> None:
    state["updated_at"] = time.time()
    _atomic_json(_voice_dir(voice_id) / SYNC_STATE, state)


def serialize(voice: dict, monitor=None) -> dict:
    result = dict(voice)
    result["audio_url"] = f"/api/hub/shared-voices/{voice['id']}/audio"
    state = _state(voice["id"])
    saved = state.get("targets") or {}
    targets = []
    if monitor is not None:
        for studio in _targets(monitor):
            row = dict(saved.get(studio["id"]) or {})
            row.update({
                "studio_id": studio["id"],
                "machine": studio.get("machine", "local"),
                "reachable": monitor.status.get(studio["id"], {}).get("status") == "up",
            })
            row.setdefault("status", "pending")
            row.setdefault("message", "Waiting to synchronize")
            targets.append(row)
    else:
        targets = list(saved.values())
    counts: dict[str, int] = {}
    for target in targets:
        status = target.get("status", "pending")
        counts[status] = counts.get(status, 0) + 1
    result["targets"] = targets
    result["sync"] = {
        "total": len(targets), "synced": counts.get("synced", 0),
        "pending": counts.get("pending", 0), "conflict": counts.get("conflict", 0),
        "unsupported": counts.get("unsupported", 0), "failed": counts.get("failed", 0),
        "syncing": voice["id"] in _tasks and not _tasks[voice["id"]].done(),
        "updated_at": state.get("updated_at"),
    }
    return result


def list_voices(monitor=None) -> list[dict]:
    ROOT.mkdir(parents=True, exist_ok=True)
    rows = []
    for child in ROOT.iterdir():
        if child.name.startswith(".") or not child.is_dir() or child.is_symlink():
            continue
        try:
            voice = _load(child.name)
        except ValueError:
            voice = None
        if voice:
            rows.append(serialize(voice, monitor))
    rows.sort(key=lambda row: row.get("created_at", 0), reverse=True)
    return rows


def synced_studio_ids(voice_id: str) -> set[str] | None:
    """Return workers that hold a Hub voice, or None for a non-Hub voice id."""
    try:
        voice = _load(voice_id)
    except ValueError:
        return None
    if not voice:
        return None
    targets = (_state(voice_id).get("targets") or {}).values()
    return {
        str(target.get("studio_id"))
        for target in targets
        if target.get("status") == "synced" and target.get("studio_id")
    }


async def _sync_target(monitor, voice: dict, studio: dict) -> dict:
    now = time.time()
    base = {
        "studio_id": studio["id"], "machine": studio.get("machine", "local"),
        "attempted_at": now,
    }
    if monitor.status.get(studio["id"], {}).get("status") != "up":
        return {**base, "status": "pending", "message": "Voice Studio is offline; automatic retry is scheduled"}
    path = audio_path(voice["id"])
    if not path:
        return {**base, "status": "failed", "message": "Canonical reference audio is missing"}
    url, headers = studio_request(studio, f"/api/voices/{voice['id']}/fleet-sync")
    data = {
        "audio_sha256": voice["audio_sha256"], "name": voice["name"],
        "language": voice["language"], "gender": voice["gender"],
        "license": voice["license"], "notes": voice.get("notes") or "",
        "source_url": voice.get("source_url") or "",
        "transcript": voice.get("transcript") or "",
        "permission_acknowledged": "true",
    }
    try:
        with path.open("rb") as handle:
            response = await monitor._client.put(
                url, headers=headers, data=data,
                files={"audio": (f"reference{voice['audio_extension']}", handle, "application/octet-stream")},
                timeout=45.0,
            )
        if response.status_code == 404:
            return {**base, "status": "unsupported", "message": "Update Voice Studio to v1.19.0 or later"}
        if response.status_code == 409:
            detail = response.json().get("detail", "stable ID conflict")
            return {**base, "status": "conflict", "message": str(detail)}
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail")
            except Exception:
                detail = response.text
            status = "pending" if response.status_code in TRANSIENT_STATUS else "failed"
            return {**base, "status": status, "message": f"HTTP {response.status_code}: {detail or 'sync failed'}"}
        payload = response.json()
        remote = payload.get("voice") or {}
        sync = payload.get("sync") or {}
        if remote.get("id") != voice["id"] or sync.get("sha256") != voice["audio_sha256"]:
            return {**base, "status": "failed", "message": "Voice Studio returned an unverifiable ID or audio hash"}
        return {
            **base, "status": "synced", "message": "Synchronized",
            "completed_at": time.time(), "remote_action": sync.get("status"),
        }
    except (httpx.HTTPError, OSError) as exc:
        return {
            **base, "status": "pending",
            "message": f"Connection dropped; automatic retry is scheduled ({type(exc).__name__})",
        }
    except Exception as exc:
        return {**base, "status": "failed", "message": f"{type(exc).__name__}: {exc}"}


async def sync_voice(monitor, voice_id: str, target_ids: set[str] | None = None) -> dict:
    async with _sync_lock:
        voice = _load(voice_id)
        if not voice:
            raise KeyError(voice_id)
        state = _state(voice_id)
        state.setdefault("targets", {})
        targets = [s for s in _targets(monitor) if target_ids is None or s["id"] in target_ids]
        for studio in targets:
            state["targets"][studio["id"]] = {
                "studio_id": studio["id"], "machine": studio.get("machine", "local"),
                "status": "syncing", "message": "Synchronizing", "attempted_at": time.time(),
            }
        _save_state(voice_id, state)

        async def run_target(studio: dict) -> tuple[str, dict]:
            return studio["id"], await _sync_target(monitor, voice, studio)

        pending = [asyncio.create_task(run_target(studio)) for studio in targets]
        try:
            for completed in asyncio.as_completed(pending):
                studio_id, result = await completed
                state["targets"][studio_id] = result
                _save_state(voice_id, state)
        finally:
            unfinished = [task for task in pending if not task.done()]
            for task in unfinished:
                task.cancel()
            if unfinished:
                await asyncio.gather(*unfinished, return_exceptions=True)
        return serialize(_load(voice_id) or voice, monitor)


def start_sync(monitor, voice_id: str) -> bool:
    existing = _tasks.get(voice_id)
    if existing and not existing.done():
        return False
    task = asyncio.create_task(sync_voice(monitor, voice_id))
    _tasks[voice_id] = task
    task.add_done_callback(lambda finished, key=voice_id: _tasks.pop(key, None))
    return True


async def reconcile_once(monitor) -> None:
    now = time.time()
    for voice in list_voices(monitor):
        if voice["id"] in _tasks:
            continue
        retry = {
            target["studio_id"] for target in voice.get("targets", [])
            if (
                target.get("status") in {"pending", "syncing"}
                or (
                    target.get("status") in {"unsupported", "failed"}
                    and now - float(target.get("attempted_at") or 0) >= 300
                )
            )
        }
        if retry:
            await sync_voice(monitor, voice["id"], retry)


async def _reconcile_loop(monitor) -> None:
    while True:
        try:
            await asyncio.sleep(30)
            await reconcile_once(monitor)
        except asyncio.CancelledError:
            return
        except Exception:
            await asyncio.sleep(30)


def start_reconciler(monitor) -> None:
    global _reconciler_task
    if _reconciler_task is None or _reconciler_task.done():
        _reconciler_task = asyncio.create_task(_reconcile_loop(monitor))


async def stop() -> None:
    global _reconciler_task
    tasks = [task for task in [*_tasks.values(), _reconciler_task] if task and not task.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _tasks.clear()
    _reconciler_task = None


def srt_to_text(srt: str) -> str:
    lines = []
    for raw in (srt or "").replace("\r", "").split("\n"):
        line = raw.strip()
        if not line or line.isdigit() or "-->" in line:
            continue
        if not lines or lines[-1] != line:
            lines.append(line)
    return " ".join(lines).strip()
