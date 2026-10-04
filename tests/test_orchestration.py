"""Tests for scratch/output storage, the atomic merge (TMP -> APPEND) and the orchestrated loop."""

from __future__ import annotations

import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, BinaryIO, cast

import pytest

from datamining_skill import (
    ChunkingConfig,
    ChunkMetadata,
    ChunkResult,
    ChunkStatus,
    InvalidConfigurationException,
    MiningOrchestrator,
    MiningProgress,
    OrchestratorConfig,
    OutputIntegrityException,
    RegexExtractor,
    ResultAggregator,
    StateManager,
    StateStoreException,
    UnsupportedDataFormatException,
    create_orchestrator,
)
from datamining_skill.application.extraction_strategy import EMAIL_PATTERN
from datamining_skill.domain.ports import ChunkStateStore, StreamReader
from datamining_skill.infrastructure import FileStreamReader, JsonLogFormatter
from datamining_skill.infrastructure.output_sink import LocalOutputSink
from datamining_skill.infrastructure.scratch import LocalScratchStore
from tests.conftest import SCRATCH_ROOT, OpenState, WriteFile
from tests.support import SIMULATION_SCRIPT, load_simulation

sim = load_simulation()
CRASH_CHUNK = 3
SCALED = ChunkingConfig(
    critical_available_bytes=1 << 20, min_chunk_bytes=64 << 10, fallback_available_bytes=1 << 20
)


class Crash(BaseException):
    """Stands in for ``sys.exit()`` / a kill: not an ``Exception``, so nothing may swallow it."""


def die() -> None:
    raise Crash




def test_scratch_paths_are_built_only_from_integer_chunk_ids(state_dir: Path) -> None:
    scratch = LocalScratchStore(state_dir, allowed_roots=[SCRATCH_ROOT])

    assert scratch.tmp_path(7) == scratch.directory / "chunk_7.tmp"
    for hostile in ("../../etc/passwd", "1/../2", -1, True, 1.5, None):
        with pytest.raises(ValueError, match="chunk_id"):
            scratch.tmp_path(hostile)  # type: ignore[arg-type]


def test_scratch_directory_must_be_inside_allowed_roots(state_dir: Path) -> None:
    with pytest.raises(InvalidConfigurationException, match="inside one of"):
        LocalScratchStore(Path(tempfile.gettempdir()) / f"scratch-{uuid.uuid4().hex}", allowed_roots=[state_dir])
    with pytest.raises(InvalidConfigurationException, match="inside one of"):
        LocalScratchStore(state_dir / ".." / "elsewhere", allowed_roots=[state_dir])
    assert LocalScratchStore(state_dir, allowed_roots=[state_dir]).directory == state_dir.resolve()


def test_default_scratch_root_is_the_dot_scratch_directory(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(state_dir)

    assert LocalScratchStore(state_dir / ".scratch").directory.name == ".scratch"
    with pytest.raises(InvalidConfigurationException):
        LocalScratchStore(state_dir / "somewhere-else")


def test_symlinked_scratch_file_is_refused(state_dir: Path) -> None:
    scratch = LocalScratchStore(state_dir, allowed_roots=[SCRATCH_ROOT])
    victim = state_dir / "victim.txt"
    victim.write_text("retained export")
    try:
        os.symlink(victim, scratch.tmp_path(1))
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not available to this user")

    with pytest.raises(InvalidConfigurationException, match="symbolic link"):
        with scratch.open_tmp(1):
            pass
    assert victim.read_text() == "retained export"


def test_open_tmp_truncates_and_clear_stale_keeps_others(state_dir: Path) -> None:
    scratch = LocalScratchStore(state_dir, allowed_roots=[SCRATCH_ROOT])
    scratch.tmp_path(1).write_bytes(b"old partial content")
    with scratch.open_tmp(1) as handle:
        handle.write(b"new")
    scratch.tmp_path(2).write_bytes(b"x")
    for keep in ("notes.txt", "chunk_x.tmp", "chunk_3.tmp.bak"):
        (state_dir / keep).write_text("keep me")

    assert scratch.tmp_path(1).read_bytes() == b"new"
    assert scratch.clear_stale() == 2
    assert sorted(p.name for p in state_dir.iterdir()) == ["chunk_3.tmp.bak", "chunk_x.tmp", "notes.txt"]
    scratch.discard_tmp(99)  # missing is fine


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_scratch_and_output_files_are_owner_only(state_dir: Path) -> None:
    scratch = LocalScratchStore(state_dir, allowed_roots=[SCRATCH_ROOT])
    with scratch.open_tmp(1):
        pass
    sink = LocalOutputSink(state_dir / "out.csv")
    sink.reset(b"h\n")

    assert scratch.tmp_path(1).stat().st_mode & 0o777 == 0o600
    assert sink.path.stat().st_mode & 0o777 == 0o600




def test_sink_primitives_reset_append_and_truncate(state_dir: Path) -> None:
    sink = LocalOutputSink(state_dir / "out.csv")
    part = state_dir / "part.tmp"
    part.write_bytes(b"row1\nrow2\n")

    assert sink.size() is None
    assert sink.reset(b"email\n") == 6
    assert sink.append_from(part) == 16
    assert sink.append_from(part) == 26
    sink.truncate(16)
    assert sink.path.read_bytes() == b"email\nrow1\nrow2\n"
    sink.truncate(16)  # already that long: no-op
    assert sink.path.read_bytes() == b"email\nrow1\nrow2\n"


def test_sink_refuses_short_or_missing_file(state_dir: Path) -> None:
    sink = LocalOutputSink(state_dir / "out.csv")
    with pytest.raises(OutputIntegrityException, match="missing"):
        sink.truncate(10)
    sink.reset(b"abc\n")

    with pytest.raises(OutputIntegrityException, match="deleted, truncated or replaced"):
        sink.truncate(10)
    assert sink.path.read_bytes() == b"abc\n"  # nothing was changed or created


def test_sink_identifies_the_source_file_and_rejects_bad_paths(state_dir: Path) -> None:
    source = state_dir / "source.csv"
    source.write_text("x")
    sink = LocalOutputSink(source)

    assert sink.refers_to(source)
    assert sink.refers_to(state_dir / "." / "source.csv")
    assert not sink.refers_to(state_dir / "other.csv")
    with pytest.raises(InvalidConfigurationException, match="directory"):
        LocalOutputSink(state_dir)
    with pytest.raises(InvalidConfigurationException, match="does not exist"):
        LocalOutputSink(state_dir / "missing-dir" / "out.csv")




class AggregatorRig:
    """A ResultAggregator wired to real files and a real state store."""

    def __init__(self, state_dir: Path, state: StateManager) -> None:
        self.state = state
        self.scratch = LocalScratchStore(state_dir / "tmp", allowed_roots=[SCRATCH_ROOT])
        self.sink = LocalOutputSink(state_dir / "results.csv")
        self.aggregator = ResultAggregator(
            sink=self.sink,
            scratch=self.scratch,
            state=state,
            formatter=_Header(),
            logger=logging.getLogger("tests.aggregator"),
        )
        state.initialize([ChunkMetadata(i, (i - 1) * 10, i * 10) for i in range(1, 5)])

    def stage(self, chunk_id: int, payload: bytes) -> ChunkResult:
        """What a worker leaves behind: a scratch file plus its result."""
        with self.scratch.open_tmp(chunk_id) as handle:
            handle.write(payload)
        return ChunkResult(chunk_id, 1, payload.count(b"\n"), len(payload))

    def commit(self, result: ChunkResult) -> int:
        """The orchestrator's three steps for one chunk."""
        claimed = self.state.claim_next_pending()
        assert claimed is not None and claimed.chunk_id == result.chunk_id
        end = self.aggregator.merge(result)
        self.state.mark_completed(result.chunk_id, output_end=end)
        return end

    @property
    def output(self) -> bytes:
        return self.sink.path.read_bytes()


class _Header:
    def header(self) -> bytes:
        return b"email\n"

    def format(self, record: Any) -> bytes:  # unused by the aggregator
        return b""


@pytest.fixture
def rig(state_dir: Path, open_state: OpenState) -> AggregatorRig:
    return AggregatorRig(state_dir, open_state())


def test_merge_appends_the_scratch_file_then_deletes_it(rig: AggregatorRig) -> None:
    rig.aggregator.prepare()
    result = rig.stage(1, b"billing@mx.corp.test\nsupport@mx.corp.test\n")

    end = rig.commit(result)

    assert rig.output == b"email\nbilling@mx.corp.test\nsupport@mx.corp.test\n"
    assert end == len(rig.output) == rig.state.committed_output_end()
    assert not rig.scratch.tmp_path(1).exists()
    assert rig.state.get(1).status is ChunkStatus.COMPLETED


def test_chunks_are_appended_in_order_and_commits_track_the_length(rig: AggregatorRig) -> None:
    rig.aggregator.prepare()

    ends = [rig.commit(rig.stage(i, f"row{i}\n".encode())) for i in (1, 2, 3)]

    assert rig.output == b"email\nrow1\nrow2\nrow3\n"
    assert ends == [11, 16, 21]


def test_remerge_after_uncommitted_append_has_no_duplicates(rig: AggregatorRig) -> None:
    """Crash after the append but before COMPLETED: the retry must not write the rows twice."""
    rig.aggregator.prepare()
    rig.commit(rig.stage(1, b"one\n"))
    claimed = rig.state.claim_next_pending()  # chunk 2
    assert claimed is not None
    rig.aggregator.merge(rig.stage(2, b"two\n"))  # appended ...
    assert rig.output.endswith(b"two\n")  # ... but never marked COMPLETED: the "crash"

    # Restart: orphan recovery makes chunk 2 PENDING again; the worker redoes it.
    rig.aggregator.prepare()  # what the next run does first: cut back to the last commit
    assert rig.output == b"email\none\n"
    rig.state.recover_orphans()
    rig.commit(rig.stage(2, b"two\n"))

    assert rig.output == b"email\none\ntwo\n"


def test_merge_discards_uncommitted_tail_first(rig: AggregatorRig) -> None:
    rig.aggregator.prepare()
    rig.commit(rig.stage(1, b"one\n"))
    with rig.sink.path.open("ab") as handle:  # half-written append from a dead process
        handle.write(b"tw")

    rig.commit(rig.stage(2, b"two\n"))

    assert rig.output == b"email\none\ntwo\n"


def test_prepare_rewrites_header_on_fresh_run(rig: AggregatorRig) -> None:
    rig.sink.path.write_bytes(b"leftover from an aborted first attempt\n" * 5)

    rig.aggregator.prepare()

    assert rig.output == b"email\n"


def test_merge_of_empty_chunk_leaves_output_alone(rig: AggregatorRig) -> None:
    rig.aggregator.prepare()
    rig.commit(rig.stage(1, b"one\n"))
    before = rig.output

    end = rig.commit(rig.stage(2, b""))

    assert rig.output == before and end == len(before)


def test_scratch_size_mismatch_is_rejected(rig: AggregatorRig) -> None:
    rig.aggregator.prepare()
    result = rig.stage(1, b"data\n")
    rig.scratch.tmp_path(1).write_bytes(b"da")  # truncated behind the worker's back
    rig.state.claim_next_pending()

    with pytest.raises(OutputIntegrityException, match="worker reported"):
        rig.aggregator.merge(result)
    assert rig.output == b"email\n"


def test_deleted_or_replaced_output_is_detected(rig: AggregatorRig) -> None:
    rig.aggregator.prepare()
    rig.commit(rig.stage(1, b"one\n"))
    rig.sink.path.write_bytes(b"x")  # someone replaced results.csv with something shorter

    with pytest.raises(OutputIntegrityException):
        rig.aggregator.prepare()


def test_merge_requires_prepare(rig: AggregatorRig) -> None:
    with pytest.raises(RuntimeError, match="prepare"):
        rig.aggregator.merge(rig.stage(1, b"x\n"))


def test_rollback_cuts_the_output_back_to_the_last_commit(rig: AggregatorRig) -> None:
    rig.aggregator.prepare()
    rig.commit(rig.stage(1, b"one\n"))
    with rig.sink.path.open("ab") as handle:
        handle.write(b"partial")

    rig.aggregator.rollback_uncommitted()

    assert rig.output == b"email\none\n"




@pytest.fixture(scope="module")
def dataset(workspace: Path) -> Iterator[tuple[Path, list[str]]]:
    """8 MiB CSV with 13k planted addresses (decoys included) and the expected mining output."""
    directory = workspace / f"data-{uuid.uuid4().hex[:8]}"
    directory.mkdir()
    path = directory / "source.csv"
    expected = cast(list[str], sim.build_email_dataset(path, 8))
    yield path, expected
    shutil.rmtree(directory)


def build(
    state: ChunkStateStore,
    source: Path,
    run_dir: Path,
    *,
    output_name: str = "results.csv",
    **overrides: Any,
) -> MiningOrchestrator:
    options: dict[str, Any] = {
        "output_path": run_dir / output_name,
        "state": state,
        "scratch_dir": run_dir / "tmp",
        "allowed_roots": [SCRATCH_ROOT],
        "chunking_config": SCALED,
        "memory_provider": sim.FixedMemory(source.stat().st_size // 5),
    }
    options.update(overrides)
    return create_orchestrator(**options)


def read_results(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "email"
    return lines[1:]


def test_end_to_end_mines_all_addresses_in_order(
    dataset: tuple[Path, list[str]], state_dir: Path, open_state: OpenState
) -> None:
    source, expected = dataset
    state = open_state()

    summary = build(state, source, state_dir).run(source)

    mined = read_results(state_dir / "results.csv")
    assert mined == expected
    assert len(set(mined)) == len(mined)
    assert summary.succeeded and not summary.resumed
    assert 30 <= summary.chunks_total <= 40
    assert summary.chunks_processed == summary.chunks_total
    assert summary.records_written == len(expected)
    assert state.is_complete()
    assert list((state_dir / "tmp").glob("chunk_*.tmp")) == []  # every scratch file merged and removed


def test_acceptance_50_mib_dataset_is_fully_mined(
    workspace: Path, state_dir: Path, open_state: OpenState
) -> None:
    source = state_dir / "fifty.csv"
    expected = cast(list[str], sim.build_email_dataset(source, 50))
    assert source.stat().st_size > 50 * 1024 * 1024

    summary = build(open_state(), source, state_dir).run(source)

    assert read_results(state_dir / "results.csv") == expected
    assert summary.succeeded and 30 <= summary.chunks_total <= 40


def test_jsonl_output(dataset: tuple[Path, list[str]], state_dir: Path, open_state: OpenState) -> None:
    source, expected = dataset

    build(open_state(), source, state_dir, output_name="results.jsonl").run(source)

    rows = [json.loads(line) for line in (state_dir / "results.jsonl").read_text("utf-8").splitlines()]
    assert [row["email"] for row in rows] == expected


def test_custom_regex_strategy_and_header_with_bom(state_dir: Path, open_state: OpenState) -> None:
    source = state_dir / "orders.csv"
    rows = "".join(f"{i},ORD-{i:05d},note\n" for i in range(3000))
    source.write_bytes(b"\xef\xbb\xbf" + b"id,ORD-99999,note\n" + rows.encode())  # header looks like a match
    strategy = RegexExtractor(r"ORD-(\d{5})", ("order_number",))

    build(open_state(), source, state_dir, strategy=strategy, chunking_config=ChunkingConfig(
        critical_available_bytes=1 << 10, min_chunk_bytes=1 << 10, fallback_available_bytes=1 << 10,
        max_chunk_bytes=8 << 10,
    ), memory_provider=sim.FixedMemory(1 << 20)).run(source)

    out = (state_dir / "results.csv").read_text().splitlines()
    assert out[0] == "order_number"
    assert out[1:] == [f"{i:05d}" for i in range(3000)]  # header row and BOM not mined




def mine_with_crash_at_chunk_3(
    point: str,
    source: Path,
    run_dir: Path,
    state: StateManager,
    monkeypatch: pytest.MonkeyPatch,
    nth: int = CRASH_CHUNK,
) -> None:
    """Run the pipeline rigged to die (``Crash``) on the ``nth`` chunk this process handles.

    On a first run that is chunk ``nth``; on a restart it is the ``nth`` chunk from the
    first PENDING one (``nth=1`` means "die on the very chunk that was recovered").
    """
    streams: StreamReader | None = None
    store: ChunkStateStore = state
    if point == "mid-chunk":
        streams = cast(StreamReader, sim.MidChunkCrash(FileStreamReader(65_536, 1 << 20), nth, die))
    elif point == "after-append":
        store = cast(ChunkStateStore, sim.CrashBeforeCompletion(state, nth, die))
    elif point == "mid-append":
        real, calls = shutil.copyfileobj, []

        def copy_half_then_die(src: BinaryIO, dst: BinaryIO, length: int = 0) -> None:
            calls.append(1)
            if len(calls) != nth:
                real(src, dst, length)
                return
            payload = src.read()
            dst.write(payload[: len(payload) // 2])
            dst.flush()
            raise Crash

        monkeypatch.setattr(shutil, "copyfileobj", copy_half_then_die)
    with pytest.raises(Crash):
        build(store, source, run_dir, stream_reader=streams).run(source)
    monkeypatch.undo()


@pytest.mark.parametrize("point", ["mid-chunk", "mid-append", "after-append"])
def test_crash_during_chunk_3_then_restart_yields_a_perfect_result(
    point: str,
    dataset: tuple[Path, list[str]],
    state_dir: Path,
    open_state: OpenState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, expected = dataset
    first = open_state()
    mine_with_crash_at_chunk_3(point, source, state_dir, first, monkeypatch)

    # State left by the "dead" process: chunks 1-2 done, chunk 3 orphaned, output possibly dirty.
    observer = open_state("state.sqlite3", recover_orphans=False)
    assert observer.get(CRASH_CHUNK).status is ChunkStatus.IN_PROGRESS
    assert [observer.get(i).status for i in (1, 2)] == [ChunkStatus.COMPLETED] * 2
    committed = observer.committed_output_end()
    assert committed is not None
    dirty = (state_dir / "results.csv").stat().st_size - committed
    assert (dirty == 0) if point == "mid-chunk" else (dirty > 0)
    done_before = [observer.get(i) for i in (1, 2)]
    observer.close()

    # Restart: a brand-new manager (orphan recovery) and orchestrator, no faults.
    restarted = open_state()
    summary = build(restarted, source, state_dir).run(source)

    mined = read_results(state_dir / "results.csv")
    assert mined == expected  # complete, in order
    assert len(mined) == len(set(mined))  # zero duplicates
    assert summary.resumed and summary.recovered_orphans == 1
    assert summary.chunks_previously_completed == 2  # zero chunks skipped or redone
    assert summary.chunks_processed == summary.chunks_total - 2
    assert restarted.is_complete()
    assert restarted.get(CRASH_CHUNK).retry_count == 1
    assert [restarted.get(i) for i in (1, 2)] == done_before
    assert list((state_dir / "tmp").glob("chunk_*.tmp")) == []


def test_repeated_crashes_still_converge_to_the_exact_result(
    dataset: tuple[Path, list[str]], state_dir: Path, open_state: OpenState, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, expected = dataset
    # Four consecutive runs, each killed at a different place. Run 1 dies at chunk 3;
    # runs 2-4 resume at chunk 3 and die on their 1st, 2nd and 2nd chunk respectively.
    for point, nth in (("mid-append", 3), ("mid-chunk", 1), ("after-append", 2), ("mid-append", 2)):
        mine_with_crash_at_chunk_3(point, source, state_dir, open_state(), monkeypatch, nth)

    final = open_state()
    summary = build(final, source, state_dir, orchestrator_config=OrchestratorConfig(max_attempts=10)).run(source)

    mined = read_results(state_dir / "results.csv")
    assert mined == expected and len(mined) == len(set(mined))
    assert summary.succeeded and final.is_complete()
    assert final.get(CRASH_CHUNK).retry_count == 2  # killed twice while it was the active chunk


def test_restart_reuses_the_stored_plan_even_if_free_ram_changed(
    dataset: tuple[Path, list[str]], state_dir: Path, open_state: OpenState, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, expected = dataset
    first = open_state()
    mine_with_crash_at_chunk_3("mid-chunk", source, state_dir, first, monkeypatch)
    chunk_count = sum(first.summary().values())

    restarted = open_state()
    summary = build(
        restarted, source, state_dir, memory_provider=sim.FixedMemory(source.stat().st_size)  # 5x more RAM
    ).run(source)

    assert summary.chunks_total == chunk_count  # not re-planned with the larger chunk size
    assert read_results(state_dir / "results.csv") == expected


def test_repeatedly_crashing_chunk_is_abandoned(
    dataset: tuple[Path, list[str]], state_dir: Path, open_state: OpenState, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, expected = dataset
    config = OrchestratorConfig(max_attempts=2)
    for nth in (3, 1):  # chunk 3 kills the process on each of its two allowed attempts
        mine_with_crash_at_chunk_3("mid-chunk", source, state_dir, open_state(), monkeypatch, nth)

    state = open_state()
    summary = build(state, source, state_dir, orchestrator_config=config).run(source)

    assert state.get(CRASH_CHUNK).status is ChunkStatus.FAILED  # given up, not crash-looped
    assert summary.chunks_failed == 1 and not summary.succeeded
    start, end = state.get(CRASH_CHUNK).start_byte, state.get(CRASH_CHUNK).end_byte
    lost = set(re.findall(EMAIL_PATTERN, source.read_bytes()[start:end].decode()))
    assert read_results(state_dir / "results.csv") == [e for e in expected if e not in lost]




class FailsOnCall:
    """Strategy wrapper that raises an ordinary exception on its Nth line."""

    def __init__(self, inner: RegexExtractor, fail_on_line: int) -> None:
        self._inner = inner
        self._fail_on = fail_on_line
        self._calls = 0
        self.fields = inner.fields

    def extract(self, line: str) -> Iterator[Any]:
        self._calls += 1
        if self._calls == self._fail_on:
            raise ValueError("strategy bug")
        return self._inner.extract(line)


def test_ordinary_error_fails_only_that_chunk(
    dataset: tuple[Path, list[str]], state_dir: Path, open_state: OpenState
) -> None:
    source, expected = dataset
    state = open_state()

    summary = build(state, source, state_dir, strategy=FailsOnCall(RegexExtractor.emails(), 10_000)).run(source)

    failed = [r for r in state.records(ChunkStatus.FAILED)]
    assert len(failed) == 1 and summary.chunks_failed == 1
    lost = set(re.findall(EMAIL_PATTERN, source.read_bytes()[failed[0].start_byte : failed[0].end_byte].decode()))
    assert lost  # the failed chunk did contain addresses
    assert read_results(state_dir / "results.csv") == [e for e in expected if e not in lost]
    assert list((state_dir / "tmp").glob("chunk_*.tmp")) == []  # failed chunk's scratch file removed


def test_failed_chunks_are_retried_on_the_next_run(
    dataset: tuple[Path, list[str]], state_dir: Path, open_state: OpenState
) -> None:
    source, expected = dataset
    build(open_state(), source, state_dir, strategy=FailsOnCall(RegexExtractor.emails(), 10_000)).run(source)

    restarted = open_state()
    summary = build(restarted, source, state_dir).run(source)

    assert summary.succeeded and restarted.is_complete()
    mined = read_results(state_dir / "results.csv")
    assert sorted(mined) == sorted(expected)  # nothing lost or duplicated ...
    assert len(mined) == len(set(mined))  # ... though the retried chunk's rows land at the end




def test_output_may_not_be_the_source_file(
    dataset: tuple[Path, list[str]], state_dir: Path, open_state: OpenState
) -> None:
    source, _ = dataset
    victim = state_dir / "victim.csv"
    shutil.copy(source, victim)

    with pytest.raises(InvalidConfigurationException, match="must not be the source"):
        build(open_state(), victim, state_dir, output_name="victim.csv").run(victim)
    assert victim.read_bytes() == source.read_bytes()  # untouched


def test_fresh_run_refuses_existing_output_without_overwrite(
    dataset: tuple[Path, list[str]], state_dir: Path, open_state: OpenState
) -> None:
    source, expected = dataset
    existing = state_dir / "results.csv"
    existing.write_text("retained earlier results\n")

    with pytest.raises(InvalidConfigurationException, match="already exists"):
        build(open_state("a.sqlite3"), source, state_dir).run(source)
    assert existing.read_text() == "retained earlier results\n"

    build(open_state("b.sqlite3"), source, state_dir).run(source, overwrite_output=True)
    assert read_results(existing) == expected


def test_resuming_after_the_source_changed_is_refused(
    state_dir: Path, open_state: OpenState, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = state_dir / "growing.csv"
    expected = cast(list[str], sim.build_email_dataset(source, 8))
    mine_with_crash_at_chunk_3("mid-chunk", source, state_dir, open_state(), monkeypatch)
    with source.open("ab") as handle:
        handle.write(b"9999999,2025-01-01T00:00:00Z,x,late.arrival@internal.corp.test\n")

    with pytest.raises(StateStoreException, match="source changed"):
        build(open_state(), source, state_dir).run(source)
    assert expected  # (dataset was generated)


def test_unsupported_output_extension_is_rejected_up_front(
    dataset: tuple[Path, list[str]], state_dir: Path, open_state: OpenState
) -> None:
    source, _ = dataset

    with pytest.raises(InvalidConfigurationException, match=r"\.csv, \.jsonl"):
        build(open_state(), source, state_dir, output_name="results.xlsx")


def test_invalid_orchestrator_settings_are_rejected() -> None:
    with pytest.raises(InvalidConfigurationException):
        OrchestratorConfig(max_attempts=0)




def test_events_report_progress_without_leaking_mined_content(
    dataset: tuple[Path, list[str]], state_dir: Path, open_state: OpenState
) -> None:
    source, expected = dataset
    stream = io.StringIO()
    logger = logging.getLogger("tests.mining.events")
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        summary = build(open_state(), source, state_dir, logger=logger).run(source)
    finally:
        logger.removeHandler(handler)

    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    names = [e["event"] for e in events]
    assert names[0] == "mining.started" and names[-1] == "mining.completed"
    assert names.count("chunk.completed") == summary.chunks_total
    assert events[-1]["data"]["records_written"] == len(expected)
    text = stream.getvalue()
    assert expected[0] not in text and str(state_dir) not in text  # no content, no paths




def test_validation_script_passes_with_real_hard_exits() -> None:
    result = subprocess.run(
        [sys.executable, str(SIMULATION_SCRIPT), "--size-mib", "8"],
        capture_output=True,
        text=True,
        check=False,
        cwd=SIMULATION_SCRIPT.parent.parent,
        timeout=300,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count("zero duplicate addresses") == 3  # all three crash points
    assert "PASS" in result.stdout





class RaisesOnCall(FailsOnCall):
    """Like ``FailsOnCall`` but with a caller-chosen exception."""

    def __init__(self, inner: RegexExtractor, fail_on_line: int, error: Exception) -> None:
        super().__init__(inner, fail_on_line)
        self._error = error

    def extract(self, line: str) -> Iterator[Any]:
        self._calls += 1
        if self._calls == self._fail_on:
            raise self._error
        return self._inner.extract(line)


def test_first_failure_reason_reaches_summary_without_content(
    dataset: tuple[Path, list[str]], state_dir: Path, open_state: OpenState
) -> None:
    source, _ = dataset
    boom = ValueError("row 4411 held j.okafor@ap-south.corp.test")

    summary = build(
        open_state(), source, state_dir, strategy=RaisesOnCall(RegexExtractor.emails(), 10_000, boom)
    ).run(source)

    assert summary.first_error is not None
    assert re.fullmatch(r"chunk \d+: ValueError", summary.first_error)  # type only: messages can quote data
    assert summary.to_dict()["first_error"] == summary.first_error


def test_an_os_error_is_described_without_its_path(
    dataset: tuple[Path, list[str]], state_dir: Path, open_state: OpenState
) -> None:
    source, _ = dataset
    denied = PermissionError(13, "Permission denied", str(state_dir / "restricted" / "payroll.csv"))

    summary = build(
        open_state(), source, state_dir, strategy=RaisesOnCall(RegexExtractor.emails(), 10_000, denied)
    ).run(source)

    assert summary.first_error is not None and summary.first_error.endswith(": Permission denied")
    assert "restricted" not in summary.first_error


def test_a_clean_run_has_no_failure_reason(
    dataset: tuple[Path, list[str]], state_dir: Path, open_state: OpenState
) -> None:
    summary = build(open_state(), dataset[0], state_dir).run(dataset[0])

    assert summary.succeeded and summary.first_error is None


def test_source_truncated_mid_run_fails_affected_chunks(
    dataset: tuple[Path, list[str]], state_dir: Path, open_state: OpenState
) -> None:
    source = state_dir / "shrinking.csv"
    shutil.copy(dataset[0], source)
    expected = dataset[1]
    truncated = False

    def truncate_after_two_chunks(progress: MiningProgress) -> None:
        nonlocal truncated
        if progress.chunks_done == 2 and not truncated:
            truncated = True
            with source.open("r+b") as handle:
                handle.truncate(source.stat().st_size // 2)

    summary = build(open_state(), source, state_dir).run(source, on_progress=truncate_after_two_chunks)

    mined = read_results(state_dir / "results.csv")
    assert not summary.succeeded and summary.chunks_failed >= 1
    assert summary.first_error is not None and "shorter than the byte range" in summary.first_error
    # Whatever was committed is complete: an exact prefix of the right answer, never a ragged tail.
    assert 0 < len(mined) < len(expected)
    assert mined == expected[: len(mined)]


def test_a_utf16_source_is_refused_up_front_with_the_reason(
    state_dir: Path, open_state: OpenState
) -> None:
    source = state_dir / "wide.csv"
    rows = "".join(f"{i},escalate to sys.admin{i}@internal.corp.test\r\n" for i in range(60))
    source.write_bytes(b"\xff\xfe" + ("id,note\r\n" + rows).encode("utf-16-le"))
    state = open_state()

    with pytest.raises(UnsupportedDataFormatException, match="utf-16.*convert it to UTF-8"):
        build(state, source, state_dir).run(source)

    assert not state.is_initialized()  # nothing was planned or recorded
    assert not (state_dir / "results.csv").exists()
