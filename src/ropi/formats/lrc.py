"""LRC timestamped and plain-text lyrics parser/serializer (Qt/Win32 independent)."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ropi.core.errors import FormatLoadError

_TAG_PATTERN = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")
_META_PATTERN = re.compile(r"^\[(ti|ar|al|by|offset|length|re|ve):(.*)\]$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class LyricLine:
    """Single line of song lyrics with optional timestamp in milliseconds."""

    line_no: int
    time_ms: int | None
    text: str

    def __post_init__(self) -> None:
        if self.line_no < 0:
            raise ValueError(f"line_no must be >= 0, got {self.line_no}")
        if self.time_ms is not None and self.time_ms < 0:
            raise ValueError(f"time_ms must be >= 0, got {self.time_ms}")


def _parse_frac_ms(frac: str | None) -> int:
    if not frac:
        return 0
    if len(frac) == 1:
        return int(frac) * 100
    if len(frac) == 2:
        return int(frac) * 10
    return int(frac[:3])


def parse_lrc(raw_text: str) -> list[LyricLine]:
    """Parse LRC or plain-text lyrics into normalized `LyricLine` items."""
    offset_ms = 0
    timed_entries: list[tuple[int, int, str]] = []
    plain_entries: list[tuple[int, str]] = []
    order = 0

    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        meta_match = _META_PATTERN.match(line)
        if meta_match:
            key = meta_match.group(1).lower()
            val = meta_match.group(2).strip()
            if key == "offset":
                try:
                    offset_ms = int(val)
                except ValueError:
                    offset_ms = 0
            continue

        tags = list(_TAG_PATTERN.finditer(line))
        if tags:
            lyric_text = _TAG_PATTERN.sub("", line).strip()
            for match in tags:
                minutes = int(match.group(1))
                seconds = int(match.group(2))
                if seconds >= 60:
                    continue
                ms = (minutes * 60 + seconds) * 1000 + _parse_frac_ms(match.group(3))
                timed_entries.append((max(0, ms + offset_ms), order, lyric_text))
                order += 1
        else:
            plain_entries.append((order, line))
            order += 1

    if timed_entries and not plain_entries:
        timed_entries.sort(key=lambda item: (item[0], item[1]))
        return [
            LyricLine(line_no=idx, time_ms=t_ms, text=txt)
            for idx, (t_ms, _ord, txt) in enumerate(timed_entries)
        ]

    if plain_entries and not timed_entries:
        return [
            LyricLine(line_no=idx, time_ms=None, text=txt)
            for idx, (_o, txt) in enumerate(plain_entries)
        ]

    # Mixed file: keep timed sorted entries, then untimed lines in order.
    combined: list[tuple[int | None, int, str]] = [
        (t_ms, ord_i, txt) for t_ms, ord_i, txt in timed_entries
    ]
    combined.extend((None, ord_i, txt) for ord_i, txt in plain_entries)
    combined.sort(key=lambda item: (0 if item[0] is not None else 1, item[0] or 0, item[1]))
    return [
        LyricLine(line_no=idx, time_ms=t_ms, text=txt)
        for idx, (t_ms, _ord, txt) in enumerate(combined)
    ]


def load_lrc_file(path: Path) -> list[LyricLine]:
    """Load an LRC or plain lyrics `.txt` file with encoding fallback."""
    if not path.exists():
        raise FormatLoadError(f"가사 파일을 찾을 수 없습니다: {path.name}")
    raw = path.read_bytes()
    if b"\x00" in raw[:1024]:
        raise FormatLoadError(f"올바른 텍스트 가사 파일이 아닙니다: {path.name}")
    text: str | None = None
    for encoding in ("utf-8-sig", "utf-8", "cp949"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise FormatLoadError(f"가사 파일 인코딩을 해석할 수 없습니다: {path.name}")
    return parse_lrc(text)


def format_lrc(
    lines: Sequence[LyricLine],
    *,
    title: str = "",
    artist: str = "",
) -> str:
    """Serialize `LyricLine` items back into standard LRC text."""
    out: list[str] = []
    if title:
        out.append(f"[ti:{title}]")
    if artist:
        out.append(f"[ar:{artist}]")

    ordered = sorted(
        lines, key=lambda ln: (0 if ln.time_ms is not None else 1, ln.time_ms or 0, ln.line_no)
    )
    for ln in ordered:
        if ln.time_ms is None:
            out.append(ln.text)
        else:
            total_cs = ln.time_ms // 10
            cs = total_cs % 100
            total_sec = total_cs // 100
            sec = total_sec % 60
            minutes = total_sec // 60
            out.append(f"[{minutes:02d}:{sec:02d}.{cs:02d}]{ln.text}")
    return "\n".join(out) + ("\n" if out else "")
