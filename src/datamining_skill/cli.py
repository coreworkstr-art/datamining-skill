"""Command-line interface: ``profile``, ``mine``, ``preview``, ``clean`` and ``mcp`` (see README.md).

``profile``, ``mine``, ``preview`` and ``clean`` write JSON to stdout and log events to stderr;
``mcp`` keeps stdout for the protocol alone.

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
from typing import TextIO

from datamining_skill._version import __version__
from datamining_skill.application.config import ProfilerConfig
from datamining_skill.application.miner_worker import JSON_ESCAPES_MODES
from datamining_skill.application.orchestrator import MiningProgress
from datamining_skill.bootstrap import create_mcp_server, profile_source, run_mining
from datamining_skill.domain.exceptions import (
    DataMiningException,
    InvalidConfigurationException,
    UnsupportedDataFormatException,
    printable,
)
from datamining_skill.infrastructure.cleanup import clean_workspace
from datamining_skill.infrastructure.config_loader import load_config
from datamining_skill.infrastructure.logging import configure_json_logging, disable_json_logging
from datamining_skill.infrastructure.mcp_server import serve_stdio
from datamining_skill.infrastructure.paths import check_path_text
from datamining_skill.infrastructure.result_preview import (
    DEFAULT_PREVIEW_ROWS,
    MAX_PREVIEW_ROWS,
    preview_result,
)
from datamining_skill.infrastructure.source_prep import DEFAULT_MAX_EXPANDED_BYTES

ALLOWED_DIRS_ENV = "DATAMINING_SKILL_ALLOWED_DIRS"
_LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"]
_RESULT_SUFFIXES = (".csv", ".jsonl", ".ndjson")

_MAIN_EXAMPLES = """\
examples:
  datamining-skill profile events.csv
  datamining-skill mine events.csv emails.csv --unique --lowercase
  datamining-skill preview emails.csv
  datamining-skill mcp --allow-dir ~/data

Everything runs on this machine; nothing is sent over a network.
"""
_MINE_EXAMPLES = """\
examples:
  datamining-skill mine export.csv emails.csv --unique --lowercase
  datamining-skill mine app.log.gz addresses.jsonl
  datamining-skill mine access.log hosts.csv --pattern "(?:\\d{1,3}\\.){3}\\d{1,3}" --fields ip

An interrupted run resumes when the same command is repeated.
"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="datamining-skill",
        description="Local-only, streaming data mining toolkit.",
        epilog=_MAIN_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="{profile,mine,preview,clean,mcp}")

    profile = commands.add_parser(
        "profile", help="profile a CSV, JSON, JSONL or log file (also gzip, bzip2, xz, zip)"
    )
    profile.add_argument("path", help="file to profile (read-only, never copied)")
    profile.add_argument("--config", help="TOML file with a [profiler] table")
    _add_logging_options(profile, default_level="WARNING")

    mine = commands.add_parser(
        "mine",
        help="mine a file into a CSV/JSONL result (resumable, crash-safe)",
        description=(
            "Extract data from SOURCE into OUTPUT (.csv, .jsonl or .ndjson), in "
            "memory-bounded chunks with crash-safe checkpoints. By default extracts "
            "e-mail addresses. Re-running an interrupted job resumes it; changing any "
            "setting starts a new job. Compressed (gzip, bzip2, xz, zip) and UTF-16 "
            "sources are converted to a temporary UTF-8 copy first."
        ),
        epilog=_MINE_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
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
    mine.add_argument("--lowercase", action="store_true", help="lower-case every extracted value")
    mine.add_argument(
        "--unique",
        action="store_true",
        help=(
            "write each record once, across the whole file and resumed runs "
            "(combine with --lowercase to ignore case)"
        ),
    )
    mine.add_argument(
        "--json-escapes",
        choices=JSON_ESCAPES_MODES,
        default="auto",
        help=(
            "decode JSON string escapes such as \\u0040 before searching: auto (JSON and "
            "JSON Lines sources with the built-in e-mail extraction), on or off"
        ),
    )
    mine.add_argument(
        "--max-expanded-gib",
        type=float,
        default=DEFAULT_MAX_EXPANDED_BYTES / 1024**3,
        metavar="GIB",
        help="refuse a compressed source that expands beyond this size (default: %(default)g)",
    )
    mine.add_argument(
        "--no-progress",
        action="store_true",
        help="do not show the progress line (it appears only when stderr is a terminal)",
    )
    _add_logging_options(mine, default_level="WARNING")

    preview = commands.add_parser(
        "preview",
        help="show the first records of a result file",
        description="Print the first records of a .csv, .jsonl or .ndjson result file as JSON.",
    )
    preview.add_argument("path", help="result file written by 'mine'")
    preview.add_argument(
        "--rows",
        type=int,
        default=DEFAULT_PREVIEW_ROWS,
        help=f"records to show, 1 to {MAX_PREVIEW_ROWS} (default: {DEFAULT_PREVIEW_ROWS})",
    )
    _add_logging_options(preview, default_level="WARNING")

    clean = commands.add_parser(
        "clean",
        help="remove the state and scratch files of finished jobs",
        description=(
            "Remove the state databases and scratch files that finished jobs leave under "
            "<workspace>/.scratch. Result files are never touched, and a job that is running "
            "is skipped. Repeating the call of a job removed here needs --overwrite, because its "
            "result file already exists."
        ),
    )
    clean.add_argument("--workspace", help="the workspace whose .scratch folder to clean (default: current)")
    clean.add_argument(
        "--all",
        action="store_true",
        help="also remove jobs that have not finished (they can no longer be resumed)",
    )
    clean.add_argument("--dry-run", action="store_true", help="only report what would be removed")
    _add_logging_options(clean, default_level="WARNING")

    mcp = commands.add_parser(
        "mcp",
        help="run the Model Context Protocol server on stdio",
        description=(
            "Run an MCP server exposing profile_dataset, mine_dataset, mining_status, "
            "cancel_mining and preview_result over stdio. File arguments are restricted to "
            "the allowed directories."
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


class ProgressLine:
    """A single self-overwriting progress line on a terminal."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self._width = 0

    def __call__(self, update: MiningProgress) -> None:
        text = f"mining: {update.chunks_done}/{update.chunks_total} chunks, {update.records_written:,} records"
        self._width = max(self._width, len(text))
        self._stream.write(f"\r{text}")
        self._stream.flush()

    def finish(self) -> None:
        """Erase the line so that the summary or an error starts on a clean one."""
        if self._width:
            self._stream.write("\r" + " " * self._width + "\r")
            self._stream.flush()
            self._width = 0


def _progress_line(args: argparse.Namespace) -> ProgressLine | None:
    if args.no_progress or not sys.stderr.isatty():
        return None
    return ProgressLine(sys.stderr)


def _run_profile(args: argparse.Namespace) -> int:
    config = load_config(args.config) if args.config else ProfilerConfig()
    json.dump(profile_source(args.path, config=config), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def _run_clean(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace) if args.workspace else Path.cwd()
    outcomes = clean_workspace(workspace, include_unfinished=args.all, dry_run=args.dry_run)
    removed = [outcome for outcome in outcomes if outcome.removed]
    report: dict[str, object] = {
        "dry_run": args.dry_run,
        "jobs": [outcome.to_dict() for outcome in outcomes],
        "removed_jobs": len(removed),
        "freed_bytes": sum(outcome.size_bytes for outcome in removed),
    }
    if args.dry_run:
        report["would_free_bytes"] = sum(o.size_bytes for o in outcomes if o.reason is None)
    json.dump(report, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def _run_preview(args: argparse.Namespace) -> int:
    check_path_text(args.path, "the result path")
    result = Path(args.path)
    if result.suffix.lower() not in _RESULT_SUFFIXES:
        raise InvalidConfigurationException("the result file must end in .csv, .jsonl or .ndjson")
    if not result.is_file():
        raise InvalidConfigurationException("the result path does not name an existing file")
    json.dump(preview_result(result, args.rows), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def _run_mine(args: argparse.Namespace) -> int:
    fields = [name.strip() for name in args.fields.split(",")] if args.fields else None
    progress = _progress_line(args)
    try:
        summary = run_mining(
            args.source,
            args.output,
            workspace=args.workspace,
            pattern=args.pattern,
            fields=fields,
            overwrite=args.overwrite,
            csv_formula_guard=args.csv_formula_guard,
            lowercase=args.lowercase,
            unique=args.unique,
            json_escapes=args.json_escapes,
            max_expanded_bytes=int(args.max_expanded_gib * 1024**3),
            on_progress=progress,
        )
    finally:
        if progress is not None:
            progress.finish()
    json.dump(summary.to_dict(), sys.stdout, indent=2)
    sys.stdout.write("\n")
    if summary.already_complete:
        print(
            "note: this job already finished and its result is unchanged; "
            "add --overwrite to run it again.",
            file=sys.stderr,
        )
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

    if args.no_logs:
        disable_json_logging()
    else:
        configure_json_logging(getattr(logging, args.log_level), stream=sys.stderr)

    handlers = {
        "profile": _run_profile,
        "mine": _run_mine,
        "preview": _run_preview,
        "clean": _run_clean,
        "mcp": _run_mcp,
    }
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
        if args.command == "mine":
            print("interrupted: run the same command again to resume.", file=sys.stderr)
        return EXIT_INTERRUPTED
    except Exception as exc:  # noqa: BLE001 - last resort: never show a raw traceback unasked
        return _fail(
            f"internal error ({type(exc).__name__}); rerun with --debug for the traceback",
            EXIT_INTERNAL_ERROR,
            args.debug,
        )


if __name__ == "__main__":
    sys.exit(main())
