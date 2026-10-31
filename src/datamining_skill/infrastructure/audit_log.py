"""An append-only record of what the MCP server did, one JSON object per line.

Each tool call leaves the time, the tool, whether it worked, its duration, workspace-relative
paths, switches and counts. It never holds file contents, patterns or the text of an
argument. The file must lie outside the directories the tools may touch, otherwise the
assistant could read it with ``preview_result`` or overwrite it with ``mine_dataset``. It is
created owner-only, reopened for every record (nothing stays open), opened with ``O_APPEND`` so
concurrent lines do not mix, and never written through a symbolic link.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from datamining_skill.domain.exceptions import InvalidConfigurationException
from datamining_skill.infrastructure.paths import check_path_text
from datamining_skill.infrastructure.permissions import PRIVATE_FILE_MODE, restrict_to_owner

_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_APPEND
    | getattr(os, "O_BINARY", 0)  # Windows: no newline translation
    | getattr(os, "O_NOFOLLOW", 0)  # POSIX: never write through a symlink
)


class AuditLog:
    """Appends records to a file outside ``forbidden_roots``."""

    def __init__(self, path: str | os.PathLike[str], *, forbidden_roots: Sequence[Path] = ()) -> None:
        raw = os.fspath(path)
        check_path_text(raw, "the audit log path")
        requested = Path(raw)
        try:
            parent = requested.parent.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise InvalidConfigurationException("the folder of the audit log does not exist") from exc
        if not parent.is_dir():
            raise InvalidConfigurationException("the folder of the audit log is not a directory")
        target = parent / requested.name
        if not requested.name or target.is_symlink() or target.is_dir():
            raise InvalidConfigurationException("the audit log path must name a regular file")
        for root in forbidden_roots:
            if parent == root or root in parent.parents:
                raise InvalidConfigurationException(
                    "the audit log must lie outside the allowed directories, where the assistant "
                    "could read or overwrite it"
                )
        self._path = target
        self._lock = threading.Lock()
        self._restricted = False

    def record(self, **fields: Any) -> None:
        """Append one record. Raises ``OSError`` when the file cannot be written."""
        entry = {"time": datetime.now(UTC).isoformat(timespec="milliseconds"), **fields}
        line = (json.dumps(entry, ensure_ascii=True, separators=(",", ":"), default=str) + "\n").encode("ascii")
        with self._lock:
            descriptor = os.open(self._path, _FLAGS, PRIVATE_FILE_MODE)
            try:
                view = memoryview(line)
                while view:
                    view = view[os.write(descriptor, view) :]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            if not self._restricted:
                restrict_to_owner(self._path)
                self._restricted = True
