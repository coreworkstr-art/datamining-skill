"""Splits a profiled file into memory-safe, newline-aligned byte ranges (docs/architecture.md)."""

from __future__ import annotations

import logging
import math
import os
import time
from collections.abc import Callable, Generator
from pathlib import Path

from datamining_skill.application.config import ChunkingConfig
from datamining_skill.application.support import emit_event, stat_regular_file
from datamining_skill.domain.exceptions import (
    DataMiningException,
    ResourceExhaustionError,
    UnsupportedDataFormatException,
)
from datamining_skill.domain.models import ChunkMetadata, FileProfile, format_size
from datamining_skill.domain.ports import AvailableMemoryProvider, BoundaryLocator

_SEPARATOR_DESCRIPTION = "newline"


class ChunkingEngine:
    """Sizes chunks from the RAM available *now*: ``min(available * fraction, max_chunk_bytes)``.

    Only offsets are computed; chunk contents are never read, so planning memory is O(1).
    Each candidate end is moved *backwards* to just past the nearest newline. Moving back,
    never forward, is what makes the chunk size a hard upper bound.

    Chunks tile the file exactly, each starts and ends on a record boundary except the first
    start and last end, and a file that fits in one chunk yields exactly one. A record is a
    physical line, so a quoted CSV field spanning lines can be split; UTF-16/32 files can
    only be chunked whole. The size is fixed when planning starts, not re-read per chunk.
    """

    def __init__(
        self,
        *,
        config: ChunkingConfig,
        memory_provider: AvailableMemoryProvider,
        boundary_locator: BoundaryLocator,
        logger: logging.Logger,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._config = config
        self._memory = memory_provider
        self._locator = boundary_locator
        self._logger = logger
        self._clock = clock

    def plan(
        self, path: str | os.PathLike[str], profile: FileProfile
    ) -> Generator[ChunkMetadata, None, None]:
        """Check memory, file and encoding now, and return a lazy generator of chunks.

        Raises ``ResourceExhaustionError`` when free RAM is below the critical floor or too
        small for a worthwhile chunk. A record longer than the chunk size can only be found
        while walking the file, so that ``UnsupportedDataFormatException`` is raised from
        the generator, after the chunks before it.
        """
        source = Path(path)
        chunk_bytes, available = self._chunk_size_from_memory()
        size_bytes = stat_regular_file(source)

        if size_bytes > chunk_bytes and not profile.encoding.ascii_compatible:
            raise UnsupportedDataFormatException(
                source.name,
                f"{profile.encoding.name} data cannot be partitioned by byte offset "
                "(record separators are multi-byte); convert to UTF-8 first",
            )

        expected_chunks = max(1, math.ceil(size_bytes / chunk_bytes))
        emit_event(
            self._logger,
            logging.INFO,
            "chunking.started",
            file_name=source.name,
            size_bytes=size_bytes,
            available_memory_bytes=available,
            memory_fraction=self._config.memory_fraction,
            chunk_size_bytes=chunk_bytes,
            expected_chunks=expected_chunks,
        )
        if size_bytes != profile.size_bytes:
            emit_event(
                self._logger,
                logging.WARNING,
                "chunking.profile_stale",
                file_name=source.name,
                profiled_size_bytes=profile.size_bytes,
                current_size_bytes=size_bytes,
            )
        return self._generate(source, size_bytes, chunk_bytes, profile)

    def _chunk_size_from_memory(self) -> tuple[int, int]:
        config = self._config
        available = self._memory.available_bytes()
        if available is None:
            available = config.fallback_available_bytes
            emit_event(
                self._logger,
                logging.INFO,  # the normal case on macOS: expected, not a fault
                "chunking.memory_unavailable",
                assumed_available_bytes=available,
            )

        if available < config.critical_available_bytes:
            error = ResourceExhaustionError(
                f"available memory ({format_size(available)}) is below the critical "
                f"minimum ({format_size(config.critical_available_bytes)}); refusing to plan",
                available,
                config.critical_available_bytes,
            )
            self._emit_failure(error)
            raise error

        chunk_bytes = min(int(available * config.memory_fraction), config.max_chunk_bytes)
        if chunk_bytes < config.min_chunk_bytes:
            error = ResourceExhaustionError(
                f"available memory ({format_size(available)}) allows chunks of only "
                f"{format_size(chunk_bytes)}, below the minimum "
                f"({format_size(config.min_chunk_bytes)})",
                available,
                int(config.min_chunk_bytes / config.memory_fraction),
            )
            self._emit_failure(error)
            raise error
        return chunk_bytes, available

    def _generate(
        self, source: Path, size_bytes: int, chunk_bytes: int, profile: FileProfile
    ) -> Generator[ChunkMetadata, None, None]:
        started_at = self._clock()
        records_per_byte = (
            profile.records.count / profile.size_bytes if profile.size_bytes > 0 else 0.0
        )
        start = 0
        chunk_id = 0
        try:
            while start < size_bytes:
                if size_bytes - start <= chunk_bytes:
                    end = size_bytes
                else:
                    boundary = self._locator.last_boundary(source, start, start + chunk_bytes)
                    if boundary is None:
                        raise UnsupportedDataFormatException(
                            source.name,
                            f"a single record starting at byte {start} is at least "
                            f"{format_size(chunk_bytes)} long (no {_SEPARATOR_DESCRIPTION} "
                            "within the chunk limit), so the file cannot be safely partitioned",
                        )
                    end = boundary
                chunk_id += 1
                yield ChunkMetadata(
                    chunk_id=chunk_id,
                    start_byte=start,
                    end_byte=end,
                    estimated_records=round((end - start) * records_per_byte),
                )
                start = end
        except DataMiningException as exc:
            self._emit_failure(exc, file_name=source.name, chunks_produced=chunk_id)
            raise

        emit_event(
            self._logger,
            logging.INFO,
            "chunking.completed",
            file_name=source.name,
            size_bytes=size_bytes,
            chunk_count=chunk_id,
            chunk_size_bytes=chunk_bytes,
            duration_ms=round((self._clock() - started_at) * 1000, 3),
        )

    def _emit_failure(self, error: DataMiningException, **context: object) -> None:
        emit_event(
            self._logger,
            logging.WARNING,
            "chunking.failed",
            error_type=type(error).__name__,
            error=str(error),
            **context,
        )
