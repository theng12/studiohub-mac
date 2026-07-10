"""Time-series metrics + watchdog.

Metrics: an in-memory ring buffer sampled from the monitor's poll loop —
host memory/CPU plus per-studio RSS. One sample per SAMPLE_EVERY_S, capped at
MAX_SAMPLES (~24h). In-memory only by design: it's a dashboard aid, not a
long-term store.

Watchdog: opt-in per studio. When enabled and the studio is down, the Hub
fires a pterm start — with a cooldown so a crash-looping studio doesn't get
hammered, and auto-disable after too many consecutive failed revives.
Watchdog flags persist in hub_state.json at the launcher root (gitignored).
"""

import json
import time
from collections import deque

from .control import control_studio
from .registry import DATA_DIR
from .resources import host_stats, studio_process_stats

STATE_FILE = DATA_DIR / "hub_state.json"

SAMPLE_EVERY_S = 15
MAX_SAMPLES = 5760  # 24h at 15s

WATCHDOG_COOLDOWN_S = 120
WATCHDOG_MAX_FAILURES = 5

samples: deque = deque(maxlen=MAX_SAMPLES)
_last_sample = 0.0

# studio_id -> {"enabled": bool, "last_attempt": ts, "failures": int}
watchdog: dict[str, dict] = {}


def _load_state():
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            for sid, enabled in data.get("watchdog", {}).items():
                watchdog[sid] = {"enabled": bool(enabled), "last_attempt": 0.0,
                                 "failures": 0}
        except (json.JSONDecodeError, OSError):
            pass


def _save_state():
    try:
        STATE_FILE.write_text(json.dumps(
            {"watchdog": {sid: w["enabled"] for sid, w in watchdog.items()}},
            indent=2,
        ))
    except OSError:
        pass


_load_state()


def set_watchdog(studio_id: str, enabled: bool):
    w = watchdog.setdefault(
        studio_id, {"enabled": False, "last_attempt": 0.0, "failures": 0})
    w["enabled"] = enabled
    w["failures"] = 0
    _save_state()


def on_poll(registry: list[dict], statuses: dict):
    """Called from the monitor loop after each poll round: record a metrics
    sample (rate-limited) and run watchdog revival checks."""
    global _last_sample
    now = time.time()

    if now - _last_sample >= SAMPLE_EVERY_S:
        _last_sample = now
        host = host_stats()
        per_studio = {}
        for s in registry:
            st = statuses.get(s["id"], {})
            if st.get("status") == "up" and s.get("machine", "local") == "local":
                proc = studio_process_stats(s["port"])
                per_studio[s["id"]] = proc["rss_gb"] if proc else None
            else:
                per_studio[s["id"]] = None
        samples.append({
            "ts": now,
            "mem_percent": host["percent"],
            "mem_used_gb": host["used_gb"],
            "cpu_percent": host["cpu_percent"],
            "studios": per_studio,
        })

    for s in registry:
        w = watchdog.get(s["id"])
        if not w or not w["enabled"]:
            continue
        st = statuses.get(s["id"], {})
        if st.get("status") == "up":
            w["failures"] = 0
            continue
        if st.get("status") != "down":
            continue  # unknown = first poll hasn't happened; don't act on it
        if now - w["last_attempt"] < WATCHDOG_COOLDOWN_S:
            continue
        if w["failures"] >= WATCHDOG_MAX_FAILURES:
            w["enabled"] = False  # crash loop — stop trying, needs a human
            _save_state()
            continue
        w["last_attempt"] = now
        w["failures"] += 1
        control_studio(s, "start")


def get_metrics(minutes: int = 60, max_points: int = 240) -> dict:
    cutoff = time.time() - minutes * 60
    window = [s for s in samples if s["ts"] >= cutoff]
    if len(window) > max_points:  # downsample evenly for the chart
        step = len(window) / max_points
        window = [window[int(i * step)] for i in range(max_points)]
    return {"samples": window, "interval_s": SAMPLE_EVERY_S}


def watchdog_status() -> dict:
    return {sid: {"enabled": w["enabled"], "failures": w["failures"]}
            for sid, w in watchdog.items()}
