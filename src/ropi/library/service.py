"""High-level LibraryService orchestrating DB, FileStore, Search, Import/Export, and Backup."""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from ropi.core.errors import FormatLoadError, LibraryError
from ropi.domain.keymap import Keymap
from ropi.domain.note import SongData
from ropi.formats.lrc import LyricLine, format_lrc, load_lrc_file
from ropi.formats.ropi_format import RopiManifest, export_ropi, load_ropi
from ropi.library.backup import export_library_archive, restore_library_archive
from ropi.library.db import Database
from ropi.library.file_store import LibraryFileStore, compute_sha256
from ropi.library.models import (
    FolderRecord,
    NewSongInput,
    PlaylistRecord,
    SongPatch,
    SongQuery,
    SongRecord,
    TagRecord,
)
from ropi.library.repository import LibraryRepository
from ropi.library.search import search_songs
from ropi.playback.service import load_song_file

logger = logging.getLogger(__name__)

SUPPORTED_SONG_EXTENSIONS: frozenset[str] = frozenset({".mid", ".midi", ".txt", ".ropi"})
DuplicatePolicy = Literal["skip", "copy", "ask"]
ImportProgressCallback = Callable[[int, int, Path], None]


@dataclass(frozen=True, slots=True)
class ImportSummary:
    """Result report returned after importing files or folders."""

    imported: list[SongRecord] = field(default_factory=list)
    skipped_duplicates: list[Path] = field(default_factory=list)
    duplicate_matches: list[tuple[Path, SongRecord]] = field(default_factory=list)
    errors: list[tuple[Path, str]] = field(default_factory=list)
    cancelled: bool = False


class LibraryService:
    """Application-layer service managing songs, folders, search, trash, and backups."""

    def __init__(self, db: Database, file_store: LibraryFileStore) -> None:
        self.db = db
        self.file_store = file_store
        self.repo = LibraryRepository(db)

    def initialize(self, *, auto_purge_days: int = 30) -> int:
        """Initialize database, create startup backup, apply migrations, and purge expired trash."""
        self.file_store.ensure_dirs()
        version = self.db.initialize(backup_before_migrate=True)
        self.purge_expired_trash(days=auto_purge_days)
        return version

    # ------------------------------------------------------------------- Query
    def search(self, query: SongQuery | None = None) -> list[SongRecord]:
        with self.db.read() as conn:
            return search_songs(conn, query or SongQuery())

    def get_song(self, song_id: int) -> SongRecord | None:
        rec = self.repo.get_song(song_id)
        if rec is None:
            return None
        missing = not self.file_store.exists(rec.file_rel_path)
        return replace(rec, is_missing=missing)

    def load_song_data(self, song: SongRecord, *, keymap: Keymap | None = None) -> SongData:
        path = self.file_store.resolve_path(song.file_rel_path)
        if not path.is_file():
            raise LibraryError(f"곡 파일을 찾을 수 없습니다 (누락됨): {song.title}")
        return load_song_file(path, keymap=keymap)

    def resolve_song_path(self, song: SongRecord) -> Path:
        return self.file_store.resolve_path(song.file_rel_path)

    # ------------------------------------------------------------- Import Batch
    def import_folder(
        self,
        root_dir: Path,
        *,
        folder_id: int | None = None,
        preserve_subfolders: bool = True,
        duplicate_policy: DuplicatePolicy = "skip",
        keymap: Keymap | None = None,
        on_progress: ImportProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
    ) -> ImportSummary:
        """Recursively scan `root_dir` and import all supported song files."""
        if not root_dir.is_dir():
            raise LibraryError(f"가져올 폴더를 찾을 수 없습니다: {root_dir}")

        files = sorted(
            p
            for p in root_dir.rglob("*")
            if p.is_file() and p.suffix.lower() in SUPPORTED_SONG_EXTENSIONS
        )
        return self.import_files(
            files,
            folder_id=folder_id,
            base_dir=root_dir,
            preserve_subfolders=preserve_subfolders,
            duplicate_policy=duplicate_policy,
            keymap=keymap,
            on_progress=on_progress,
            cancel_event=cancel_event,
        )

    def import_files(
        self,
        paths: Sequence[Path],
        *,
        folder_id: int | None = None,
        base_dir: Path | None = None,
        preserve_subfolders: bool = False,
        duplicate_policy: DuplicatePolicy = "skip",
        keymap: Keymap | None = None,
        on_progress: ImportProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
    ) -> ImportSummary:
        """Import song files (.mid, .midi, .txt, .ropi) without blocking or mutating originals."""
        summary = ImportSummary()
        resolved_files: list[Path] = []
        for p in paths:
            if p.is_dir():
                resolved_files.extend(
                    sorted(
                        sub
                        for sub in p.rglob("*")
                        if sub.is_file() and sub.suffix.lower() in SUPPORTED_SONG_EXTENSIONS
                    )
                )
            elif p.is_file():
                resolved_files.append(p)

        total = len(resolved_files)
        for idx, src_path in enumerate(resolved_files):
            if cancel_event is not None and cancel_event.is_set():
                return replace(summary, cancelled=True)
            if on_progress is not None:
                on_progress(idx + 1, total, src_path)

            ext = src_path.suffix.lower()
            if ext not in SUPPORTED_SONG_EXTENSIONS:
                summary.errors.append((src_path, f"지원하지 않는 파일 형식입니다: {ext}"))
                continue

            try:
                self._import_single(
                    src_path=src_path,
                    default_folder_id=folder_id,
                    base_dir=base_dir,
                    preserve_subfolders=preserve_subfolders,
                    duplicate_policy=duplicate_policy,
                    keymap=keymap,
                    summary=summary,
                )
            except Exception as exc:
                logger.warning("Failed to import %s: %s", src_path, exc)
                summary.errors.append((src_path, str(exc)))

        return summary

    def _import_single(
        self,
        *,
        src_path: Path,
        default_folder_id: int | None,
        base_dir: Path | None,
        preserve_subfolders: bool,
        duplicate_policy: DuplicatePolicy,
        keymap: Keymap | None,
        summary: ImportSummary,
    ) -> None:
        file_hash = compute_sha256(src_path)
        existing = self.repo.find_by_hash(file_hash, include_deleted=False)
        if existing and duplicate_policy != "copy":
            summary.skipped_duplicates.append(src_path)
            if duplicate_policy == "ask":
                summary.duplicate_matches.append((src_path, existing[0]))
            return

        target_folder_id = default_folder_id
        subdir = ""
        if preserve_subfolders and base_dir is not None:
            try:
                rel_parent = src_path.resolve().parent.relative_to(base_dir.resolve())
                if rel_parent.parts:
                    subdir = rel_parent.as_posix()
                    created = self.repo.get_or_create_folder_path(
                        self._folder_chain_parts(default_folder_id, rel_parent.parts)
                    )
                    if created is not None:
                        target_folder_id = created.id
            except ValueError:
                pass

        ext = src_path.suffix.lower()
        if ext == ".ropi":
            archive = load_ropi(src_path, keymap=keymap)
            stored = self.file_store.import_copy(src_path, subdir=subdir, file_hash=file_hash)
            song = archive.song
            meta = archive.manifest
            lyrics: Sequence[LyricLine] = archive.lyrics
            item = NewSongInput(
                folder_id=target_folder_id,
                title=meta.title or song.title or src_path.stem,
                artist=meta.artist,
                memo=meta.memo,
                file_rel_path=stored.file_rel_path,
                format="ropi",
                file_hash=stored.file_hash,
                duration_ms=song.duration_ms,
                note_count=song.note_count,
                bpm=song.bpm,
                min_note=song.min_note,
                max_note=song.max_note,
                difficulty=meta.difficulty,
                source_url=meta.source_url,
                source_engine=meta.source_engine,
                settings=meta.settings,
                tags=meta.tags,
            )
            record = self.repo.insert_song(item, lyrics=lyrics)
            summary.imported.append(record)
            return

        song = load_song_file(src_path, keymap=keymap)
        lyrics_list: list[LyricLine] = []
        sidecar_lrc = src_path.with_suffix(".lrc")
        if sidecar_lrc.is_file():
            try:
                lyrics_list = load_lrc_file(sidecar_lrc)
            except FormatLoadError:
                logger.warning("Failed to parse sidecar LRC: %s", sidecar_lrc)

        stored = self.file_store.import_copy(src_path, subdir=subdir, file_hash=file_hash)
        fmt = "mid" if ext in {".mid", ".midi"} else "txt"
        item = NewSongInput(
            folder_id=target_folder_id,
            title=song.title or src_path.stem,
            file_rel_path=stored.file_rel_path,
            format=fmt,
            file_hash=stored.file_hash,
            duration_ms=song.duration_ms,
            note_count=song.note_count,
            bpm=song.bpm,
            min_note=song.min_note,
            max_note=song.max_note,
        )
        record = self.repo.insert_song(item, lyrics=lyrics_list)
        summary.imported.append(record)

    def _folder_chain_parts(
        self, base_folder_id: int | None, subparts: tuple[str, ...]
    ) -> list[str]:
        if base_folder_id is None:
            return list(subparts)
        folders = {f.id: f for f in self.repo.list_folders()}
        chain: list[str] = []
        cur: int | None = base_folder_id
        while cur is not None and cur in folders:
            chain.append(folders[cur].name)
            cur = folders[cur].parent_id
        chain.reverse()
        chain.extend(subparts)
        return chain

    # ------------------------------------------------------------ Song Metadata
    def update_song(self, song_id: int, patch: SongPatch) -> SongRecord:
        return self.repo.update_song(song_id, patch)

    def record_play(self, song_id: int) -> None:
        self.repo.record_play(song_id)

    def list_tags(self) -> list[TagRecord]:
        return self.repo.list_tags()

    # ------------------------------------------------------------------ Folders
    def list_folders(self) -> list[FolderRecord]:
        return self.repo.list_folders()

    def create_folder(self, name: str, parent_id: int | None = None) -> FolderRecord:
        return self.repo.create_folder(name, parent_id)

    def rename_folder(self, folder_id: int, new_name: str) -> FolderRecord:
        return self.repo.rename_folder(folder_id, new_name)

    def move_folder(self, folder_id: int, new_parent_id: int | None) -> FolderRecord:
        return self.repo.move_folder(folder_id, new_parent_id)

    def reorder_folders(self, ordered_ids: Sequence[int]) -> None:
        self.repo.reorder_folders(ordered_ids)

    def delete_folder(self, folder_id: int) -> None:
        self.repo.delete_folder(folder_id)

    # -------------------------------------------------------------------- Trash
    def move_to_trash(self, song_id: int) -> SongRecord:
        song = self.repo.get_song(song_id)
        if song is None:
            raise LibraryError(f"곡을 찾을 수 없습니다: id={song_id}")
        trash_rel = self.file_store.move_to_trash(song.file_rel_path)
        return self.repo.soft_delete_song(song_id, new_rel_path=trash_rel)

    def restore_from_trash(self, song_id: int) -> SongRecord:
        song = self.repo.get_song(song_id)
        if song is None:
            raise LibraryError(f"곡을 찾을 수 없습니다: id={song_id}")
        active_rel = self.file_store.restore_from_trash(song.file_rel_path)
        return self.repo.restore_song(song_id, new_rel_path=active_rel)

    def delete_permanently(self, song_id: int) -> None:
        removed = self.repo.hard_delete_song(song_id)
        if removed is not None:
            self.file_store.delete_permanently(removed.file_rel_path)

    def purge_expired_trash(self, *, days: int = 30) -> int:
        expired = self.repo.purge_expired_trash(older_than_days=days)
        for rec in expired:
            self.file_store.delete_permanently(rec.file_rel_path)
        return len(expired)

    # ---------------------------------------------------------------- Integrity
    def check_integrity(self) -> list[SongRecord]:
        """Return active songs whose managed file is missing from disk (F2-11)."""
        missing: list[SongRecord] = []
        for song in self.repo.list_active_songs_light():
            if not self.file_store.exists(song.file_rel_path):
                missing.append(replace(song, is_missing=True))
        return missing

    def relink_song(
        self, song_id: int, new_source: Path, *, keymap: Keymap | None = None
    ) -> SongRecord:
        song = self.repo.get_song(song_id)
        if song is None:
            raise LibraryError(f"곡을 찾을 수 없습니다: id={song_id}")
        parsed = load_song_file(new_source, keymap=keymap)
        stored = self.file_store.relink_file(song.file_rel_path, new_source)
        return self.repo.update_song_file_location(
            song_id,
            file_rel_path=stored.file_rel_path,
            file_hash=stored.file_hash,
            duration_ms=parsed.duration_ms,
            note_count=parsed.note_count,
            bpm=parsed.bpm,
            min_note=parsed.min_note,
            max_note=parsed.max_note,
        )

    # ------------------------------------------------------------------- Lyrics
    def get_lyrics(self, song_id: int) -> list[LyricLine]:
        return self.repo.get_lyrics(song_id)

    def save_lyrics(self, song_id: int, lines: Sequence[LyricLine]) -> None:
        self.repo.replace_lyrics(song_id, lines)

    def import_lrc(self, song_id: int, lrc_path: Path) -> list[LyricLine]:
        lines = load_lrc_file(lrc_path)
        self.repo.replace_lyrics(song_id, lines)
        return lines

    def export_lrc(self, song_id: int, dest_path: Path) -> Path:
        song = self.repo.get_song(song_id)
        if song is None:
            raise LibraryError(f"곡을 찾을 수 없습니다: id={song_id}")
        lines = self.repo.get_lyrics(song_id)
        text = format_lrc(lines, title=song.title, artist=song.artist)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_text(text, encoding="utf-8")
        return dest_path

    # ------------------------------------------------------------------- Export
    def export_song_ropi(self, song_id: int, dest_path: Path) -> Path:
        song = self.repo.get_song(song_id)
        if song is None:
            raise LibraryError(f"곡을 찾을 수 없습니다: id={song_id}")
        src_file = self.file_store.resolve_path(song.file_rel_path)
        if not src_file.is_file():
            raise LibraryError(f"내보낼 원본 곡 파일이 누락되었습니다: {song.title}")

        lyrics = self.repo.get_lyrics(song_id)
        if song.format == "ropi":
            arch = load_ropi(src_file)
            music_bytes = arch.music_bytes
            music_fmt = arch.manifest.music_format
        else:
            music_bytes = src_file.read_bytes()
            music_fmt = "txt" if song.format == "txt" else "mid"

        manifest = RopiManifest(
            title=song.title,
            artist=song.artist,
            memo=song.memo,
            difficulty=song.difficulty,
            tags=song.tags,
            settings=song.settings,
            source_url=song.source_url,
            source_engine=song.source_engine,
            music_filename="sheet.txt" if music_fmt == "txt" else "music.mid",
            music_format=music_fmt,
        )
        return export_ropi(dest_path, manifest=manifest, music_bytes=music_bytes, lyrics=lyrics)

    def export_song_original(self, song_id: int, dest_path: Path) -> Path:
        song = self.repo.get_song(song_id)
        if song is None:
            raise LibraryError(f"곡을 찾을 수 없습니다: id={song_id}")
        src_file = self.file_store.resolve_path(song.file_rel_path)
        if not src_file.is_file():
            raise LibraryError(f"내보낼 곡 파일이 누락되었습니다: {song.title}")
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        if song.format == "ropi" and dest_path.suffix.lower() != ".ropi":
            arch = load_ropi(src_file)
            fd, tmp_name = tempfile.mkstemp(
                prefix=".ropi_orig_", suffix=".tmp", dir=str(dest_path.parent)
            )
            os.close(fd)
            tmp_path = Path(tmp_name)
            try:
                tmp_path.write_bytes(arch.music_bytes)
                os.replace(tmp_path, dest_path)
            except Exception:
                tmp_path.unlink(missing_ok=True)
                raise
            return dest_path

        shutil.copyfile(src_file, dest_path)
        return dest_path

    # ---------------------------------------------------------------- Playlists
    def list_playlists(self) -> list[PlaylistRecord]:
        return self.repo.list_playlists()

    def create_playlist(self, name: str) -> PlaylistRecord:
        return self.repo.create_playlist(name)

    def rename_playlist(self, playlist_id: int, new_name: str) -> None:
        self.repo.rename_playlist(playlist_id, new_name)

    def delete_playlist(self, playlist_id: int) -> None:
        self.repo.delete_playlist(playlist_id)

    def get_playlist_songs(self, playlist_id: int) -> list[SongRecord]:
        return self.repo.get_playlist_songs(playlist_id)

    def set_playlist_items(self, playlist_id: int, song_ids: Sequence[int]) -> None:
        self.repo.set_playlist_items(playlist_id, song_ids)

    # ----------------------------------------------------------- Full Backups
    def export_backup(self, dest_zip: Path) -> Path:
        return export_library_archive(self.db, self.file_store, dest_zip)

    def restore_backup(self, src_zip: Path) -> int:
        db_path = self.db.db_path
        lib_dir = self.file_store.root_dir
        backup_dir = self.db.backup_dir
        migrations_dir = self.db.migrations_dir
        self.db.close()
        try:
            return restore_library_archive(src_zip, db_path, lib_dir)
        finally:
            self.db = Database(db_path, backup_dir=backup_dir, migrations_dir=migrations_dir)
            self.db.initialize(backup_before_migrate=False)
            self.repo = LibraryRepository(self.db)
            self.file_store.ensure_dirs()
