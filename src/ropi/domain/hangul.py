"""Pure Python Hangul chosung (initial consonant) extraction helpers for fast FTS5 search."""

from __future__ import annotations

_HANGUL_BASE = 0xAC00
_HANGUL_END = 0xD7A3
_CHOSUNG_BLOCK = 21 * 28  # 588 syllables per initial consonant

CHOSUNG_LIST: tuple[str, ...] = (
    "ㄱ",
    "ㄲ",
    "ㄴ",
    "ㄷ",
    "ㄸ",
    "ㄹ",
    "ㅁ",
    "ㅂ",
    "ㅃ",
    "ㅅ",
    "ㅆ",
    "ㅇ",
    "ㅈ",
    "ㅉ",
    "ㅊ",
    "ㅋ",
    "ㅌ",
    "ㅍ",
    "ㅎ",
)
CHOSUNG_SET: frozenset[str] = frozenset(CHOSUNG_LIST)


def extract_chosung(text: str) -> str:
    """Convert Hangul syllables in `text` to their initial consonants, keeping ASCII/spaces."""
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if _HANGUL_BASE <= code <= _HANGUL_END:
            out.append(CHOSUNG_LIST[(code - _HANGUL_BASE) // _CHOSUNG_BLOCK])
        elif ch in CHOSUNG_SET or ch.isalnum() or ch.isspace():
            out.append(ch)
    return "".join(out)


def is_chosung_token(token: str) -> bool:
    """Return True if non-empty `token` consists solely of Hangul chosung consonants."""
    return bool(token) and all(ch in CHOSUNG_SET for ch in token)


def is_chosung_query(query: str) -> bool:
    """Return True if stripped query has at least one char and all non-space chars are chosung."""
    compact = "".join(query.split())
    return is_chosung_token(compact)


def build_chosung_index(*texts: str) -> str:
    """Build FTS5-friendly chosung index string (spaced + space-collapsed tokens per field)."""
    parts: list[str] = []
    for text in texts:
        if not text:
            continue
        raw = " ".join(extract_chosung(text).split())
        if not raw:
            continue
        parts.append(raw)
        collapsed = "".join(raw.split())
        if collapsed != raw:
            parts.append(collapsed)
    return " ".join(parts)
