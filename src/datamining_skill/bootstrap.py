"""Composition root: the only module that knows both the application and infrastructure layers."""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from datamining_skill._version import __version__
from datamining_skill.application.chunking_engine import ChunkingEngine
from datamining_skill.application.config import ChunkingConfig, OrchestratorConfig, ProfilerConfig
from datamining_skill.application.data_profiler import DataProfiler
from datamining_skill.application.extraction_strategy import ExtractionStrategy, RegexExtractor
from datamining_skill.application.format_detection import FormatDetector
from datamining_skill.application.miner_worker import MinerWorker
from datamining_skill.application.options import MiningOptions
from datamining_skill.application.orchestrator import MiningOrchestrator, MiningProgress, MiningSummary
from datamining_skill.application.record_estimation import RecordEstimator
from datamining_skill.application.record_formats import CsvFormatter, JsonlFormatter
from datamining_skill.application.result_aggregator import ResultAggregator
from datamining_skill.application.support import stat_regular_file
from datamining_skill.domain.exceptions import (
    InvalidConfigurationException,
    UnsupportedDataFormatException,
)
from datamining_skill.domain.models import format_size
from datamining_skill.domain.ports import (
    AvailableMemoryProvider,
    ChunkStateStore,
    FormatHandler,
    MemoryProbe,
    RecordFormatter,
    StreamReader,
    UniqueKeyStore,
)
from datamining_skill.infrastructure.boundaries import NewlineBoundaryLocator
from datamining_skill.infrastructure.encoding import StdlibEncodingDetector
from datamining_skill.infrastructure.guard import BinaryContentGuard
from datamining_skill.infrastructure.handlers import (
    CsvHandler,
    JsonDocumentHandler,
    JsonlHandler,
    LogHandler,
)
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
from datamining_skill.infrastructure.source_prep import (
    DEFAULT_MAX_EXPANDED_BYTES,
    compression_of,
    convert_to_utf8,
    needs_conversion,
)
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
        JsonDocumentHandler(max_fields=settings.max_fields),
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
    unique_keys: UniqueKeyStore | None = None,
    json_escapes: str = "off",
    logger: logging.Logger | None = None,
) -> MiningOrchestrator:
    """Build the full mining pipeline.

    The output suffix (``.csv``, ``.jsonl``, ``.ndjson``) selects the format. The caller owns
    ``state``'s lifecycle. ``scratch_dir`` must lie inside an allowed root and be dedicated
    to one run: stale-file cleanup removes every ``chunk_<n>.tmp`` in it. ``unique_keys``
    turns on unique mining (``StateManager`` implements it); ``json_escapes`` is ``"on"``,
    ``"off"`` or ``"auto"`` (see ``MinerWorker``).
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
        worker=MinerWorker(
            strategy=strategy,
            formatter=formatter,
            streams=streams,
            scratch=scratch,
            unique_keys=unique_keys,
            json_escapes=json_escapes,
        ),
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
        unique_keys=unique_keys,
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


def run_layout(workspace: Path, source: Path, output: Path, settings: str = "") -> RunLayout:
    """Derive the run's paths for a (source, output, settings) triple.

    ``settings`` identifies what the job extracts (``MiningOptions.fingerprint``): the same
    source and output with another pattern is another job, never a resumed one.
    """
    key = hashlib.sha256(
        "\0".join(
            [*(os.path.normcase(str(path.resolve())) for path in (source, output)), settings]
        ).encode()
    ).hexdigest()[:16]
    root = confined_child(workspace, ".scratch")
    return RunLayout(
        root, root / f"mining-{key}.sqlite3", root / f"mining-{key}", root / f"mining-{key}.lock"
    )


_PREPARED_NAME = "source.utf8"
PROFILE_SAMPLE_BYTES = 4 * 1024 * 1024


def run_mining(
    source: str | os.PathLike[str],
    output: str | os.PathLike[str],
    *,
    workspace: str | os.PathLike[str] | None = None,
    pattern: str | None = None,
    fields: Sequence[str] | None = None,
    overwrite: bool = False,
    csv_formula_guard: bool = False,
    lowercase: bool = False,
    unique: bool = False,
    json_escapes: str = "auto",
    max_expanded_bytes: int = DEFAULT_MAX_EXPANDED_BYTES,
    logger: logging.Logger | None = None,
    on_progress: Callable[[MiningProgress], None] | None = None,
    chunking_config: ChunkingConfig | None = None,
    memory_provider: AvailableMemoryProvider | None = None,
) -> MiningSummary:
    """Run or resume one mining job; shared by the ``mine`` command and the MCP tool.

    ``workspace`` (default: current directory) holds the ``.scratch`` folder. A job is
    identified by its source, its output and its settings, so changing the pattern or any
    other setting starts a new job. With ``overwrite``, earlier progress is discarded and the
    output replaced; without it an interrupted job resumes, a finished one reports
    ``already_complete`` and an unrelated existing output file is refused. Compressed
    (gzip, bzip2, xz, zip) and UTF-16/32 sources are converted to a UTF-8 copy in the
    scratch folder first, limited to ``max_expanded_bytes``. Raises ``JobLockedException`` if
    another process runs the same job.
    """
    started = time.perf_counter()
    source_path = Path(source)
    output_path = Path(output)
    options = MiningOptions.of(
        pattern=pattern,
        fields=fields,
        lowercase=lowercase,
        unique=unique,
        csv_formula_guard=csv_formula_guard,
        json_escapes=json_escapes,
    )
    strategy = options.strategy()
    stat_regular_file(source_path)
    if output_path.exists() and os.path.samefile(output_path, source_path):
        raise InvalidConfigurationException("the output file must not be the source file")
    layout = run_layout(
        Path(workspace) if workspace is not None else Path.cwd(),
        source_path,
        output_path,
        options.fingerprint(),
    )
    ensure_private_directory(layout.scratch_root)
    roots = [layout.scratch_root]
    with JobLock(layout.lock_path):  # held first: --overwrite must not delete a live job's state
        if overwrite:
            for suffix in ("", "-wal", "-shm"):
                Path(f"{layout.state_path}{suffix}").unlink(missing_ok=True)
            shutil.rmtree(layout.scratch_dir, ignore_errors=True)
        with StateManager(layout.state_path, allowed_roots=roots) as state:
            fingerprint = _source_fingerprint(source_path)
            finished = _finished_summary(state, output_path, fingerprint, started)
            if finished is not None:
                return finished
            mining_source = source_path
            transform: str | None = None
            if needs_conversion(source_path):
                ensure_private_directory(layout.scratch_dir)
                mining_source = layout.scratch_dir / _PREPARED_NAME
                transform = _prepare_source(
                    state, source_path, mining_source, fingerprint, max_expanded_bytes
                )
            state.set_meta("source", fingerprint)
            orchestrator = create_orchestrator(
                output_path=output_path,
                state=state,
                strategy=strategy,
                scratch_dir=layout.scratch_dir,
                allowed_roots=roots,
                chunking_config=chunking_config,
                memory_provider=memory_provider,
                csv_formula_guard=options.csv_formula_guard,
                unique_keys=state if options.unique else None,
                json_escapes=options.effective_json_escapes(),
                logger=logger,
            )
            summary = orchestrator.run(
                mining_source, overwrite_output=overwrite, on_progress=on_progress
            )
            if summary.succeeded:
                if mining_source != source_path:
                    mining_source.unlink(missing_ok=True)  # the copy is only needed while mining
                if options.unique:
                    state.release_keys()  # a finished job never registers keys again
            return replace(summary, source_transform=transform)


def _source_fingerprint(source: Path) -> str:
    info = source.stat()
    return f"{info.st_size}:{info.st_mtime_ns}"


def _prepare_source(
    state: StateManager, source: Path, prepared: Path, fingerprint: str, max_bytes: int
) -> str | None:
    """Convert ``source`` to ``prepared`` unless a copy made for this very file already exists."""
    if (
        prepared.is_file()
        and state.get_meta("source") == fingerprint
        and state.get_meta("prepared_size") == str(prepared.stat().st_size)
    ):
        return state.get_meta("transform") or None
    transform = convert_to_utf8(source, prepared, max_bytes=max_bytes)
    state.set_meta("prepared_size", str(prepared.stat().st_size))
    state.set_meta("transform", transform or "")
    return transform


def _finished_summary(
    state: StateManager, output: Path, fingerprint: str, started: float
) -> MiningSummary | None:
    """The summary of a job that already finished with this very source, else ``None``.

    Skips profiling and conversion for a repeated call. Anything that does not match (a
    changed source, a missing or altered output) takes the normal path, which reports it.
    """
    if not (state.is_initialized() and state.is_complete()):
        return None
    if state.get_meta("source") != fingerprint:
        return None
    try:
        size = output.stat().st_size
    except OSError:
        return None
    if size != state.committed_output_end():
        return None
    total = sum(state.summary().values())
    return MiningSummary(
        output_name=output.name,
        resumed=True,
        recovered_orphans=state.recovered_orphans,
        chunks_total=total,
        chunks_previously_completed=total,
        chunks_processed=0,
        chunks_failed=0,
        records_written=0,
        duration_seconds=time.perf_counter() - started,
        already_complete=True,
        source_transform=state.get_meta("transform") or None,
    )


def profile_source(
    path: str | os.PathLike[str],
    *,
    workspace: str | os.PathLike[str] | None = None,
    config: ProfilerConfig | None = None,
) -> dict[str, Any]:
    """Profile ``path`` as a dictionary, looking inside gzip, bzip2, xz and zip files.

    A compressed file is judged by a sample, its first ``PROFILE_SAMPLE_BYTES`` once
    decompressed, held briefly in the workspace's ``.scratch`` folder; its record count is
    therefore not estimated. Any other file is profiled in place.
    """
    source = Path(path)
    stat_regular_file(source)
    profiler = create_profiler(config)
    with source.open("rb") as handle:
        compressed = compression_of(handle.read(8)) is not None
    if not compressed:
        return profiler.profile_as_dict(source)

    root = confined_child(Path(workspace) if workspace is not None else Path.cwd(), ".scratch")
    ensure_private_directory(root)
    sample = root / f"profile-{os.urandom(6).hex()}.sample"
    try:
        transform = convert_to_utf8(source, sample, sample_bytes=PROFILE_SAMPLE_BYTES)
        try:
            result = profiler.profile_as_dict(sample)
        except UnsupportedDataFormatException as exc:
            raise UnsupportedDataFormatException(source.name, exc.reason) from exc
    finally:
        sample.unlink(missing_ok=True)
    size = source.stat().st_size
    result["file"].update(name=source.name, size_bytes=size, size_human=format_size(size))
    result["compression"] = transform
    result["sample"] = {
        "scope": f"first {format_size(PROFILE_SAMPLE_BYTES)} of the decompressed content",
        "bytes": result["records"]["sampled_bytes"],
    }
    result["records"].update(count=None, is_exact=False, method="not-estimated-compressed")
    return result


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
        profile=profile_runner
        or (lambda path: profile_source(path, workspace=policy.workspace)),
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
