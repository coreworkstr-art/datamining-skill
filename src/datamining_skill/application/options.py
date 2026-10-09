"""What a mining job extracts and how it writes the result."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass

from datamining_skill.application.extraction_strategy import ExtractionStrategy, RegexExtractor
from datamining_skill.application.miner_worker import JSON_ESCAPES_MODES
from datamining_skill.domain.exceptions import InvalidConfigurationException


@dataclass(frozen=True, slots=True)
class MiningOptions:
    """The settings that decide a job's result.

    ``pattern`` replaces the built-in e-mail extraction and ``fields`` names its columns.
    ``lowercase`` lower-cases every value and ``unique`` keeps only the first occurrence of
    each record, over the whole job and across resumed runs; together they make e-mail
    addresses compare case-insensitively. ``csv_formula_guard`` neutralises spreadsheet
    formulas in CSV output. ``json_escapes`` is ``"auto"`` (decode JSON string escapes such as
    ``\\u0040`` for JSON and JSON Lines sources searched with the built-in e-mail extraction),
    ``"on"`` or ``"off"``.
    """

    pattern: str | None = None
    fields: tuple[str, ...] | None = None
    lowercase: bool = False
    unique: bool = False
    csv_formula_guard: bool = False
    json_escapes: str = "auto"

    def __post_init__(self) -> None:
        if self.json_escapes not in JSON_ESCAPES_MODES:
            raise InvalidConfigurationException(
                f"'json_escapes' must be one of {', '.join(JSON_ESCAPES_MODES)}"
            )
        if self.fields is not None and self.pattern is None:
            raise InvalidConfigurationException("'fields' requires 'pattern'")

    @classmethod
    def of(
        cls,
        *,
        pattern: str | None = None,
        fields: Sequence[str] | None = None,
        lowercase: bool = False,
        unique: bool = False,
        csv_formula_guard: bool = False,
        json_escapes: str = "auto",
    ) -> MiningOptions:
        return cls(
            pattern,
            None if fields is None else tuple(fields),
            lowercase,
            unique,
            csv_formula_guard,
            json_escapes,
        )

    def strategy(self) -> ExtractionStrategy:
        if self.pattern is None:
            return RegexExtractor.emails(lowercase=self.lowercase)
        return RegexExtractor(self.pattern, self.fields, lowercase=self.lowercase)

    def effective_json_escapes(self) -> str:
        """``"auto"`` applies to the built-in e-mail extraction only; a custom pattern sees raw text."""
        if self.json_escapes == "auto" and self.pattern is not None:
            return "off"
        return self.json_escapes

    def fingerprint(self) -> str:
        """A short identifier of these settings, so a changed setting starts a new job."""
        canonical = json.dumps(
            [
                self.pattern,
                self.fields,
                self.lowercase,
                self.unique,
                self.csv_formula_guard,
                self.json_escapes,
            ],
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("ascii")).hexdigest()[:16]
