"""Validated limits for profiling, chunking and mining.

Every profiler buffer is bounded by one of these values, so peak memory depends on the
configuration, not on the input size.
"""

from __future__ import annotations

from dataclasses import dataclass

from datamining_skill.domain.exceptions import InvalidConfigurationException

KIB = 1024
MIB = 1024 * KIB


@dataclass(frozen=True, slots=True)
class ChunkingConfig:
    """Chunk size is ``min(available_memory * memory_fraction, max_chunk_bytes)``.

    A computed size below ``min_chunk_bytes`` counts as resource exhaustion (rather than
    thousands of tiny chunks), as does available RAM below ``critical_available_bytes``.
    ``fallback_available_bytes`` is assumed where the platform cannot report free memory and
    must be at least the critical floor. ``boundary_block_bytes`` is the read size of the
    backward separator scan.
    """

    memory_fraction: float = 0.15
    max_chunk_bytes: int = 512 * MIB
    min_chunk_bytes: int = 1 * MIB
    critical_available_bytes: int = 100 * MIB
    fallback_available_bytes: int = 512 * MIB
    boundary_block_bytes: int = 64 * KIB

    def __post_init__(self) -> None:
        if not 0.0 < self.memory_fraction <= 1.0:
            raise InvalidConfigurationException("'memory_fraction' must be in (0, 1]")
        for name in (
            "max_chunk_bytes",
            "min_chunk_bytes",
            "critical_available_bytes",
            "fallback_available_bytes",
            "boundary_block_bytes",
        ):
            if getattr(self, name) <= 0:
                raise InvalidConfigurationException(f"'{name}' must be a positive integer")
        if self.min_chunk_bytes > self.max_chunk_bytes:
            raise InvalidConfigurationException("'min_chunk_bytes' must be <= 'max_chunk_bytes'")
        if self.fallback_available_bytes < self.critical_available_bytes:
            raise InvalidConfigurationException(
                "'fallback_available_bytes' must be >= 'critical_available_bytes'"
            )


@dataclass(frozen=True, slots=True)
class OrchestratorConfig:
    """Mining loop settings.

    A chunk that already failed or was abandoned by a crash ``max_attempts`` times is marked
    FAILED without another try, so a chunk that kills the process cannot crash-loop forever.
    ``retry_failed_on_start`` requeues FAILED chunks with attempts left when resuming.
    """

    max_attempts: int = 3
    retry_failed_on_start: bool = True

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise InvalidConfigurationException("'max_attempts' must be at least 1")


@dataclass(frozen=True, slots=True)
class ProfilerConfig:
    """Profiler settings.

    ``head_sample_bytes`` feeds encoding and structure detection. Lines over ``max_line_bytes``
    are truncated and the rest skipped in constant space. Large files are extrapolated from
    ``estimation_windows`` (at least 2) evenly spaced windows of ``window_bytes``; files up to
    ``exact_count_max_bytes`` are scanned completely, still streaming.
    """

    chunk_size_bytes: int = 64 * KIB
    head_sample_bytes: int = 1 * MIB
    max_line_bytes: int = 1 * MIB
    structure_sample_records: int = 1_000
    estimation_windows: int = 8
    window_bytes: int = 256 * KIB
    exact_count_max_bytes: int = 16 * MIB
    min_format_confidence: float = 0.6
    max_fields: int = 1_024

    def __post_init__(self) -> None:
        positive_ints = (
            "chunk_size_bytes",
            "head_sample_bytes",
            "structure_sample_records",
            "window_bytes",
            "exact_count_max_bytes",
            "max_fields",
        )
        for name in positive_ints:
            if getattr(self, name) <= 0:
                raise InvalidConfigurationException(f"'{name}' must be a positive integer")
        if self.max_line_bytes < 1 * KIB:
            raise InvalidConfigurationException("'max_line_bytes' must be at least 1024")
        if self.estimation_windows < 2:
            raise InvalidConfigurationException("'estimation_windows' must be at least 2")
        if not 0.0 < self.min_format_confidence <= 1.0:
            raise InvalidConfigurationException("'min_format_confidence' must be in (0, 1]")
        if self.exact_count_max_bytes < self.window_bytes:
            raise InvalidConfigurationException(
                "'exact_count_max_bytes' must be >= 'window_bytes'"
            )
