"""Result-file formatters. Output is UTF-8 with ``\\n`` endings, so record sizes do not depend on the platform."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Sequence

_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


class CsvFormatter:
    """RFC 4180-style CSV with a header row.

    ``formula_guard`` prefixes cells beginning with ``= + - @`` (or tab, CR) with an apostrophe
    so spreadsheets do not run mined text as a formula (CSV injection). Off by default
    because it alters values.
    """

    def __init__(self, fields: Sequence[str], *, formula_guard: bool = False) -> None:
        self._fields = tuple(fields)
        self._guard = formula_guard
        self._buffer = io.StringIO()
        self._writer = csv.writer(self._buffer, lineterminator="\n")

    def header(self) -> bytes:
        return self._encode(self._fields)

    def format(self, record: Sequence[str]) -> bytes:
        if len(record) != len(self._fields):
            raise ValueError(f"record has {len(record)} values, expected {len(self._fields)}")
        if self._guard:
            record = [f"'{cell}" if cell.startswith(_FORMULA_TRIGGERS) else cell for cell in record]
        return self._encode(record)

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
