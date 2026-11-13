"""SQLite FTS5 + Hangul chosung search and filter/sort query builder."""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from ropi.domain.hangul import is_chosung_query
from ropi.library.models import SongQuery, SongRecord, SortField
from ropi.library.repository import hydrate_songs

_WORD_TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣ㄱ-ㅎㅏ-ㅣ]+")

_SORT_SQL: dict[SortField, tuple[str, str]] = {
    "title": ("s.title COLLATE NOCASE ASC, s.id ASC", "s.title COLLATE NOCASE DESC, s.id DESC"),
    "artist": (
        "s.artist COLLATE NOCASE ASC, s.title COLLATE NOCASE ASC",
        "s.artist COLLATE NOCASE DESC, s.title COLLATE NOCASE DESC",
    ),
    "duration_ms": (
        "COALESCE(s.duration_ms, 0) ASC, s.title COLLATE NOCASE ASC",
        "COALESCE(s.duration_ms, 0) DESC, s.title COLLATE NOCASE ASC",
    ),
    "bpm": (
        "COALESCE(s.bpm, 0) ASC, s.title COLLATE NOCASE ASC",
        "COALESCE(s.bpm, 0) DESC, s.title COLLATE NOCASE ASC",
    ),
    "created_at": ("s.created_at ASC, s.id ASC", "s.created_at DESC, s.id DESC"),
    "last_played_at": (
        "s.last_played_at IS NULL ASC, s.last_played_at ASC, s.id ASC",
        "s.last_played_at IS NULL ASC, s.last_played_at DESC, s.id DESC",
    ),
    "play_count": (
        "s.play_count ASC, s.title COLLATE NOCASE ASC",
        "s.play_count DESC, s.title COLLATE NOCASE ASC",
    ),
    "difficulty": (
        "s.difficulty IS NULL ASC, s.difficulty ASC, s.title COLLATE NOCASE ASC",
        "s.difficulty IS NULL ASC, s.difficulty DESC, s.title COLLATE NOCASE ASC",
    ),
    "relevance": ("s.title COLLATE NOCASE ASC, s.id ASC", "s.title COLLATE NOCASE DESC, s.id DESC"),
}


def build_fts5_match_expression(raw_query: str) -> str | None:
    """Convert raw user search text into a safe FTS5 MATCH query string."""
    stripped = raw_query.strip()
    if not stripped:
        return None
    tokens = _WORD_TOKEN_RE.findall(stripped)
    if not tokens:
        return None

    if is_chosung_query(stripped):
        return " ".join(f'chosung:"{tok}"*' for tok in tokens)
    return " ".join(f'"{tok}"*' for tok in tokens)


def search_songs(conn: sqlite3.Connection, query: SongQuery) -> list[SongRecord]:
    """Execute a parameterized FTS5 + metadata filter query against the library."""
    ctes: list[str] = []
    joins: list[str] = []
    wheres: list[str] = []
    params: list[Any] = []

    raw_text = query.text.strip()
    fts_expr = build_fts5_match_expression(raw_text)

    if fts_expr is not None:
        joins.append("JOIN song_fts ON song_fts.rowid = s.id")
        wheres.append("song_fts MATCH ?")
        params.append(fts_expr)
    elif raw_text:
        escaped = raw_text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        wheres.append("(s.title LIKE ? ESCAPE '\\' OR s.artist LIKE ? ESCAPE '\\')")
        like_pat = f"%{escaped}%"
        params.extend((like_pat, like_pat))

    if query.trash_only:
        wheres.append("s.deleted_at IS NOT NULL")
    elif not query.include_deleted:
        wheres.append("s.deleted_at IS NULL")

    if query.folder_id is not None:
        if query.include_subfolders:
            ctes.append(
                """
                _subfolders(id) AS (
                    SELECT ?
                    UNION ALL
                    SELECT f.id FROM folders f JOIN _subfolders sf ON f.parent_id = sf.id
                )
                """
            )
            # Prepend CTE param before main SELECT params cleanly below.
            wheres.append("s.folder_id IN (SELECT id FROM _subfolders)")
        else:
            wheres.append("s.folder_id = ?")
            params.append(query.folder_id)

    if query.tag:
        joins.append("JOIN song_tags _st ON _st.song_id = s.id")
        joins.append("JOIN tags _t ON _t.id = _st.tag_id")
        wheres.append("_t.name = ?")
        params.append(query.tag.strip())

    if query.favorite_only:
        wheres.append("s.favorite = 1")

    if query.format_filter:
        wheres.append("s.format = ?")
        params.append(query.format_filter.lower().lstrip("."))

    all_params: list[Any] = []
    if query.folder_id is not None and query.include_subfolders:
        all_params.append(query.folder_id)
    all_params.extend(params)

    cte_sql = f"WITH RECURSIVE {', '.join(ctes)} " if ctes else ""
    join_sql = " ".join(joins)
    where_sql = f"WHERE {' AND '.join(wheres)}" if wheres else ""

    if query.sort_by == "relevance" and fts_expr is not None:
        order_sql = "bm25(song_fts) ASC, s.title COLLATE NOCASE ASC"
    else:
        asc_sql, desc_sql = _SORT_SQL.get(query.sort_by, _SORT_SQL["title"])
        order_sql = desc_sql if query.sort_desc else asc_sql

    limit_sql = ""
    if query.limit is not None:
        limit_sql = "LIMIT ? OFFSET ?"
        all_params.extend((max(0, query.limit), max(0, query.offset)))
    elif query.offset > 0:
        limit_sql = "LIMIT -1 OFFSET ?"
        all_params.append(query.offset)

    sql = f"{cte_sql}SELECT DISTINCT s.* FROM songs s {join_sql} {where_sql} ORDER BY {order_sql} {limit_sql}"
    rows = conn.execute(sql, all_params).fetchall()
    return hydrate_songs(conn, rows)
