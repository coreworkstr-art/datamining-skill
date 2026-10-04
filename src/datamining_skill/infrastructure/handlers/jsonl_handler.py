"""Recognises JSON Lines and collects keys and value types."""

from __future__ import annotations

import json
from collections.abc import Iterator
from itertools import islice
from typing import Any

from datamining_skill.domain.models import DataFormat, StructureAnalysis, StructureInfo, TextLine

_MIN_VALID_RATIO = 0.5


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


class JsonlHandler:
    """Recognises newline-delimited JSON objects with ``json.loads``, which builds plain data only.

    Lines that are not JSON objects, exceed the line-size cap or nest past the recursion limit
    count as invalid. Memory is one line plus a key table capped at ``max_fields``.
    """

    data_format = DataFormat.JSONL
    extensions = frozenset({".jsonl", ".ndjson", ".json"})

    def __init__(self, max_fields: int = 1_024) -> None:
        self._max_fields = max_fields

    def analyze(self, lines: Iterator[TextLine], max_records: int) -> StructureAnalysis | None:
        non_empty = (line for line in lines if line.text.strip())
        key_types: dict[str, set[str]] = {}
        total = 0
        valid = 0

        for line in islice(non_empty, max_records):
            total += 1
            if line.truncated:
                continue
            try:
                document = json.loads(line.text)
            except (ValueError, RecursionError):
                continue
            if not isinstance(document, dict):
                continue
            valid += 1
            for key, value in document.items():
                if key in key_types:
                    key_types[key].add(_json_type(value))
                elif len(key_types) < self._max_fields:
                    key_types[key] = {_json_type(value)}

        if total == 0:
            return None
        ratio = valid / total
        if ratio < _MIN_VALID_RATIO:
            return None
        return StructureAnalysis(
            confidence=ratio,
            structure=StructureInfo(
                fields=tuple(key_types),
                sampled_records=valid,
                field_types={key: tuple(sorted(types)) for key, types in key_types.items()},
            ),
        )
