"""`.ropi` ZIP container format loader and atomic exporter (SPEC 6.6)."""

from __future__ import annotations

import json
import os
import tempfile
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ropi.core.errors import FormatLoadError
from ropi.domain.keymap import Keymap
from ropi.domain.note import SongData
from ropi.formats.lrc import LyricLine, format_lrc, parse_lrc
from ropi.formats.midi_loader import load_midi
from ropi.formats.vp_sheet import parse_vp_sheet

_MAX_MEMBER_BYTES = 25 * 1024 * 1024  # 25 MB safety limit per archive member


@dataclass(frozen=True, slots=True)
class RopiManifest:
    """Metadata stored inside manifest.json within a .ropi archive."""

    title: str
    artist: str = ""
    memo: str = ""
    difficulty: int | None = None
    tags: tuple[str, ...] = ()
    settings: dict[str, Any] = field(default_factory=dict)
    source_url: str | None = None
    source_engine: str | None = None
    music_filename: str = "music.mid"
    music_format: str = "mid"
    version: int = 1


@dataclass(frozen=True, slots=True)
class RopiArchive:
    """Complete parsed representation of a `.ropi` song container."""

    manifest: RopiManifest
    song: SongData
    lyrics: tuple[LyricLine, ...]
    music_bytes: bytes


def _read_zip_member_limited(zf: zipfile.ZipFile, name: str) -> bytes:
    info = zf.getinfo(name)
    if info.file_size > _MAX_MEMBER_BYTES:
        raise FormatLoadError(f".ropi 내부 파일 크기가 너무 큽니다: {name}")
    with zf.open(info, "r") as handle:
        data = handle.read(_MAX_MEMBER_BYTES + 1)
    if len(data) > _MAX_MEMBER_BYTES:
        raise FormatLoadError(f".ropi 내부 파일 크기 제한을 초과했습니다: {name}")
    return data


def load_ropi(path: Path, *, keymap: Keymap | None = None) -> RopiArchive:
    """Load and validate a `.ropi` archive without trusting unvalidated ZIP contents."""
    if not path.exists():
        raise FormatLoadError(f".ropi 파일을 찾을 수 없습니다: {path.name}")

    try:
        with zipfile.ZipFile(path, "r") as zf:
            names = set(zf.namelist())
            if "manifest.json" not in names:
                raise FormatLoadError(f"manifest.json이 없는 .ropi 파일입니다: {path.name}")

            raw_manifest = _read_zip_member_limited(zf, "manifest.json")
            try:
                manifest_data = json.loads(raw_manifest.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise FormatLoadError(f"손상된 .ropi 메타데이터입니다: {path.name}") from exc

            if not isinstance(manifest_data, dict) or manifest_data.get("format") != "ropi":
                raise FormatLoadError(f"올바른 Ropi 포맷이 아닙니다: {path.name}")

            music_filename = str(manifest_data.get("music_filename") or "")
            if not music_filename or music_filename not in names:
                if "music.mid" in names:
                    music_filename = "music.mid"
                elif "sheet.txt" in names:
                    music_filename = "sheet.txt"
                else:
                    raise FormatLoadError(f"음악 데이터가 없는 .ropi 파일입니다: {path.name}")

            music_bytes = _read_zip_member_limited(zf, music_filename)
            music_format = str(
                manifest_data.get("music_format")
                or ("txt" if music_filename.endswith(".txt") else "mid")
            ).lower()

            lyrics_list: list[LyricLine] = []
            if "lyrics.lrc" in names:
                raw_lrc = _read_zip_member_limited(zf, "lyrics.lrc")
                lyrics_list = parse_lrc(raw_lrc.decode("utf-8-sig", errors="replace"))
    except zipfile.BadZipFile as exc:
        raise FormatLoadError(f"손상된 .ropi 압축 파일입니다: {path.name}") from exc

    song = _parse_embedded_song(
        music_bytes=music_bytes,
        music_format=music_format,
        fallback_title=str(manifest_data.get("title") or path.stem),
        keymap=keymap,
    )

    raw_tags = manifest_data.get("tags") or []
    tags = tuple(str(t).strip() for t in raw_tags if str(t).strip())
    raw_settings = manifest_data.get("settings")
    settings: dict[str, Any] = dict(raw_settings) if isinstance(raw_settings, dict) else {}
    difficulty_raw = manifest_data.get("difficulty")
    difficulty = int(difficulty_raw) if isinstance(difficulty_raw, int) else None

    manifest = RopiManifest(
        title=str(manifest_data.get("title") or song.title or path.stem),
        artist=str(manifest_data.get("artist") or ""),
        memo=str(manifest_data.get("memo") or ""),
        difficulty=difficulty,
        tags=tags,
        settings=settings,
        source_url=(str(manifest_data["source_url"]) if manifest_data.get("source_url") else None),
        source_engine=(
            str(manifest_data["source_engine"]) if manifest_data.get("source_engine") else None
        ),
        music_filename=music_filename,
        music_format=music_format,
        version=int(manifest_data.get("version") or 1),
    )

    return RopiArchive(
        manifest=manifest,
        song=song,
        lyrics=tuple(lyrics_list),
        music_bytes=music_bytes,
    )


def _parse_embedded_song(
    *,
    music_bytes: bytes,
    music_format: str,
    fallback_title: str,
    keymap: Keymap | None,
) -> SongData:
    if music_format == "txt":
        text = music_bytes.decode("utf-8-sig", errors="replace")
        return parse_vp_sheet(text, title=fallback_title, keymap=keymap)

    with tempfile.TemporaryDirectory(prefix="ropi_zip_") as tmp_dir:
        tmp_mid = Path(tmp_dir) / "music.mid"
        tmp_mid.write_bytes(music_bytes)
        song = load_midi(tmp_mid)
        if not song.title or song.title == "music":
            return SongData(
                notes=song.notes,
                tracks=song.tracks,
                duration_ms=song.duration_ms,
                bpm=song.bpm,
                min_note=song.min_note,
                max_note=song.max_note,
                source_format=song.source_format,
                title=fallback_title,
                pedals=song.pedals,
            )
        return song


def export_ropi(
    dest_path: Path,
    *,
    manifest: RopiManifest,
    music_bytes: bytes,
    lyrics: Sequence[LyricLine] = (),
) -> Path:
    """Atomically write a `.ropi` ZIP archive containing manifest, song bytes, and optional LRC."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    music_ext = "txt" if manifest.music_format == "txt" else "mid"
    music_filename = "sheet.txt" if music_ext == "txt" else "music.mid"

    payload: dict[str, Any] = {
        "format": "ropi",
        "version": manifest.version,
        "title": manifest.title,
        "artist": manifest.artist,
        "memo": manifest.memo,
        "difficulty": manifest.difficulty,
        "tags": list(manifest.tags),
        "settings": manifest.settings,
        "source_url": manifest.source_url,
        "source_engine": manifest.source_engine,
        "music_filename": music_filename,
        "music_format": music_ext,
    }

    fd, tmp_name = tempfile.mkstemp(prefix=".ropi_exp_", suffix=".tmp", dir=str(dest_path.parent))
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", json.dumps(payload, ensure_ascii=False, indent=2))
            zf.writestr(music_filename, music_bytes)
            if lyrics:
                zf.writestr(
                    "lyrics.lrc",
                    format_lrc(lyrics, title=manifest.title, artist=manifest.artist),
                )
        os.replace(tmp_path, dest_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return dest_path
