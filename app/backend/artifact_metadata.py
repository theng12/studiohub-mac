"""Small, dependency-free validation for brokered media artifacts.

The Hub deliberately validates bytes rather than trusting a filename or a
worker's MIME header.  WAV is supported with the standard library because it
is the current Voice Studio/Kokoro output and includes all billable metadata.
Other media types retain a valid upstream Content-Type at proxy time.
"""

from __future__ import annotations

import hashlib
import io
import wave


_MEDIA_TYPES = {
    "image": {"image/png", "image/jpeg", "image/webp", "image/gif"},
    "video": {"video/mp4", "video/webm", "video/quicktime"},
    "render": {"video/mp4", "video/webm", "video/quicktime"},
    "voice": {"audio/wav", "audio/mpeg", "audio/flac", "audio/ogg", "audio/mp4"},
    "music": {"audio/wav", "audio/mpeg", "audio/flac", "audio/ogg", "audio/mp4"},
}


def trusted_media_type(value: str | None, modality: str) -> str | None:
    """Return a normalized allowed MIME type, never an arbitrary header."""
    if not isinstance(value, str):
        return None
    media_type = value.split(";", 1)[0].strip().lower()
    return media_type if media_type in _MEDIA_TYPES.get(modality, set()) else None


def media_type_for_proxy(modality: str, cached: str | None,
                         upstream: str | None) -> str:
    """Prefer byte-validated metadata, then a modality-valid upstream header."""
    return (trusted_media_type(cached, modality)
            or trusted_media_type(upstream, modality)
            or "application/octet-stream")


def wav_metadata(data: bytes) -> dict:
    """Decode a PCM WAV and return immutable, billable media facts.

    ``wave`` reads the RIFF/WAVE structure itself, so neither an extension nor
    a claimed Content-Type can make unrelated bytes appear to be audio.
    """
    if not data.startswith(b"RIFF") or data[8:12] != b"WAVE":
        raise ValueError("artifact is not a RIFF/WAVE file")
    try:
        with wave.open(io.BytesIO(data), "rb") as audio:
            if audio.getcomptype() != "NONE":
                raise ValueError("compressed WAV is not supported for billing metadata")
            frames = audio.getnframes()
            sample_rate_hz = audio.getframerate()
            channels = audio.getnchannels()
    except (EOFError, wave.Error) as exc:
        raise ValueError(f"invalid WAV artifact: {exc}") from exc
    if frames <= 0 or sample_rate_hz <= 0 or channels not in (1, 2):
        raise ValueError("WAV has invalid duration, sample rate, or channel count")
    duration_s = frames / sample_rate_hz
    return {
        "media_type": "audio/wav",
        "format": "wav",
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "audio_duration_s": round(duration_s, 6),
        "audio_duration_ms": round(duration_s * 1000),
        "sample_rate_hz": sample_rate_hz,
        "channels": channels,
    }
