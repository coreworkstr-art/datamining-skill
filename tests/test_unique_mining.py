"""Unique mining, lower-casing, job identity and the report for a finished job."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable, Sequence
from contextlib import closing
from hashlib import blake2b
from pathlib import Path
from typing import Any

import pytest

from datamining_skill import (
    ChunkingConfig,
    ChunkMetadata,
    CsvFormatter,
    InvalidConfigurationException,
    MinerWorker,
    MiningProgress,
    MiningSummary,
    OutputIntegrityException,
    RegexExtractor,
    SourceDescriptor,
    StateManager,
    StateStoreException,
    run_mining,
)
from datamining_skill.application import miner_worker
from datamining_skill.application.options import MiningOptions
from datamining_skill.bootstrap import run_layout
from datamining_skill.domain.models import EncodingInfo
from datamining_skill.infrastructure import FileStreamReader
from datamining_skill.infrastructure.scratch import LocalScratchStore
from tests.conftest import SCRATCH_ROOT
from tests.support import load_simulation
from tests.test_orchestration import Crash

sim = load_simulation()
SMALL = ChunkingConfig(critical_available_bytes=1024, min_chunk_bytes=1024, fallback_available_bytes=2048)
ROWS = 4000


def build_source(path: Path) -> tuple[list[str], list[str]]:
    """A CSV of ``ROWS`` rows repeating 400 addresses in mixed case; returns (all, first-seen)."""
    emails = [
        f"{'User' if i % 2 else 'user'}{i % 400}@{'Corp' if i % 3 else 'CORP'}.test" for i in range(ROWS)
    ]
    path.write_text("id,contact,note\n" + "".join(f"{i},{e},row\n" for i, e in enumerate(emails)), encoding="utf-8")
    return emails, list(dict.fromkeys(emails))


def mine(source: Path, output: Path, workspace: Path, **kwargs: Any) -> MiningSummary:
    # about 20 KiB per chunk, so the file is cut into several chunks
    return run_mining(
        source,
        output,
        workspace=workspace,
        chunking_config=SMALL,
        memory_provider=sim.FixedMemory(source.stat().st_size // 5),
        **kwargs,
    )


def rows_of(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "email"
    return lines[1:]


def test_every_occurrence_is_written_without_unique(state_dir: Path) -> None:
    emails, _ = build_source(state_dir / "in.csv")

    summary = mine(state_dir / "in.csv", state_dir / "out.csv", state_dir)

    assert rows_of(state_dir / "out.csv") == emails
    assert summary.duplicates_skipped == 0 and summary.chunks_total > 3


def test_unique_keeps_the_first_occurrence_of_each_record_across_chunks(state_dir: Path) -> None:
    emails, first_seen = build_source(state_dir / "in.csv")

    summary = mine(state_dir / "in.csv", state_dir / "out.csv", state_dir, unique=True)

    assert rows_of(state_dir / "out.csv") == first_seen  # exact case, first-seen order
    assert summary.records_written == len(first_seen)
    assert summary.duplicates_skipped == len(emails) - len(first_seen)


def test_unique_with_lowercase_ignores_case(state_dir: Path) -> None:
    emails, _ = build_source(state_dir / "in.csv")
    expected = list(dict.fromkeys(e.lower() for e in emails))

    mine(state_dir / "in.csv", state_dir / "out.csv", state_dir, unique=True, lowercase=True)

    assert rows_of(state_dir / "out.csv") == expected
    assert len(expected) < len(set(emails))


def test_an_interrupted_unique_job_resumes_without_losing_or_repeating_records(state_dir: Path) -> None:
    _, first_seen = build_source(state_dir / "in.csv")
    reports = 0

    def die_after_three_chunks(_: MiningProgress) -> None:
        nonlocal reports
        reports += 1
        if reports == 4:  # the initial report plus three finished chunks
            raise Crash

    with pytest.raises(Crash):
        mine(state_dir / "in.csv", state_dir / "out.csv", state_dir, unique=True, on_progress=die_after_three_chunks)

    resumed = mine(state_dir / "in.csv", state_dir / "out.csv", state_dir, unique=True)

    assert resumed.resumed and resumed.chunks_previously_completed == 3
    assert rows_of(state_dir / "out.csv") == first_seen


def test_a_chunk_that_fails_after_registering_keys_does_not_hide_its_records(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    emails, first_seen = build_source(state_dir / "in.csv")
    real_process = miner_worker.MinerWorker.process
    failed_once = False

    def process_then_fail_chunk_two(self: miner_worker.MinerWorker, source: Any, chunk: Any) -> Any:
        nonlocal failed_once
        result = real_process(self, source, chunk)  # registers the chunk's keys
        if chunk.chunk_id == 2 and not failed_once:
            failed_once = True
            raise RuntimeError("simulated failure after the keys were registered")
        return result

    monkeypatch.setattr(miner_worker.MinerWorker, "process", process_then_fail_chunk_two)
    first = mine(state_dir / "in.csv", state_dir / "out.csv", state_dir, unique=True)
    assert first.chunks_failed == 1 and first.first_error is not None

    second = mine(state_dir / "in.csv", state_dir / "out.csv", state_dir, unique=True)

    assert second.succeeded
    written = rows_of(state_dir / "out.csv")
    assert len(written) == len(set(written)) == len(first_seen)  # none repeated, none lost
    assert set(written) == set(emails)


def test_a_finished_job_reports_that_there_was_nothing_to_do(state_dir: Path) -> None:
    _, first_seen = build_source(state_dir / "in.csv")
    mine(state_dir / "in.csv", state_dir / "out.csv", state_dir, unique=True)
    before = (state_dir / "out.csv").read_bytes()

    again = mine(state_dir / "in.csv", state_dir / "out.csv", state_dir, unique=True)

    assert again.already_complete and again.records_written == 0 and again.chunks_processed == 0
    assert again.chunks_previously_completed == again.chunks_total and again.succeeded
    assert (state_dir / "out.csv").read_bytes() == before
    assert rows_of(state_dir / "out.csv") == first_seen


def test_a_finished_job_is_rerun_when_the_output_was_deleted(state_dir: Path) -> None:
    build_source(state_dir / "in.csv")
    mine(state_dir / "in.csv", state_dir / "out.csv", state_dir)
    (state_dir / "out.csv").unlink()

    with pytest.raises(OutputIntegrityException, match="missing"):
        mine(state_dir / "in.csv", state_dir / "out.csv", state_dir)


def test_overwrite_runs_a_finished_job_again(state_dir: Path) -> None:
    build_source(state_dir / "in.csv")
    mine(state_dir / "in.csv", state_dir / "out.csv", state_dir)

    again = mine(state_dir / "in.csv", state_dir / "out.csv", state_dir, overwrite=True)

    assert not again.already_complete and again.records_written == ROWS


def test_a_changed_pattern_is_a_new_job_and_never_returns_the_old_result(state_dir: Path) -> None:
    source, output = state_dir / "in.csv", state_dir / "out.csv"
    source.write_text("id,tel,mail\n1,0532 111 22 33,a@internal.corp.test\n", encoding="utf-8")
    run_mining(source, output, workspace=state_dir, pattern=r"05\d\d \d{3} \d\d \d\d")
    assert output.read_text(encoding="utf-8").splitlines()[1:] == ["0532 111 22 33"]

    # same source and output, other pattern: refused as an unrelated output, not "resumed" silently
    with pytest.raises(InvalidConfigurationException, match="already exists"):
        run_mining(source, output, workspace=state_dir)

    replaced = run_mining(source, output, workspace=state_dir, overwrite=True)

    assert not replaced.resumed and rows_of(output) == ["a@internal.corp.test"]


@pytest.mark.parametrize("setting", [{"lowercase": True}, {"unique": True}, {"csv_formula_guard": True}])
def test_changing_any_setting_starts_a_new_job(state_dir: Path, setting: dict[str, bool]) -> None:
    build_source(state_dir / "in.csv")
    first = mine(state_dir / "in.csv", state_dir / "first.csv", state_dir)
    other = mine(state_dir / "in.csv", state_dir / "second.csv", state_dir, **setting)

    assert not other.resumed and not first.already_complete


# ----------------------------------------------- the hashes in the state file are keyed


def die_after(chunks: int) -> Callable[[MiningProgress], None]:
    """A progress callback that kills the run once ``chunks`` chunks have finished."""
    reports = 0

    def callback(_: MiningProgress) -> None:
        nonlocal reports
        reports += 1
        if reports == chunks + 1:  # the first report comes before any chunk
            raise Crash

    return callback


def state_path_of(workspace: Path, source: Path, output: Path) -> Path:
    return run_layout(workspace, source, output, MiningOptions(unique=True).fingerprint()).state_path


def stored_keys(state_path: Path) -> set[bytes]:
    with closing(sqlite3.connect(state_path)) as connection:
        return {bytes(row[0]) for row in connection.execute("SELECT key FROM seen_records")}


def stored_secret(state_path: Path) -> str | None:
    with closing(sqlite3.connect(state_path)) as connection:
        row = connection.execute("SELECT value FROM job_meta WHERE name = ?", ("dedupe_key",)).fetchone()
    return None if row is None else str(row[0])


def interrupted_unique_job(workspace: Path, name: str = "out.csv", chunks: int = 3) -> tuple[Path, Path]:
    source, output = workspace / "in.csv", workspace / name
    if not source.exists():
        build_source(source)
    with pytest.raises(Crash):
        mine(source, output, workspace, unique=True, on_progress=die_after(chunks))
    return output, state_path_of(workspace, source, output)


def test_the_hashes_left_by_an_interrupted_run_are_keyed_not_the_plain_hash_of_the_record(state_dir: Path) -> None:
    output, state_path = interrupted_unique_job(state_dir)

    keys = stored_keys(state_path)
    secret = bytes.fromhex(stored_secret(state_path) or "")
    written = output.read_bytes().splitlines(keepends=True)[1:]  # the header is not a record

    assert written and len(secret) == 32
    keyed = {blake2b(line, digest_size=16, key=secret).digest() for line in written}
    plain = {blake2b(line, digest_size=16).digest() for line in written}
    assert keyed <= keys  # every record that was written is remembered under its keyed hash
    assert not plain & keys  # and a table of plain hashes of guessed addresses finds none of them


def test_the_secret_is_made_once_kept_across_resumed_runs_and_purged_when_the_job_ends(state_dir: Path) -> None:
    source = state_dir / "in.csv"
    output, state_path = interrupted_unique_job(state_dir, chunks=2)
    first = stored_secret(state_path)
    keys_after_first = stored_keys(state_path)
    assert first is not None and len(bytes.fromhex(first)) == 32

    with pytest.raises(Crash):  # resumed, and killed again further on
        mine(source, output, state_dir, unique=True, on_progress=die_after(2))
    assert stored_secret(state_path) == first  # the same key, so the old hashes still match
    assert keys_after_first < stored_keys(state_path)

    _, first_seen = build_source(state_dir / "again.csv")  # what a complete run must produce
    finished = mine(source, output, state_dir, unique=True)

    assert finished.succeeded and rows_of(output) == first_seen  # no repeat slipped through the restarts
    assert stored_secret(state_path) is None and stored_keys(state_path) == set()
    leftovers = b"".join(file.read_bytes() for file in state_path.parent.glob(state_path.name + "*"))
    assert first.encode() not in leftovers and bytes.fromhex(first) not in leftovers


def test_every_job_has_a_secret_of_its_own(state_dir: Path) -> None:
    _, first = interrupted_unique_job(state_dir, "first.csv")
    _, second = interrupted_unique_job(state_dir, "second.csv")

    assert stored_secret(first) != stored_secret(second)


def test_a_job_without_the_secret_cannot_be_matched_against_another_jobs_hashes(state_dir: Path) -> None:
    output, state_path = interrupted_unique_job(state_dir)
    other_secret = os.urandom(32)
    written = output.read_bytes().splitlines(keepends=True)[1:]

    foreign = {blake2b(line, digest_size=16, key=other_secret).digest() for line in written}

    assert not foreign & stored_keys(state_path)


def test_the_state_manager_keeps_its_secret_across_reopening(state_dir: Path) -> None:
    path = state_dir / "s.sqlite3"
    with StateManager(path, allowed_roots=[SCRATCH_ROOT]) as state:
        first = state.dedupe_key()
        assert state.dedupe_key() == first and len(first) == 32
    with StateManager(path, allowed_roots=[SCRATCH_ROOT]) as state:
        assert state.dedupe_key() == first


def test_releasing_the_keys_also_deletes_the_secret(state_dir: Path) -> None:
    path = state_dir / "s.sqlite3"
    with StateManager(path, allowed_roots=[SCRATCH_ROOT]) as state:
        state.initialize([ChunkMetadata(1, 0, 10)])
        secret = state.dedupe_key()
        state.register(1, [blake2b(b"a@b.test\n", digest_size=16, key=secret).digest()])

        state.release_keys()

        assert state.get_meta("dedupe_key") is None
        assert state.register(1, [bytes(16)]) == [True]  # the hash table is empty again
    on_disk = b"".join(file.read_bytes() for file in state_dir.glob("s.sqlite3*"))
    assert secret.hex().encode() not in on_disk


def test_a_job_begun_with_plain_hashes_carries_on_without_a_key(state_dir: Path) -> None:
    """A state file made by a release that hashed without a key must keep finding its repeats."""
    path = state_dir / "legacy.sqlite3"
    with StateManager(path, allowed_roots=[SCRATCH_ROOT]) as state:
        state.initialize([ChunkMetadata(1, 0, 10)])
        state.register(1, [blake2b(b"old@record.test\n", digest_size=16).digest()])

        assert state.dedupe_key() == b""
        assert state.get_meta("dedupe_key") == ""
    with StateManager(path, allowed_roots=[SCRATCH_ROOT]) as state:
        assert state.dedupe_key() == b""


def test_a_damaged_secret_is_reported_and_not_replaced_silently(state_dir: Path) -> None:
    with StateManager(state_dir / "s.sqlite3", allowed_roots=[SCRATCH_ROOT]) as state:
        state.set_meta("dedupe_key", "not-hex")

        with pytest.raises(StateStoreException, match="damaged"):
            state.dedupe_key()


class PlainStore:
    """A unique-key store that knows nothing about secrets, like a test double or an older store."""

    def __init__(self) -> None:
        self.registered: list[bytes] = []

    def register(self, chunk_id: int, keys: Sequence[bytes]) -> list[bool]:
        self.registered.extend(keys)
        return [True] * len(keys)

    def discard(self, chunk_id: int) -> None:
        pass


def test_a_store_without_a_secret_still_gets_plain_hashes(state_dir: Path) -> None:
    source = state_dir / "plain.csv"
    source.write_text("id,note\n1,mail a@internal.corp.test\n", encoding="utf-8")
    scratch = LocalScratchStore(state_dir, allowed_roots=[SCRATCH_ROOT])
    store = PlainStore()
    worker = MinerWorker(
        strategy=RegexExtractor.emails(),
        formatter=CsvFormatter(("email",)),
        streams=FileStreamReader(64 * 1024, 1024 * 1024),
        scratch=scratch,
        unique_keys=store,
    )

    worker.process(SourceDescriptor(source, EncodingInfo(name="utf-8")), ChunkMetadata(1, 0, source.stat().st_size))

    written = scratch.tmp_path(1).read_bytes()
    assert store.registered == [blake2b(written, digest_size=16).digest()]


def test_keyed_hashing_does_not_change_what_unique_mining_writes(state_dir: Path) -> None:
    emails, first_seen = build_source(state_dir / "in.csv")

    summary = mine(state_dir / "in.csv", state_dir / "out.csv", state_dir, unique=True)

    assert rows_of(state_dir / "out.csv") == first_seen
    assert summary.duplicates_skipped == len(emails) - len(first_seen)
