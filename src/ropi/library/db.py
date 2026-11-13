"""SQLite database management: WAL mode, migrations, transactions, and startup backups."""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path

from ropi.core.errors import LibraryError

logger = logging.getLogger(__name__)

_MIGRATION_FILE_RE = re.compile(r"^(\d+)_.*\.sql$")
_DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


class Database:
    """Thread-safe SQLite manager supporting WAL, migrations, and rolling backups."""

    def __init__(
        self,
        db_path: Path,
        *,
        backup_dir: Path | None = None,
        migrations_dir: Path | None = None,
        max_backups: int = 5,
    ) -> None:
        self.db_path = db_path
        self.backup_dir = backup_dir or (db_path.parent / "backups")
        self.migrations_dir = migrations_dir or _DEFAULT_MIGRATIONS_DIR
        self.max_backups = max(1, max_backups)

        self._write_lock = threading.RLock()
        self._conn_lock = threading.Lock()
        self._local = threading.local()
        self._connections: list[sqlite3.Connection] = []
        self._closed = False

    def initialize(self, *, backup_before_migrate: bool = True) -> int:
        """Create parent directories, optionally back up existing DB, and apply migrations."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)

        if backup_before_migrate and self.db_path.exists() and self.db_path.stat().st_size > 0:
            try:
                self.create_backup()
            except Exception:
                logger.exception("Failed to create startup database backup")

        return self.migrate()

    def connect(self) -> sqlite3.Connection:
        """Return this thread's configured SQLite connection."""
        if self._closed:
            raise LibraryError("데이터베이스가 이미 닫혔습니다.")
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None:
            return conn

        conn = sqlite3.connect(
            str(self.db_path),
            timeout=10.0,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        conn.execute("PRAGMA temp_store = MEMORY;")

        self._local.conn = conn
        with self._conn_lock:
            self._connections.append(conn)
        return conn

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """Provide a configured connection for read-only queries."""
        yield self.connect()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Serialize writes in an explicit IMMEDIATE transaction with rollback safety."""
        with self._write_lock:
            conn = self.connect()
            conn.execute("BEGIN IMMEDIATE;")
            try:
                yield conn
            except Exception:
                conn.execute("ROLLBACK;")
                raise
            else:
                conn.execute("COMMIT;")

    def current_version(self) -> int:
        """Return currently applied schema version (0 if uninitialized)."""
        with self.read() as conn:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
            ).fetchone()
            if exists is None:
                return 0
            row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
            if row is None or row["v"] is None:
                return 0
            return int(row["v"])

    def migrate(self) -> int:
        """Apply all pending SQL migrations in ascending numeric order."""
        migrations = self._discover_migrations()
        current = self.current_version()

        for version, sql_path in migrations:
            if version <= current:
                continue
            sql = sql_path.read_text(encoding="utf-8")
            with self._write_lock:
                conn = self.connect()
                try:
                    # executescript implicitly commits before running, so wrap version insert cleanly.
                    conn.executescript(sql)
                    conn.execute("PRAGMA foreign_keys = ON;")
                    with self.transaction() as tx:
                        tx.execute("DELETE FROM schema_version")
                        tx.execute("INSERT INTO schema_version(version) VALUES (?)", (version,))
                except sqlite3.Error as exc:
                    raise LibraryError(f"DB 마이그레이션 실패 ({sql_path.name}): {exc}") from exc
            current = version
            logger.info("Applied DB migration v%d (%s)", version, sql_path.name)

        return current

    def snapshot_to(self, dest_path: Path) -> Path:
        """Write an online consistent SQLite snapshot of the database to `dest_path`."""
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        src_conn = self.connect()
        dst_conn = sqlite3.connect(str(dest_path))
        try:
            with self._write_lock:
                src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
        return dest_path

    def create_backup(self, tag: str = "auto") -> Path:
        """Create an online consistent SQLite backup and prune backups exceeding max_backups."""
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
        safe_tag = re.sub(r"[^a-zA-Z0-9_-]", "", tag) or "backup"
        dest_path = self.backup_dir / f"ropi-{safe_tag}-{stamp}.db"
        self.snapshot_to(dest_path)
        self._prune_old_backups()
        return dest_path

    def list_backups(self) -> list[Path]:
        """Return existing backup DB files ordered newest-first."""
        if not self.backup_dir.exists():
            return []
        return sorted(
            self.backup_dir.glob("ropi-*.db"),
            key=lambda p: (p.stat().st_mtime, p.name),
            reverse=True,
        )

    def _prune_old_backups(self) -> None:
        backups = self.list_backups()
        for stale in backups[self.max_backups :]:
            stale.unlink(missing_ok=True)

    def _discover_migrations(self) -> list[tuple[int, Path]]:
        if not self.migrations_dir.exists():
            raise LibraryError(f"마이그레이션 폴더를 찾을 수 없습니다: {self.migrations_dir}")
        discovered: list[tuple[int, Path]] = []
        for path in sorted(self.migrations_dir.iterdir()):
            match = _MIGRATION_FILE_RE.match(path.name)
            if match and path.is_file():
                discovered.append((int(match.group(1)), path))
        discovered.sort(key=lambda item: item[0])
        return discovered

    def close(self) -> None:
        """Close all thread-local connections held by this Database."""
        self._closed = True
        with self._conn_lock:
            for conn in self._connections:
                with suppress(sqlite3.Error):
                    conn.close()
            self._connections.clear()
        self._local = threading.local()
