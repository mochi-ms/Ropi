"""Pure Python arrangement engine: transpose, range folding, speed, humanize -> KeyActions."""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from ropi.domain.keymap import MODIFIER_SCANCODES, KeyBinding, Keymap, Modifier
from ropi.domain.note import NoteEvent, SongData

OutOfRangeMode = Literal["fold", "drop", "keymap_88"]
SustainMode = Literal["tap", "hold", "pedal"]
ActionKind = Literal["down", "up"]

_KIND_ORDER: dict[ActionKind, int] = {"up": 0, "down": 1}
_MODIFIER_ORDER: dict[Modifier, int] = {"none": 0, "shift": 1, "ctrl": 2}
_MIN_REPRESS_GAP_NS = 8_000_000  # 8ms minimum release before re-striking same key
_MIN_HOLD_NS = 15_000_000  # 15ms minimum press duration


@dataclass(frozen=True, slots=True)
class ArrangeOptions:
    """User-configurable playback arrangement parameters."""

    speed: float = 1.0
    transpose: int = 0
    enabled_tracks: frozenset[int] | None = None
    out_of_range: OutOfRangeMode = "fold"
    sustain_mode: SustainMode = "tap"
    tap_duration_ms: int = 35
    humanize_timing_ms: int = 0
    humanize_roll_ms: int = 0
    seed: int | None = None

    def __post_init__(self) -> None:
        if not 0.25 <= self.speed <= 2.0:
            raise ValueError(f"speed must be between 0.25 and 2.0, got {self.speed}")
        if not -24 <= self.transpose <= 24:
            raise ValueError(f"transpose must be between -24 and 24, got {self.transpose}")
        if not 0 <= self.humanize_timing_ms <= 30:
            raise ValueError("humanize_timing_ms must be 0..30")
        if not 0 <= self.humanize_roll_ms <= 20:
            raise ValueError("humanize_roll_ms must be 0..20")


@dataclass(frozen=True, slots=True)
class KeyAction:
    """Single scheduled keyboard press or release event."""

    time_ns: int
    kind: ActionKind
    scancode: int
    modifier: Modifier
    midi_note: int
    label: str
    song_ms: int  # original unscaled position in ms for UI progress sync


def fold_note_to_range(midi_note: int, min_note: int, max_note: int) -> int:
    """Fold a MIDI note by octaves (+/-12) into [min_note, max_note] if possible."""
    if min_note > max_note:
        return midi_note
    note = midi_note
    while note < min_note and note + 12 <= max_note:
        note += 12
    while note > max_note and note - 12 >= min_note:
        note -= 12
    return note


def suggest_transpose(
    notes: Sequence[NoteEvent],
    keymap: Keymap,
    search_range: range = range(-24, 25),
) -> tuple[int, int]:
    """Recommend a semitone transpose offset that minimizes out-of-range notes.

    Returns (best_transpose, out_of_range_count).
    Tie-breaker prefers smaller absolute shift, then octave-aligned shifts.
    """
    if not notes:
        return (0, 0)

    min_n = keymap.min_note
    max_n = keymap.max_note
    best_shift = 0
    best_score: tuple[int, int, int] | None = None
    best_oob = len(notes)

    for shift in search_range:
        oob = 0
        for n in notes:
            shifted = n.midi_note + shift
            if shifted < min_n or shifted > max_n or keymap.resolve(shifted) is None:
                oob += 1
        # Prefer fewer OOB notes, smaller |shift|, and octave multiples on tie.
        score = (oob, abs(shift), 0 if shift % 12 == 0 else 1)
        if best_score is None or score < best_score:
            best_score = score
            best_shift = shift
            best_oob = oob

    return (best_shift, best_oob)


@dataclass(slots=True)
class _ScheduledNote:
    down_ns: int
    up_ns: int
    binding: KeyBinding
    song_ms: int


def arrange_song(
    song: SongData,
    keymap: Keymap,
    options: ArrangeOptions | None = None,
) -> list[KeyAction]:
    """Convert SongData into sorted KeyActions respecting range, speed, sustain, and humanize."""
    opts = options or ArrangeOptions()
    drum_tracks = {t.index for t in song.tracks if t.is_drum}

    filtered: list[NoteEvent] = []
    for note in song.notes:
        if opts.enabled_tracks is not None:
            if note.track not in opts.enabled_tracks:
                continue
        elif note.track in drum_tracks:
            continue
        filtered.append(note)

    if not filtered:
        return []

    rng = random.Random(opts.seed) if opts.seed is not None else random.Random()
    inv_speed = 1.0 / opts.speed

    # Group notes by original start_ms so chord roll humanization applies cleanly.
    by_start: dict[int, list[tuple[NoteEvent, KeyBinding]]] = defaultdict(list)
    for note in filtered:
        binding = _map_note(note.midi_note + opts.transpose, keymap, opts.out_of_range)
        if binding is not None:
            by_start[note.start_ms].append((note, binding))

    scheduled: list[_ScheduledNote] = []
    for orig_ms in sorted(by_start.keys()):
        chord = sorted(by_start[orig_ms], key=lambda pair: pair[1].midi_note)
        base_ns = round(orig_ms * inv_speed * 1_000_000)

        jitter_ns = 0
        if opts.humanize_timing_ms > 0:
            jitter_ns = rng.randint(-opts.humanize_timing_ms, opts.humanize_timing_ms) * 1_000_000

        chord_len = len(chord)
        for idx, (note, binding) in enumerate(chord):
            roll_ns = 0
            if opts.humanize_roll_ms > 0 and chord_len > 1:
                spread_ms = opts.humanize_roll_ms * (idx / (chord_len - 1))
                roll_ns = round(spread_ms * 1_000_000)

            down_ns = max(0, base_ns + jitter_ns + roll_ns)
            scaled_dur_ns = max(_MIN_HOLD_NS, round(note.duration_ms * inv_speed * 1_000_000))
            if opts.sustain_mode == "tap":
                tap_ns = max(_MIN_HOLD_NS, opts.tap_duration_ms * 1_000_000)
                dur_ns = min(scaled_dur_ns, tap_ns) if note.duration_ms > 0 else tap_ns
            else:
                dur_ns = scaled_dur_ns

            scheduled.append(
                _ScheduledNote(
                    down_ns=down_ns,
                    up_ns=down_ns + dur_ns,
                    binding=binding,
                    song_ms=orig_ms,
                )
            )

    # Deduplicate simultaneous identical scancodes and ensure clean release gaps before re-strikes.
    scheduled.sort(key=lambda item: (item.down_ns, item.binding.scancode))
    last_by_scancode: dict[int, _ScheduledNote] = {}
    cleaned: list[_ScheduledNote] = []

    for item in scheduled:
        sc = item.binding.scancode
        prev = last_by_scancode.get(sc)
        if prev is not None:
            if item.down_ns == prev.down_ns and item.binding.modifier == prev.binding.modifier:
                prev.up_ns = max(prev.up_ns, item.up_ns)
                continue
            if item.down_ns <= prev.down_ns + _MIN_HOLD_NS:
                item.down_ns = prev.down_ns + _MIN_HOLD_NS + _MIN_REPRESS_GAP_NS
                item.up_ns = max(item.up_ns, item.down_ns + _MIN_HOLD_NS)
            if prev.up_ns > item.down_ns - _MIN_REPRESS_GAP_NS:
                prev.up_ns = max(prev.down_ns + _MIN_HOLD_NS, item.down_ns - _MIN_REPRESS_GAP_NS)
        last_by_scancode[sc] = item
        cleaned.append(item)

    actions: list[KeyAction] = []
    for item in cleaned:
        actions.append(
            KeyAction(
                time_ns=item.down_ns,
                kind="down",
                scancode=item.binding.scancode,
                modifier=item.binding.modifier,
                midi_note=item.binding.midi_note,
                label=item.binding.key,
                song_ms=item.song_ms,
            )
        )
        actions.append(
            KeyAction(
                time_ns=item.up_ns,
                kind="up",
                scancode=item.binding.scancode,
                modifier=item.binding.modifier,
                midi_note=item.binding.midi_note,
                label=item.binding.key,
                song_ms=item.song_ms,
            )
        )

    if opts.sustain_mode == "pedal" and cleaned:
        _append_pedal_actions(cleaned, actions)

    actions.sort(
        key=lambda a: (
            a.time_ns,
            _KIND_ORDER[a.kind],
            _MODIFIER_ORDER[a.modifier],
            a.scancode,
        )
    )
    return actions


def _map_note(midi_note: int, keymap: Keymap, mode: OutOfRangeMode) -> KeyBinding | None:
    if mode == "drop":
        return keymap.resolve(midi_note)
    folded = fold_note_to_range(midi_note, keymap.min_note, keymap.max_note)
    return keymap.resolve(folded)


def _append_pedal_actions(scheduled: Sequence[_ScheduledNote], out: list[KeyAction]) -> None:
    """Emit sustain pedal (Space) down/up around musical phrases when pedal mode is active."""
    pedal_sc = MODIFIER_SCANCODES["space"]
    gap_threshold_ns = 350_000_000  # lift pedal during pauses > 350ms
    phrase_start = scheduled[0].down_ns
    phrase_end = scheduled[0].up_ns
    phrase_start_song_ms = scheduled[0].song_ms
    phrase_end_song_ms = scheduled[0].song_ms

    for item in scheduled[1:]:
        if item.down_ns - phrase_end > gap_threshold_ns:
            out.append(
                KeyAction(
                    time_ns=phrase_start,
                    kind="down",
                    scancode=pedal_sc,
                    modifier="none",
                    midi_note=-1,
                    label="Space",
                    song_ms=phrase_start_song_ms,
                )
            )
            out.append(
                KeyAction(
                    time_ns=phrase_end,
                    kind="up",
                    scancode=pedal_sc,
                    modifier="none",
                    midi_note=-1,
                    label="Space",
                    song_ms=phrase_end_song_ms,
                )
            )
            phrase_start = item.down_ns
            phrase_start_song_ms = item.song_ms
        if item.up_ns >= phrase_end:
            phrase_end = item.up_ns
            phrase_end_song_ms = item.song_ms

    out.append(
        KeyAction(
            time_ns=phrase_start,
            kind="down",
            scancode=pedal_sc,
            modifier="none",
            midi_note=-1,
            label="Space",
            song_ms=phrase_start_song_ms,
        )
    )
    out.append(
        KeyAction(
            time_ns=phrase_end,
            kind="up",
            scancode=pedal_sc,
            modifier="none",
            midi_note=-1,
            label="Space",
            song_ms=phrase_end_song_ms,
        )
    )
