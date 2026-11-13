"""Unit tests for Hangul chosung extraction, LRC parsing/serialization, and .ropi format."""

from __future__ import annotations

import zipfile
from pathlib import Path

import mido
import pytest

from ropi.core.errors import FormatLoadError
from ropi.domain.hangul import (
    build_chosung_index,
    extract_chosung,
    is_chosung_query,
    is_chosung_token,
)
from ropi.formats.lrc import LyricLine, format_lrc, load_lrc_file, parse_lrc
from ropi.formats.ropi_format import RopiManifest, export_ropi, load_ropi
from ropi.playback.service import load_song_file


def _make_midi_bytes(notes: list[int] | None = None) -> bytes:
    if notes is None:
        notes = [60, 64, 67]
    mid = mido.MidiFile(ticks_per_beat=480)
    track = mido.MidiTrack()
    mid.tracks.append(track)
    track.append(mido.MetaMessage("set_tempo", tempo=500_000, time=0))
    for n in notes:
        track.append(mido.Message("note_on", note=n, velocity=80, time=0))
        track.append(mido.Message("note_off", note=n, velocity=0, time=240))
    track.append(mido.MetaMessage("end_of_track", time=0))
    import io

    buf = io.BytesIO()
    mid.save(file=buf)
    return buf.getvalue()


def test_hangul_chosung_extraction_and_detection() -> None:
    assert extract_chosung("아리랑 변주곡") == "ㅇㄹㄹ ㅂㅈㄱ"
    assert extract_chosung("쇼팽 Nocturne No.2") == "ㅅㅍ Nocturne No2"
    assert extract_chosung("ㄲㅊ ㅎ") == "ㄲㅊ ㅎ"

    assert is_chosung_token("ㅇㄹㄹ") is True
    assert is_chosung_token("아리랑") is False
    assert is_chosung_token("") is False

    assert is_chosung_query("ㅇㄹㄹ") is True
    assert is_chosung_query("ㅇㄹㄹ ㅂㅈㄱ") is True
    assert is_chosung_query("ㅇㄹㄹ piano") is False
    assert is_chosung_query("   ") is False

    idx = build_chosung_index("아리랑 변주곡", "Chopin")
    assert "ㅇㄹㄹ ㅂㅈㄱ" in idx
    assert "ㅇㄹㄹㅂㅈㄱ" in idx
    assert "Chopin" in idx


def test_lrc_parse_multitag_offset_plain_and_roundtrip(tmp_path: Path) -> None:
    raw = """
    [ti:Arirang]
    [ar:Traditional]
    [offset:500]
    [00:01.00]First line
    [00:05.50][00:12.250]Chorus line
    [00:99.00]Invalid seconds ignored
    Untimed note
    """
    lines = parse_lrc(raw)
    texts = [(line.time_ms, line.text) for line in lines]
    assert (1500, "First line") in texts
    assert (6000, "Chorus line") in texts
    assert (12750, "Chorus line") in texts
    assert (None, "Untimed note") in texts
    assert all("Invalid seconds" not in line.text for line in lines)

    # Plain-only text
    plain_lines = parse_lrc("Line A\n\nLine B\n")
    assert [line.text for line in plain_lines] == ["Line A", "Line B"]
    assert all(line.time_ms is None for line in plain_lines)

    # Offset clamping >= 0
    neg = parse_lrc("[offset:-5000]\n[00:01.00]Clamped")
    assert neg[0].time_ms == 0

    # Serialization roundtrip
    serialized = format_lrc(
        [
            LyricLine(line_no=0, time_ms=1500, text="Hello"),
            LyricLine(line_no=1, time_ms=None, text="Tail"),
        ],
        title="Sample",
        artist="Tester",
    )
    assert "[ti:Sample]" in serialized
    assert "[00:01.50]Hello" in serialized
    assert "Tail" in serialized

    # File encoding UTF-8 and CP949 fallback
    utf8_file = tmp_path / "utf8.lrc"
    utf8_file.write_text("[00:02.00]안녕하세요\n", encoding="utf-8-sig")
    assert load_lrc_file(utf8_file)[0].text == "안녕하세요"

    cp949_file = tmp_path / "cp949.lrc"
    cp949_file.write_bytes("[00:03.00]한글 가사\n".encode("cp949"))
    assert load_lrc_file(cp949_file)[0].text == "한글 가사"

    binary_file = tmp_path / "bad.lrc"
    binary_file.write_bytes(b"\x00\x01\x02\xff\xfe")
    with pytest.raises(FormatLoadError):
        load_lrc_file(binary_file)


def test_ropi_archive_roundtrip_mid_and_txt_and_validation(tmp_path: Path) -> None:
    midi_bytes = _make_midi_bytes()
    manifest = RopiManifest(
        title="녹턴 Op.9 No.2",
        artist="쇼팽",
        memo="페달 추천",
        difficulty=4,
        tags=("클래식", "쇼팽"),
        settings={"speed": 1.0, "pedal_strategy": "smart"},
        source_url="https://example.invalid/nocturne",
        source_engine="unit-test",
        music_filename="music.mid",
        music_format="mid",
    )
    lyrics = [
        LyricLine(line_no=0, time_ms=1000, text="Intro"),
        LyricLine(line_no=1, time_ms=3000, text="Theme"),
    ]
    ropi_mid = tmp_path / "nocturne.ropi"
    export_ropi(ropi_mid, manifest=manifest, music_bytes=midi_bytes, lyrics=lyrics)

    loaded = load_ropi(ropi_mid)
    assert loaded.manifest.title == "녹턴 Op.9 No.2"
    assert loaded.manifest.artist == "쇼팽"
    assert loaded.manifest.difficulty == 4
    assert loaded.manifest.tags == ("클래식", "쇼팽")
    assert loaded.manifest.settings["pedal_strategy"] == "smart"
    assert len(loaded.lyrics) == 2
    assert loaded.lyrics[1].text == "Theme"
    assert loaded.song.note_count == 3

    # Integrated loader via playback.service.load_song_file
    direct_song = load_song_file(ropi_mid)
    assert direct_song.note_count == 3

    # Embedded VP text sheet inside .ropi
    txt_manifest = RopiManifest(
        title="Twinkle VP",
        music_filename="sheet.txt",
        music_format="txt",
    )
    ropi_txt = tmp_path / "twinkle.ropi"
    export_ropi(ropi_txt, manifest=txt_manifest, music_bytes=b"t t o o p p o", lyrics=())
    loaded_txt = load_ropi(ropi_txt)
    assert loaded_txt.manifest.music_format == "txt"
    assert loaded_txt.song.note_count == 7

    # Error handling: missing, corrupt zip, missing manifest, bad format, missing music
    with pytest.raises(FormatLoadError):
        load_ropi(tmp_path / "missing.ropi")

    corrupt = tmp_path / "corrupt.ropi"
    corrupt.write_bytes(b"not-a-zip")
    with pytest.raises(FormatLoadError):
        load_ropi(corrupt)

    no_manifest = tmp_path / "no_manifest.ropi"
    with zipfile.ZipFile(no_manifest, "w") as zf:
        zf.writestr("music.mid", midi_bytes)
    with pytest.raises(FormatLoadError):
        load_ropi(no_manifest)

    wrong_fmt = tmp_path / "wrong_fmt.ropi"
    with zipfile.ZipFile(wrong_fmt, "w") as zf:
        zf.writestr("manifest.json", '{"format":"other"}')
    with pytest.raises(FormatLoadError):
        load_ropi(wrong_fmt)

    no_music = tmp_path / "no_music.ropi"
    with zipfile.ZipFile(no_music, "w") as zf:
        zf.writestr("manifest.json", '{"format":"ropi","title":"X"}')
    with pytest.raises(FormatLoadError):
        load_ropi(no_music)
