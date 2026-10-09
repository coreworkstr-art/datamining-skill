# Changelog

All notable changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.2.0] - 2026-10-08

### Added

- **Clean address lists.** `--unique` writes each record once, across the whole file and across
  resumed runs (a hash ledger in the job's state database, so memory stays constant), and
  `--lowercase` lower-cases every value. Together they give one lower-case line per address.
  The MCP tool has `unique` and `lowercase` arguments; the summary reports `duplicates_skipped`.
- **More sources.** gzip, bzip2, xz and single-file zip archives, and UTF-16/32 text (what Excel
  calls "Unicode Text"), are converted on the fly to a private UTF-8 copy that is removed when
  the job finishes. Expansion is capped (`--max-expanded-gib`, default 64) and a nearly full disk
  stops the conversion. `profile` judges a compressed file by a sample.
- **JSON documents.** `.json` files (minified or pretty-printed, one line or many) are recognised
  as format `json`. JSON string escapes such as `@` are decoded before searching JSON and
  JSON Lines sources (`--json-escapes auto|on|off`), so addresses spelled that way are no longer
  missed.
- **International addresses.** The built-in extraction accepts letters and digits of any script
  (`müşteri@firma.com.tr`, `名前@例え.jp`, `x@y.рф`) and punycode domains. Before, such an address
  produced a truncated, wrong result such as `teri@firma.com.tr`.
- **`preview` command and `preview_result` tool** show the first records of a result file (at most
  256 KiB are read), so a result can be checked without opening it.
- **Background jobs for the MCP server.** `mine_dataset` with `wait=false` returns a job id at once;
  `mining_status` and `cancel_mining` follow it up. Clients that time out long calls can now mine
  files of any size.
- **`clean` command** removes the state and scratch files that finished jobs leave behind (never
  result files, never a running job). A finished unique job also gives its keys back at once.
- **Claude Code plugin and skill.** `.claude-plugin/` and `skills/datamining/SKILL.md` install the
  MCP server together with instructions on when and how to use it; see the README.
- **Progress line** on a terminal, `--version`, usage examples in `--help`, and a note when a
  finished job is repeated (`already_complete` in the summary).
- **Release engineering.** Tag-driven release workflow with PyPI trusted publishing (opt-in),
  GitHub release notes taken from this file, CodeQL, Dependabot, issue forms, `ruff`, actions pinned
  to commit hashes, and checks that the distributions, plugin metadata, documentation and CLI
  stay in step.

### Changed

- **A job is identified by its settings as well as its paths.** Changing the pattern, fields,
  `lowercase`, `unique` or `csv_formula_guard` starts a new job. Before, repeating a call with
  another pattern was answered "success, 0 records" with the old result left in place.
- **Very long lines are searched in full.** A line over the 1 MiB reader cap (a minified JSON
  document, a CSV cell with embedded data) is searched in overlapping windows instead of being
  skipped, with memory still bounded; `oversized_lines` counts them.
- `mine` logs at WARNING by default (it used to print every chunk event as JSON); use
  `--log-level INFO` for them. `profile` is quiet by default as well.
- Faster: the built-in extraction finds each `@` with a plain string search and runs the pattern only
  around it, lines are searched in blocks, and the CSV writer is bypassed for values that need no
  quoting. A log file with a rare address mines about ten times faster, a CSV with one on every
  row about one and a half times.
- The state database is schema 3 (a v2 database is upgraded in place).
- Docstrings and comments were condensed and point to `docs/architecture.md`; test and script
  fixtures use reserved `.test` domains and enterprise-style sample data.

### Fixed

- **Repeated capturing groups.** A pattern such as `(\d{1,3}\.){3}\d{1,3}` returned fragments
  (`0.`) without any warning; it is now rejected with the fix (`(?:...)`).
- **Large CSV cells.** A valid CSV with one cell over 128 KiB was refused as "no supported format";
  lines cut at the size cap no longer count against the column-count check, and the `csv`
  module's field limit is lifted for the duration of the check.
- **One-line files** such as a minified JSON array were mistaken for a one-row CSV.
- **Concurrent writers.** Two processes mining the same job could silently lose records. `run_mining`
  (and so the CLI and the MCP tool) now holds a cross-process lock per job; a second process is
  refused immediately with "already running", and `--overwrite` can no longer wipe a live job.
- **Raw tracebacks.** The CLI turns file-system errors, invalid UTF-8 and unexpected exceptions into a
  one-line `error:` message (exit codes 3 and 1); `--debug` shows the traceback. A closed stdout pipe
  exits quietly with 141.
- **Silent truncation.** A source that shrinks while it is being mined now fails the affected chunks
  with an explicit reason instead of dropping their records.
- **Opaque failures.** `MiningSummary.first_error` (also in the CLI and MCP messages) says why the first
  chunk failed.
- The record count of a CSV with multi-line quoted values is reported with its unit (`lines`) and a
  `multiline_records` flag.
- The CLI no longer refuses workspaces whose path exceeds 260 characters on Windows systems that
  have long paths enabled; a system that cannot open the path reports the operating system's error.
- Logging handlers no longer pile up when the command-line entry point is called repeatedly in
  one process.

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
- Converted copies of compressed or UTF-16 sources are created owner-only, are size-capped and
  never outlive a finished job.
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
