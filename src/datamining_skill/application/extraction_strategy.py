"""Pluggable mining logic: a pure function from one line of text to zero or more records, with no I/O."""

from __future__ import annotations

import functools
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

    ``(\\d{1,3}\\.){3}`` keeps only the last repetition ("0." for "10.0.0.0"), so the output
    would silently hold fragments, and so would ``(?:(\\d{1,3})\\.)+``, whose capture sits inside
    a repeated group. The fix is a non-capturing group: ``(?:\\d{1,3}\\.){3}``.
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
                    "(for example '(\\d{1,3}\\.){3}' yields '0.'); write it as a non-capturing group "
                    "such as '(?:\\d{1,3}\\.){3}'"
                )
            if open_groups and holds_capture:
                open_groups[-1][1] = True
            index += 1
        else:
            index += 1


# Characters tried when asking whether two pieces of a pattern can start with the same character:
# every code point up to U+2FFF (Latin, Greek, Cyrillic, Hebrew, Arabic, Indic scripts, general
# punctuation, symbols) plus a few from the CJK, Hangul and emoji ranges.
_PROBE = (
    *(chr(code) for code in range(0x3000)),
    *(chr(code) for code in range(0x3000, 0xA000, 16)),  # CJK, sampled
    *(chr(code) for code in range(0xAC00, 0xD7A4, 16)),  # Hangul, sampled
    *(chr(code) for code in range(0x1F300, 0x1F650, 4)),  # emoji, sampled
)
_ZERO_WIDTH_ESCAPES = frozenset("bBAZ")
_ESCAPE_HEX_DIGITS = {"x": 2, "u": 4, "U": 8}


@functools.lru_cache(maxsize=1024)
def _characters_matching(token: str) -> frozenset[str]:
    """The characters that one pattern token (a literal, escape, class or ``.``) can match.

    Found by trying every probe character, plus the character itself (in both cases) when the
    token is a single literal, so that a literal outside the probe ranges still overlaps with
    itself. Compared without regard to case, so ``a`` and ``A`` count as overlapping. A token
    that does not compile stands for every character: when in doubt, they overlap.
    """
    try:
        regex = re.compile(token, re.IGNORECASE)
    except re.error:
        return frozenset(_PROBE)
    matching = {char for char in _PROBE if regex.fullmatch(char)}
    literal = token[1:] if len(token) == 2 and token[0] == "\\" and not token[1].isalnum() else token
    if len(literal) == 1 and literal != ".":
        matching |= {literal, literal.lower(), literal.upper()}
    return frozenset(matching)


def _minimum_repeat(pattern: str, index: int, length: int) -> int:
    """The fewest times the quantifier of ``length`` characters at ``index`` lets its token occur."""
    if length == 0:
        return 1
    char = pattern[index]
    if char in "*?":
        return 0
    if char == "{":
        return int(pattern[index + 1 : pattern.find("}", index)].partition(",")[0] or 0)
    return 1


def _leading_tokens(branch: str) -> list[str] | None:
    """The tokens one alternative of a group can begin with, or ``None`` if it cannot be told.

    The first token counts, and so does each one after it for as long as the tokens before them
    may match nothing (``a?b`` can begin with ``a`` or ``b``). Zero-width anchors are skipped.
    A group, a look-around or a back-reference at the start is not analysed.
    """
    tokens: list[str] = []
    index = 0
    while index < len(branch):
        char = branch[index]
        if char in "^$":
            index += 1
            continue
        if char == "\\":
            escaped = branch[index + 1 : index + 2]
            if not escaped or (escaped.isdigit() and escaped != "0"):
                return None  # a dangling backslash or a back-reference
            if escaped in _ZERO_WIDTH_ESCAPES:
                index += 2
                continue
            end = index + 2 + _ESCAPE_HEX_DIGITS.get(escaped, 0)
            if escaped == "N" and branch[end : end + 1] == "{":
                end = branch.find("}", end) + 1 or len(branch)
            token = branch[index:end]
        elif char == "[":
            end = _skip_character_class(branch, index)
            token = branch[index:end]
        elif char in "(*+?":
            return None
        else:
            end = index + 1
            token = "." if char == "." else re.escape(char)
        _, _, length = _quantifier_at(branch, end)
        tokens.append(token)
        index = end + length
        if _minimum_repeat(branch, end, length) >= 1:
            break
    return tokens


def _matches_empty(branch: str) -> bool:
    try:
        return re.fullmatch(branch, "") is not None
    except re.error:  # a piece that only makes sense inside the whole pattern
        return False


def _branches_overlap(branches: list[str]) -> bool:
    """Whether the alternatives of a group cannot be told apart by their first character.

    That is so when one of them can match nothing, when two of them can begin with the same
    character (``a`` and ``aa``, ``foo`` and ``foobar``, ``\\w`` and ``a``, ``\\d`` and ``1``), or
    when one begins with something that is not analysed (a nested group, a look-around, a
    back-reference). Repeated, such a group lets the same text be split in many ways, which takes
    exponential time on a near-miss: ``(a|aa)+``.
    """
    seen: set[str] = set()
    for branch in branches:
        if _matches_empty(branch):
            return True
        tokens = _leading_tokens(branch)
        if not tokens:
            return True
        first: set[str] = set()
        for token in tokens:
            first |= _characters_matching(token)
        if first & seen:
            return True
        seen |= first
    return False


_MAX_CHAIN = 3  # this many overlapping unbounded repeats in a row are refused
_CLASS_ESCAPES = frozenset("wWdDsS")
_EVERYTHING = frozenset(_PROBE)


class _Group:
    """What vet_pattern knows about an open group (or the top level) while it scans."""

    __slots__ = ("ambiguous", "body_start", "has_literal", "lookaround", "run", "splits", "unbounded")

    def __init__(self, body_start: int, *, lookaround: bool = False) -> None:
        self.body_start = body_start
        self.lookaround = lookaround
        self.splits: list[int] = []  # positions of the "|" that belong to this group
        self.ambiguous = False  # holds a repeat that can split text in several ways
        self.unbounded = False  # holds a repeat with no upper bound
        self.has_literal = False  # holds a fixed character, which separates repeats around it
        # characters matched by the unbounded repeats that follow each other here and can
        # match the same text: three of them make the match time grow with the cube of the input
        self.run: list[frozenset[str]] = []


def _atom_at(pattern: str, index: int) -> tuple[str | None, bool, int]:
    """The atom that starts at ``index`` as ``(token, is_literal, end)``.

    ``token`` is ``None`` for something that matches no character (an anchor, ``\\b``, a stray
    quantifier); ``is_literal`` is true for one fixed character, so not for ``.``, a class,
    ``\\w``-style escapes or a back-reference.
    """
    char = pattern[index]
    if char == "\\":
        escaped = pattern[index + 1 : index + 2]
        if not escaped:
            return None, False, index + 1
        if escaped in _ZERO_WIDTH_ESCAPES:
            return None, False, index + 2
        end = index + 2 + _ESCAPE_HEX_DIGITS.get(escaped, 0)
        if escaped == "N" and pattern[end : end + 1] == "{":
            end = pattern.find("}", end) + 1 or len(pattern)
        is_literal = not (escaped in _CLASS_ESCAPES or (escaped.isdigit() and escaped != "0"))
        return pattern[index:end], is_literal, end
    if char == "[":
        end = _skip_character_class(pattern, index)
        return pattern[index:end], False, end
    if char == ".":
        return ".", False, index + 1
    if char in "^$":
        return None, False, index + 1
    if char in "*+?" or (char == "{" and _quantifier_at(pattern, index)[2]):
        return None, False, index + 1  # a quantifier with nothing to repeat: the pattern is invalid anyway
    return re.escape(char), True, index + 1


def _extend_run(
    run: list[frozenset[str]], chars: frozenset[str], minimum: int, *, unbounded: bool, literal: bool
) -> bool:
    """Note one more repeat in the current run; ``True`` once the run is as long as it may be.

    A fixed character that must occur ends the run: the repeats before it and after it cannot
    swap text freely (``.*foo.*bar.*`` is fine). A bounded token or one that may be absent does
    not end it. An unbounded repeat joins the run when it can match some of the same
    characters as one already in it, and starts a new run when it is mandatory and shares none
    (``\\d+\\s+\\d+``: digits and spaces cannot trade places). One that may be absent and shares
    none is passed over, because the others then become neighbours (``\\d+\\s*\\d+``).
    """
    if literal and minimum >= 1 and not unbounded:
        run.clear()
        return False
    if not unbounded:
        return False
    if not run or any(chars & member for member in run):
        run.append(chars)
    elif minimum >= 1:
        run[:] = [chars]
    return len(run) >= _MAX_CHAIN


def _first_characters(branches: list[str]) -> frozenset[str]:
    """Every character a group made of ``branches`` can begin with (all of them if unknown)."""
    first: set[str] = set()
    for branch in branches:
        tokens = _leading_tokens(branch)
        if not tokens:
            return _EVERYTHING
        for token in tokens:
            first |= _characters_matching(token)
    return frozenset(first)


def _starts_with_literal(branch: str) -> bool:
    return bool(branch) and _atom_at(branch, 0)[1]


def vet_pattern(pattern: str) -> None:
    """Raise ``InvalidConfigurationException`` for a pattern shape that can take exponential or
    high-degree polynomial time on a near-miss.

    Rejected: a group that holds a repeat without an upper bound and is itself repeated, however
    the outer repeat is bounded (``(a+)+``, ``(\\w+\\s?)*``, ``(a+){2}``, ``(a+){2,64}``); a group
    that holds any ambiguous repeat and is repeated without an upper bound (``(a{1,64})+``); a
    repeated group whose alternatives cannot be told apart by their first character
    (``(a|aa)+``, ``(\\w|a)*``, ``(x|)+``); and three or more unbounded repeats in a row that can
    match the same text with no fixed character between them (``.*.*.*x``, ``\\w+.*\\w+@``,
    ``.+[a-z]+.*!``). A bounded repeat of a bounded one, such as the ``(\\d{1,3}\\.){3}`` of an
    IPv4 address, passes, and so does ``^.*foo.*bar.*$``, whose repeats are kept apart by text.

    This is a conservative filter, not a guarantee: ``re`` has no time limit, and a pattern can
    be slow in ways that are not looked for, so untrusted callers should stay away from custom
    patterns (docs/privacy-and-security.md).
    """
    root = _Group(0)
    groups: list[_Group] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        scope = groups[-1] if groups else root
        if char == "(":
            index += 1
            lookaround = pattern.startswith(("?=", "?!", "?<=", "?<!"), index)
            if pattern.startswith("?", index):  # (?: (?= (?P<name> (?i) ...
                marker_end = index + 1
                while marker_end < len(pattern) and pattern[marker_end] not in ":=!>)":
                    marker_end += 1
                index = marker_end if pattern[marker_end : marker_end + 1] == ")" else marker_end + 1
            groups.append(_Group(index, lookaround=lookaround))
        elif char == "|":
            scope.splits.append(index)
            scope.run.clear()  # the next alternative starts afresh
            index += 1
        elif char == ")":
            group = groups.pop() if groups else _Group(index)
            parent = groups[-1] if groups else root
            ambiguous, unbounded, length = _quantifier_at(pattern, index + 1)
            repeated = _repeats(pattern, index + 1)
            if (group.ambiguous and unbounded) or (group.unbounded and repeated):
                raise InvalidConfigurationException(
                    "the pattern repeats a group that itself repeats without an upper bound "
                    "(for example '(a+)+' or '(a+){2}'), which can take exponential or high-degree "
                    "polynomial time; use bounded quantifiers such as {1,64}"
                )
            cuts = [group.body_start, *(split + 1 for split in group.splits)]
            ends = [*group.splits, index]
            branches = [pattern[start:end] for start, end in zip(cuts, ends, strict=True)]
            if repeated and group.splits and _branches_overlap(branches):
                raise InvalidConfigurationException(
                    "the pattern repeats a group whose alternatives overlap (for example "
                    "'(a|aa)+'), which can take exponential time; make the alternatives begin "
                    "with different characters, or drop the repeat"
                )
            minimum = _minimum_repeat(pattern, index + 1, length)
            if not group.lookaround:
                if unbounded:
                    if _extend_run(
                        parent.run, _first_characters(branches), minimum, unbounded=True, literal=False
                    ):
                        raise _chain_error()
                elif minimum >= 1 and not group.splits:
                    # a plain group is part of the sequence around it: its fixed text separates
                    # what comes before from what comes after, and the repeats it ends with
                    # continue the run, so that (.*)(.*)(.*) counts like .*.*.*
                    if group.has_literal:
                        parent.run.clear()
                        parent.has_literal = True
                    for member in group.run:
                        if _extend_run(parent.run, member, 0, unbounded=True, literal=False):
                            raise _chain_error()
                elif minimum >= 1 and all(_starts_with_literal(branch) for branch in branches):
                    parent.run.clear()  # every alternative opens with fixed text
                    parent.has_literal = True
            parent.ambiguous = parent.ambiguous or group.ambiguous or ambiguous
            parent.unbounded = parent.unbounded or group.unbounded or unbounded
            index += 1 + length
        else:
            token, literal, end = _atom_at(pattern, index)
            ambiguous, unbounded, length = _quantifier_at(pattern, end)
            if token is not None:
                if length:
                    scope.ambiguous = scope.ambiguous or ambiguous
                    scope.unbounded = scope.unbounded or unbounded
                minimum = _minimum_repeat(pattern, end, length)
                if literal and minimum >= 1 and not unbounded:
                    scope.has_literal = True
                chars = _characters_matching(token) if unbounded else frozenset()
                if _extend_run(scope.run, chars, minimum, unbounded=unbounded, literal=literal):
                    raise _chain_error()
            index = end + length


def _chain_error() -> InvalidConfigurationException:
    return InvalidConfigurationException(
        f"the pattern chains {_MAX_CHAIN} or more repeats without an upper bound that can match "
        "the same text and are not kept apart by fixed characters (for example '.*.*.*x' or "
        "'\\w+.*\\w+@'), which takes polynomial time that grows with every added repeat; "
        "separate them with literal text or use bounded quantifiers such as {1,64}"
    )


def _lowered(value: str) -> str:
    """``value`` in lower case, where the Turkish dotted capital I (U+0130) gives a plain i.

    ``str.lower`` turns that letter into an i followed by a combining dot, so one address written
    in capitals and in lower case would come out as two different addresses.
    """
    return value.lower() if value.isascii() else value.replace("\u0130", "i").lower()

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
            return [tuple(_lowered(value) for value in record) for record in records]
        return records

    def _matches_before(self, line: str, start: int, stop: int) -> Iterator[Sequence[str]]:
        for match in self._regex.finditer(line, start):
            if match.start() >= stop:
                return
            if self._regex.groups == 0:
                values: tuple[str, ...] = (match.group(0),)
            else:
                values = tuple(group or "" for group in match.groups())
            yield tuple(_lowered(value) for value in values) if self._lowercase else values


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
        ascii_line = line.isascii()
        regex = self._ascii if ascii_line else self._unicode
        if stop is None and len(line) - start < count * _DENSE:
            found = regex.findall(line, start)
        else:
            found = self._around_each_at_sign(regex, line, start, stop)
        if not found:
            return ()
        if self._lowercase:
            if ascii_line:
                return [(address.lower(),) for address in found]
            return [(_lowered(address),) for address in found]
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
