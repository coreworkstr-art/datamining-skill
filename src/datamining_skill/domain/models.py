"""Immutable value objects shared by every layer."""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any


class DataFormat(enum.StrEnum):
    CSV = "csv"
    JSONL = "jsonl"
    LOG = "log"


@dataclass(frozen=True, slots=True)
class TextLine:
    """A decoded line without its terminator.

    ``byte_length`` is the on-disk size, terminator included and counting any bytes dropped
    by truncation; ``truncated`` means the line exceeded the size cap and ``text`` is a prefix.
    """

    text: str
    byte_length: int
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class EncodingInfo:
    name: str
    has_bom: bool = False
    bom_length: int = 0
    ascii_compatible: bool = True
    confidence: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "has_bom": self.has_bom,
            "confidence": round(self.confidence, 3),
        }


@dataclass(frozen=True, slots=True)
class StructureInfo:
    """Columns, JSON keys or log fields found in the head sample."""

    fields: tuple[str, ...]
    sampled_records: int
    delimiter: str | None = None
    has_header: bool | None = None
    pattern: str | None = None
    field_types: Mapping[str, tuple[str, ...]] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "fields": list(self.fields),
            "field_count": len(self.fields),
            "sampled_records": self.sampled_records,
            "delimiter": self.delimiter,
            "has_header": self.has_header,
            "pattern": self.pattern,
            "field_types": (
                {name: list(types) for name, types in self.field_types.items()}
                if self.field_types is not None
                else None
            ),
        }


@dataclass(frozen=True, slots=True)
class StructureAnalysis:
    """A format handler's verdict: ``confidence`` in [0, 1] and the structure it found.

    ``header_bytes`` is the size of the header record (0 if none); record estimation starts
    after it.
    """

    confidence: float
    structure: StructureInfo
    header_bytes: int = 0


@dataclass(frozen=True, slots=True)
class RecordEstimate:
    count: int
    is_exact: bool
    method: str
    sampled_records: int
    sampled_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "is_exact": self.is_exact,
            "method": self.method,
            "sampled_records": self.sampled_records,
            "sampled_bytes": self.sampled_bytes,
        }


@dataclass(frozen=True, slots=True)
class MemorySnapshot:
    """Process memory at one instant; ``None`` where the platform cannot report it."""

    rss_bytes: int | None
    peak_rss_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class ProfilingStats:
    duration_seconds: float
    memory_start: MemorySnapshot
    memory_end: MemorySnapshot

    @property
    def rss_delta_bytes(self) -> int | None:
        start, end = self.memory_start.rss_bytes, self.memory_end.rss_bytes
        if start is None or end is None:
            return None
        return end - start

    def to_dict(self) -> dict[str, Any]:
        return {
            "duration_ms": round(self.duration_seconds * 1000, 3),
            "memory": {
                "start_rss_bytes": self.memory_start.rss_bytes,
                "end_rss_bytes": self.memory_end.rss_bytes,
                "delta_rss_bytes": self.rss_delta_bytes,
                "peak_rss_bytes": self.memory_end.peak_rss_bytes,
            },
        }


@dataclass(frozen=True, slots=True)
class ChunkMetadata:
    """A record-aligned byte range ``[start_byte, end_byte)`` of a data file.

    The chunks of one plan tile the file exactly. Every chunk but the first starts right after
    a record separator and every chunk but the last ends with one (the last ends at end of file,
    which may lack a newline). The first also holds any BOM and header row.
    ``estimated_records`` comes from the profile and is not a count.
    """

    chunk_id: int
    start_byte: int
    end_byte: int
    estimated_records: int = 0

    @property
    def size_bytes(self) -> int:
        return self.end_byte - self.start_byte

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "start_byte": self.start_byte,
            "end_byte": self.end_byte,
            "size_bytes": self.size_bytes,
            "estimated_records": self.estimated_records,
        }


class ChunkStatus(enum.StrEnum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class ChunkRecord:
    """A chunk's persisted state. Metadata only, never dataset content.

    ``retry_count`` counts attempts that did not complete (failures and crash-abandoned
    attempts); ``updated_at`` is a timezone-aware UTC timestamp.
    """

    chunk_id: int
    start_byte: int
    end_byte: int
    status: ChunkStatus
    retry_count: int
    updated_at: datetime

    @property
    def size_bytes(self) -> int:
        return self.end_byte - self.start_byte

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "start_byte": self.start_byte,
            "end_byte": self.end_byte,
            "status": self.status.value,
            "retry_count": self.retry_count,
            "updated_at": self.updated_at.isoformat(),
        }


def format_size(size_bytes: int) -> str:
    scaled = float(size_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if scaled < 1024.0 or unit == "TiB":
            return f"{int(scaled)} B" if unit == "B" else f"{scaled:.2f} {unit}"
        scaled /= 1024.0
    raise AssertionError("unreachable")  # pragma: no cover


@dataclass(frozen=True, slots=True)
class FileProfile:
    file_name: str
    size_bytes: int
    data_format: DataFormat
    encoding: EncodingInfo
    structure: StructureInfo
    records: RecordEstimate
    stats: ProfilingStats
    data_offset: int = 0  # first data byte, after any BOM and header row

    def to_dict(self) -> dict[str, Any]:
        """A JSON-serialisable metadata dictionary."""
        return {
            "file": {
                "name": self.file_name,
                "size_bytes": self.size_bytes,
                "size_human": format_size(self.size_bytes),
                "data_offset_bytes": self.data_offset,
            },
            "format": self.data_format.value,
            "encoding": self.encoding.to_dict(),
            "structure": self.structure.to_dict(),
            "records": self.records.to_dict(),
            "profiling": self.stats.to_dict(),
        }
