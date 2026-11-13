"""Application exceptions and global unhandled-exception hooks."""

from __future__ import annotations

import logging
import sys
import threading
import traceback
from collections.abc import Callable
from types import TracebackType

logger = logging.getLogger(__name__)


class RopiError(Exception):
    """Base exception for all Ropi domain/application errors."""


class FormatLoadError(RopiError):
    """Raised when a MIDI, VP sheet, LRC, or .ropi file cannot be parsed."""


class PlaybackError(RopiError):
    """Raised when playback preparation or input dispatch fails."""


class LibraryError(RopiError):
    """Raised when database or file-store operations fail."""


class ConvertError(RopiError):
    """Raised when YouTube/audio download or transcription fails."""


ErrorCallback = Callable[[str, str], None]

_orig_sys_excepthook = sys.excepthook
_orig_threading_excepthook = threading.excepthook


def _format_traceback(
    exc_type: type[BaseException],
    exc_value: BaseException,
    exc_tb: TracebackType | None,
) -> str:
    return "".join(traceback.format_exception(exc_type, exc_value, exc_tb))


def install_exception_hooks(on_unhandled: ErrorCallback | None = None) -> None:
    """Install global exception hooks for main thread, worker threads, and Qt messages."""

    def handle_sys_exception(
        exc_type: type[BaseException],
        exc_value: BaseException,
        exc_tb: TracebackType | None,
    ) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            _orig_sys_excepthook(exc_type, exc_value, exc_tb)
            return
        details = _format_traceback(exc_type, exc_value, exc_tb)
        logger.critical("Unhandled exception on main thread:\n%s", details)
        if on_unhandled is not None:
            on_unhandled("예기치 못한 오류가 발생했습니다.", details)

    def handle_thread_exception(args: threading.ExceptHookArgs) -> None:
        if args.exc_value is None or issubclass(args.exc_type, SystemExit):
            _orig_threading_excepthook(args)
            return
        details = _format_traceback(args.exc_type, args.exc_value, args.exc_traceback)
        thread_name = args.thread.name if args.thread is not None else "unknown"
        logger.critical("Unhandled exception in thread '%s':\n%s", thread_name, details)
        if on_unhandled is not None:
            on_unhandled(f"백그라운드 작업({thread_name}) 중 오류가 발생했습니다.", details)

    sys.excepthook = handle_sys_exception
    threading.excepthook = handle_thread_exception
    _install_qt_message_handler()


def restore_exception_hooks() -> None:
    """Restore original Python exception hooks (used in tests)."""
    sys.excepthook = _orig_sys_excepthook
    threading.excepthook = _orig_threading_excepthook


def _install_qt_message_handler() -> None:
    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler
    except ImportError:
        return

    qt_logger = logging.getLogger("ropi.qt")

    def qt_message_handler(msg_type: QtMsgType, _context: object, message: str) -> None:
        if msg_type == QtMsgType.QtDebugMsg:
            qt_logger.debug("%s", message)
        elif msg_type == QtMsgType.QtInfoMsg:
            qt_logger.info("%s", message)
        elif msg_type == QtMsgType.QtWarningMsg:
            qt_logger.warning("%s", message)
        elif msg_type == QtMsgType.QtCriticalMsg:
            qt_logger.error("%s", message)
        elif msg_type == QtMsgType.QtFatalMsg:
            qt_logger.critical("%s", message)

    qInstallMessageHandler(qt_message_handler)
