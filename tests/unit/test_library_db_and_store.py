"""Unit tests for Database migrations/WAL/backups and LibraryFileStore."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from ropi.core.errors import LibraryError
from ropi.library.db import Database
from ropi.library.file_store import (
    LibraryFileStore,
    compute_sha256,
    sanitize_filename,
)


def test_database_wal_fk_migrations_transactions_and_rolling_backups(tmp_path: Path) -> None:
    db_path = tmp_path / "data" / "ropi.db"
    backup_dir = tmp_path / "data" / "backups"
    db = Database(db_path, backup_dir=backup_dir, max_backups=5)

    version = db.initialize(backup_before_migrate=True)
    assert version == 1
    assert db.current_version() == 1

    with db.read() as conn:
        journal_mode = str(conn.execute("PRAGMA journal_mode;").fetchone()[0]).lower()
        fk_enabled = int(conn.execute("PRAGMA foreign_keys;").fetchone()[0])
        tables = {
            str(r[0])
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")
        }
    assert journal_mode == "wal"
    assert fk_enabled == 1
    assert {
        "folders",
        "songs",
        "tags",
        "song_tags",
        "lyrics",
        "playlists",
        "playlist_items",
        "convert_jobs",
        "song_fts",
    } <= tables

    # Transaction rollback safety
    with pytest.raises(RuntimeError), db.transaction() as tx:
        tx.execute(
            "INSERT INTO folders(parent_id, name, sort_order, created_at) VALUES (NULL, 'Temp', 0, '2026-01-01')"
        )
        raise RuntimeError("boom")
    with db.read() as conn:
        count = conn.execute("SELECT COUNT(*) FROM folders WHERE name = 'Temp'").fetchone()[0]
    assert count == 0

    # Snapshot + rolling 5 startup backups
    snap = db.snapshot_to(tmp_path / "snap" / "copy.db")
    assert snap.is_file() and snap.stat().st_size > 0

    db.close()
    for i in range(7):
        time.sleep(0.01)
        d = Database(db_path, backup_dir=backup_dir, max_backups=5)
        d.initialize(backup_before_migrate=True)
        if i == 0:
            backups = d.list_backups()
            assert len(backups) >= 1
        d.close()

    final_db = Database(db_path, backup_dir=backup_dir, max_backups=5)
    assert len(final_db.list_backups()) == 5
    final_db.close()

    with pytest.raises(LibraryError), final_db.read():
        pass


def test_file_store_sanitization_trash_restore_and_traversal_guard(tmp_path: Path) -> None:
    assert sanitize_filename('Bad<>:"/\\|?*Name . ') == "Bad_________Name"
    assert sanitize_filename("CON") == "_CON"
    assert sanitize_filename("NUL.mid") == "_NUL.mid"
    assert sanitize_filename("...") == "song"

    store = LibraryFileStore(tmp_path / "library")
    src = tmp_path / "external" / "CON.mid"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"MThd\x00\x00\x00\x06\x00\x01\x00\x01\x01\xe0")

    first = store.import_copy(src, subdir="Classic/Chopin")
    assert first.file_rel_path == "Classic/Chopin/_CON.mid"
    assert first.file_hash == compute_sha256(src)
    assert store.exists(first.file_rel_path)

    # Duplicate filename creates disambiguated name
    second = store.import_copy(src, subdir="Classic/Chopin")
    assert second.file_rel_path == "Classic/Chopin/_CON (2).mid"
    assert store.exists(second.file_rel_path)

    # Move to trash and restore
    trash_rel = store.move_to_trash(first.file_rel_path)
    assert trash_rel.startswith(".trash/")
    assert not store.exists(first.file_rel_path)
    assert store.exists(trash_rel)

    restored_rel = store.restore_from_trash(trash_rel)
    assert restored_rel == "Classic/Chopin/_CON.mid"
    assert store.exists(restored_rel)
    assert not store.exists(trash_rel)

    # Permanent delete
    assert store.delete_permanently(second.file_rel_path) is True
    assert store.delete_permanently(second.file_rel_path) is False

    # Relink preserves subdirectory
    replacement = tmp_path / "external" / "new_version.mid"
    replacement.write_bytes(b"MThd_replacement")
    relinked = store.relink_file(restored_rel, replacement)
    assert relinked.file_rel_path.startswith("Classic/Chopin/")
    assert store.exists(relinked.file_rel_path)

    # Traversal guard
    with pytest.raises(LibraryError):
        store.resolve_path("../escape.mid")
    with pytest.raises(LibraryError):
        store.resolve_path("")
    assert store.exists("../escape.mid") is False
