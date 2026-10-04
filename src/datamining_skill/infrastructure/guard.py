"""Rejects binary and compressed content before parsing."""

from __future__ import annotations

from datamining_skill.domain.exceptions import UnsupportedDataFormatException
from datamining_skill.domain.models import EncodingInfo

_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x1f\x8b", "gzip-compressed"),
    (b"\x28\xb5\x2f\xfd", "zstandard-compressed"),
    (b"\xfd7zXZ\x00", "xz-compressed"),
    (b"7z\xbc\xaf\x27\x1c", "7-zip archive"),
    (b"PK\x03\x04", "zip archive"),
    (b"PAR1", "Parquet"),
    (b"SQLite format 3\x00", "SQLite database"),
    (b"%PDF-", "PDF document"),
)


def _is_bzip2(head: bytes) -> bool:
    return head.startswith(b"BZh") and head[4:10] == b"1AY&SY"


class BinaryContentGuard:
    """Spots known container signatures and NUL bytes in the first chunk; nothing is parsed."""

    def inspect(self, source_name: str, head: bytes, encoding: EncodingInfo) -> None:
        for signature, description in _SIGNATURES:
            if head.startswith(signature):
                raise UnsupportedDataFormatException(
                    source_name,
                    f"{description} content; decompress or convert it to a text format first",
                )
        if _is_bzip2(head):
            raise UnsupportedDataFormatException(
                source_name,
                "bzip2-compressed content; decompress or convert it to a text format first",
            )
        if encoding.ascii_compatible and b"\x00" in head:
            raise UnsupportedDataFormatException(
                source_name, "binary content (NUL bytes in a text-encoded file)"
            )
