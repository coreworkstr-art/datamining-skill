"""Plain text: the reading of last resort, for text in which no structure is recognised."""

from __future__ import annotations

import re
from collections.abc import Iterator
from itertools import islice

from datamining_skill.domain.models import DataFormat, StructureAnalysis, StructureInfo, TextLine

# Control characters that text does not contain; a tab, the line breaks, a form feed and the escape
# of coloured terminal output are not among them. A file full of them is binary even without a NUL.
_CONTROL = re.compile(r"[\x00-\x08\x0e-\x1a\x1c-\x1f\x7f]")
_MAX_CONTROL_SHARE = 0.02
# Nothing structured was recognised, so this is the least specific reading there is.
_CONFIDENCE = 0.5


class TextHandler:
    """Reads text that no other handler recognises as plain lines of text.

    It is not a candidate in the usual comparison: ``FormatDetector`` asks it only when every other
    handler has declined. A list of addresses, a web page, an e-mail message, a contact card or a log
    with a layout of its own has no columns, but its lines can be searched all the same, so the file
    is mined line by line. It needs a line with something in it and not many control characters.
    """

    data_format = DataFormat.TEXT
    extensions = frozenset({".txt", ".text", ".eml", ".html", ".htm", ".vcf", ".md", ".xml"})

    def analyze(self, lines: Iterator[TextLine], max_records: int) -> StructureAnalysis | None:
        sampled = characters = control = 0
        for line in islice(lines, max_records):
            if not line.text.strip():
                continue
            sampled += 1
            characters += len(line.text)
            control += len(_CONTROL.findall(line.text))
        if not sampled or control > characters * _MAX_CONTROL_SHARE:
            return None
        return StructureAnalysis(
            confidence=_CONFIDENCE,
            structure=StructureInfo(fields=(), sampled_records=sampled),
        )
