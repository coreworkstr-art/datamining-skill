"""Cross-process exclusive lock for one mining job (docs/architecture.md, "Single writer").

A second process would steal in-flight chunks during orphan recovery and truncate the first
one's output, so ``run_mining`` holds a ``JobLock`` for the whole job and a second process
fails at once. It is an OS file lock (``msvcrt.locking`` on Windows, ``flock`` elsewhere),
which the kernel drops when the holder dies. The lock file is never unlinked: a third process
could otherwise lock a fresh file while the second still holds the old one.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import TracebackType
from typing import Self

from datamining_skill.domain.exceptions import JobLockedException
from datamining_skill.infrastructure.permissions import PRIVATE_FILE_MODE, restrict_to_owner

if sys.platform == "win32":
    import msvcrt

    def _try_lock(fd: int) -> bool:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


class JobLock:
    """Holds an exclusive, non-blocking lock on ``path``.

    Entering raises ``JobLockedException`` if any other holder, in this or another process,
    has it.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: int | None = None

    def __enter__(self) -> Self:
        created = not self._path.exists()
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), PRIVATE_FILE_MODE)
        if created:
            restrict_to_owner(self._path)
        if not _try_lock(fd):
            os.close(fd)
            raise JobLockedException(
                "this mining job is already running in another process; "
                "wait for it to finish or stop it before starting another"
            )
        self._fd = fd
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            try:
                _unlock(fd)
            finally:
                os.close(fd)
