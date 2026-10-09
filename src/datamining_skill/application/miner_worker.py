"""Mines one chunk into its scratch file (see docs/architecture.md, "Mining pipeline")."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable, Sequence
from contextlib import closing
from dataclasses import dataclass
from hashlib import blake2b
from pathlib import Path
from typing import BinaryIO, cast

from datamining_skill.application.extraction_strategy import (
    ExtractionStrategy,
    SpanExtractionStrategy,
)
from datamining_skill.application.json_text import decode_json_escapes
from datamining_skill.domain.models import ChunkMetadata, DataFormat, EncodingInfo
from datamining_skill.domain.ports import (
    RecordFormatter,
    ScratchStore,
    StreamReader,
    UniqueKeyStore,
)

JSON_ESCAPES_MODES = ("auto", "on", "off")
_JSON_FORMATS = frozenset({DataFormat.JSON, DataFormat.JSONL})
_DEDUPE_BATCH = 4096

_Decoder = Callable[[str], str]


def _accepts_span(extract: Callable[..., object]) -> bool:
    """Whether ``extract`` takes the ``start`` and ``stop`` arguments of the current protocol."""
    try:
        parameters = inspect.signature(extract).parameters.values()
    except (TypeError, ValueError):
        return False
    if any(parameter.kind is inspect.Parameter.VAR_POSITIONAL for parameter in parameters):
        return True
    return len(list(parameters)) >= 3


@dataclass(frozen=True, slots=True)
class SourceDescriptor:
    """The profiled source. ``data_offset`` is the first data byte, past any BOM and header."""

    path: Path
    encoding: EncodingInfo
    data_offset: int = 0
    data_format: DataFormat | None = None


@dataclass(frozen=True, slots=True)
class ChunkResult:
    """Counts for one mined chunk; the records themselves are in its scratch file.

    ``oversized_lines`` counts lines longer than the reader's size cap, which were searched
    window by window; ``duplicates_skipped`` counts records dropped by ``unique`` mining.
    """

    chunk_id: int
    lines_read: int
    records_written: int
    bytes_written: int
    oversized_lines: int = 0
    duplicates_skipped: int = 0


class _Tally:
    """Running counts of one chunk and the records waiting to be written."""

    def __init__(self) -> None:
        self.lines = 0
        self.oversized = 0
        self.records = 0
        self.bytes = 0
        self.duplicates = 0
        self.pending: list[bytes] = []


class MinerWorker:
    """Reads one chunk's byte range and writes the strategy's records to its scratch file.

    The worker owns all I/O; the strategy only turns text into records. Memory is O(1): each
    record is formatted and written as soon as it is produced, or, with ``unique_keys``, in
    batches that are checked against the keys of everything written before. The scratch file
    is truncated on open, so a partial file from a crashed attempt is overwritten.

    ``json_escapes`` decides whether JSON string escapes (``\\u0040`` for ``@``) are decoded
    before searching: ``"on"``, ``"off"``, or ``"auto"`` for JSON and JSON Lines sources only.

    A strategy that declares itself ``line_independent`` is given blocks of many lines at once,
    which is several times faster; any other strategy sees one line at a time. One whose
    ``extract`` takes ``start`` and ``stop`` is searched exactly in lines longer than the size cap;
    one that takes only the line sees, for such a line, each window's own span, so a match
    that straddles two windows can be missed.
    """

    def __init__(
        self,
        *,
        strategy: ExtractionStrategy,
        formatter: RecordFormatter,
        streams: StreamReader,
        scratch: ScratchStore,
        unique_keys: UniqueKeyStore | None = None,
        json_escapes: str = "off",
    ) -> None:
        if json_escapes not in JSON_ESCAPES_MODES:
            raise ValueError(f"json_escapes must be one of {', '.join(JSON_ESCAPES_MODES)}")
        self._strategy = strategy
        self._formatter = formatter
        self._streams = streams
        self._scratch = scratch
        self._unique_keys = unique_keys
        self._json_escapes = json_escapes
        self._by_blocks = bool(getattr(strategy, "line_independent", False))
        self._span_strategy: SpanExtractionStrategy | None = (
            cast(SpanExtractionStrategy, strategy) if _accepts_span(strategy.extract) else None
        )
        # a strategy whose values never need quoting lets a CSV writer skip checking each one
        self._format_record: Callable[[Sequence[str]], bytes] = (
            getattr(formatter, "format_clean", formatter.format)
            if getattr(strategy, "clean_values", False)
            else formatter.format
        )

    def process(self, source: SourceDescriptor, chunk: ChunkMetadata) -> ChunkResult:
        """Mine ``chunk`` into its scratch file.

        Raises ``UnsupportedDataFormatException`` for a multi-byte source or a misaligned
        chunk start, and ``DataSourceUnavailableException`` if the source is shorter than
        the chunk.
        """
        start = max(chunk.start_byte, source.data_offset)
        decode = self._decoder(source)
        tally = _Tally()

        with self._scratch.open_tmp(chunk.chunk_id) as scratch_file:
            if start < chunk.end_byte:
                # A chunk must begin right after a line feed, except at the first data byte,
                # which follows a BOM or header rather than a "\n".
                aligned = start > source.data_offset
                if self._by_blocks:
                    self._mine_blocks(source, start, chunk, aligned, decode, tally, scratch_file)
                else:
                    self._mine_lines(source, start, chunk, aligned, decode, tally, scratch_file)
            self._flush(tally, chunk.chunk_id, scratch_file)

        return ChunkResult(
            chunk.chunk_id,
            tally.lines,
            tally.records,
            tally.bytes,
            tally.oversized,
            tally.duplicates,
        )

    def _mine_blocks(
        self,
        source: SourceDescriptor,
        start: int,
        chunk: ChunkMetadata,
        aligned: bool,
        decode: _Decoder | None,
        tally: _Tally,
        sink: BinaryIO,
    ) -> None:
        extract = self._strategy.extract
        format_record = self._format_record
        add = tally.pending.append
        blocks = self._streams.range_blocks(
            source.path, source.encoding, start, chunk.end_byte, check_alignment=aligned
        )
        with closing(blocks):
            for block in blocks:
                emit_from = block.emit_from
                emit_until = block.emit_until
                tally.lines += block.line_count
                if emit_from or emit_until is not None:  # one window of a very long line
                    if not emit_from:
                        tally.oversized += 1
                    records = self._extract_window(block.text, emit_from, emit_until, decode)
                else:
                    records = extract(decode(block.text) if decode is not None else block.text)
                for record in records:
                    add(format_record(record))
                if len(tally.pending) >= _DEDUPE_BATCH:
                    self._flush(tally, chunk.chunk_id, sink)

    def _mine_lines(
        self,
        source: SourceDescriptor,
        start: int,
        chunk: ChunkMetadata,
        aligned: bool,
        decode: _Decoder | None,
        tally: _Tally,
        sink: BinaryIO,
    ) -> None:
        extract = self._strategy.extract
        format_record = self._format_record
        add = tally.pending.append
        lines = self._streams.range_lines(
            source.path, source.encoding, start, chunk.end_byte, check_alignment=aligned
        )
        with closing(lines):
            for line in lines:
                emit_from = line.emit_from
                emit_until = line.emit_until
                if emit_from or emit_until is not None:  # one window of a very long line
                    if not emit_from:
                        tally.lines += 1
                        tally.oversized += 1
                    records = self._extract_window(line.text, emit_from, emit_until, decode)
                else:
                    tally.lines += 1
                    records = extract(decode(line.text) if decode is not None else line.text)
                for record in records:
                    add(format_record(record))
                if len(tally.pending) >= _DEDUPE_BATCH:
                    self._flush(tally, chunk.chunk_id, sink)

    def _flush(self, tally: _Tally, chunk_id: int, sink: BinaryIO) -> None:
        """Write the pending records, minus those an earlier record already covers."""
        pending = tally.pending
        if not pending:
            return
        if self._unique_keys is None:
            fresh = [True] * len(pending)
        else:
            keys = [blake2b(record, digest_size=16).digest() for record in pending]
            fresh = self._unique_keys.register(chunk_id, keys)
        for encoded_record, is_new in zip(pending, fresh, strict=True):
            if is_new:
                sink.write(encoded_record)
                tally.records += 1
                tally.bytes += len(encoded_record)
            else:
                tally.duplicates += 1
        pending.clear()

    def _extract_window(
        self, text: str, start: int, stop: int | None, decode: _Decoder | None
    ) -> Iterable[Sequence[str]]:
        if decode is not None:
            # span bounds move with the text, so they are measured on the decoded prefix
            start = len(decode(text[:start]))
            stop = None if stop is None else len(decode(text[:stop]))
            text = decode(text)
        if self._span_strategy is None:
            # a strategy written for ``extract(line)`` sees only the part of the window it answers for
            return self._strategy.extract(text[start:stop])
        return self._span_strategy.extract(text, start, stop)

    def _decoder(self, source: SourceDescriptor) -> _Decoder | None:
        if self._json_escapes == "on" or (
            self._json_escapes == "auto" and source.data_format in _JSON_FORMATS
        ):
            return decode_json_escapes
        return None
