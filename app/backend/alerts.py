"""Alerts + structured event log.

Turns silent failures into signals. `emit()` records an event to an in-memory
ring (surfaced in the dashboard), logs it, and — if configured — POSTs it to a
webhook and/or raises a desktop notification. Wired to studio up/down
transitions and batch failures so an unattended fleet actually tells you when
something breaks.

Config lives in `.alerts.json` (DATA_DIR): {"webhook": url, "desktop": bool}.
"""

import asyncio
import json
import logging
import subprocess
import time
from collections import deque

import httpx

from .registry import DATA_DIR

log = logging.getLogger("studiohub.alerts")

ALERTS_FILE = DATA_DIR / ".alerts.json"
_recent: deque = deque(maxlen=200)


def load_config() -> dict:
    if ALERTS_FILE.exists():
        try:
            return json.loads(ALERTS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def set_config(cfg: dict):
    ALERTS_FILE.write_text(json.dumps(cfg or {}, indent=2) + "\n")


def recent(limit: int = 100) -> list:
    return list(_recent)[-limit:][::-1]  # newest first


async def _post_webhook(url: str, event: dict):
    try:
        async with httpx.AsyncClient() as c:
            await c.post(url, json=event, timeout=10.0)
    except httpx.HTTPError as e:
        log.warning("alert webhook failed: %s", e)


def _desktop_push(message: str):
    try:
        from .control import find_pterm
        pterm = find_pterm()
        if pterm:
            subprocess.Popen([pterm, "push", message],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            stdin=subprocess.DEVNULL, start_new_session=True)
    except Exception as e:  # never let a notification break the caller
        log.debug("desktop push skipped: %s", e)


def emit(kind: str, message: str, data: dict | None = None):
    """Record + fan out an alert. Safe from any context (sync or async)."""
    event = {"ts": time.time(), "kind": kind, "message": message, "data": data or {}}
    _recent.append(event)
    log.info("[%s] %s", kind, message)
    cfg = load_config()
    url = cfg.get("webhook")
    if url:
        try:
            asyncio.get_running_loop().create_task(_post_webhook(url, event))
        except RuntimeError:
            pass  # no running loop (rare) — skip the webhook, ring/log still recorded
    if cfg.get("desktop"):
        _desktop_push(f"Studio Hub: {message}")
    return event
