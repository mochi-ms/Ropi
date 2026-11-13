"""Windows foreground process inspection and Roblox window activation."""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Protocol

DEFAULT_ROBLOX_PROCESSES: frozenset[str] = frozenset(
    {
        "robloxplayerbeta.exe",
        "robloxplayerlauncher.exe",
        "windows10universal.exe",  # Microsoft Store Roblox UWP client
    }
)

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SW_RESTORE = 9


class FocusBackend(Protocol):
    """Abstract foreground window probe for testing and Windows runtime."""

    def is_roblox_foreground(self) -> bool: ...

    def is_roblox_running(self) -> bool: ...

    def activate_roblox(self) -> bool: ...


class FakeFocusBackend:
    """Deterministic focus backend for unit tests."""

    def __init__(self, *, foreground: bool = True, running: bool = True) -> None:
        self.foreground = foreground
        self.running = running
        self.activate_calls = 0

    def is_roblox_foreground(self) -> bool:
        return self.foreground

    def is_roblox_running(self) -> bool:
        return self.running

    def activate_roblox(self) -> bool:
        self.activate_calls += 1
        if self.running:
            self.foreground = True
            return True
        return False


class Win32FocusBackend:
    """Inspects and activates the Roblox desktop window via Win32 API."""

    def __init__(self, target_processes: frozenset[str] = DEFAULT_ROBLOX_PROCESSES) -> None:
        if sys.platform != "win32":
            raise RuntimeError("Win32FocusBackend requires Windows.")
        self._targets = {p.lower() for p in target_processes}
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        self._kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        self._kernel32.OpenProcess.restype = wintypes.HANDLE
        self._kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        self._kernel32.QueryFullProcessImageNameW.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        )
        self._kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

    def get_foreground_process_name(self) -> str | None:
        hwnd = self._user32.GetForegroundWindow()
        if not hwnd:
            return None
        return self._process_name_for_hwnd(hwnd)

    def is_roblox_foreground(self) -> bool:
        proc = self.get_foreground_process_name()
        return proc is not None and proc.lower() in self._targets

    def find_roblox_window(self) -> int | None:
        found: list[int] = []
        enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def callback(hwnd: int, _lparam: int) -> bool:
            if not self._user32.IsWindowVisible(hwnd):
                return True
            proc = self._process_name_for_hwnd(hwnd)
            if proc is not None and proc.lower() in self._targets:
                found.append(hwnd)
                return False
            return True

        self._user32.EnumWindows(enum_proc(callback), 0)
        return found[0] if found else None

    def is_roblox_running(self) -> bool:
        return self.find_roblox_window() is not None

    def activate_roblox(self) -> bool:
        hwnd = self.find_roblox_window()
        if hwnd is None:
            return False
        if self._user32.IsIconic(hwnd):
            self._user32.ShowWindow(hwnd, _SW_RESTORE)
        if self._user32.SetForegroundWindow(hwnd):
            return True
        # Fallback: temporarily attach thread input queues to allow SetForegroundWindow.
        fg = self._user32.GetForegroundWindow()
        fg_tid = self._user32.GetWindowThreadProcessId(fg, None) if fg else 0
        cur_tid = self._kernel32.GetCurrentThreadId()
        attached = False
        try:
            if fg_tid and fg_tid != cur_tid:
                attached = bool(self._user32.AttachThreadInput(cur_tid, fg_tid, True))
            self._user32.BringWindowToTop(hwnd)
            return bool(self._user32.SetForegroundWindow(hwnd))
        finally:
            if attached:
                self._user32.AttachThreadInput(cur_tid, fg_tid, False)

    def _process_name_for_hwnd(self, hwnd: int) -> str | None:
        pid = wintypes.DWORD(0)
        self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return None
        handle = self._kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not handle:
            return None
        try:
            buf_len = wintypes.DWORD(512)
            buf = ctypes.create_unicode_buffer(buf_len.value)
            if not self._kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(buf_len)):
                return None
            return Path(buf.value).name.lower()
        finally:
            self._kernel32.CloseHandle(handle)
