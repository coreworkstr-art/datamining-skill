"""Tests for the memory-aware :class:`ChunkingEngine`."""

from __future__ import annotations

import io
import json
import logging
import sys
import tracemalloc
import types
from collections.abc import Iterator
from pathlib import Path

import pytest

from datamining_skill import (
    ChunkingConfig,
    ChunkingEngine,
    ChunkMetadata,
    DataSourceUnavailableException,
    FileProfile,
    InvalidConfigurationException,
    ResourceExhaustionError,
    UnsupportedDataFormatException,
    create_chunking_engine,
    create_profiler,
)
from datamining_skill.application.config import KIB, MIB
from datamining_skill.infrastructure import (
    JsonLogFormatter,
    NewlineBoundaryLocator,
    SystemMemoryProbe,
)
from tests.conftest import WriteFile

# Scaled-down limits so a 50 MiB file reproduces the "10 GB file / 2 GB free RAM" ratio.
SCALED = ChunkingConfig(
    critical_available_bytes=1 * MIB,
    min_chunk_bytes=64 * KIB,
    fallback_available_bytes=1 * MIB,
)
GIB = 1024 * MIB


class FakeMemory:
    """Simulated system environment."""

    def __init__(self, available: int | None) -> None:
        self._available = available

    def available_bytes(self) -> int | None:
        return self._available


class SpyLocator:
    """Wraps the real locator and counts how often the engine seeks."""

    def __init__(self, block_bytes: int = 64 * KIB) -> None:
        self._inner = NewlineBoundaryLocator(block_bytes)
        self.calls = 0

    def last_boundary(self, path: Path, start: int, limit: int) -> int | None:
        self.calls += 1
        return self._inner.last_boundary(path, start, limit)


def make_engine(
    available: int | None,
    config: ChunkingConfig | None = None,
    logger: logging.Logger | None = None,
) -> ChunkingEngine:
    return create_chunking_engine(config, memory_provider=FakeMemory(available), logger=logger)


def profile_of(path: Path) -> FileProfile:
    return create_profiler().profile(path)


def write_rows(path: Path, size_mib: int) -> int:
    """Write a CSV of sequentially numbered, variable-width rows; return the row count."""
    next_id = 0
    written = 0
    with path.open("wb") as sink:
        header = b"id,payload\n"
        sink.write(header)
        written += len(header)
        while written < size_mib * MIB:
            block = "".join(
                f"{next_id + i},{'x' * ((next_id + i) % 40 + 10)}\n" for i in range(10_000)
            ).encode("ascii")
            sink.write(block)
            written += len(block)
            next_id += 10_000
    return next_id


def assert_valid_partition(path: Path, chunks: list[ChunkMetadata], max_chunk: int) -> None:
    """Contiguity, size cap, and byte-level alignment on record separators."""
    size = path.stat().st_size
    assert chunks[0].start_byte == 0
    assert chunks[-1].end_byte == size
    with path.open("rb") as handle:
        for index, chunk in enumerate(chunks):
            assert chunk.chunk_id == index + 1
            assert 0 < chunk.size_bytes <= max_chunk
            if index > 0:
                assert chunk.start_byte == chunks[index - 1].end_byte
                handle.seek(chunk.start_byte - 1)
                assert handle.read(1) == b"\n", "chunk must start right after a separator"
            if index < len(chunks) - 1:
                handle.seek(chunk.end_byte - 1)
                assert handle.read(1) == b"\n", "chunk must end exactly on a separator"


def assert_valid_partition_prefix(path: Path, chunks: list[ChunkMetadata], max_chunk: int) -> None:
    """Like :func:`assert_valid_partition` for a partial plan (every chunk ends on a separator)."""
    assert chunks[0].start_byte == 0
    with path.open("rb") as handle:
        for index, chunk in enumerate(chunks):
            assert 0 < chunk.size_bytes <= max_chunk
            if index > 0:
                assert chunk.start_byte == chunks[index - 1].end_byte
            handle.seek(chunk.end_byte - 1)
            assert handle.read(1) == b"\n"


@pytest.fixture(scope="module")
def csv_50mb(workspace: Path) -> Iterator[tuple[Path, int]]:
    path = workspace / "fifty.csv"
    rows = write_rows(path, 50)
    yield path, rows
    path.unlink(missing_ok=True)




def test_scaled_large_file_yields_30_to_40_aligned_chunks(
    csv_50mb: tuple[Path, int],
) -> None:
    path, total_rows = csv_50mb
    size = path.stat().st_size
    simulated_free_ram = size // 5  # 10 GB file : 2 GB free RAM
    engine = make_engine(simulated_free_ram, SCALED)
    chunk_cap = int(simulated_free_ram * SCALED.memory_fraction)

    chunks = list(engine.plan(path, profile_of(path)))

    assert 30 <= len(chunks) <= 40
    assert_valid_partition(path, chunks, chunk_cap)

    # Row-level proof: every chunk begins with the next sequential id, so no row was split.
    expected_id = -1  # the header row precedes id 0
    first_row_ids: list[int] = []
    with path.open("rb") as handle:
        for chunk in chunks:
            handle.seek(chunk.start_byte)
            data = handle.read(chunk.size_bytes)
            lines = data.splitlines()
            if chunk.chunk_id == 1:
                assert lines[0] == b"id,payload"
                lines = lines[1:]
            first_row_ids.append(int(lines[0].split(b",")[0]))
            for line in lines:
                expected_id += 1
                assert int(line.split(b",")[0]) == expected_id
    assert expected_id + 1 == total_rows
    assert first_row_ids == sorted(first_row_ids)


def test_file_smaller_than_threshold_yields_a_single_chunk(write_file: WriteFile) -> None:
    path = write_file("tiny.csv", "id,name\n1,a\n2,b\n3,c\n")
    engine = make_engine(2 * GIB)

    (chunk,) = engine.plan(path, profile_of(path))

    assert (chunk.chunk_id, chunk.start_byte, chunk.end_byte) == (1, 0, path.stat().st_size)


def test_chunk_limit_boundary_is_one_chunk_plus_one_byte_is_two(
    write_file: WriteFile,
) -> None:
    config = ChunkingConfig(
        max_chunk_bytes=1 * MIB, min_chunk_bytes=64 * KIB, critical_available_bytes=1 * MIB
    )
    exact = write_file("exact.csv", "a,b\n" * (MIB // 4))
    over = write_file("over.csv", "a,b\n" * (MIB // 4) + "\n")
    engine = make_engine(8 * GIB, config)

    assert len(list(engine.plan(exact, profile_of(exact)))) == 1
    assert len(list(engine.plan(over, profile_of(over)))) == 2


def test_chunk_size_is_15_percent_of_free_ram_capped_at_512_mib() -> None:
    config = ChunkingConfig()
    assert min(int(2 * GIB * config.memory_fraction), config.max_chunk_bytes) == int(0.15 * 2 * GIB)
    assert min(int(64 * GIB * config.memory_fraction), config.max_chunk_bytes) == 512 * MIB




def test_record_longer_than_the_chunk_limit_cannot_be_partitioned(write_file: WriteFile) -> None:
    config = ChunkingConfig(
        max_chunk_bytes=1 * MIB, min_chunk_bytes=64 * KIB, critical_available_bytes=1 * MIB
    )
    # Ordinary rows fill more than the profiler's 1 MiB head sample, then one 3 MiB record.
    normal = "".join(f"{i},name{i}\n" for i in range(120_000))
    giant = "x," + "y" * (3 * MIB)
    path = write_file("giant.csv", f"id,name\n{normal}{giant}\n1,z\n")
    produced: list[ChunkMetadata] = []

    def plan_everything() -> None:
        for chunk in make_engine(8 * GIB, config).plan(path, profile_of(path)):
            produced.append(chunk)

    with pytest.raises(UnsupportedDataFormatException, match="single record"):
        plan_everything()

    # Chunks before the oversized record were still delivered, aligned and within the cap.
    assert len(produced) >= 2
    assert_valid_partition_prefix(path, produced, config.max_chunk_bytes)


def test_long_lines_below_the_limit_are_never_split(write_file: WriteFile) -> None:
    config = ChunkingConfig(
        max_chunk_bytes=1 * MIB, min_chunk_bytes=64 * KIB, critical_available_bytes=1 * MIB
    )
    rows = [f"{i},{'y' * 100_000}" for i in range(40)]  # ~4 MiB of 100 KB lines
    path = write_file("long.csv", "id,blob\n" + "\n".join(rows) + "\n")

    chunks = list(make_engine(8 * GIB, config).plan(path, profile_of(path)))

    assert len(chunks) >= 4
    assert_valid_partition(path, chunks, config.max_chunk_bytes)


def test_single_long_line_that_fits_in_one_chunk_is_one_chunk(write_file: WriteFile) -> None:
    path = write_file("oneline.jsonl", json.dumps({"k": "v" * 200_000}))  # no newline at all
    engine = make_engine(2 * GIB)

    (chunk,) = engine.plan(path, profile_of(path))

    assert (chunk.start_byte, chunk.end_byte) == (0, path.stat().st_size)


def test_file_without_trailing_newline_ends_last_chunk_at_eof(write_file: WriteFile) -> None:
    config = ChunkingConfig(
        max_chunk_bytes=64 * KIB, min_chunk_bytes=64 * KIB, critical_available_bytes=1 * MIB
    )
    body = "\n".join(f"{i},value{i}" for i in range(20_000))  # ~270 KiB, no final newline
    path = write_file("nonl.csv", "id,v\n" + body)

    chunks = list(make_engine(8 * GIB, config).plan(path, profile_of(path)))

    assert len(chunks) > 1
    assert_valid_partition(path, chunks, config.max_chunk_bytes)
    assert path.read_bytes()[-1:] != b"\n"


def test_crlf_files_are_split_after_the_line_feed(write_file: WriteFile) -> None:
    config = ChunkingConfig(
        max_chunk_bytes=64 * KIB, min_chunk_bytes=64 * KIB, critical_available_bytes=1 * MIB
    )
    path = write_file("crlf.csv", "id,v\r\n" + "".join(f"{i},v{i}\r\n" for i in range(20_000)))

    chunks = list(make_engine(8 * GIB, config).plan(path, profile_of(path)))

    assert_valid_partition(path, chunks, config.max_chunk_bytes)
    with path.open("rb") as handle:
        handle.seek(chunks[0].end_byte - 2)
        assert handle.read(2) == b"\r\n"


def test_multibyte_encoding_rejected_unless_file_fits_one_chunk(
    write_file: WriteFile,
) -> None:
    config = ChunkingConfig(
        max_chunk_bytes=64 * KIB, min_chunk_bytes=64 * KIB, critical_available_bytes=1 * MIB
    )
    bom = b"\xff\xfe"
    large = write_file(
        "wide.csv", bom + ("id,v\r\n" + "".join(f"{i},ü\r\n" for i in range(30_000))).encode("utf-16-le")
    )
    small = write_file("small-wide.csv", bom + "id,v\r\n1,ü\r\n2,ö\r\n".encode("utf-16-le"))
    engine = make_engine(8 * GIB, config)

    with pytest.raises(UnsupportedDataFormatException, match="utf-16"):
        engine.plan(large, profile_of(large))
    assert len(list(engine.plan(small, profile_of(small)))) == 1


def test_changed_file_is_partitioned_by_its_live_size(write_file: WriteFile) -> None:
    path = write_file("grow.csv", "a,b\n1,2\n3,4\n")
    profile = profile_of(path)
    with path.open("ab") as handle:
        handle.write(b"5,6\n7,8\n")

    (chunk,) = make_engine(2 * GIB).plan(path, profile)

    assert chunk.end_byte == path.stat().st_size


def test_missing_file_is_reported_as_unavailable(write_file: WriteFile) -> None:
    path = write_file("gone.csv", "a,b\n1,2\n")
    profile = profile_of(path)
    path.unlink()

    with pytest.raises(DataSourceUnavailableException):
        make_engine(2 * GIB).plan(path, profile)




def test_critically_low_ram_is_refused_before_any_file_access(workspace: Path) -> None:
    locator = SpyLocator()
    engine = ChunkingEngine(
        config=ChunkingConfig(),
        memory_provider=FakeMemory(50 * MIB),
        boundary_locator=locator,
        logger=logging.getLogger("tests.chunking"),
    )
    never_created = workspace / "does-not-exist.csv"

    # Not even the (missing) file is examined: the memory check comes first.
    with pytest.raises(ResourceExhaustionError) as caught:
        engine.plan(never_created, None)  # type: ignore[arg-type]

    assert caught.value.available_bytes == 50 * MIB
    assert caught.value.required_bytes == 100 * MIB
    assert locator.calls == 0


def test_critical_floor_is_inclusive(write_file: WriteFile) -> None:
    path = write_file("t.csv", "a,b\n1,2\n")
    profile = profile_of(path)
    floor = ChunkingConfig().critical_available_bytes

    assert len(list(make_engine(floor).plan(path, profile))) == 1
    with pytest.raises(ResourceExhaustionError):
        make_engine(floor - 1).plan(path, profile)


def test_ram_too_small_for_a_worthwhile_chunk_is_refused(write_file: WriteFile) -> None:
    path = write_file("t.csv", "a,b\n1,2\n")
    config = ChunkingConfig(critical_available_bytes=1 * KIB, min_chunk_bytes=1 * MIB)

    with pytest.raises(ResourceExhaustionError, match="minimum"):
        make_engine(4 * MIB, config).plan(path, profile_of(path))  # 15% of 4 MiB < 1 MiB


def test_unknown_memory_falls_back_to_the_configured_assumption(write_file: WriteFile) -> None:
    path = write_file("t.csv", "a,b\n1,2\n")
    stream = io.StringIO()
    logger = logging.getLogger("tests.chunking.fallback")
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        chunks = list(make_engine(None, logger=logger).plan(path, profile_of(path)))
    finally:
        logger.removeHandler(handler)

    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert len(chunks) == 1
    assert events[0]["event"] == "chunking.memory_unavailable"
    assert events[0]["data"]["assumed_available_bytes"] == ChunkingConfig().fallback_available_bytes




def test_planning_is_lazy_and_returns_a_generator(csv_50mb: tuple[Path, int]) -> None:
    path, _ = csv_50mb
    locator = SpyLocator()
    engine = ChunkingEngine(
        config=SCALED,
        memory_provider=FakeMemory(10 * MIB),
        boundary_locator=locator,
        logger=logging.getLogger("tests.chunking"),
    )

    plan = engine.plan(path, profile_of(path))
    assert isinstance(plan, types.GeneratorType)
    assert locator.calls == 0  # nothing computed until iteration starts

    next(plan)
    assert locator.calls == 1
    next(plan)
    assert locator.calls == 2
    plan.close()


def test_planning_never_loads_chunk_data_into_memory(csv_50mb: tuple[Path, int]) -> None:
    path, _ = csv_50mb
    profile = profile_of(path)
    engine = make_engine(10 * MIB, SCALED)  # 1.5 MiB chunks

    tracemalloc.start()
    try:
        chunks = list(engine.plan(path, profile))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert len(chunks) > 30
    assert peak < 1 * MIB, f"planning allocated {peak / MIB:.2f} MiB; a chunk is 1.5 MiB"




def test_chunk_metadata_serialises_to_the_documented_shape() -> None:
    chunk = ChunkMetadata(chunk_id=1, start_byte=0, end_byte=104_857_600, estimated_records=7)

    assert chunk.size_bytes == 104_857_600
    assert chunk.to_dict() == {
        "chunk_id": 1,
        "start_byte": 0,
        "end_byte": 104_857_600,
        "size_bytes": 104_857_600,
        "estimated_records": 7,
    }
    with pytest.raises(AttributeError):
        chunk.chunk_id = 2  # type: ignore[misc]


def test_estimated_records_add_up_to_the_profile_estimate(csv_50mb: tuple[Path, int]) -> None:
    path, _ = csv_50mb
    profile = profile_of(path)

    chunks = list(make_engine(10 * MIB, SCALED).plan(path, profile))

    assert abs(sum(c.estimated_records for c in chunks) - profile.records.count) <= len(chunks)


def test_structured_events_describe_the_plan(csv_50mb: tuple[Path, int]) -> None:
    path, _ = csv_50mb
    stream = io.StringIO()
    logger = logging.getLogger("tests.chunking.events")
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        chunks = list(make_engine(10 * MIB, SCALED, logger).plan(path, profile_of(path)))
    finally:
        logger.removeHandler(handler)

    started, completed = (json.loads(line) for line in stream.getvalue().splitlines())
    assert started["event"] == "chunking.started"
    assert started["data"]["available_memory_bytes"] == 10 * MIB
    assert started["data"]["chunk_size_bytes"] == int(10 * MIB * 0.15)
    assert completed["event"] == "chunking.completed"
    assert completed["data"]["chunk_count"] == len(chunks)
    assert completed["data"]["duration_ms"] >= 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"memory_fraction": 0.0},
        {"memory_fraction": 1.1},
        {"max_chunk_bytes": 0},
        {"min_chunk_bytes": 10 * MIB, "max_chunk_bytes": 1 * MIB},
        {"fallback_available_bytes": 1 * KIB, "critical_available_bytes": 1 * MIB},
        {"boundary_block_bytes": -1},
    ],
)
def test_invalid_chunking_settings_are_rejected(overrides: dict[str, float]) -> None:
    with pytest.raises(InvalidConfigurationException):
        ChunkingConfig(**overrides)  # type: ignore[arg-type]


@pytest.mark.skipif(sys.platform not in ("win32", "linux"), reason="native probe: Windows/Linux")
def test_system_memory_probe_reports_a_plausible_value() -> None:
    available = SystemMemoryProbe().available_bytes()

    assert available is not None
    assert 1 * MIB < available < 1024 * GIB


def test_boundary_locator_scans_backwards_across_blocks(write_file: WriteFile) -> None:
    path = write_file("b.txt", b"ab\ncd\nef")
    locator = NewlineBoundaryLocator(block_bytes=2)  # force several blocks

    assert locator.last_boundary(path, 0, 8) == 6
    assert locator.last_boundary(path, 0, 5) == 3
    assert locator.last_boundary(path, 0, 3) == 3  # newline is the last byte of the range
    assert locator.last_boundary(path, 0, 2) is None
    assert locator.last_boundary(path, 3, 6) == 6
    assert locator.last_boundary(path, 6, 8) is None
