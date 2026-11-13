"""Timing accuracy, sticky-key safety, focus-loss, and scheduler tests."""

from __future__ import annotations

import statistics
import threading
import time
from collections.abc import Sequence

import pytest

from ropi.domain.arranger import ArrangeOptions, KeyAction, arrange_song
from ropi.domain.keymap import Keymap, build_roblox_61_profile
from ropi.domain.note import NoteEvent, SongData, TrackInfo
from ropi.playback.focus_watcher import FakeFocusBackend
from ropi.playback.scheduler import PlaybackScheduler, PlaybackSnapshot, PlaybackState
from ropi.playback.service import PlaybackService
from ropi.playback.win32_input import FakeInputBackend


class FailingInputBackend(FakeInputBackend):
    """Fake backend that raises after N batches to test finally-block key safety."""

    def __init__(self, fail_after: int = 3) -> None:
        super().__init__()
        self._fail_after = fail_after
        self._batches = 0

    def execute_batch(self, actions: Sequence[KeyAction]) -> None:
        self._batches += 1
        super().execute_batch(actions)
        if self._batches >= self._fail_after:
            raise RuntimeError("Simulated Win32 SendInput failure")


@pytest.mark.slow
def test_scheduler_10k_events_timing_accuracy() -> None:
    """Measure timing accuracy over >10,000 KeyActions (5,040 down + 5,040 up)."""
    km = Keymap(build_roblox_61_profile())
    white_notes = [b.midi_note for b in km.all_bindings() if b.modifier == "none"]
    assert len(white_notes) == 36
    groups = [white_notes[0:12], white_notes[12:24], white_notes[24:36]]

    # 420 chords * 12 distinct scancodes = 5,040 notes = 10,080 KeyActions over ~3.8s.
    # Each group repeats every 27ms with 15ms hold and 12ms gap (zero scancode collision).
    notes: list[NoteEvent] = []
    for step in range(420):
        start_ms = 20 + step * 9
        for pitch in groups[step % 3]:
            notes.append(NoteEvent(start_ms=start_ms, duration_ms=15, midi_note=pitch))

    song = SongData(
        notes=tuple(notes),
        tracks=(TrackInfo(0, "Bench", 0, len(notes)),),
        duration_ms=20 + 420 * 9 + 30,
        bpm=120.0,
        min_note=white_notes[0],
        max_note=white_notes[-1],
    )
    actions = arrange_song(song, km, ArrangeOptions(sustain_mode="hold"))
    assert len(actions) == 10_080

    backend = FakeInputBackend(record_modifiers=False)
    focus = FakeFocusBackend(foreground=True)
    done = threading.Event()

    def on_snap(snap: PlaybackSnapshot) -> None:
        if snap.state == PlaybackState.IDLE and snap.position_ms > 0:
            done.set()

    scheduler = PlaybackScheduler(backend=backend, focus_backend=focus, on_snapshot=on_snap)
    scheduler.start(
        actions,
        duration_ms=song.duration_ms,
        countdown_sec=0,
        pause_on_focus_loss=True,
    )
    assert done.wait(timeout=10.0), "10k benchmark did not complete in time"
    assert len(backend.events) == 10_080

    # Align actual relative timestamps against first event to evaluate scheduler jitter.
    origin_perf = backend.events[0].perf_ns - backend.events[0].target_ns
    errors_ms = [
        abs((ev.perf_ns - origin_perf) - ev.target_ns) / 1_000_000 for ev in backend.events
    ]
    mean_err = statistics.mean(errors_ms)
    sorted_err = sorted(errors_ms)
    p99_err = sorted_err[int(len(sorted_err) * 0.99)]
    print(f"\n[Timing 10k] mean={mean_err:.3f}ms, p99={p99_err:.3f}ms, max={sorted_err[-1]:.3f}ms")

    assert mean_err <= 2.0
    assert p99_err <= 5.0
    assert not backend.pressed_keys


def test_scheduler_releases_all_keys_on_exception() -> None:
    km = Keymap(build_roblox_61_profile())
    song = SongData(
        notes=tuple(NoteEvent(i * 40, 200, 60 + (i % 5)) for i in range(10)),
        tracks=(TrackInfo(0, "Test", 0, 10),),
        duration_ms=600,
        bpm=120.0,
        min_note=60,
        max_note=64,
    )
    actions = arrange_song(song, km, ArrangeOptions(sustain_mode="hold"))
    backend = FailingInputBackend(fail_after=2)
    done = threading.Event()
    errors: list[str] = []

    def on_snap(snap: PlaybackSnapshot) -> None:
        if snap.error_message:
            errors.append(snap.error_message)
            done.set()

    scheduler = PlaybackScheduler(backend=backend, on_snapshot=on_snap)
    scheduler.start(actions, duration_ms=600, pause_on_focus_loss=False)
    assert done.wait(timeout=5.0)
    assert errors and "Simulated Win32 SendInput failure" in errors[0]
    assert backend.release_all_calls >= 1
    assert not backend.pressed_keys


def test_service_focus_loss_pause_resume_seek_panic() -> None:
    km = Keymap(build_roblox_61_profile())
    notes = [NoteEvent(i * 50, 40, 60 + (i % 8)) for i in range(12)]
    song = SongData(
        notes=tuple(notes),
        tracks=(TrackInfo(0, "Test", 0, len(notes)),),
        duration_ms=600,
        bpm=120.0,
        min_note=60,
        max_note=67,
    )
    backend = FakeInputBackend()
    focus = FakeFocusBackend(foreground=True)
    snaps: list[PlaybackSnapshot] = []
    paused_event = threading.Event()
    finished_event = threading.Event()

    def on_snap(snap: PlaybackSnapshot) -> None:
        snaps.append(snap)
        if snap.state == PlaybackState.PAUSED and snap.focus_paused:
            paused_event.set()
        if snap.state == PlaybackState.IDLE and snap.position_ms >= song.duration_ms:
            finished_event.set()

    service = PlaybackService(backend=backend, focus_backend=focus, keymap=km, on_snapshot=on_snap)
    service.load_song(song)
    assert service.is_roblox_running() is True

    # Play, then simulate focus loss
    service.play(countdown_sec=0, activate_roblox=False)
    time.sleep(0.08)
    focus.foreground = False
    assert paused_event.wait(timeout=2.0)
    assert not backend.pressed_keys

    # Resume with focus restored and complete
    focus.foreground = True
    service.set_speed(1.5)
    service.resume(countdown_sec=0, activate_roblox=False)
    assert finished_event.wait(timeout=3.0)
    assert not backend.pressed_keys

    # Seek & panic safety
    service.seek(200)
    service.play(start_ms=200, countdown_sec=0, activate_roblox=False)
    time.sleep(0.03)
    service.panic()
    assert service.state == PlaybackState.IDLE
    assert not backend.pressed_keys


def test_win32_input_struct_layout_and_cli_scales() -> None:
    import ctypes

    from ropi.playback.win32_input import _INPUT
    from ropi.tools.play_cli import build_scale_song

    assert ctypes.sizeof(_INPUT) == 40
    assert build_scale_song("cmajor").note_count == 29
    assert build_scale_song("chromatic61").note_count == 61
    assert build_scale_song("chromatic88").note_count == 88
