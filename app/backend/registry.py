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
REGISTRY_FILE = LAUNCHER_ROOT / "studios.json"

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
     "app": "videostudio-mac.git"},
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
                47871: "chat", 47872: "video"}
MODALITY_EMOJI = {"image": "🎨", "music": "🎵", "voice": "🎙️",
                  "chat": "💬", "video": "🎬"}


def remove_machine(machine: str) -> int:
    """Drop all studios.json entries for a machine (defaults are untouchable)."""
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
    added = 0
    for entry in entries:
        if entry.get("id") and entry["id"] not in by_id:
            by_id[entry["id"]] = entry
            added += 1
    REGISTRY_FILE.write_text(json.dumps(list(by_id.values()), indent=2) + "\n")
    return added
