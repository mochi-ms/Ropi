"""Pure Python arrangement engine: transpose, range folding, speed, humanize -> KeyActions."""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from ropi.domain.keymap import MODIFIER_SCANCODES, KeyBinding, Keymap, Modifier
from ropi.domain.note import NoteEvent, PedalEvent, SongData

OutOfRangeMode = Literal["fold", "drop", "keymap_88"]
SustainMode = Literal["tap", "hold", "pedal"]
PedalStrategy = Literal["auto", "midi", "smart"]
ActionKind = Literal["down", "up"]

_KIND_ORDER: dict[ActionKind, int] = {"up": 0, "down": 1}
_MODIFIER_ORDER: dict[Modifier, int] = {"none": 0, "shift": 1, "ctrl": 2}
_MIN_REPRESS_GAP_NS = 8_000_000  # 8ms minimum release before re-striking same key
_MIN_HOLD_NS = 15_000_000  # 15ms minimum press duration
_PEDAL_LIFT_GAP_NS = 30_000_000  # >= 30ms release so Roblox registers Space InputEnded->InputBegan
_PEDAL_MIN_HOLD_NS = 25_000_000
_SMART_GAP_LIFT_NS = 300_000_000  # lift pedal during rests > 300ms
_SMART_MIN_CHANGE_NS = 380_000_000  # avoid rapid chattering within one chord
_SMART_MAX_SPAN_NS = 2_200_000_000  # force clean re-pedal before excess blur
_SLICE_WINDOW_NS = 25_000_000


@dataclass(frozen=True, slots=True)
class ArrangeOptions:
    """User-configurable playback arrangement parameters."""

    speed: float = 1.0
    transpose: int = 0
    enabled_tracks: frozenset[int] | None = None
    out_of_range: OutOfRangeMode = "fold"
    sustain_mode: SustainMode = "tap"
    pedal_strategy: PedalStrategy = "auto"
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
    end_song_ms: int


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
                    end_song_ms=orig_ms + note.duration_ms,
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
                song_ms=item.end_song_ms,
            )
        )

    if opts.sustain_mode == "pedal" and cleaned:
        _append_pedal_actions(
            scheduled=cleaned,
            midi_pedals=song.pedals,
            drum_tracks=drum_tracks,
            opts=opts,
            inv_speed=inv_speed,
            out=actions,
        )

    actions.sort(
        key=lambda a: (
            a.time_ns,
            _KIND_ORDER[a.kind],
            0 if a.midi_note < 0 else 1,
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


@dataclass(slots=True)
class _PedalSpan:
    down_ns: int
    up_ns: int
    song_start_ms: int
    song_end_ms: int


@dataclass(slots=True)
class _NoteSlice:
    start_ns: int
    end_ns: int
    min_midi: int
    pitch_classes: set[int]
    song_start_ms: int
    song_end_ms: int


def _append_pedal_actions(
    *,
    scheduled: Sequence[_ScheduledNote],
    midi_pedals: Sequence[PedalEvent],
    drum_tracks: set[int],
    opts: ArrangeOptions,
    inv_speed: float,
    out: list[KeyAction],
) -> None:
    """Emit sustain pedal (Space) down/up actions from MIDI CC64 or smart chord/phrase logic."""
    filtered_pedals: list[PedalEvent] = []
    for p in midi_pedals:
        if opts.enabled_tracks is not None:
            if p.track not in opts.enabled_tracks:
                continue
        elif p.track in drum_tracks:
            continue
        filtered_pedals.append(p)

    use_midi = opts.pedal_strategy == "midi" or (
        opts.pedal_strategy == "auto" and bool(filtered_pedals)
    )
    if use_midi:
        spans = _schedule_midi_pedal_spans(filtered_pedals, inv_speed)
    else:
        spans = _schedule_smart_pedal_spans(scheduled)

    _normalize_and_emit_pedal_spans(spans, out, speed=opts.speed)


def _schedule_midi_pedal_spans(pedals: Sequence[PedalEvent], inv_speed: float) -> list[_PedalSpan]:
    raw: list[_PedalSpan] = []
    for p in sorted(pedals, key=lambda item: (item.start_ms, item.end_ms)):
        down_ns = max(0, round(p.start_ms * inv_speed * 1_000_000))
        up_ns = max(down_ns, round(p.end_ms * inv_speed * 1_000_000))
        if up_ns - down_ns >= _PEDAL_MIN_HOLD_NS:
            raw.append(
                _PedalSpan(
                    down_ns=down_ns,
                    up_ns=up_ns,
                    song_start_ms=p.start_ms,
                    song_end_ms=p.end_ms,
                )
            )
    return raw


def _group_note_slices(scheduled: Sequence[_ScheduledNote]) -> list[_NoteSlice]:
    slices: list[_NoteSlice] = []
    for item in scheduled:
        pc = item.binding.midi_note % 12
        if slices and item.down_ns - slices[-1].start_ns <= _SLICE_WINDOW_NS:
            cur = slices[-1]
            cur.end_ns = max(cur.end_ns, item.up_ns)
            cur.min_midi = min(cur.min_midi, item.binding.midi_note)
            cur.pitch_classes.add(pc)
            cur.song_end_ms = max(cur.song_end_ms, item.song_ms)
        else:
            slices.append(
                _NoteSlice(
                    start_ns=item.down_ns,
                    end_ns=item.up_ns,
                    min_midi=item.binding.midi_note,
                    pitch_classes={pc},
                    song_start_ms=item.song_ms,
                    song_end_ms=item.song_ms,
                )
            )
    return slices


def _should_refresh_pedal(
    *,
    span_start_ns: int,
    span_bass: int,
    span_pcs: set[int],
    next_slice: _NoteSlice,
) -> bool:
    held_ns = next_slice.start_ns - span_start_ns
    if held_ns < _SMART_MIN_CHANGE_NS:
        return False
    if held_ns >= _SMART_MAX_SPAN_NS:
        return True

    bass_changed = (
        next_slice.min_midi != span_bass
        and (next_slice.min_midi <= 59 or next_slice.min_midi < span_bass + 12)
        and (abs(next_slice.min_midi - span_bass) % 12 != 0)
        and (
            next_slice.min_midi <= 55
            or len(next_slice.pitch_classes) >= 2
            or held_ns >= 700_000_000
        )
    )
    if bass_changed:
        return True

    combined_pcs = span_pcs | next_slice.pitch_classes
    if len(combined_pcs) >= 5:
        return True

    if held_ns >= 1_150_000_000:
        removed_old_pcs = span_pcs - next_slice.pitch_classes
        for new_pc in next_slice.pitch_classes:
            for old_pc in removed_old_pcs:
                if (new_pc - old_pc) % 12 in (1, 11):
                    return True

    return False


def _schedule_smart_pedal_spans(scheduled: Sequence[_ScheduledNote]) -> list[_PedalSpan]:
    slices = _group_note_slices(scheduled)
    if not slices:
        return []

    spans: list[_PedalSpan] = []
    first = slices[0]
    span_down_ns = first.start_ns
    span_end_ns = first.end_ns
    span_start_ms = first.song_start_ms
    span_end_ms = first.song_end_ms
    span_bass = first.min_midi
    span_pcs = set(first.pitch_classes)

    for sl in slices[1:]:
        gap_ns = sl.start_ns - span_end_ns
        if gap_ns > _SMART_GAP_LIFT_NS:
            lift_ns = min(span_end_ns + 40_000_000, sl.start_ns - _PEDAL_LIFT_GAP_NS)
            lift_ns = max(span_down_ns + _PEDAL_MIN_HOLD_NS, lift_ns)
            spans.append(
                _PedalSpan(
                    down_ns=span_down_ns,
                    up_ns=lift_ns,
                    song_start_ms=span_start_ms,
                    song_end_ms=span_end_ms,
                )
            )
            span_down_ns = sl.start_ns
            span_end_ns = sl.end_ns
            span_start_ms = sl.song_start_ms
            span_end_ms = sl.song_end_ms
            span_bass = sl.min_midi
            span_pcs = set(sl.pitch_classes)
            continue

        if _should_refresh_pedal(
            span_start_ns=span_down_ns,
            span_bass=span_bass,
            span_pcs=span_pcs,
            next_slice=sl,
        ):
            lift_ns = sl.start_ns - _PEDAL_LIFT_GAP_NS
            if lift_ns - span_down_ns >= _PEDAL_MIN_HOLD_NS:
                spans.append(
                    _PedalSpan(
                        down_ns=span_down_ns,
                        up_ns=lift_ns,
                        song_start_ms=span_start_ms,
                        song_end_ms=sl.song_start_ms,
                    )
                )
                span_down_ns = sl.start_ns
                span_end_ns = sl.end_ns
                span_start_ms = sl.song_start_ms
                span_end_ms = sl.song_end_ms
                span_bass = sl.min_midi
                span_pcs = set(sl.pitch_classes)
                continue

        span_end_ns = max(span_end_ns, sl.end_ns)
        span_end_ms = max(span_end_ms, sl.song_end_ms)
        span_pcs |= sl.pitch_classes
        if sl.start_ns - span_down_ns <= 120_000_000:
            span_bass = min(span_bass, sl.min_midi)

    spans.append(
        _PedalSpan(
            down_ns=span_down_ns,
            up_ns=max(span_down_ns + _PEDAL_MIN_HOLD_NS, span_end_ns),
            song_start_ms=span_start_ms,
            song_end_ms=span_end_ms,
        )
    )
    return spans


def _normalize_and_emit_pedal_spans(
    spans: Sequence[_PedalSpan], out: list[KeyAction], *, speed: float
) -> None:
    """Merge overlapping spans, enforce clean Space lift gaps, and emit KeyActions."""
    if not spans:
        return
    ordered = sorted(spans, key=lambda s: (s.down_ns, s.up_ns))
    normalized: list[_PedalSpan] = [
        _PedalSpan(
            down_ns=ordered[0].down_ns,
            up_ns=ordered[0].up_ns,
            song_start_ms=ordered[0].song_start_ms,
            song_end_ms=ordered[0].song_end_ms,
        )
    ]

    for cur in ordered[1:]:
        prev = normalized[-1]
        if cur.down_ns <= prev.up_ns:
            prev.up_ns = max(prev.up_ns, cur.up_ns)
            prev.song_end_ms = max(prev.song_end_ms, cur.song_end_ms)
            continue

        gap = cur.down_ns - prev.up_ns
        if gap < _PEDAL_LIFT_GAP_NS:
            trimmed_up = cur.down_ns - _PEDAL_LIFT_GAP_NS
            if trimmed_up - prev.down_ns >= _PEDAL_MIN_HOLD_NS:
                prev.up_ns = trimmed_up
            else:
                prev.up_ns = max(prev.up_ns, cur.up_ns)
                prev.song_end_ms = max(prev.song_end_ms, cur.song_end_ms)
                continue

        normalized.append(
            _PedalSpan(
                down_ns=cur.down_ns,
                up_ns=cur.up_ns,
                song_start_ms=cur.song_start_ms,
                song_end_ms=cur.song_end_ms,
            )
        )

    pedal_sc = MODIFIER_SCANCODES["space"]
    for span in normalized:
        out.append(
            KeyAction(
                time_ns=span.down_ns,
                kind="down",
                scancode=pedal_sc,
                modifier="none",
                midi_note=-1,
                label="Space",
                song_ms=span.song_start_ms,
            )
        )
        out.append(
            KeyAction(
                time_ns=span.up_ns,
                kind="up",
                scancode=pedal_sc,
                modifier="none",
                midi_note=-1,
                label="Space",
                song_ms=round((span.up_ns * speed) / 1_000_000),
            )
        )
