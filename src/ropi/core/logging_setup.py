"""Logging and native crash dump setup for Ropi."""

from __future__ import annotations

import faulthandler
import logging
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TextIO

_MAX_LOG_BYTES = 5 * 1024 * 1024  # 5 MB
_BACKUP_COUNT = 5
_FAULT_FILE_HANDLE: TextIO | None = None


def setup_logging(logs_dir: Path, level: int = logging.INFO) -> Path:
    """Configure rotating file logs and faulthandler crash dump file.

    Returns the main log file path.
    """
    global _FAULT_FILE_HANDLE
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / "ropi.log"

    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Clear previously attached Ropi handlers to remain idempotent in tests.
    for handler in list(root_logger.handlers):
        if getattr(handler, "_ropi_managed", False):
            root_logger.removeHandler(handler)
            handler.close()

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s (%(threadName)s): %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=_MAX_LOG_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    file_handler._ropi_managed = True  # type: ignore[attr-defined]
    root_logger.addHandler(file_handler)

    if sys.stderr is not None:
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setLevel(level)
        stream_handler.setFormatter(formatter)
        stream_handler._ropi_managed = True  # type: ignore[attr-defined]
        root_logger.addHandler(stream_handler)

    # Enable faulthandler for native crash dumps (N2).
    _prune_old_crash_logs(logs_dir)
    if _FAULT_FILE_HANDLE is not None and not _FAULT_FILE_HANDLE.closed:
        if faulthandler.is_enabled():
            faulthandler.disable()
        _FAULT_FILE_HANDLE.close()

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    crash_path = logs_dir / f"crash-{stamp}.log"
    _FAULT_FILE_HANDLE = crash_path.open("w", encoding="utf-8")
    faulthandler.enable(file=_FAULT_FILE_HANDLE, all_threads=True)

    logging.getLogger(__name__).info("Logging initialized at %s", log_path)
    return log_path


def shutdown_logging() -> None:
    """Cleanly flush and close faulthandler and managed log handlers."""
    global _FAULT_FILE_HANDLE
    if faulthandler.is_enabled():
        faulthandler.disable()

    if _FAULT_FILE_HANDLE is not None:
        crash_path = Path(_FAULT_FILE_HANDLE.name)
        if not _FAULT_FILE_HANDLE.closed:
            _FAULT_FILE_HANDLE.flush()
            _FAULT_FILE_HANDLE.close()
        _FAULT_FILE_HANDLE = None
        # Remove zero-byte crash log left by a clean shutdown.
        if crash_path.exists() and crash_path.stat().st_size == 0:
            crash_path.unlink(missing_ok=True)

    root_logger = logging.getLogger()
    for handler in list(root_logger.handlers):
        if getattr(handler, "_ropi_managed", False):
            root_logger.removeHandler(handler)
            handler.close()


def _prune_old_crash_logs(logs_dir: Path, keep: int = 10) -> None:
    crash_files = sorted(
        logs_dir.glob("crash-*.log"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in crash_files[keep:]:
        old.unlink(missing_ok=True)
