"""Virtual Piano text sheet parser (.txt) into SongData."""

from __future__ import annotations

import re
from pathlib import Path

from ropi.core.errors import FormatLoadError
from ropi.domain.keymap import Keymap, build_roblox_61_profile
from ropi.domain.note import NoteEvent, SongData, TrackInfo

_BPM_HEADER_RE = re.compile(r"^\s*(?:#|//)?\s*bpm\s*[:=]\s*(\d+(?:\.\d+)?)\s*$", re.IGNORECASE)
_TITLE_HEADER_RE = re.compile(r"^\s*(?:#|//)?\s*title\s*[:=]\s*(.+?)\s*$", re.IGNORECASE)


def parse_vp_sheet(
    text: str,
    *,
    title: str = "",
    bpm: float = 120.0,
    keymap: Keymap | None = None,
) -> SongData:
    """Parse Virtual Piano sheet text (`[tu] o p | a s ...`) into SongData.

    Timing rules:
    - Characters inside `[...]` form a simultaneous chord, advancing by 1 step after `]`.
    - Each standalone key character plays for 1 step (`60_000 / (bpm * 2)` ms, eighth-note grid).
    - Space, `|`, `-`, newline advance 1 step (rest/hold) without pressing a key; consecutive spaces collapse.
    - Optional metadata lines at the top: `# BPM: 140`, `# Title: Canon in D`.
    """
    km = keymap or Keymap(build_roblox_61_profile())
    effective_bpm = bpm
    effective_title = title
    body_lines: list[str] = []

    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            body_lines.append("")
            continue
        if m := _BPM_HEADER_RE.match(stripped):
            val = float(m.group(1))
            if 20.0 <= val <= 400.0:
                effective_bpm = val
            continue
        if m := _TITLE_HEADER_RE.match(stripped):
            if not effective_title:
                effective_title = m.group(1).strip()
            continue
        if stripped.startswith("#") or stripped.startswith("//"):
            continue
        body_lines.append(raw_line)

    content = "\n".join(body_lines)
    step_ms = max(40, round(60_000.0 / (effective_bpm * 2.0)))

    notes: list[NoteEvent] = []
    pos_ms = 0
    idx = 0
    n = len(content)

    while idx < n:
        ch = content[idx]
        if ch == "[":
            end = content.find("]", idx + 1)
            if end == -1:
                raise FormatLoadError("VP 악보의 '[' 괄호가 닫히지 않았습니다.")
            chord_body = content[idx + 1 : end]
            chord_notes = _resolve_chord_chars(chord_body, km)
            for midi_note in chord_notes:
                notes.append(
                    NoteEvent(
                        start_ms=pos_ms,
                        duration_ms=step_ms,
                        midi_note=midi_note,
                        velocity=80,
                        track=0,
                    )
                )
            if chord_notes:
                pos_ms += step_ms
            idx = end + 1
            continue

        if ch in {" ", "\t", "\r", "\n"}:
            # Collapse multiple whitespace characters into one rest step.
            while idx < n and content[idx] in {" ", "\t", "\r", "\n"}:
                idx += 1
            if notes:
                pos_ms += step_ms
            continue

        if ch in {"|", "-", "."}:
            pos_ms += step_ms
            idx += 1
            continue

        binding = km.resolve_vp_char(ch)
        if binding is not None:
            notes.append(
                NoteEvent(
                    start_ms=pos_ms,
                    duration_ms=step_ms,
                    midi_note=binding.midi_note,
                    velocity=80,
                    track=0,
                )
            )
            pos_ms += step_ms
        idx += 1

    if not notes:
        raise FormatLoadError("VP 악보에서 연주할 음표를 찾지 못했습니다.")

    duration_ms = max(n_ev.end_ms for n_ev in notes)
    min_note = min(n_ev.midi_note for n_ev in notes)
    max_note = max(n_ev.midi_note for n_ev in notes)
    return SongData(
        notes=tuple(notes),
        tracks=(TrackInfo(index=0, name="VP Sheet", channel=0, note_count=len(notes)),),
        duration_ms=duration_ms,
        bpm=effective_bpm,
        min_note=min_note,
        max_note=max_note,
        source_format="txt",
        title=effective_title,
    )


def load_vp_sheet_file(path: Path, *, bpm: float = 120.0, keymap: Keymap | None = None) -> SongData:
    """Read a UTF-8 VP sheet `.txt` file into SongData."""
    if not path.exists():
        raise FormatLoadError(f"VP 악보 파일을 찾을 수 없습니다: {path.name}")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise FormatLoadError(f"파일을 읽을 수 없습니다: {path.name}") from exc

    if b"\x00" in raw:
        raise FormatLoadError(f"텍스트 악보 형식이 아닙니다: {path.name}")

    for encoding in ("utf-8-sig", "utf-8", "cp949"):
        try:
            text = raw.decode(encoding)
            return parse_vp_sheet(text, title=path.stem, bpm=bpm, keymap=keymap)
        except UnicodeDecodeError:
            continue
    raise FormatLoadError(f"텍스트 인코딩을 해석할 수 없습니다: {path.name}")


def _resolve_chord_chars(chord_body: str, keymap: Keymap) -> list[int]:
    result: list[int] = []
    seen: set[int] = set()
    for ch in chord_body:
        if ch.isspace():
            continue
        binding = keymap.resolve_vp_char(ch)
        if binding is not None and binding.midi_note not in seen:
            seen.add(binding.midi_note)
            result.append(binding.midi_note)
    return result
