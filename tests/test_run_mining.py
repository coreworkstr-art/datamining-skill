"""Tests for the ``run_mining`` facade, its on-disk layout and the progress callback."""

from __future__ import annotations

import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest

from datamining_skill import InvalidConfigurationException, MiningProgress, run_mining
from datamining_skill.bootstrap import run_layout
from tests.support import load_simulation
from tests.test_orchestration import SCALED, Crash

sim = load_simulation()


@pytest.fixture(scope="module")
def dataset(workspace: Path) -> Iterator[tuple[Path, list[str]]]:
    directory = workspace / f"runmining-{uuid.uuid4().hex[:8]}"
    directory.mkdir()
    path = directory / "source.csv"
    expected = cast(list[str], sim.build_email_dataset(path, 8))
    yield path, expected
    shutil.rmtree(directory)


def mine(source: Path, output: Path, workspace: Path, **kwargs: object) -> object:
    return run_mining(
        source,
        output,
        workspace=workspace,
        chunking_config=SCALED,
        memory_provider=sim.FixedMemory(source.stat().st_size // 5),
        **kwargs,  # type: ignore[arg-type]
    )


def results(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "email"
    return lines[1:]


def test_layout_is_deterministic_confined_and_specific_to_the_job(state_dir: Path) -> None:
    a, b = state_dir / "a.csv", state_dir / "b.csv"
    first = run_layout(state_dir, a, state_dir / "out1.csv")

    assert first == run_layout(state_dir, a, state_dir / "out1.csv")  # same job, same files
    assert first != run_layout(state_dir, a, state_dir / "out2.csv")  # different output
    assert first != run_layout(state_dir, b, state_dir / "out1.csv")  # different source
    assert first.scratch_root == state_dir.resolve() / ".scratch"
    assert first.state_path.parent == first.scratch_root
    assert first.scratch_dir.parent == first.scratch_root
    assert first.state_path.suffix == ".sqlite3"


def test_runs_end_to_end_and_reports_monotonic_progress(
    dataset: tuple[Path, list[str]], state_dir: Path
) -> None:
    source, expected = dataset
    snapshots: list[MiningProgress] = []

    summary = mine(source, state_dir / "out.csv", state_dir, on_progress=snapshots.append)

    assert results(state_dir / "out.csv") == expected
    done = [s.chunks_done for s in snapshots]
    assert done[0] == 0 and done == sorted(done) and len(set(done)) == len(done)
    assert snapshots[-1].chunks_done == snapshots[-1].chunks_total == summary.chunks_total  # type: ignore[attr-defined]
    assert snapshots[-1].records_written == len(expected)
    assert {s.chunks_total for s in snapshots} == {summary.chunks_total}  # type: ignore[attr-defined]


def test_an_interrupted_job_resumes_without_duplicates(
    dataset: tuple[Path, list[str]], state_dir: Path
) -> None:
    source, expected = dataset
    calls = 0

    def die_after_three_chunks(_: MiningProgress) -> None:
        nonlocal calls
        calls += 1
        if calls == 4:  # the initial report plus three finished chunks
            raise Crash

    with pytest.raises(Crash):
        mine(source, state_dir / "out.csv", state_dir, on_progress=die_after_three_chunks)

    resumed = mine(source, state_dir / "out.csv", state_dir)

    assert resumed.resumed is True and resumed.chunks_previously_completed == 3  # type: ignore[attr-defined]
    assert results(state_dir / "out.csv") == expected


def test_overwrite_discards_progress_and_state(
    dataset: tuple[Path, list[str]], state_dir: Path
) -> None:
    source, expected = dataset
    mine(source, state_dir / "out.csv", state_dir)
    layout = run_layout(state_dir, source, state_dir / "out.csv")
    assert layout.state_path.exists()

    fresh = mine(source, state_dir / "out.csv", state_dir, overwrite=True)

    assert fresh.resumed is False and fresh.chunks_processed == fresh.chunks_total  # type: ignore[attr-defined]
    assert results(state_dir / "out.csv") == expected


def test_an_unrelated_existing_output_is_refused_and_left_alone(
    dataset: tuple[Path, list[str]], state_dir: Path
) -> None:
    (state_dir / "out.csv").write_text("not ours\n")

    with pytest.raises(InvalidConfigurationException, match="already exists"):
        mine(dataset[0], state_dir / "out.csv", state_dir)

    assert (state_dir / "out.csv").read_text() == "not ours\n"


def test_state_and_scratch_stay_in_workspace_scratch(
    dataset: tuple[Path, list[str]], state_dir: Path
) -> None:
    mine(dataset[0], state_dir / "out.csv", state_dir)

    created = {p.name for p in state_dir.iterdir()}
    assert created == {"out.csv", ".scratch"}  # nothing else leaks into the workspace
    leftovers = list((state_dir / ".scratch").rglob("chunk_*.tmp"))
    assert leftovers == []
