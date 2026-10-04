"""Recognises Apache access, syslog and ISO-timestamp log layouts."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from itertools import islice

from datamining_skill.domain.models import DataFormat, StructureAnalysis, StructureInfo, TextLine

# only a line's prefix is matched: bounds regex work and rules out backtracking on very long lines
_MATCH_PREFIX_CHARS = 2_048
_MIN_MATCH_RATIO = 0.8
# heuristic detection: capped so JSONL and headered CSV win ties, yet above headerless delimiter look-alikes
_CONFIDENCE_CEILING = 0.95


@dataclass(frozen=True, slots=True)
class _LogPattern:
    name: str
    regex: re.Pattern[str]

    @property
    def fields(self) -> tuple[str, ...]:
        groups = self.regex.groupindex
        return tuple(sorted(groups, key=groups.__getitem__))


_PATTERNS: tuple[_LogPattern, ...] = (
    _LogPattern(
        "apache_access",
        re.compile(
            r'^(?P<remote_host>\S+) (?P<ident>\S+) (?P<user>\S+) '
            r'\[(?P<time>[^\]]+)\] "(?P<request>[^"]*)" '
            r'(?P<status>\d{3}) (?P<bytes>\d+|-)'
            r'(?: "(?P<referer>[^"]*)" "(?P<user_agent>[^"]*)")?'
        ),
    ),
    _LogPattern(
        "syslog",
        re.compile(
            r"^(?P<timestamp>[A-Z][a-z]{2} [ \d]\d \d{2}:\d{2}:\d{2}) "
            r"(?P<host>\S+) (?P<process>[^\s:\[]+)(?:\[(?P<pid>\d+)\])?: (?P<message>.*)$"
        ),
    ),
    _LogPattern(
        "iso_timestamp",
        re.compile(
            r"^\[?(?P<timestamp>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"
            r"(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?)\]?[ \t]+"
            r"(?:\[?(?P<level>TRACE|DEBUG|INFO|NOTICE|WARN|WARNING|ERROR|CRITICAL|FATAL)\]?[ \t:-]*)?"
            r"(?P<message>.*)$"
        ),
    ),
)


class LogHandler:
    """Accepts a layout when at least 80% of sampled non-empty lines match, tolerating stack-trace continuations."""

    data_format = DataFormat.LOG
    extensions = frozenset({".log", ".out", ".txt"})

    def analyze(self, lines: Iterator[TextLine], max_records: int) -> StructureAnalysis | None:
        non_empty = (line for line in lines if line.text.strip())
        matches = [0] * len(_PATTERNS)
        total = 0

        for line in islice(non_empty, max_records):
            total += 1
            prefix = line.text[:_MATCH_PREFIX_CHARS]
            for index, pattern in enumerate(_PATTERNS):
                if pattern.regex.match(prefix):
                    matches[index] += 1

        if total == 0:
            return None
        best_index = max(range(len(_PATTERNS)), key=matches.__getitem__)
        ratio = matches[best_index] / total
        if ratio < _MIN_MATCH_RATIO:
            return None
        pattern = _PATTERNS[best_index]
        return StructureAnalysis(
            confidence=ratio * _CONFIDENCE_CEILING,
            structure=StructureInfo(
                fields=pattern.fields,
                sampled_records=matches[best_index],
                pattern=pattern.name,
            ),
        )
