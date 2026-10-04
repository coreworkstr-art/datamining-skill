"""The MCP tools ``profile_dataset`` and ``mine_dataset``, and all validation of model-supplied input.

Covers tool declarations, a small argument validator for exactly these schemas, and the
``WorkspacePolicy`` path confinement. JSON-RPC lives in ``mcp_server``; the profiler and mining
runner are injected (see docs/architecture.md, "MCP server").
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from datamining_skill.application.extraction_strategy import vet_pattern
from datamining_skill.application.orchestrator import MiningProgress, MiningSummary
from datamining_skill.application.support import describe_failure
from datamining_skill.domain.exceptions import DataMiningException, InvalidConfigurationException
from datamining_skill.infrastructure.paths import check_path_text, resolve_within
from datamining_skill.infrastructure.permissions import ensure_private_directory

TOOL_PROFILE = "profile_dataset"
TOOL_MINE = "mine_dataset"

MAX_PATH_CHARS = 4096
MAX_PATTERN_CHARS = 512
MAX_FIELDS = 32
_FIELD_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_ \-]{0,63}")

ProgressFn = Callable[[int, int, str], None]
ProfileFn = Callable[[Path], dict[str, Any]]


class MineRunner(Protocol):
    """Runs or resumes a mining job; ``bootstrap.run_mining`` satisfies this."""

    def __call__(
        self,
        source: Path,
        output: Path,
        *,
        workspace: Path,
        pattern: str | None,
        fields: Sequence[str] | None,
        overwrite: bool,
        csv_formula_guard: bool,
        on_progress: Callable[[MiningProgress], None] | None,
    ) -> MiningSummary: ...


class UnknownToolError(Exception):
    """The requested tool name is not declared by this server."""

    def __init__(self, name: str) -> None:
        super().__init__(f"Unknown tool: {name}")
        self.name = name


class ToolInputError(Exception):
    """Unacceptable arguments; reported to the model so it can correct them."""


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """One entry of ``tools/list``."""

    name: str
    title: str
    description: str
    input_schema: dict[str, Any]
    annotations: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": self.annotations,
        }


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """Structured data on success, a message on failure."""

    data: dict[str, Any] | None = None
    error: str | None = None

    @property
    def is_error(self) -> bool:
        return self.error is not None


class WorkspacePolicy:
    """Confines every model-supplied path to the allowed directories.

    A path is accepted only if, once ``..`` and symbolic links are resolved, it lies strictly
    inside an allowed directory. Relative paths start at the first one (the workspace). Errors
    never echo resolved paths. The check happens at call time, so it cannot stop a local
    process swapping in a symlink before the open (time-of-check/time-of-use).
    """

    def __init__(self, allowed_dirs: Sequence[Path]) -> None:
        if not allowed_dirs:
            raise InvalidConfigurationException("at least one allowed directory is required")
        roots: list[Path] = []
        for directory in allowed_dirs:
            resolved = Path(directory).expanduser().resolve()
            if not resolved.is_dir():
                raise InvalidConfigurationException(
                    f"allowed directory '{resolved.name}' does not exist or is not a directory"
                )
            roots.append(resolved)
        self._roots = tuple(roots)

    @property
    def roots(self) -> tuple[Path, ...]:
        return self._roots

    @property
    def workspace(self) -> Path:
        return self._roots[0]

    def resolve(self, raw: object, label: str) -> Path:
        """The resolved path, or ``ToolInputError``."""
        if not isinstance(raw, str) or not raw.strip():
            raise ToolInputError(f"'{label}' must be a non-empty string")
        if len(raw) > MAX_PATH_CHARS:
            raise ToolInputError(f"'{label}' is not a valid path")
        try:
            # text checks first: no filesystem (on Windows possibly network) access before that
            check_path_text(raw, f"'{label}'")
            candidate = Path(raw)
            if not candidate.is_absolute():
                candidate = self.workspace / candidate
            resolved = resolve_within(candidate, self._roots, label=f"'{label}'")
        except InvalidConfigurationException as exc:
            raise ToolInputError(str(exc)) from exc
        except (OSError, ValueError, RuntimeError) as exc:  # RuntimeError: symlink loop before 3.13
            raise ToolInputError(f"'{label}' is not a valid path") from exc
        if os.name == "nt" and ":" in resolved.name:
            # NTFS alternate data streams ("file.csv:hidden") would bypass file checks
            raise ToolInputError(f"'{label}' must not contain a stream specifier")
        return resolved

    def display(self, path: Path) -> str:
        """Workspace-relative POSIX path, stable across platforms."""
        try:
            return path.relative_to(self.workspace).as_posix()
        except ValueError:
            return path.name


def _profile_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "maxLength": MAX_PATH_CHARS,
                "description": (
                    "File to inspect (CSV, TSV, JSONL/NDJSON or log). Relative paths are "
                    "resolved against the workspace directory; the file must be inside an "
                    "allowed directory."
                ),
            }
        },
        "required": ["path"],
        "additionalProperties": False,
    }


def _mine_schema(allow_custom_patterns: bool) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "path": {
            "type": "string",
            "maxLength": MAX_PATH_CHARS,
            "description": "Source file to mine (read-only). Must be inside an allowed directory.",
        },
        "output_path": {
            "type": "string",
            "maxLength": MAX_PATH_CHARS,
            "description": (
                "Result file to create. Must end in .csv, .jsonl or .ndjson (this selects the "
                "format) and be inside an allowed directory."
            ),
        },
        "overwrite": {
            "type": "boolean",
            "default": False,
            "description": (
                "Start over: discard any earlier progress for this path/output_path pair and "
                "replace an existing output file. Leave false to resume an interrupted run."
            ),
        },
        "csv_formula_guard": {
            "type": "boolean",
            "default": False,
            "description": (
                "Prefix CSV cells that start with = + - @ with an apostrophe so spreadsheets "
                "do not execute them as formulas. Changes those values."
            ),
        },
    }
    if allow_custom_patterns:
        properties["pattern"] = {
            "type": "string",
            "maxLength": MAX_PATTERN_CHARS,
            "description": (
                "Python regular expression to extract instead of e-mail addresses. Each match "
                "(or the tuple of its capture groups) becomes one output row. Use bounded "
                "quantifiers such as {1,64}; patterns with nested unbounded quantifiers like "
                "(a+)+ are rejected because they can hang the machine."
            ),
        }
        properties["fields"] = {
            "type": "array",
            "items": {"type": "string", "maxLength": 64},
            "maxItems": MAX_FIELDS,
            "description": "Output column names, one per capture group of 'pattern'.",
        }
    return {
        "type": "object",
        "properties": properties,
        "required": ["path", "output_path"],
        "additionalProperties": False,
    }


def validate_arguments(schema: Mapping[str, Any], arguments: Mapping[str, Any]) -> list[str]:
    """Check ``arguments`` against the JSON Schema subset these tools use; return the problems found."""
    problems: list[str] = []
    properties: Mapping[str, Mapping[str, Any]] = schema.get("properties", {})
    for name in schema.get("required", []):
        if name not in arguments:
            problems.append(f"missing required argument '{name}'")
    for name, value in arguments.items():
        spec = properties.get(name)
        if spec is None:
            allowed = ", ".join(sorted(properties))
            problems.append(f"unknown argument '{name}' (allowed: {allowed})")
            continue
        problem = _check_value(name, spec, value)
        if problem:
            problems.append(problem)
    return problems


def _check_value(name: str, spec: Mapping[str, Any], value: Any) -> str | None:
    kind = spec.get("type")
    if kind == "string":
        if not isinstance(value, str):
            return f"'{name}' must be a string"
        if len(value) > spec.get("maxLength", len(value)):
            return f"'{name}' is longer than {spec['maxLength']} characters"
        if len(value) < spec.get("minLength", 0):
            return f"'{name}' is shorter than {spec['minLength']} characters"
    elif kind == "boolean":
        if not isinstance(value, bool):
            return f"'{name}' must be true or false"
    elif kind == "array":
        if not isinstance(value, list):
            return f"'{name}' must be an array"
        if len(value) > spec.get("maxItems", len(value)):
            return f"'{name}' has more than {spec['maxItems']} items"
        item_spec = spec.get("items", {})
        for index, item in enumerate(value):
            problem = _check_value(f"{name}[{index}]", item_spec, item)
            if problem:
                return problem
    return None


class MiningTools:
    """Declares and executes the MCP tools.

    ``pattern``/``fields`` are offered only with ``allow_custom_patterns``: a caller-supplied
    regular expression can backtrack catastrophically and stall the host, while the built-in
    e-mail extractor is safe by construction. ``logger`` receives failures that are not the
    caller's fault.
    """

    def __init__(
        self,
        policy: WorkspacePolicy,
        *,
        profile: ProfileFn,
        mine: MineRunner,
        allow_custom_patterns: bool = False,
        logger: logging.Logger | None = None,
    ) -> None:
        self._policy = policy
        self._profile = profile
        self._mine = mine
        self._allow_custom_patterns = allow_custom_patterns
        self._logger = logger or logging.getLogger(__name__)
        self._definitions = (
            ToolDefinition(
                name=TOOL_PROFILE,
                title="Profile a data file",
                description=(
                    "Inspect a local data file (CSV, TSV, JSONL/NDJSON or log) without reading "
                    "it fully into memory. Returns the size, encoding, detected format, column "
                    "names or JSON keys, and an estimated record count. Runs entirely on this "
                    "machine in constant memory, so it is safe for very large files. Use it "
                    "before mine_dataset to understand a file."
                ),
                input_schema=_profile_schema(),
                annotations={
                    "title": "Profile a data file",
                    "readOnlyHint": True,
                    "destructiveHint": False,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
            ),
            ToolDefinition(
                name=TOOL_MINE,
                title="Mine a data file",
                description=(
                    "Extract data from a large local file into a CSV or JSONL result file, "
                    "processing it in memory-bounded chunks with crash-safe checkpoints. By "
                    "default it extracts e-mail addresses into a column named 'email'. If a "
                    "run is interrupted, calling again with the same path and output_path "
                    "resumes where it stopped and never duplicates results. Progress is "
                    "reported per chunk. All work is local; nothing is sent over the network."
                ),
                input_schema=_mine_schema(allow_custom_patterns),
                annotations={
                    "title": "Mine a data file",
                    "readOnlyHint": False,
                    "destructiveHint": True,  # overwrite=true can replace an existing result file
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
            ),
        )

    def definitions(self) -> list[ToolDefinition]:
        """Tool declarations, in a fixed order."""
        return list(self._definitions)

    def call(
        self, name: str, arguments: Mapping[str, Any], progress: ProgressFn | None = None
    ) -> ToolOutcome:
        """Validate and run a tool.

        Caller mistakes and library errors come back as an error outcome; only an undeclared
        ``name`` raises ``UnknownToolError``.
        """
        definition = next((d for d in self._definitions if d.name == name), None)
        if definition is None:
            raise UnknownToolError(name)
        try:
            self._check_arguments(definition, arguments)
            if name == TOOL_PROFILE:
                return self._run_profile(arguments)
            return self._run_mine(arguments, progress)
        except ToolInputError as exc:
            return ToolOutcome(error=str(exc))
        except DataMiningException as exc:
            return ToolOutcome(error=f"{type(exc).__name__}: {exc}")
        except OSError as exc:
            return ToolOutcome(error=f"file system error: {describe_failure(exc)}")
        except Exception as exc:  # noqa: BLE001 - a tool bug must not take the server down
            self._logger.exception("tool %s failed unexpectedly", name)
            return ToolOutcome(error=f"internal error ({type(exc).__name__}); see the server log")

    def _check_arguments(self, definition: ToolDefinition, arguments: Mapping[str, Any]) -> None:
        problems = validate_arguments(definition.input_schema, arguments)
        if not self._allow_custom_patterns and ({"pattern", "fields"} & arguments.keys()):
            problems = [
                "custom patterns are disabled on this server (start it with --allow-custom-patterns)"
            ]
        if problems:
            raise ToolInputError("invalid arguments: " + "; ".join(problems))

    def _run_profile(self, arguments: Mapping[str, Any]) -> ToolOutcome:
        source = self._existing_file(arguments["path"], "path")
        return ToolOutcome(data=self._profile(source))

    def _run_mine(self, arguments: Mapping[str, Any], progress: ProgressFn | None) -> ToolOutcome:
        source = self._existing_file(arguments["path"], "path")
        output = self._policy.resolve(arguments["output_path"], "output_path")
        pattern = arguments.get("pattern")
        fields = arguments.get("fields")
        if fields is not None:
            if pattern is None:
                raise ToolInputError("'fields' requires 'pattern'")
            invalid_names = [name for name in fields if not _FIELD_NAME.fullmatch(name)]
            if invalid_names:
                raise ToolInputError("'fields' entries must be simple names (letters, digits, _ - space)")
        if pattern is not None:
            try:
                vet_pattern(pattern)
            except InvalidConfigurationException as exc:
                raise ToolInputError(str(exc)) from exc
        try:
            ensure_private_directory(output.parent)  # already confined to an allowed directory
        except OSError as exc:
            raise ToolInputError("the output directory cannot be created") from exc

        def on_progress(update: MiningProgress) -> None:
            if progress is not None:
                progress(
                    update.chunks_done,
                    update.chunks_total,
                    f"{update.chunks_done}/{update.chunks_total} chunks, "
                    f"{update.records_written} records this run",
                )

        summary = self._mine(
            source,
            output,
            workspace=self._policy.workspace,
            pattern=pattern,
            fields=fields,
            overwrite=bool(arguments.get("overwrite", False)),
            csv_formula_guard=bool(arguments.get("csv_formula_guard", False)),
            on_progress=on_progress,
        )
        summary_data = summary.to_dict()
        summary_data["output_path"] = self._policy.display(output)
        summary_data["succeeded"] = summary.succeeded
        if not summary.succeeded:
            reason = f" First failure: {summary.first_error}." if summary.first_error else ""
            return ToolOutcome(
                error=(
                    f"{summary.chunks_failed} chunk(s) failed; the result file is incomplete.{reason} "
                    "Calling again retries them. Details: " + str(summary_data)
                )
            )
        return ToolOutcome(data=summary_data)

    def _existing_file(self, raw: object, label: str) -> Path:
        path = self._policy.resolve(raw, label)
        if not path.is_file():
            raise ToolInputError(f"'{label}' does not name an existing regular file")
        return path
