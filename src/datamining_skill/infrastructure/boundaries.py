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
                index = self._last_unquoted(block)
                if index != -1:
                    return begin + index + 1
                end = begin
        return None

    @classmethod
    def _last_unquoted(cls, block: bytes) -> int:
        """Index of the last newline in ``block``, preferring one outside a quoted value.

        A newline inside a quoted CSV value (``"line 1\\nline 2"``) is not a record end. Within the
        block, a newline is taken to be inside quotes when an odd number of double quotes comes
        before it (a doubled quote ``""`` adds two, so it does not change that). The block is
        assumed to begin outside quotes, which is a guess for a block cut from the middle of a
        file, so when every newline looks quoted the last one is used after all: a boundary is
        found whenever there is a newline, as before.
        """
        last = block.rfind(cls._SEPARATOR)
        if last == -1 or b'"' not in block:
            return last
        quotes_in_block = block.count(b'"')
        quotes_from_here = 0  # quotes at and after the newline under test
        upper = len(block)
        index = last
        while index != -1:
            quotes_from_here += block.count(b'"', index, upper)
            upper = index
            if (quotes_in_block - quotes_from_here) % 2 == 0:
                return index
            index = block.rfind(cls._SEPARATOR, 0, index)
        return last
