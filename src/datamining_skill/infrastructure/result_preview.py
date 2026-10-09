"""A bounded look at the start of a result file, for people and assistants checking a run."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

DEFAULT_PREVIEW_ROWS = 20
MAX_PREVIEW_ROWS = 200
_PREVIEW_BYTES = 256 * 1024
_MAX_JSON_DEPTH = 32


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


def preview_result(path: Path, rows: int = DEFAULT_PREVIEW_ROWS) -> dict[str, Any]:
    """The first ``rows`` records of a ``.csv``, ``.jsonl`` or ``.ndjson`` result file.

    Reads at most 256 KiB, so any file size is safe. CSV rows come back as lists below
    ``columns``, JSON Lines records as objects. ``has_more`` says whether the file holds
    more than was returned.
    """
    rows = max(1, min(rows, MAX_PREVIEW_ROWS))
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
                table.append(row)
                if len(table) > rows + 1:
                    break
        except csv.Error:  # a value over the csv module's 128 KiB limit: show what came before it
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
