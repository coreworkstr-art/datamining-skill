# Changelog

All notable changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

- **Concurrent writers.** Two processes mining the same job could silently lose records. `run_mining`
  (and so the CLI and the MCP tool) now holds a cross-process lock per job; a second process is
  refused immediately with "already running", and `--overwrite` can no longer wipe a live job.
- **Raw tracebacks.** The CLI turns file-system errors, invalid UTF-8 and unexpected exceptions into a
  one-line `error:` message (exit codes 3 and 1); `--debug` shows the traceback. A closed stdout pipe
  exits quietly with 141.
- **Silent truncation.** A source that shrinks while it is being mined now fails the affected chunks
  with an explicit reason instead of dropping their records.
- **Opaque failures.** `MiningSummary.first_error` (also in the CLI and MCP messages) says why the first
  chunk failed, and UTF-16/32 sources are refused before any work starts.

### Changed

- Docstrings and comments were condensed and now point to `docs/architecture.md` for design detail;
  banner comments were removed and local variables renamed. No behaviour or public signature changed.
- Test and script fixtures use reserved `.test` domains and enterprise-style sample data; test names
  are shorter.

### Security

- Windows UNC, device and extended-length paths are now rejected from the path text, before any
  filesystem call. Previously, validating `\\host\share\...` made Windows open a network connection.
- MCP path arguments reject control characters, alternate data streams in any component, reserved
  device names (`NUL`, `CON.txt`, ...), names Windows silently rewrites (trailing dot or space) and
  macOS resource-fork suffixes; symlink loops are reported cleanly instead of as internal errors.
- A `.scratch` directory that is a symlink or junction leading outside the workspace is refused.
- New directories are created `0700` on POSIX, and Windows objects created by the package get a
  protected owner-only ACL (previously they inherited the parent's, which often allows every local
  user to read them).
- Custom MCP patterns that repeat a repeating group without an upper bound (`(a+)+`) are rejected,
  and enabling custom patterns prints a warning to stderr.
- Error messages escape non-printable characters in file names so they cannot rewrite a terminal.
- Added a security regression suite (path attacks, link escapes, ReDoS, network isolation, SQL
  construction, kill-9 database integrity, permissions) and a CI resource-leak gate.

## [0.1.0] - 2026-10-04

First public release.

### Added

- **Data Profiler.** Size, encoding, format (CSV/TSV, JSONL/NDJSON, logs), columns or keys and
  record-count estimate for files of any size, in constant memory.
- **Chunking Engine.** Splits a file into newline-aligned byte ranges sized from the RAM that is
  free now (`min(15% of free RAM, 512 MiB)`), by seeking only; refuses to plan when memory is
  critically low.
- **State Manager.** SQLite (WAL, `synchronous=NORMAL`) tracking of chunk status with atomic
  transitions, orphan recovery and a commit ledger.
- **Mining pipeline.** `MinerWorker`, pluggable `ExtractionStrategy` (with a bounded-quantifier
  e-mail `RegexExtractor`), `ResultAggregator` with idempotent TMP -> APPEND merging, and a
  resumable single-threaded `MiningOrchestrator`. Crash-tested at three crash points.
- **MCP server** (`datamining-skill mcp`) over stdio with `profile_dataset` and `mine_dataset`
  tools, progress notifications and workspace-confined paths. Speaks both the legacy
  (`initialize`, 2024-11-05 to 2025-11-25) and the stateless 2026-07-28 protocol revisions,
  using only the standard library.
- **CLI:** `profile`, `mine` and `mcp` commands.
- **Quality gates:** `mypy --strict`, a test suite exercising crash recovery, memory bounds and
  path security, and a GitHub Actions matrix (Linux, macOS, Windows; Python 3.11-3.14).

### Security

- Zero runtime dependencies; no network access.
- All run artefacts are confined to `.scratch/` or `data/`; files are created owner-only on POSIX.
- Custom regular expressions are available to MCP callers only when the operator opts in.
