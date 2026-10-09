"""Profiles a CSV, JSONL or log file from bounded, streamed samples (O(1) memory, see docs/architecture.md)."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import replace
from itertools import chain
from pathlib import Path
from typing import Any

from datamining_skill.application.config import ProfilerConfig
from datamining_skill.application.format_detection import FormatDetector
from datamining_skill.application.record_estimation import RecordEstimator
from datamining_skill.application.support import emit_event, stat_regular_file
from datamining_skill.domain.exceptions import (
    DataMiningException,
    DataSourceUnavailableException,
    UnsupportedDataFormatException,
)
from datamining_skill.domain.models import DataFormat, FileProfile, MemorySnapshot, ProfilingStats
from datamining_skill.domain.ports import (
    ContentGuard,
    EncodingDetector,
    MemoryProbe,
    StreamReader,
)


class DataProfiler:
    """Determines size, encoding, format, structure and record count of a file.

    Build one with ``bootstrap.create_profiler``. It logs ``profile.started``,
    ``format.detected``, ``profile.completed`` and ``profile.failed`` with the file name,
    durations and memory use, never file contents.
    """

    def __init__(
        self,
        *,
        config: ProfilerConfig,
        streams: StreamReader,
        encoding_detector: EncodingDetector,
        content_guard: ContentGuard,
        format_detector: FormatDetector,
        record_estimator: RecordEstimator,
        memory_probe: MemoryProbe,
        logger: logging.Logger,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._config = config
        self._streams = streams
        self._encoding_detector = encoding_detector
        self._content_guard = content_guard
        self._format_detector = format_detector
        self._record_estimator = record_estimator
        self._memory_probe = memory_probe
        self._logger = logger
        self._clock = clock

    def profile(self, path: str | os.PathLike[str]) -> FileProfile:
        """Profile ``path``.

        Raises ``DataSourceUnavailableException`` if it is missing, not a regular file or
        unreadable, and ``UnsupportedDataFormatException`` if it is empty, binary, compressed
        or in no recognised format.
        """
        source = Path(path)
        size_bytes = stat_regular_file(source)

        started_at = self._clock()
        memory_start = self._memory_probe.snapshot()
        self._emit(
            logging.INFO,
            "profile.started",
            file_name=source.name,
            size_bytes=size_bytes,
            rss_bytes=memory_start.rss_bytes,
            peak_rss_bytes=memory_start.peak_rss_bytes,
        )

        try:
            profile = self._run(source, size_bytes, started_at, memory_start)
        except DataMiningException as exc:
            self._emit_failure(source, exc, started_at, memory_start)
            raise
        except OSError as exc:
            error = DataSourceUnavailableException(source.name, f"read failed ({type(exc).__name__})")
            self._emit_failure(source, error, started_at, memory_start)
            raise error from exc

        stats = profile.stats
        self._emit(
            logging.INFO,
            "profile.completed",
            file_name=source.name,
            format=profile.data_format.value,
            size_bytes=size_bytes,
            estimated_records=profile.records.count,
            records_exact=profile.records.is_exact,
            duration_ms=round(stats.duration_seconds * 1000, 3),
            start_rss_bytes=stats.memory_start.rss_bytes,
            end_rss_bytes=stats.memory_end.rss_bytes,
            delta_rss_bytes=stats.rss_delta_bytes,
            peak_rss_bytes=stats.memory_end.peak_rss_bytes,
        )
        return profile

    def profile_as_dict(self, path: str | os.PathLike[str]) -> dict[str, Any]:
        """Profile ``path`` as a JSON-serialisable dict."""
        return self.profile(path).to_dict()

    def _run(
        self,
        source: Path,
        size_bytes: int,
        started_at: float,
        memory_start: MemorySnapshot,
    ) -> FileProfile:
        if size_bytes == 0:
            raise UnsupportedDataFormatException(source.name, "file is empty")

        # encoding detection and the binary/compressed guard share one streamed pass
        head_chunks = self._streams.chunks(source, 0, self._config.head_sample_bytes)
        with closing(head_chunks):
            first_chunk = next(head_chunks, b"")
            encoding = self._encoding_detector.detect(chain((first_chunk,), head_chunks))
        self._content_guard.inspect(source.name, first_chunk, encoding)

        match = self._format_detector.detect(source, encoding)
        self._emit(
            logging.INFO,
            "format.detected",
            file_name=source.name,
            format=match.data_format.value,
            encoding=encoding.name,
            confidence=round(match.analysis.confidence, 3),
            size_bytes=size_bytes,
        )

        data_offset = encoding.bom_length + match.analysis.header_bytes
        records = self._record_estimator.estimate(source, size_bytes, data_offset, encoding)
        if match.data_format is DataFormat.JSONL:
            records = replace(records, unit="records")

        memory_end = self._memory_probe.snapshot()
        stats = ProfilingStats(
            duration_seconds=self._clock() - started_at,
            memory_start=memory_start,
            memory_end=memory_end,
        )
        return FileProfile(
            file_name=source.name,
            size_bytes=size_bytes,
            data_format=match.data_format,
            encoding=encoding,
            structure=match.analysis.structure,
            records=records,
            stats=stats,
            data_offset=data_offset,
        )

    def _emit_failure(
        self,
        source: Path,
        error: DataMiningException,
        started_at: float,
        memory_start: MemorySnapshot,
    ) -> None:
        memory_end = self._memory_probe.snapshot()
        self._emit(
            logging.WARNING,
            "profile.failed",
            file_name=source.name,
            error_type=type(error).__name__,
            error=str(error),
            duration_ms=round((self._clock() - started_at) * 1000, 3),
            start_rss_bytes=memory_start.rss_bytes,
            end_rss_bytes=memory_end.rss_bytes,
            peak_rss_bytes=memory_end.peak_rss_bytes,
        )

    def _emit(self, level: int, event: str, **context: Any) -> None:
        emit_event(self._logger, level, event, **context)
