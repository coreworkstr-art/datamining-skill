"""Delimited-text (CSV/TSV) recognition and header extraction."""

from __future__ import annotations

import csv
from collections import Counter
from collections.abc import Iterator
from itertools import islice

from datamining_skill.domain.models import DataFormat, StructureAnalysis, StructureInfo, TextLine

_MAX_SAMPLE_RECORDS = 200
_MIN_CONSISTENCY = 0.9
_HEADERLESS_FACTOR = 0.8
_MAX_COLUMN_NAME_CHARS = 64


def _looks_like_column_name(cell: str) -> bool:
    """Short and not starting with a digit; rules out numbers, dates and free-text fragments."""
    name = cell.strip()
    return 0 < len(name) <= _MAX_COLUMN_NAME_CHARS and not name[0].isdigit()


class CsvHandler:
    """Recognises delimited tables by field-count consistency.

    Sampled lines are tokenised with ``csv`` per candidate delimiter, and the delimiter whose
    rows most consistently give the same field count (at least two) wins, so single-column
    files are rejected. The first row is a header if its cells are unique, short and do not
    start with a digit.
    """

    data_format = DataFormat.CSV
    extensions = frozenset({".csv", ".tsv"})

    def __init__(
        self, max_fields: int = 1_024, delimiters: tuple[str, ...] = (",", ";", "\t", "|")
    ) -> None:
        self._max_fields = max_fields
        self._delimiters = delimiters

    def analyze(self, lines: Iterator[TextLine], max_records: int) -> StructureAnalysis | None:
        # bounded by the head-sample byte budget, never by file size
        sample = list(islice(lines, min(max_records, _MAX_SAMPLE_RECORDS)))
        if not sample:
            return None

        best_candidate: tuple[float, int, str, list[tuple[list[str], int]]] | None = None
        for delimiter in self._delimiters:
            rows = self._tokenize(sample, delimiter)
            if rows is None or not rows:
                continue
            widths = Counter(len(row) for row, _ in rows)
            width, frequency = widths.most_common(1)[0]
            if width < 2 or width > self._max_fields:
                continue
            consistency = frequency / len(rows)
            if consistency < _MIN_CONSISTENCY:
                continue
            candidate = (consistency, width, delimiter, rows)
            if best_candidate is None or candidate[:2] > best_candidate[:2]:
                best_candidate = candidate
        if best_candidate is None:
            return None

        consistency, width, delimiter, rows = best_candidate
        header_row, header_end = rows[0]
        has_header = (
            len({cell.strip() for cell in header_row}) == len(header_row)
            and all(_looks_like_column_name(cell) for cell in header_row)
        )
        if has_header:
            fields = tuple(cell.strip() for cell in header_row)
            header_bytes = header_end
            data_rows = len(rows) - 1
        else:
            fields = tuple(f"column_{index}" for index in range(1, width + 1))
            header_bytes = 0
            data_rows = len(rows)

        # penalise weak evidence: tiny samples, and headerless tables (imitated by free text with commas)
        confidence = consistency
        if len(rows) < 3:
            confidence *= 0.8
        if not has_header:
            confidence *= _HEADERLESS_FACTOR
        return StructureAnalysis(
            confidence=confidence,
            structure=StructureInfo(
                fields=fields,
                sampled_records=data_rows,
                delimiter=delimiter,
                has_header=has_header,
            ),
            header_bytes=header_bytes,
        )

    @staticmethod
    def _tokenize(
        sample: list[TextLine], delimiter: str
    ) -> list[tuple[list[str], int]] | None:
        """Parse the sample into non-blank rows, each with its end byte offset."""
        consumed = 0

        def feed() -> Iterator[str]:
            nonlocal consumed
            for line in sample:
                consumed += line.byte_length
                yield line.text

        rows: list[tuple[list[str], int]] = []
        try:
            for row in csv.reader(feed(), delimiter=delimiter):
                if row:
                    rows.append((row, consumed))
        except csv.Error:
            return None
        return rows
