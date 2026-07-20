"""Persisted, operator-visible RAM admission policy for local model dispatch.

Studio catalogs provide conservative technical defaults. Studio Hub may carry
fleet-qualified defaults based on measured production use, and the owner may
override either floor from the Models UI. The effective policy is site-local;
it never changes a worker catalog or GenStudio's global ownership decisions.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from . import workload_policy
from .registry import DATA_DIR


SETTINGS_FILE = DATA_DIR / "memory_admission_overrides.json"
DEFAULT_MIN_FREE_MEMORY_GB = 2.0
SUPPORTED_MODALITIES = frozenset({"image", "voice", "music", "video"})

# These are measured fleet defaults, intentionally allowed to be less
# conservative than a worker catalog. FLUX.2 Klein 4B MLX 4-bit has completed
# thousands of generations on the owner's 8 GB Apple-silicon nodes. Keep the
# free-memory floor as the live safety guard instead of excluding that class.
_FLEET_DEFAULTS = {
    "aitrader/flux2-klein-4b-mlx-4bit": {
        "min_total_memory_gb": 8.0,
        "min_free_memory_gb": 2.0,
        "reason": "Fleet-qualified on 8 GB Apple-silicon Macs",
    },
}

_cache: dict[str, dict] | None = None


def _normalized(model: str) -> str:
    return str(model or "").strip().lower()


def applies_to(modality: str | None, *, is_cloud: bool = False) -> bool:
    """Whether this queue uses the broker's local inference RAM governor."""
    return not is_cloud and str(modality or "") in SUPPORTED_MODALITIES


def _number(value) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _read() -> dict[str, dict]:
    global _cache
    if _cache is not None:
        return _cache
    try:
        raw = json.loads(SETTINGS_FILE.read_text()) if SETTINGS_FILE.exists() else {}
    except (OSError, json.JSONDecodeError):
        raw = {}
    _cache = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                continue
            total = _number(value.get("min_total_memory_gb"))
            free = _number(value.get("min_free_memory_gb"))
            if total is None or free is None:
                continue
            _cache[_normalized(key)] = {
                "model": str(value.get("model") or key),
                "min_total_memory_gb": total,
                "min_free_memory_gb": free,
                "updated_at": _number(value.get("updated_at")) or 0.0,
            }
    return _cache


def _write(values: dict[str, dict]) -> None:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{SETTINGS_FILE.name}-", dir=SETTINGS_FILE.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w") as handle:
            json.dump(values, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        temporary.replace(SETTINGS_FILE)
    finally:
        if temporary.exists():
            temporary.unlink()


def describe(model: str, catalog_entry: dict) -> dict:
    """Return catalog, fleet-default, and effective site-local RAM floors."""
    key = _normalized(model or catalog_entry.get("repo"))
    catalog_total = _number(catalog_entry.get("min_unified_memory_gb"))
    catalog_free = _number(catalog_entry.get("min_free_memory_gb"))
    qualified_total = workload_policy.required_total_memory_gb(model, catalog_entry)
    qualified_free = workload_policy.required_free_memory_gb(model, catalog_entry)
    fleet_default = _FLEET_DEFAULTS.get(key)
    workload_default = bool(
        (qualified_total is not None and qualified_total != catalog_total)
        or (qualified_free is not None and qualified_free != catalog_free)
    )

    default_total = (
        fleet_default["min_total_memory_gb"] if fleet_default
        else qualified_total
    )
    default_free = (
        fleet_default["min_free_memory_gb"] if fleet_default
        else max(DEFAULT_MIN_FREE_MEMORY_GB, qualified_free or 0.0)
    )
    override = _read().get(key)
    effective_total = (
        override["min_total_memory_gb"] if override else default_total
    )
    effective_free = (
        override["min_free_memory_gb"] if override else default_free
    )
    source = "operator_override" if override else (
        "fleet_default" if fleet_default or workload_default else "catalog"
    )
    return {
        "model": str(model or catalog_entry.get("repo") or ""),
        "catalog_min_total_memory_gb": catalog_total,
        "catalog_min_free_memory_gb": catalog_free,
        "default_min_total_memory_gb": default_total,
        "default_min_free_memory_gb": default_free,
        "effective_min_total_memory_gb": effective_total,
        "effective_min_free_memory_gb": effective_free,
        "source": source,
        "default_reason": (
            fleet_default.get("reason") if fleet_default
            else "Hub production qualification" if workload_default
            else "Studio catalog requirement"
        ),
        "overridden": override is not None,
        "updated_at": override.get("updated_at") if override else None,
    }


def set_override(model: str, *, min_total_memory_gb: float,
                 min_free_memory_gb: float, catalog_entry: dict) -> dict:
    key = _normalized(model)
    if not key:
        raise ValueError("model is required")
    values = dict(_read())
    values[key] = {
        "model": model,
        "min_total_memory_gb": float(min_total_memory_gb),
        "min_free_memory_gb": float(min_free_memory_gb),
        "updated_at": time.time(),
    }
    _write(values)
    global _cache
    _cache = values
    return describe(model, catalog_entry)


def reset_override(model: str, catalog_entry: dict) -> dict:
    key = _normalized(model)
    values = dict(_read())
    values.pop(key, None)
    if values:
        _write(values)
    else:
        try:
            SETTINGS_FILE.unlink()
        except FileNotFoundError:
            pass
    global _cache
    _cache = values
    return describe(model, catalog_entry)


def reset_for_tests() -> None:
    global _cache
    _cache = None
