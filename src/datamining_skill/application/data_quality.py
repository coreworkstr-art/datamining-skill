"""Single-pass data-quality statistics for CSV and JSON Lines records.

``QualityAccumulator`` is fed one record at a time and keeps only fixed-size counters, so a
file of any size is checked in constant memory: at most ``MAX_TRACKED_FIELDS`` fields are
followed, each with one counter per value type and the first line each type appeared on. It
reports malformed records, how complete each field is, and *schema drift*, a field whose values
are of different types. It never stores a value, only counts and line numbers
(docs/architecture.md, "Data quality").
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

MAX_TRACKED_FIELDS = 512
MAX_EXAMPLES = 5
MAX_ISSUES = 25

EMPTY = "empty"
NULL = "null"
INTEGER = "integer"
FLOAT = "float"
BOOLEAN = "boolean"
DATE = "date"
DATETIME = "datetime"
STRING = "string"
OBJECT = "object"
ARRAY = "array"

_NULL_KINDS = frozenset({EMPTY, NULL})
# integers and floats are both numbers, a date and a datetime are both points in time: mixing
# within a group is normal and not reported as drift
_GROUP = {INTEGER: "number", FLOAT: "number", DATE: "temporal", DATETIME: "temporal"}
_NULL_TOKENS = frozenset({"null", "none", "nan", "n/a", "na", "nil", "\\n"})  # "\N" is MySQL's NULL
_BOOLEANS = frozenset({"true", "false"})
_INTEGER = re.compile(r"[+-]?\d{1,30}")
_FLOAT = re.compile(r"[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_DATETIME = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,9})?)?(?:Z|[+-]\d{2}:?\d{2})?")
_NUMBER_START = frozenset("0123456789+-.")


def classify_text(value: str) -> str:
    """The type of a CSV cell: ``empty``, ``null`` (a token such as ``NULL``), a number, a
    boolean, a date, a date and time, or ``string``."""
    stripped = value.strip()
    if not stripped:
        return EMPTY
    if stripped[0] in _NUMBER_START:
        if _INTEGER.fullmatch(stripped):
            return INTEGER
        if _FLOAT.fullmatch(stripped):
            return FLOAT
        if len(stripped) >= 10 and stripped[4] == "-":
            if _DATE.fullmatch(stripped):
                return DATE
            if _DATETIME.fullmatch(stripped):
                return DATETIME
        return STRING
    lowered = stripped.lower()
    if lowered in _BOOLEANS:
        return BOOLEAN
    if lowered in _NULL_TOKENS:
        return NULL
    return STRING


def classify_json(value: object) -> str:
    """The type of a JSON value. A string that looks like a number stays a string."""
    if value is None:
        return NULL
    if isinstance(value, bool):
        return BOOLEAN
    if isinstance(value, int):
        return INTEGER
    if isinstance(value, float):
        return FLOAT
    if isinstance(value, str):
        return STRING if value.strip() else EMPTY
    if isinstance(value, dict):
        return OBJECT
    return ARRAY


def unique_names(names: Sequence[str]) -> list[str]:
    """``names`` with repeated and blank names made distinct (``a``, ``a#2``, ``column_3``)."""
    seen: Counter[str] = Counter()
    result: list[str] = []
    for index, name in enumerate(names, 1):
        base = name.strip() or f"column_{index}"
        seen[base] += 1
        result.append(base if seen[base] == 1 else f"{base}#{seen[base]}")
    return result


class _FieldStats:
    __slots__ = ("first_line", "types")

    def __init__(self) -> None:
        self.types: Counter[str] = Counter()
        self.first_line: dict[str, int] = {}

    def add(self, kind: str, line: int) -> None:
        self.types[kind] += 1
        if kind not in self.first_line:
            self.first_line[kind] = line


class QualityAccumulator:
    """Counts what a stream of records looks like; see the module docstring."""

    def __init__(self, data_format: str) -> None:
        self._format = data_format
        self.valid = 0
        self.blank_lines = 0
        self.undecodable_lines = 0
        self.untracked_values = 0
        self._malformed: Counter[str] = Counter()
        self._examples: list[dict[str, Any]] = []
        self._fields: dict[str, _FieldStats] = {}
        self._columns: list[str] = []
        self._notes: list[str] = []

    def note(self, text: str) -> None:
        """A finding about the file as a whole, listed before the findings about fields."""
        self._notes.append(text)

    @property
    def total(self) -> int:
        """Records examined so far, valid or not."""
        return self.valid + sum(self._malformed.values())

    def set_columns(self, names: Sequence[str]) -> None:
        """Fix the column names of a CSV; every later row must have exactly this many cells."""
        self._columns = unique_names(names)
        for name in self._columns[:MAX_TRACKED_FIELDS]:
            self._fields[name] = _FieldStats()

    def malformed(self, line: int, problem: str) -> None:
        self._malformed[problem] += 1
        if len(self._examples) < MAX_EXAMPLES:
            self._examples.append({"line": line, "problem": problem})

    def add_row(self, line: int, row: Sequence[str]) -> None:
        """A CSV row that started on ``line``. Rows of the wrong length are malformed."""
        expected = len(self._columns)
        if len(row) != expected:
            self.malformed(line, "too_few_columns" if len(row) < expected else "too_many_columns")
            return
        self.valid += 1
        tracked = min(expected, MAX_TRACKED_FIELDS)
        for index in range(tracked):
            self._fields[self._columns[index]].add(classify_text(row[index]), line)
        self.untracked_values += expected - tracked

    def add_object(self, line: int, record: Mapping[str, Any]) -> None:
        """A JSON Lines record that is a JSON object."""
        self.valid += 1
        for key, value in record.items():
            stats = self._fields.get(key)
            if stats is None:
                if len(self._fields) >= MAX_TRACKED_FIELDS:
                    self.untracked_values += 1
                    continue
                stats = self._fields[key] = _FieldStats()
            stats.add(classify_json(value), line)

    def report(self) -> dict[str, Any]:
        """The findings as a JSON-serialisable dictionary."""
        fields: list[dict[str, Any]] = []
        field_issues: list[str] = []
        drifting = 0
        for name, stats in self._fields.items():
            entry, found, drifted = self._describe(name, stats)
            fields.append(entry)
            field_issues.extend(found)
            drifting += drifted
        malformed_total = sum(self._malformed.values())
        # findings about the whole file come first: a long list of field findings must not hide them
        issues: list[str] = list(self._notes)
        if malformed_total:
            kinds = ", ".join(f"{kind} ({count:,})" for kind, count in self._malformed.most_common())
            issues.append(f"{malformed_total:,} of {self.total:,} records are malformed: {kinds}")
        if self.undecodable_lines:
            issues.append(f"{self.undecodable_lines:,} lines contain bytes that are not valid in the detected encoding")
        if self.untracked_values:
            issues.append(f"more than {MAX_TRACKED_FIELDS} fields: values of the others are not analysed")
        issues.extend(field_issues)
        shown = issues[:MAX_ISSUES]
        if len(issues) > MAX_ISSUES:
            shown.append(f"and {len(issues) - MAX_ISSUES} more")
        return {
            "format": self._format,
            "summary": {
                "records_valid": self.valid,
                "records_malformed": malformed_total,
                "fields": len(fields),
                "fields_with_drift": drifting,
                "issues": len(issues),
            },
            "records": {
                "valid": self.valid,
                "malformed": malformed_total,
                "blank_lines": self.blank_lines,
                "undecodable_lines": self.undecodable_lines,
            },
            "malformed": {"by_kind": dict(self._malformed.most_common()), "examples": self._examples},
            "fields": fields,
            "issues": shown,
        }

    def _describe(self, name: str, stats: _FieldStats) -> tuple[dict[str, Any], list[str], int]:
        present = sum(stats.types.values())
        nulls = sum(count for kind, count in stats.types.items() if kind in _NULL_KINDS)
        missing = max(self.valid - present, 0)
        groups: Counter[str] = Counter()
        for kind, count in stats.types.items():
            if kind not in _NULL_KINDS:
                groups[_GROUP.get(kind, kind)] += count
        drift = len(groups) > 1
        entry: dict[str, Any] = {
            "name": name,
            "present": present,
            "missing": missing,
            "empty_or_null": nulls,
            "completeness": round(1 - (nulls + missing) / self.valid, 4) if self.valid else None,
            "types": dict(stats.types.most_common()),
            "dominant_type": groups.most_common(1)[0][0] if groups else None,
            "drift": drift,
            "first_seen_line": dict(stats.first_line),
        }
        issues: list[str] = []
        if drift:
            top = groups.most_common()
            mixed = " and ".join(f"{kind} ({count:,})" for kind, count in top[:3])
            minority = top[1][0]
            first = min(line for kind, line in stats.first_line.items() if _GROUP.get(kind, kind) == minority)
            issues.append(f"field '{name}' mixes {mixed}; {minority} first appears on line {first:,}")
        if self.valid and nulls + missing >= self.valid:
            issues.append(f"field '{name}' never has a value")
        elif self.valid and (nulls + missing) / self.valid >= 0.5:
            share = round(100 * (nulls + missing) / self.valid)
            issues.append(f"field '{name}' is empty or missing in {share}% of records")
        return entry, issues, int(drift)
