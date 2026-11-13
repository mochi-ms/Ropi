"""Win32 SendInput hardware scan-code keyboard backend and test fake."""

from __future__ import annotations

import ctypes
import sys
import threading
import time
from collections.abc import Sequence
from ctypes import wintypes
from dataclasses import dataclass
from typing import Protocol

from ropi.domain.arranger import KeyAction
from ropi.domain.keymap import MODIFIER_SCANCODES, Modifier

_KEYEVENTF_EXTENDEDKEY = 0x0001
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_SCANCODE = 0x0008
_INPUT_KEYBOARD = 1


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", _MOUSEINPUT),
        ("ki", _KEYBDINPUT),
        ("hi", _HARDWAREINPUT),
    ]


class _INPUT(ctypes.Structure):
    _fields_ = [
        ("type", wintypes.DWORD),
        ("union", _INPUT_UNION),
    ]


class PressedKeyTracker:
    """Thread-safe tracker of currently pressed hardware scan codes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pressed: set[int] = set()

    def mark_down(self, scancode: int) -> None:
        with self._lock:
            self._pressed.add(scancode)

    def mark_up(self, scancode: int) -> None:
        with self._lock:
            self._pressed.discard(scancode)

    def snapshot_and_clear(self) -> list[int]:
        with self._lock:
            codes = sorted(self._pressed)
            self._pressed.clear()
            return codes

    @property
    def pressed(self) -> frozenset[int]:
        with self._lock:
            return frozenset(self._pressed)


class InputBackend(Protocol):
    """Abstract keyboard output backend for real Win32 input and deterministic tests."""

    @property
    def pressed_keys(self) -> frozenset[int]: ...

    def execute_batch(self, actions: Sequence[KeyAction]) -> None: ...

    def release_all(self) -> None: ...


@dataclass(frozen=True, slots=True)
class RecordedInputEvent:
    """Single key event captured by FakeInputBackend."""

    perf_ns: int
    target_ns: int
    kind: str  # "down" | "up"
    scancode: int
    modifier: Modifier
    label: str
    is_modifier_key: bool = False


class FakeInputBackend:
    """In-memory InputBackend recording exact dispatch timestamps for tests."""

    def __init__(self, *, record_modifiers: bool = True) -> None:
        self._tracker = PressedKeyTracker()
        self._record_modifiers = record_modifiers
        self.events: list[RecordedInputEvent] = []
        self._active_modifier: Modifier = "none"
        self.release_all_calls = 0

    @property
    def pressed_keys(self) -> frozenset[int]:
        return self._tracker.pressed

    def execute_batch(self, actions: Sequence[KeyAction]) -> None:
        now = time.perf_counter_ns()
        ups = [a for a in actions if a.kind == "up"]
        downs = [a for a in actions if a.kind == "down"]

        for act in ups:
            self._tracker.mark_up(act.scancode)
            self.events.append(
                RecordedInputEvent(
                    perf_ns=now,
                    target_ns=act.time_ns,
                    kind="up",
                    scancode=act.scancode,
                    modifier=act.modifier,
                    label=act.label,
                )
            )

        if not ups and not downs:
            return

        # Release modifier once all note keys needing it are released if no new downs use it.
        if not downs and self._active_modifier != "none":
            self._release_modifier(now)

        for mod in ("none", "shift", "ctrl"):
            group = [a for a in downs if a.modifier == mod]
            if not group:
                continue
            self._ensure_modifier(mod, now, group[0].time_ns)
            for act in group:
                self._tracker.mark_down(act.scancode)
                self.events.append(
                    RecordedInputEvent(
                        perf_ns=now,
                        target_ns=act.time_ns,
                        kind="down",
                        scancode=act.scancode,
                        modifier=act.modifier,
                        label=act.label,
                    )
                )

    def release_all(self) -> None:
        self.release_all_calls += 1
        now = time.perf_counter_ns()
        for sc in self._tracker.snapshot_and_clear():
            self.events.append(
                RecordedInputEvent(
                    perf_ns=now,
                    target_ns=now,
                    kind="up",
                    scancode=sc,
                    modifier="none",
                    label=f"sc:{sc:#x}",
                )
            )
        self._active_modifier = "none"

    def _ensure_modifier(self, target: Modifier, now: int, target_ns: int) -> None:
        if self._active_modifier == target:
            return
        if self._active_modifier != "none":
            self._release_modifier(now)
        if target != "none":
            mod_sc = MODIFIER_SCANCODES[target]
            self._tracker.mark_down(mod_sc)
            if self._record_modifiers:
                self.events.append(
                    RecordedInputEvent(
                        perf_ns=now,
                        target_ns=target_ns,
                        kind="down",
                        scancode=mod_sc,
                        modifier=target,
                        label=target,
                        is_modifier_key=True,
                    )
                )
        self._active_modifier = target

    def _release_modifier(self, now: int) -> None:
        if self._active_modifier == "none":
            return
        mod_sc = MODIFIER_SCANCODES[self._active_modifier]
        self._tracker.mark_up(mod_sc)
        if self._record_modifiers:
            self.events.append(
                RecordedInputEvent(
                    perf_ns=now,
                    target_ns=now,
                    kind="up",
                    scancode=mod_sc,
                    modifier="none",
                    label=self._active_modifier,
                    is_modifier_key=True,
                )
            )
        self._active_modifier = "none"


class Win32InputBackend:
    """Real Windows SendInput scan-code backend with sticky-key protection."""

    def __init__(self, *, modifier_settle_ms: float = 2.0) -> None:
        if sys.platform != "win32":
            raise RuntimeError("Win32InputBackend is only supported on Windows.")
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int)
        self._user32.SendInput.restype = wintypes.UINT
        self._tracker = PressedKeyTracker()
        self._active_modifier: Modifier = "none"
        self._modifier_settle_s = max(0.0, modifier_settle_ms / 1000.0)

    @property
    def pressed_keys(self) -> frozenset[int]:
        return self._tracker.pressed

    def execute_batch(self, actions: Sequence[KeyAction]) -> None:
        ups = [a for a in actions if a.kind == "up"]
        downs = [a for a in actions if a.kind == "down"]

        if ups:
            self._send_scancodes([(a.scancode, False) for a in ups])
            for a in ups:
                self._tracker.mark_up(a.scancode)

        if not downs:
            # Release active modifier once all regular note keys are released.
            note_keys_still_down = self._tracker.pressed - set(MODIFIER_SCANCODES.values())
            if not note_keys_still_down and self._active_modifier != "none":
                self._set_modifier("none")
            return

        for mod in ("none", "shift", "ctrl"):
            group = [a for a in downs if a.modifier == mod]
            if not group:
                continue
            changed = self._set_modifier(mod)
            if changed and mod != "none" and self._modifier_settle_s > 0:
                time.sleep(self._modifier_settle_s)
            self._send_scancodes([(a.scancode, True) for a in group])
            for a in group:
                self._tracker.mark_down(a.scancode)

    def release_all(self) -> None:
        codes = self._tracker.snapshot_and_clear()
        # Always include Left Shift, Left Ctrl, and Space as extra safety net.
        for mod_sc in MODIFIER_SCANCODES.values():
            if mod_sc not in codes:
                codes.append(mod_sc)
        if codes:
            self._send_scancodes([(sc, False) for sc in codes])
        self._active_modifier = "none"

    def _set_modifier(self, target: Modifier) -> bool:
        if self._active_modifier == target:
            return False
        events: list[tuple[int, bool]] = []
        if self._active_modifier != "none":
            prev_sc = MODIFIER_SCANCODES[self._active_modifier]
            events.append((prev_sc, False))
            self._tracker.mark_up(prev_sc)
        if target != "none":
            next_sc = MODIFIER_SCANCODES[target]
            events.append((next_sc, True))
            self._tracker.mark_down(next_sc)
        if events:
            self._send_scancodes(events)
        self._active_modifier = target
        return True

    def _send_scancodes(self, items: Sequence[tuple[int, bool]]) -> None:
        if not items:
            return
        arr = (_INPUT * len(items))()
        for idx, (scancode, is_down) in enumerate(items):
            flags = _KEYEVENTF_SCANCODE
            if not is_down:
                flags |= _KEYEVENTF_KEYUP
            if scancode > 0xFF:
                flags |= _KEYEVENTF_EXTENDEDKEY
            arr[idx].type = _INPUT_KEYBOARD
            arr[idx].union.ki = _KEYBDINPUT(
                wVk=0,
                wScan=scancode & 0xFF,
                dwFlags=flags,
                time=0,
                dwExtraInfo=0,
            )
        sent = self._user32.SendInput(len(items), arr, ctypes.sizeof(_INPUT))
        if sent != len(items):
            err = ctypes.get_last_error()
            if err != 0:
                raise ctypes.WinError(err)
