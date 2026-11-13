"""High-precision background playback scheduler with sticky-key safety."""

from __future__ import annotations

import atexit
import ctypes
import logging
import sys
import threading
import time
from collections.abc import Callable, Sequence
from ctypes import wintypes
from dataclasses import dataclass
from enum import StrEnum

from ropi.domain.arranger import KeyAction
from ropi.playback.focus_watcher import FocusBackend
from ropi.playback.timing import HighResTimer, wait_until_ns
from ropi.playback.win32_input import InputBackend

logger = logging.getLogger(__name__)

_THREAD_PRIORITY_ABOVE_NORMAL = 1
_PROGRESS_THROTTLE_NS = 33_000_000  # ~30 Hz max UI updates


class PlaybackState(StrEnum):
    """Lifecycle states of the playback scheduler."""

    IDLE = "idle"
    COUNTDOWN = "countdown"
    PLAYING = "playing"
    PAUSED = "paused"


@dataclass(frozen=True, slots=True)
class PlaybackSnapshot:
    """Thread-safe snapshot emitted to listeners/UI."""

    state: PlaybackState
    position_ms: int
    duration_ms: int
    countdown_sec: int = 0
    focus_paused: bool = False
    active_labels: tuple[str, ...] = ()
    active_notes: tuple[int, ...] = ()
    error_message: str = ""


SnapshotCallback = Callable[[PlaybackSnapshot], None]


def _group_batches(actions: Sequence[KeyAction]) -> list[list[KeyAction]]:
    """Group sorted KeyActions sharing the same timestamp into atomic batches."""
    if not actions:
        return []
    batches: list[list[KeyAction]] = []
    current: list[KeyAction] = [actions[0]]
    current_ns = actions[0].time_ns
    for act in actions[1:]:
        if act.time_ns == current_ns:
            current.append(act)
        else:
            batches.append(current)
            current = [act]
            current_ns = act.time_ns
    batches.append(current)
    return batches


class PlaybackScheduler:
    """Executes scheduled KeyActions on a dedicated high-priority thread."""

    def __init__(
        self,
        backend: InputBackend,
        focus_backend: FocusBackend | None = None,
        on_snapshot: SnapshotCallback | None = None,
    ) -> None:
        self._backend = backend
        self._focus_backend = focus_backend
        self._on_snapshot = on_snapshot

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = PlaybackState.IDLE

        self._actions: list[KeyAction] = []
        self._duration_ms = 0
        self._speed = 1.0
        self._position_ms = 0
        self._pause_on_focus_loss = True
        self._dry_run = False

        atexit.register(self._atexit_release)

    @property
    def state(self) -> PlaybackState:
        with self._lock:
            return self._state

    @property
    def position_ms(self) -> int:
        with self._lock:
            return self._position_ms

    def set_position_ms(self, pos_ms: int) -> None:
        with self._lock:
            self._position_ms = max(0, pos_ms)

    def start(
        self,
        actions: Sequence[KeyAction],
        *,
        duration_ms: int,
        speed: float = 1.0,
        start_song_ms: int = 0,
        countdown_sec: int = 0,
        pause_on_focus_loss: bool = True,
        dry_run: bool = False,
        activate_roblox: bool = False,
    ) -> None:
        """Stop any active playback and start playing `actions` from `start_song_ms`."""
        self.stop()
        with self._lock:
            self._actions = list(actions)
            self._duration_ms = max(0, duration_ms)
            self._speed = speed
            self._position_ms = max(0, start_song_ms)
            self._pause_on_focus_loss = pause_on_focus_loss
            self._dry_run = dry_run
            self._stop_event.clear()

            thread = threading.Thread(
                target=self._run_worker,
                args=(start_song_ms, countdown_sec, activate_roblox),
                name="RopiPlaybackScheduler",
                daemon=True,
            )
            self._thread = thread
            self._state = PlaybackState.COUNTDOWN if countdown_sec > 0 else PlaybackState.PLAYING
            thread.start()

    def pause(self, *, focus_paused: bool = False) -> int:
        """Pause playback, immediately releasing all pressed keys. Returns current song_ms."""
        thread_to_join: threading.Thread | None = None
        with self._lock:
            if self._state not in (PlaybackState.PLAYING, PlaybackState.COUNTDOWN):
                return self._position_ms
            self._stop_event.set()
            thread_to_join = self._thread

        if thread_to_join is not None and thread_to_join is not threading.current_thread():
            thread_to_join.join(timeout=1.5)

        with self._lock:
            self._backend.release_all()
            self._state = PlaybackState.PAUSED
            pos = self._position_ms
            dur = self._duration_ms
        self._emit(
            PlaybackSnapshot(
                state=PlaybackState.PAUSED,
                position_ms=pos,
                duration_ms=dur,
                focus_paused=focus_paused,
            )
        )
        return pos

    def stop(self) -> None:
        """Stop playback and reset position to 0, releasing all pressed keys."""
        thread_to_join: threading.Thread | None = None
        with self._lock:
            self._stop_event.set()
            thread_to_join = self._thread

        if thread_to_join is not None and thread_to_join is not threading.current_thread():
            thread_to_join.join(timeout=1.5)

        with self._lock:
            self._backend.release_all()
            self._thread = None
            self._state = PlaybackState.IDLE
            self._position_ms = 0
            dur = self._duration_ms
        self._emit(PlaybackSnapshot(state=PlaybackState.IDLE, position_ms=0, duration_ms=dur))

    def _run_worker(
        self,
        start_song_ms: int,
        countdown_sec: int,
        activate_roblox: bool,
    ) -> None:
        _elevate_thread_priority()
        completed_naturally = False
        focus_lost = False
        error_msg = ""

        try:
            with HighResTimer(1):
                if countdown_sec > 0:
                    for remaining in range(countdown_sec, 0, -1):
                        if self._stop_event.is_set():
                            return
                        self._emit(
                            PlaybackSnapshot(
                                state=PlaybackState.COUNTDOWN,
                                position_ms=start_song_ms,
                                duration_ms=self._duration_ms,
                                countdown_sec=remaining,
                            )
                        )
                        if self._stop_event.wait(timeout=1.0):
                            return

                if activate_roblox and self._focus_backend is not None and not self._dry_run:
                    self._focus_backend.activate_roblox()
                    # Brief settle after window switch.
                    if self._stop_event.wait(timeout=0.12):
                        return

                with self._lock:
                    self._state = PlaybackState.PLAYING
                    actions = list(self._actions)
                    speed = self._speed
                    dur_ms = self._duration_ms
                    pause_focus = self._pause_on_focus_loss
                    dry_run = self._dry_run

                start_scaled_ns = round((start_song_ms / speed) * 1_000_000)
                filtered_actions = _slice_actions_from(actions, start_scaled_ns)
                batches = _group_batches(filtered_actions)

                self._emit(
                    PlaybackSnapshot(
                        state=PlaybackState.PLAYING,
                        position_ms=start_song_ms,
                        duration_ms=dur_ms,
                    )
                )

                origin_perf_ns = time.perf_counter_ns() - start_scaled_ns
                last_progress_ns = 0
                active_notes: dict[int, str] = {}

                for batch in batches:
                    if self._stop_event.is_set():
                        return

                    target_perf_ns = origin_perf_ns + batch[0].time_ns
                    if not wait_until_ns(target_perf_ns, self._stop_event):
                        return

                    if (
                        pause_focus
                        and self._focus_backend is not None
                        and not dry_run
                        and not self._focus_backend.is_roblox_foreground()
                    ):
                        focus_lost = True
                        break

                    if not dry_run:
                        self._backend.execute_batch(batch)

                    for act in batch:
                        if act.midi_note >= 0:
                            if act.kind == "down":
                                active_notes[act.midi_note] = act.label
                            else:
                                active_notes.pop(act.midi_note, None)

                    pos_ms = batch[-1].song_ms
                    with self._lock:
                        self._position_ms = pos_ms

                    now_ns = time.perf_counter_ns()
                    if now_ns - last_progress_ns >= _PROGRESS_THROTTLE_NS:
                        last_progress_ns = now_ns
                        self._emit(
                            PlaybackSnapshot(
                                state=PlaybackState.PLAYING,
                                position_ms=pos_ms,
                                duration_ms=dur_ms,
                                active_labels=tuple(active_notes.values()),
                                active_notes=tuple(active_notes.keys()),
                            )
                        )

                completed_naturally = not self._stop_event.is_set()
        except Exception as exc:
            logger.exception("Playback scheduler error")
            error_msg = str(exc)
        finally:
            try:
                self._backend.release_all()
            except Exception:
                logger.exception("Failed to release keys in scheduler finally block")

        if focus_lost:
            with self._lock:
                self._state = PlaybackState.PAUSED
                pos = self._position_ms
                dur = self._duration_ms
            self._emit(
                PlaybackSnapshot(
                    state=PlaybackState.PAUSED,
                    position_ms=pos,
                    duration_ms=dur,
                    focus_paused=True,
                )
            )
        elif error_msg:
            with self._lock:
                self._state = PlaybackState.IDLE
                self._position_ms = 0
                dur = self._duration_ms
            self._emit(
                PlaybackSnapshot(
                    state=PlaybackState.IDLE,
                    position_ms=0,
                    duration_ms=dur,
                    error_message=error_msg,
                )
            )
        elif completed_naturally:
            with self._lock:
                self._state = PlaybackState.IDLE
                dur = self._duration_ms
                self._position_ms = dur
            self._emit(
                PlaybackSnapshot(
                    state=PlaybackState.IDLE,
                    position_ms=dur,
                    duration_ms=dur,
                )
            )

    def _emit(self, snapshot: PlaybackSnapshot) -> None:
        if self._on_snapshot is not None:
            try:
                self._on_snapshot(snapshot)
            except Exception:
                logger.exception("Snapshot callback failed")

    def _atexit_release(self) -> None:
        try:
            self._stop_event.set()
            self._backend.release_all()
        except Exception:
            pass


def _slice_actions_from(actions: Sequence[KeyAction], start_scaled_ns: int) -> list[KeyAction]:
    """Keep actions at or after `start_scaled_ns`, dropping orphan UP events without a DOWN."""
    if start_scaled_ns <= 0:
        return list(actions)
    pressed_notes: set[tuple[int, int]] = set()
    result: list[KeyAction] = []
    for act in actions:
        if act.time_ns < start_scaled_ns:
            continue
        key = (act.scancode, act.midi_note)
        if act.kind == "down":
            pressed_notes.add(key)
            result.append(act)
        elif key in pressed_notes:
            pressed_notes.discard(key)
            result.append(act)
    return result


def _elevate_thread_priority() -> None:
    if sys.platform != "win32":
        return
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentThread.argtypes = ()
        kernel32.GetCurrentThread.restype = wintypes.HANDLE
        kernel32.SetThreadPriority.argtypes = (wintypes.HANDLE, wintypes.INT)
        kernel32.SetThreadPriority.restype = wintypes.BOOL
        handle = kernel32.GetCurrentThread()
        kernel32.SetThreadPriority(handle, _THREAD_PRIORITY_ABOVE_NORMAL)
    except Exception:
        pass
