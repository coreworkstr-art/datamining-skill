"""Composition root: the only module that knows both the application and infrastructure layers."""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from datamining_skill._version import __version__
from datamining_skill.application.chunking_engine import ChunkingEngine
from datamining_skill.application.config import ChunkingConfig, OrchestratorConfig, ProfilerConfig
from datamining_skill.application.data_profiler import DataProfiler
from datamining_skill.application.extraction_strategy import ExtractionStrategy, RegexExtractor
from datamining_skill.application.format_detection import FormatDetector
from datamining_skill.application.miner_worker import MinerWorker
from datamining_skill.application.orchestrator import MiningOrchestrator, MiningProgress, MiningSummary
from datamining_skill.application.record_estimation import RecordEstimator
from datamining_skill.application.record_formats import CsvFormatter, JsonlFormatter
from datamining_skill.application.result_aggregator import ResultAggregator
from datamining_skill.domain.exceptions import InvalidConfigurationException
from datamining_skill.domain.ports import (
    AvailableMemoryProvider,
    ChunkStateStore,
    FormatHandler,
    MemoryProbe,
    RecordFormatter,
    StreamReader,
)
from datamining_skill.infrastructure.boundaries import NewlineBoundaryLocator
from datamining_skill.infrastructure.encoding import StdlibEncodingDetector
from datamining_skill.infrastructure.guard import BinaryContentGuard
from datamining_skill.infrastructure.handlers import CsvHandler, JsonlHandler, LogHandler
from datamining_skill.infrastructure.job_lock import JobLock
from datamining_skill.infrastructure.logging import (
    CHUNKING_LOGGER_NAME,
    MINING_LOGGER_NAME,
    PROFILER_LOGGER_NAME,
)
from datamining_skill.infrastructure.mcp_server import DEFAULT_MAX_MESSAGE_CHARS, McpServer, ServerIdentity
from datamining_skill.infrastructure.mcp_tools import MineRunner, MiningTools, ProfileFn, WorkspacePolicy
from datamining_skill.infrastructure.memory import ProcessMemoryProbe
from datamining_skill.infrastructure.output_sink import LocalOutputSink
from datamining_skill.infrastructure.paths import confined_child
from datamining_skill.infrastructure.permissions import ensure_private_directory
from datamining_skill.infrastructure.scratch import LocalScratchStore
from datamining_skill.infrastructure.state_manager import StateManager
from datamining_skill.infrastructure.streaming import FileStreamReader
from datamining_skill.infrastructure.system_memory import SystemMemoryProbe


def create_profiler(
    config: ProfilerConfig | None = None,
    *,
    logger: logging.Logger | None = None,
    memory_probe: MemoryProbe | None = None,
    extra_handlers: tuple[FormatHandler, ...] = (),
) -> DataProfiler:
    """Build a ``DataProfiler`` from local components.

    ``extra_handlers`` are tried after the built-in ones. The default logger stays silent
    until a handler is attached (``configure_json_logging``).
    """
    settings = config or ProfilerConfig()
    streams = FileStreamReader(settings.chunk_size_bytes, settings.max_line_bytes)
    handlers: tuple[FormatHandler, ...] = (
        JsonlHandler(max_fields=settings.max_fields),
        CsvHandler(max_fields=settings.max_fields),
        LogHandler(),
        *extra_handlers,
    )
    return DataProfiler(
        config=settings,
        streams=streams,
        encoding_detector=StdlibEncodingDetector(),
        content_guard=BinaryContentGuard(),
        format_detector=FormatDetector(handlers, streams, settings),
        record_estimator=RecordEstimator(streams, settings),
        memory_probe=memory_probe or ProcessMemoryProbe(),
        logger=logger or logging.getLogger(PROFILER_LOGGER_NAME),
    )


def create_chunking_engine(
    config: ChunkingConfig | None = None,
    *,
    memory_provider: AvailableMemoryProvider | None = None,
    logger: logging.Logger | None = None,
) -> ChunkingEngine:
    """Build a ``ChunkingEngine`` that reads live system memory; ``memory_provider`` overrides that."""
    settings = config or ChunkingConfig()
    return ChunkingEngine(
        config=settings,
        memory_provider=memory_provider or SystemMemoryProbe(),
        boundary_locator=NewlineBoundaryLocator(settings.boundary_block_bytes),
        logger=logger or logging.getLogger(CHUNKING_LOGGER_NAME),
    )


def create_orchestrator(
    *,
    output_path: str | os.PathLike[str],
    state: ChunkStateStore,
    strategy: ExtractionStrategy | None = None,
    scratch_dir: str | os.PathLike[str] = ".scratch",
    allowed_roots: Sequence[Path] | None = None,
    profiler_config: ProfilerConfig | None = None,
    chunking_config: ChunkingConfig | None = None,
    orchestrator_config: OrchestratorConfig | None = None,
    memory_provider: AvailableMemoryProvider | None = None,
    stream_reader: StreamReader | None = None,
    csv_formula_guard: bool = False,
    logger: logging.Logger | None = None,
) -> MiningOrchestrator:
    """Build the full mining pipeline.

    The output suffix (``.csv``, ``.jsonl``, ``.ndjson``) selects the format. The caller owns
    ``state``'s lifecycle. ``scratch_dir`` must lie inside an allowed root and be dedicated
    to one run: stale-file cleanup removes every ``chunk_<n>.tmp`` in it.
    """
    strategy = strategy or RegexExtractor.emails()
    profiler = create_profiler(profiler_config)
    settings = profiler_config or ProfilerConfig()
    streams = stream_reader or FileStreamReader(settings.chunk_size_bytes, settings.max_line_bytes)
    scratch = LocalScratchStore(scratch_dir, allowed_roots=allowed_roots)
    formatter: RecordFormatter
    suffix = Path(output_path).suffix.lower()
    if suffix == ".csv":
        formatter = CsvFormatter(strategy.fields, formula_guard=csv_formula_guard)
    elif suffix in (".jsonl", ".ndjson"):
        formatter = JsonlFormatter(strategy.fields)
    else:
        raise InvalidConfigurationException("the output file must end in .csv, .jsonl or .ndjson")
    log = logger or logging.getLogger(MINING_LOGGER_NAME)
    return MiningOrchestrator(
        profiler=profiler,
        chunking_engine=create_chunking_engine(chunking_config, memory_provider=memory_provider),
        state=state,
        worker=MinerWorker(strategy=strategy, formatter=formatter, streams=streams, scratch=scratch),
        aggregator=ResultAggregator(
            sink=LocalOutputSink(output_path),
            scratch=scratch,
            state=state,
            formatter=formatter,
            logger=log,
        ),
        scratch=scratch,
        config=orchestrator_config or OrchestratorConfig(),
        logger=log,
    )


@dataclass(frozen=True, slots=True)
class RunLayout:
    """State, scratch and lock paths of one run, under ``<workspace>/.scratch``.

    Names derive from a hash of the resolved source and output paths: the same job finds its
    earlier progress and resumes, different jobs never collide.
    """

    scratch_root: Path
    state_path: Path
    scratch_dir: Path
    lock_path: Path


def run_layout(workspace: Path, source: Path, output: Path) -> RunLayout:
    """Derive the run's paths for a (source, output) pair."""
    key = hashlib.sha256(
        "\0".join(os.path.normcase(str(path.resolve())) for path in (source, output)).encode()
    ).hexdigest()[:16]
    root = confined_child(workspace, ".scratch")
    return RunLayout(
        root, root / f"mining-{key}.sqlite3", root / f"mining-{key}", root / f"mining-{key}.lock"
    )


def run_mining(
    source: str | os.PathLike[str],
    output: str | os.PathLike[str],
    *,
    workspace: str | os.PathLike[str] | None = None,
    pattern: str | None = None,
    fields: Sequence[str] | None = None,
    overwrite: bool = False,
    csv_formula_guard: bool = False,
    logger: logging.Logger | None = None,
    on_progress: Callable[[MiningProgress], None] | None = None,
    chunking_config: ChunkingConfig | None = None,
    memory_provider: AvailableMemoryProvider | None = None,
) -> MiningSummary:
    """Run or resume one mining job; shared by the ``mine`` command and the MCP tool.

    ``workspace`` (default: current directory) holds the ``.scratch`` folder. With
    ``overwrite``, earlier progress is discarded and the output replaced; without it an
    interrupted job resumes and an unrelated existing output file is refused. Raises
    ``JobLockedException`` if another process runs the same job.
    """
    source_path = Path(source)
    output_path = Path(output)
    layout = run_layout(Path(workspace) if workspace is not None else Path.cwd(), source_path, output_path)
    ensure_private_directory(layout.scratch_root)
    strategy = RegexExtractor(pattern, fields) if pattern is not None else RegexExtractor.emails()
    roots = [layout.scratch_root]
    with JobLock(layout.lock_path):  # held first: --overwrite must not delete a live job's state
        if overwrite:
            for suffix in ("", "-wal", "-shm"):
                Path(f"{layout.state_path}{suffix}").unlink(missing_ok=True)
            shutil.rmtree(layout.scratch_dir, ignore_errors=True)
        with StateManager(layout.state_path, allowed_roots=roots) as state:
            orchestrator = create_orchestrator(
                output_path=output_path,
                state=state,
                strategy=strategy,
                scratch_dir=layout.scratch_dir,
                allowed_roots=roots,
                chunking_config=chunking_config,
                memory_provider=memory_provider,
                csv_formula_guard=csv_formula_guard,
                logger=logger,
            )
            return orchestrator.run(source_path, overwrite_output=overwrite, on_progress=on_progress)


def create_mcp_server(
    allowed_dirs: Sequence[Path],
    *,
    allow_custom_patterns: bool = False,
    logger: logging.Logger | None = None,
    mine_runner: MineRunner | None = None,
    profile_runner: ProfileFn | None = None,
    max_message_chars: int = DEFAULT_MAX_MESSAGE_CHARS,
) -> McpServer:
    """Build the MCP server exposing ``profile_dataset`` and ``mine_dataset``.

    Every file argument must stay inside ``allowed_dirs``; the first is the workspace.
    Caller-supplied regular expressions need ``allow_custom_patterns``.
    """
    policy = WorkspacePolicy(allowed_dirs)
    if allow_custom_patterns:
        (logger or logging.getLogger(MINING_LOGGER_NAME)).warning(
            "custom regular expressions are enabled: the MCP client can run patterns of its own "
            "choosing against your files, and a crafted pattern can hang this process"
        )
    tools = MiningTools(
        policy,
        profile=profile_runner or (lambda path: create_profiler().profile_as_dict(path)),
        mine=mine_runner or run_mining,
        allow_custom_patterns=allow_custom_patterns,
        logger=logger,
    )
    return McpServer(
        tools,
        identity=ServerIdentity(version=__version__),
        logger=logger,
        max_message_chars=max_message_chars,
    )
