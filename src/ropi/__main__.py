"""Ropi application entry point."""

from __future__ import annotations

import argparse
import atexit
import logging
import sys
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import QApplication, QLabel, QMainWindow, QMessageBox, QVBoxLayout, QWidget

from ropi import __app_name__, __version__
from ropi.core.errors import install_exception_hooks
from ropi.core.logging_setup import setup_logging, shutdown_logging
from ropi.core.paths import get_app_paths

logger = logging.getLogger(__name__)


class _ErrorBridge(QObject):
    """Marshals background-thread unhandled errors onto the Qt main thread."""

    error_occurred = Signal(str, str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.error_occurred.connect(self._show_dialog, Qt.ConnectionType.QueuedConnection)

    @Slot(str, str)
    def _show_dialog(self, summary: str, details: str) -> None:
        box = QMessageBox()
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle(self.tr("Ropi 오류"))
        box.setText(summary)
        box.setDetailedText(details)
        box.exec()


def create_placeholder_window() -> QMainWindow:
    """Create the Phase 0 placeholder main window."""
    window = QMainWindow()
    window.setWindowTitle(f"{__app_name__} v{__version__}")
    window.resize(1280, 800)
    window.setMinimumSize(960, 600)

    central = QWidget(window)
    layout = QVBoxLayout(central)
    label = QLabel(central.tr("Ropi 초기화 완료 (Phase 0)"), central)
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    layout.addWidget(label)
    window.setCentralWidget(central)
    return window


def main(argv: list[str] | None = None) -> int:
    """Run the Ropi desktop application."""
    parser = argparse.ArgumentParser(prog="ropi", add_help=True)
    parser.add_argument(
        "--smoke-exit-ms",
        type=int,
        default=0,
        help="Automatically exit after N milliseconds (for startup verification).",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Override base directory for Ropi app data and logs.",
    )
    args, qt_args = parser.parse_known_args(argv)

    paths = get_app_paths(base_dir=args.data_dir)
    paths.ensure_dirs()
    setup_logging(paths.logs_dir)
    atexit.register(shutdown_logging)

    app = QApplication.instance()
    if not isinstance(app, QApplication):
        app = QApplication([sys.argv[0], *qt_args])
    app.setApplicationName(__app_name__)
    app.setApplicationVersion(__version__)

    error_bridge = _ErrorBridge()

    def notify_error(summary: str, details: str) -> None:
        error_bridge.error_occurred.emit(summary, details)

    install_exception_hooks(on_unhandled=notify_error)

    window = create_placeholder_window()
    window.show()
    logger.info("Ropi v%s started.", __version__)

    if args.smoke_exit_ms > 0:
        QTimer.singleShot(args.smoke_exit_ms, app.quit)

    exit_code = app.exec()
    logger.info("Ropi exiting with code %d.", exit_code)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
