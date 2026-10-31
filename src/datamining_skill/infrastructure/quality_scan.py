"""Streams a CSV or JSON Lines file through ``QualityAccumulator`` (docs/architecture.md, "Data quality").

One pass, constant memory: lines come from ``FileStreamReader`` (a line over the size cap is
counted, never parsed), CSV is parsed with the standard ``csv`` module in strict mode so that a
broken quote is an error and not silently accepted, and JSON Lines with ``json``. Only counts
and line numbers are kept, never values.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from datamining_skill.application.data_quality import QualityAccumulator
from datamining_skill.domain.exceptions import UnsupportedDataFormatException
from datamining_skill.domain.models import DataFormat, FileProfile, TextLine
from datamining_skill.infrastructure.streaming import FileStreamReader

DEFAULT_MAX_RECORDS = 1_000_000
MAX_MAX_RECORDS = 100_000_000
_CSV_FIELD_LIMIT = 4 * 1024 * 1024
_SUPPORTED = "CSV, TSV and JSON Lines"
_SURE_ENCODING = 0.9  # ASCII, UTF-8 and anything with a byte-order mark are above this; a guess is not


def scan_quality(
    path: Path, profile: FileProfile, reader: FileStreamReader, *, max_records: int = DEFAULT_MAX_RECORDS
) -> dict[str, Any]:
    """Check up to ``max_records`` records of ``path`` (already profiled as ``profile``)."""
    if profile.data_format not in (DataFormat.CSV, DataFormat.JSONL):
        raise UnsupportedDataFormatException(
            path.name,
            f"quality checks cover {_SUPPORTED} files; this file looks like {profile.data_format.value}",
        )
    max_records = max(1, min(max_records, MAX_MAX_RECORDS))
    encoding = profile.encoding
    start = encoding.bom_length
    remaining = max(profile.size_bytes - start, 0)
    accumulator = QualityAccumulator(profile.data_format.value)
    if encoding.confidence < _SURE_ENCODING:
        accumulator.note(
            f"the encoding is a guess ({encoding.name}, confidence {encoding.confidence:.2f}): the file is "
            "not valid UTF-8, so non-ASCII text may be read wrongly; re-save it as UTF-8"
        )
    progress = _Progress()
    lines = reader.lines(path, encoding, start, remaining)
    if profile.data_format is DataFormat.CSV:
        _scan_csv(lines, profile, accumulator, progress, max_records)
    else:
        _scan_json_lines(lines, accumulator, progress, max_records)
    report = accumulator.report()
    report["encoding"] = encoding.to_dict()
    complete = progress.bytes_read >= remaining
    report["scope"] = {
        "records_examined": accumulator.total,
        "lines_read": progress.line,
        "bytes_read": progress.bytes_read,
        "max_records": max_records,
        "complete": complete,
        "note": None if complete else f"stopped after {accumulator.total:,} records; raise max_records to read on",
    }
    return report


class _Progress:
    def __init__(self) -> None:
        self.line = 0
        self.bytes_read = 0
        self.stand_ins = 0  # empty lines fed to the CSV parser in place of lines that were too long


def _scan_json_lines(
    lines: Iterator[TextLine], accumulator: QualityAccumulator, progress: _Progress, max_records: int
) -> None:
    for item in lines:
        progress.line += 1
        progress.bytes_read += item.byte_length
        if item.truncated:
            accumulator.malformed(progress.line, "line_too_long")
        elif not item.text.strip():
            accumulator.blank_lines += 1
        else:
            if "�" in item.text:
                accumulator.undecodable_lines += 1
            try:
                record = json.loads(item.text)
            except (ValueError, RecursionError):
                accumulator.malformed(progress.line, "invalid_json")
            else:
                if isinstance(record, dict):
                    accumulator.add_object(progress.line, record)
                else:
                    accumulator.malformed(progress.line, "not_an_object")
        if accumulator.total >= max_records:
            return


def _scan_csv(
    lines: Iterator[TextLine],
    profile: FileProfile,
    accumulator: QualityAccumulator,
    progress: _Progress,
    max_records: int,
) -> None:
    structure = profile.structure
    delimiter = structure.delimiter or ","

    def feed() -> Iterator[str]:
        for item in lines:
            progress.line += 1
            progress.bytes_read += item.byte_length
            if item.truncated:
                accumulator.malformed(progress.line, "line_too_long")
                progress.stand_ins += 1
                yield "\n"
                continue
            if "�" in item.text:
                accumulator.undecodable_lines += 1
            yield item.text + "\n"  # the break is part of a quoted value that spans lines

    reader = csv.reader(feed(), delimiter=delimiter, strict=True)
    header_pending = structure.has_header is not False
    if not header_pending:
        accumulator.set_columns(structure.fields)
    previous_limit = csv.field_size_limit(_CSV_FIELD_LIMIT)  # restored below, as the CSV profiler does
    try:
        stalled = 0
        while accumulator.total < max_records:
            record_line = progress.line + 1
            lines_before = progress.line
            try:
                row = next(reader)
            except StopIteration:
                return
            except csv.Error as problem:
                kind = "field_too_large" if "field larger" in str(problem) else "bad_quoting"
                accumulator.malformed(record_line, kind)
                stalled = stalled + 1 if progress.line == lines_before else 0
                if stalled > 3:  # defensive: an error that consumes nothing must not loop forever
                    return
                continue
            if not row:
                if progress.stand_ins:
                    progress.stand_ins -= 1  # already counted as a line that was too long
                else:
                    accumulator.blank_lines += 1
                continue
            if header_pending:
                header_pending = False
                accumulator.set_columns(row)
                continue
            accumulator.add_row(record_line, row)
    finally:
        csv.field_size_limit(previous_limit)
