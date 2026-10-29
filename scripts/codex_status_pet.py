"""Launcher entry point for the per-user narrow quota bar."""

import atexit
import msvcrt
import os
from pathlib import Path

from api.narrow_bar_logging_api import LOGGER, configure_logging


def acquire_single_instance_lock():
    """Keep one overlay process for this installation root."""
    lock_path = Path(__file__).resolve().parents[1] / "narrow-bar.instance.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = lock_path.open("a+b")
    try:
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        lock_file.close()
        return None
    return lock_file


configure_logging()
try:
    _instance_lock = acquire_single_instance_lock()
except OSError as error:
    LOGGER.error("single-instance lock setup failed | exception_type=%s", type(error).__name__)
    raise SystemExit(1) from None

if _instance_lock is None:
    LOGGER.info("duplicate instance launch ignored")
    raise SystemExit(0)

atexit.register(_instance_lock.close)
LOGGER.info("process entry started")
atexit.register(lambda: LOGGER.info("process exit hook invoked"))

from narrow_status_bar import run


if __name__ == "__main__":
    run()
    LOGGER.info("process entry returned normally")
