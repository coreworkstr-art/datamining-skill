"""A bounded look at the start of a result file, for people and assistants checking a run."""

from __future__ import annotations

import csv
import json
import random
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any, BinaryIO

from datamining_skill.application.pii_mask import mask_value

DEFAULT_PREVIEW_ROWS = 20
MAX_PREVIEW_ROWS = 200
_PREVIEW_BYTES = 256 * 1024
_MAX_JSON_DEPTH = 32
_MAX_SAMPLE_SCAN_BYTES = 64 * 1024 * 1024  # a sampling call reads no more than this, whatever the file's size
_MAX_LINE_BYTES = 1024 * 1024  # a longer line is skipped
_MAX_SAMPLE_RECORD_CHARS = 16 * 1024  # a longer record is not a candidate: 200 of them must still fit a response
_CELL_LIMIT = 128 * 1024  # a longer CSV value ends the preview, as the csv module's default limit did


def _is_shallow(value: Any) -> bool:
    """Whether ``value`` nests no deeper than a tool response can safely carry.

    Some interpreters parse JSON nested a hundred thousand levels deep, which no client or
    encoder handles; such a record is shown as text instead. Checked without recursion.
    """
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, dict | list):
            if depth > _MAX_JSON_DEPTH:
                return False
            children = item.values() if isinstance(item, dict) else item
            stack.extend((child, depth + 1) for child in children)
    return True


def preview_result(
    path: Path,
    rows: int = DEFAULT_PREVIEW_ROWS,
    *,
    mask: bool = False,
    sample: bool = False,
    seed: int | None = None,
) -> dict[str, Any]:
    """The first ``rows`` records of a ``.csv``, ``.jsonl`` or ``.ndjson`` result file.

    Reads at most 256 KiB, so any file size is safe. CSV rows come back as lists below
    ``columns``, JSON Lines records as objects. ``has_more`` says whether the file holds
    more than was returned. With ``mask`` every recognised piece of personal data is replaced
    by a tag (``application/pii_mask.py``) and ``pii_masked`` tells how many of each.

    With ``sample`` the records are drawn uniformly from the file (reading at most its first
    64 MiB) by reservoir sampling instead of being the first ones, and come back in file order;
    ``seed`` makes the draw repeatable. Records longer than 16 KiB are not candidates.
    """
    clamped = max(1, min(rows, MAX_PREVIEW_ROWS))
    result = _sample_preview(path, clamped, seed) if sample else _read_preview(path, clamped)
    if mask:
        counts: Counter[str] = Counter()
        for key in ("columns", "rows"):
            if key in result:
                result[key] = mask_value(result[key], counts)
        result["pii_masked"] = dict(sorted(counts.items()))
    return result


def _read_preview(path: Path, rows: int) -> dict[str, Any]:
    size = path.stat().st_size
    with path.open("rb") as handle:
        head = handle.read(_PREVIEW_BYTES)
    lines = head.decode("utf-8", errors="replace").split("\n")
    if size > len(head):
        lines.pop()  # the last line may be cut short
    elif lines and lines[-1] == "":
        lines.pop()
    lines = [line.rstrip("\r") for line in lines]
    complete = size <= len(head)

    if path.suffix.lower() == ".csv":
        table: list[list[str]] = []
        cut_short = False
        try:
            for row in csv.reader(line + "\n" for line in lines):  # a line break can be part of a value
                # the csv module's own limit is a setting of the whole process that other threads
                # raise, so the limit this preview stands by is checked here
                if any(len(cell) > _CELL_LIMIT for cell in row):
                    cut_short = True
                    break
                table.append(row)
                if len(table) > rows + 1:
                    break
        except csv.Error:  # a value the csv module itself refuses: show what came before it
            cut_short = True
        columns = table[0] if table else []
        records: list[Any] = table[1 : 1 + rows]
        has_more = len(table) - 1 > rows or not complete or cut_short
        return {
            "file": path.name,
            "format": "csv",
            "size_bytes": size,
            "columns": columns,
            "rows": records,
            "rows_returned": len(records),
            "has_more": has_more,
        }

    shown: list[Any] = []
    for line in lines[:rows]:
        try:
            parsed = json.loads(line)
        except (ValueError, RecursionError):  # not JSON, or nested deeper than the parser allows
            shown.append(line)
        else:
            shown.append(parsed if _is_shallow(parsed) else line)
    return {
        "file": path.name,
        "format": "jsonl",
        "size_bytes": size,
        "rows": shown,
        "rows_returned": len(shown),
        "has_more": len(lines) > rows or not complete,
    }


class _Scan:
    """Where a bounded scan of a file stands."""

    def __init__(self) -> None:
        self.bytes_read = 0
        self.stopped_early = False  # the byte budget ran out before the end of the file
        self.skipped = 0  # lines or records that were too long to be candidates


def _scan_lines(handle: BinaryIO, scan: _Scan) -> Iterator[str]:
    """Decoded lines without their terminator, in bounded memory; a line over 1 MiB comes back empty."""
    while True:
        if scan.bytes_read >= _MAX_SAMPLE_SCAN_BYTES:
            scan.stopped_early = bool(handle.peek(1)) if hasattr(handle, "peek") else True
            return
        raw = handle.readline(_MAX_LINE_BYTES + 1)
        if not raw:
            return
        scan.bytes_read += len(raw)
        if len(raw) > _MAX_LINE_BYTES and not raw.endswith(b"\n"):
            while True:  # drop the rest of the line without keeping it
                rest = handle.readline(_MAX_LINE_BYTES)
                scan.bytes_read += len(rest)
                if not rest or rest.endswith(b"\n"):
                    break
            scan.skipped += 1
            yield ""
            continue
        yield raw.decode("utf-8", errors="replace").rstrip("\r\n")


def _reservoir(candidates: Iterator[tuple[int, Any]], rows: int, rng: random.Random) -> list[tuple[int, Any]]:
    """Vitter's Algorithm R: ``rows`` of the candidates, each equally likely, in O(rows) memory.

    A candidate is ``(its index, its content)``; the result is in index order.
    """
    reservoir: list[tuple[int, Any]] = []
    for index, content in candidates:
        if index < rows:
            reservoir.append((index, content))
        else:
            slot = rng.randrange(index + 1)
            if slot < rows:
                reservoir[slot] = (index, content)
    reservoir.sort(key=lambda item: item[0])
    return reservoir


def _sample_preview(path: Path, rows: int, seed: int | None) -> dict[str, Any]:
    size = path.stat().st_size
    rng = random.Random(seed)
    scan = _Scan()
    seen = 0
    with path.open("rb") as handle:
        if path.suffix.lower() == ".csv":
            columns: list[str] = []
            header_read = False

            def records() -> Iterator[tuple[int, Any]]:
                nonlocal seen, columns, header_read
                reader = csv.reader(line + "\n" for line in _scan_lines(handle, scan))
                while True:
                    try:
                        row = next(reader)
                    except StopIteration:
                        return
                    except csv.Error:  # a value the csv module itself refuses: nothing more can be read safely
                        scan.stopped_early = True
                        return
                    if not row:
                        continue
                    if sum(len(cell) for cell in row) > _MAX_SAMPLE_RECORD_CHARS or any(
                        len(cell) > _CELL_LIMIT for cell in row
                    ):
                        scan.skipped += 1
                        if not header_read:
                            header_read = True
                        continue
                    if not header_read:
                        columns, header_read = row, True
                        continue
                    yield seen, row
                    seen += 1

            chosen = _reservoir(records(), rows, rng)
            sampled: list[Any] = [content for _, content in chosen]
            result: dict[str, Any] = {"file": path.name, "format": "csv", "columns": columns}
        else:

            def lines() -> Iterator[tuple[int, Any]]:
                nonlocal seen
                for line in _scan_lines(handle, scan):
                    if not line.strip():
                        continue
                    if len(line) > _MAX_SAMPLE_RECORD_CHARS:
                        scan.skipped += 1
                        continue
                    yield seen, line
                    seen += 1

            chosen = _reservoir(lines(), rows, rng)
            sampled = []
            for _, line in chosen:
                try:
                    parsed = json.loads(line)
                except (ValueError, RecursionError):
                    sampled.append(line)
                else:
                    sampled.append(parsed if _is_shallow(parsed) else line)
            result = {"file": path.name, "format": "jsonl"}
    result.update(
        {
            "size_bytes": size,
            "rows": sampled,
            "rows_returned": len(sampled),
            "has_more": seen > len(sampled) or scan.stopped_early,
            "sampled": True,
            "records_sampled_from": seen,
        }
    )
    if scan.stopped_early:
        result["scan_stopped_at_bytes"] = scan.bytes_read
    if scan.skipped:
        result["skipped_too_long"] = scan.skipped
    return result
