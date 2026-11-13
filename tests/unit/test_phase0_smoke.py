"""Phase 0 smoke tests for Ropi core infrastructure and GUI bootstrap."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from ropi import __app_name__, __version__
from ropi.core.errors import install_exception_hooks, restore_exception_hooks
from ropi.core.logging_setup import setup_logging, shutdown_logging
from ropi.core.paths import get_app_paths


def test_app_metadata() -> None:
    assert __app_name__ == "Ropi"
    assert __version__ == "0.1.0"


def test_paths_and_logging_roundtrip(tmp_path: Path) -> None:
    paths = get_app_paths(base_dir=tmp_path)
    paths.ensure_dirs()
    assert paths.config_dir.is_dir()
    assert paths.logs_dir.is_dir()
    assert paths.default_library_dir.is_dir()

    log_path = setup_logging(paths.logs_dir)
    logging.getLogger("ropi.test").info("smoke log message")
    shutdown_logging()

    assert log_path.exists()
    content = log_path.read_text(encoding="utf-8")
    assert "smoke log message" in content


def test_exception_hook_captures_error() -> None:
    captured: list[tuple[str, str]] = []
    install_exception_hooks(on_unhandled=lambda s, d: captured.append((s, d)))
    try:
        import sys

        try:
            raise ValueError("boom")
        except ValueError as exc:
            sys.excepthook(type(exc), exc, exc.__traceback__)
    finally:
        restore_exception_hooks()

    assert len(captured) == 1
    assert "ValueError: boom" in captured[0][1]


@pytest.mark.gui
def test_placeholder_window_creates(qtbot: object) -> None:
    from PySide6.QtWidgets import QMainWindow

    from ropi.__main__ import create_placeholder_window

    win = create_placeholder_window()
    assert isinstance(win, QMainWindow)
    assert "Ropi" in win.windowTitle()
    win.close()


@pytest.mark.gui
def test_main_smoke_exit(tmp_path: Path, qtbot: object) -> None:
    from ropi.__main__ import main

    exit_code = main(["--smoke-exit-ms", "50", "--data-dir", str(tmp_path)])
    assert exit_code == 0
    log_file = tmp_path / "localappdata" / "logs" / "ropi.log"
    assert log_file.exists()
    assert "started." in log_file.read_text(encoding="utf-8")
