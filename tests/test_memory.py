"""Verifies the O(1)-memory guarantee with ``memory-profiler``.

A multi-hundred-megabyte file is profiled and the process's peak memory growth is
compared against a limit far below the file size. If any code path loaded the
file (or a size-proportional structure) into RAM, the growth would be at least the
file size and the assertion would fail.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from memory_profiler import memory_usage

from datamining_skill import DataProfiler, create_profiler
from datamining_skill.application.config import MIB

FILE_SIZE_MIB = 256
# Observed growth is ~1 MiB for a 256 MiB file on Windows. The limit leaves headroom for
# allocator and RSS-accounting differences on Linux/macOS CI runners while staying 16x
# below the file size, so a size-proportional regression would still fail decisively.
MEMORY_GROWTH_LIMIT_MIB = 16.0
SAMPLE_INTERVAL_S = 0.01


def _as_float(value: Any) -> float:
    """``memory_usage(..., max_usage=True)`` returns a float or a 1-item list by version."""
    return float(value[0] if isinstance(value, list) else value)


def _build_csv(path: Path, size_mib: int) -> None:
    rows = "".join(
        f"{i},2025-01-{i % 28 + 1:02d}T00:00:00Z,emp{i}@internal.corp.test,{i % 977}.{i % 100:02d},paid\n"
        for i in range(12_000)
    ).encode("ascii")
    with path.open("wb") as sink:
        sink.write(b"id,created_at,email,amount,status\n")
        written = 0
        while written < size_mib * MIB:
            sink.write(rows)
            written += len(rows)


@pytest.fixture(scope="module")
def large_csv(workspace: Path) -> Iterator[Path]:
    path = workspace / "large.csv"
    _build_csv(path, FILE_SIZE_MIB)
    yield path
    path.unlink(missing_ok=True)


@pytest.mark.memory
def test_peak_memory_stays_far_below_file_size(large_csv: Path) -> None:
    profiler: DataProfiler = create_profiler()
    file_mib = large_csv.stat().st_size / MIB
    assert file_mib > 10 * MEMORY_GROWTH_LIMIT_MIB  # the file must dwarf the allowed growth

    profiler.profile(large_csv)  # warm-up: imports, regex compilation, allocator pools

    baseline = _as_float(
        memory_usage(-1, interval=SAMPLE_INTERVAL_S, timeout=0.3, max_usage=True)
    )
    peak = _as_float(
        memory_usage(
            (profiler.profile, (large_csv,), {}), interval=SAMPLE_INTERVAL_S, max_usage=True
        )
    )
    growth = peak - baseline

    assert growth < MEMORY_GROWTH_LIMIT_MIB, (
        f"peak memory grew by {growth:.1f} MiB while profiling a {file_mib:.0f} MiB file"
    )
