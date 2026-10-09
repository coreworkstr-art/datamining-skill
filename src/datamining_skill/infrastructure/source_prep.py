"""Converts a compressed or UTF-16/32 source into a plain UTF-8 copy that can be mined.

Mining locates records by byte range, which needs one byte per character and a plain file.
gzip, bzip2, xz and single-file zip archives are decompressed, and UTF-16/32 text (what Excel
calls "Unicode Text") is re-encoded, both as a stream in constant memory. The copy lives in
the job's private scratch directory and is deleted when the job finishes. Everything is
local; nothing is downloaded or executed (docs/architecture.md, "Source conversion").
"""

from __future__ import annotations

import bz2
import codecs
import gzip
import lzma
import os
import shutil
import zipfile
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, cast

from datamining_skill.domain.exceptions import (
    InvalidConfigurationException,
    ResourceExhaustionError,
    UnsupportedDataFormatException,
)
from datamining_skill.domain.models import format_size
from datamining_skill.infrastructure.permissions import PRIVATE_FILE_MODE, restrict_to_owner

DEFAULT_MAX_EXPANDED_BYTES = 64 * 1024**3
_BLOCK_BYTES = 1024 * 1024
_FREE_CHECK_INTERVAL = 64 * 1024 * 1024
_MIN_FREE_BYTES = 1024 * 1024 * 1024  # never leave the volume nearly full for other programs
_CREATE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_TRUNC
    | getattr(os, "O_BINARY", 0)  # Windows: no newline translation
    | getattr(os, "O_NOFOLLOW", 0)  # POSIX: never write through a symlink
)

_COMPRESSION_MAGIC = (
    (b"\x1f\x8b", "gzip"),
    (b"BZh", "bzip2"),
    (b"\xfd7zXZ\x00", "xz"),
    (b"PK\x03\x04", "zip"),
)
# the UTF-32 byte-order marks begin like the UTF-16 ones, so they come first
_WIDE_BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32-le"),
    (codecs.BOM_UTF32_BE, "utf-32-be"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
)
_READ_ERRORS = (OSError, EOFError, zlib.error, lzma.LZMAError, zipfile.BadZipFile)


def compression_of(head: bytes) -> str | None:
    """The compression format a file starting with ``head`` uses, if any."""
    for magic, name in _COMPRESSION_MAGIC:
        if head.startswith(magic):
            return name
    return None


def wide_codec_of(head: bytes) -> tuple[str, int] | None:
    """The UTF-16/32 codec and byte-order-mark length for ``head``, if it starts with one."""
    for bom, codec in _WIDE_BOMS:
        if head.startswith(bom):
            return codec, len(bom)
    return None


def needs_conversion(source: Path) -> bool:
    """Whether ``source`` must be converted before it can be mined."""
    with source.open("rb") as handle:
        head = handle.read(8)
    return compression_of(head) is not None or wide_codec_of(head) is not None


def convert_to_utf8(
    source: Path,
    target: Path,
    *,
    max_bytes: int = DEFAULT_MAX_EXPANDED_BYTES,
    sample_bytes: int | None = None,
) -> str | None:
    """Write a plain UTF-8 copy of ``source`` to ``target`` and name the conversions applied.

    Stops after ``sample_bytes`` when given (a head sample for profiling). Raises
    ``UnsupportedDataFormatException`` if the data is damaged, encrypted, an archive of several
    files, or larger than ``max_bytes`` once expanded (a decompression bomb), and
    ``ResourceExhaustionError`` if the disk is nearly full. A partial copy is never left behind.
    """
    with source.open("rb") as handle:
        compression = compression_of(handle.read(8))
    steps = [compression] if compression else []
    partial = target.with_name(target.name + ".partial")
    written = 0
    since_check = 0
    done = False
    if partial.is_symlink():  # a planted link must not redirect the write to another file
        raise InvalidConfigurationException("the temporary conversion file is a symbolic link")
    descriptor = os.open(partial, _CREATE_FLAGS, PRIVATE_FILE_MODE)
    try:
        with os.fdopen(descriptor, "wb") as sink, _open_stream(source, compression) as reader:
            restrict_to_owner(partial)
            first = _read(reader, 4, source)
            wide = wide_codec_of(first)
            decoder: codecs.IncrementalDecoder | None = None
            block = first
            at_end = not first
            if wide is not None:
                steps.append(wide[0])
                decoder = codecs.getincrementaldecoder(wide[0])(errors="replace")
                block = first[wide[1] :]
            while True:
                data = block if decoder is None else decoder.decode(block, final=at_end).encode("utf-8")
                if data:
                    sink.write(data)
                    written += len(data)
                    since_check += len(data)
                    if written > max_bytes:
                        raise UnsupportedDataFormatException(
                            source.name,
                            f"expands to more than {format_size(max_bytes)}; "
                            "raise the expansion limit if this is expected",
                        )
                    if sample_bytes is not None and written >= sample_bytes:
                        break
                    if since_check >= _FREE_CHECK_INTERVAL:
                        since_check = 0
                        _require_free_space(target.parent)
                if at_end:
                    break
                block = _read(reader, _BLOCK_BYTES, source)
                at_end = not block
        os.replace(partial, target)
        done = True
    finally:
        if not done:
            partial.unlink(missing_ok=True)
    return "+".join(steps) if steps else None


def _require_free_space(directory: Path) -> None:
    free = shutil.disk_usage(directory).free
    if free < _MIN_FREE_BYTES:
        raise ResourceExhaustionError(
            f"only {format_size(free)} of disk space is left for the converted copy",
            free,
            _MIN_FREE_BYTES,
        )


def _read(reader: IO[bytes], size: int, source: Path) -> bytes:
    try:
        return reader.read(size)
    except _READ_ERRORS as exc:
        raise UnsupportedDataFormatException(
            source.name, "the compressed data is damaged or incomplete"
        ) from exc


@contextmanager
def _open_stream(source: Path, compression: str | None) -> Iterator[IO[bytes]]:
    try:
        if compression is None:
            with source.open("rb") as plain:
                yield plain
        elif compression == "gzip":
            with gzip.open(source, "rb") as gzip_stream:
                yield cast(IO[bytes], gzip_stream)  # typeshed's GzipFile is not an IO[bytes]
        elif compression == "bzip2":
            with bz2.open(source, "rb") as stream:
                yield stream
        elif compression == "xz":
            with lzma.open(source, "rb") as stream:
                yield stream
        else:
            with zipfile.ZipFile(source) as archive:
                members = [item for item in archive.infolist() if not item.is_dir()]
                if len(members) != 1:
                    raise UnsupportedDataFormatException(
                        source.name,
                        f"zip archive with {len(members)} files; extract the file to mine first",
                    )
                if members[0].flag_bits & 0x1:
                    raise UnsupportedDataFormatException(
                        source.name, "encrypted zip archive; extract it first"
                    )
                with archive.open(members[0]) as stream:
                    yield stream
    except zipfile.BadZipFile as exc:
        raise UnsupportedDataFormatException(
            source.name, "the compressed data is damaged or incomplete"
        ) from exc
