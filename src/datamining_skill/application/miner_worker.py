"""Mines one chunk into its scratch file (see docs/architecture.md, "Mining pipeline")."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from datamining_skill.domain.models import ChunkMetadata, EncodingInfo
from datamining_skill.domain.ports import RecordFormatter, ScratchStore, StreamReader
from datamining_skill.application.extraction_strategy import ExtractionStrategy


@dataclass(frozen=True, slots=True)
class SourceDescriptor:
    """The profiled source. ``data_offset`` is the first data byte, past any BOM and header."""

    path: Path
    encoding: EncodingInfo
    data_offset: int = 0


@dataclass(frozen=True, slots=True)
class ChunkResult:
    """Counts for one mined chunk; the records themselves are in its scratch file."""

    chunk_id: int
    lines_read: int
    records_written: int
    bytes_written: int
    oversized_lines: int = 0


class MinerWorker:
    """Reads one chunk's byte range and writes the strategy's records to its scratch file.

    The worker owns all I/O; the strategy only turns a line into records. Memory is O(1):
    each record is formatted and written as soon as it is produced. The scratch file is
    truncated on open, so a partial file from a crashed attempt is overwritten. Lines over
    the reader's size cap are skipped and counted, never matched in truncated form.
    """

    def __init__(
        self,
        *,
        strategy: ExtractionStrategy,
        formatter: RecordFormatter,
        streams: StreamReader,
        scratch: ScratchStore,
    ) -> None:
        self._strategy = strategy
        self._formatter = formatter
        self._streams = streams
        self._scratch = scratch

    def process(self, source: SourceDescriptor, chunk: ChunkMetadata) -> ChunkResult:
        """Mine ``chunk`` into its scratch file.

        Raises ``UnsupportedDataFormatException`` for a multi-byte source or a misaligned
        chunk start, and ``DataSourceUnavailableException`` if the source is shorter than
        the chunk.
        """
        start = max(chunk.start_byte, source.data_offset)
        lines_read = records_written = bytes_written = skipped_oversized = 0

        with self._scratch.open_tmp(chunk.chunk_id) as scratch_file:
            if start < chunk.end_byte:
                lines = self._streams.range_lines(
                    source.path,
                    source.encoding,
                    start,
                    chunk.end_byte,
                    # A chunk must begin right after a line feed, except at the first data
                    # byte, which follows a BOM or header rather than a "\n".
                    check_alignment=start > source.data_offset,
                )
                with closing(lines):
                    for line in lines:
                        lines_read += 1
                        if line.truncated:
                            skipped_oversized += 1
                            continue
                        for record in self._strategy.extract(line.text):
                            encoded_record = self._formatter.format(record)
                            scratch_file.write(encoded_record)
                            records_written += 1
                            bytes_written += len(encoded_record)

        return ChunkResult(
            chunk.chunk_id, lines_read, records_written, bytes_written, skipped_oversized
        )
