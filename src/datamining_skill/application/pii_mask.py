"""Best-effort masking of personal data in text that is about to be shown to a language model.

Recognised: e-mail addresses, IPv4 addresses, payment card numbers (13 to 19 digits that pass
the Luhn check), Turkish national identity numbers (11 digits that pass the official check
digits) and IBANs (that pass the ISO 7064 mod 97 check). Each hit is replaced by a tag such as
``[EMAIL]``. Checksums keep ordinary numbers from being hidden by mistake.

This is a safety net, not a guarantee. Names, addresses, free text, phone numbers and IPv6
addresses are not recognised, and a number with a wrong digit is left alone. Do not describe
masked output as anonymised (docs/privacy-and-security.md, "Masking").
"""

from __future__ import annotations

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

_EMAIL = re.compile(EMAIL_PATTERN)
_OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
_IPV4 = re.compile(rf"(?<![\w.]){_OCTET}(?:\.{_OCTET}){{3}}(?!\w|\.\d)")
_CARD = re.compile(r"(?<![\w-])\d(?:[ -]?\d){12,18}(?![\w-])")
_TCKN = re.compile(r"(?<!\w)[1-9]\d{10}(?!\w)")
_IBAN = re.compile(r"(?<!\w)[A-Z]{2}\d{2}(?: ?[A-Z0-9]){11,30}(?!\w)")
_DIGIT = re.compile(r"\d")
_MAX_DEPTH = 40

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


def _digits(text: str) -> str:
    return "".join(char for char in text if char.isdigit())


def _card_ok(text: str) -> bool:
    digits = _digits(text)
    return 13 <= len(digits) <= 19 and luhn_valid(digits)


def _iban_ok(text: str) -> bool:
    return iban_valid(text.replace(" ", ""))


_DETECTORS: tuple[tuple[str, re.Pattern[str], Callable[[str], bool]], ...] = (
    (TAG_EMAIL, _EMAIL, lambda _: True),
    (TAG_IBAN, _IBAN, _iban_ok),
    (TAG_CARD, _CARD, _card_ok),
    (TAG_TCKN, _TCKN, tckn_valid),
    (TAG_IP, _IPV4, lambda _: True),
)


def mask_text(text: str, counts: Counts | None = None) -> str:
    """``text`` with every recognised item replaced by its tag; ``counts`` tallies the hits."""
    if "@" not in text and _DIGIT.search(text) is None:
        return text
    hits: list[tuple[int, int, str]] = []
    for tag, pattern, accept in _DETECTORS:
        for match in pattern.finditer(text):
            if accept(match.group()):
                hits.append((match.start(), match.end(), tag))
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
