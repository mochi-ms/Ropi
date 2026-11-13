"""SQLite repository for folders, songs, tags, lyrics, playlists, and FTS5 indexing."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from ropi.core.errors import LibraryError
from ropi.domain.hangul import build_chosung_index
from ropi.formats.lrc import LyricLine
from ropi.library.db import Database
from ropi.library.models import (
    FolderRecord,
    NewSongInput,
    PlaylistRecord,
    SongPatch,
    SongRecord,
    TagRecord,
)


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _parse_settings(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return dict(parsed) if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def hydrate_songs(conn: sqlite3.Connection, rows: Sequence[sqlite3.Row]) -> list[SongRecord]:
    """Attach tags and lyric flags to raw `songs` rows in batched queries (no N+1)."""
    if not rows:
        return []
    song_ids = [int(r["id"]) for r in rows]
    placeholders = ",".join("?" for _ in song_ids)

    tags_by_song: dict[int, list[str]] = {sid: [] for sid in song_ids}
    tag_rows = conn.execute(
        f"""
        SELECT st.song_id, t.name
        FROM song_tags st
        JOIN tags t ON t.id = st.tag_id
        WHERE st.song_id IN ({placeholders})
        ORDER BY t.name COLLATE NOCASE
        """,
        song_ids,
    ).fetchall()
    for tr in tag_rows:
        tags_by_song[int(tr["song_id"])].append(str(tr["name"]))

    lyric_song_ids: set[int] = set()
    lyric_rows = conn.execute(
        f"SELECT DISTINCT song_id FROM lyrics WHERE song_id IN ({placeholders})",
        song_ids,
    ).fetchall()
    for lr in lyric_rows:
        lyric_song_ids.add(int(lr["song_id"]))

    return [
        SongRecord(
            id=int(r["id"]),
            folder_id=int(r["folder_id"]) if r["folder_id"] is not None else None,
            title=str(r["title"]),
            artist=str(r["artist"] or ""),
            memo=str(r["memo"] or ""),
            file_rel_path=str(r["file_rel_path"]),
            format=str(r["format"]),
            file_hash=str(r["file_hash"]),
            duration_ms=int(r["duration_ms"]) if r["duration_ms"] is not None else None,
            note_count=int(r["note_count"]) if r["note_count"] is not None else None,
            bpm=float(r["bpm"]) if r["bpm"] is not None else None,
            min_note=int(r["min_note"]) if r["min_note"] is not None else None,
            max_note=int(r["max_note"]) if r["max_note"] is not None else None,
            difficulty=int(r["difficulty"]) if r["difficulty"] is not None else None,
            favorite=bool(r["favorite"]),
            play_count=int(r["play_count"] or 0),
            last_played_at=str(r["last_played_at"]) if r["last_played_at"] else None,
            source_url=str(r["source_url"]) if r["source_url"] else None,
            source_engine=str(r["source_engine"]) if r["source_engine"] else None,
            settings=_parse_settings(r["settings_json"]),
            deleted_at=str(r["deleted_at"]) if r["deleted_at"] else None,
            created_at=str(r["created_at"]),
            updated_at=str(r["updated_at"]),
            tags=tuple(tags_by_song[int(r["id"])]),
            has_lyrics=int(r["id"]) in lyric_song_ids,
        )
        for r in rows
    ]


class LibraryRepository:
    """CRUD and transactional persistence over the Ropi SQLite schema."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------ Folders
    def list_folders(self) -> list[FolderRecord]:
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM folders ORDER BY sort_order ASC, name COLLATE NOCASE ASC"
            ).fetchall()
        return [self._row_to_folder(r) for r in rows]

    def get_folder(self, folder_id: int) -> FolderRecord | None:
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM folders WHERE id = ?", (folder_id,)).fetchone()
        return self._row_to_folder(row) if row is not None else None

    def create_folder(self, name: str, parent_id: int | None = None) -> FolderRecord:
        clean = name.strip()
        if not clean:
            raise LibraryError("폴더 이름은 비워둘 수 없습니다.")
        now = _utc_now_iso()
        with self.db.transaction() as tx:
            if parent_id is not None:
                parent_row = tx.execute(
                    "SELECT id FROM folders WHERE id = ?", (parent_id,)
                ).fetchone()
                if parent_row is None:
                    raise LibraryError(f"상위 폴더를 찾을 수 없습니다: id={parent_id}")
            sort_row = tx.execute(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 AS next_ord FROM folders WHERE parent_id IS ?",
                (parent_id,),
            ).fetchone()
            sort_order = int(sort_row["next_ord"]) if sort_row is not None else 0
            try:
                cur = tx.execute(
                    "INSERT INTO folders(parent_id, name, sort_order, created_at) VALUES (?, ?, ?, ?)",
                    (parent_id, clean, sort_order, now),
                )
            except sqlite3.IntegrityError as exc:
                raise LibraryError(
                    f"같은 위치에 동일한 이름의 폴더가 이미 있습니다: {clean}"
                ) from exc
            assert cur.lastrowid is not None
            row = tx.execute("SELECT * FROM folders WHERE id = ?", (cur.lastrowid,)).fetchone()
        assert row is not None
        return self._row_to_folder(row)

    def get_or_create_folder_path(self, parts: Sequence[str]) -> FolderRecord | None:
        """Ensure nested virtual folder path exists and return the leaf folder."""
        parent_id: int | None = None
        last_folder: FolderRecord | None = None
        for raw_part in parts:
            name = raw_part.strip()
            if not name:
                continue
            with self.db.transaction() as tx:
                if parent_id is None:
                    existing = tx.execute(
                        "SELECT * FROM folders WHERE parent_id IS NULL AND name = ?",
                        (name,),
                    ).fetchone()
                else:
                    existing = tx.execute(
                        "SELECT * FROM folders WHERE parent_id = ? AND name = ?",
                        (parent_id, name),
                    ).fetchone()
                if existing is not None:
                    last_folder = self._row_to_folder(existing)
                else:
                    sort_row = tx.execute(
                        "SELECT COALESCE(MAX(sort_order), -1) + 1 AS next_ord FROM folders WHERE parent_id IS ?",
                        (parent_id,),
                    ).fetchone()
                    sort_order = int(sort_row["next_ord"]) if sort_row is not None else 0
                    cur = tx.execute(
                        "INSERT INTO folders(parent_id, name, sort_order, created_at) VALUES (?, ?, ?, ?)",
                        (parent_id, name, sort_order, _utc_now_iso()),
                    )
                    assert cur.lastrowid is not None
                    created = tx.execute(
                        "SELECT * FROM folders WHERE id = ?", (cur.lastrowid,)
                    ).fetchone()
                    assert created is not None
                    last_folder = self._row_to_folder(created)
                parent_id = last_folder.id
        return last_folder

    def rename_folder(self, folder_id: int, new_name: str) -> FolderRecord:
        clean = new_name.strip()
        if not clean:
            raise LibraryError("폴더 이름은 비워둘 수 없습니다.")
        with self.db.transaction() as tx:
            try:
                cur = tx.execute("UPDATE folders SET name = ? WHERE id = ?", (clean, folder_id))
            except sqlite3.IntegrityError as exc:
                raise LibraryError(f"이미 같은 이름의 폴더가 있습니다: {clean}") from exc
            if cur.rowcount == 0:
                raise LibraryError(f"폴더를 찾을 수 없습니다: id={folder_id}")
            row = tx.execute("SELECT * FROM folders WHERE id = ?", (folder_id,)).fetchone()
        assert row is not None
        return self._row_to_folder(row)

    def move_folder(self, folder_id: int, new_parent_id: int | None) -> FolderRecord:
        if folder_id == new_parent_id:
            raise LibraryError("폴더를 자기 자신으로 이동할 수 없습니다.")
        with self.db.transaction() as tx:
            row = tx.execute("SELECT * FROM folders WHERE id = ?", (folder_id,)).fetchone()
            if row is None:
                raise LibraryError(f"폴더를 찾을 수 없습니다: id={folder_id}")
            if new_parent_id is not None:
                desc = tx.execute(
                    """
                    WITH RECURSIVE anc(id, parent_id) AS (
                        SELECT id, parent_id FROM folders WHERE id = ?
                        UNION ALL
                        SELECT f.id, f.parent_id FROM folders f JOIN anc ON f.id = anc.parent_id
                    )
                    SELECT 1 FROM anc WHERE id = ?
                    """,
                    (new_parent_id, folder_id),
                ).fetchone()
                if desc is not None:
                    raise LibraryError("하위 폴더 아래로 이동할 수 없습니다.")
                parent_exists = tx.execute(
                    "SELECT 1 FROM folders WHERE id = ?", (new_parent_id,)
                ).fetchone()
                if parent_exists is None:
                    raise LibraryError(f"대상 상위 폴더가 없습니다: id={new_parent_id}")
            try:
                tx.execute(
                    "UPDATE folders SET parent_id = ? WHERE id = ?", (new_parent_id, folder_id)
                )
            except sqlite3.IntegrityError as exc:
                raise LibraryError("대상 위치에 같은 이름의 폴더가 이미 있습니다.") from exc
            updated = tx.execute("SELECT * FROM folders WHERE id = ?", (folder_id,)).fetchone()
        assert updated is not None
        return self._row_to_folder(updated)

    def reorder_folders(self, ordered_ids: Sequence[int]) -> None:
        with self.db.transaction() as tx:
            for idx, fid in enumerate(ordered_ids):
                tx.execute("UPDATE folders SET sort_order = ? WHERE id = ?", (idx, fid))

    def delete_folder(self, folder_id: int) -> None:
        with self.db.transaction() as tx:
            tx.execute("DELETE FROM folders WHERE id = ?", (folder_id,))

    # -------------------------------------------------------------------- Songs
    def insert_song(
        self,
        item: NewSongInput,
        *,
        lyrics: Sequence[LyricLine] = (),
    ) -> SongRecord:
        now = _utc_now_iso()
        with self.db.transaction() as tx:
            song_id = self._insert_song_tx(tx, item, lyrics=lyrics, now=now)
            row = tx.execute("SELECT * FROM songs WHERE id = ?", (song_id,)).fetchone()
            assert row is not None
            return hydrate_songs(tx, [row])[0]

    def insert_songs_bulk(self, items: Sequence[NewSongInput]) -> list[int]:
        """Insert many songs inside one transaction (fast batch import & benchmarks)."""
        now = _utc_now_iso()
        ids: list[int] = []
        with self.db.transaction() as tx:
            for item in items:
                ids.append(self._insert_song_tx(tx, item, lyrics=(), now=now))
        return ids

    def _insert_song_tx(
        self,
        tx: sqlite3.Connection,
        item: NewSongInput,
        *,
        lyrics: Sequence[LyricLine],
        now: str,
    ) -> int:
        cur = tx.execute(
            """
            INSERT INTO songs(
                folder_id, title, artist, memo, file_rel_path, format, file_hash,
                duration_ms, note_count, bpm, min_note, max_note, difficulty,
                favorite, play_count, last_played_at, source_url, source_engine,
                settings_json, deleted_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?, ?, ?, NULL, ?, ?)
            """,
            (
                item.folder_id,
                item.title.strip() or "제목 없음",
                item.artist.strip(),
                item.memo,
                item.file_rel_path,
                item.format,
                item.file_hash,
                item.duration_ms,
                item.note_count,
                item.bpm,
                item.min_note,
                item.max_note,
                item.difficulty,
                1 if item.favorite else 0,
                item.source_url,
                item.source_engine,
                json.dumps(item.settings, ensure_ascii=False),
                now,
                now,
            ),
        )
        assert cur.lastrowid is not None
        song_id = int(cur.lastrowid)

        if item.tags:
            self._set_song_tags_tx(tx, song_id, item.tags)
        if lyrics:
            self._replace_lyrics_tx(tx, song_id, lyrics)
        self._reindex_song_fts_tx(tx, song_id)
        return song_id

    def get_song(self, song_id: int) -> SongRecord | None:
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM songs WHERE id = ?", (song_id,)).fetchone()
            if row is None:
                return None
            return hydrate_songs(conn, [row])[0]

    def find_by_hash(self, file_hash: str, *, include_deleted: bool = False) -> list[SongRecord]:
        with self.db.read() as conn:
            if include_deleted:
                rows = conn.execute(
                    "SELECT * FROM songs WHERE file_hash = ? ORDER BY id ASC", (file_hash,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM songs WHERE file_hash = ? AND deleted_at IS NULL ORDER BY id ASC",
                    (file_hash,),
                ).fetchall()
            return hydrate_songs(conn, rows)

    def update_song(self, song_id: int, patch: SongPatch) -> SongRecord:
        now = _utc_now_iso()
        with self.db.transaction() as tx:
            existing = tx.execute("SELECT * FROM songs WHERE id = ?", (song_id,)).fetchone()
            if existing is None:
                raise LibraryError(f"곡을 찾을 수 없습니다: id={song_id}")

            updates: list[str] = ["updated_at = ?"]
            params: list[Any] = [now]

            if patch.folder_id is not ...:
                updates.append("folder_id = ?")
                params.append(patch.folder_id)
            if patch.title is not None:
                updates.append("title = ?")
                params.append(patch.title.strip() or "제목 없음")
            if patch.artist is not None:
                updates.append("artist = ?")
                params.append(patch.artist.strip())
            if patch.memo is not None:
                updates.append("memo = ?")
                params.append(patch.memo)
            if patch.difficulty is not ...:
                updates.append("difficulty = ?")
                params.append(patch.difficulty)
            if patch.favorite is not None:
                updates.append("favorite = ?")
                params.append(1 if patch.favorite else 0)
            if patch.settings is not None:
                updates.append("settings_json = ?")
                params.append(json.dumps(patch.settings, ensure_ascii=False))

            params.append(song_id)
            tx.execute(f"UPDATE songs SET {', '.join(updates)} WHERE id = ?", params)

            if patch.tags is not None:
                self._set_song_tags_tx(tx, song_id, patch.tags)
            self._reindex_song_fts_tx(tx, song_id)

            row = tx.execute("SELECT * FROM songs WHERE id = ?", (song_id,)).fetchone()
            assert row is not None
            return hydrate_songs(tx, [row])[0]

    def update_song_file_location(
        self,
        song_id: int,
        *,
        file_rel_path: str,
        file_hash: str,
        duration_ms: int | None = None,
        note_count: int | None = None,
        bpm: float | None = None,
        min_note: int | None = None,
        max_note: int | None = None,
    ) -> SongRecord:
        now = _utc_now_iso()
        with self.db.transaction() as tx:
            cur = tx.execute(
                """
                UPDATE songs
                SET file_rel_path = ?, file_hash = ?,
                    duration_ms = COALESCE(?, duration_ms),
                    note_count = COALESCE(?, note_count),
                    bpm = COALESCE(?, bpm),
                    min_note = COALESCE(?, min_note),
                    max_note = COALESCE(?, max_note),
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    file_rel_path,
                    file_hash,
                    duration_ms,
                    note_count,
                    bpm,
                    min_note,
                    max_note,
                    now,
                    song_id,
                ),
            )
            if cur.rowcount == 0:
                raise LibraryError(f"곡을 찾을 수 없습니다: id={song_id}")
            row = tx.execute("SELECT * FROM songs WHERE id = ?", (song_id,)).fetchone()
            assert row is not None
            return hydrate_songs(tx, [row])[0]

    def record_play(self, song_id: int) -> None:
        now = _utc_now_iso()
        with self.db.transaction() as tx:
            tx.execute(
                """
                UPDATE songs
                SET play_count = play_count + 1, last_played_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (now, now, song_id),
            )

    def soft_delete_song(self, song_id: int, *, new_rel_path: str | None = None) -> SongRecord:
        now = _utc_now_iso()
        with self.db.transaction() as tx:
            if new_rel_path is not None:
                cur = tx.execute(
                    "UPDATE songs SET deleted_at = ?, file_rel_path = ?, updated_at = ? WHERE id = ?",
                    (now, new_rel_path, now, song_id),
                )
            else:
                cur = tx.execute(
                    "UPDATE songs SET deleted_at = ?, updated_at = ? WHERE id = ?",
                    (now, now, song_id),
                )
            if cur.rowcount == 0:
                raise LibraryError(f"곡을 찾을 수 없습니다: id={song_id}")
            row = tx.execute("SELECT * FROM songs WHERE id = ?", (song_id,)).fetchone()
            assert row is not None
            return hydrate_songs(tx, [row])[0]

    def restore_song(self, song_id: int, *, new_rel_path: str | None = None) -> SongRecord:
        now = _utc_now_iso()
        with self.db.transaction() as tx:
            if new_rel_path is not None:
                cur = tx.execute(
                    "UPDATE songs SET deleted_at = NULL, file_rel_path = ?, updated_at = ? WHERE id = ?",
                    (new_rel_path, now, song_id),
                )
            else:
                cur = tx.execute(
                    "UPDATE songs SET deleted_at = NULL, updated_at = ? WHERE id = ?",
                    (now, song_id),
                )
            if cur.rowcount == 0:
                raise LibraryError(f"곡을 찾을 수 없습니다: id={song_id}")
            row = tx.execute("SELECT * FROM songs WHERE id = ?", (song_id,)).fetchone()
            assert row is not None
            return hydrate_songs(tx, [row])[0]

    def hard_delete_song(self, song_id: int) -> SongRecord | None:
        with self.db.transaction() as tx:
            row = tx.execute("SELECT * FROM songs WHERE id = ?", (song_id,)).fetchone()
            if row is None:
                return None
            record = hydrate_songs(tx, [row])[0]
            tx.execute("DELETE FROM song_fts WHERE rowid = ?", (song_id,))
            tx.execute("DELETE FROM songs WHERE id = ?", (song_id,))
            return record

    def purge_expired_trash(self, *, older_than_days: int = 30) -> list[SongRecord]:
        cutoff = (datetime.now(UTC) - timedelta(days=older_than_days)).isoformat()
        with self.db.transaction() as tx:
            rows = tx.execute(
                "SELECT * FROM songs WHERE deleted_at IS NOT NULL AND deleted_at <= ?",
                (cutoff,),
            ).fetchall()
            if not rows:
                return []
            records = hydrate_songs(tx, rows)
            for rec in records:
                tx.execute("DELETE FROM song_fts WHERE rowid = ?", (rec.id,))
                tx.execute("DELETE FROM songs WHERE id = ?", (rec.id,))
            return records

    def list_active_songs_light(self) -> list[SongRecord]:
        with self.db.read() as conn:
            rows = conn.execute("SELECT * FROM songs WHERE deleted_at IS NULL").fetchall()
            return hydrate_songs(conn, rows)

    # --------------------------------------------------------------------- Tags
    def list_tags(self) -> list[TagRecord]:
        with self.db.read() as conn:
            rows = conn.execute("SELECT * FROM tags ORDER BY name COLLATE NOCASE ASC").fetchall()
        return [
            TagRecord(
                id=int(r["id"]),
                name=str(r["name"]),
                color=str(r["color"]) if r["color"] else None,
            )
            for r in rows
        ]

    def set_song_tags(self, song_id: int, tag_names: Sequence[str]) -> tuple[str, ...]:
        with self.db.transaction() as tx:
            row = tx.execute("SELECT id FROM songs WHERE id = ?", (song_id,)).fetchone()
            if row is None:
                raise LibraryError(f"곡을 찾을 수 없습니다: id={song_id}")
            tags = self._set_song_tags_tx(tx, song_id, tag_names)
            self._reindex_song_fts_tx(tx, song_id)
            return tags

    def _set_song_tags_tx(
        self, tx: sqlite3.Connection, song_id: int, tag_names: Sequence[str]
    ) -> tuple[str, ...]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in tag_names:
            name = raw.strip()
            key = name.casefold()
            if name and key not in seen:
                seen.add(key)
                cleaned.append(name)

        tx.execute("DELETE FROM song_tags WHERE song_id = ?", (song_id,))
        for name in cleaned:
            tx.execute("INSERT OR IGNORE INTO tags(name) VALUES (?)", (name,))
            tag_row = tx.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
            assert tag_row is not None
            tx.execute(
                "INSERT OR IGNORE INTO song_tags(song_id, tag_id) VALUES (?, ?)",
                (song_id, int(tag_row["id"])),
            )
        return tuple(cleaned)

    # ------------------------------------------------------------------- Lyrics
    def get_lyrics(self, song_id: int) -> list[LyricLine]:
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT line_no, time_ms, text FROM lyrics WHERE song_id = ? ORDER BY line_no ASC",
                (song_id,),
            ).fetchall()
        return [
            LyricLine(
                line_no=int(r["line_no"]),
                time_ms=int(r["time_ms"]) if r["time_ms"] is not None else None,
                text=str(r["text"]),
            )
            for r in rows
        ]

    def replace_lyrics(self, song_id: int, lines: Sequence[LyricLine]) -> None:
        with self.db.transaction() as tx:
            row = tx.execute("SELECT id FROM songs WHERE id = ?", (song_id,)).fetchone()
            if row is None:
                raise LibraryError(f"곡을 찾을 수 없습니다: id={song_id}")
            self._replace_lyrics_tx(tx, song_id, lines)
            self._reindex_song_fts_tx(tx, song_id)

    def _replace_lyrics_tx(
        self, tx: sqlite3.Connection, song_id: int, lines: Sequence[LyricLine]
    ) -> None:
        tx.execute("DELETE FROM lyrics WHERE song_id = ?", (song_id,))
        if lines:
            tx.executemany(
                "INSERT INTO lyrics(song_id, line_no, time_ms, text) VALUES (?, ?, ?, ?)",
                [(song_id, idx, ln.time_ms, ln.text) for idx, ln in enumerate(lines)],
            )

    # ---------------------------------------------------------------- Playlists
    def list_playlists(self) -> list[PlaylistRecord]:
        with self.db.read() as conn:
            rows = conn.execute(
                """
                SELECT p.id, p.name, p.created_at, COUNT(pi.song_id) AS cnt
                FROM playlists p
                LEFT JOIN playlist_items pi ON pi.playlist_id = p.id
                GROUP BY p.id
                ORDER BY p.created_at ASC, p.id ASC
                """
            ).fetchall()
        return [
            PlaylistRecord(
                id=int(r["id"]),
                name=str(r["name"]),
                created_at=str(r["created_at"]),
                song_count=int(r["cnt"]),
            )
            for r in rows
        ]

    def create_playlist(self, name: str) -> PlaylistRecord:
        clean = name.strip()
        if not clean:
            raise LibraryError("재생목록 이름은 비워둘 수 없습니다.")
        now = _utc_now_iso()
        with self.db.transaction() as tx:
            cur = tx.execute(
                "INSERT INTO playlists(name, created_at) VALUES (?, ?)",
                (clean, now),
            )
            assert cur.lastrowid is not None
            return PlaylistRecord(
                id=int(cur.lastrowid),
                name=clean,
                created_at=now,
                song_count=0,
            )

    def rename_playlist(self, playlist_id: int, new_name: str) -> None:
        clean = new_name.strip()
        if not clean:
            raise LibraryError("재생목록 이름은 비워둘 수 없습니다.")
        with self.db.transaction() as tx:
            cur = tx.execute("UPDATE playlists SET name = ? WHERE id = ?", (clean, playlist_id))
            if cur.rowcount == 0:
                raise LibraryError(f"재생목록을 찾을 수 없습니다: id={playlist_id}")

    def delete_playlist(self, playlist_id: int) -> None:
        with self.db.transaction() as tx:
            tx.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))

    def get_playlist_songs(self, playlist_id: int) -> list[SongRecord]:
        with self.db.read() as conn:
            rows = conn.execute(
                """
                SELECT s.*
                FROM playlist_items pi
                JOIN songs s ON s.id = pi.song_id
                WHERE pi.playlist_id = ? AND s.deleted_at IS NULL
                ORDER BY pi.position ASC
                """,
                (playlist_id,),
            ).fetchall()
            return hydrate_songs(conn, rows)

    def set_playlist_items(self, playlist_id: int, song_ids: Sequence[int]) -> None:
        with self.db.transaction() as tx:
            exists = tx.execute("SELECT 1 FROM playlists WHERE id = ?", (playlist_id,)).fetchone()
            if exists is None:
                raise LibraryError(f"재생목록을 찾을 수 없습니다: id={playlist_id}")
            tx.execute("DELETE FROM playlist_items WHERE playlist_id = ?", (playlist_id,))
            if song_ids:
                tx.executemany(
                    "INSERT INTO playlist_items(playlist_id, song_id, position) VALUES (?, ?, ?)",
                    [(playlist_id, sid, pos) for pos, sid in enumerate(song_ids)],
                )

    # ------------------------------------------------------------ Convert Jobs
    def insert_convert_job(self, source: str, engine: str, status: str = "queued") -> int:
        now = _utc_now_iso()
        with self.db.transaction() as tx:
            cur = tx.execute(
                """
                INSERT INTO convert_jobs(source, engine, status, progress, error, song_id, created_at)
                VALUES (?, ?, ?, 0.0, NULL, NULL, ?)
                """,
                (source, engine, status, now),
            )
            assert cur.lastrowid is not None
            return int(cur.lastrowid)

    def update_convert_job(
        self,
        job_id: int,
        *,
        status: str | None = None,
        progress: float | None = None,
        error: str | None = None,
        song_id: int | None = None,
    ) -> None:
        with self.db.transaction() as tx:
            tx.execute(
                """
                UPDATE convert_jobs
                SET status = COALESCE(?, status),
                    progress = COALESCE(?, progress),
                    error = COALESCE(?, error),
                    song_id = COALESCE(?, song_id)
                WHERE id = ?
                """,
                (status, progress, error, song_id, job_id),
            )

    # --------------------------------------------------------------------- FTS5
    def _reindex_song_fts_tx(self, tx: sqlite3.Connection, song_id: int) -> None:
        song_row = tx.execute("SELECT title, artist FROM songs WHERE id = ?", (song_id,)).fetchone()
        if song_row is None:
            return
        title = str(song_row["title"] or "")
        artist = str(song_row["artist"] or "")

        tag_rows = tx.execute(
            """
            SELECT t.name
            FROM song_tags st JOIN tags t ON t.id = st.tag_id
            WHERE st.song_id = ?
            """,
            (song_id,),
        ).fetchall()
        tags_text = " ".join(str(r["name"]) for r in tag_rows)

        lyric_rows = tx.execute(
            "SELECT text FROM lyrics WHERE song_id = ? ORDER BY line_no ASC", (song_id,)
        ).fetchall()
        lyrics_text = " ".join(str(r["text"]) for r in lyric_rows if r["text"])

        chosung_text = build_chosung_index(title, artist, tags_text)

        tx.execute("DELETE FROM song_fts WHERE rowid = ?", (song_id,))
        tx.execute(
            """
            INSERT INTO song_fts(rowid, title, artist, tags, lyrics, chosung)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (song_id, title, artist, tags_text, lyrics_text, chosung_text),
        )

    @staticmethod
    def _row_to_folder(row: sqlite3.Row) -> FolderRecord:
        return FolderRecord(
            id=int(row["id"]),
            parent_id=int(row["parent_id"]) if row["parent_id"] is not None else None,
            name=str(row["name"]),
            sort_order=int(row["sort_order"] or 0),
            created_at=str(row["created_at"]),
        )
