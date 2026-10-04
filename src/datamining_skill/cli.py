"""Command-line interface: ``profile``, ``mine`` and ``mcp`` (see README.md for usage).

``profile`` and ``mine`` write JSON to stdout and log events to stderr; ``mcp`` keeps stdout
for the protocol alone.

Exit status: 0 success, 1 internal error, 2 unsupported format, 3 unavailable source, bad
configuration, file-system or other library error, 4 mining finished with failed chunks,
130 interrupted, 141 output pipe closed. A failure prints one ``error:`` line; ``--debug``
adds the traceback.
"""

from __future__ import annotations

import argparse
import errno
import json
import logging
import os
import sys
import traceback
from collections.abc import Sequence
from pathlib import Path

from datamining_skill.application.config import ProfilerConfig
from datamining_skill.bootstrap import create_mcp_server, create_profiler, run_mining
from datamining_skill.domain.exceptions import (
    DataMiningException,
    InvalidConfigurationException,
    UnsupportedDataFormatException,
    printable,
)
from datamining_skill.infrastructure.config_loader import load_config
from datamining_skill.infrastructure.logging import configure_json_logging
from datamining_skill.infrastructure.mcp_server import serve_stdio

ALLOWED_DIRS_ENV = "DATAMINING_SKILL_ALLOWED_DIRS"
_LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="datamining-skill",
        description="Local-only, streaming data mining toolkit.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    profile = commands.add_parser("profile", help="profile a CSV, JSONL or log file")
    profile.add_argument("path", help="file to profile (read-only, never copied)")
    profile.add_argument("--config", help="TOML file with a [profiler] table")
    _add_logging_options(profile, default_level="INFO")

    mine = commands.add_parser(
        "mine",
        help="mine a file into a CSV/JSONL result (resumable, crash-safe)",
        description=(
            "Extract data from SOURCE into OUTPUT (.csv, .jsonl or .ndjson), in "
            "memory-bounded chunks with crash-safe checkpoints. By default extracts "
            "e-mail addresses. Re-running an interrupted job resumes it."
        ),
    )
    mine.add_argument("source", help="file to mine (read-only)")
    mine.add_argument("output", help="result file to write (.csv, .jsonl or .ndjson)")
    mine.add_argument(
        "--workspace",
        help="directory whose .scratch folder holds state and scratch files (default: current)",
    )
    mine.add_argument("--pattern", help="regular expression to extract instead of e-mail addresses")
    mine.add_argument("--fields", help="comma-separated output column names for the pattern's groups")
    mine.add_argument(
        "--overwrite",
        action="store_true",
        help="discard earlier progress and replace an existing output file",
    )
    mine.add_argument(
        "--csv-formula-guard",
        action="store_true",
        help="prefix CSV cells starting with = + - @ so spreadsheets do not run them",
    )
    _add_logging_options(mine, default_level="INFO")

    mcp = commands.add_parser(
        "mcp",
        help="run the Model Context Protocol server on stdio",
        description=(
            "Run an MCP server exposing profile_dataset and mine_dataset over stdio. "
            "File arguments are restricted to the allowed directories."
        ),
    )
    mcp.add_argument(
        "--allow-dir",
        action="append",
        default=[],
        metavar="DIR",
        help=(
            "directory the tools may read and write (repeatable; the first is the workspace). "
            f"Default: ${ALLOWED_DIRS_ENV} (path-separator list), else the current directory"
        ),
    )
    mcp.add_argument(
        "--allow-custom-patterns",
        action="store_true",
        help="let tool callers supply regular expressions (off by default: ReDoS risk)",
    )
    _add_logging_options(mcp, default_level="WARNING")
    return parser


def _add_logging_options(parser: argparse.ArgumentParser, *, default_level: str) -> None:
    parser.add_argument(
        "--log-level",
        default=default_level,
        choices=_LOG_LEVELS,
        help=f"verbosity of structured logs on stderr (default: {default_level})",
    )
    parser.add_argument("--no-logs", action="store_true", help="suppress structured logs")
    parser.add_argument(
        "--debug", action="store_true", help="print the full traceback when a command fails"
    )


def _allowed_dirs(arguments: Sequence[str]) -> list[Path]:
    directories = [Path(item) for item in arguments]
    if not directories:
        configured = os.environ.get(ALLOWED_DIRS_ENV, "")
        directories = [Path(item) for item in configured.split(os.pathsep) if item]
    if not directories:
        cwd = Path.cwd().resolve()
        if cwd.parent == cwd:
            raise InvalidConfigurationException(
                "refusing to use the filesystem root as the workspace; pass --allow-dir"
            )
        directories = [cwd]
    return directories


def _run_profile(args: argparse.Namespace) -> int:
    config = load_config(args.config) if args.config else ProfilerConfig()
    json.dump(create_profiler(config).profile_as_dict(args.path), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def _run_mine(args: argparse.Namespace) -> int:
    fields = [name.strip() for name in args.fields.split(",")] if args.fields else None
    summary = run_mining(
        args.source,
        args.output,
        workspace=args.workspace,
        pattern=args.pattern,
        fields=fields,
        overwrite=args.overwrite,
        csv_formula_guard=args.csv_formula_guard,
    )
    json.dump(summary.to_dict(), sys.stdout, indent=2)
    sys.stdout.write("\n")
    if not summary.succeeded:
        reason = f" First failure: {summary.first_error}." if summary.first_error else ""
        print(
            f"error: {summary.chunks_failed} chunk(s) failed; the result is incomplete.{reason} "
            "Run the same command again to retry them.",
            file=sys.stderr,
        )
        return 4
    return 0


CUSTOM_PATTERN_WARNING = (
    "WARNING: --allow-custom-patterns lets the MCP client run regular expressions of its own "
    "choosing against your files. A crafted pattern can hang this process; only enable this "
    "for clients you trust."
)


def _run_mcp(args: argparse.Namespace) -> int:
    if args.allow_custom_patterns:
        print(CUSTOM_PATTERN_WARNING, file=sys.stderr)  # stderr: stdout is the protocol channel
    server = create_mcp_server(
        _allowed_dirs(args.allow_dir), allow_custom_patterns=args.allow_custom_patterns
    )
    try:
        serve_stdio(server)
    except OSError as exc:
        if not _is_closed_pipe(exc):
            raise
    return 0  # the client closed the pipe or stdin ended: a normal shutdown


EXIT_INTERNAL_ERROR = 1
EXIT_UNSUPPORTED = 2
EXIT_FAILURE = 3
EXIT_INTERRUPTED = 130
EXIT_PIPE_CLOSED = 141


def _is_closed_pipe(error: OSError) -> bool:
    # Windows reports a write to a closed pipe as EINVAL, not EPIPE
    return isinstance(error, BrokenPipeError) or error.errno in (errno.EPIPE, errno.EINVAL)


def _describe_os_error(error: OSError) -> str:
    reason = printable(error.strerror or type(error).__name__)
    if error.filename:
        return f"{reason} ({printable(Path(str(error.filename)).name)})"
    return reason


def _fail(message: str, code: int, debug: bool) -> int:
    print(f"error: {message}", file=sys.stderr)
    if debug:
        traceback.print_exc()
    return code


def _discard_stdout() -> None:
    """Point stdout at the null device so the interpreter's exit flush cannot fail again."""
    try:
        null_device = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(null_device, sys.stdout.fileno())
        finally:
            os.close(null_device)
    except (OSError, ValueError):
        pass


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if not args.no_logs:
        configure_json_logging(getattr(logging, args.log_level), stream=sys.stderr)

    handlers = {"profile": _run_profile, "mine": _run_mine, "mcp": _run_mcp}
    try:
        code = handlers[args.command](args)
        sys.stdout.flush()
        return code
    except UnsupportedDataFormatException as exc:
        return _fail(str(exc), EXIT_UNSUPPORTED, args.debug)
    except DataMiningException as exc:
        return _fail(str(exc), EXIT_FAILURE, args.debug)
    except OSError as exc:
        if _is_closed_pipe(exc):
            _discard_stdout()
            return EXIT_PIPE_CLOSED
        return _fail(_describe_os_error(exc), EXIT_FAILURE, args.debug)
    except UnicodeDecodeError:
        return _fail("a file is not valid UTF-8 text", EXIT_FAILURE, args.debug)
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    except Exception as exc:  # noqa: BLE001 - last resort: never show a raw traceback unasked
        return _fail(
            f"internal error ({type(exc).__name__}); rerun with --debug for the traceback",
            EXIT_INTERNAL_ERROR,
            args.debug,
        )


if __name__ == "__main__":
    sys.exit(main())
