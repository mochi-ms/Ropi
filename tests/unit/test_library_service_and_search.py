"""Comprehensive tests for LibraryService workflows, FTS5 + Hangul chosung search, and 10k benchmark."""

from __future__ import annotations

import threading
import time
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import mido
import pytest

from ropi.core.errors import LibraryError
from ropi.formats.lrc import LyricLine
from ropi.formats.ropi_format import RopiManifest, export_ropi
from ropi.library.db import Database
from ropi.library.file_store import LibraryFileStore
from ropi.library.models import NewSongInput, SongPatch, SongQuery
from ropi.library.service import LibraryService


def _write_test_midi(path: Path, notes: list[int] | None = None) -> Path:
    if notes is None:
        notes = [60, 62, 64]
    path.parent.mkdir(parents=True, exist_ok=True)
    mid = mido.MidiFile(ticks_per_beat=480)
    track = mido.MidiTrack()
    mid.tracks.append(track)
    track.append(mido.MetaMessage("set_tempo", tempo=500_000, time=0))
    for n in notes:
        track.append(mido.Message("note_on", note=n, velocity=90, time=0))
        track.append(mido.Message("note_off", note=n, velocity=0, time=240))
    track.append(mido.MetaMessage("end_of_track", time=0))
    mid.save(path)
    return path


def _make_service(tmp_path: Path) -> LibraryService:
    db = Database(tmp_path / "data" / "ropi.db", backup_dir=tmp_path / "data" / "backups")
    store = LibraryFileStore(tmp_path / "library")
    svc = LibraryService(db, store)
    svc.initialize()
    return svc


def test_folder_crud_cycle_protection_and_deletion_policy(tmp_path: Path) -> None:
    svc = _make_service(tmp_path)
    try:
        classic = svc.create_folder("Classic")
        jazz = svc.create_folder("Jazz")
        chopin = svc.create_folder("Chopin", parent_id=classic.id)

        with pytest.raises(LibraryError):
            svc.create_folder("Classic")  # duplicate root sibling
        with pytest.raises(LibraryError):
            svc.move_folder(classic.id, chopin.id)  # cycle into descendant
        with pytest.raises(LibraryError):
            svc.move_folder(classic.id, classic.id)  # self cycle

        renamed = svc.rename_folder(jazz.id, "Modern Jazz")
        assert renamed.name == "Modern Jazz"

        svc.reorder_folders([jazz.id, classic.id])
        folders = [f for f in svc.list_folders() if f.parent_id is None]
        assert [f.id for f in folders] == [jazz.id, classic.id]

        # Import a song into Chopin, search recursively from Classic and delete folder -> song becomes uncategorized
        midi_src = _write_test_midi(tmp_path / "drop" / "prelude.mid")
        res = svc.import_files([midi_src], folder_id=chopin.id)
        assert len(res.imported) == 1
        song = res.imported[0]

        recursive_hits = svc.search(SongQuery(folder_id=classic.id, include_subfolders=True))
        direct_hits = svc.search(SongQuery(folder_id=classic.id, include_subfolders=False))
        assert [s.id for s in recursive_hits] == [song.id]
        assert direct_hits == []

        svc.delete_folder(chopin.id)
        reloaded = svc.get_song(song.id)
        assert reloaded is not None
        assert reloaded.folder_id is None
    finally:
        svc.db.close()


def test_batch_import_duplicates_sidecar_lrc_ropi_errors_and_cancel(tmp_path: Path) -> None:
    svc = _make_service(tmp_path)
    try:
        inbox = tmp_path / "inbox"
        sub_kr = inbox / "한국곡"
        midi_arirang = _write_test_midi(sub_kr / "아리랑 변주곡.mid", [60, 62, 65])
        (sub_kr / "아리랑 변주곡.lrc").write_text("[00:01.00]아리랑 아리랑\n", encoding="utf-8")

        sheet_txt = inbox / "밤하늘 Piano.txt"
        sheet_txt.parent.mkdir(parents=True, exist_ok=True)
        sheet_txt.write_text("t u o [eu]", encoding="utf-8")

        corrupt_mid = inbox / "broken.mid"
        corrupt_mid.write_bytes(b"NOT_A_MIDI")

        ropi_src = tmp_path / "ext" / "showcase.ropi"
        mid_bytes = midi_arirang.read_bytes()
        export_ropi(
            ropi_src,
            manifest=RopiManifest(
                title="녹턴 Op.9",
                artist="쇼팽",
                tags=("클래식", "밤"),
                difficulty=5,
                settings={"speed": 0.9},
            ),
            music_bytes=mid_bytes,
            lyrics=[LyricLine(line_no=0, time_ms=500, text="Nocturne")],
        )

        prog_events: list[tuple[int, int]] = []
        summary = svc.import_folder(
            inbox,
            preserve_subfolders=True,
            on_progress=lambda cur, tot, _p: prog_events.append((cur, tot)),
        )
        assert len(summary.imported) == 2
        assert len(summary.errors) == 1
        assert summary.errors[0][0].name == "broken.mid"
        assert prog_events[-1] == (3, 3)

        # Originals untouched on disk
        assert midi_arirang.exists()
        assert sheet_txt.exists()

        arirang = next(s for s in summary.imported if "아리랑" in s.title)
        assert arirang.has_lyrics is True
        assert svc.get_lyrics(arirang.id)[0].text == "아리랑 아리랑"

        # Duplicate policies: skip, ask, copy
        dup_skip = svc.import_files([midi_arirang], duplicate_policy="skip")
        assert len(dup_skip.skipped_duplicates) == 1
        assert dup_skip.duplicate_matches == []

        dup_ask = svc.import_files([midi_arirang], duplicate_policy="ask")
        assert len(dup_ask.duplicate_matches) == 1
        assert dup_ask.duplicate_matches[0][1].id == arirang.id

        dup_copy = svc.import_files([midi_arirang], duplicate_policy="copy")
        assert len(dup_copy.imported) == 1

        # .ropi import (allow copy since same raw midi bytes differ from .ropi sha256)
        ropi_res = svc.import_files([ropi_src])
        assert len(ropi_res.imported) == 1
        nocturne = ropi_res.imported[0]
        assert nocturne.format == "ropi"
        assert set(nocturne.tags) == {"클래식", "밤"}
        assert nocturne.settings["speed"] == 0.9
        assert svc.load_song_data(nocturne).note_count == 3

        # Cancellation stops before importing remaining files
        cancel = threading.Event()
        cancel.set()
        cancelled = svc.import_files([midi_arirang], duplicate_policy="copy", cancel_event=cancel)
        assert cancelled.cancelled is True
        assert cancelled.imported == []
    finally:
        svc.db.close()


def test_metadata_trash_integrity_relink_playlists_export_and_backup_restore(
    tmp_path: Path,
) -> None:
    svc = _make_service(tmp_path)
    try:
        src1 = _write_test_midi(tmp_path / "src" / "moonlight.mid", [61, 64, 68])
        src2 = _write_test_midi(tmp_path / "src" / "canon.mid", [62, 66, 69, 74])
        imported = svc.import_files([src1, src2]).imported
        s1, s2 = imported[0], imported[1]

        # Metadata update + FTS reindex + play count
        updated = svc.update_song(
            s1.id,
            SongPatch(
                title="월광 소나타 1악장",
                artist="베토벤",
                memo="서정적",
                difficulty=3,
                favorite=True,
                tags=("클래식", "명곡"),
                settings={"pedal_strategy": "smart"},
            ),
        )
        assert updated.favorite is True
        assert set(updated.tags) == {"클래식", "명곡"}
        svc.record_play(s1.id)
        after_play = svc.get_song(s1.id)
        assert after_play is not None and after_play.play_count == 1

        # LRC & .ropi & original export
        lrc_file = tmp_path / "lyrics.lrc"
        lrc_file.write_text("[00:05.00]달빛 아래\n", encoding="utf-8")
        svc.import_lrc(s1.id, lrc_file)
        exported_lrc = svc.export_lrc(s1.id, tmp_path / "out" / "moon.lrc")
        assert "달빛 아래" in exported_lrc.read_text(encoding="utf-8")

        exported_ropi = svc.export_song_ropi(s1.id, tmp_path / "out" / "moon.ropi")
        assert exported_ropi.is_file()
        exported_orig = svc.export_song_original(s1.id, tmp_path / "out" / "moon_copy.mid")
        assert exported_orig.read_bytes() == src1.read_bytes()

        # Playlist CRUD & ordering
        pl = svc.create_playlist("연습 리스트")
        svc.set_playlist_items(pl.id, [s2.id, s1.id])
        assert [x.id for x in svc.get_playlist_songs(pl.id)] == [s2.id, s1.id]
        svc.rename_playlist(pl.id, "저녁 연습")
        assert svc.list_playlists()[0].name == "저녁 연습"

        # Soft delete -> trash -> restore
        trashed = svc.move_to_trash(s2.id)
        assert trashed.deleted_at is not None
        assert trashed.file_rel_path.startswith(".trash/")
        assert all(x.id != s2.id for x in svc.search(SongQuery()))
        assert [x.id for x in svc.search(SongQuery(trash_only=True))] == [s2.id]

        restored = svc.restore_from_trash(s2.id)
        assert restored.deleted_at is None
        assert not restored.file_rel_path.startswith(".trash/")

        # Expired trash auto-purge after 30 days
        svc.move_to_trash(s2.id)
        old_ts = (datetime.now(UTC) - timedelta(days=31)).isoformat()
        with svc.db.transaction() as tx:
            tx.execute("UPDATE songs SET deleted_at = ? WHERE id = ?", (old_ts, s2.id))
        purged = svc.purge_expired_trash(days=30)
        assert purged == 1
        assert svc.get_song(s2.id) is None

        # Integrity check for missing file on disk + relink
        managed_s1 = svc.resolve_song_path(svc.get_song(s1.id) or updated)
        managed_s1.unlink()
        missing = svc.check_integrity()
        assert [m.id for m in missing] == [s1.id]
        assert svc.get_song(s1.id) is not None and svc.get_song(s1.id).is_missing is True  # type: ignore[union-attr]

        replacement_mid = _write_test_midi(tmp_path / "src" / "moonlight_v2.mid", [60, 67, 72])
        relinked = svc.relink_song(s1.id, replacement_mid)
        assert svc.check_integrity() == []
        assert svc.load_song_data(relinked).note_count == 3

        # Backup export & full verified restore into fresh location (also via restore_backup)
        backup_zip = svc.export_backup(tmp_path / "backups" / "full_lib.zip")
        assert backup_zip.is_file()

        # Corrupt backup archive is rejected safely & DB stays open
        with pytest.raises(LibraryError):
            svc.restore_backup(tmp_path / "backups" / "does_not_exist.zip")

        non_zip = tmp_path / "backups" / "not_zip.zip"
        non_zip.write_bytes(b"corrupt zip bytes")
        with pytest.raises(LibraryError):
            svc.restore_backup(non_zip)

        no_manifest_zip = tmp_path / "backups" / "bad.zip"
        with zipfile.ZipFile(no_manifest_zip, "w") as zf:
            zf.writestr("ropi.db", b"corrupt sqlite bytes")
        with pytest.raises(LibraryError):
            svc.restore_backup(no_manifest_zip)

        corrupt_db_zip = tmp_path / "backups" / "corrupt_db.zip"
        with zipfile.ZipFile(corrupt_db_zip, "w") as zf:
            zf.writestr("backup_manifest.json", '{"format":"ropi_backup","version":1}')
            zf.writestr("ropi.db", b"not a sqlite file")
        with pytest.raises(LibraryError):
            svc.restore_backup(corrupt_db_zip)

        # Mutate state (permanently delete s1 + create convert job + delete playlist), then restore backup
        svc.delete_playlist(pl.id)
        assert svc.list_playlists() == []
        job_id = svc.repo.insert_convert_job("https://example.invalid/audio", "demucs", "queued")
        svc.repo.update_convert_job(job_id, status="done", progress=1.0, song_id=s1.id)
        assert {t.name for t in svc.list_tags()} == {"클래식", "명곡"}

        svc.delete_permanently(s1.id)
        assert svc.search(SongQuery()) == []
        restored_count = svc.restore_backup(backup_zip)
        assert restored_count >= 1
        recovered = svc.search(SongQuery(text="ㅇㄱ ㅅㄴㅌ"))
        assert len(recovered) == 1
        assert recovered[0].title == "월광 소나타 1악장"
        assert svc.check_integrity() == []

        # Also test moving folder to root + export original from .ropi
        sub = svc.create_folder("TempSub")
        nested = svc.create_folder("Child", parent_id=sub.id)
        moved = svc.move_folder(nested.id, None)
        assert moved.parent_id is None
        ropi_imp = svc.import_files([exported_ropi], duplicate_policy="copy").imported[0]
        extracted_mid = svc.export_song_original(ropi_imp.id, tmp_path / "out" / "extracted.mid")
        assert extracted_mid.is_file() and extracted_mid.stat().st_size > 0
        ropi_reexp = svc.export_song_ropi(ropi_imp.id, tmp_path / "out" / "reexported.ropi")
        assert ropi_reexp.is_file()
    finally:
        svc.db.close()


def test_chosung_search_filters_and_10k_song_benchmark(tmp_path: Path) -> None:
    svc = _make_service(tmp_path)
    try:
        # Insert 10,000 songs cleanly in one bulk transaction
        bulk_items: list[NewSongInput] = []
        titles_kr = ("별빛 야상곡", "봄날의 왈츠", "겨울 바다", "기억의 습작", "첫눈처럼")
        artists = ("Chopin", "Debussy", "Yiruma", "이루마", "모차르트")
        for i in range(9995):
            title = f"{titles_kr[i % len(titles_kr)]} No.{i + 1}"
            fmt = "mid" if i % 4 else "txt"
            bulk_items.append(
                NewSongInput(
                    folder_id=None,
                    title=title,
                    artist=artists[i % len(artists)],
                    file_rel_path=f"bulk/song_{i}.{fmt}",
                    format=fmt,
                    file_hash=f"hash_{i:05d}",
                    duration_ms=60_000 + (i % 120) * 1000,
                    note_count=200 + (i % 500),
                    bpm=120.0,
                    difficulty=(i % 5) + 1,
                    favorite=(i % 10 == 0),
                    tags=("연습",) if i % 50 == 0 else (),
                )
            )
        # Specific Hangul chosung + mixed-language + punctuation test targets
        bulk_items.extend(
            [
                NewSongInput(
                    folder_id=None,
                    title="아리랑 변주곡",
                    artist="작곡가 미상",
                    file_rel_path="bulk/arirang.mid",
                    format="mid",
                    file_hash="hash_arirang",
                    favorite=True,
                    tags=("민요",),
                ),
                NewSongInput(
                    folder_id=None,
                    title="밤하늘 Piano",
                    artist="Ropi",
                    file_rel_path="bulk/night_sky.txt",
                    format="txt",
                    file_hash="hash_nightsky",
                ),
                NewSongInput(
                    folder_id=None,
                    title="C# Minor [Hard] Prelude",
                    artist="Rachmaninoff",
                    file_rel_path="bulk/csharp.mid",
                    format="mid",
                    file_hash="hash_csharp",
                ),
            ]
        )

        svc.repo.insert_songs_bulk(bulk_items)

        # 1. Exact chosung tests from ROADMAP
        t0 = time.perf_counter()
        hit1 = svc.search(SongQuery(text="ㅇㄹㄹ", sort_by="relevance"))
        dt1_ms = (time.perf_counter() - t0) * 1000.0
        assert [s.title for s in hit1] == ["아리랑 변주곡"]

        t1 = time.perf_counter()
        hit2 = svc.search(SongQuery(text="ㅂㅈㄱ"))
        dt2_ms = (time.perf_counter() - t1) * 1000.0
        assert [s.title for s in hit2] == ["아리랑 변주곡"]

        t2 = time.perf_counter()
        hit3 = svc.search(SongQuery(text="ㅂㅎㄴ piano"))
        dt3_ms = (time.perf_counter() - t2) * 1000.0
        assert any(s.title == "밤하늘 Piano" for s in hit3)

        # 2. Combined filters + punctuation safety
        hit4 = svc.search(SongQuery(text="C# [Hard]", format_filter="mid"))
        assert [s.title for s in hit4] == ["C# Minor [Hard] Prelude"]

        tagged_favs = svc.search(SongQuery(tag="연습", favorite_only=True, limit=10))
        assert len(tagged_favs) == 10

        # 3. 10,000-song prefix/broad benchmark assertion (<= 100ms)
        t3 = time.perf_counter()
        broad = svc.search(SongQuery(text="야상곡", limit=100))
        dt4_ms = (time.perf_counter() - t3) * 1000.0
        assert len(broad) == 100

        print(
            f"\n[10k Benchmark] chosung='ㅇㄹㄹ': {dt1_ms:.2f}ms | 'ㅂㅈㄱ': {dt2_ms:.2f}ms | "
            f"mixed='ㅂㅎㄴ piano': {dt3_ms:.2f}ms | korean='야상곡'(limit 100): {dt4_ms:.2f}ms"
        )
        assert dt1_ms <= 100.0
        assert dt2_ms <= 100.0
        assert dt3_ms <= 100.0
        assert dt4_ms <= 100.0
    finally:
        svc.db.close()
