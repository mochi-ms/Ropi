"""Application-layer playback service tying loaders, keymaps, arranger, and scheduler."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from ropi.core.errors import FormatLoadError, PlaybackError
from ropi.domain.arranger import ArrangeOptions, KeyAction, arrange_song, suggest_transpose
from ropi.domain.keymap import Keymap, build_roblox_61_profile, build_roblox_88_profile
from ropi.domain.note import SongData
from ropi.formats.midi_loader import load_midi
from ropi.formats.ropi_format import load_ropi
from ropi.formats.vp_sheet import load_vp_sheet_file
from ropi.playback.focus_watcher import FocusBackend, Win32FocusBackend
from ropi.playback.scheduler import (
    PlaybackScheduler,
    PlaybackSnapshot,
    PlaybackState,
    SnapshotCallback,
)
from ropi.playback.win32_input import InputBackend, Win32InputBackend


def load_song_file(path: Path, *, keymap: Keymap | None = None) -> SongData:
    """Load any supported song format (.mid, .midi, .txt, .ropi) into SongData."""
    ext = path.suffix.lower()
    if ext in {".mid", ".midi"}:
        return load_midi(path)
    if ext == ".txt":
        return load_vp_sheet_file(path, keymap=keymap)
    if ext == ".ropi":
        return load_ropi(path, keymap=keymap).song
    raise FormatLoadError(f"지원하지 않는 파일 형식입니다: {ext or '(없음)'}")


class PlaybackService:
    """High-level playback controller independent of UI frameworks."""

    def __init__(
        self,
        *,
        backend: InputBackend | None = None,
        focus_backend: FocusBackend | None = None,
        keymap: Keymap | None = None,
        on_snapshot: SnapshotCallback | None = None,
    ) -> None:
        self._backend: InputBackend = backend or Win32InputBackend()
        self._focus: FocusBackend = focus_backend or Win32FocusBackend()
        self._keymap_61 = keymap or Keymap(build_roblox_61_profile())
        self._keymap_88 = Keymap(build_roblox_88_profile())

        self._listeners: list[SnapshotCallback] = []
        if on_snapshot is not None:
            self._listeners.append(on_snapshot)

        self._scheduler = PlaybackScheduler(
            backend=self._backend,
            focus_backend=self._focus,
            on_snapshot=self._handle_snapshot,
        )

        self._current_song: SongData | None = None
        self._options = ArrangeOptions()
        self._pause_on_focus_loss = True
        self._countdown_sec = 3
        self._practice_mode = False
        self._last_snapshot = PlaybackSnapshot(
            state=PlaybackState.IDLE, position_ms=0, duration_ms=0
        )

    def add_listener(self, callback: SnapshotCallback) -> None:
        if callback not in self._listeners:
            self._listeners.append(callback)

    def remove_listener(self, callback: SnapshotCallback) -> None:
        if callback in self._listeners:
            self._listeners.remove(callback)

    @property
    def state(self) -> PlaybackState:
        return self._scheduler.state

    @property
    def current_song(self) -> SongData | None:
        return self._current_song

    @property
    def options(self) -> ArrangeOptions:
        return self._options

    @property
    def active_keymap(self) -> Keymap:
        if self._options.out_of_range == "keymap_88":
            return self._keymap_88
        return self._keymap_61

    def set_keymap(self, keymap: Keymap) -> None:
        self._keymap_61 = keymap

    def set_playback_behavior(
        self,
        *,
        pause_on_focus_loss: bool | None = None,
        countdown_sec: int | None = None,
        practice_mode: bool | None = None,
    ) -> None:
        if pause_on_focus_loss is not None:
            self._pause_on_focus_loss = pause_on_focus_loss
        if countdown_sec is not None:
            self._countdown_sec = max(0, min(10, countdown_sec))
        if practice_mode is not None:
            self._practice_mode = practice_mode

    def is_roblox_running(self) -> bool:
        return self._focus.is_roblox_running()

    def is_roblox_foreground(self) -> bool:
        return self._focus.is_roblox_foreground()

    def load_song(self, song: SongData, options: ArrangeOptions | None = None) -> None:
        self.stop()
        self._current_song = song
        if options is not None:
            self._options = options

    def recommend_transpose(self, song: SongData | None = None) -> tuple[int, int]:
        target = song or self._current_song
        if target is None:
            return (0, 0)
        return suggest_transpose(target.notes, self.active_keymap)

    def play(
        self,
        *,
        start_ms: int = 0,
        countdown_sec: int | None = None,
        activate_roblox: bool = True,
    ) -> None:
        if self._current_song is None:
            raise PlaybackError("재생할 곡이 선택되지 않았습니다.")
        actions = self._build_actions()
        cd = self._countdown_sec if countdown_sec is None else max(0, countdown_sec)
        self._scheduler.start(
            actions,
            duration_ms=self._current_song.duration_ms,
            speed=self._options.speed,
            start_song_ms=start_ms,
            countdown_sec=cd,
            pause_on_focus_loss=self._pause_on_focus_loss,
            dry_run=self._practice_mode,
            activate_roblox=activate_roblox and not self._practice_mode,
        )

    def pause(self) -> int:
        return self._scheduler.pause()

    def resume(self, *, countdown_sec: int = 0, activate_roblox: bool = True) -> None:
        if self._current_song is None:
            return
        pos = self._scheduler.position_ms
        self.play(start_ms=pos, countdown_sec=countdown_sec, activate_roblox=activate_roblox)

    def toggle_play_pause(self) -> None:
        state = self.state
        if state in (PlaybackState.PLAYING, PlaybackState.COUNTDOWN):
            self.pause()
        elif state == PlaybackState.PAUSED:
            self.resume()
        elif self._current_song is not None:
            self.play()

    def stop(self) -> None:
        self._scheduler.stop()

    def panic(self) -> None:
        """Immediately stop playback and force-release all keys."""
        self._scheduler.stop()
        self._backend.release_all()

    def seek(self, position_ms: int) -> None:
        if self._current_song is None:
            return
        clamped = max(0, min(position_ms, self._current_song.duration_ms))
        if self.state == PlaybackState.PLAYING:
            self.play(start_ms=clamped, countdown_sec=0, activate_roblox=False)
        else:
            self._scheduler.set_position_ms(clamped)
            self._handle_snapshot(
                PlaybackSnapshot(
                    state=self.state,
                    position_ms=clamped,
                    duration_ms=self._current_song.duration_ms,
                )
            )

    def set_speed(self, speed: float) -> None:
        clamped = max(0.25, min(2.0, round(speed, 2)))
        self.update_options(replace(self._options, speed=clamped))

    def set_transpose(self, semitones: int) -> None:
        clamped = max(-24, min(24, semitones))
        self.update_options(replace(self._options, transpose=clamped))

    def update_options(self, options: ArrangeOptions) -> None:
        was_playing = self.state == PlaybackState.PLAYING
        pos = self._scheduler.position_ms
        self._options = options
        if was_playing and self._current_song is not None:
            self.play(start_ms=pos, countdown_sec=0, activate_roblox=False)

    def preview_actions(self) -> list[KeyAction]:
        """Return arranged KeyActions for the currently loaded song and options."""
        if self._current_song is None:
            return []
        return self._build_actions()

    def _build_actions(self) -> list[KeyAction]:
        assert self._current_song is not None
        return arrange_song(self._current_song, self.active_keymap, self._options)

    def _handle_snapshot(self, snapshot: PlaybackSnapshot) -> None:
        self._last_snapshot = snapshot
        for cb in list(self._listeners):
            cb(snapshot)
