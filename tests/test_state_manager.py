"""Tests for the crash-safe :class:`StateManager`.

Teardown is strict by design: every manager opened through ``open_state`` is closed,
then the database directory is removed *without* ``ignore_errors``. On Windows a
dangling connection or lock keeps the files open and fails that removal, so a leak
shows up as a test error instead of passing silently.
"""

from __future__ import annotations

import ast
import io
import json
import logging
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable, Iterator
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from datamining_skill import (
    ChunkMetadata,
    ChunkStatus,
    InvalidConfigurationException,
    InvalidStateTransitionException,
    StateManager,
    StateStoreException,
    create_chunking_engine,
    create_profiler,
)
from datamining_skill.application.config import MIB
from datamining_skill.infrastructure import JsonLogFormatter
from tests.conftest import SCRATCH_ROOT, OpenState, WriteFile
from tests.test_chunking_engine import SCALED, FakeMemory

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_MODULE = PROJECT_ROOT / "src" / "datamining_skill" / "infrastructure" / "state_manager.py"


def make_chunks(count: int) -> list[ChunkMetadata]:
    return [ChunkMetadata(i, (i - 1) * 100, i * 100, estimated_records=5) for i in range(1, count + 1)]


class StepClock:
    """Deterministic UTC clock advancing one second per call."""

    def __init__(self) -> None:
        self._now = datetime(2025, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        self._now += timedelta(seconds=1)
        return self._now




def test_ten_chunks_are_initialized_as_pending(open_state: OpenState) -> None:
    state = open_state()
    assert state.is_initialized() is False

    assert state.initialize(make_chunks(10)) is True

    records = list(state.records())
    assert [r.chunk_id for r in records] == list(range(1, 11))
    assert all(r.status is ChunkStatus.PENDING and r.retry_count == 0 for r in records)
    assert (records[2].start_byte, records[2].end_byte) == (200, 300)
    assert state.summary() == {
        ChunkStatus.PENDING: 10,
        ChunkStatus.IN_PROGRESS: 0,
        ChunkStatus.COMPLETED: 0,
        ChunkStatus.FAILED: 0,
    }


def test_initialize_consumes_a_generator_lazily(open_state: OpenState) -> None:
    state = open_state()
    consumed: list[int] = []

    def plan() -> Iterator[ChunkMetadata]:
        for chunk in make_chunks(25):
            consumed.append(chunk.chunk_id)
            yield chunk

    state.initialize(plan())

    assert consumed == list(range(1, 26))
    assert state.summary()[ChunkStatus.PENDING] == 25


def test_a_real_chunking_plan_can_be_stored(open_state: OpenState, write_file: WriteFile) -> None:
    rows = "".join(f"{i},value-{i}\n" for i in range(150_000))
    path = write_file("data.csv", "id,v\n" + rows)  # ~2 MiB
    profile = create_profiler().profile(path)
    engine = create_chunking_engine(SCALED, memory_provider=FakeMemory(4 * MIB))
    expected = list(engine.plan(path, profile))
    state = open_state()

    state.initialize(engine.plan(path, profile))  # generator straight into the store

    stored = list(state.records())
    assert len(stored) == len(expected) > 1
    assert [(r.chunk_id, r.start_byte, r.end_byte) for r in stored] == [
        (c.chunk_id, c.start_byte, c.end_byte) for c in expected
    ]


def test_reinitializing_the_same_plan_preserves_progress(open_state: OpenState) -> None:
    state = open_state()
    state.initialize(make_chunks(5))
    state.claim_next_pending()
    state.mark_completed(1)

    assert state.initialize(make_chunks(5)) is False

    assert state.get(1).status is ChunkStatus.COMPLETED


@pytest.mark.parametrize("other", [make_chunks(4), make_chunks(6), [ChunkMetadata(1, 0, 99)] + make_chunks(5)[1:]])
def test_a_different_plan_is_rejected_and_progress_is_kept(
    open_state: OpenState, other: list[ChunkMetadata]
) -> None:
    state = open_state()
    state.initialize(make_chunks(5))
    state.claim_next_pending()
    state.mark_completed(1)

    with pytest.raises(StateStoreException, match="differs"):
        state.initialize(other)

    assert state.get(1).status is ChunkStatus.COMPLETED
    assert state.summary()[ChunkStatus.PENDING] == 4


def test_failed_initialization_is_atomic(open_state: OpenState) -> None:
    state = open_state()

    def exploding_plan() -> Iterator[ChunkMetadata]:
        yield from make_chunks(5)
        raise RuntimeError("process interrupted mid-plan")

    with pytest.raises(RuntimeError):
        state.initialize(exploding_plan())

    assert state.is_initialized() is False  # nothing half-written
    assert state.initialize(make_chunks(5)) is True  # and a clean retry works


def test_duplicate_ids_and_invalid_ranges_roll_back(open_state: OpenState) -> None:
    state = open_state()

    with pytest.raises(StateStoreException):
        state.initialize([*make_chunks(3), ChunkMetadata(2, 500, 600)])
    with pytest.raises(StateStoreException):
        state.initialize([ChunkMetadata(1, 100, 100)])  # empty range violates the CHECK
    with pytest.raises(StateStoreException, match="empty"):
        state.initialize([])

    assert state.is_initialized() is False




def test_full_lifecycle_and_next_pending_order(open_state: OpenState) -> None:
    state = open_state(clock=StepClock())
    state.initialize(make_chunks(3))

    peeked = state.next_pending()
    assert peeked is not None and peeked.chunk_id == 1
    assert state.next_pending() == peeked  # peeking changes nothing

    state.mark_in_progress(1)
    state.mark_completed(1)
    state.mark_in_progress(2)
    state.mark_failed(2)

    assert state.get(1).status is ChunkStatus.COMPLETED
    assert (state.get(2).status, state.get(2).retry_count) == (ChunkStatus.FAILED, 1)
    next_chunk = state.next_pending()
    assert next_chunk is not None and next_chunk.chunk_id == 3


def test_claim_next_pending_is_atomic_and_exhausts(open_state: OpenState) -> None:
    state = open_state()
    state.initialize(make_chunks(3))

    claimed = [state.claim_next_pending() for _ in range(4)]

    assert [c.chunk_id if c else None for c in claimed] == [1, 2, 3, None]
    assert all(c.status is ChunkStatus.IN_PROGRESS for c in claimed if c)
    assert state.get(2).status is ChunkStatus.IN_PROGRESS
    assert state.next_pending() is None


def test_two_connections_never_claim_the_same_chunk(open_state: OpenState) -> None:
    first = open_state()
    first.initialize(make_chunks(6))
    second = open_state(recover_orphans=False)  # a second live worker must not steal work

    claimed = []
    for _ in range(3):
        claimed.append(first.claim_next_pending())
        claimed.append(second.claim_next_pending())

    ids = [c.chunk_id for c in claimed if c]
    assert sorted(ids) == [1, 2, 3, 4, 5, 6]
    assert len(set(ids)) == 6


@pytest.mark.parametrize(
    ("setup", "action"),
    [
        ([], "mark_completed"),  # PENDING -> COMPLETED skips IN_PROGRESS
        ([], "mark_failed"),  # PENDING -> FAILED
        (["mark_in_progress"], "mark_in_progress"),  # double claim
        (["mark_in_progress", "mark_completed"], "mark_in_progress"),  # COMPLETED is terminal
        (["mark_in_progress", "mark_completed"], "mark_failed"),
        (["mark_in_progress", "mark_completed"], "mark_completed"),
        (["mark_in_progress", "mark_failed"], "mark_completed"),  # FAILED must be requeued first
    ],
)
def test_illegal_transitions_are_rejected_and_change_nothing(
    open_state: OpenState, setup: list[str], action: str
) -> None:
    state = open_state(clock=StepClock())
    state.initialize(make_chunks(1))
    for step in setup:
        getattr(state, step)(1)
    before = state.get(1)

    with pytest.raises(InvalidStateTransitionException) as caught:
        getattr(state, action)(1)

    assert state.get(1) == before  # rolled back: status, retry_count and timestamp intact
    assert caught.value.chunk_id == 1
    assert caught.value.current == before.status.value


def test_stale_view_cannot_overwrite_newer_state(open_state: OpenState) -> None:
    """Compare-and-set: the second of two racing writers loses cleanly."""
    first = open_state()
    first.initialize(make_chunks(1))
    second = open_state(recover_orphans=False)

    first.mark_in_progress(1)
    with pytest.raises(InvalidStateTransitionException):
        second.mark_in_progress(1)

    assert first.get(1).status is ChunkStatus.IN_PROGRESS


def test_unknown_chunk_is_reported_distinctly(open_state: OpenState) -> None:
    state = open_state()
    state.initialize(make_chunks(2))

    for call in (state.mark_in_progress, state.mark_completed, state.mark_failed, state.get):
        with pytest.raises(StateStoreException, match="unknown chunk 99") as caught:
            call(99)
        assert not isinstance(caught.value, InvalidStateTransitionException)


def test_failed_chunks_can_be_requeued_up_to_a_retry_limit(open_state: OpenState) -> None:
    state = open_state()
    state.initialize(make_chunks(2))
    for _ in range(2):  # chunk 1 fails twice, chunk 2 once
        state.mark_in_progress(1)
        state.mark_failed(1)
        if state.get(1).retry_count < 2:
            state.requeue_failed()
    state.mark_in_progress(2)
    state.mark_failed(2)

    assert state.requeue_failed(max_retries=2) == 1  # only chunk 2 (1 retry) is below the limit
    assert state.get(2).status is ChunkStatus.PENDING
    assert state.get(1).status is ChunkStatus.FAILED
    assert state.requeue_failed() == 1  # unlimited
    assert state.get(1).status is ChunkStatus.PENDING


def test_records_filter_summary_and_completion(open_state: OpenState) -> None:
    state = open_state()
    state.initialize(make_chunks(3))
    assert state.is_complete() is False

    while (chunk := state.claim_next_pending()) is not None:
        state.mark_completed(chunk.chunk_id)

    assert state.is_complete() is True
    assert [r.chunk_id for r in state.records(ChunkStatus.COMPLETED)] == [1, 2, 3]
    assert list(state.records(ChunkStatus.PENDING)) == []
    assert state.summary()[ChunkStatus.COMPLETED] == 3




def test_in_progress_chunk_reverts_to_pending_after_crash(
    open_state: OpenState,
) -> None:
    first = open_state()
    first.initialize(make_chunks(10))
    first.mark_in_progress(1)
    # "Crash": `first` is abandoned without close() or any cleanup; a new process starts.

    second = open_state()

    assert second.recovered_orphans == 1
    recovered = second.get(1)
    assert recovered.status is ChunkStatus.PENDING
    assert recovered.retry_count == 1  # the abandoned attempt is counted
    claimed = second.claim_next_pending()
    assert claimed is not None and claimed.chunk_id == 1  # picked up again first
    assert second.summary()[ChunkStatus.PENDING] == 9


def test_recovery_only_touches_in_progress_chunks(open_state: OpenState) -> None:
    state = open_state(clock=StepClock())
    state.initialize(make_chunks(5))
    for chunk_id in (1, 2, 3):
        state.mark_in_progress(chunk_id)
    state.mark_completed(1)
    state.mark_failed(2)
    snapshot = {i: state.get(i) for i in (1, 2, 4, 5)}

    restarted = open_state("state.sqlite3")

    assert restarted.recovered_orphans == 1  # only chunk 3
    assert restarted.get(3).status is ChunkStatus.PENDING
    for chunk_id, before in snapshot.items():
        assert restarted.get(chunk_id) == before


def test_recovery_can_be_disabled_for_observers(open_state: OpenState) -> None:
    worker = open_state()
    worker.initialize(make_chunks(2))
    worker.mark_in_progress(1)

    observer = open_state(recover_orphans=False)

    assert observer.recovered_orphans == 0
    assert observer.get(1).status is ChunkStatus.IN_PROGRESS


def test_crash_resume_end_to_end_with_a_real_hard_exit() -> None:
    """Runs the validation script: 100 chunks, process killed at chunk 50, then resumed."""
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "simulate_crash_resume.py")],
        capture_output=True,
        text=True,
        check=False,
        cwd=PROJECT_ROOT,
        timeout=120,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "resumed at chunk 50" in result.stdout
    assert "PASS" in result.stdout




def test_completed_status_persists_across_clean_restart(open_state: OpenState) -> None:
    state = open_state()
    state.initialize(make_chunks(4))
    state.claim_next_pending()
    state.mark_completed(1)
    state.close()

    reopened = open_state()

    assert reopened.get(1).status is ChunkStatus.COMPLETED
    assert reopened.recovered_orphans == 0
    assert reopened.summary()[ChunkStatus.COMPLETED] == 1


def test_completed_status_survives_a_crash_without_close(open_state: OpenState) -> None:
    state = open_state()
    state.initialize(make_chunks(4))
    state.claim_next_pending()
    state.mark_completed(1)
    state.claim_next_pending()  # chunk 2 in flight when the "crash" happens

    restarted = open_state()

    assert restarted.get(1).status is ChunkStatus.COMPLETED  # permanent
    assert restarted.get(2).status is ChunkStatus.PENDING  # orphan reverted
    assert restarted.get(1).retry_count == 0


def test_resume_does_not_touch_completed_chunks(open_state: OpenState) -> None:
    state = open_state(clock=StepClock())
    state.initialize(make_chunks(10))
    for _ in range(4):
        chunk = state.claim_next_pending()
        assert chunk is not None
        state.mark_completed(chunk.chunk_id)
    done_before = [state.get(i) for i in range(1, 5)]
    state.claim_next_pending()  # chunk 5 in flight

    resumed = open_state()
    while (chunk := resumed.claim_next_pending()) is not None:
        assert chunk.chunk_id >= 5  # never hands out finished work again
        resumed.mark_completed(chunk.chunk_id)

    assert [resumed.get(i) for i in range(1, 5)] == done_before
    assert resumed.is_complete()




def test_wal_and_normal_synchronous_are_active(open_state: OpenState, state_dir: Path) -> None:
    state = open_state()

    assert state.diagnostics() == {"journal_mode": "wal", "synchronous": 1, "schema_version": 2}
    with closing(sqlite3.connect(state_dir / "state.sqlite3")) as raw:
        assert raw.execute("PRAGMA journal_mode").fetchone()[0] == "wal"  # persisted in the file


def test_schema_stores_metadata_columns_only(open_state: OpenState, state_dir: Path) -> None:
    state = open_state()
    state.initialize(make_chunks(1))

    with closing(sqlite3.connect(state_dir / "state.sqlite3")) as raw:
        tables = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        columns = [r[1] for r in raw.execute("PRAGMA table_info(chunks)")]
        commit_columns = [r[1] for r in raw.execute("PRAGMA table_info(chunk_commits)")]

    assert tables == {"chunks", "chunk_commits"}
    assert columns == ["chunk_id", "start_byte", "end_byte", "status", "retry_count", "updated_at"]
    assert commit_columns == ["chunk_id", "output_end"]  # offsets only, never content


def test_version_1_database_is_migrated_in_place(state_dir: Path) -> None:
    path = state_dir / "old.sqlite3"
    with closing(sqlite3.connect(path)) as raw:
        raw.executescript(
            """
            CREATE TABLE chunks (
                chunk_id INTEGER PRIMARY KEY, start_byte INTEGER NOT NULL, end_byte INTEGER NOT NULL,
                status TEXT NOT NULL, retry_count INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL
            );
            INSERT INTO chunks VALUES (1, 0, 100, 'COMPLETED', 0, '2025-01-01T00:00:00.000+00:00');
            PRAGMA user_version = 1;
            """
        )

    with StateManager(path, allowed_roots=[SCRATCH_ROOT]) as state:
        assert state.diagnostics()["schema_version"] == 2
        assert state.get(1).status is ChunkStatus.COMPLETED  # existing progress kept
        assert state.committed_output_end() is None




def test_completion_and_output_offset_are_recorded_atomically(open_state: OpenState) -> None:
    state = open_state()
    state.initialize(make_chunks(3))
    assert state.committed_output_end() is None

    state.claim_next_pending()
    state.mark_completed(1, output_end=120)
    state.claim_next_pending()
    state.mark_completed(2, output_end=305)

    assert state.committed_output_end() == 305
    assert state.plan_end_byte() == 300  # the plan's extent: end of the last chunk


def test_a_rejected_completion_does_not_record_an_offset(open_state: OpenState) -> None:
    state = open_state()
    state.initialize(make_chunks(2))

    with pytest.raises(InvalidStateTransitionException):
        state.mark_completed(1, output_end=999)  # chunk 1 is still PENDING

    assert state.committed_output_end() is None  # rolled back together with the status


def test_committed_offset_survives_a_crash(open_state: OpenState) -> None:
    first = open_state()
    first.initialize(make_chunks(3))
    first.claim_next_pending()
    first.mark_completed(1, output_end=77)
    first.claim_next_pending()  # chunk 2 in flight; "crash"

    restarted = open_state()

    assert restarted.committed_output_end() == 77
    assert restarted.get(2).status is ChunkStatus.PENDING


def test_timestamps_are_utc_and_follow_the_injected_clock(open_state: OpenState) -> None:
    state = open_state(clock=StepClock())
    state.initialize(make_chunks(1))
    created = state.get(1).updated_at
    state.mark_in_progress(1)
    changed = state.get(1).updated_at

    assert created.utcoffset() == timedelta(0)
    assert changed - created == timedelta(seconds=1)
    assert state.get(1).to_dict()["updated_at"].endswith("+00:00")


def test_non_utc_clock_values_are_normalised_to_utc(open_state: OpenState) -> None:
    plus_three = timezone(timedelta(hours=3))
    state = open_state(clock=lambda: datetime(2025, 6, 1, 12, 0, tzinfo=plus_three))

    state.initialize(make_chunks(1))

    assert state.get(1).updated_at == datetime(2025, 6, 1, 9, 0, tzinfo=UTC)
    assert state.get(1).updated_at.utcoffset() == timedelta(0)


def test_naive_clock_is_refused_and_no_handle_leaks(state_dir: Path) -> None:
    with pytest.raises(StateStoreException, match="timezone-aware"):
        StateManager(
            state_dir / "naive.sqlite3",
            allowed_roots=[SCRATCH_ROOT],
            clock=lambda: datetime(2025, 1, 1),  # noqa: DTZ001 - deliberately naive
        )
    (state_dir / "naive.sqlite3").unlink()  # fails on Windows if the connection leaked


def test_context_manager_closes_even_when_the_body_raises(state_dir: Path) -> None:
    path = state_dir / "ctx.sqlite3"
    holder: list[StateManager] = []

    with pytest.raises(RuntimeError):
        with StateManager(path, allowed_roots=[SCRATCH_ROOT]) as state:
            holder.append(state)
            state.initialize(make_chunks(2))
            raise RuntimeError("worker blew up")

    manager = holder[0]
    assert manager.closed
    with pytest.raises(StateStoreException, match="closed"):
        manager.next_pending()
    manager.close()  # idempotent
    # WAL side files are folded back in and removed by a clean close.
    assert sorted(p.name for p in state_dir.iterdir()) == ["ctx.sqlite3"]


def test_unwritten_handle_leaves_nothing_locked_after_failure(open_state: OpenState) -> None:
    state = open_state()
    state.initialize(make_chunks(2))
    with pytest.raises(InvalidStateTransitionException):
        state.mark_completed(1)

    # The failed call rolled back; another connection can write immediately.
    other = open_state(recover_orphans=False)
    other.mark_in_progress(1)
    assert other.get(1).status is ChunkStatus.IN_PROGRESS


def test_foreign_schema_version_is_refused(open_state: OpenState, state_dir: Path) -> None:
    open_state().close()
    with closing(sqlite3.connect(state_dir / "state.sqlite3")) as raw:
        raw.execute("PRAGMA user_version = 99")

    with pytest.raises(StateStoreException, match="schema version 99"):
        open_state()


def test_non_database_file_is_refused_cleanly(state_dir: Path) -> None:
    bogus = state_dir / "bogus.sqlite3"
    bogus.write_bytes(b"this is not a sqlite database " * 50)

    with pytest.raises(StateStoreException):
        StateManager(bogus, allowed_roots=[SCRATCH_ROOT])

    bogus.unlink()  # no handle left behind




def test_database_outside_allowed_directories_is_refused(state_dir: Path) -> None:
    system_tmp = Path(tempfile.gettempdir()) / f"state-{uuid.uuid4().hex}.sqlite3"
    traversal = state_dir / ".." / "escaped.sqlite3"
    outside_root = PROJECT_ROOT / "not-allowed" / "state.sqlite3"

    for candidate in (system_tmp, traversal, outside_root, Path(":memory:")):
        with pytest.raises(InvalidConfigurationException, match="inside one of"):
            StateManager(candidate, allowed_roots=[state_dir])

    # Refusal happens before anything is created on disk.
    assert not system_tmp.exists()
    assert not outside_root.parent.exists()
    assert not (state_dir.parent / "escaped.sqlite3").exists()


def test_directories_and_the_root_itself_are_refused(state_dir: Path) -> None:
    (state_dir / "sub").mkdir()
    with pytest.raises(InvalidConfigurationException):
        StateManager(state_dir, allowed_roots=[state_dir])
    with pytest.raises(InvalidConfigurationException, match="directory"):
        StateManager(state_dir / "sub", allowed_roots=[state_dir])


def test_default_roots_are_scratch_and_data_under_cwd(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(state_dir)

    with StateManager(state_dir / "data" / "run.sqlite3") as inside_data:
        assert inside_data.path.parent.name == "data"
    with StateManager(state_dir / ".scratch" / "run.sqlite3") as inside_scratch:
        assert inside_scratch.path.parent.name == ".scratch"
    with pytest.raises(InvalidConfigurationException):
        StateManager(state_dir / "other" / "run.sqlite3")
    with pytest.raises(InvalidConfigurationException):
        StateManager(Path(tempfile.gettempdir()) / "run.sqlite3")


def test_every_sql_statement_is_parameterised() -> None:
    """Static guard: executed SQL must be a module-level constant, never built at runtime."""
    tree = ast.parse(STATE_MODULE.read_text(encoding="utf-8"))
    constants = {
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert {"_INSERT", "_TRANSITION", "SCHEMA_DDL"} <= constants

    executed = 0
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        method = node.func.attr
        if method in {"execute", "executemany", "executescript"}:
            first = node.args[0]
            # `sql` is the parameter of the internal _fetch helper, audited below.
            assert isinstance(first, ast.Name) and (first.id in constants or first.id == "sql"), (
                f"line {node.lineno}: SQL must be a module-level string constant"
            )
            executed += 1
        elif method in {"_fetch", "_scalar"}:
            first = node.args[0]
            # `_scalar` forwards its own `sql` parameter; its callers are checked here too.
            assert isinstance(first, ast.Name) and (first.id in constants or first.id == "sql"), (
                f"line {node.lineno}: helper called with non-constant SQL"
            )
    assert executed >= 15
    # No string formatting or concatenation anywhere near SQL: no f-strings in the module.
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.JoinedStr) and _mentions_sql(n)]


def _mentions_sql(node: ast.JoinedStr) -> bool:
    text = " ".join(
        part.value for part in node.values if isinstance(part, ast.Constant) and isinstance(part.value, str)
    ).upper()
    return any(keyword in text for keyword in ("SELECT ", "UPDATE ", "INSERT ", "DELETE ", "PRAGMA "))


def test_hostile_values_cannot_alter_the_database(open_state: OpenState) -> None:
    state = open_state()
    state.initialize(make_chunks(2))

    with pytest.raises(StateStoreException):
        state.get("1; DROP TABLE chunks; --")  # type: ignore[arg-type]

    assert state.is_initialized()
    assert state.summary()[ChunkStatus.PENDING] == 2




def test_events_are_structured_and_contain_no_paths(state_dir: Path) -> None:
    stream = io.StringIO()
    logger = logging.getLogger("tests.state.events")
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    path = state_dir / "events.sqlite3"
    try:
        with StateManager(path, allowed_roots=[SCRATCH_ROOT], logger=logger) as state:
            state.initialize(make_chunks(3))
            state.mark_in_progress(1)
        with StateManager(path, allowed_roots=[SCRATCH_ROOT], logger=logger):
            pass
    finally:
        logger.removeHandler(handler)

    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [e["event"] for e in events] == [
        "state.opened",
        "state.initialized",
        "state.orphans_recovered",  # second start finds chunk 1 abandoned
        "state.opened",
    ]
    recovered = next(e for e in events if e["event"] == "state.orphans_recovered")
    assert recovered["level"] == "WARNING" and recovered["data"]["chunk_count"] == 1
    assert str(state_dir) not in stream.getvalue()
