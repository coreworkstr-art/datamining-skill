"""Idempotent append of chunk results to the output file (docs/architecture.md, "Idempotent merge")."""

from __future__ import annotations

import logging
from pathlib import Path

from datamining_skill.application.miner_worker import ChunkResult
from datamining_skill.application.support import emit_event
from datamining_skill.domain.exceptions import OutputIntegrityException
from datamining_skill.domain.ports import ChunkStateStore, OutputSink, RecordFormatter, ScratchStore


class ResultAggregator:
    """Appends a finished chunk's scratch file to the output, once.

    The state store records the output length atomically with each COMPLETED status, so the
    last recorded length is always the exact size of the valid output. ``merge`` truncates to
    it (dropping a half-written or never-committed append), appends and fsyncs the scratch
    file, deletes it, and returns the new length for ``mark_completed``. A crash anywhere
    leaves at most uncommitted bytes past the committed length, removed before the chunk is
    appended again: no record is written twice or lost. Assumes a single writer.
    """

    def __init__(
        self,
        *,
        sink: OutputSink,
        scratch: ScratchStore,
        state: ChunkStateStore,
        formatter: RecordFormatter,
        logger: logging.Logger,
    ) -> None:
        self._sink = sink
        self._scratch = scratch
        self._state = state
        self._formatter = formatter
        self._logger = logger
        self._header_length: int | None = None

    @property
    def output_path(self) -> Path:
        return self._sink.path

    def has_output(self) -> bool:
        """Whether the output file exists and is not empty."""
        return bool(self._sink.size())

    def refers_to(self, path: Path) -> bool:
        """Whether the output file is the same file as ``path``."""
        return self._sink.refers_to(path)

    def prepare(self) -> None:
        """Recreate the output with just its header if nothing is committed, else cut it back to
        the last committed length, dropping bytes a crashed run appended."""
        committed = self._state.committed_output_end()
        if committed is None:
            self._header_length = self._sink.reset(self._formatter.header())
        else:
            self._sink.truncate(committed)

    def merge(self, result: ChunkResult) -> int:
        """Append the chunk's scratch file, delete it, and return the new output length.

        The caller passes that length to ``mark_completed``. Raises ``OutputIntegrityException``
        if the scratch file differs in size from what the worker reported, or the output is
        missing or shorter than the ledger says.
        """
        base = self._committed_length()
        self._sink.truncate(base)  # discard any uncommitted tail first

        if result.bytes_written:
            actual = self._scratch.tmp_size(result.chunk_id)
            if actual != result.bytes_written:
                raise OutputIntegrityException(
                    f"chunk {result.chunk_id}: scratch file holds {actual} bytes, "
                    f"worker reported {result.bytes_written}"
                )
            new_end = self._sink.append_from(self._scratch.tmp_path(result.chunk_id))
            if new_end != base + result.bytes_written:
                raise OutputIntegrityException(
                    f"chunk {result.chunk_id}: output grew to {new_end} bytes, "
                    f"expected {base + result.bytes_written}"
                )
        else:
            new_end = base

        self._scratch.discard_tmp(result.chunk_id)
        emit_event(
            self._logger,
            logging.DEBUG,
            "chunk.merged",
            chunk_id=result.chunk_id,
            appended_bytes=result.bytes_written,
            output_bytes=new_end,
        )
        return new_end

    def rollback_uncommitted(self) -> None:
        """Cut the output back to the last committed length after a failed chunk."""
        self._sink.truncate(self._committed_length())

    def _committed_length(self) -> int:
        committed = self._state.committed_output_end()
        if committed is not None:
            return committed
        if self._header_length is None:
            raise RuntimeError("ResultAggregator.prepare() must be called before merging")
        return self._header_length
