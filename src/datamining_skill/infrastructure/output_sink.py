"""The final result file, modified only through crash-safe primitives."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from datamining_skill.domain.exceptions import (
    InvalidConfigurationException,
    OutputIntegrityException,
)
from datamining_skill.infrastructure.permissions import PRIVATE_FILE_MODE, restrict_to_owner

_COPY_BLOCK_BYTES = 1024 * 1024
_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)


class LocalOutputSink:
    """``OutputSink`` on a local file, built from ``reset``, ``truncate`` and ``append_from``.

    Each fsyncs before returning, so a reported length is on disk before it is recorded as
    committed. The file is created owner-only (``0600``) on POSIX; Windows applies the
    owner-only ACL from ``permissions.py``.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        resolved = Path(path).expanduser().resolve()
        if resolved.is_dir():
            raise InvalidConfigurationException("output path is a directory")
        if not resolved.parent.is_dir():
            raise InvalidConfigurationException("the output file's directory does not exist")
        self._path = resolved

    @property
    def path(self) -> Path:
        return self._path

    def size(self) -> int | None:
        try:
            return self._path.stat().st_size
        except FileNotFoundError:
            return None

    def refers_to(self, other: Path) -> bool:
        resolved = other.expanduser().resolve()
        if resolved == self._path:
            return True
        try:
            return os.path.samefile(resolved, self._path)
        except OSError:  # one of them does not exist
            return False

    def reset(self, header: bytes) -> int:
        created = not self._path.exists()
        fd = os.open(self._path, _CREATE_FLAGS, PRIVATE_FILE_MODE)
        with os.fdopen(fd, "wb") as handle:
            handle.write(header)
            handle.flush()
            os.fsync(handle.fileno())
        if created:
            restrict_to_owner(self._path)
        return len(header)

    def truncate(self, length: int) -> None:
        try:
            with self._path.open("r+b") as handle:
                current = handle.seek(0, os.SEEK_END)
                if current < length:
                    raise OutputIntegrityException(
                        f"output file holds {current} bytes but {length} are recorded as "
                        "committed; it was deleted, truncated or replaced"
                    )
                if current > length:
                    handle.truncate(length)
                    handle.flush()
                    os.fsync(handle.fileno())
        except FileNotFoundError as exc:
            raise OutputIntegrityException(
                "the output file is missing although chunks are recorded as committed"
            ) from exc

    def append_from(self, source: Path) -> int:
        with source.open("rb") as reader, self._path.open("ab") as writer:
            shutil.copyfileobj(reader, writer, _COPY_BLOCK_BYTES)
            writer.flush()
            os.fsync(writer.fileno())
            return os.fstat(writer.fileno()).st_size
