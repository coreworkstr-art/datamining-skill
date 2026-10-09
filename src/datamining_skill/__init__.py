"""DataMining Skill: a local-only, streaming-first data mining toolkit.

Example::

    from datamining_skill import create_chunking_engine, create_profiler

    profile = create_profiler().profile("events.jsonl")
    for chunk in create_chunking_engine().plan("events.jsonl", profile):
        print(chunk.to_dict())
"""

import logging

from datamining_skill._version import __version__
from datamining_skill.application.chunking_engine import ChunkingEngine
from datamining_skill.application.config import ChunkingConfig, OrchestratorConfig, ProfilerConfig
from datamining_skill.application.data_profiler import DataProfiler
from datamining_skill.application.extraction_strategy import ExtractionStrategy, RegexExtractor
from datamining_skill.application.miner_worker import ChunkResult, MinerWorker, SourceDescriptor
from datamining_skill.application.options import MiningOptions
from datamining_skill.application.orchestrator import (
    MiningOrchestrator,
    MiningProgress,
    MiningSummary,
)
from datamining_skill.application.record_formats import CsvFormatter, JsonlFormatter
from datamining_skill.application.result_aggregator import ResultAggregator
from datamining_skill.bootstrap import (
    create_chunking_engine,
    create_mcp_server,
    create_orchestrator,
    create_profiler,
    profile_source,
    run_mining,
)
from datamining_skill.domain.exceptions import (
    DataMiningException,
    DataSourceUnavailableException,
    InvalidConfigurationException,
    InvalidStateTransitionException,
    JobLockedException,
    MiningCancelledException,
    OutputIntegrityException,
    ResourceExhaustionError,
    StateStoreException,
    UnsupportedDataFormatException,
)
from datamining_skill.domain.models import (
    ChunkMetadata,
    ChunkRecord,
    ChunkStatus,
    DataFormat,
    FileProfile,
)
from datamining_skill.infrastructure.state_manager import StateManager

# stay silent unless the host application opts in
logging.getLogger(__name__).addHandler(logging.NullHandler())

__all__ = [
    "ChunkMetadata",
    "ChunkRecord",
    "ChunkResult",
    "ChunkStatus",
    "ChunkingConfig",
    "ChunkingEngine",
    "CsvFormatter",
    "DataFormat",
    "DataMiningException",
    "DataProfiler",
    "DataSourceUnavailableException",
    "ExtractionStrategy",
    "FileProfile",
    "InvalidConfigurationException",
    "InvalidStateTransitionException",
    "JobLockedException",
    "JsonlFormatter",
    "MinerWorker",
    "MiningCancelledException",
    "MiningOptions",
    "MiningOrchestrator",
    "MiningProgress",
    "MiningSummary",
    "OrchestratorConfig",
    "OutputIntegrityException",
    "ProfilerConfig",
    "RegexExtractor",
    "ResourceExhaustionError",
    "ResultAggregator",
    "SourceDescriptor",
    "StateManager",
    "StateStoreException",
    "UnsupportedDataFormatException",
    "__version__",
    "create_chunking_engine",
    "create_mcp_server",
    "create_orchestrator",
    "create_profiler",
    "profile_source",
    "run_mining",
]
