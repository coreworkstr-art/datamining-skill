"""The final result file, modified only through crash-safe primitives."""

from __future__ import annotations

import errno
import os
import shutil
import stat
from pathlib import Path

from datamining_skill.domain.exceptions import (
    InvalidConfigurationException,
    OutputIntegrityException,
)
from datamining_skill.infrastructure.permissions import PRIVATE_FILE_MODE, restrict_to_owner

_COPY_BLOCK_BYTES = 1024 * 1024
_BINARY = getattr(os, "O_BINARY", 0)  # Windows: no newline translation
_NO_FOLLOW = getattr(os, "O_NOFOLLOW", 0)  # POSIX: never open through a symlink
# a FIFO opened for writing blocks until a reader appears; with this it fails at once, and the
# file type check below then refuses whatever turned up. Has no effect on regular files.
_NO_BLOCK = getattr(os, "O_NONBLOCK", 0)
_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _BINARY | _NO_FOLLOW | _NO_BLOCK
_RDWR_FLAGS = os.O_RDWR | _BINARY | _NO_FOLLOW | _NO_BLOCK
_APPEND_FLAGS = os.O_WRONLY | os.O_APPEND | _BINARY | _NO_FOLLOW | _NO_BLOCK
_SYMLINK_ERRORS = frozenset({errno.ELOOP, errno.EMLINK})  # what O_NOFOLLOW reports on a symlink


class LocalOutputSink:
    """``OutputSink`` on a local file, built from ``reset``, ``truncate`` and ``append_from``.

    Each fsyncs before returning, so a reported length is on disk before it is recorded as
    committed. The file is created owner-only (``0600``) on POSIX; Windows applies the
    owner-only ACL from ``permissions.py``.

    Every open goes through ``_open_checked``: a symbolic link at the output path is refused
    (also at the moment of opening on POSIX, through ``O_NOFOLLOW``), the opened file must be a
    regular file, and its identity (device and inode) is remembered, so a file swapped in
    between two operations is detected instead of written to.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        resolved = Path(path).expanduser().resolve()
        if resolved.is_dir():
            raise InvalidConfigurationException("output path is a directory")
        if not resolved.parent.is_dir():
            raise InvalidConfigurationException("the output file's directory does not exist")
        self._path = resolved
        self._identity: tuple[int, int] | None = None

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

    def _open_checked(self, flags: int, mode: int = PRIVATE_FILE_MODE, *, creating: bool = False) -> int:
        """Open the output file and return the descriptor, or raise.

        ``creating`` starts a new file (``reset``), whose identity replaces the remembered one.
        Raises ``OutputIntegrityException`` for a symbolic link, a file that is not a regular
        file, or a file other than the one used before; ``InvalidConfigurationException`` for a
        link found while the file is first being created, which is a mistake in the path given.
        """
        if self._path.is_symlink():
            error = InvalidConfigurationException if creating and self._identity is None else OutputIntegrityException
            raise error("output path is a symbolic link")
        try:
            descriptor = os.open(self._path, flags, mode)
        except OSError as exc:
            if exc.errno in _SYMLINK_ERRORS:
                raise OutputIntegrityException("output path is a symbolic link") from exc
            if exc.errno == errno.ENXIO:  # a FIFO without a reader
                raise OutputIntegrityException("output path is not a regular file") from exc
            raise
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise OutputIntegrityException("output path is not a regular file")
            if info.st_ino != 0:  # some file systems report no inode: nothing to compare then
                identity = (info.st_dev, info.st_ino)
                if creating or self._identity is None:
                    self._identity = identity
                elif identity != self._identity:
                    raise OutputIntegrityException("output file was replaced while mining")
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    def reset(self, header: bytes) -> int:
        created = not self._path.exists()
        fd = self._open_checked(_CREATE_FLAGS, creating=True)
        with os.fdopen(fd, "wb") as handle:
            handle.write(header)
            handle.flush()
            os.fsync(handle.fileno())
        if created:
            restrict_to_owner(self._path)
        return len(header)

    def truncate(self, length: int) -> None:
        try:
            fd = self._open_checked(_RDWR_FLAGS)
        except FileNotFoundError as exc:
            raise OutputIntegrityException(
                "the output file is missing although chunks are recorded as committed"
            ) from exc
        with os.fdopen(fd, "r+b") as handle:
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

    def append_from(self, source: Path) -> int:
        with source.open("rb") as reader:
            try:
                fd = self._open_checked(_APPEND_FLAGS)
            except FileNotFoundError as exc:
                raise OutputIntegrityException(
                    "the output file is missing although chunks are recorded as committed"
                ) from exc
            with os.fdopen(fd, "ab") as writer:
                shutil.copyfileobj(reader, writer, _COPY_BLOCK_BYTES)
                writer.flush()
                os.fsync(writer.fileno())
                return os.fstat(writer.fileno()).st_size
