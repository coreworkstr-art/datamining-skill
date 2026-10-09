"""Extraction patterns, their safety checks, and the settings that identify a mining job."""

from __future__ import annotations

import random
import re

import pytest

from datamining_skill import InvalidConfigurationException, RegexExtractor
from datamining_skill.application.extraction_strategy import (
    EMAIL_PATTERN,
    EMAIL_PATTERN_ASCII,
    EmailExtractor,
)
from datamining_skill.application.options import MiningOptions


@pytest.mark.parametrize(
    "pattern",
    [
        r"\b(\d{1,3}\.){3}\d{1,3}\b",
        r"(\w)+@x",
        r"([a-z]){2}",
        r"(?P<octet>\d+\.)+",
        r"(a){2,}",
        r"(?:(a)b)+",  # the capture sits inside a repeated group
        r"(?:x(?:y(\d))*)*",
    ],
)
def test_a_repeated_capturing_group_is_rejected_with_the_fix(pattern: str) -> None:
    with pytest.raises(InvalidConfigurationException, match=r"non-capturing group"):
        RegexExtractor(pattern)


@pytest.mark.parametrize(
    "pattern",
    [
        r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
        r"(\d+)@(\w+)",  # groups that are not repeated are fine
        r"(a)?b",
        r"(x){1}",
        r"(?i)(abc)",
        r"[(]\d+[)]+",  # parentheses inside a class are literals
        r"\(\w+\)+",  # escaped ones too
        r"(?=(a))b",
        r"(?P<name>\w+)",
        r"(?:\d+\.)+(\w+)",  # the repeated group does not capture
        r"(?:a(b)?)",  # an optional capture is not a repetition
    ],
)
def test_other_patterns_are_accepted(pattern: str) -> None:
    RegexExtractor(pattern)


def test_group_values_are_reported_per_column() -> None:
    extractor = RegexExtractor(r"(\d+)@(\w+)", ("id", "host"))

    assert list(extractor.extract("7@alpha 8@beta")) == [("7", "alpha"), ("8", "beta")]


def test_lowercase_applies_to_every_value() -> None:
    extractor = RegexExtractor(r"(\w+)@(\w+)", lowercase=True)

    assert list(extractor.extract("Ada@Example")) == [("ada", "example")]
    assert list(RegexExtractor.emails(lowercase=True).extract("Ops@Corp.TEST")) == [("ops@corp.test",)]
    assert list(RegexExtractor.emails().extract("Ops@Corp.TEST")) == [("Ops@Corp.TEST",)]


def test_extract_reports_only_matches_that_begin_inside_the_span() -> None:
    extractor = RegexExtractor.emails()
    line = "a@x.test and b@y.test and c@z.test"

    assert [r[0] for r in extractor.extract(line, 10, 21)] == ["b@y.test"]
    assert [r[0] for r in extractor.extract(line, 0, 1)] == ["a@x.test"]
    assert [r[0] for r in extractor.extract(line, 12)] == ["b@y.test", "c@z.test"]


def test_the_look_behind_sees_text_before_the_span_start() -> None:
    # starting inside the local part must not produce a shorter address
    assert list(RegexExtractor.emails().extract("xyz.abc@host.test", 4)) == []


def test_every_setting_changes_the_job_fingerprint() -> None:
    base = MiningOptions()
    variants = [
        MiningOptions.of(pattern=r"\d+"),
        MiningOptions.of(pattern=r"\d+", fields=("n",)),
        MiningOptions.of(lowercase=True),
        MiningOptions.of(unique=True),
        MiningOptions.of(csv_formula_guard=True),
        MiningOptions.of(json_escapes="off"),
    ]

    prints = {base.fingerprint(), *(variant.fingerprint() for variant in variants)}

    assert len(prints) == len(variants) + 1
    assert base.fingerprint() == MiningOptions().fingerprint()  # stable for equal settings


def test_options_are_validated() -> None:
    with pytest.raises(InvalidConfigurationException, match="json_escapes"):
        MiningOptions.of(json_escapes="sometimes")
    with pytest.raises(InvalidConfigurationException, match="requires 'pattern'"):
        MiningOptions.of(fields=["a"])


def test_json_escape_decoding_is_automatic_only_for_the_built_in_extraction() -> None:
    assert MiningOptions().effective_json_escapes() == "auto"
    assert MiningOptions.of(pattern=r"\d+").effective_json_escapes() == "off"  # raw text for custom patterns
    assert MiningOptions.of(pattern=r"\d+", json_escapes="on").effective_json_escapes() == "on"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("info@müller.de", ["info@müller.de"]),
        ("müşteri@firma.com.tr", ["müşteri@firma.com.tr"]),  # not the truncated "teri@..."
        ("a@bücher.example", ["a@bücher.example"]),
        ("名前@例え.jp", ["名前@例え.jp"]),
        ("x@y.рф", ["x@y.рф"]),
        ("u@host.xn--p1ai", ["u@host.xn--p1ai"]),
        ("mail Ada.Hollis@Internal.Corp.TEST now", ["Ada.Hollis@Internal.Corp.TEST"]),
        ("(a@b.co)", ["a@b.co"]),
        ("ok@host.com.", ["ok@host.com"]),  # a sentence's final dot is not part of the address
        ("a@b.co_x", ["a@b.co"]),  # an underscore ends the domain
        ("a@b_c.com", []),
        ("no at sign here", []),
        ("x@y", []),
    ],
)
def test_email_extraction_handles_international_addresses(text: str, expected: list[str]) -> None:
    assert [record[0] for record in RegexExtractor.emails().extract(text)] == expected


def test_the_ascii_fast_pattern_agrees_with_the_unicode_pattern_on_ascii_text() -> None:
    import random
    import re

    from datamining_skill.application.extraction_strategy import EMAIL_PATTERN, EMAIL_PATTERN_ASCII

    rng = random.Random(2025)
    alphabet = "abn9._%+-@ ,;<>()[]\t\"'xyz_"
    unicode_regex, ascii_regex = re.compile(EMAIL_PATTERN), re.compile(EMAIL_PATTERN_ASCII)

    for _ in range(30_000):
        text = "".join(rng.choices(alphabet, k=rng.randint(1, 40)))
        assert unicode_regex.findall(text) == ascii_regex.findall(text), text


def test_a_line_without_an_at_sign_is_skipped_before_any_pattern_runs() -> None:
    extractor = RegexExtractor.emails()

    assert list(extractor.extract("x" * 100_000)) == []
    assert RegexExtractor(r"\d+").extract("12") is not None  # a custom pattern has no such filter
    assert [m[0] for m in RegexExtractor(r"\d+").extract("12 and 34")] == ["12", "34"]


# ----------------------------------------------------------- the anchor-first e-mail search

UNICODE_REGEX = re.compile(EMAIL_PATTERN)
ASCII_REGEX = re.compile(EMAIL_PATTERN_ASCII)


def regex_for(text: str) -> re.Pattern[str]:
    return ASCII_REGEX if text.isascii() else UNICODE_REGEX


def reference_addresses(text: str, start: int = 0, stop: int | None = None) -> list[str]:
    """What one pass of the pattern over the whole text finds."""
    return [m.group() for m in regex_for(text).finditer(text, start) if stop is None or m.start() < stop]


def random_text(rng: random.Random, length: int) -> str:
    pieces = [
        "@", "@", "a", "b", "x9", ".", "-", "_", "%", "+", " ", ",", ";", "<", ">", "(", ")", "\n", "\t",
        "com", "org", "test", "xn--p1ai", "ü", "ş", "名", "рф", "😀", "co", "uk", "A", "Z", "0",
    ]  # fmt: skip
    return "".join(rng.choices(pieces, k=length))


def test_searching_around_each_at_sign_finds_exactly_what_one_pass_finds() -> None:
    rng = random.Random(7)
    around = EmailExtractor()._around_each_at_sign

    for _ in range(40_000):
        text = random_text(rng, rng.randint(1, 120))
        assert around(regex_for(text), text, 0, None) == reference_addresses(text), repr(text)


def test_long_runs_and_long_domains_behave_like_the_pattern() -> None:
    extractor = EmailExtractor()
    cases = [
        "a" * 64 + "@host.test",  # the longest local part
        "a" * 65 + "@host.test",  # one too long: no match at all
        "x" * 200 + "@host.test",
        "ok " + "a" * 65 + "@host.test ok2@host.test",
        "u@" + ".".join(["a" * 63] * 9) + ".test",  # nine long labels
        "u@" + ".".join(["a" * 63] * 10) + ".test",  # a tenth is too many
        "u@" + "a" * 64 + ".test",  # a label of 64
        "a@b.test.c@d.test",  # an address inside the domain of the previous one
        "first@a.test second@b.test third@c.test",
        "@@@@",
        "a@b.co" * 100,
    ]
    for text in cases:
        assert [r[0] for r in extractor.extract(text)] == reference_addresses(text), text[:50]
        assert [r[0] for r in extractor.extract(text, 0, 30)] == reference_addresses(text, 0, 30), text[:50]


def test_dense_and_sparse_text_take_different_paths_with_the_same_result() -> None:
    extractor = EmailExtractor()
    dense = "ada@internal.corp.test\n" * 50
    sparse = ("some log text " * 100 + "ada@internal.corp.test\n") * 50

    for text in (dense, sparse):
        assert [r[0] for r in extractor.extract(text)] == reference_addresses(text)


def test_windows_report_matches_that_begin_in_their_span_only() -> None:
    rng = random.Random(11)
    extractor = EmailExtractor()
    for _ in range(5_000):
        text = random_text(rng, rng.randint(1, 150))
        start = rng.randint(0, len(text))
        stop = rng.choice([None, rng.randint(start, len(text) + 5)])
        found = [r[0] for r in extractor.extract(text, start, stop)]
        assert found == reference_addresses(text, start, stop), (repr(text), start, stop)


def test_the_extractor_lowercases_and_exposes_its_fields() -> None:
    extractor = EmailExtractor(lowercase=True)

    assert extractor.fields == ("email",) and extractor.line_independent is True
    assert list(extractor.extract("Ada@Internal.CORP.test")) == [("ada@internal.corp.test",)]
