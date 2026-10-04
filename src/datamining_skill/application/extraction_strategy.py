"""Pluggable mining logic: a pure function from one line of text to zero or more records, with no I/O."""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from typing import Protocol

from datamining_skill.domain.exceptions import InvalidConfigurationException


class ExtractionStrategy(Protocol):
    @property
    def fields(self) -> tuple[str, ...]: ...

    def extract(self, line: str) -> Iterator[Sequence[str]]:
        """Yield each record in ``line`` as ``len(fields)`` strings."""
        ...


# Bounded quantifiers plus a look-behind (a match starts only where a local-part run begins)
# keep scanning linear: a line of a million 'a' cannot cause catastrophic backtracking.
EMAIL_PATTERN = (
    r"(?<![A-Za-z0-9._%+\-])"
    r"[A-Za-z0-9._%+\-]{1,64}"
    r"@"
    r"[A-Za-z0-9\-]{1,63}(?:\.[A-Za-z0-9\-]{1,63}){0,8}"
    r"\.[A-Za-z]{2,24}"
    r"(?![A-Za-z0-9\-])"
)


def _quantifier_at(pattern: str, index: int) -> tuple[bool, bool, int]:
    """Return ``(ambiguous, unbounded, length)`` for the quantifier at ``index``.

    A repeat is ambiguous when the same text can be split across iterations in several ways
    (``*``, ``+``, ``{m,}``, ``{m,n}`` with n > m). ``length`` is 0 if there is no quantifier
    (a ``{`` that is not a valid repeat is a literal brace).
    """
    if index >= len(pattern):
        return False, False, 0
    char = pattern[index]
    if char in "*+":
        ambiguous, unbounded, length = True, True, 1
    elif char == "?":
        ambiguous, unbounded, length = False, False, 1
    elif char == "{":
        close = pattern.find("}", index)
        body = pattern[index + 1 : close] if close != -1 else ""
        if not re.fullmatch(r"\d*(,\d*)?", body) or body in ("", ","):
            return False, False, 0
        low, _, high = body.partition(",")
        unbounded = body.endswith(",")
        ambiguous = unbounded or (bool(high) and int(high or 0) > int(low or 0))
        length = close - index + 1
    else:
        return False, False, 0
    if index + length < len(pattern) and pattern[index + length] in "?+":
        length += 1  # lazy or possessive suffix
    return ambiguous, unbounded, length


def _skip_character_class(pattern: str, index: int) -> int:
    index += 1
    if pattern[index : index + 1] == "^":
        index += 1
    if pattern[index : index + 1] == "]":
        index += 1  # a leading ']' is a literal
    while index < len(pattern) and pattern[index] != "]":
        index += 2 if pattern[index] == "\\" else 1
    return index + 1


def vet_pattern(pattern: str) -> None:
    """Raise ``InvalidConfigurationException`` for a group that holds an ambiguous repeat and is
    itself repeated without an upper bound, such as ``(a+)+`` or ``(\\w+\\s?)*``.

    That shape takes exponential time on a near-miss. This is a conservative filter, not a
    guarantee: overlapping alternations like ``(a|aa)+`` pass and ``re`` has no time limit, so
    untrusted callers should stay away from custom patterns (docs/privacy-and-security.md).
    """
    groups: list[bool] = []  # per open group: does it contain an ambiguous repeat?
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "\\":
            index += 2
        elif char == "[":
            index = _skip_character_class(pattern, index)
        elif char == "(":
            groups.append(False)
            index += 1
            if pattern.startswith("?", index):  # (?: (?= (?P<name> (?i) ...
                marker_end = index + 1
                while marker_end < len(pattern) and pattern[marker_end] not in ":=!>)":
                    marker_end += 1
                index = marker_end if pattern[marker_end : marker_end + 1] == ")" else marker_end + 1
        elif char == ")":
            contained = groups.pop() if groups else False
            ambiguous, unbounded, length = _quantifier_at(pattern, index + 1)
            if contained and unbounded:
                raise InvalidConfigurationException(
                    "the pattern repeats a group that itself repeats without an upper bound "
                    "(for example '(a+)+'), which can take exponential time; use bounded "
                    "quantifiers such as {1,64}"
                )
            if groups and (contained or ambiguous):
                groups[-1] = True
            index += 1 + length
        else:
            ambiguous, _, length = _quantifier_at(pattern, index)
            if length:
                if ambiguous and groups:
                    groups[-1] = True
                index += length
            else:
                index += 1


class RegexExtractor:
    """Yields every regex match per line: the whole match, or the group tuple if the pattern has
    groups (unmatched optional groups become empty strings).

    The pattern is compiled, never evaluated as code, but a careless one can still backtrack
    catastrophically on hostile lines; prefer bounded quantifiers like ``EMAIL_PATTERN``.
    ``fields`` defaults to ``("match",)`` or ``group_1..n``. Raises
    ``InvalidConfigurationException`` for an invalid pattern or a wrong field count.
    """

    def __init__(
        self, pattern: str, fields: Sequence[str] | None = None, *, flags: int = 0
    ) -> None:
        try:
            self._regex = re.compile(pattern, flags)
        except re.error as exc:
            raise InvalidConfigurationException(f"invalid regular expression: {exc}") from exc
        columns = max(1, self._regex.groups)
        if fields is None:
            fields = ("match",) if self._regex.groups == 0 else tuple(
                f"group_{index}" for index in range(1, columns + 1)
            )
        if len(fields) != columns:
            raise InvalidConfigurationException(
                f"expected {columns} field name(s) for this pattern, got {len(fields)}"
            )
        self._fields = tuple(fields)

    @classmethod
    def emails(cls) -> RegexExtractor:
        """Extract e-mail addresses into a single ``email`` column."""
        return cls(EMAIL_PATTERN, ("email",))

    @property
    def fields(self) -> tuple[str, ...]:
        return self._fields

    def extract(self, line: str) -> Iterator[Sequence[str]]:
        for match in self._regex.finditer(line):
            if self._regex.groups == 0:
                yield (match.group(0),)
            else:
                yield tuple(group or "" for group in match.groups())
