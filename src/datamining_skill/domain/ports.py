"""Interfaces the application layer depends on; implementations live in ``infrastructure``."""

from __future__ import annotations

from collections.abc import Generator, Iterable, Iterator, Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import BinaryIO, Protocol

from datamining_skill.domain.models import (
    ChunkMetadata,
    ChunkRecord,
    ChunkStatus,
    DataFormat,
    EncodingInfo,
    MemorySnapshot,
    StructureAnalysis,
    TextBlock,
    TextLine,
)


class StreamReader(Protocol):
    """Reads bounded slices of a file lazily, one buffer or line at a time."""

    def chunks(self, path: Path, offset: int, length: int) -> Generator[bytes, None, None]: ...

    def lines(
        self,
        path: Path,
        encoding: EncodingInfo,
        offset: int,
        length: int,
        *,
        align: bool = False,
    ) -> Generator[TextLine, None, None]:
        """Yield decoded lines until at least ``length`` bytes were consumed.

        With ``align``, a partial first line (``offset`` inside a line) is skipped.
        """
        ...

    def range_lines(
        self,
        path: Path,
        encoding: EncodingInfo,
        start: int,
        end: int,
        *,
        check_alignment: bool = False,
    ) -> Generator[TextLine, None, None]:
        """Yield the lines of bytes ``[start, end)``, never reading past ``end``.

        A line over the size cap arrives as overlapping windows (see ``TextLine``). With
        ``check_alignment`` the byte before ``start`` must be a line feed. Raises
        ``DataSourceUnavailableException`` if the file is shorter than ``end``.
        """
        ...

    def range_blocks(
        self,
        path: Path,
        encoding: EncodingInfo,
        start: int,
        end: int,
        *,
        check_alignment: bool = False,
    ) -> Generator[TextBlock, None, None]:
        """Like ``range_lines``, but yield runs of whole lines as single blocks of text.

        A block never splits a line, apart from the windows of a line over the size cap. The
        same errors are raised as by ``range_lines``.
        """
        ...


class EncodingDetector(Protocol):
    def detect(self, chunks: Iterator[bytes]) -> EncodingInfo: ...


class ContentGuard(Protocol):
    """Raises ``UnsupportedDataFormatException`` for binary or compressed content."""

    def inspect(self, source_name: str, head: bytes, encoding: EncodingInfo) -> None: ...


class FormatHandler(Protocol):
    @property
    def data_format(self) -> DataFormat: ...

    @property
    def extensions(self) -> frozenset[str]:
        """Lower-case extensions with dot; used only as a tie-breaker."""
        ...

    def analyze(self, lines: Iterator[TextLine], max_records: int) -> StructureAnalysis | None:
        """Inspect up to ``max_records`` leading records; ``None`` if this is not the format."""
        ...


class MemoryProbe(Protocol):
    def snapshot(self) -> MemorySnapshot: ...


class AvailableMemoryProvider(Protocol):
    def available_bytes(self) -> int | None:
        """Bytes the system can hand out now, or ``None`` if the platform cannot tell."""
        ...


class RecordFormatter(Protocol):
    def header(self) -> bytes:
        """Written once at the top of the output; empty for header-less formats."""
        ...

    def format(self, record: Sequence[str]) -> bytes:
        """One record, including its line terminator."""
        ...


class ScratchStore(Protocol):
    """Per-chunk temporary result files (``chunk_{id}.tmp``) in a private directory."""

    def tmp_path(self, chunk_id: int) -> Path: ...

    def open_tmp(self, chunk_id: int) -> AbstractContextManager[BinaryIO]:
        """Open for writing, discarding any previous content."""
        ...

    def tmp_size(self, chunk_id: int) -> int: ...

    def discard_tmp(self, chunk_id: int) -> None: ...

    def clear_stale(self) -> int:
        """Delete every scratch file left by a dead attempt; return how many."""
        ...


class OutputSink(Protocol):
    """The result file, changed only through these crash-safe primitives."""

    @property
    def path(self) -> Path: ...

    def size(self) -> int | None:
        """Size in bytes, or ``None`` if the file does not exist."""
        ...

    def refers_to(self, other: Path) -> bool: ...

    def reset(self, header: bytes) -> int:
        """Create or truncate, write ``header`` durably, and return its length."""
        ...

    def truncate(self, length: int) -> None:
        """Cut back to ``length``; ``OutputIntegrityException`` if the file is shorter."""
        ...

    def append_from(self, source: Path) -> int:
        """Append ``source`` durably and return the new size."""
        ...


class ChunkStateStore(Protocol):
    """Durable, crash-safe record of each chunk's status."""

    @property
    def recovered_orphans(self) -> int:
        """Chunks reverted from IN_PROGRESS to PENDING when the store was opened."""
        ...

    def initialize(self, chunks: Iterable[ChunkMetadata]) -> bool:
        """Store a plan as PENDING; ``False`` if an identical plan is already stored."""
        ...

    def is_initialized(self) -> bool: ...

    def plan_end_byte(self) -> int | None:
        """End of the stored plan, i.e. the source size it was made for."""
        ...

    def committed_output_end(self) -> int | None:
        """Output length after the latest committed chunk, if any."""
        ...

    def requeue_failed(self, max_retries: int | None = None) -> int: ...

    def next_pending(self) -> ChunkRecord | None: ...

    def claim_next_pending(self) -> ChunkRecord | None:
        """Atomically move the next PENDING chunk to IN_PROGRESS and return it."""
        ...

    def mark_in_progress(self, chunk_id: int) -> None: ...

    def mark_completed(self, chunk_id: int, output_end: int | None = None) -> None:
        """IN_PROGRESS -> COMPLETED, recording ``output_end`` in the same transaction."""
        ...

    def mark_failed(self, chunk_id: int) -> None: ...

    def summary(self) -> dict[ChunkStatus, int]: ...


class BoundaryLocator(Protocol):
    """Finds record separators by seeking, never by loading data into memory."""

    def last_boundary(self, path: Path, start: int, limit: int) -> int | None:
        """Offset just past the last separator in ``[start, limit)``.

        ``None`` means there is none: one record is at least ``limit - start`` bytes long.
        """
        ...


class UniqueKeyStore(Protocol):
    """Remembers which records were already written, so ``unique`` mining can drop repeats."""

    def register(self, chunk_id: int, keys: Sequence[bytes]) -> list[bool]:
        """Remember ``keys`` for ``chunk_id``; ``True`` marks each key not seen before."""
        ...

    def discard(self, chunk_id: int) -> None:
        """Forget what an earlier, failed or interrupted attempt at ``chunk_id`` remembered."""
        ...
