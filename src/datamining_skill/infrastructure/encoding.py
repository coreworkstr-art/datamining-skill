"""Encoding detection from BOMs and trial decoding."""

from __future__ import annotations

import codecs
from collections.abc import Iterator

from datamining_skill.domain.models import EncodingInfo

# order matters: the UTF-32-LE BOM begins with the UTF-16-LE BOM
_BOMS: tuple[tuple[bytes, str, bool], ...] = (
    (codecs.BOM_UTF32_LE, "utf-32-le", False),
    (codecs.BOM_UTF32_BE, "utf-32-be", False),
    (codecs.BOM_UTF8, "utf-8", True),
    (codecs.BOM_UTF16_LE, "utf-16-le", False),
    (codecs.BOM_UTF16_BE, "utf-16-be", False),
)


class StdlibEncodingDetector:
    """Tries, in order: BOM, pure ASCII, strict UTF-8, strict cp1252, then ISO-8859-1 (decodes
    anything, hence low confidence). Chunks are consumed one at a time.

    BOM-less UTF-16/32 is not guessed; the binary-content guard rejects such files downstream.
    """

    def detect(self, chunks: Iterator[bytes]) -> EncodingInfo:
        first = next(chunks, b"")
        for bom, name, ascii_compatible in _BOMS:
            if first.startswith(bom):
                return EncodingInfo(
                    name=name,
                    has_bom=True,
                    bom_length=len(bom),
                    ascii_compatible=ascii_compatible,
                    confidence=1.0,
                )

        utf8 = codecs.getincrementaldecoder("utf-8")()
        utf8_valid = True
        cp1252_valid = True
        all_ascii = True

        def feed(chunk: bytes) -> None:
            nonlocal utf8_valid, cp1252_valid, all_ascii
            if not chunk.isascii():
                all_ascii = False
            if utf8_valid:
                try:
                    # final=False tolerates a multi-byte sequence cut by the sample boundary
                    utf8.decode(chunk, final=False)
                except UnicodeDecodeError:
                    utf8_valid = False
            if cp1252_valid:
                try:
                    chunk.decode("cp1252")
                except UnicodeDecodeError:
                    cp1252_valid = False

        feed(first)
        for chunk in chunks:
            feed(chunk)

        if all_ascii:
            return EncodingInfo(name="ascii", confidence=1.0)
        if utf8_valid:
            return EncodingInfo(name="utf-8", confidence=0.99)
        if cp1252_valid:
            return EncodingInfo(name="cp1252", confidence=0.5)
        return EncodingInfo(name="latin-1", confidence=0.3)
