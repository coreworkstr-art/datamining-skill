"""Tests for the ``run_mining`` facade, its on-disk layout and the progress callback."""

from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from datamining_skill import (
    InvalidConfigurationException,
    MiningProgress,
    StateManager,
    StateStoreException,
    run_mining,
)
from datamining_skill.application.options import MiningOptions
from datamining_skill.bootstrap import _source_fingerprint, run_layout
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
    layout = run_layout(state_dir, source, state_dir / "out.csv", MiningOptions().fingerprint())
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


# --------------------------------------------- a changed source is noticed by its content


def interrupted_job(dataset: tuple[Path, list[str]], state_dir: Path) -> tuple[Path, Path, Path]:
    """A job killed after three chunks, on a private copy of the dataset: (source, output, state)."""
    source, output = state_dir / "in.csv", state_dir / "out.csv"
    shutil.copyfile(dataset[0], source)
    calls = 0

    def die_after_three_chunks(_: MiningProgress) -> None:
        nonlocal calls
        calls += 1
        if calls == 4:
            raise Crash

    with pytest.raises(Crash):
        mine(source, output, state_dir, on_progress=die_after_three_chunks)
    layout = run_layout(state_dir, source, output, MiningOptions().fingerprint())
    return source, output, layout.state_path


def edit_in_place(path: Path, offset: int) -> None:
    """Change one byte without changing the size, then put the access and modification times back."""
    before = path.stat()
    data = bytearray(path.read_bytes())
    data[offset] = ord("y") if data[offset] != ord("y") else ord("z")
    path.write_bytes(bytes(data))
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = path.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)  # the edit left no trace


@pytest.mark.parametrize("where", ["start", "middle", "end"])
def test_bytes_edited_in_place_with_size_and_time_restored_are_refused_on_resume(
    dataset: tuple[Path, list[str]], state_dir: Path, where: str
) -> None:
    source, output, _ = interrupted_job(dataset, state_dir)
    partial = output.read_bytes()
    size = source.stat().st_size
    edit_in_place(source, {"start": 40, "middle": size // 2, "end": size - 40}[where])

    with pytest.raises(StateStoreException, match="source changed since this run began"):
        mine(source, output, state_dir)

    assert output.read_bytes() == partial  # nothing was appended to a result for other data


def test_a_finished_job_is_not_answered_from_its_state_when_the_source_was_edited(
    dataset: tuple[Path, list[str]], state_dir: Path
) -> None:
    source, output = state_dir / "in.csv", state_dir / "out.csv"
    shutil.copyfile(dataset[0], source)
    mine(source, output, state_dir)
    edit_in_place(source, source.stat().st_size // 2)

    with pytest.raises(StateStoreException, match="source changed"):
        mine(source, output, state_dir)


def test_only_the_modification_time_changing_is_refused_too(
    dataset: tuple[Path, list[str]], state_dir: Path
) -> None:
    source, output, _ = interrupted_job(dataset, state_dir)
    info = source.stat()
    os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns + 5_000_000_000))

    with pytest.raises(StateStoreException, match="source changed"):
        mine(source, output, state_dir)


def test_the_refusal_names_the_way_out_and_overwrite_starts_over_on_the_new_content(
    dataset: tuple[Path, list[str]], state_dir: Path
) -> None:
    source, output, _ = interrupted_job(dataset, state_dir)
    edit_in_place(source, source.stat().st_size // 2)

    with pytest.raises(StateStoreException, match="--overwrite"):
        mine(source, output, state_dir)
    fresh = mine(source, output, state_dir, overwrite=True)

    assert fresh.resumed is False and fresh.succeeded  # type: ignore[attr-defined]


def test_an_untouched_source_still_resumes(dataset: tuple[Path, list[str]], state_dir: Path) -> None:
    source, output, _ = interrupted_job(dataset, state_dir)

    resumed = mine(source, output, state_dir)

    assert resumed.resumed is True and results(output) == dataset[1]  # type: ignore[attr-defined]


def test_a_state_database_from_a_release_that_kept_only_size_and_time_still_resumes(
    dataset: tuple[Path, list[str]], state_dir: Path
) -> None:
    source, output, state_path = interrupted_job(dataset, state_dir)
    info = source.stat()
    with StateManager(state_path, allowed_roots=[state_dir / ".scratch"]) as state:
        state.set_meta("source", f"{info.st_size}:{info.st_mtime_ns}")  # the old two-part fingerprint
    resumed = mine(source, output, state_dir)

    assert resumed.resumed is True and results(output) == dataset[1]  # type: ignore[attr-defined]
    with StateManager(state_path, allowed_roots=[state_dir / ".scratch"]) as state:
        assert (state.get_meta("source") or "").count(":") == 2  # upgraded to the content-aware form


def test_the_fingerprint_has_size_time_and_a_digest(state_dir: Path) -> None:
    path = state_dir / "f.bin"
    path.write_bytes(b"abc" * 1000)

    size, modified, digest = _source_fingerprint(path).split(":")

    assert size == "3000" and modified == str(path.stat().st_mtime_ns) and len(digest) == 32


@pytest.mark.parametrize(
    ("offset", "noticed"),
    [(10, True), (400_000, False), (1 << 19, True), (700_000, False), ((1 << 20) - 10, True)],
    ids=["start", "between-start-and-middle", "middle", "between-middle-and-end", "end"],
)
def test_the_fingerprint_digest_covers_three_windows_of_the_file(state_dir: Path, offset: int, noticed: bool) -> None:
    path = state_dir / "f.bin"
    path.write_bytes(bytes(1 << 20))  # one mebibyte of zeros
    before = _source_fingerprint(path).split(":")[2]

    data = bytearray(path.read_bytes())
    data[offset] = 1
    path.write_bytes(bytes(data))

    assert (_source_fingerprint(path).split(":")[2] != before) is noticed  # a tripwire, not a checksum


@pytest.mark.parametrize("size", [0, 1, 100, 65_536, 65_537, 100_000, 200_000])
def test_the_fingerprint_works_for_files_shorter_than_its_windows(state_dir: Path, size: int) -> None:
    path = state_dir / "f.bin"
    path.write_bytes(bytes(range(256)) * (size // 256 + 1))
    path.write_bytes(path.read_bytes()[:size])
    first = _source_fingerprint(path)

    if size:
        data = bytearray(path.read_bytes())
        data[size // 2] ^= 0xFF
        path.write_bytes(bytes(data))
        assert _source_fingerprint(path).split(":")[2] != first.split(":")[2]
    assert first.startswith(f"{size}:")


def test_the_fingerprint_reads_a_bounded_amount_whatever_the_size(state_dir: Path) -> None:
    path = state_dir / "large.bin"
    with path.open("wb") as handle:
        handle.truncate(300 * 1024 * 1024)  # a 300 MiB file that is mostly empty
    reads: list[int] = []
    real_open = Path.open

    class CountingReader:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def __enter__(self) -> CountingReader:
            return self

        def __exit__(self, *_exc: object) -> None:
            self._inner.close()

        def fileno(self) -> int:
            return int(self._inner.fileno())

        def seek(self, *args: int) -> int:
            return int(self._inner.seek(*args))

        def read(self, size: int = -1) -> bytes:
            reads.append(size)
            return bytes(self._inner.read(size))

    def counting_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        handle = real_open(self, *args, **kwargs)
        return CountingReader(handle) if self == path else handle

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Path, "open", counting_open)
        _source_fingerprint(path)

    assert reads == [64 * 1024] * 3  # three windows of 64 KiB: 192 KiB of a 300 MiB file
