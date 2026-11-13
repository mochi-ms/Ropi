"""Unit and fuzz tests for MIDI and VP sheet format loaders."""

from __future__ import annotations

import random
from pathlib import Path

import mido
import pytest

from ropi.core.errors import FormatLoadError
from ropi.formats.midi_loader import load_midi
from ropi.formats.vp_sheet import load_vp_sheet_file, parse_vp_sheet


def _write_temp_midi(path: Path, build_fn: object) -> Path:
    mid = mido.MidiFile(ticks_per_beat=480)
    build_fn(mid)  # type: ignore[operator]
    mid.save(str(path))
    return path


def test_midi_tempo_changes_and_multitrack(tmp_path: Path) -> None:
    def build(mid: mido.MidiFile) -> None:
        conductor = mido.MidiTrack()
        conductor.append(mido.MetaMessage("track_name", name="Tempo", time=0))
        # Beat 0: 120 BPM (500_000 us/beat) -> 1 beat = 500 ms
        conductor.append(mido.MetaMessage("set_tempo", tempo=500_000, time=0))
        # Beat 2 (960 ticks): switch to 60 BPM (1_000_000 us/beat) -> 1 beat = 1000 ms
        conductor.append(mido.MetaMessage("set_tempo", tempo=1_000_000, time=960))
        mid.tracks.append(conductor)

        piano = mido.MidiTrack()
        piano.append(mido.MetaMessage("track_name", name="Piano", time=0))
        # Note at tick 0 -> 0 ms, duration 480 ticks (500 ms at 120 BPM)
        piano.append(mido.Message("note_on", channel=0, note=60, velocity=90, time=0))
        piano.append(mido.Message("note_on", channel=0, note=60, velocity=0, time=480))
        # Note at tick 960 -> 1000 ms, duration 480 ticks (1000 ms at 60 BPM!)
        piano.append(mido.Message("note_on", channel=0, note=64, velocity=80, time=480))
        piano.append(mido.Message("note_off", channel=0, note=64, velocity=0, time=480))
        mid.tracks.append(piano)

        drums = mido.MidiTrack()
        drums.append(mido.MetaMessage("track_name", name="Drums", time=0))
        drums.append(mido.Message("note_on", channel=9, note=36, velocity=100, time=0))
        drums.append(mido.Message("note_off", channel=9, note=36, velocity=0, time=240))
        mid.tracks.append(drums)

    midi_path = _write_temp_midi(tmp_path / "tempo_test.mid", build)
    song = load_midi(midi_path)

    piano_notes = [n for n in song.notes if n.track == 1]
    assert len(piano_notes) == 2
    assert piano_notes[0].start_ms == 0
    assert piano_notes[0].duration_ms == 500
    assert piano_notes[1].start_ms == 1000
    assert piano_notes[1].duration_ms == 1000

    drum_tracks = [t for t in song.tracks if t.is_drum]
    assert len(drum_tracks) == 1
    assert drum_tracks[0].channel == 9

    no_drums = load_midi(midi_path, include_drums=False)
    assert len(no_drums.notes) == 2


def test_midi_overlapping_same_pitch_and_dangling_notes(tmp_path: Path) -> None:
    def build(mid: mido.MidiFile) -> None:
        tr = mido.MidiTrack()
        tr.append(mido.Message("note_on", note=60, velocity=80, time=0))
        tr.append(mido.Message("note_on", note=60, velocity=80, time=240))
        tr.append(mido.Message("note_off", note=60, velocity=0, time=240))  # closes first
        # second note left dangling until end of track at tick 960
        tr.append(mido.MetaMessage("end_of_track", time=480))
        mid.tracks.append(tr)

    midi_path = _write_temp_midi(tmp_path / "overlap.mid", build)
    song = load_midi(midi_path)
    assert len(song.notes) == 2
    assert song.notes[0].start_ms == 0
    assert song.notes[0].duration_ms == 500
    assert song.notes[1].start_ms == 250
    assert song.notes[1].duration_ms == 750


def test_midi_zero_length_note(tmp_path: Path) -> None:
    def build(mid: mido.MidiFile) -> None:
        tr = mido.MidiTrack()
        tr.append(mido.Message("note_on", note=62, velocity=75, time=0))
        tr.append(mido.Message("note_off", note=62, velocity=0, time=0))
        mid.tracks.append(tr)

    song = load_midi(_write_temp_midi(tmp_path / "zero.mid", build))
    assert len(song.notes) == 1
    assert song.notes[0].duration_ms == 0


def test_corrupt_midi_fuzz_50_cases(tmp_path: Path) -> None:
    rng = random.Random(42)
    valid_header = b"MThd\x00\x00\x00\x06\x00\x01\x00\x01\x01\xe0MTrk\x00\x00\x00\x04"

    cases: list[bytes] = [
        b"",
        b"not a midi file at all",
        b"MThd",
        b"MThd\x00\x00\x00\x06\x00\x01\x00\x01\x00\x00",  # zero ticks_per_beat
        b"MThd\x00\x00\x00\x06\x00\x01\x00\x05\x01\xe0",  # missing tracks
        valid_header + b"\xff\xff\xff\xff",
        valid_header + b"\x00\x90\x3c",  # truncated event
        b"MThd\x00\x00\x00\x10\xff\xff\xff\xff\xff\xff",
        b"RIFF\x00\x00\x00\x00WAVEfmt ",
        b"\x00" * 128,
    ]
    for i in range(40):
        length = rng.randint(1, 256)
        payload = bytes(rng.getrandbits(8) for _ in range(length))
        if i % 2 == 0:
            payload = valid_header[: rng.randint(4, len(valid_header))] + payload
        cases.append(payload)

    assert len(cases) == 50
    for idx, data in enumerate(cases):
        bad_file = tmp_path / f"corrupt_{idx}.mid"
        bad_file.write_bytes(data)
        with pytest.raises(FormatLoadError):
            load_midi(bad_file)


def test_midi_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FormatLoadError):
        load_midi(tmp_path / "does_not_exist.mid")


def test_vp_sheet_parsing_and_errors(tmp_path: Path) -> None:
    sheet = """
    # Title: Test Melody
    # BPM: 150
    [tu] o p | a - s
    """
    song = parse_vp_sheet(sheet)
    assert song.title == "Test Melody"
    assert song.bpm == 150.0
    # [tu] -> 2 simultaneous notes at 0ms (t=60, u=64), then o, p, rest, a, rest, s
    assert song.notes[0].start_ms == 0
    assert song.notes[1].start_ms == 0
    assert {song.notes[0].midi_note, song.notes[1].midi_note} == {60, 64}
    assert len(song.notes) == 6

    txt_file = tmp_path / "melody.txt"
    txt_file.write_text("[ty] u i", encoding="utf-8")
    loaded = load_vp_sheet_file(txt_file)
    assert loaded.title == "melody"
    assert loaded.note_count == 4

    with pytest.raises(FormatLoadError):
        parse_vp_sheet("[tu unclosed")

    with pytest.raises(FormatLoadError):
        parse_vp_sheet("   | -   ")

    bin_file = tmp_path / "binary.txt"
    bin_file.write_bytes(b"\x00\x01\x02")
    with pytest.raises(FormatLoadError):
        load_vp_sheet_file(bin_file)
