"""Rotating, privacy-conscious diagnostics for the narrow quota bar."""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import threading
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path


LOGGER = logging.getLogger("codex_status_pet.narrow_bar")
_CONFIGURED = False


def log_exception(event, error, tb=None):
    """Log exception type and source locations, not messages or payloads."""
    if not _CONFIGURED:
        configure_logging()
    tb = tb or getattr(error, "__traceback__", None)
    frames = traceback.extract_tb(tb)[-8:] if tb else []
    locations = " > ".join(
        f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}" for frame in frames
    ) or "unavailable"
    LOGGER.error("%s | exception_type=%s | stack=%s", event, type(error).__name__, locations)


def _main_exception_hook(exc_type, error, tb):
    log_exception("uncaught main-thread exception", error, tb)


def _thread_exception_hook(args):
    log_exception("uncaught background-thread exception", args.exc_value, args.exc_traceback)


def configure_logging():
    global _CONFIGURED
    if _CONFIGURED:
        return LOGGER

    directories = []
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        directories.append(Path(local_app_data) / "CodexStatusPet" / "logs")
    directories.append(Path(tempfile.gettempdir()) / "CodexStatusPet" / "logs")

    handler = None
    for directory in directories:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                directory / "narrow_status_bar.log",
                maxBytes=512 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
            break
        except OSError:
            continue

    if handler is None:
        LOGGER.addHandler(logging.NullHandler())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s pid=%(process)d %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False
    sys.excepthook = _main_exception_hook
    if hasattr(threading, "excepthook"):
        threading.excepthook = _thread_exception_hook
    _CONFIGURED = True
    return LOGGER
