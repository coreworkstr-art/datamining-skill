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
from datamining_skill.domain.models import EncodingInfo, TextLine

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

        Lines over ``max_line_bytes`` are truncated and flagged, their remainder skipped in
        constant space and within the range. Raises ``UnsupportedDataFormatException`` for a
        multi-byte encoding or, with ``check_alignment``, a ``start`` inside a line. Raises
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
                truncated = False
                if not raw.endswith(b"\n") and total == max_line and total < remaining:
                    skipped = self._skip_line_within(handle, remaining - total)
                    total += skipped
                    truncated = skipped > 0
                position += total
                yield TextLine(
                    raw.decode(encoding.name, errors="replace").rstrip("\r\n"), total, truncated
                )

    def _skip_line_within(self, handle: BinaryIO, limit: int) -> int:
        """Discard up to ``limit`` bytes of the current line; return the number skipped."""
        skipped = 0
        while skipped < limit:
            piece = handle.readline(min(self._chunk_size, limit - skipped))
            skipped += len(piece)
            if not piece or piece.endswith(b"\n"):
                break
        return skipped

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
