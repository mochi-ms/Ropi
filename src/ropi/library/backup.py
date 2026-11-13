"""Full library + database ZIP backup export and verified restore (F2-9, N4)."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from ropi.core.errors import LibraryError
from ropi.library.db import Database
from ropi.library.file_store import LibraryFileStore

_MAX_MEMBER_BYTES = 100 * 1024 * 1024  # 100 MB safety cap per archive member


def _safe_rel_posix(member_name: str, prefix: str) -> PurePosixPath | None:
    if not member_name.startswith(prefix):
        return None
    sub = member_name[len(prefix) :]
    if not sub or sub.endswith("/"):
        return None
    pure = PurePosixPath(sub)
    if pure.is_absolute() or ".." in pure.parts:
        raise LibraryError(f"안전하지 않은 백업 경로가 포함되어 있습니다: {member_name}")
    return pure


def export_library_archive(
    db: Database,
    file_store: LibraryFileStore,
    dest_zip: Path,
) -> Path:
    """Export SQLite snapshot + all managed library files atomically into a `.zip` archive."""
    dest_zip.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="ropi_bk_stage_") as stage_dir_str:
        stage_dir = Path(stage_dir_str)
        snapshot_db = db.snapshot_to(stage_dir / "ropi.db")

        managed_files: list[tuple[Path, str]] = []
        if file_store.root_dir.exists():
            for path in sorted(file_store.root_dir.rglob("*")):
                if not path.is_file():
                    continue
                if path.name.startswith(".ropi_") or path.suffix.lower() == ".tmp":
                    continue
                rel = path.relative_to(file_store.root_dir).as_posix()
                managed_files.append((path, f"files/{rel}"))

        manifest = {
            "format": "ropi_backup",
            "version": 1,
            "created_at": datetime.now(UTC).isoformat(),
            "file_count": len(managed_files),
        }

        fd, tmp_name = tempfile.mkstemp(
            prefix=".ropi_bkp_", suffix=".tmp", dir=str(dest_zip.parent)
        )
        os.close(fd)
        tmp_zip = Path(tmp_name)
        try:
            with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.writestr(
                    "backup_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2)
                )
                zf.write(snapshot_db, arcname="ropi.db")
                for src_path, arcname in managed_files:
                    zf.write(src_path, arcname=arcname)
            os.replace(tmp_zip, dest_zip)
        except Exception:
            tmp_zip.unlink(missing_ok=True)
            raise

    return dest_zip


def restore_library_archive(
    src_zip: Path,
    target_db_path: Path,
    target_library_dir: Path,
) -> int:
    """Validate and restore a library archive after closing active DB connections.

    Returns the number of song files restored into `target_library_dir`.
    """
    if not src_zip.is_file():
        raise LibraryError(f"백업 파일을 찾을 수 없습니다: {src_zip}")

    with tempfile.TemporaryDirectory(prefix="ropi_restore_") as stage_str:
        stage_dir = Path(stage_str)
        staged_db = stage_dir / "ropi.db"
        staged_files = stage_dir / "files"
        staged_files.mkdir(parents=True, exist_ok=True)

        restored_count = 0
        try:
            with zipfile.ZipFile(src_zip, "r") as zf:
                names = set(zf.namelist())
                if "backup_manifest.json" not in names or "ropi.db" not in names:
                    raise LibraryError("유효한 Ropi 백업 파일이 아닙니다.")

                raw_manifest = zf.read("backup_manifest.json")
                try:
                    manifest = json.loads(raw_manifest.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise LibraryError(f"백업 메타데이터가 손상되었습니다: {exc}") from exc
                if not isinstance(manifest, dict) or manifest.get("format") != "ropi_backup":
                    raise LibraryError("Ropi 백업 아카이브 형식이 올바르지 않습니다.")

                db_info = zf.getinfo("ropi.db")
                if db_info.file_size > _MAX_MEMBER_BYTES:
                    raise LibraryError("백업 DB 크기 제한을 초과했습니다.")
                staged_db.write_bytes(zf.read("ropi.db"))

                _verify_sqlite_integrity(staged_db)

                for info in zf.infolist():
                    rel_pure = _safe_rel_posix(info.filename, "files/")
                    if rel_pure is None:
                        continue
                    if info.file_size > _MAX_MEMBER_BYTES:
                        raise LibraryError(f"백업 파일 크기 초과: {info.filename}")
                    dest_file = (staged_files / Path(*rel_pure.parts)).resolve()
                    if not dest_file.is_relative_to(staged_files.resolve()):
                        raise LibraryError(f"허용되지 않은 복원 경로: {info.filename}")
                    dest_file.parent.mkdir(parents=True, exist_ok=True)
                    dest_file.write_bytes(zf.read(info.filename))
                    restored_count += 1
        except zipfile.BadZipFile as exc:
            raise LibraryError(f"손상된 백업 ZIP 파일입니다: {src_zip.name}") from exc

        # Atomically replace database file and clean stale WAL/SHM sidecars.
        target_db_path.parent.mkdir(parents=True, exist_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(f"{target_db_path}{suffix}").unlink(missing_ok=True)
        shutil.copyfile(staged_db, target_db_path)

        # Replace managed library contents safely.
        target_library_dir.mkdir(parents=True, exist_ok=True)
        for existing in list(target_library_dir.iterdir()):
            if existing.is_dir():
                shutil.rmtree(existing)
            else:
                existing.unlink()
        for item in staged_files.iterdir():
            dest_item = target_library_dir / item.name
            if item.is_dir():
                shutil.copytree(item, dest_item)
            else:
                shutil.copy2(item, dest_item)
        (target_library_dir / ".trash").mkdir(parents=True, exist_ok=True)

    return restored_count


def _verify_sqlite_integrity(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        if row is None or str(row[0]).lower() != "ok":
            raise LibraryError("백업된 데이터베이스 무결성 검사에 실패했습니다.")
        has_songs = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='songs'"
        ).fetchone()
        if has_songs is None:
            raise LibraryError("백업 DB에 필수 테이블이 없습니다.")
    except sqlite3.Error as exc:
        raise LibraryError(f"백업 DB를 검증할 수 없습니다: {exc}") from exc
    finally:
        conn.close()
