"""Unit tests for the musical arrangement engine."""

from __future__ import annotations

from ropi.domain.arranger import (
    ArrangeOptions,
    arrange_song,
    fold_note_to_range,
    suggest_transpose,
)
from ropi.domain.keymap import Keymap, build_roblox_61_profile, build_roblox_88_profile
from ropi.domain.note import NoteEvent, SongData, TrackInfo


def _make_song(notes: list[NoteEvent], *, include_drum: bool = False) -> SongData:
    tracks = [TrackInfo(0, "Piano", 0, len(notes), is_drum=False)]
    if include_drum:
        tracks.append(TrackInfo(1, "Drums", 9, 1, is_drum=True))
    dur = max((n.end_ms for n in notes), default=0)
    return SongData(
        notes=tuple(notes),
        tracks=tuple(tracks),
        duration_ms=dur,
        bpm=120.0,
        min_note=min((n.midi_note for n in notes), default=None),
        max_note=max((n.midi_note for n in notes), default=None),
    )


def test_fold_and_suggest_transpose() -> None:
    km61 = Keymap(build_roblox_61_profile())  # 36..96
    assert fold_note_to_range(24, 36, 96) == 36
    assert fold_note_to_range(108, 36, 96) == 96
    assert fold_note_to_range(60, 36, 96) == 60

    # Notes shifted down by 12 so optimal transpose is +12 (0 out-of-range)
    low_notes = [NoteEvent(i * 100, 80, 24 + i) for i in range(6)]
    rec, oob = suggest_transpose(low_notes, km61)
    assert rec == 12
    assert oob == 0


def test_out_of_range_modes_and_drum_exclusion() -> None:
    km61 = Keymap(build_roblox_61_profile())
    km88 = Keymap(build_roblox_88_profile())
    notes = [
        NoteEvent(0, 100, 24, track=0),  # below 36 -> C1
        NoteEvent(100, 100, 60, track=0),  # C4
        NoteEvent(200, 100, 36, track=1),  # drum track
    ]
    song = _make_song(notes, include_drum=True)

    folded = arrange_song(song, km61, ArrangeOptions(out_of_range="fold"))
    assert [a.midi_note for a in folded if a.kind == "down"] == [36, 60]

    dropped = arrange_song(song, km61, ArrangeOptions(out_of_range="drop"))
    assert [a.midi_note for a in dropped if a.kind == "down"] == [60]

    full_88 = arrange_song(song, km88, ArrangeOptions(out_of_range="keymap_88"))
    assert [a.midi_note for a in full_88 if a.kind == "down"] == [24, 60]


def test_chord_ordering_white_before_shift_and_speed() -> None:
    km61 = Keymap(build_roblox_61_profile())
    # C minor chord at t=1000ms: C4(60, none), D#4(63, shift), G4(67, none)
    song = _make_song(
        [
            NoteEvent(1000, 200, 63),
            NoteEvent(1000, 200, 67),
            NoteEvent(1000, 200, 60),
        ]
    )
    actions = arrange_song(song, km61, ArrangeOptions(speed=2.0, sustain_mode="hold"))
    downs = [a for a in actions if a.kind == "down"]
    ups = [a for a in actions if a.kind == "up"]

    assert [a.time_ns for a in downs] == [500_000_000, 500_000_000, 500_000_000]
    assert [a.time_ns for a in ups] == [600_000_000, 600_000_000, 600_000_000]
    # White keys ("none") precede black key ("shift") at exact same timestamp
    assert [a.modifier for a in downs] == ["none", "none", "shift"]


def test_repress_gap_and_pedal_and_humanize() -> None:
    km61 = Keymap(build_roblox_61_profile())
    # Overlapping same note -> prior UP must happen before second DOWN
    song = _make_song(
        [
            NoteEvent(0, 500, 60),
            NoteEvent(100, 100, 60),
        ]
    )
    actions = arrange_song(song, km61, ArrangeOptions(sustain_mode="hold"))
    assert len(actions) == 4
    assert actions[0].kind == "down"
    assert actions[1].kind == "up"
    assert actions[2].kind == "down"
    assert actions[1].time_ns < actions[2].time_ns

    # Pedal mode adds Space down/up
    pedal_actions = arrange_song(song, km61, ArrangeOptions(sustain_mode="pedal"))
    assert any(a.label == "Space" and a.kind == "down" for a in pedal_actions)
    assert any(a.label == "Space" and a.kind == "up" for a in pedal_actions)

    # Humanize with deterministic seed spreads chord and shifts timing
    chord_song = _make_song([NoteEvent(500, 100, 60), NoteEvent(500, 100, 64)])
    hum_actions = arrange_song(
        chord_song,
        km61,
        ArrangeOptions(humanize_timing_ms=10, humanize_roll_ms=12, seed=7),
    )
    hum_downs = [a for a in hum_actions if a.kind == "down"]
    assert hum_downs[1].time_ns - hum_downs[0].time_ns == 12_000_000
