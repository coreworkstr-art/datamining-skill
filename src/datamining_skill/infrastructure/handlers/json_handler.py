"""Recognises a JSON document (an array or object) by its opening bracket and its keys."""

from __future__ import annotations

import re
from collections.abc import Iterator
from itertools import islice

from datamining_skill.domain.models import DataFormat, StructureAnalysis, StructureInfo, TextLine

_KEY = re.compile(r'"([^"\\]{1,64})"\s*:')
# only a line's prefix is scanned: bounds regex work on a minified document held on one line
_SCAN_PREFIX_CHARS = 65_536
# below JSON Lines, so a file of one object per line is still reported as JSONL
_CONFIDENCE = 0.7


class JsonDocumentHandler:
    """Recognises a document that starts with ``[`` or ``{`` and contains ``"key":`` pairs.

    The document is not parsed: mining searches its text line by line, so a minified
    document on one line and a pretty-printed one are treated alike. The field list is the
    set of keys seen in the head sample, in order of first appearance.
    """

    data_format = DataFormat.JSON
    extensions = frozenset({".json"})

    def __init__(self, max_fields: int = 1_024) -> None:
        self._max_fields = max_fields

    def analyze(self, lines: Iterator[TextLine], max_records: int) -> StructureAnalysis | None:
        keys: dict[str, None] = {}
        opened = False
        sampled = 0
        for line in islice((line for line in lines if line.text.strip()), max_records):
            text = line.text.strip()
            if not opened:
                if text[0] not in "[{":
                    return None
                opened = True
            sampled += 1
            for match in _KEY.finditer(text[:_SCAN_PREFIX_CHARS]):
                if len(keys) < self._max_fields:
                    keys.setdefault(match.group(1))
        if not opened or not keys:
            return None
        return StructureAnalysis(
            confidence=_CONFIDENCE,
            structure=StructureInfo(fields=tuple(keys), sampled_records=sampled),
        )
