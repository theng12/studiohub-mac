"""Short-lived private input assets for site-local model execution.

GenStudio remains the durable customer-asset authority. Studio Hub stores only
an authenticated, checksum-bound execution copy long enough to route and retry
one assigned site attempt. Model-specific preparation remains worker-owned.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from .registry import DATA_DIR


ROOT = DATA_DIR / "execution_assets" / "voice_references"
MAX_BYTES = 25_000_000
DEFAULT_TTL_SECONDS = 24 * 60 * 60
MAX_TTL_SECONDS = 48 * 60 * 60
ALLOWED_EXTENSIONS = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".opus", ".aac", ".webm"}
MIME_BY_EXTENSION = {
    ".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
    ".flac": "audio/flac", ".ogg": "audio/ogg", ".opus": "audio/ogg",
    ".aac": "audio/aac", ".webm": "audio/webm",
}
_ASSET_ID = re.compile(r"^[0-9a-f]{24}$")


class ExecutionAssetError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _dir(asset_id: str) -> Path:
    if not _ASSET_ID.fullmatch(asset_id or ""):
        raise ExecutionAssetError(
            "VOICE_REFERENCE_ASSET_MISSING", "The private voice reference is unavailable."
        )
    return ROOT / asset_id


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _segments(value: object) -> list[dict[str, Any]]:
    if value in (None, ""):
        return []
    if not isinstance(value, list) or len(value) > 10_000:
        raise ExecutionAssetError(
            "REFERENCE_TRANSCRIPT_SEGMENTS_INVALID",
            "Reference transcript segments must be a bounded ordered list.",
        )
    result = []
    for raw in value:
        if not isinstance(raw, dict):
            raise ExecutionAssetError(
                "REFERENCE_TRANSCRIPT_SEGMENTS_INVALID", "A transcript segment is invalid."
            )
        try:
            start = float(raw.get("start_seconds", raw.get("start")))
            end = float(raw.get("end_seconds", raw.get("end")))
        except (TypeError, ValueError) as exc:
            raise ExecutionAssetError(
                "REFERENCE_TRANSCRIPT_SEGMENTS_INVALID", "A transcript timestamp is invalid."
            ) from exc
        text = str(raw.get("text") or "").strip()
        result.append({"start": start, "end": end, "text": text[:10_000]})
    return result


def stage_voice_reference(
    *, audio_bytes: bytes, filename: str, source_asset_id: str,
    declared_sha256: str, transcript: str = "", transcript_segments: object = None,
    language: str = "", ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> dict[str, Any]:
    if not source_asset_id.strip() or len(source_asset_id) > 500:
        raise ExecutionAssetError(
            "SOURCE_ASSET_ID_REQUIRED", "GenStudio source_asset_id is required."
        )
    if not audio_bytes:
        raise ExecutionAssetError("REFERENCE_AUDIO_EMPTY", "The private voice reference is empty.")
    if len(audio_bytes) > MAX_BYTES:
        raise ExecutionAssetError("REFERENCE_AUDIO_TOO_LARGE", "The private voice reference exceeds 25 MB.")
    if Path(filename).name != filename:
        raise ExecutionAssetError("REFERENCE_AUDIO_TYPE_UNSUPPORTED", "The reference filename is invalid.")
    extension = Path(filename).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise ExecutionAssetError("REFERENCE_AUDIO_TYPE_UNSUPPORTED", "The reference audio type is unsupported.")
    digest = hashlib.sha256(audio_bytes).hexdigest()
    if not re.fullmatch(r"[0-9a-fA-F]{64}", declared_sha256 or ""):
        raise ExecutionAssetError(
            "REFERENCE_AUDIO_CHECKSUM_REQUIRED", "A full source SHA-256 is required."
        )
    if not hashlib.sha256(audio_bytes).hexdigest() == declared_sha256.lower():
        raise ExecutionAssetError(
            "REFERENCE_AUDIO_CHECKSUM_MISMATCH",
            "The uploaded reference does not match GenStudio's checksum.",
        )
    segments = _segments(transcript_segments)
    transcript = transcript.strip()
    if len(transcript) > 200_000:
        raise ExecutionAssetError("REFERENCE_TRANSCRIPT_TOO_LARGE", "The reference transcript is too long.")
    ttl = max(300, min(int(ttl_seconds), MAX_TTL_SECONDS))
    source_fingerprint = hashlib.sha256(source_asset_id.strip().encode()).hexdigest()
    identity = hashlib.sha256(
        json.dumps({
            "source": source_fingerprint,
            "sha256": digest,
            "transcript": transcript,
            "segments": segments,
            "language": language.strip(),
        }, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    asset_id = identity[:24]
    target = ROOT / asset_id
    expires_at = time.time() + ttl
    existing = _read(target / "metadata.json") if target.is_dir() else None
    if existing:
        existing["expires_at"] = max(float(existing.get("expires_at") or 0), expires_at)
        existing["updated_at"] = time.time()
        _atomic_json(target / "metadata.json", existing)
        return existing

    ROOT.mkdir(parents=True, exist_ok=True)
    if ROOT.is_symlink():
        raise ExecutionAssetError(
            "VOICE_REFERENCE_ASSET_INACCESSIBLE", "The execution asset store is unsafe."
        )
    temporary = ROOT / f".{asset_id}.{uuid.uuid4().hex}.tmp"
    temporary.mkdir(mode=0o700)
    metadata = {
        "id": asset_id,
        "kind": "voice_reference",
        "source_asset_fingerprint": source_fingerprint,
        "sha256": digest,
        "bytes": len(audio_bytes),
        "audio_extension": extension,
        "media_type": MIME_BY_EXTENSION[extension],
        "transcript": transcript,
        "transcript_segments": segments,
        "language": language.strip() or None,
        "created_at": time.time(),
        "updated_at": time.time(),
        "expires_at": expires_at,
    }
    try:
        (temporary / f"reference{extension}").write_bytes(audio_bytes)
        _atomic_json(temporary / "metadata.json", metadata)
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
    return metadata


def resolve_voice_reference(asset_id: str, *, now: float | None = None) -> tuple[dict, Path]:
    directory = _dir(asset_id)
    metadata = _read(directory / "metadata.json")
    if not metadata:
        raise ExecutionAssetError(
            "VOICE_REFERENCE_ASSET_MISSING",
            "The private voice reference is missing. Upload it again before generating.",
        )
    if float(metadata.get("expires_at") or 0) <= (time.time() if now is None else now):
        raise ExecutionAssetError(
            "VOICE_REFERENCE_ASSET_EXPIRED",
            "The private voice reference has expired. Upload it again before generating.",
        )
    audio = directory / f"reference{metadata.get('audio_extension') or ''}"
    if audio.is_symlink() or not audio.is_file():
        raise ExecutionAssetError(
            "VOICE_REFERENCE_ASSET_INACCESSIBLE",
            "The private voice reference is no longer readable. Upload it again.",
        )
    if hashlib.sha256(audio.read_bytes()).hexdigest() != metadata.get("sha256"):
        raise ExecutionAssetError(
            "VOICE_REFERENCE_ASSET_INACCESSIBLE",
            "The private voice reference failed checksum verification. Upload it again.",
        )
    return metadata, audio


def public(metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        key: metadata.get(key) for key in (
            "id", "kind", "sha256", "bytes", "media_type", "created_at", "expires_at"
        )
    }


def delete(asset_id: str) -> bool:
    directory = _dir(asset_id)
    if not directory.exists():
        return False
    shutil.rmtree(directory)
    return True


def cleanup_expired(now: float | None = None) -> int:
    current = time.time() if now is None else now
    removed = 0
    if not ROOT.is_dir():
        return 0
    for child in ROOT.iterdir():
        if child.is_symlink() or not child.is_dir() or child.name.startswith("."):
            continue
        metadata = _read(child / "metadata.json") or {}
        if float(metadata.get("expires_at") or 0) <= current:
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
    return removed
