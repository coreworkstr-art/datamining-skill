"""Library exceptions, all derived from ``DataMiningException``.

Messages name the file only (never its full path) and never include data content, so they
are safe to log or show to a user.
"""

from __future__ import annotations


def printable(text: str) -> str:
    """Escape non-printable characters so a file name cannot forge terminal output.

    An ANSI escape sequence or bidirectional override in a name would otherwise rewrite the
    user's terminal when an error message is shown.
    """
    return "".join(
        char if char.isprintable() else f"\\x{ord(char):02x}" if ord(char) < 256 else f"\\u{ord(char):04x}"
        for char in text
    )


class DataMiningException(Exception):
    """Base class for every error this library raises deliberately."""


class UnsupportedDataFormatException(DataMiningException):
    """Unrecognised, binary, compressed or empty input, or a structure that cannot be determined."""

    def __init__(self, source_name: str, reason: str) -> None:
        source_name = printable(source_name)
        super().__init__(f"Unsupported data format for '{source_name}': {reason}")
        self.source_name = source_name
        self.reason = reason


class DataSourceUnavailableException(DataMiningException):
    """The input does not exist, is not a regular file, cannot be read, or changed while read."""

    def __init__(self, source_name: str, reason: str) -> None:
        source_name = printable(source_name)
        super().__init__(f"Data source '{source_name}' is unavailable: {reason}")
        self.source_name = source_name
        self.reason = reason


class ResourceExhaustionError(DataMiningException):
    """System resources are too low to start safely; raised before any work begins."""

    def __init__(self, reason: str, available_bytes: int, required_bytes: int) -> None:
        super().__init__(reason)
        self.reason = reason
        self.available_bytes = available_bytes
        self.required_bytes = required_bytes


class StateStoreException(DataMiningException):
    """The chunk state database failed, is unusable, or was used incorrectly.

    Messages never include file paths or SQL.
    """


class InvalidStateTransitionException(StateStoreException):
    """A chunk status change is not permitted from the chunk's current status."""

    def __init__(self, chunk_id: int, current: str, requested: str) -> None:
        super().__init__(f"chunk {chunk_id}: cannot move from {current} to {requested}")
        self.chunk_id = chunk_id
        self.current = current
        self.requested = requested


class OutputIntegrityException(DataMiningException):
    """The result file or a chunk's scratch file does not match what the ledger requires.

    Merging stops rather than producing a silently corrupt result.
    """


class JobLockedException(DataMiningException):
    """Another process is already running the same mining job.

    Two writers on one job would corrupt the shared output file, so the second is refused
    instead of waiting.
    """


class InvalidConfigurationException(DataMiningException, ValueError):
    """A configuration value is missing, malformed or out of range."""


class MiningCancelledException(DataMiningException):
    """The caller asked for the run to stop; the job resumes from its last checkpoint."""
