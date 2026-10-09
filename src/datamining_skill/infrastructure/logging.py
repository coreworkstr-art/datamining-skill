"""Single-line JSON logging: ``{"timestamp", "level", "logger", "event", "data"}``.

Payloads arrive via ``extra={"event_data": {...}}`` and never contain file contents.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import IO, Any

PROFILER_LOGGER_NAME = "datamining_skill.profiler"
CHUNKING_LOGGER_NAME = "datamining_skill.chunking"
STATE_LOGGER_NAME = "datamining_skill.state"
MINING_LOGGER_NAME = "datamining_skill.mining"
_PACKAGE_LOGGER_NAME = "datamining_skill"


class JsonLogFormatter(logging.Formatter):
    """Renders a log record as one compact JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        event_data = getattr(record, "event_data", None)
        if isinstance(event_data, Mapping):
            payload["data"] = dict(event_data)
        if record.exc_info and record.exc_info[0] is not None:
            payload["exception_type"] = record.exc_info[0].__name__
        return json.dumps(payload, default=str, separators=(",", ":"))


class _JsonStreamHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """Marker subclass that makes repeated configuration idempotent.

    ``StreamHandler`` is not subscripted: ``StreamHandler[...]`` fails at runtime on some
    supported Python versions.
    """


def configure_json_logging(
    level: int = logging.INFO, stream: IO[str] | None = None
) -> logging.Logger:
    """Attach a JSON handler to the package logger, replacing one installed earlier.

    For applications and the CLI; the library itself never calls it.
    """
    logger = disable_json_logging()
    handler = _JsonStreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(JsonLogFormatter())
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return logger


def disable_json_logging() -> logging.Logger:
    """Remove the JSON handler installed by ``configure_json_logging``, if any.

    A process that calls the command-line entry point repeatedly would otherwise keep logging
    to the stream of an earlier call, which may be closed by now.
    """
    logger = logging.getLogger(_PACKAGE_LOGGER_NAME)
    for existing in list(logger.handlers):
        if isinstance(existing, _JsonStreamHandler):
            logger.removeHandler(existing)
    logger.propagate = True
    return logger
