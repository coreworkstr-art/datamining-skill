"""Result-file formatters. Output is UTF-8 with ``\\n`` endings, so record sizes do not depend on the platform."""

from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Sequence

_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")
# a value that needs no quoting in any Python version: not empty, no delimiter, quote or line break
_NEEDS_THE_CSV_WRITER = re.compile(r'[,"\r\n]|^$')


class CsvFormatter:
    """RFC 4180-style CSV with a header row.

    ``formula_guard`` prefixes cells beginning with ``= + - @`` (or tab, CR) with an apostrophe
    so spreadsheets do not run mined text as a formula (CSV injection). Off by default
    because it alters values.
    """

    def __init__(self, fields: Sequence[str], *, formula_guard: bool = False) -> None:
        self._fields = tuple(fields)
        self._width = len(self._fields)
        self._guard = formula_guard
        self._buffer = io.StringIO()
        self._writer = csv.writer(self._buffer, lineterminator="\n")

    def header(self) -> bytes:
        return self._encode(self._fields)

    def format(self, record: Sequence[str]) -> bytes:
        if len(record) != self._width:
            raise ValueError(f"record has {len(record)} values, expected {self._width}")
        if self._guard:
            record = [f"'{cell}" if cell.startswith(_FORMULA_TRIGGERS) else cell for cell in record]
        search = _NEEDS_THE_CSV_WRITER.search
        for cell in record:
            if search(cell):
                return self._encode(record)
        return (",".join(record) + "\n").encode("utf-8")  # the same bytes, without the csv module

    def format_clean(self, record: Sequence[str]) -> bytes:
        """Like ``format`` for values known to hold no comma, quote or line break (never empty)."""
        if len(record) != self._width:
            raise ValueError(f"record has {len(record)} values, expected {self._width}")
        if self._guard:
            record = [f"'{cell}" if cell.startswith(_FORMULA_TRIGGERS) else cell for cell in record]
        return (",".join(record) + "\n").encode("utf-8")

    def _encode(self, row: Sequence[str]) -> bytes:
        self._writer.writerow(row)
        encoded_record = self._buffer.getvalue().encode("utf-8")
        self._buffer.seek(0)
        self._buffer.truncate(0)
        return encoded_record


class JsonlFormatter:
    """One compact JSON object per line, no header."""

    def __init__(self, fields: Sequence[str]) -> None:
        self._fields = tuple(fields)

    def header(self) -> bytes:
        return b""

    def format(self, record: Sequence[str]) -> bytes:
        if len(record) != len(self._fields):
            raise ValueError(f"record has {len(record)} values, expected {len(self._fields)}")
        document = json.dumps(
            dict(zip(self._fields, record, strict=True)), ensure_ascii=False, separators=(",", ":")
        )
        return (document + "\n").encode("utf-8")
