"""Finds record separators by bounded backward seeking."""

from __future__ import annotations

from pathlib import Path


class NewlineBoundaryLocator:
    """``BoundaryLocator`` for newline-delimited data.

    Scans ``[start, limit)`` backwards in ``block_bytes`` blocks: one block in memory, and
    usually a single small read since a newline is close to the candidate end.
    """

    _SEPARATOR = b"\n"

    def __init__(self, block_bytes: int) -> None:
        self._block = block_bytes

    def last_boundary(self, path: Path, start: int, limit: int) -> int | None:
        with path.open("rb") as handle:
            end = limit
            while end > start:
                begin = max(start, end - self._block)
                handle.seek(begin)
                block = handle.read(end - begin)
                index = block.rfind(self._SEPARATOR)
                if index != -1:
                    return begin + index + 1
                end = begin
        return None
