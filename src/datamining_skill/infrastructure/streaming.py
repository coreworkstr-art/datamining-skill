"""Lazy, read-only file readers: at most one line (``max_line_bytes``) or one chunk
(``chunk_size_bytes``) is held in memory, whatever the file size or content."""

from __future__ import annotations

import codecs
import os
from collections.abc import Generator
from pathlib import Path
from typing import BinaryIO

from datamining_skill.domain.exceptions import (
    DataSourceUnavailableException,
    UnsupportedDataFormatException,
)
from datamining_skill.domain.models import EncodingInfo, TextBlock, TextLine

_SHORTER_THAN_PLANNED = (
    "the file is shorter than the byte range being read; it was truncated or replaced while mining"
)


class FileStreamReader:
    """Generator-based ``StreamReader``."""

    def __init__(self, chunk_size_bytes: int, max_line_bytes: int) -> None:
        self._chunk_size = chunk_size_bytes
        self._max_line = max_line_bytes

    def chunks(self, path: Path, offset: int, length: int) -> Generator[bytes, None, None]:
        """Yield at most ``length`` bytes from ``offset`` in ``chunk_size`` pieces."""
        with path.open("rb") as handle:
            handle.seek(offset)
            remaining = length
            while remaining > 0:
                piece = handle.read(min(self._chunk_size, remaining))
                if not piece:
                    return
                remaining -= len(piece)
                yield piece

    def lines(
        self,
        path: Path,
        encoding: EncodingInfo,
        offset: int,
        length: int,
        *,
        align: bool = False,
    ) -> Generator[TextLine, None, None]:
        """Yield decoded lines until at least ``length`` bytes are consumed.

        Decoding never raises: undecodable bytes become U+FFFD, so a few corrupt records
        cannot abort profiling.
        """
        if encoding.ascii_compatible:
            yield from self._byte_lines(path, encoding.name, offset, length, align)
            return
        if align:
            raise ValueError("line alignment is unsupported for multi-byte encodings")
        yield from self._wide_lines(path, encoding.name, offset, length)

    def range_lines(
        self,
        path: Path,
        encoding: EncodingInfo,
        start: int,
        end: int,
        *,
        check_alignment: bool = False,
    ) -> Generator[TextLine, None, None]:
        """Yield the lines of bytes ``[start, end)``, never reading at or past ``end``.

        A line over ``max_line_bytes`` is not truncated: it arrives as overlapping windows
        (see ``TextLine``), so a minified JSON document on one line is searched in full while
        memory stays bounded. Raises ``UnsupportedDataFormatException`` for a multi-byte
        encoding or, with ``check_alignment``, a ``start`` inside a line. Raises
        ``DataSourceUnavailableException`` if the file ends before ``end``: a plan is made for
        one size, so a shorter file was truncated or replaced mid-run, and returning what is
        left would silently drop records.
        """
        if not encoding.ascii_compatible:
            raise UnsupportedDataFormatException(
                path.name,
                f"{encoding.name} data cannot be read by byte range; convert it to UTF-8 first",
            )
        max_line = self._max_line
        with path.open("rb") as handle:
            if end > os.fstat(handle.fileno()).st_size:
                raise DataSourceUnavailableException(path.name, _SHORTER_THAN_PLANNED)
            if check_alignment and start > 0:
                handle.seek(start - 1)
                if handle.read(1) != b"\n":
                    raise UnsupportedDataFormatException(
                        path.name, f"byte offset {start} is not on a record boundary"
                    )
            else:
                handle.seek(start)

            position = start
            while position < end:
                remaining = end - position
                raw = handle.readline(min(max_line, remaining))
                if not raw:
                    raise DataSourceUnavailableException(path.name, _SHORTER_THAN_PLANNED)
                total = len(raw)
                if not raw.endswith(b"\n") and total == max_line and total < remaining:
                    for window in self._windows(handle, path, encoding.name, raw, remaining):
                        position += window.byte_length
                        yield window
                    continue
                position += total
                yield TextLine(raw.decode(encoding.name, errors="replace").rstrip("\r\n"), total)

    def range_blocks(
        self,
        path: Path,
        encoding: EncodingInfo,
        start: int,
        end: int,
        *,
        check_alignment: bool = False,
    ) -> Generator[TextBlock, None, None]:
        """Yield the bytes ``[start, end)`` as blocks of whole lines, never reading past ``end``.

        A block is about one read buffer (``chunk_size_bytes``, at most ``max_line_bytes``) cut
        after its last line feed, so searching it is like searching those lines one by one
        without the cost of handling each. A line longer than the buffer is read whole if it
        fits ``max_line_bytes``, otherwise it arrives as windows, exactly as ``range_lines``
        delivers it. The errors are those of ``range_lines``.
        """
        if not encoding.ascii_compatible:
            raise UnsupportedDataFormatException(
                path.name,
                f"{encoding.name} data cannot be read by byte range; convert it to UTF-8 first",
            )
        codec = encoding.name
        block_size = max(min(self._chunk_size, self._max_line), 1)
        with path.open("rb") as handle:
            if end > os.fstat(handle.fileno()).st_size:
                raise DataSourceUnavailableException(path.name, _SHORTER_THAN_PLANNED)
            if check_alignment and start > 0:
                handle.seek(start - 1)
                if handle.read(1) != b"\n":
                    raise UnsupportedDataFormatException(
                        path.name, f"byte offset {start} is not on a record boundary"
                    )
            else:
                handle.seek(start)

            position = start
            while position < end:
                data = handle.read(min(block_size, end - position))
                if not data:
                    raise DataSourceUnavailableException(path.name, _SHORTER_THAN_PLANNED)
                if position + len(data) < end:  # more of the range follows: cut after a line feed
                    cut = data.rfind(b"\n") + 1
                    if cut == 0:  # no line feed at all: the buffer lies inside one long line
                        for block in self._long_line(handle, path, codec, data, end - position):
                            position += block.byte_length
                            yield block
                        continue
                    if cut < len(data):
                        handle.seek(position + cut)
                        data = data[:cut]
                text = data.decode(codec, errors="replace")
                lines = text.count("\n") + (0 if text.endswith("\n") else 1)
                position += len(data)
                yield TextBlock(text, len(data), lines)

    def _long_line(
        self, handle: BinaryIO, path: Path, codec: str, first: bytes, limit: int
    ) -> Generator[TextBlock, None, None]:
        """Deliver a line that began without a line feed in its first buffer.

        It is read on while it still fits ``max_line_bytes``; a line that does is one block, a
        longer one goes out as windows. ``limit`` counts the bytes of the range left, from the
        line start.
        """
        line = bytearray(first)
        ended = False
        while not ended and len(line) <= self._max_line:
            piece = handle.readline(min(self._chunk_size, limit - len(line)))
            if not piece:
                raise DataSourceUnavailableException(path.name, _SHORTER_THAN_PLANNED)
            line += piece
            ended = piece.endswith(b"\n") or len(line) >= limit
        if ended and len(line) <= self._max_line:
            yield TextBlock(line.decode(codec, errors="replace"), len(line), 1)
            return
        for window in self._windows(handle, path, codec, bytes(line), limit, ended=ended):
            yield TextBlock(
                window.text,
                window.byte_length,
                1 if window.emit_from == 0 else 0,
                window.emit_from,
                window.emit_until,
            )

    def _windows(
        self,
        handle: BinaryIO,
        path: Path,
        codec: str,
        first: bytes,
        limit: int,
        *,
        ended: bool = False,
    ) -> Generator[TextLine, None, None]:
        """Split a line longer than ``max_line_bytes`` into windows searched one by one.

        ``first`` is what was already read; ``limit`` is how many bytes of the range remain,
        counted from the line start. Window ``k`` owns the span ``[emit_from, emit_until)`` of
        its text. Around that span lie ``context`` bytes before it (so a look-behind sees what
        precedes) and ``overlap`` bytes after it (so a match that begins inside the span and
        runs past it is still found whole, provided it is shorter than ``overlap``). The
        span's end snaps to just after a comma, space, tab or semicolon when one is near,
        which keeps JSON escapes such as ``\\u0040`` in one piece, and to a character
        boundary in UTF-8. Memory stays near ``max_line_bytes``.
        """
        max_line = self._max_line
        span = max(max_line // 2, 256)
        overlap = min(4096, max_line // 4)
        context = min(256, max_line // 8)
        snap = min(4096, max_line // 8)
        utf8 = codec.lower().replace("_", "-") in ("utf-8", "ascii", "us-ascii")

        buffer = bytearray(first)
        base = 0  # offset of buffer[0] from the start of the line
        consumed = len(first)
        span_start = 0
        while True:
            wanted = span_start + span + overlap + snap
            while not ended and base + len(buffer) < wanted:
                piece = handle.readline(min(self._chunk_size, limit - consumed))
                if not piece:
                    raise DataSourceUnavailableException(path.name, _SHORTER_THAN_PLANNED)
                buffer += piece
                consumed += len(piece)
                ended = piece.endswith(b"\n") or consumed >= limit
            line_end = base + len(buffer)
            first_byte = max(span_start - context, base)
            prefix = bytes(buffer[first_byte - base : span_start - base]).decode(codec, errors="replace")

            if ended and line_end - span_start <= span + overlap:
                text = bytes(buffer[first_byte - base :]).decode(codec, errors="replace")
                yield TextLine(text.rstrip("\r\n"), line_end - span_start, False, len(prefix), None)
                return

            span_end = self._snap(buffer, base, span_start + span, line_end - overlap, utf8, snap)
            window = bytes(buffer[first_byte - base : min(span_end + overlap, line_end) - base])
            owned = bytes(buffer[span_start - base : span_end - base]).decode(codec, errors="replace")
            yield TextLine(
                window.decode(codec, errors="replace"),
                span_end - span_start,
                False,
                len(prefix),
                len(prefix) + len(owned),
            )
            drop = max(span_end - context - base, 0)
            del buffer[:drop]
            base += drop
            span_start = span_end

    @staticmethod
    def _snap(
        buffer: bytearray, base: int, target: int, ceiling: int, utf8: bool, margin: int
    ) -> int:
        """Move ``target`` (a line offset) forward to just after a delimiter, within bounds."""
        index = target - base
        stop = min(index + margin, len(buffer), max(ceiling - base, index))
        found = (buffer.find(mark, index - 1, stop) for mark in (b",", b" ", b"\t", b";"))
        hits = [position for position in found if position != -1]
        if hits:
            index = min(hits) + 1
        if utf8:
            while index < len(buffer) and buffer[index] & 0xC0 == 0x80:
                index += 1
        return base + index

    def _byte_lines(
        self, path: Path, codec: str, offset: int, length: int, align: bool
    ) -> Generator[TextLine, None, None]:
        max_line = self._max_line
        with path.open("rb") as handle:
            if align and offset > 0:
                handle.seek(offset - 1)
                if handle.read(1) != b"\n":
                    self._skip_line(handle)
            else:
                handle.seek(offset)

            consumed = 0
            while consumed < length:
                raw = handle.readline(max_line)
                if not raw:
                    return
                total = len(raw)
                truncated = False
                if len(raw) == max_line and not raw.endswith(b"\n"):
                    skipped = self._skip_line(handle)
                    total += skipped
                    truncated = skipped > 0
                consumed += total
                yield TextLine(
                    raw.decode(codec, errors="replace").rstrip("\r\n"), total, truncated
                )

    def _skip_line(self, handle: BinaryIO) -> int:
        """Discard the remainder of the current line in constant space."""
        skipped = 0
        while True:
            piece = handle.readline(self._chunk_size)
            skipped += len(piece)
            if not piece or piece.endswith(b"\n"):
                return skipped

    def _wide_lines(
        self, path: Path, codec: str, offset: int, length: int
    ) -> Generator[TextLine, None, None]:
        """Stream lines of UTF-16/32 data through an incremental decoder.

        A line over ``max_line_bytes`` ends the stream early: skipping to the next newline would
        need unbounded decoder re-synchronisation, and such a file is not line-oriented text.
        """
        decoder = codecs.getincrementaldecoder(codec)(errors="replace")
        pending = ""
        consumed = 0
        with path.open("rb") as handle:
            handle.seek(offset)
            while consumed < length:
                block = handle.read(self._chunk_size)
                pending += decoder.decode(block, final=not block)
                parts = pending.split("\n")
                pending = parts.pop()
                for part in parts:
                    line_bytes = len((part + "\n").encode(codec))
                    consumed += line_bytes
                    yield TextLine(part.rstrip("\r"), line_bytes)
                    if consumed >= length:
                        return
                if len(pending.encode(codec)) > self._max_line:
                    return
                if not block:
                    if pending:
                        yield TextLine(pending.rstrip("\r"), len(pending.encode(codec)))
                    return
