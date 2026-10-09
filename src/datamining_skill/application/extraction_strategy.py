"""Pluggable mining logic: a pure function from one line of text to zero or more records, with no I/O."""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Sequence
from typing import Protocol

from datamining_skill.domain.exceptions import InvalidConfigurationException


class ExtractionStrategy(Protocol):
    @property
    def fields(self) -> tuple[str, ...]: ...

    def extract(self, line: str) -> Iterable[Sequence[str]]:
        """Return each record in ``line`` as ``len(fields)`` strings (a list or a generator).

        A strategy may declare ``extract(self, line, start=0, stop=None)`` instead (see
        ``SpanExtractionStrategy``): it is then searched exactly even in lines longer than the
        reader's cap.
        """
        ...


class SpanExtractionStrategy(Protocol):
    """A strategy that can search part of a line, which makes very long lines exact."""

    @property
    def fields(self) -> tuple[str, ...]: ...

    def extract(
        self, line: str, start: int = 0, stop: int | None = None
    ) -> Iterable[Sequence[str]]:
        """Return the records of the matches that begin in ``[start, stop)`` of ``line``.

        ``stop=None`` means to the end. This lets overlapping windows of one very long line be
        searched without repeats; text before ``start`` is still visible to a look-behind.
        """
        ...


# Bounded quantifiers plus a look-behind (a match starts only where a local-part run begins)
# keep scanning linear: a line of a million 'a' cannot cause catastrophic backtracking.
# The Unicode pattern accepts letters and digits of any script (müşteri@firma.com.tr,
# 名前@例え.jp, x@y.рф) and punycode top-level domains; the ASCII one is its exact equivalent
# on ASCII-only text (a test compares them) and about twice as fast, so it serves such lines.
EMAIL_PATTERN = (
    r"(?<![\w.%+\-])"
    r"[\w.%+\-]{1,64}"
    r"@"
    r"(?:[^\W_]|-){1,63}(?:\.(?:[^\W_]|-){1,63}){0,8}"
    r"\.(?:xn--[A-Za-z0-9\-]{2,59}|[^\W\d_]{2,24})"
    r"(?![^\W_]|-)"
)
EMAIL_PATTERN_ASCII = (
    r"(?<![A-Za-z0-9._%+\-])"
    r"[A-Za-z0-9._%+\-]{1,64}"
    r"@"
    r"[A-Za-z0-9\-]{1,63}(?:\.[A-Za-z0-9\-]{1,63}){0,8}"
    r"\.(?:xn--[A-Za-z0-9\-]{2,59}|[A-Za-z]{2,24})"
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


def _repeats(pattern: str, index: int) -> bool:
    """Whether the quantifier at ``index`` allows more than one iteration."""
    if index >= len(pattern):
        return False
    char = pattern[index]
    if char in "*+":
        return True
    if char != "{":
        return False
    close = pattern.find("}", index)
    body = pattern[index + 1 : close] if close != -1 else ""
    if not re.fullmatch(r"\d*(,\d*)?", body) or body in ("", ","):
        return False
    low, comma, high = body.partition(",")
    if not comma:
        return int(low) > 1
    return high == "" or int(high) > 1


def reject_repeated_capture(pattern: str) -> None:
    """Raise ``InvalidConfigurationException`` for a repeated group that captures.

    ``(\\d+\\.){3}`` keeps only the last repetition ("0." for "10.0.0.0"), so the output would
    silently hold fragments, and so would ``(?:(\\d+)\\.)+``, whose capture sits inside a
    repeated group. The fix is a non-capturing group: ``(?:\\d+\\.){3}``.
    """
    # per open group: does it capture, and does it contain a capturing group?
    open_groups: list[list[bool]] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "\\":
            index += 2
        elif char == "[":
            index = _skip_character_class(pattern, index)
        elif char == "(":
            captures = not pattern.startswith("?", index + 1) or pattern.startswith("?P<", index + 1)
            open_groups.append([captures, False])
            index += 1
        elif char == ")":
            captures, contains = open_groups.pop() if open_groups else (False, False)
            holds_capture = captures or contains
            if holds_capture and _repeats(pattern, index + 1):
                raise InvalidConfigurationException(
                    "the pattern repeats a capturing group, which keeps only its last repetition "
                    "(for example '(\\d+\\.){3}' yields '0.'); write it as a non-capturing group "
                    "such as '(?:\\d+\\.){3}'"
                )
            if open_groups and holds_capture:
                open_groups[-1][1] = True
            index += 1
        else:
            index += 1


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
    ``fields`` defaults to ``("match",)`` or ``group_1..n``. ``lowercase`` lower-cases every
    value, which with ``unique`` makes values compare case-insensitively. Raises
    ``InvalidConfigurationException`` for an invalid pattern, a repeated capturing group or a
    wrong field count.
    """

    def __init__(
        self,
        pattern: str,
        fields: Sequence[str] | None = None,
        *,
        flags: int = 0,
        lowercase: bool = False,
    ) -> None:
        try:
            self._regex = re.compile(pattern, flags)
        except re.error as exc:
            raise InvalidConfigurationException(f"invalid regular expression: {exc}") from exc
        reject_repeated_capture(pattern)
        self._lowercase = lowercase
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
    def emails(cls, *, lowercase: bool = False) -> EmailExtractor:
        """Extract e-mail addresses into a single ``email`` column."""
        return EmailExtractor(lowercase=lowercase)

    @property
    def fields(self) -> tuple[str, ...]:
        return self._fields

    def extract(
        self, line: str, start: int = 0, stop: int | None = None
    ) -> Iterable[Sequence[str]]:
        if stop is not None:
            return self._matches_before(line, start, stop)
        found = self._regex.findall(line, start)  # all matches at once, at C speed
        if not found:
            return ()
        records: list[tuple[str, ...]] = [(value,) for value in found] if self._regex.groups < 2 else found
        if self._lowercase:
            return [tuple(value.lower() for value in record) for record in records]
        return records

    def _matches_before(self, line: str, start: int, stop: int) -> Iterator[Sequence[str]]:
        for match in self._regex.finditer(line, start):
            if match.start() >= stop:
                return
            if self._regex.groups == 0:
                values: tuple[str, ...] = (match.group(0),)
            else:
                values = tuple(group or "" for group in match.groups())
            yield tuple(value.lower() for value in values) if self._lowercase else values


_LOCAL_REACH = 65  # a local part has at most 64 characters; the one before them must not match
_DOMAIN_REACH = 700  # longer than any domain the pattern can match
_DENSE = 200  # fewer characters than this per "@": one pass over the text is quicker


class EmailExtractor:
    """The built-in e-mail extraction, tuned for text in which addresses are rare.

    It finds the same addresses as ``EMAIL_PATTERN`` (a test compares them on random text), but
    it first locates each ``@`` with a plain string search and runs the pattern only in a small
    window around it, so a log file with an address every few kilobytes is read at close to the
    speed of the disk. Text with an ``@`` every 200 characters or less is searched in one pass
    instead, which is faster there. No match can contain a line break, so the worker may hand
    over many lines at once (``line_independent``), and none can contain a comma or a quote, so
    a CSV writer need not check each value for quoting (``clean_values``).
    """

    fields = ("email",)
    line_independent = True
    clean_values = True  # an address never holds a comma, a quote or a line break

    def __init__(self, *, lowercase: bool = False) -> None:
        self._lowercase = lowercase
        self._unicode = re.compile(EMAIL_PATTERN)
        self._ascii = re.compile(EMAIL_PATTERN_ASCII)

    def extract(
        self, line: str, start: int = 0, stop: int | None = None
    ) -> Iterable[Sequence[str]]:
        count = line.count("@", start)
        if not count:
            return ()
        regex = self._ascii if line.isascii() else self._unicode
        if stop is None and len(line) - start < count * _DENSE:
            found = regex.findall(line, start)
        else:
            found = self._around_each_at_sign(regex, line, start, stop)
        if not found:
            return ()
        if self._lowercase:
            return [(address.lower(),) for address in found]
        return [(address,) for address in found]

    @staticmethod
    def _around_each_at_sign(
        regex: re.Pattern[str], text: str, start: int, stop: int | None
    ) -> list[str]:
        found: list[str] = []
        end = len(text)
        taken_to = start  # nothing before this may start a match: an earlier match used it
        at = text.find("@", start)
        while at != -1:
            if stop is not None and at - _LOCAL_REACH >= stop:
                break  # a match for this sign would begin at or after the stop
            match = regex.search(
                text, max(taken_to, at - _LOCAL_REACH, start), min(end, at + _DOMAIN_REACH)
            )
            if match is not None and match.start() <= at:
                if stop is not None and match.start() >= stop:
                    break
                found.append(match.group())
                taken_to = match.end()
                at = text.find("@", taken_to)
            else:  # no address around this sign (a match further on is found from its own sign)
                at = text.find("@", at + 1)
        return found
