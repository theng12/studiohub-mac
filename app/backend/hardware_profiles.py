"""Reusable hardware identities for registered Studio Hub machines.

Profiles describe stable hardware classes only. Purchase prices and commercial
assumptions remain GenStudio concerns, while Studio Hub publishes the selected
profile id with its live resource telemetry.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from .registry import DATA_DIR

CUSTOM_PROFILES_FILE = DATA_DIR / "hardware_profiles.json"
MACHINE_PROFILES_FILE = DATA_DIR / "machine_hardware_profiles.json"

DEFAULT_HARDWARE_PROFILES = (
    {
        "id": "mac-mini-m1-8gb",
        "display_name": "Mac mini M1 · 8 GB",
        "machine_type": "Mac mini",
        "machine_prefix": "macmini-m1-8gb",
        "chip": "M1",
        "memory_gb": 8,
        "planned_units": 5,
    },
    {
        "id": "mac-mini-m1-16gb",
        "display_name": "Mac mini M1 · 16 GB",
        "machine_type": "Mac mini",
        "machine_prefix": "macmini-m1-16gb",
        "chip": "M1",
        "memory_gb": 16,
        "planned_units": 0,
    },
    {
        "id": "mac-mini-m2-8gb",
        "display_name": "Mac mini M2 · 8 GB",
        "machine_type": "Mac mini",
        "machine_prefix": "macmini-m2-8gb",
        "chip": "M2",
        "memory_gb": 8,
        "planned_units": 6,
    },
    {
        "id": "mac-mini-m2-16gb",
        "display_name": "Mac mini M2 · 16 GB",
        "machine_type": "Mac mini",
        "machine_prefix": "macmini-m2-16gb",
        "chip": "M2",
        "memory_gb": 16,
        "planned_units": 2,
    },
    {
        "id": "mac-mini-m4-16gb",
        "display_name": "Mac mini M4 · 16 GB",
        "machine_type": "Mac mini",
        "machine_prefix": "macmini-m4-16gb",
        "chip": "M4",
        "memory_gb": 16,
        "planned_units": 3,
    },
    {
        "id": "mac-mini-m4-24gb",
        "display_name": "Mac mini M4 · 24 GB",
        "machine_type": "Mac mini",
        "machine_prefix": "macmini-m4-24gb",
        "chip": "M4",
        "memory_gb": 24,
        "planned_units": 1,
    },
    {
        "id": "macbook-m4-16gb",
        "display_name": "MacBook M4 · 16 GB",
        "machine_type": "MacBook",
        "machine_prefix": "macbook-m4-16gb",
        "chip": "M4",
        "memory_gb": 16,
        "planned_units": 1,
    },
    {
        "id": "imac-m1-8gb",
        "display_name": "iMac M1 · 8 GB",
        "machine_type": "iMac",
        "machine_prefix": "imac-m1-8gb",
        "chip": "M1",
        "memory_gb": 8,
        "planned_units": 4,
    },
    {
        "id": "imac-m3-8gb",
        "display_name": "iMac M3 · 8 GB",
        "machine_type": "iMac",
        "machine_prefix": "imac-m3-8gb",
        "chip": "M3",
        "memory_gb": 8,
        "planned_units": 2,
    },
)

_PROFILE_ID = re.compile(r"[a-z0-9][a-z0-9-]{2,63}")
_MACHINE_PREFIX = re.compile(r"[a-z0-9][a-z0-9-]{2,79}")
_custom_cache: list[dict] | None = None
_assignment_cache: dict[str, str] | None = None


def _read_json(path: Path, fallback):
    try:
        return json.loads(path.read_text()) if path.exists() else fallback
    except (json.JSONDecodeError, OSError):
        return fallback


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w") as handle:
            json.dump(value, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _validate_profile(value: dict, *, custom: bool) -> dict:
    profile_id = str(value.get("id") or "").strip().lower()
    display_name = str(value.get("display_name") or "").strip()
    machine_type = str(value.get("machine_type") or "").strip()
    machine_prefix = str(value.get("machine_prefix") or profile_id).strip().lower()
    chip = str(value.get("chip") or "").strip().upper()
    try:
        memory_gb = int(value.get("memory_gb"))
        planned_units = int(value.get("planned_units", 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("memory and planned units must be whole numbers") from exc
    if not _PROFILE_ID.fullmatch(profile_id):
        raise ValueError("profile id must use lowercase letters, numbers, and dashes")
    if not display_name or len(display_name) > 100:
        raise ValueError("display name is required and must be at most 100 characters")
    if not machine_type or len(machine_type) > 50:
        raise ValueError("machine type is required and must be at most 50 characters")
    if not _MACHINE_PREFIX.fullmatch(machine_prefix):
        raise ValueError("machine id prefix must use lowercase letters, numbers, and dashes")
    if not re.fullmatch(r"M[1-9][A-Z0-9-]{0,15}", chip):
        raise ValueError("chip must be an Apple chip name such as M1, M2, M3, or M4")
    if not 4 <= memory_gb <= 512:
        raise ValueError("memory must be between 4 and 512 GB")
    if not 0 <= planned_units <= 10_000:
        raise ValueError("planned units must be between 0 and 10,000")
    return {
        "id": profile_id,
        "display_name": display_name,
        "machine_type": machine_type,
        "machine_prefix": machine_prefix,
        "chip": chip,
        "memory_gb": memory_gb,
        "planned_units": planned_units,
        "custom": custom,
    }


def load_hardware_profiles() -> list[dict]:
    global _custom_cache
    if _custom_cache is None:
        values = _read_json(CUSTOM_PROFILES_FILE, [])
        _custom_cache = []
        if isinstance(values, list):
            for value in values:
                try:
                    if isinstance(value, dict):
                        _custom_cache.append(_validate_profile(value, custom=True))
                except ValueError:
                    continue
    profiles = [_validate_profile(value, custom=False) for value in DEFAULT_HARDWARE_PROFILES]
    default_ids = {profile["id"] for profile in profiles}
    profiles.extend(profile for profile in _custom_cache if profile["id"] not in default_ids)
    return profiles


def hardware_profile(profile_id: str | None) -> dict | None:
    if not profile_id:
        return None
    return next(
        (profile for profile in load_hardware_profiles() if profile["id"] == profile_id),
        None,
    )


def add_custom_hardware_profile(value: dict) -> dict:
    global _custom_cache
    profile = _validate_profile(value, custom=True)
    if hardware_profile(profile["id"]) is not None:
        raise ValueError(f"hardware profile {profile['id']!r} already exists")
    custom = list(_custom_cache or [])
    custom.append(profile)
    custom.sort(key=lambda row: (row["machine_type"], row["chip"], row["memory_gb"]))
    _write_json(CUSTOM_PROFILES_FILE, custom)
    _custom_cache = custom
    return dict(profile)


def load_machine_profile_ids() -> dict[str, str]:
    global _assignment_cache
    if _assignment_cache is None:
        values = _read_json(MACHINE_PROFILES_FILE, {})
        _assignment_cache = {
            str(machine): str(profile_id)
            for machine, profile_id in values.items()
            if isinstance(machine, str) and isinstance(profile_id, str)
        } if isinstance(values, dict) else {}
    return dict(_assignment_cache)


def set_machine_hardware_profile(machine: str, profile_id: str | None) -> dict | None:
    global _assignment_cache
    assignments = load_machine_profile_ids()
    if profile_id is None:
        assignments.pop(machine, None)
        profile = None
    else:
        profile = hardware_profile(profile_id)
        if profile is None:
            raise ValueError(f"unknown hardware profile {profile_id!r}")
        assignments[machine] = profile["id"]
    _write_json(MACHINE_PROFILES_FILE, assignments)
    _assignment_cache = assignments
    return dict(profile) if profile else None


def machine_hardware_profile(machine: str) -> dict | None:
    profile = hardware_profile(load_machine_profile_ids().get(machine))
    return dict(profile) if profile else None


def remove_machine_hardware_profile(machine: str) -> None:
    if machine in load_machine_profile_ids():
        set_machine_hardware_profile(machine, None)


def suggested_machine_id(profile_id: str, known_machines: set[str]) -> str:
    profile = hardware_profile(profile_id)
    if profile is None:
        raise ValueError(f"unknown hardware profile {profile_id!r}")
    assignments = load_machine_profile_ids()
    sequence = sum(1 for assigned in assignments.values() if assigned == profile_id) + 1
    while True:
        candidate = f"{profile['machine_prefix']}-{sequence:03d}"
        if not any(
            machine == candidate or machine.startswith(f"{candidate}-")
            for machine in known_machines
        ):
            return candidate
        sequence += 1


def hardware_profile_catalog(known_machines: set[str]) -> dict:
    assignments = load_machine_profile_ids()
    profiles = []
    for profile in load_hardware_profiles():
        assigned = sorted(
            machine for machine, profile_id in assignments.items() if profile_id == profile["id"]
        )
        profiles.append(
            {
                **profile,
                "assigned_units": len(assigned),
                "assigned_machines": assigned,
                "suggested_machine_id": suggested_machine_id(profile["id"], known_machines),
            }
        )
    return {"profiles": profiles, "assignments": assignments}
