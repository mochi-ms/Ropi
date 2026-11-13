"""High-resolution Windows multimedia timer and hybrid sleep/spin waiter."""

from __future__ import annotations

import ctypes
import sys
import threading
import time
from types import TracebackType

_DEFAULT_SPIN_THRESHOLD_NS = (
    2_500_000  # 2.5 ms spin window covers Win11 high-res timer wakeup jitter
)
_STOP_POLL_SLICE_NS = 12_000_000  # check stop_event at least every 12 ms during long waits


class HighResTimer:
    """Context manager enabling 1ms Windows system timer resolution (timeBeginPeriod)."""

    def __init__(self, period_ms: int = 1) -> None:
        self._period_ms = period_ms
        self._active = False
        self._winmm = ctypes.WinDLL("winmm") if sys.platform == "win32" else None
        if self._winmm is not None:
            self._winmm.timeBeginPeriod.argtypes = (ctypes.c_uint,)
            self._winmm.timeBeginPeriod.restype = ctypes.c_uint
            self._winmm.timeEndPeriod.argtypes = (ctypes.c_uint,)
            self._winmm.timeEndPeriod.restype = ctypes.c_uint

    def __enter__(self) -> HighResTimer:
        if self._winmm is not None:
            res = self._winmm.timeBeginPeriod(self._period_ms)
            self._active = res == 0
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_val: BaseException | None,
        _exc_tb: TracebackType | None,
    ) -> None:
        if self._winmm is not None and self._active:
            self._winmm.timeEndPeriod(self._period_ms)
            self._active = False


def wait_until_ns(
    target_ns: int,
    stop_event: threading.Event,
    *,
    spin_threshold_ns: int = _DEFAULT_SPIN_THRESHOLD_NS,
) -> bool:
    """Sleep + spin-wait until `time.perf_counter_ns() >= target_ns`.

    Returns True when target time is reached, or False if `stop_event` was set early.
    """
    while True:
        if stop_event.is_set():
            return False
        now = time.perf_counter_ns()
        remaining = target_ns - now
        if remaining <= 0:
            return True
        if remaining > spin_threshold_ns:
            # Python 3.11+ time.sleep uses Win32 high-res waitable timers (~0.5ms jitter),
            # whereas Event.wait relies on condition variables quantized to ~15.6ms ticks.
            sleep_ns = min(remaining - spin_threshold_ns, _STOP_POLL_SLICE_NS)
            time.sleep(sleep_ns / 1_000_000_000)
        else:
            # Fine spin-wait for sub-millisecond accuracy.
            while time.perf_counter_ns() < target_ns:
                if stop_event.is_set():
                    return False
            return True
