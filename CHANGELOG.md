# Changelog

All notable changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.5.1] - 2026-11-06

The Claude Code plugin now connects on Windows machines with Smart App Control. The package itself
is unchanged.

### Fixed

- **The Claude Code plugin did not connect on Windows with Smart App Control.** uvx makes an `.exe`
  launcher for the command it runs, and the policy blocked it (`Failed to spawn: datamining-skill`,
  `os error 4551`), so the server never started. The plugin now runs `python -m datamining_skill`,
  which needs no launcher. If you start the server by hand and meet the error, the troubleshooting
  page shows the same way round.

## [0.5.0] - 2026-11-06

This release makes the tool accept the files people actually bring: plain text, address lists, mail
and web pages are read instead of refused, the first address of a list without a header row is
kept, Turkish text is read correctly, and a quality check answers before a client gives up on it.

### Fixed

- **A list without a header row lost its first address.** A table whose first row held short names
  was taken to have a header, and a first row such as `ada@example.com,Ada` or
  `Lovelace, Ada <ada@example.com>` fits that description, so its address was left out of the
  result. A first row with an `@` in it is data now.
- **`--lowercase` left a stray dot on the Turkish dotted capital I.** `str.lower()` turns it into an
  i followed by a combining dot, so one address written in capitals and in lower case came out as
  two. It now gives a plain i.
- **Windows-1254 text was read as Windows-1252.** The Turkish letters g-breve, dotted and dotless i
  and s-cedilla came out as Icelandic letters, in field names and in addresses that contain them. A
  file that is not UTF-8 and holds those bytes without the accented vowels of the Western languages
  is now read as cp1254.
- **Amounts with a decimal comma were reported as type drift.** In a semicolon-delimited export
  (the usual Turkish or European layout) a column holding `12,50`, `7` and `1.234,56` was called a
  mix of text and numbers and got no statistics. When the delimiter is not a comma, a decimal comma
  and dots between thousands now make a number.
- **`check_quality` could outlast a client's patience.** A million rows of twelve columns took a
  minute, about where many clients give up on a tool call, and the server answers one request at a
  time, so every call after it waited too. The MCP tool now stops after `max_seconds` (20 by
  default, 1 to 600) and reports on what it read; the command line has an optional `--max-seconds`.

### Changed

- **Text with no recognised structure is read as plain lines instead of being refused.** A list of
  addresses, a web page, an `.eml` or `.vcf` file, prose and a log with a layout of its own all
  ended in "content matches no supported format", with no way to mine them. They are now profiled
  as `text` (no columns) and mined line by line. An empty file, a file of only blank lines or mostly
  control characters, and binary content are still refused, and `check_quality` still covers CSV,
  TSV and JSON Lines only ("this file looks like text" for the rest).
- README: the first commands work without a clone, the plugin's need for uv is stated, and the new
  behaviour is described.

## [0.4.1] - 2026-11-06

The package's code is the same as in 0.4.0. This release corrects the documentation that comes with
it.

### Fixed

- **README table.** The exit-code row for 141 had an unescaped pipe inside a code span, so GitHub cut
  the row off in the middle of the sentence.
- **README figures.** A quality check of a million CSV rows with six columns takes about 30 seconds
  (the README still said 8, from before the streaming statistics) and half a million JSON Lines
  records about 20. The README and `docs/troubleshooting.md` no longer call mining single-threaded
  without saying that the library can use `max_workers` for little gain (at most about 1.2 times).
  The distinct counts, number statistics and sampled previews are described in the README as well.

### Added

- **An overview diagram** at the top of the README (`docs/images/overview.webp`), included in the
  source distribution.

### Changed

- Tests and CI only: the timing check that shows `(a|aa)+` is exponential takes the shortest of
  several short runs, so a busy machine cannot fail it; CodeQL leaves `tests/` out of its scan,
  because the tests hold regular expressions that are exponential on purpose, and still scans
  `src/` and `scripts/`.

## [0.4.0] - 2026-11-05

### Security

- **Masking now covers IPv6 addresses and secrets.** IPv6 addresses (full, with `::`, with an IPv4
  tail) become `[IP]`; times, dates and `std::vector` are not mistaken for them. JSON Web Tokens,
  AWS access key ids, GitHub, OpenAI/Anthropic and Slack tokens become `[SECRET]`, and so does the
  value after `Bearer` when it is at least 20 characters, mixes two kinds of character and has a
  Shannon entropy of 3.8 bits per character or more (`Bearer password` stays readable). Every
  pattern is bounded and starts at a token boundary; megabyte-long hostile lines are masked in
  under two seconds in the tests. Still best effort, not anonymisation.
- **Custom patterns are vetted more strictly** (only offered with `--allow-custom-patterns`). A group
  that holds an unbounded repeat is now refused when it is repeated at all, so `(a+){2}` and
  `(a+){2,64}` join `(a+)+`, and so is a repeated group whose alternatives cannot be told apart by
  their first character, such as `(a|aa)+`, `(\w|a)*` or `(x|)+`. A bounded repeat of a bounded one,
  like the `(\d{1,3}\.){3}` of an IPv4 address, still passes. Three or more unbounded repeats in a row
  that can match the same text with no fixed character between them (`.*.*.*x`, `\w+.*\w+@`,
  `.+[a-z]+.*!`) are refused as well; `^.*foo.*bar.*$` and `\d+\s+\d+\s+\d+` pass. Write
  `(?:\d{1,3}\.){3}` instead of `(?:\d+\.){3}`; the capture-group message now suggests that form.
- **Record hashes in the state file are keyed.** A unique job hashes records with BLAKE2b keyed by a
  random 32-byte secret of its own, kept in the state database and deleted with the hashes when the job
  ends, so tables of hashes computed beforehand are useless and two jobs cannot be compared. The key
  sits in the same file: it does not stop someone who can read the file from testing guessed values,
  so the state database stays as sensitive as the result. A job begun by an earlier build carries on
  with plain hashes so that it keeps finding its repeats.
- **SQLite `secure_delete` is on**, so rows deleted from the state database (discarded hashes, the
  secret) are overwritten with zeros instead of staying in the page.
- **A source edited in place is noticed.** The fingerprint of a source now includes a digest of 64 KiB
  from its start, middle and end (at most 192 KiB read), and a run is refused when it no longer matches
  the one the job began with, also when only the modification time changed: before, a job whose source
  had the same size resumed on whatever it found. The state database of an earlier build, which kept only
  size and time, still resumes. The fingerprint is a tripwire and not a checksum: a change outside
  those windows that keeps size and time is not seen.
- **The result file can no longer be redirected or swapped.** Every open of it refuses a symbolic
  link (beforehand on every platform and, on POSIX, through `O_NOFOLLOW` when opening), accepts only
  a regular file (a named pipe fails at once instead of blocking) and compares the file's identity
  with the one seen when the run began, so a link or another file put in its place between two chunks
  is refused and left untouched instead of being truncated or appended to.
- **Path text rejects more.** C1 control characters (including the 8-bit CSI `\x9b`), invisible
  format characters such as the bidirectional overrides, zero-width spaces and the byte-order mark,
  surrogates and private-use characters are refused before any filesystem access. The zero-width
  non-joiner and joiner (U+200C, U+200D), which Persian, Arabic and Indic names and emoji sequences
  need, stay allowed.
- **A zip archive that lists many entries is refused before it is read.** Opening one made
  `zipfile` load the whole central directory, so an archive listing millions of files exhausted memory
  before the "exactly one file" check. The end of the file is now read first, in constant memory, and
  an archive announcing more than 16 entries or a directory over 64 KiB (also through the ZIP64
  records) is refused with a plain message.

### Added

- **Streaming statistics in `check_quality`.** Every field reports `approx_distinct` and
  `uniqueness_ratio` (HyperLogLog, standard error about 6.5 %), and a numeric field with at least ten
  values reports `numeric_stats`: mean, standard deviation, skewness, excess kurtosis and estimated
  quartiles. They come from fixed-size one-pass accumulators (`application/stream_statistics.py`), no
  value is kept, and the exact minimum and maximum are deliberately not reported. A quality check is
  now several times slower (about 1 to 2 MiB/s); `max_records` bounds it.
- **Sampling in `preview_result`.** `sample` (MCP, `preview --sample`) draws the rows uniformly from
  the file by reservoir sampling instead of taking the first ones, in file order, from at most the
  first 64 MiB; `seed` makes it repeatable.

### Changed

- **The CSV field limit is no longer put back.** `csv.field_size_limit` is a setting of the whole
  process; the profiler and the quality scan each set it and restored the old value, so threads that
  overlapped undid each other's limit. It is now only raised (to 4 MiB), the quality scan checks its
  own limit on every row, and `preview_result` its own 128 KiB.
- **`CsvFormatter` is safe to share between threads**: the slow path (a cell with a comma, quote,
  line break, or empty) no longer reuses one buffer.
- **Record boundaries respect quotes.** When a chunk end falls in a CSV file with quoted values that
  contain line breaks, the boundary is moved to a newline outside quotes where one is in reach (judged
  within the block that is searched; with none, the last newline is used as before).
- **`OrchestratorConfig(max_workers=1..16)`** mines several chunks at once on threads (library use;
  default 1). Chunks are still merged and committed in order on the calling thread, so the output and
  the state ledger match a sequential run. Unique mining stays sequential.
## [0.3.0] - 2026-11-01

### Added

- **`quality` command and `check_quality` tool.** One pass over a CSV, TSV or JSON Lines file, in
  constant memory, reporting malformed records (too few or too many columns, a broken quote, a line
  that is not JSON or not an object, a line over 1 MiB) by kind with the first line numbers, how many
  values of each field are empty, null or missing, the value types found in each field, and schema
  drift: a field whose values change type, with the line where the other type first appears. It also
  says when the encoding is only a guess because the file is not valid UTF-8. Counts and line
  numbers only, never values. It stops after `--max-records` records (default 1,000,000) and says
  whether it read the whole file; a compressed source is checked on its first 64 MiB. About 8 MiB/s
  on a dense CSV and 15 MiB/s on JSON Lines.
- **Masking of what the assistant sees.** `preview_result` rows and the field names in
  `profile_dataset` and `check_quality` have e-mail addresses, IPv4 addresses, payment card numbers
  (Luhn), Turkish national identity numbers (check digits) and IBANs (mod 97) replaced by tags such as
  `[EMAIL]`, with `pii_masked` counting them. It is best effort and not anonymisation. The result file
  is never changed. `datamining-skill preview --mask` and `quality --mask` do the same on the command
  line; `mcp --unmasked-preview` turns it off for the server and prints a warning.
- **Audit log.** `datamining-skill mcp --audit-log FILE` appends one JSON line per tool call (time,
  tool, success, duration, workspace-relative paths, switches, result counts, error text; never
  contents, patterns or rows). The file must lie outside the allowed directories so that the assistant
  can neither read nor overwrite it.

### Changed

- **`preview_result` and the field names of `profile_dataset` are masked by default** (see above). A
  client that compared the returned rows with the mined values now sees tags instead; start the server
  with `--unmasked-preview` to get the old behaviour.

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
- The README opens in plainer words and no longer claims more than the tests show (for example
  "strictly" confined access, or a single record never lost in any failure). It gained a note on
  processing personal data and a statement that the project is not affiliated with the clients it
  names. Two stale details in the documentation were corrected (a broken link, the list of format
  handlers). The package, plugin and marketplace descriptions use the same plainer wording.
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
- `preview_result` no longer fails on a JSON Lines record nested deeper than the parser allows or on a
  CSV value over 128 KiB. A record nested more than 32 levels deep is shown as text (the same on
  every Python version, since some parse a hundred thousand levels), and a CSV preview stops
  before an oversized value.
- **macOS printed a warning on every run.** macOS cannot report free memory through `sysconf`, so
  each `mine` logged `chunking.memory_unavailable` at WARNING and broke the quiet default. The
  documented 512 MiB assumption is now logged at INFO.

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
- The temporary file of a source conversion is opened with `O_NOFOLLOW` and refused when it is a
  symbolic link, so a planted link cannot make a conversion overwrite another file.
- The skill tells the assistant to treat column names, keys and preview rows as data, never as
  instructions (prompt injection through a mined file).
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
