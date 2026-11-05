"""Best-effort masking of personal data and credentials in text that is about to be shown to a model.

Recognised: e-mail addresses, IPv4 and IPv6 addresses, payment card numbers (13 to 19 digits
that pass the Luhn check), Turkish national identity numbers (11 digits that pass the official
check digits), IBANs (that pass the ISO 7064 mod 97 check) and secrets: tokens of five
well-known formats (JSON Web Tokens, AWS access key ids, GitHub, OpenAI/Anthropic and Slack
tokens) and the value of an ``Authorization: Bearer`` header when it looks random (Shannon
entropy of at least 3.8 bits per character, mixing at least two kinds of character). Each hit is
replaced by a tag such as ``[EMAIL]``. Checksums, formats and entropy keep ordinary text and
numbers from being hidden by mistake.

This is a safety net, not a guarantee. Names, addresses, free text, phone numbers and secrets
of any other shape are not recognised, an IPv6 address needs a decimal digit and must not be glued
to a preceding colon, and a number with a wrong digit is left alone. Do not describe masked output
as anonymised (docs/privacy-and-security.md, "Masking").

Every pattern is bounded and starts only at a token boundary, so matching time grows linearly
with the text even for hostile lines (tests/test_pii_masking.py).
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Callable
from typing import Any

from datamining_skill.application.extraction_strategy import EMAIL_PATTERN

TAG_EMAIL = "EMAIL"
TAG_IP = "IP"
TAG_CARD = "CARD"
TAG_TCKN = "TCKN"
TAG_IBAN = "IBAN"
TAG_SECRET = "SECRET"  # noqa: S105 - the name of a tag, not a password

_EMAIL = re.compile(EMAIL_PATTERN)
_OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
_IPV4 = re.compile(rf"(?<![\w.]){_OCTET}(?:\.{_OCTET}){{3}}(?!\w|\.\d)")
# up to seven "hextet:" groups (a group may be empty, which is how "::" appears) and a last
# hextet or an embedded IPv4 address: at most 50 characters, so no run can make it slow
_IPV6 = re.compile(
    r"(?<![\w:])(?:[0-9A-Fa-f]{0,4}:){2,7}(?:(?:[0-9]{1,3}\.){3}[0-9]{1,3}|[0-9A-Fa-f]{1,4})?(?![\w:]|\.\d)"
)
_CARD = re.compile(r"(?<![\w-])\d(?:[ -]?\d){12,18}(?![\w-])")
_TCKN = re.compile(r"(?<!\w)[1-9]\d{10}(?!\w)")
_IBAN = re.compile(r"(?<!\w)[A-Z]{2}\d{2}(?: ?[A-Z0-9]){11,30}(?!\w)")

# Secrets with a format of their own. Each starts at a token boundary (a look-behind) and has an
# upper bound on every run, so that "eyJeyJeyJ..." or a megabyte of "a" cannot make it quadratic.
_JWT = re.compile(
    r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{10,4096}\.eyJ[A-Za-z0-9_-]{10,4096}\.[A-Za-z0-9_-]{10,4096}"
)
_AWS_KEY = re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])")
_GITHUB_TOKEN = re.compile(
    r"(?<![\w-])(?:gh[pousr]_[A-Za-z0-9]{30,64}|github_pat_[A-Za-z0-9_]{30,96})(?![\w-])"
)
_AI_KEY = re.compile(r"(?<![\w-])sk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,128}(?![\w-])")
_SLACK_TOKEN = re.compile(r"(?<![\w-])xox[baprs]-[A-Za-z0-9-]{20,128}(?![\w-])")
# the token is group 1, so "Bearer" stays readable in the output
_BEARER = re.compile(r"(?i:\bBearer\s{1,16})([A-Za-z0-9._~+/=-]{20,512})")

# a cheap test that runs first: text with none of these cannot contain anything above
_CANDIDATE = re.compile(r"@|\d|eyJ|AKIA|ASIA|gh[pousr]_|github_pat_|sk-|xox[baprs]-|(?i:bearer)")
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
_MAX_DEPTH = 40
_BEARER_MIN_ENTROPY = 3.8

Counts = Counter[str]


def luhn_valid(digits: str) -> bool:
    """The Luhn check used by payment card numbers."""
    total = 0
    for position, char in enumerate(reversed(digits)):
        value = int(char)
        if position % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def tckn_valid(digits: str) -> bool:
    """The check digits of a Turkish national identity number (T.C. kimlik no)."""
    if len(digits) != 11 or digits[0] == "0":
        return False
    d = [int(char) for char in digits]
    tenth = ((d[0] + d[2] + d[4] + d[6] + d[8]) * 7 - (d[1] + d[3] + d[5] + d[7])) % 10
    return d[9] == tenth and d[10] == sum(d[:10]) % 10


def iban_valid(compact: str) -> bool:
    """ISO 13616: 15 to 34 characters whose rearranged value is 1 modulo 97."""
    if not 15 <= len(compact) <= 34:
        return False
    rearranged = compact[4:] + compact[:4]
    number = "".join(str(int(char, 36)) for char in rearranged)
    return int(number) % 97 == 1


def shannon_entropy(text: str) -> float:
    """The Shannon entropy of ``text`` in bits per character: -sum(p * log2(p)) over its characters.

    0 for an empty or one-letter text, log2(n) when n different characters occur equally often.
    """
    if not text:
        return 0.0
    length = len(text)
    entropy = -sum((count / length) * math.log2(count / length) for count in Counter(text).values())
    return entropy if entropy > 0 else 0.0


def _ipv6_ok(text: str) -> bool:
    """Whether ``text`` is an IPv6 address in the usual notation.

    Eight groups of one to four hex digits, or fewer with a single ``::`` standing for the
    missing zeros, with an IPv4 address allowed in place of the last two groups. Without ``::``
    all eight groups are required, so a time (``12:34:56``) or a date (``2026:10:31``) is never an
    address. At least one decimal digit is required too, which keeps ``a::b`` and
    ``dead::beef``, the way names read in C++ or Rust code, out. Written without ``ipaddress``,
    which pulls in more than a masking step needs.
    """
    if not any("0" <= char <= "9" for char in text):
        return False
    head, double, tail = text.partition("::")
    if "::" in tail:
        return False
    groups = [group for part in (head, tail) if part for group in part.split(":")]
    width = len(groups)
    if groups and "." in groups[-1]:
        octets = groups.pop().split(".")
        if len(octets) != 4 or not all(octet.isdigit() and int(octet) <= 255 for octet in octets):
            return False
        width += 1  # the popped group stood for two hextets
    if any(not 1 <= len(group) <= 4 or not _HEX_DIGITS.issuperset(group) for group in groups):
        return False
    return width <= 7 if double else width == 8


def _bearer_secret_ok(token: str) -> bool:
    """Whether the value after ``Bearer`` is random enough to be a credential.

    A real token has high entropy and mixes kinds of character; a word, a counter or a
    repeated test string (``Bearer password``, ``Bearer aaaa...``) does not, and stays readable.
    """
    kinds = sum(
        (
            any(char.islower() for char in token),
            any(char.isupper() for char in token),
            any(char.isdigit() for char in token),
            any(not char.isalnum() for char in token),
        )
    )
    return kinds >= 2 and shannon_entropy(token) >= _BEARER_MIN_ENTROPY


def _digits(text: str) -> str:
    return "".join(char for char in text if char.isdigit())


def _card_ok(text: str) -> bool:
    digits = _digits(text)
    return 13 <= len(digits) <= 19 and luhn_valid(digits)


def _iban_ok(text: str) -> bool:
    return iban_valid(text.replace(" ", ""))


def _always(_: str) -> bool:
    return True


# (tag, pattern, validator, group): the text of ``group`` is validated, and replaced when it passes
_DETECTORS: tuple[tuple[str, re.Pattern[str], Callable[[str], bool], int], ...] = (
    (TAG_EMAIL, _EMAIL, _always, 0),
    (TAG_IBAN, _IBAN, _iban_ok, 0),
    (TAG_CARD, _CARD, _card_ok, 0),
    (TAG_TCKN, _TCKN, tckn_valid, 0),
    (TAG_IP, _IPV4, _always, 0),
    (TAG_IP, _IPV6, _ipv6_ok, 0),
    (TAG_SECRET, _JWT, _always, 0),
    (TAG_SECRET, _AWS_KEY, _always, 0),
    (TAG_SECRET, _GITHUB_TOKEN, _always, 0),
    (TAG_SECRET, _AI_KEY, _always, 0),
    (TAG_SECRET, _SLACK_TOKEN, _always, 0),
    (TAG_SECRET, _BEARER, _bearer_secret_ok, 1),
)


def mask_text(text: str, counts: Counts | None = None) -> str:
    """``text`` with every recognised item replaced by its tag; ``counts`` tallies the hits."""
    if _CANDIDATE.search(text) is None:
        return text
    hits: list[tuple[int, int, str]] = []
    for tag, pattern, accept, group in _DETECTORS:
        for match in pattern.finditer(text):
            if accept(match.group(group)):
                hits.append((match.start(group), match.end(group), tag))
    if not hits:
        return text
    hits.sort(key=lambda hit: (hit[0], -(hit[1] - hit[0])))
    pieces: list[str] = []
    position = 0
    for start, end, tag in hits:
        if start < position:  # overlaps an earlier, longer or equal hit
            continue
        pieces.append(text[position:start])
        pieces.append(f"[{tag}]")
        position = end
        if counts is not None:
            counts[tag] += 1
    pieces.append(text[position:])
    return "".join(pieces)


def mask_value(value: Any, counts: Counts | None = None, _depth: int = 0) -> Any:
    """``value`` with the text in every string, list item and object key masked."""
    if isinstance(value, str):
        return mask_text(value, counts)
    if _depth >= _MAX_DEPTH:
        return value
    if isinstance(value, list):
        return [mask_value(item, counts, _depth + 1) for item in value]
    if isinstance(value, dict):
        return {
            (mask_text(key, counts) if isinstance(key, str) else key): mask_value(item, counts, _depth + 1)
            for key, item in value.items()
        }
    return value


def mask_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """A profile whose data-derived names are masked.

    A CSV without a header row makes its first data row the "column names", and a JSON key can
    be a value, so the field names and the type table are masked. Counts and labels stay.
    """
    counts: Counts = Counter()
    masked = dict(profile)
    structure = profile.get("structure")
    if isinstance(structure, dict):
        masked_structure = dict(structure)
        for key in ("fields", "field_types", "pattern"):
            if key in structure:
                masked_structure[key] = mask_value(structure[key], counts)
        masked["structure"] = masked_structure
    masked["pii_masked"] = dict(sorted(counts.items()))
    return masked
