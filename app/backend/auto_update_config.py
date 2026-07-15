"""Studio Hub's fixed, non-user-editable updater identity."""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from .auto_update import AutoUpdater


ROOT = Path(__file__).resolve().parents[2]
SPEC = {
    "root": str(ROOT),
    "title": "Studio Hub KH",
    "slug": "studiohub",
    "expected_remote": "https://github.com/theng12/studiohub-mac.git",
    "branch": "main",
    "port": 47873,
    "server_label": "com.kh.studiohub.server",
    "watchdog_label": "com.kh.studiohub.watchdog",
    "default_hour": 1,
    "default_weekday": 6,
    "verify_module": "backend.main",
    "requirements": "requirements.lock",
}


def create_updater(readiness: Optional[Callable[[], list[str]]] = None, **kwargs) -> AutoUpdater:
    return AutoUpdater(SPEC, readiness=readiness, **kwargs)
