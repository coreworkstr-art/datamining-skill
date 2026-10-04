"""Per-chunk scratch files (``chunk_{id}.tmp``) in a confined, private directory."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

from datamining_skill.domain.exceptions import InvalidConfigurationException
from datamining_skill.infrastructure.paths import default_allowed_roots, resolve_within
from datamining_skill.infrastructure.permissions import PRIVATE_FILE_MODE, ensure_private_directory

_TMP_NAME = re.compile(r"chunk_\d+\.tmp")
_OPEN_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_TRUNC
    | getattr(os, "O_BINARY", 0)  # Windows: no newline translation
    | getattr(os, "O_NOFOLLOW", 0)  # POSIX: never write through a symlink
)


class LocalScratchStore:
    """``ScratchStore`` in a directory inside an allowed root (``.scratch/`` or ``data/``).

    The directory must resolve inside a root, names come only from an integer chunk id so no
    caller text reaches the path, and a symlinked scratch path is refused. Permissions: see
    ``permissions.py``.
    """

    def __init__(
        self, directory: str | os.PathLike[str], *, allowed_roots: Sequence[Path] | None = None
    ) -> None:
        roots = allowed_roots or default_allowed_roots()
        self._dir = resolve_within(directory, roots, label="scratch directory", allow_root=True)
        ensure_private_directory(self._dir)

    @property
    def directory(self) -> Path:
        return self._dir

    def tmp_path(self, chunk_id: int) -> Path:
        if isinstance(chunk_id, bool) or not isinstance(chunk_id, int) or chunk_id < 0:
            raise ValueError("chunk_id must be a non-negative integer")
        path = self._dir / f"chunk_{chunk_id}.tmp"
        if path.parent != self._dir:  # defence in depth; cannot happen for an int id
            raise ValueError("scratch path escaped its directory")
        return path

    @contextmanager
    def open_tmp(self, chunk_id: int) -> Iterator[BinaryIO]:
        """Open for writing, truncating a partial file left by a crashed attempt."""
        path = self.tmp_path(chunk_id)
        if path.is_symlink():
            raise InvalidConfigurationException("scratch file is a symbolic link")
        fd = os.open(path, _OPEN_FLAGS, PRIVATE_FILE_MODE)
        with os.fdopen(fd, "wb") as handle:
            yield handle

    def tmp_size(self, chunk_id: int) -> int:
        return self.tmp_path(chunk_id).stat().st_size

    def discard_tmp(self, chunk_id: int) -> None:
        self.tmp_path(chunk_id).unlink(missing_ok=True)

    def clear_stale(self) -> int:
        """Remove every ``chunk_<n>.tmp`` left by dead attempts; return how many."""
        removed = 0
        for entry in self._dir.iterdir():
            if _TMP_NAME.fullmatch(entry.name) and entry.is_file() and not entry.is_symlink():
                entry.unlink(missing_ok=True)
                removed += 1
        return removed
