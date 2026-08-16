import threading

import pytest

from backend.controller_settings_lock import (
    SettingsWriterBusy,
    settings_writer_lock,
)


def test_second_thread_gets_settings_writer_busy(reset):
    """A repair writer excludes a simultaneous managed settings writer."""
    result = []
    finished = threading.Event()

    def second_writer():
        try:
            with settings_writer_lock():
                result.append("acquired")
        except SettingsWriterBusy:
            result.append("busy")
        finally:
            finished.set()

    with settings_writer_lock():
        worker = threading.Thread(target=second_writer)
        worker.start()
        assert finished.wait(2)
    worker.join(2)

    assert result == ["busy"]


def test_nested_nonwriting_settings_reads_remain_available(reset):
    from backend import control_plane

    with settings_writer_lock():
        loaded = control_plane.load_settings()
        public = control_plane.public_settings()

    assert loaded["role"] == "standalone"
    assert public["role"] == "standalone"
