"""Chunk state in SQLite (WAL): see docs/architecture.md, "State manager".

Metadata only: ids, byte ranges, status, retry counts, UTC timestamps. Never dataset content.

``synchronous=NORMAL`` in WAL mode fsyncs at checkpoints, not per commit. A killed process
loses nothing it committed; a power cut can lose the latest commits (never corrupt the file),
so a chunk may come back PENDING and run again. Chunk processing must therefore be
idempotent. Every state change is a compare-and-set inside ``BEGIN IMMEDIATE``.

SQL text is always a module-level constant with values bound as ``?`` parameters. One writer
only: opening the manager recovers orphans, which would take work from a live process
(``run_mining`` enforces this with ``JobLock``). Connections are tied to their thread.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Callable, Generator, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from datamining_skill.application.support import emit_event
from datamining_skill.domain.exceptions import (
    InvalidStateTransitionException,
    StateStoreException,
)
from datamining_skill.domain.models import ChunkMetadata, ChunkRecord, ChunkStatus
from datamining_skill.infrastructure.logging import STATE_LOGGER_NAME
from datamining_skill.infrastructure.paths import default_allowed_roots, resolve_within
from datamining_skill.infrastructure.permissions import (
    PRIVATE_FILE_MODE,
    ensure_private_directory,
    restrict_to_owner,
)

SCHEMA_VERSION = 2
_UNLIMITED_RETRIES = 2**62

SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id    INTEGER PRIMARY KEY,
    start_byte  INTEGER NOT NULL CHECK (start_byte >= 0),
    end_byte    INTEGER NOT NULL,
    status      TEXT    NOT NULL
                CHECK (status IN ('PENDING', 'IN_PROGRESS', 'COMPLETED', 'FAILED')),
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    updated_at  TEXT    NOT NULL,
    CHECK (end_byte > start_byte)
);
CREATE INDEX IF NOT EXISTS idx_chunks_status ON chunks (status, chunk_id);

-- Output-file length after each committed chunk (schema v2). Written in the same
-- transaction that marks the chunk COMPLETED; see ResultAggregator for how it makes
-- appends idempotent. Offsets only, never content.
CREATE TABLE IF NOT EXISTS chunk_commits (
    chunk_id   INTEGER PRIMARY KEY,
    output_end INTEGER NOT NULL CHECK (output_end >= 0)
);
"""

_SET_JOURNAL_MODE = "PRAGMA journal_mode=WAL"
_SET_SYNCHRONOUS = "PRAGMA synchronous=NORMAL"
_GET_JOURNAL_MODE = "PRAGMA journal_mode"
_GET_SYNCHRONOUS = "PRAGMA synchronous"
_GET_USER_VERSION = "PRAGMA user_version"
_SET_USER_VERSION = "PRAGMA user_version = 2"  # keep in sync with SCHEMA_VERSION
_BEGIN = "BEGIN IMMEDIATE"
_COMMIT = "COMMIT"
_ROLLBACK = "ROLLBACK"

_COUNT = "SELECT COUNT(*) FROM chunks"
_INSERT = (
    "INSERT INTO chunks (chunk_id, start_byte, end_byte, status, retry_count, updated_at) "
    "VALUES (?, ?, ?, ?, 0, ?)"
)
_PLAN_ROWS = "SELECT chunk_id, start_byte, end_byte FROM chunks ORDER BY chunk_id"
_SELECT_ONE = (
    "SELECT chunk_id, start_byte, end_byte, status, retry_count, updated_at "
    "FROM chunks WHERE chunk_id = ?"
)
_SELECT_NEXT = (
    "SELECT chunk_id, start_byte, end_byte, status, retry_count, updated_at "
    "FROM chunks WHERE status = ? ORDER BY chunk_id LIMIT 1"
)
_SELECT_ALL = (
    "SELECT chunk_id, start_byte, end_byte, status, retry_count, updated_at "
    "FROM chunks ORDER BY chunk_id"
)
_SELECT_BY_STATUS = (
    "SELECT chunk_id, start_byte, end_byte, status, retry_count, updated_at "
    "FROM chunks WHERE status = ? ORDER BY chunk_id"
)
_STATUS_OF = "SELECT status FROM chunks WHERE chunk_id = ?"
_SUMMARY = "SELECT status, COUNT(*) FROM chunks GROUP BY status"
_TRANSITION = (
    "UPDATE chunks SET status = ?, retry_count = retry_count + ?, updated_at = ? "
    "WHERE chunk_id = ? AND status = ?"
)
_RECOVER_ORPHANS = (
    "UPDATE chunks SET status = ?, retry_count = retry_count + 1, updated_at = ? "
    "WHERE status = ?"
)
_REQUEUE_FAILED = (
    "UPDATE chunks SET status = ?, updated_at = ? WHERE status = ? AND retry_count < ?"
)
_RECORD_COMMIT = "INSERT OR REPLACE INTO chunk_commits (chunk_id, output_end) VALUES (?, ?)"
_COMMITTED_OUTPUT_END = "SELECT MAX(output_end) FROM chunk_commits"
_PLAN_END = "SELECT MAX(end_byte) FROM chunks"


def _to_record(row: Sequence[Any]) -> ChunkRecord:
    return ChunkRecord(
        chunk_id=int(row[0]),
        start_byte=int(row[1]),
        end_byte=int(row[2]),
        status=ChunkStatus(row[3]),
        retry_count=int(row[4]),
        updated_at=datetime.fromisoformat(row[5]),
    )


class StateManager:
    """Durable chunk lifecycle: PENDING -> IN_PROGRESS -> COMPLETED | FAILED.

    Use it as a context manager so the connection is always released. Any other
    transition raises ``InvalidStateTransitionException``; COMPLETED is terminal:

        PENDING     -> IN_PROGRESS   mark_in_progress, claim_next_pending
        IN_PROGRESS -> COMPLETED     mark_completed
        IN_PROGRESS -> FAILED        mark_failed (retry_count + 1)
        IN_PROGRESS -> PENDING       orphan recovery (retry_count + 1)
        FAILED      -> PENDING       requeue_failed

    ``db_path`` must resolve inside ``allowed_roots`` (default: ``.scratch`` and ``data`` under
    the working directory). ``recover_orphans=False`` is for read-only observers. ``clock`` must
    return timezone-aware datetimes.
    """

    def __init__(
        self,
        db_path: str | os.PathLike[str],
        *,
        allowed_roots: Sequence[Path] | None = None,
        recover_orphans: bool = True,
        logger: logging.Logger | None = None,
        clock: Callable[[], datetime] | None = None,
        busy_timeout_seconds: float = 5.0,
    ) -> None:
        roots = allowed_roots or default_allowed_roots()
        self._path = resolve_within(db_path, roots, label="state database")
        self._logger = logger or logging.getLogger(STATE_LOGGER_NAME)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._conn: sqlite3.Connection | None = None
        self.recovered_orphans = 0

        ensure_private_directory(self._path.parent)
        if not self._path.exists():
            os.close(os.open(self._path, os.O_CREAT | os.O_WRONLY, PRIVATE_FILE_MODE))
            restrict_to_owner(self._path)

        try:
            # Autocommit mode: transactions are explicit, see _transaction().
            self._conn = sqlite3.connect(
                self._path, timeout=busy_timeout_seconds, isolation_level=None
            )
            self._configure()
            if recover_orphans:
                self.recovered_orphans = self.recover_orphans()
        except BaseException as exc:
            self.close()
            if isinstance(exc, sqlite3.Error):
                raise StateStoreException(
                    f"cannot open state database ({type(exc).__name__})"
                ) from exc
            raise
        self._emit(
            logging.INFO,
            "state.opened",
            file_name=self._path.name,
            recovered_orphans=self.recovered_orphans,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Release the connection and its file handles. Idempotent."""
        conn, self._conn = self._conn, None
        if conn is not None:
            conn.close()

    @property
    def closed(self) -> bool:
        return self._conn is None

    @property
    def path(self) -> Path:
        return self._path

    def is_initialized(self) -> bool:
        return self._scalar(_COUNT) > 0

    def initialize(self, chunks: Iterable[ChunkMetadata]) -> bool:
        """Store ``chunks`` as PENDING, consuming the iterable lazily.

        All-or-nothing: a crash or error part-way leaves the database empty. If a plan is
        already stored it is kept, with its progress, and ``chunks`` is checked against it
        row by row. Returns ``True`` if stored, ``False`` if an identical plan existed.
        Raises ``StateStoreException`` for an empty plan, duplicate ids, invalid ranges or a
        plan that differs from the stored one.
        """
        stamp = self._timestamp()
        with self._transaction() as conn:
            if conn.execute(_COUNT).fetchone()[0] > 0:
                self._verify_stored_plan(conn, chunks)
                return False
            conn.executemany(
                _INSERT,
                (
                    (chunk.chunk_id, chunk.start_byte, chunk.end_byte, ChunkStatus.PENDING.value, stamp)
                    for chunk in chunks
                ),
            )
            stored = conn.execute(_COUNT).fetchone()[0]
            if stored == 0:
                raise StateStoreException("cannot initialize state from an empty chunk plan")
        self._emit(logging.INFO, "state.initialized", file_name=self._path.name, chunk_count=stored)
        return True

    @staticmethod
    def _verify_stored_plan(conn: sqlite3.Connection, chunks: Iterable[ChunkMetadata]) -> None:
        mismatch = StateStoreException(
            "the chunk plan differs from the one stored in the state database; "
            "resume from the stored plan instead of re-planning"
        )
        stored = conn.execute(_PLAN_ROWS)
        try:
            for chunk in chunks:
                row = stored.fetchone()
                if row is None or tuple(row) != (chunk.chunk_id, chunk.start_byte, chunk.end_byte):
                    raise mismatch
            if stored.fetchone() is not None:
                raise mismatch
        finally:
            # Closed explicitly: a half-read cursor kept alive by the exception's traceback
            # would hold the database file open.
            stored.close()

    def next_pending(self) -> ChunkRecord | None:
        """The lowest-numbered PENDING chunk, without changing it."""
        rows = self._fetch(_SELECT_NEXT, (ChunkStatus.PENDING.value,))
        return _to_record(rows[0]) if rows else None

    def get(self, chunk_id: int) -> ChunkRecord:
        rows = self._fetch(_SELECT_ONE, (chunk_id,))
        if not rows:
            raise StateStoreException(f"unknown chunk {chunk_id}")
        return _to_record(rows[0])

    def records(self, status: ChunkStatus | None = None) -> Generator[ChunkRecord, None, None]:
        """Iterate chunks in id order, optionally only those with ``status``."""
        conn = self._connection()
        try:
            if status is None:
                cursor = conn.execute(_SELECT_ALL)
            else:
                cursor = conn.execute(_SELECT_BY_STATUS, (status.value,))
            try:
                for row in cursor:
                    yield _to_record(row)
            finally:
                cursor.close()
        except sqlite3.Error as exc:
            raise StateStoreException(f"state query failed ({type(exc).__name__})") from exc

    def summary(self) -> dict[ChunkStatus, int]:
        """Chunk count per status; every status is present, zero if none."""
        counts = dict.fromkeys(ChunkStatus, 0)
        for status, count in self._fetch(_SUMMARY, ()):
            counts[ChunkStatus(status)] = int(count)
        return counts

    def is_complete(self) -> bool:
        counts = self.summary()
        total = sum(counts.values())
        return total > 0 and counts[ChunkStatus.COMPLETED] == total

    def diagnostics(self) -> dict[str, str | int]:
        journal = self._fetch(_GET_JOURNAL_MODE, ())[0][0]
        synchronous = self._fetch(_GET_SYNCHRONOUS, ())[0][0]
        version = self._fetch(_GET_USER_VERSION, ())[0][0]
        return {
            "journal_mode": str(journal),
            "synchronous": int(synchronous),  # 1 == NORMAL
            "schema_version": int(version),
        }

    def claim_next_pending(self) -> ChunkRecord | None:
        """Select the next PENDING chunk and mark it IN_PROGRESS in one transaction."""
        stamp = self._timestamp()
        with self._transaction() as conn:
            row = conn.execute(_SELECT_NEXT, (ChunkStatus.PENDING.value,)).fetchone()
            if row is None:
                return None
            record = _to_record(row)
            conn.execute(
                _TRANSITION,
                (ChunkStatus.IN_PROGRESS.value, 0, stamp, record.chunk_id, ChunkStatus.PENDING.value),
            )
        self._emit(logging.DEBUG, "state.claimed", chunk_id=record.chunk_id)
        return replace(record, status=ChunkStatus.IN_PROGRESS, updated_at=datetime.fromisoformat(stamp))

    def mark_in_progress(self, chunk_id: int) -> None:
        self._transition(chunk_id, ChunkStatus.PENDING, ChunkStatus.IN_PROGRESS)

    def mark_completed(self, chunk_id: int, output_end: int | None = None) -> None:
        """Mark a chunk COMPLETED, recording ``output_end`` in the same transaction.

        That pairing is the crash-safety contract: after any crash, "COMPLETED" and "the
        output is committed up to byte N" either both hold or neither does.
        """
        self._transition(
            chunk_id, ChunkStatus.IN_PROGRESS, ChunkStatus.COMPLETED, commit_output_end=output_end
        )

    def committed_output_end(self) -> int | None:
        """Output length after the latest committed chunk, or ``None`` if none is committed."""
        latest_end = self._fetch(_COMMITTED_OUTPUT_END, ())[0][0]
        return None if latest_end is None else int(latest_end)

    def plan_end_byte(self) -> int | None:
        """End of the stored plan, i.e. the source size it was made for."""
        planned_end = self._fetch(_PLAN_END, ())[0][0]
        return None if planned_end is None else int(planned_end)

    def mark_failed(self, chunk_id: int) -> None:
        self._transition(chunk_id, ChunkStatus.IN_PROGRESS, ChunkStatus.FAILED, retry_increment=1)

    def requeue_failed(self, max_retries: int | None = None) -> int:
        """Return FAILED chunks with ``retry_count < max_retries`` to PENDING; give the count."""
        limit = _UNLIMITED_RETRIES if max_retries is None else max_retries
        stamp = self._timestamp()
        with self._transaction() as conn:
            changed = conn.execute(
                _REQUEUE_FAILED,
                (ChunkStatus.PENDING.value, stamp, ChunkStatus.FAILED.value, limit),
            ).rowcount
        self._emit(logging.INFO, "state.requeued", chunk_count=changed)
        return int(changed)

    def recover_orphans(self) -> int:
        """Revert IN_PROGRESS chunks to PENDING, counting the abandoned attempt.

        Runs on open: a chunk still IN_PROGRESS then belongs to a process that died.
        """
        stamp = self._timestamp()
        with self._transaction() as conn:
            recovered = int(
                conn.execute(
                    _RECOVER_ORPHANS,
                    (ChunkStatus.PENDING.value, stamp, ChunkStatus.IN_PROGRESS.value),
                ).rowcount
            )
        if recovered:
            self._emit(
                logging.WARNING,
                "state.orphans_recovered",
                file_name=self._path.name,
                chunk_count=recovered,
            )
        return recovered

    def _transition(
        self,
        chunk_id: int,
        expected: ChunkStatus,
        new: ChunkStatus,
        retry_increment: int = 0,
        commit_output_end: int | None = None,
    ) -> None:
        stamp = self._timestamp()
        with self._transaction() as conn:
            # Compare-and-set: matches only if the chunk is still in `expected`.
            changed = conn.execute(
                _TRANSITION, (new.value, retry_increment, stamp, chunk_id, expected.value)
            ).rowcount
            if changed != 1:
                row = conn.execute(_STATUS_OF, (chunk_id,)).fetchone()
                if row is None:
                    raise StateStoreException(f"unknown chunk {chunk_id}")
                raise InvalidStateTransitionException(chunk_id, str(row[0]), new.value)
            if commit_output_end is not None:
                conn.execute(_RECORD_COMMIT, (chunk_id, commit_output_end))
        self._emit(logging.DEBUG, "state.transition", chunk_id=chunk_id, status=new.value)

    def _configure(self) -> None:
        conn = self._connection()
        mode = conn.execute(_SET_JOURNAL_MODE).fetchone()
        if mode is None or str(mode[0]).lower() != "wal":
            raise StateStoreException(
                "write-ahead logging could not be enabled (is the file on a network drive?)"
            )
        conn.execute(_SET_SYNCHRONOUS)

        version = conn.execute(_GET_USER_VERSION).fetchone()[0]
        if version not in range(SCHEMA_VERSION + 1):
            raise StateStoreException(
                f"unsupported state database schema version {version} "
                f"(this release supports up to {SCHEMA_VERSION})"
            )
        if version < SCHEMA_VERSION:
            # Every statement is IF NOT EXISTS, so the DDL both creates a fresh database (0)
            # and migrates an old one (1 gains chunk_commits).
            conn.executescript(SCHEMA_DDL)
            conn.execute(_SET_USER_VERSION)

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise StateStoreException("the state manager is closed")
        return self._conn

    def _timestamp(self) -> str:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise StateStoreException("the clock must return timezone-aware datetimes")
        return now.astimezone(UTC).isoformat(timespec="milliseconds")

    def _fetch(self, sql: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
        try:
            return [tuple(row) for row in self._connection().execute(sql, params).fetchall()]
        except sqlite3.Error as exc:
            raise StateStoreException(f"state query failed ({type(exc).__name__})") from exc

    def _scalar(self, sql: str) -> int:
        return int(self._fetch(sql, ())[0][0])

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """Commit on success, roll back on any exception (including KeyboardInterrupt)."""
        conn = self._connection()
        try:
            conn.execute(_BEGIN)
        except sqlite3.Error as exc:
            raise StateStoreException(f"state transaction failed ({type(exc).__name__})") from exc
        try:
            yield conn
            conn.execute(_COMMIT)
        except BaseException as exc:
            if conn.in_transaction:
                conn.execute(_ROLLBACK)
            if isinstance(exc, sqlite3.Error):
                raise StateStoreException(
                    f"state transaction failed ({type(exc).__name__})"
                ) from exc
            raise

    def _emit(self, level: int, event: str, **payload: Any) -> None:
        emit_event(self._logger, level, event, **payload)
