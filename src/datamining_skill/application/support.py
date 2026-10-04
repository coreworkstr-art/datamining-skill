"""Small helpers shared by application services."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from datamining_skill.domain.exceptions import (
    DataMiningException,
    DataSourceUnavailableException,
    printable,
)


def stat_regular_file(source: Path) -> int:
    """Size of ``source``, after checking it is an accessible regular file."""
    try:
        file_stat = source.stat()
    except FileNotFoundError as exc:
        raise DataSourceUnavailableException(source.name, "file does not exist") from exc
    except OSError as exc:
        raise DataSourceUnavailableException(
            source.name, f"cannot be accessed ({type(exc).__name__})"
        ) from exc
    if not source.is_file():
        raise DataSourceUnavailableException(source.name, "not a regular file")
    return file_stat.st_size


def describe_failure(error: Exception) -> str:
    """One-line reason for a failed chunk that never quotes data.

    Library exceptions are written to be shown, an OS error contributes only the system's
    description (no path), and anything else is reduced to its type because arbitrary
    exception text can quote the data being mined.
    """
    if isinstance(error, DataMiningException):
        return str(error)
    if isinstance(error, OSError):
        return printable(error.strerror or type(error).__name__)
    return type(error).__name__


def emit_event(logger: logging.Logger, level: int, event: str, **data: Any) -> None:
    """Log a structured event, payload under ``event_data``, if the level is enabled."""
    if logger.isEnabledFor(level):
        logger.log(level, event, extra={"event_data": data})
