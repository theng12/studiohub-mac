"""Studio registry — host-aware from day one (SPEC §6.1).

Defaults cover the five local KH studios. A per-machine `studios.json` at the
launcher root (gitignored) can override any default by `id` or add extra
entries — including studios on OTHER machines over LAN/Tailscale, which is the
foundation for federation and Swarm Batch worker pools.

studios.json format (all fields optional except id for overrides):
[
  { "id": "image", "host": "100.101.102.103", "machine": "studio-2" },
  { "id": "image-b", "modality": "image", "host": "100.101.102.103", "port": 47868,
    "machine": "studio-2", "title": "Image Studio (M2 Ultra)" }
]
"""

import json
from pathlib import Path

# Launcher root = two levels up from this file (app/backend/registry.py)
LAUNCHER_ROOT = Path(__file__).resolve().parents[2]

# Where mutable per-machine STATE lives (studios.json, hub.db, .hub_token,
# .fleet_token, machine_labels.json, hub_state.json, uploads/). Defaults to the
# launcher root — no behavior change — but overridable via STUDIOHUB_DATA_DIR so
# tests (and alternate deployments) can point state elsewhere. Code files
# (VERSION, frontend, the api/ tree for control) always stay under LAUNCHER_ROOT.
import os as _os
DATA_DIR = (Path(_os.environ["STUDIOHUB_DATA_DIR"]).resolve()
            if _os.environ.get("STUDIOHUB_DATA_DIR") else LAUNCHER_ROOT)
DATA_DIR.mkdir(parents=True, exist_ok=True)

REGISTRY_FILE = DATA_DIR / "studios.json"
LABELS_FILE = DATA_DIR / "machine_labels.json"
FLAGS_FILE = DATA_DIR / "machine_flags.json"

# machine-key -> friendly display name. The KEY (e.g. "local", "imac-pdt") is
# the technical identity used for control routing and studio ids; the label is
# purely cosmetic, so renaming never breaks anything.
_labels_cache: dict | None = None

# machine-key -> {"enabled": bool, "studios": {studio-id: {"enabled": bool}}}.
# Disabled machines and Studios stay registered and monitored; only new job
# dispatch is paused. Missing entries default to enabled, which keeps existing
# machine_flags.json files backward compatible.
_flags_cache: dict | None = None


def load_flags() -> dict:
    global _flags_cache
    if _flags_cache is None:
        try:
            _flags_cache = json.loads(FLAGS_FILE.read_text()) if FLAGS_FILE.exists() else {}
        except (json.JSONDecodeError, OSError):
            _flags_cache = {}
    return _flags_cache


def machine_enabled(machine: str) -> bool:
    return bool(load_flags().get(machine, {}).get("enabled", True))


def set_machine_enabled(machine: str, enabled: bool):
    global _flags_cache
    flags = dict(load_flags())
    flags.setdefault(machine, {})["enabled"] = bool(enabled)
    FLAGS_FILE.write_text(json.dumps(flags, indent=2) + "\n")
    _flags_cache = flags


def studio_enabled(machine: str, studio_id: str) -> bool:
    """Whether one registered Studio may receive new scheduled work."""
    machine_flags = load_flags().get(machine, {})
    studio_flags = (
        machine_flags.get("studios", {})
        if isinstance(machine_flags, dict) else {}
    )
    row = studio_flags.get(studio_id, {}) if isinstance(studio_flags, dict) else {}
    return bool(row.get("enabled", True)) if isinstance(row, dict) else bool(row)


def set_studio_enabled(machine: str, studio_id: str, enabled: bool):
    """Persist an app-specific scheduler toggle without changing its process."""
    global _flags_cache
    flags = dict(load_flags())
    saved_machine = flags.get(machine, {})
    machine_flags = dict(saved_machine) if isinstance(saved_machine, dict) else {}
    studios = dict(machine_flags.get("studios", {}))
    studios[studio_id] = {"enabled": bool(enabled)}
    machine_flags["studios"] = studios
    flags[machine] = machine_flags
    FLAGS_FILE.write_text(json.dumps(flags, indent=2) + "\n")
    _flags_cache = flags


def load_labels() -> dict:
    global _labels_cache
    if _labels_cache is None:
        try:
            _labels_cache = json.loads(LABELS_FILE.read_text()) if LABELS_FILE.exists() else {}
        except (json.JSONDecodeError, OSError):
            _labels_cache = {}
    return _labels_cache


def set_label(machine: str, name: str):
    global _labels_cache
    labels = dict(load_labels())
    if name and name.strip():
        labels[machine] = name.strip()
    else:
        labels.pop(machine, None)  # empty name clears the alias
    LABELS_FILE.write_text(json.dumps(labels, indent=2) + "\n")
    _labels_cache = labels


def label_for(machine: str) -> str:
    return load_labels().get(machine, machine)


def prune_machine_metadata(known_machines: set[str]) -> None:
    """Remove aliases/toggles whose machine is no longer registered."""
    global _labels_cache, _flags_cache
    labels = {key: value for key, value in load_labels().items()
              if key in known_machines}
    flags = {key: value for key, value in load_flags().items()
             if key in known_machines}
    if labels != load_labels():
        LABELS_FILE.write_text(json.dumps(labels, indent=2) + "\n")
    if flags != load_flags():
        FLAGS_FILE.write_text(json.dumps(flags, indent=2) + "\n")
    _labels_cache = labels
    _flags_cache = flags

# The five sibling studios and their fixed family ports. `app` is the Pinokio
# launcher folder name under PINOKIO_HOME/api — used by lifecycle control
# (pterm --ref). Override in studios.json if your folder names differ.
DEFAULT_STUDIOS = [
    {"id": "image", "title": "Image Studio KH", "modality": "image",
     "host": "127.0.0.1", "port": 47868, "machine": "local", "emoji": "🎨",
     "app": "imagestudio-mac"},
    {"id": "music", "title": "Music Studio KH", "modality": "music",
     "host": "127.0.0.1", "port": 47869, "machine": "local", "emoji": "🎵",
     "app": "musicstudio-mac"},
    {"id": "voice", "title": "Voice Studio KH", "modality": "voice",
     "host": "127.0.0.1", "port": 47870, "machine": "local", "emoji": "🎙️",
     "app": "voicestudio-mac.git"},
    {"id": "chat", "title": "Chat Studio KH", "modality": "chat",
     "host": "127.0.0.1", "port": 47871, "machine": "local", "emoji": "💬",
     "app": "chatstudio-mac.git"},
    {"id": "video", "title": "Video Studio KH", "modality": "video",
     "host": "127.0.0.1", "port": 47872, "machine": "local", "emoji": "🎬",
     "app": "videostudio-mac"},
    {"id": "render", "title": "Render Studio KH", "modality": "render",
     "host": "127.0.0.1", "port": 47874, "machine": "local", "emoji": "🖥️",
     "app": "renderstudio-mac"},
]


def load_registry() -> list[dict]:
    """Defaults merged with the optional per-machine studios.json."""
    studios = {s["id"]: dict(s) for s in DEFAULT_STUDIOS}
    if REGISTRY_FILE.exists():
        try:
            user_entries = json.loads(REGISTRY_FILE.read_text())
            for entry in user_entries:
                sid = entry.get("id")
                if not sid:
                    continue
                if sid in studios:
                    studios[sid].update(entry)  # override defaults by id
                else:
                    # New entry (e.g. a remote machine's studio). Sensible fill-ins.
                    entry.setdefault("title", sid)
                    entry.setdefault("modality", "unknown")
                    entry.setdefault("host", "127.0.0.1")
                    entry.setdefault("machine", "local")
                    entry.setdefault("emoji", "🧩")
                    if "port" not in entry:
                        continue  # unroutable without a port — skip
                    studios[sid] = entry
        except (json.JSONDecodeError, OSError):
            pass  # malformed override file → fall back to defaults, never crash
    return list(studios.values())


def base_url(studio: dict) -> str:
    return f"http://{studio['host']}:{studio['port']}"


# Family port convention — used by discovery to infer modality.
FAMILY_PORTS = {47868: "image", 47869: "music", 47870: "voice",
                47871: "chat", 47872: "video", 47874: "render"}
MODALITY_PORT = {v: k for k, v in FAMILY_PORTS.items()}
MODALITY_EMOJI = {"image": "🎨", "music": "🎵", "voice": "🎙️",
                  "chat": "💬", "video": "🎬", "render": "🖥️"}


def build_machine_entries(host: str, machine: str, modalities: list[str]) -> list[dict]:
    """Construct studios.json entries for a machine WITHOUT probing it — so a
    currently-offline machine can be pre-registered and will simply light up
    when it comes online."""
    entries = []
    for mod in modalities:
        port = MODALITY_PORT.get(mod)
        if port is None:
            continue
        entries.append({
            "id": f"{mod}@{machine}",
            "title": f"{mod.capitalize()} Studio KH ({machine})",
            "modality": mod, "host": host, "port": port,
            "machine": machine, "emoji": MODALITY_EMOJI[mod],
        })
    return entries


def remove_machine(machine: str) -> int:
    """Drop a machine's registry entries and machine-specific UI settings."""
    global _labels_cache, _flags_cache
    if not REGISTRY_FILE.exists():
        return 0
    try:
        existing = json.loads(REGISTRY_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return 0
    kept = [e for e in existing if e.get("machine") != machine]
    removed = len(existing) - len(kept)
    if removed:
        REGISTRY_FILE.write_text(json.dumps(kept, indent=2) + "\n")
        labels = dict(load_labels())
        if labels.pop(machine, None) is not None:
            LABELS_FILE.write_text(json.dumps(labels, indent=2) + "\n")
        _labels_cache = labels
        flags = dict(load_flags())
        if flags.pop(machine, None) is not None:
            FLAGS_FILE.write_text(json.dumps(flags, indent=2) + "\n")
        _flags_cache = flags
    return removed


def remove_studio(studio_id: str) -> int:
    """Drop a single studios.json entry by id (e.g. 'music@macmini-m1-01') — for
    pruning a studio type that isn't installed on that machine, without removing
    the rest. Local defaults live in code, not the file, so they're untouched."""
    global _flags_cache
    if not REGISTRY_FILE.exists():
        return 0
    try:
        existing = json.loads(REGISTRY_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return 0
    kept = [e for e in existing if e.get("id") != studio_id]
    removed = len(existing) - len(kept)
    if removed:
        REGISTRY_FILE.write_text(json.dumps(kept, indent=2) + "\n")
        entry = next((e for e in existing if e.get("id") == studio_id), None)
        if entry:
            machine = entry.get("machine", "local")
            flags = dict(load_flags())
            saved_machine = flags.get(machine, {})
            machine_flags = (
                dict(saved_machine) if isinstance(saved_machine, dict) else {}
            )
            studios = dict(machine_flags.get("studios", {}))
            if studios.pop(studio_id, None) is not None:
                machine_flags["studios"] = studios
                flags[machine] = machine_flags
                FLAGS_FILE.write_text(json.dumps(flags, indent=2) + "\n")
                _flags_cache = flags
    return removed


def add_user_entries(entries: list[dict]) -> int:
    """Append/merge entries into studios.json (per-machine registry state)."""
    existing = []
    if REGISTRY_FILE.exists():
        try:
            existing = json.loads(REGISTRY_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            existing = []
    by_id = {e.get("id"): e for e in existing if e.get("id")}
    endpoints = {(e.get("host"), e.get("port")) for e in existing
                 if e.get("host") and e.get("port")}
    added = 0
    for entry in entries:
        endpoint = (entry.get("host"), entry.get("port"))
        duplicate_endpoint = all(endpoint) and endpoint in endpoints
        if entry.get("id") and entry["id"] not in by_id and not duplicate_endpoint:
            by_id[entry["id"]] = entry
            if all(endpoint):
                endpoints.add(endpoint)
            added += 1
    REGISTRY_FILE.write_text(json.dumps(list(by_id.values()), indent=2) + "\n")
    return added
