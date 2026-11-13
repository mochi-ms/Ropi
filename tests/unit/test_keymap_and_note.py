"""Unit tests for NoteEvent, SongData, Keymap, and Roblox 61/88 profiles."""

from __future__ import annotations

from pathlib import Path

import pytest

from ropi.domain.keymap import (
    Keymap,
    KeymapProfile,
    build_roblox_61_profile,
    build_roblox_88_profile,
)
from ropi.domain.note import NoteEvent, SongData, TrackInfo, is_black_key, midi_note_name


def test_midi_note_helpers() -> None:
    assert midi_note_name(21) == "A0"
    assert midi_note_name(36) == "C2"
    assert midi_note_name(60) == "C4"
    assert midi_note_name(61) == "C#4"
    assert midi_note_name(108) == "C8"
    assert is_black_key(61) is True
    assert is_black_key(60) is False
    with pytest.raises(ValueError):
        midi_note_name(128)


def test_note_event_validation() -> None:
    ev = NoteEvent(start_ms=100, duration_ms=250, midi_note=60, velocity=90, track=1)
    assert ev.end_ms == 350
    with pytest.raises(ValueError):
        NoteEvent(start_ms=-1, duration_ms=10, midi_note=60)
    with pytest.raises(ValueError):
        NoteEvent(start_ms=0, duration_ms=10, midi_note=200)
    with pytest.raises(ValueError):
        NoteEvent(start_ms=0, duration_ms=10, midi_note=60, velocity=128)


def test_roblox_61_keymap_all_notes_accurate() -> None:
    profile = build_roblox_61_profile()
    assert len(profile.bindings) == 61
    assert profile.min_midi_note == 36
    assert profile.max_midi_note == 96

    km = Keymap(profile)
    for midi_note in range(36, 97):
        binding = km.resolve(midi_note)
        assert binding is not None
        expected_shift = is_black_key(midi_note)
        assert (binding.modifier == "shift") is expected_shift
        assert binding.scancode > 0

    # Specific landmarks from Roblox Virtual Piano
    assert km.resolve(36) is not None and km.resolve(36).key == "1"  # type: ignore[union-attr]
    assert km.resolve(60) is not None and km.resolve(60).key == "t"  # type: ignore[union-attr]
    assert km.resolve(61) is not None and km.resolve(61).key == "T"  # type: ignore[union-attr]
    assert km.resolve(96) is not None and km.resolve(96).key == "m"  # type: ignore[union-attr]
    assert km.resolve(35) is None
    assert km.resolve(97) is None


def test_roblox_88_keymap_all_notes_accurate() -> None:
    profile = build_roblox_88_profile()
    assert len(profile.bindings) == 88
    assert profile.min_midi_note == 21
    assert profile.max_midi_note == 108

    km = Keymap(profile)
    for midi_note in range(21, 109):
        binding = km.resolve(midi_note)
        assert binding is not None
        if midi_note < 36 or midi_note > 96:
            assert binding.modifier == "ctrl"
            assert binding.key.startswith("^")

    assert km.resolve(21) is not None and km.resolve(21).key == "^1"  # type: ignore[union-attr]
    assert km.resolve(35) is not None and km.resolve(35).key == "^t"  # type: ignore[union-attr]
    assert km.resolve(97) is not None and km.resolve(97).key == "^y"  # type: ignore[union-attr]
    assert km.resolve(108) is not None and km.resolve(108).key == "^j"  # type: ignore[union-attr]


def test_keymap_json_assets_and_base_shift(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    km61 = Keymap.from_json_file(repo_root / "assets" / "keymaps" / "roblox_61.json")
    km88 = Keymap.from_json_file(repo_root / "assets" / "keymaps" / "roblox_88.json")
    assert len(km61.all_bindings()) == 61
    assert len(km88.all_bindings()) == 88

    shifted = km61.with_base_midi_note(24)
    assert shifted.min_note == 24
    assert shifted.resolve(24) is not None and shifted.resolve(24).key == "1"  # type: ignore[union-attr]

    out_path = tmp_path / "custom.json"
    shifted.to_json_file(out_path)
    reloaded = Keymap.from_json_file(out_path)
    assert reloaded.min_note == 24

    # Duplicate midi_note validation fails
    dup = km61.profile.model_dump()
    dup["bindings"].append(dup["bindings"][0])
    with pytest.raises(ValueError):
        KeymapProfile.model_validate(dup)


def test_song_data_helpers() -> None:
    song = SongData(
        notes=(
            NoteEvent(0, 100, 30),
            NoteEvent(100, 100, 60),
            NoteEvent(200, 100, 100),
        ),
        tracks=(
            TrackInfo(0, "Piano", 0, 3, is_drum=False),
            TrackInfo(1, "Drums", 9, 0, is_drum=True),
        ),
        duration_ms=300,
        bpm=120.0,
        min_note=30,
        max_note=100,
    )
    assert song.note_count == 3
    assert len(song.non_drum_tracks) == 1
    assert song.count_out_of_range(36, 96) == 2
