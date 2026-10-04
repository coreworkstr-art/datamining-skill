"""Domain layer: value objects, exceptions and ports. Depends on nothing else."""

from datamining_skill.domain.exceptions import (
    DataMiningException,
    DataSourceUnavailableException,
    InvalidConfigurationException,
    InvalidStateTransitionException,
    ResourceExhaustionError,
    StateStoreException,
    UnsupportedDataFormatException,
)
from datamining_skill.domain.models import (
    ChunkMetadata,
    ChunkRecord,
    ChunkStatus,
    DataFormat,
    EncodingInfo,
    FileProfile,
    MemorySnapshot,
    ProfilingStats,
    RecordEstimate,
    StructureAnalysis,
    StructureInfo,
    TextLine,
)

__all__ = [
    "ChunkMetadata",
    "ChunkRecord",
    "ChunkStatus",
    "DataFormat",
    "DataMiningException",
    "DataSourceUnavailableException",
    "EncodingInfo",
    "FileProfile",
    "InvalidConfigurationException",
    "InvalidStateTransitionException",
    "MemorySnapshot",
    "ProfilingStats",
    "RecordEstimate",
    "ResourceExhaustionError",
    "StateStoreException",
    "StructureAnalysis",
    "StructureInfo",
    "TextLine",
    "UnsupportedDataFormatException",
]
