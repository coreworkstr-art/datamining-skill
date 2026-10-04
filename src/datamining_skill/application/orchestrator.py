"""The resumable, single-threaded mining loop (docs/architecture.md, "Mining pipeline")."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from datamining_skill.application.chunking_engine import ChunkingEngine
from datamining_skill.application.config import OrchestratorConfig
from datamining_skill.application.data_profiler import DataProfiler
from datamining_skill.application.miner_worker import MinerWorker, SourceDescriptor
from datamining_skill.application.result_aggregator import ResultAggregator
from datamining_skill.application.support import describe_failure, emit_event
from datamining_skill.domain.exceptions import (
    InvalidConfigurationException,
    StateStoreException,
    UnsupportedDataFormatException,
)
from datamining_skill.domain.models import ChunkMetadata, ChunkStatus
from datamining_skill.domain.ports import ChunkStateStore, ScratchStore


@dataclass(frozen=True, slots=True)
class MiningProgress:
    """Snapshot for ``on_progress``: ``chunks_done`` counts COMPLETED and FAILED chunks and never
    decreases during a run; ``records_written`` covers the current run only."""

    chunks_done: int
    chunks_total: int
    records_written: int


@dataclass(frozen=True, slots=True)
class MiningSummary:
    """Outcome of one ``run``. ``records_written`` and ``chunks_processed`` cover this run only;
    chunks an earlier run committed are in ``chunks_previously_completed``."""

    output_name: str
    resumed: bool
    recovered_orphans: int
    chunks_total: int
    chunks_previously_completed: int
    chunks_processed: int
    chunks_failed: int
    records_written: int
    duration_seconds: float
    first_error: str | None = None
    """Why the first chunk failed, if any did; never contains data."""

    @property
    def succeeded(self) -> bool:
        return self.chunks_failed == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "output_name": self.output_name,
            "resumed": self.resumed,
            "recovered_orphans": self.recovered_orphans,
            "chunks_total": self.chunks_total,
            "chunks_previously_completed": self.chunks_previously_completed,
            "chunks_processed": self.chunks_processed,
            "chunks_failed": self.chunks_failed,
            "records_written": self.records_written,
            "duration_ms": round(self.duration_seconds * 1000, 3),
            "first_error": self.first_error,
        }


class MiningOrchestrator:
    """Profile, plan, then per chunk: claim, mine to scratch, merge, commit.

    An ordinary ``Exception`` while mining or merging marks only that chunk FAILED and the run
    goes on. Anything else (``KeyboardInterrupt``, a hard kill) leaves the chunk IN_PROGRESS for
    the next start to requeue; truncate-then-append in the aggregator repairs the output, so
    the result equals an uninterrupted run. A resumed run reuses the stored plan and requires
    the source to keep the size it was planned for.
    """

    def __init__(
        self,
        *,
        profiler: DataProfiler,
        chunking_engine: ChunkingEngine,
        state: ChunkStateStore,
        worker: MinerWorker,
        aggregator: ResultAggregator,
        scratch: ScratchStore,
        config: OrchestratorConfig,
        logger: logging.Logger,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._profiler = profiler
        self._engine = chunking_engine
        self._state = state
        self._worker = worker
        self._aggregator = aggregator
        self._scratch = scratch
        self._config = config
        self._logger = logger
        self._clock = clock

    def run(
        self,
        source_path: str | os.PathLike[str],
        *,
        overwrite_output: bool = False,
        on_progress: Callable[[MiningProgress], None] | None = None,
    ) -> MiningSummary:
        """Mine ``source_path`` (never modified) into the output file, resuming if possible.

        ``overwrite_output`` lets a fresh run replace a non-empty output; a resumed run always
        continues its own. ``on_progress`` runs in the calling thread before the first chunk and
        after each one; an exception from it aborts the run like a crash.

        Raises ``InvalidConfigurationException`` if the output is the source or would be
        overwritten without permission, ``StateStoreException`` if the source size changed
        since the run began, plus whatever the profiler and chunking engine raise.
        """
        source = Path(source_path)
        run_started = self._clock()
        if self._aggregator.refers_to(source):
            raise InvalidConfigurationException("the output file must not be the source file")

        profile = self._profiler.profile(source)
        if not profile.encoding.ascii_compatible:
            # every chunk would fail identically: refuse now, not after a plan and retries
            raise UnsupportedDataFormatException(
                source.name,
                f"{profile.encoding.name} data cannot be mined because records are located by "
                "byte range; convert it to UTF-8 first",
            )
        resumed = self._state.is_initialized()
        if resumed:
            planned_size = self._state.plan_end_byte()
            if planned_size != profile.size_bytes:
                raise StateStoreException(
                    f"the source changed since this run began (planned for {planned_size} "
                    f"bytes, now {profile.size_bytes}); start a new run"
                )
        else:
            if self._aggregator.has_output() and not overwrite_output:
                raise InvalidConfigurationException(
                    "the output file already exists; choose another path or allow overwriting"
                )
            self._state.initialize(self._engine.plan(source, profile))

        descriptor = SourceDescriptor(source, profile.encoding, profile.data_offset)
        # every non-COMPLETED chunk is redone from scratch, so existing scratch files are stale
        self._scratch.clear_stale()
        if resumed and self._config.retry_failed_on_start:
            self._state.requeue_failed(self._config.max_attempts)
        self._aggregator.prepare()

        counts_before = self._state.summary()
        self._emit(
            logging.INFO,
            "mining.started",
            file_name=source.name,
            resumed=resumed,
            recovered_orphans=self._state.recovered_orphans,
            chunks_total=sum(counts_before.values()),
            chunks_completed=counts_before[ChunkStatus.COMPLETED],
        )

        processed = records = handled = 0
        first_error: str | None = None
        total_chunks = sum(counts_before.values())
        already_done = counts_before[ChunkStatus.COMPLETED] + counts_before[ChunkStatus.FAILED]

        def report() -> None:
            if on_progress is not None:
                on_progress(MiningProgress(already_done + handled, total_chunks, records))

        report()
        while (claimed := self._state.claim_next_pending()) is not None:
            chunk_id = claimed.chunk_id
            if claimed.retry_count >= self._config.max_attempts:
                self._state.mark_failed(chunk_id)
                handled += 1
                first_error = first_error or (
                    f"chunk {chunk_id}: given up after {claimed.retry_count} failed or interrupted attempts"
                )
                self._emit(
                    logging.WARNING,
                    "chunk.abandoned",
                    chunk_id=chunk_id,
                    attempts=claimed.retry_count,
                )
                report()
                continue

            chunk_started = self._clock()
            chunk = ChunkMetadata(chunk_id, claimed.start_byte, claimed.end_byte)
            try:
                chunk_result = self._worker.process(descriptor, chunk)
                output_end = self._aggregator.merge(chunk_result)
            except Exception as exc:  # noqa: BLE001 - isolate one chunk's failure
                self._scratch.discard_tmp(chunk_id)
                self._aggregator.rollback_uncommitted()
                self._state.mark_failed(chunk_id)
                handled += 1
                reason = describe_failure(exc)
                first_error = first_error or f"chunk {chunk_id}: {reason}"
                self._emit(
                    logging.WARNING,
                    "chunk.failed",
                    chunk_id=chunk_id,
                    error_type=type(exc).__name__,
                    reason=reason,
                    attempts=claimed.retry_count + 1,
                )
                report()
                continue

            self._state.mark_completed(chunk_id, output_end=output_end)
            processed += 1
            handled += 1
            records += chunk_result.records_written
            self._emit(
                logging.INFO,
                "chunk.completed",
                chunk_id=chunk_id,
                lines=chunk_result.lines_read,
                records=chunk_result.records_written,
                oversized_lines=chunk_result.oversized_lines,
                duration_ms=round((self._clock() - chunk_started) * 1000, 3),
            )
            report()

        counts_after = self._state.summary()
        summary = MiningSummary(
            output_name=self._aggregator.output_path.name,
            resumed=resumed,
            recovered_orphans=self._state.recovered_orphans,
            chunks_total=sum(counts_after.values()),
            chunks_previously_completed=counts_before[ChunkStatus.COMPLETED],
            chunks_processed=processed,
            chunks_failed=counts_after[ChunkStatus.FAILED],  # all that remain, incl. earlier runs'
            records_written=records,
            duration_seconds=self._clock() - run_started,
            first_error=first_error,
        )
        self._emit(logging.INFO, "mining.completed", **summary.to_dict())
        return summary

    def _emit(self, level: int, event: str, **context: Any) -> None:
        emit_event(self._logger, level, event, **context)
