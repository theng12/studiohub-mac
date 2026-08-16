"""One re-entrant writer lock for managed controller identity changes."""

from __future__ import annotations

import fcntl
import os
import threading
from contextlib import contextmanager
from collections.abc import Iterator

from .registry import DATA_DIR


SETTINGS_WRITER_LOCK_FILE = DATA_DIR / "controller_settings.json.repair.lock"
_process_lock = threading.RLock()
_local = threading.local()


class SettingsWriterBusy(RuntimeError):
    """Another thread or process is atomically changing local identity."""


@contextmanager
def settings_writer_lock(*, blocking: bool = False) -> Iterator[None]:
    """Serialize managed settings writers without putting data in the lock file."""
    if not _process_lock.acquire(blocking=blocking):
        raise SettingsWriterBusy("settings_writer_busy")
    first_entry = not getattr(_local, "depth", 0)
    try:
        if first_entry:
            SETTINGS_WRITER_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(SETTINGS_WRITER_LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
            os.chmod(SETTINGS_WRITER_LOCK_FILE, 0o600)
            try:
                flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
                fcntl.flock(descriptor, flags)
            except OSError as exc:
                os.close(descriptor)
                if exc.errno in {11, 35}:
                    raise SettingsWriterBusy("settings_writer_busy") from exc
                raise
            _local.descriptor = descriptor
            _local.depth = 0
        _local.depth += 1
        yield
    finally:
        if getattr(_local, "depth", 0):
            _local.depth -= 1
            if _local.depth == 0:
                descriptor = _local.descriptor
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)
                    del _local.descriptor
                    del _local.depth
        _process_lock.release()
