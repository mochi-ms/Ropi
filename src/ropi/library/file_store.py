"""Managed filesystem storage for imported song files and soft-delete trash."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ropi.core.errors import LibraryError

_FORBIDDEN_WIN_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)
_TRASH_DIR_NAME = ".trash"


@dataclass(frozen=True, slots=True)
class StoredFileInfo:
    """Metadata returned after importing or relinking a file into the managed library."""

    file_rel_path: str
    file_hash: str
    size_bytes: int
    format: str


def compute_sha256(path: Path) -> str:
    """Return lowercase hexadecimal SHA-256 digest of a file."""
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def sanitize_filename(name: str, *, fallback: str = "song") -> str:
    """Sanitize a filename segment for safe Windows storage."""
    cleaned = _FORBIDDEN_WIN_CHARS.sub("_", name).strip(" .")
    if not cleaned:
        cleaned = fallback
    stem = Path(cleaned).stem.upper()
    if stem in _WINDOWS_RESERVED:
        cleaned = f"_{cleaned}"
    return cleaned


def _sanitize_subdir(subdir: str) -> list[str]:
    if not subdir:
        return []
    parts: list[str] = []
    for seg in Path(subdir).parts:
        if seg in ("", ".", "..", "/", "\\"):
            continue
        if seg == _TRASH_DIR_NAME:
            continue
        parts.append(sanitize_filename(seg, fallback="folder"))
    return parts


class LibraryFileStore:
    """Manages copied song assets under a library root directory."""

    def __init__(self, root_dir: Path) -> None:
        self.root_dir = root_dir.resolve()
        self.trash_dir = self.root_dir / _TRASH_DIR_NAME
        self.ensure_dirs()

    def ensure_dirs(self) -> None:
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.trash_dir.mkdir(parents=True, exist_ok=True)

    def resolve_path(self, file_rel_path: str) -> Path:
        """Resolve a relative library path safely inside `root_dir`."""
        rel = file_rel_path.replace("\\", "/").lstrip("/")
        if not rel:
            raise LibraryError("빈 상대 경로입니다.")
        candidate = (self.root_dir / rel).resolve()
        if not candidate.is_relative_to(self.root_dir):
            raise LibraryError(f"허용되지 않은 라이브러리 경로입니다: {file_rel_path}")
        return candidate

    def exists(self, file_rel_path: str) -> bool:
        """Return True if the managed file currently exists on disk."""
        try:
            path = self.resolve_path(file_rel_path)
        except LibraryError:
            return False
        return path.is_file()

    def import_copy(
        self,
        source: Path,
        *,
        preferred_name: str | None = None,
        subdir: str = "",
        file_hash: str | None = None,
    ) -> StoredFileInfo:
        """Copy `source` into the managed library without altering the original file."""
        if not source.is_file():
            raise LibraryError(f"가져올 파일을 찾을 수 없습니다: {source}")

        self.ensure_dirs()
        ext = source.suffix.lower().lstrip(".") or "mid"
        raw_name = preferred_name if preferred_name else source.name
        stem = sanitize_filename(Path(raw_name).stem, fallback="song")
        safe_ext = sanitize_filename(Path(raw_name).suffix.lstrip(".") or ext, fallback=ext)

        subdir_parts = _sanitize_subdir(subdir)
        target_dir = self.root_dir.joinpath(*subdir_parts)
        target_dir.mkdir(parents=True, exist_ok=True)

        target_path = self._unique_target_path(target_dir, stem, f".{safe_ext}")
        digest = file_hash or compute_sha256(source)
        self._atomic_copy(source, target_path)

        rel = target_path.relative_to(self.root_dir).as_posix()
        return StoredFileInfo(
            file_rel_path=rel,
            file_hash=digest,
            size_bytes=target_path.stat().st_size,
            format=safe_ext,
        )

    def move_to_trash(self, file_rel_path: str) -> str:
        """Move a managed file into `.trash/` and return the new relative trash path."""
        src = self.resolve_path(file_rel_path)
        if not src.exists():
            return file_rel_path
        if src.is_relative_to(self.trash_dir):
            return src.relative_to(self.root_dir).as_posix()

        rel_inside = src.relative_to(self.root_dir)
        dest_dir = self.trash_dir / rel_inside.parent
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = self._unique_target_path(dest_dir, src.stem, src.suffix)
        shutil.move(str(src), str(dest))
        return dest.relative_to(self.root_dir).as_posix()

    def restore_from_trash(self, file_rel_path: str) -> str:
        """Move a file back from `.trash/` into its original active library path."""
        src = self.resolve_path(file_rel_path)
        if not src.exists():
            # Strip .trash prefix cleanly even if file is missing.
            norm = file_rel_path.replace("\\", "/")
            prefix = f"{_TRASH_DIR_NAME}/"
            return norm[len(prefix) :] if norm.startswith(prefix) else norm

        if not src.is_relative_to(self.trash_dir):
            return src.relative_to(self.root_dir).as_posix()

        rel_inside_trash = src.relative_to(self.trash_dir)
        dest_dir = self.root_dir / rel_inside_trash.parent
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = self._unique_target_path(dest_dir, src.stem, src.suffix)
        shutil.move(str(src), str(dest))
        return dest.relative_to(self.root_dir).as_posix()

    def delete_permanently(self, file_rel_path: str) -> bool:
        """Permanently delete a managed file from disk (returns True if removed)."""
        target = self.resolve_path(file_rel_path)
        if not target.exists():
            return False
        target.unlink()
        return True

    def relink_file(self, old_rel_path: str, new_source: Path) -> StoredFileInfo:
        """Copy a replacement file into the managed library (preserving old directory if valid)."""
        subdir = ""
        try:
            old_path = self.resolve_path(old_rel_path)
            if old_path.parent != self.root_dir and not old_path.is_relative_to(self.trash_dir):
                subdir = old_path.parent.relative_to(self.root_dir).as_posix()
        except LibraryError:
            subdir = ""
        return self.import_copy(new_source, subdir=subdir)

    @staticmethod
    def _unique_target_path(directory: Path, stem: str, suffix: str) -> Path:
        candidate = directory / f"{stem}{suffix}"
        if not candidate.exists():
            return candidate
        idx = 2
        while True:
            alt = directory / f"{stem} ({idx}){suffix}"
            if not alt.exists():
                return alt
            idx += 1

    @staticmethod
    def _atomic_copy(src: Path, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".ropi_cp_", suffix=".tmp", dir=str(dest.parent))
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            shutil.copyfile(src, tmp_path)
            os.replace(tmp_path, dest)
        except Exception as exc:
            tmp_path.unlink(missing_ok=True)
            raise LibraryError(f"파일 복사에 실패했습니다 ({src.name}): {exc}") from exc
