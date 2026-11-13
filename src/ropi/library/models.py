"""Typed dataclasses and query objects for the Ropi song library."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

SortField = Literal[
    "title",
    "artist",
    "duration_ms",
    "bpm",
    "created_at",
    "last_played_at",
    "play_count",
    "difficulty",
    "relevance",
]


@dataclass(frozen=True, slots=True)
class FolderRecord:
    """Virtual library folder row."""

    id: int
    parent_id: int | None
    name: str
    sort_order: int
    created_at: str


@dataclass(frozen=True, slots=True)
class SongRecord:
    """Song metadata row joined with tags and lyric count."""

    id: int
    folder_id: int | None
    title: str
    artist: str
    memo: str
    file_rel_path: str
    format: str
    file_hash: str
    duration_ms: int | None
    note_count: int | None
    bpm: float | None
    min_note: int | None
    max_note: int | None
    difficulty: int | None
    favorite: bool
    play_count: int
    last_played_at: str | None
    source_url: str | None
    source_engine: str | None
    settings: dict[str, Any]
    deleted_at: str | None
    created_at: str
    updated_at: str
    tags: tuple[str, ...] = ()
    has_lyrics: bool = False
    is_missing: bool = False


@dataclass(frozen=True, slots=True)
class NewSongInput:
    """Payload to insert a new song into the SQLite repository."""

    folder_id: int | None
    title: str
    file_rel_path: str
    format: str
    file_hash: str
    artist: str = ""
    memo: str = ""
    duration_ms: int | None = None
    note_count: int | None = None
    bpm: float | None = None
    min_note: int | None = None
    max_note: int | None = None
    difficulty: int | None = None
    favorite: bool = False
    source_url: str | None = None
    source_engine: str | None = None
    settings: dict[str, Any] = field(default_factory=dict)
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SongPatch:
    """Partial update fields for `LibraryRepository.update_song`."""

    folder_id: int | object | None = ...
    title: str | None = None
    artist: str | None = None
    memo: str | None = None
    difficulty: int | object | None = ...
    favorite: bool | None = None
    settings: dict[str, Any] | None = None
    tags: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class TagRecord:
    """Library tag row with optional display color."""

    id: int
    name: str
    color: str | None = None


@dataclass(frozen=True, slots=True)
class PlaylistRecord:
    """User-defined playlist header row."""

    id: int
    name: str
    created_at: str
    song_count: int = 0


@dataclass(frozen=True, slots=True)
class SongQuery:
    """Filter, search, and sort parameters for querying the song library."""

    text: str = ""
    folder_id: int | None = None
    include_subfolders: bool = True
    tag: str | None = None
    favorite_only: bool = False
    format_filter: str | None = None
    trash_only: bool = False
    include_deleted: bool = False
    sort_by: SortField = "title"
    sort_desc: bool = False
    limit: int | None = None
    offset: int = 0
