"""Streams a CSV or JSON Lines file through ``QualityAccumulator`` (docs/architecture.md, "Data quality").

One pass, constant memory: lines come from ``FileStreamReader`` (a line over the size cap is
counted, never parsed), CSV is parsed with the standard ``csv`` module in strict mode so that a
broken quote is an error and not silently accepted, and JSON Lines with ``json``. Only counts
and line numbers are kept, never values.
"""

from __future__ import annotations

import csv
import json
import time
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
_CLOCK_EVERY = 256  # records between two looks at the clock
_SUPPORTED = "CSV, TSV and JSON Lines"
_SURE_ENCODING = 0.9  # ASCII, UTF-8 and anything with a byte-order mark are above this; a guess is not


def scan_quality(
    path: Path,
    profile: FileProfile,
    reader: FileStreamReader,
    *,
    max_records: int = DEFAULT_MAX_RECORDS,
    max_seconds: float | None = None,
) -> dict[str, Any]:
    """Check up to ``max_records`` records of ``path`` (already profiled as ``profile``).

    With ``max_seconds`` the scan also stops once that much time has passed (looked at every
    few hundred records), and the report says so: a client that gives up on a call after a
    minute still gets an answer, about the part of the file that was read.
    """
    if profile.data_format not in (DataFormat.CSV, DataFormat.JSONL):
        raise UnsupportedDataFormatException(
            path.name,
            f"quality checks cover {_SUPPORTED} files; this file looks like {profile.data_format.value}",
        )
    max_records = max(1, min(max_records, MAX_MAX_RECORDS))
    encoding = profile.encoding
    start = encoding.bom_length
    remaining = max(profile.size_bytes - start, 0)
    # a table with another delimiter than the comma is usually a Turkish or European export, where a
    # comma inside a cell is a decimal point
    decimal_comma = profile.data_format is DataFormat.CSV and (profile.structure.delimiter or ",") != ","
    accumulator = QualityAccumulator(profile.data_format.value, decimal_comma=decimal_comma)
    if encoding.confidence < _SURE_ENCODING:
        accumulator.note(
            f"the encoding is a guess ({encoding.name}, confidence {encoding.confidence:.2f}): the file is "
            "not valid UTF-8, so non-ASCII text may be read wrongly; re-save it as UTF-8"
        )
    progress = _Progress()
    deadline = None if max_seconds is None else time.monotonic() + max_seconds
    lines = reader.lines(path, encoding, start, remaining)
    if profile.data_format is DataFormat.CSV:
        _scan_csv(lines, profile, accumulator, progress, max_records, deadline)
    else:
        _scan_json_lines(lines, accumulator, progress, max_records, deadline)
    report = accumulator.report()
    report["encoding"] = encoding.to_dict()
    complete = progress.bytes_read >= remaining
    if complete:
        note = None
    elif progress.timed_out:
        note = (
            f"stopped after {accumulator.total:,} records because the {max_seconds:g}-second time limit "
            "was reached; raise max_seconds, or read less with max_records, to see more"
        )
    else:
        note = f"stopped after {accumulator.total:,} records; raise max_records to read on"
    report["scope"] = {
        "records_examined": accumulator.total,
        "lines_read": progress.line,
        "bytes_read": progress.bytes_read,
        "max_records": max_records,
        **({} if max_seconds is None else {"max_seconds": max_seconds}),
        "complete": complete,
        "note": note,
    }
    return report


class _Progress:
    def __init__(self) -> None:
        self.line = 0
        self.bytes_read = 0
        self.stand_ins = 0  # empty lines fed to the CSV parser in place of lines that were too long
        self.timed_out = False  # the time limit, not the record limit or the end of the file, stopped the scan


def _scan_json_lines(
    lines: Iterator[TextLine],
    accumulator: QualityAccumulator,
    progress: _Progress,
    max_records: int,
    deadline: float | None,
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
        if deadline is not None and progress.line % _CLOCK_EVERY == 0 and time.monotonic() >= deadline:
            progress.timed_out = True
            return


def _scan_csv(
    lines: Iterator[TextLine],
    profile: FileProfile,
    accumulator: QualityAccumulator,
    progress: _Progress,
    max_records: int,
    deadline: float | None,
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
    # the limit is process-wide and shared with other threads, so it is only raised, never restored;
    # the limit this scan stands by is enforced on every row below as well
    if csv.field_size_limit() < _CSV_FIELD_LIMIT:
        csv.field_size_limit(_CSV_FIELD_LIMIT)
    stalled = 0
    rows_seen = 0
    while accumulator.total < max_records:
        rows_seen += 1
        if deadline is not None and rows_seen % _CLOCK_EVERY == 0 and time.monotonic() >= deadline:
            progress.timed_out = True
            return
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
        if any(len(cell) > _CSV_FIELD_LIMIT for cell in row):
            accumulator.malformed(record_line, "field_too_large")
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
