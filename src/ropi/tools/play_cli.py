"""Phase 1 CLI test player for verifying Roblox piano keymaps and playback in-game."""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

from ropi.domain.arranger import ArrangeOptions, OutOfRangeMode, SustainMode
from ropi.domain.keymap import Keymap, build_roblox_61_profile, build_roblox_88_profile
from ropi.domain.note import NoteEvent, SongData, TrackInfo
from ropi.playback.scheduler import PlaybackSnapshot, PlaybackState
from ropi.playback.service import PlaybackService, load_song_file


def build_scale_song(mode: str, step_ms: int = 260) -> SongData:
    """Build a C major 2-octave scale or full 61/88 chromatic scale test SongData."""
    notes: list[NoteEvent] = []
    t = 0
    if mode == "cmajor":
        # C4..C6 ascending and descending
        intervals = [0, 2, 4, 5, 7, 9, 11, 12, 14, 16, 17, 19, 21, 23, 24]
        seq = [60 + i for i in intervals] + [60 + i for i in reversed(intervals[:-1])]
    elif mode == "chromatic61":
        seq = list(range(36, 97))
    elif mode == "chromatic88":
        seq = list(range(21, 109))
    else:
        raise ValueError(f"Unknown test pattern: {mode}")

    for midi_note in seq:
        notes.append(
            NoteEvent(
                start_ms=t,
                duration_ms=max(80, step_ms - 40),
                midi_note=midi_note,
                velocity=90,
                track=0,
            )
        )
        t += step_ms

    return SongData(
        notes=tuple(notes),
        tracks=(TrackInfo(index=0, name="Scale Test", channel=0, note_count=len(notes)),),
        duration_ms=t,
        bpm=120.0,
        min_note=min(seq),
        max_note=max(seq),
        source_format="test",
        title=f"Test ({mode})",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ropi Phase 1 CLI auto-player")
    parser.add_argument("file", nargs="?", type=Path, help="Path to .mid or .txt VP sheet")
    parser.add_argument(
        "--test-scale",
        choices=["cmajor", "chromatic61", "chromatic88"],
        help="Play a built-in scale test instead of a file",
    )
    parser.add_argument("--keymap", choices=["61", "88"], default="61")
    parser.add_argument(
        "--base-note", type=int, default=36, help="Base MIDI note for key '1' (default 36=C2)"
    )
    parser.add_argument("--transpose", type=int, default=0, help="Semitone transpose (-24..+24)")
    parser.add_argument("--auto-transpose", action="store_true", help="Use recommended transpose")
    parser.add_argument("--speed", type=float, default=1.0, help="Speed multiplier (0.25..2.0)")
    parser.add_argument("--range-mode", choices=["fold", "drop", "keymap_88"], default="fold")
    parser.add_argument("--sustain", choices=["tap", "hold", "pedal"], default="tap")
    parser.add_argument("--countdown", type=int, default=3, help="Countdown seconds before start")
    parser.add_argument(
        "--no-focus-pause", action="store_true", help="Do not pause if Roblox loses focus"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print actions without sending OS keystrokes"
    )
    args = parser.parse_args(argv)

    if args.file is None and args.test_scale is None:
        parser.error("Either a song file or --test-scale must be provided.")

    km61 = Keymap(build_roblox_61_profile(args.base_note))
    km88 = Keymap(build_roblox_88_profile(args.base_note))
    selected_km = km88 if args.keymap == "88" or args.range_mode == "keymap_88" else km61

    song = (
        build_scale_song(args.test_scale)
        if args.test_scale is not None
        else load_song_file(args.file, keymap=selected_km)  # type: ignore[arg-type]
    )

    done = threading.Event()
    started = False

    def on_snapshot(snap: PlaybackSnapshot) -> None:
        nonlocal started
        if snap.state in (PlaybackState.COUNTDOWN, PlaybackState.PLAYING):
            started = True
        if snap.state == PlaybackState.COUNTDOWN:
            print(f"[Ropi] Starting in {snap.countdown_sec}...", flush=True)
        elif snap.state == PlaybackState.PLAYING and snap.active_labels:
            print(
                f"\r[Playing] {snap.position_ms / 1000:6.2f}s / {snap.duration_ms / 1000:6.2f}s | keys: {' '.join(snap.active_labels):<16}",
                end="",
                flush=True,
            )
        elif snap.state == PlaybackState.PAUSED and snap.focus_paused:
            print("\n[Ropi] Paused: Roblox window lost focus.", flush=True)
            done.set()
        elif snap.state == PlaybackState.IDLE and started:
            if snap.error_message:
                print(f"\n[Ropi] Error: {snap.error_message}", file=sys.stderr, flush=True)
            else:
                print("\n[Ropi] Playback finished.", flush=True)
            done.set()

    service = PlaybackService(keymap=km61, on_snapshot=on_snapshot)
    range_mode: OutOfRangeMode = "keymap_88" if args.keymap == "88" else args.range_mode
    sustain_mode: SustainMode = args.sustain

    transpose = args.transpose
    if args.auto_transpose:
        rec, oob = service.recommend_transpose(song)
        transpose = rec
        print(f"[Ropi] Auto-transpose: {rec:+d} semitones (out-of-range notes: {oob})")

    opts = ArrangeOptions(
        speed=args.speed,
        transpose=transpose,
        out_of_range=range_mode,
        sustain_mode=sustain_mode,
    )
    service.set_playback_behavior(
        pause_on_focus_loss=not args.no_focus_pause,
        countdown_sec=args.countdown,
        practice_mode=args.dry_run,
    )
    service.load_song(song, opts)

    print(
        f"[Ropi] Loaded '{song.title}' | {song.note_count} notes | "
        f"{song.duration_ms / 1000:.1f}s | BPM {song.bpm} | Roblox running={service.is_roblox_running()}"
    )
    service.play(countdown_sec=args.countdown, activate_roblox=not args.dry_run)

    try:
        while not done.is_set():
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("\n[Ropi] Interrupted -> panic release!")
        service.panic()
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
