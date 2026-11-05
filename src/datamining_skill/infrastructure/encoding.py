"""Encoding detection from BOMs, trial decoding and, for Turkish text, a look at the letters."""

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

# Windows-1254 (Turkish) and Windows-1252 (Western European) agree on every byte but six that matter:
# the capital and small g-breve, i with and without a dot, and s-cedilla of the first are the
# Icelandic eth, thorn and the accented y of the second. A file that is not valid UTF-8 and holds
# these bytes without the accented vowels of the Western languages is taken to be Turkish.
_TURKISH_BYTES = bytes.fromhex("d0 dd de f0 fd fe")
# the letters Western languages use and Turkish does not; the ones both share (c-cedilla, o and u
# with a diaeresis, the circumflexes) are left out
_WESTERN_BYTES = bytes.fromhex(
    "c0 c1 c3 c4 c5 c6 c8 c9 ca cb cc cd cf d1 d2 d3 d4 d5 d8 d9 da df "
    "e0 e1 e3 e4 e5 e6 e8 e9 ea eb ec ed ef f1 f2 f3 f4 f5 f8 f9 fa ff"
)
_TURKISH_OVER_WESTERN = 4


def _count(chunk: bytes, wanted: bytes) -> int:
    """How many bytes of ``chunk`` are among ``wanted``, counted at C speed."""
    return len(chunk) - len(chunk.translate(None, wanted))


class StdlibEncodingDetector:
    """Tries, in order: BOM, pure ASCII, strict UTF-8, strict cp1252 (cp1254 if the letters are
    Turkish), then ISO-8859-1 (decodes anything, hence low confidence). Chunks are consumed one at
    a time.

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
        cp1254_valid = True
        all_ascii = True
        turkish = western = 0

        def feed(chunk: bytes) -> None:
            nonlocal utf8_valid, cp1252_valid, cp1254_valid, all_ascii, turkish, western
            if not chunk.isascii():
                all_ascii = False
                turkish += _count(chunk, _TURKISH_BYTES)
                western += _count(chunk, _WESTERN_BYTES)
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
            if cp1254_valid:
                try:
                    chunk.decode("cp1254")
                except UnicodeDecodeError:
                    cp1254_valid = False

        feed(first)
        for chunk in chunks:
            feed(chunk)

        if all_ascii:
            return EncodingInfo(name="ascii", confidence=1.0)
        if utf8_valid:
            return EncodingInfo(name="utf-8", confidence=0.99)
        if cp1252_valid:
            if cp1254_valid and turkish and turkish >= _TURKISH_OVER_WESTERN * western:
                return EncodingInfo(name="cp1254", confidence=0.7)
            return EncodingInfo(name="cp1252", confidence=0.5)
        return EncodingInfo(name="latin-1", confidence=0.3)
