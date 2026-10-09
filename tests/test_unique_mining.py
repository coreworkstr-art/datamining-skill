"""Unique mining, lower-casing, job identity and the report for a finished job."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from datamining_skill import (
    ChunkingConfig,
    InvalidConfigurationException,
    MiningProgress,
    MiningSummary,
    OutputIntegrityException,
    run_mining,
)
from datamining_skill.application import miner_worker
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
