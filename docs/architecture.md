# Architecture

The project follows Clean Architecture: source dependencies point inward only.

```
infrastructure  ──▶  application  ──▶  domain
(adapters)           (use cases)       (models, exceptions, ports)
        ▲
   bootstrap.py / cli.py   (composition root and delivery mechanism)
```

## Layers

| Layer | Package | Responsibility | May import |
| --- | --- | --- | --- |
| Domain | `datamining_skill.domain` | Immutable value objects, the exception hierarchy and the `Protocol` ports. | Standard library |
| Application | `datamining_skill.application` | `DataProfiler`, `ChunkingEngine`, `MinerWorker`, `ExtractionStrategy`, `ResultAggregator`, `MiningOrchestrator`, record formatters, `FormatDetector`, `RecordEstimator`, and the config classes. Orchestration only; all file and OS access goes through ports. | Domain |
| Infrastructure | `datamining_skill.infrastructure` | Concrete adapters: file streaming, encoding detection, content guard, format handlers, newline boundary locator, system/process memory probes, SQLite `StateManager`, scratch store, output sink, JSON logging, TOML loader. | Domain, application config |
| Composition | `bootstrap.py`, `cli.py` | Wires adapters into the use case; command-line entry point. | All layers |

## Ports (`domain/ports.py`)

| Port | Default adapter |
| --- | --- |
| `StreamReader` | `FileStreamReader` (`lines`, `chunks`, strict `range_lines`) |
| `ScratchStore` | `LocalScratchStore` |
| `OutputSink` | `LocalOutputSink` |
| `RecordFormatter` | `CsvFormatter`, `JsonlFormatter` |
| `EncodingDetector` | `StdlibEncodingDetector` |
| `ContentGuard` | `BinaryContentGuard` |
| `FormatHandler` | `JsonlHandler`, `CsvHandler`, `LogHandler` |
| `MemoryProbe` | `ProcessMemoryProbe` |
| `AvailableMemoryProvider` | `SystemMemoryProbe` |
| `BoundaryLocator` | `NewlineBoundaryLocator` |
| `ChunkStateStore` | `StateManager` (SQLite) |

## SOLID mapping

- **Single responsibility** - detection, estimation, streaming, guarding, logging and
  orchestration live in separate classes.
- **Open/closed** - new formats are added by registering a `FormatHandler`; no
  existing code changes.
- **Liskov** - every handler honours the same contract: consume at most
  `max_records` lines and return `None` when the sample is not its format.
- **Interface segregation** - ports are small and role-specific.
- **Dependency inversion** - `DataProfiler` receives every collaborator through its
  constructor and depends only on ports.

## Profiling flow

```
stat()  ──▶ empty? ─ yes ─▶ UnsupportedDataFormatException
  │
  ▼
chunks(head)  ──▶  EncodingDetector  ──▶  ContentGuard (binary / compressed?)
  │
  ▼
FormatDetector: each FormatHandler analyses the head sample ──▶ best confidence wins
  │                                                       none ≥ minimum ─▶ UnsupportedDataFormatException
  ▼
RecordEstimator: exact scan (small) | stratified windows (large) | head window (UTF-16/32)
  │
  ▼
FileProfile  +  structured log events
```

## Memory model

Every allocation is bounded by a `ProfilerConfig` value:

| Buffer | Bound |
| --- | --- |
| Raw read chunk | `chunk_size_bytes` (64 KiB) |
| One line | `max_line_bytes` (1 MiB); the rest is skipped without buffering |
| Head sample | `head_sample_bytes` (1 MiB) plus at most one line |
| CSV sample list | 200 lines within the head sample |
| JSON key table | `max_fields` (1024) entries |
| Estimation windows | read one line at a time; counters only |

No structure grows with the number of records in the file, so space complexity is
O(1) in file size. Time is also independent of file size above
`exact_count_max_bytes`, because only `estimation_windows * window_bytes` are read.

All file parsing is expressed as generators (`yield`); the generators are closed
explicitly (`contextlib.closing`) so file handles are released promptly, which also
matters on Windows where open handles block deletion.

## Observability

Events are emitted on the `datamining_skill.profiler` logger as single-line JSON:

| Event | Level | Payload (`data`) |
| --- | --- | --- |
| `profile.started` | INFO | file name, size, RSS and peak RSS at start |
| `format.detected` | INFO | format, encoding, confidence, size |
| `profile.completed` | INFO | records, exactness, duration, start/end/delta/peak RSS |
| `profile.failed` | WARNING | error type, message, duration, RSS |

The package installs only a `NullHandler`; applications opt in with
`configure_json_logging()` (the CLI does so automatically).

## Chunking engine

`ChunkingEngine` turns a `FileProfile` into a sequence of `ChunkMetadata` byte
ranges that downstream stages can process one at a time. It computes **offsets
only**: chunk contents are never read during orchestration.

### Dynamic, memory-aware boundary calculation

```
available = AvailableMemoryProvider.available_bytes()      # live, at plan() time
available < critical_available_bytes (100 MiB)  ──▶  ResourceExhaustionError   (before any file access)

chunk_bytes = min( available x memory_fraction (0.15),  max_chunk_bytes (512 MiB) )
chunk_bytes < min_chunk_bytes (1 MiB)           ──▶  ResourceExhaustionError

start = 0
while start < file_size:
    if file_size - start <= chunk_bytes:   end = file_size                  # final chunk
    else:                                  end = last "\n" in [start, start + chunk_bytes) + 1
                                           none found ──▶ UnsupportedDataFormatException
    yield ChunkMetadata(id, start, end)
    start = end
```

Worked example: 2 GiB free RAM gives `min(0.15 x 2 GiB, 512 MiB) = 307.2 MiB`, so a 10 GiB
file is split into 34 chunks. With 64 GiB free the 512 MiB cap applies. A file no larger
than `chunk_bytes` yields exactly one chunk.

Design decisions:

- **Boundaries move backwards, never forwards.** The candidate end `start + chunk_bytes`
  is retreated to the nearest preceding newline, so `chunk_bytes` is a hard upper bound
  on every chunk rather than a target that records can overshoot.
- **Seeking only.** `NewlineBoundaryLocator` reads one 64 KiB block at a time, scanning
  backwards from the candidate end. A newline is normally found in the first block, so
  each boundary costs one small read, independent of chunk and file size. Planning
  allocation stays far below one chunk (about 200 KiB for a 10 GiB file in testing).
- **Eager checks, lazy plan.** `plan()` validates memory, file and encoding immediately
  and returns a generator; boundaries are then computed one at a time as the consumer
  iterates, so the plan for a very large file is never held in memory. The one condition
  discoverable only mid-walk (a record longer than the chunk) raises from the generator
  after the preceding chunks were delivered.
- **Fixed per plan.** Available memory is sampled once when planning starts. Chunk size
  is therefore constant within a plan (except the aligned remainder), which keeps the
  output deterministic and consumers simple.
- **Live file size.** Chunks tile the file's current size. If it differs from the
  profile's size, a `chunking.profile_stale` warning is logged.
- **No `psutil`.** Available memory comes from `GlobalMemoryStatusEx` (Windows) or
  `/proc/meminfo` `MemAvailable` (Linux), keeping the runtime dependency-free. Where the
  platform cannot report it, `fallback_available_bytes` (512 MiB) is assumed and a
  `chunking.memory_unavailable` warning is logged.

### Chunk contract

Ranges are half-open, `[start_byte, end_byte)`, and tile the file exactly. Every chunk
after the first begins immediately after a `\n`; every chunk except the last ends with
one. The last ends at end-of-file, which may lack a trailing newline. The first chunk
also contains any byte-order mark and the header row, so consumers of later chunks take
column names from the profile.

### Limitations

- A record is a physical line (as in the profiler). Quoted CSV fields or log entries that
  contain line breaks can be split across chunks.
- Lone-`\r` line endings are not recognised; `\r\n` works.
- UTF-16/32 cannot be partitioned by byte offset; such files are accepted only if they
  fit in one chunk. Convert to UTF-8 first.
- A single record at least as large as `chunk_bytes` makes the file unpartitionable.

### Chunking events

| Event | Level | Payload (`data`) |
| --- | --- | --- |
| `chunking.started` | INFO | file name, size, available memory, fraction, chunk size, expected chunk count |
| `chunking.completed` | INFO | chunk count, chunk size, duration |
| `chunking.failed` | WARNING | error type and message (low RAM, unpartitionable record) |
| `chunking.memory_unavailable` | WARNING | assumed available bytes |
| `chunking.profile_stale` | WARNING | profiled vs. current size |

## State manager

`StateManager` (`infrastructure/state_manager.py`, port `ChunkStateStore`) records the
lifecycle of every chunk in a local SQLite database so a mining run can be interrupted
at any instant and resumed without redoing finished work. It uses only the standard
library `sqlite3` module and stores **metadata only**: no dataset content ever enters
the database.

### Schema

```sql
CREATE TABLE chunks (
    chunk_id    INTEGER PRIMARY KEY,
    start_byte  INTEGER NOT NULL CHECK (start_byte >= 0),
    end_byte    INTEGER NOT NULL,
    status      TEXT    NOT NULL
                CHECK (status IN ('PENDING', 'IN_PROGRESS', 'COMPLETED', 'FAILED')),
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    updated_at  TEXT    NOT NULL,            -- ISO-8601, always UTC (+00:00)
    CHECK (end_byte > start_byte)
);
CREATE INDEX idx_chunks_status ON chunks (status, chunk_id);

CREATE TABLE chunk_commits (                     -- schema v2: the output commit ledger
    chunk_id   INTEGER PRIMARY KEY,
    output_end INTEGER NOT NULL CHECK (output_end >= 0)
);
```

`chunk_commits` records the length of the result file after each committed chunk. It is
written in the same transaction as the `COMPLETED` status (see
[Mining pipeline](#mining-pipeline-worker-aggregator-orchestrator)) and holds byte
offsets only.

`PRAGMA user_version` holds the schema version (currently 2); a version-1 database is
migrated in place on open (every DDL statement is `IF NOT EXISTS`, so existing progress
is kept) and a newer, unknown version is refused rather than guessed at. The definition
lives in `SCHEMA_DDL` in the module.

### Status lifecycle

```
            claim / mark_in_progress            mark_completed
 PENDING ───────────────────────────▶ IN_PROGRESS ───────────────▶ COMPLETED  (terminal)
    ▲                                   │      │
    │  orphan recovery (retry_count+1)  │      │ mark_failed (retry_count+1)
    ├───────────────────────────────────┘      ▼
    └──────────────── requeue_failed ───────  FAILED
```

Every other transition raises `InvalidStateTransitionException` and changes nothing.
`retry_count` counts attempts that did not complete: failures and attempts abandoned by
a crash. A chunk that repeatedly kills the worker (for example by exhausting memory)
therefore shows a rising count, which the caller can use to stop retrying it;
`requeue_failed(max_retries=N)` applies such a limit to FAILED chunks.

### Write-ahead logging and durability

The connection is configured on every open with `PRAGMA journal_mode=WAL` and
`PRAGMA synchronous=NORMAL`; opening fails if WAL cannot be enabled (for example on a
network share).

- **WAL.** Commits are appended to a separate `-wal` file instead of modifying the main
  database in place. A process killed mid-write therefore cannot leave the main file
  half-written; on the next open SQLite replays the complete transactions in the log and
  discards the partial one. Readers never block the writer.
- **`synchronous=NORMAL`.** In WAL mode the log is fsynced at checkpoints rather than on
  every commit. A process crash, `SIGINT`, `SIGTERM` or `SIGKILL` cannot lose a committed
  transaction (the data is in the OS page cache). A **power loss or operating-system
  crash** can lose the most recent commits, but never corrupts the database. A chunk may
  therefore reappear as IN_PROGRESS or PENDING after a power cut and be processed again,
  so **chunk processing must be idempotent**. Use `synchronous=FULL` if power-loss
  durability of each individual commit is required.
- **Clean close** folds the log back into the main file and removes the `-wal`/`-shm`
  side files.

### Atomicity

Connections run in autocommit mode with explicit `BEGIN IMMEDIATE ... COMMIT` blocks, and
any exception triggers `ROLLBACK`.

- **Compare-and-set transitions.** Each change is
  `UPDATE ... WHERE chunk_id = ? AND status = <expected>`. If the row is no longer in the
  expected status (a stale view, a second writer) zero rows match and nothing is
  modified, so a late writer cannot overwrite newer state.
- **Atomic claim.** `claim_next_pending()` selects the lowest PENDING chunk and marks it
  IN_PROGRESS inside one write transaction, so two connections never receive the same
  chunk. `next_pending()` is the read-only peek.
- **Atomic initialisation.** `initialize()` consumes a chunk generator inside one
  transaction: an interruption or error leaves the database empty, never half-filled.

### Orphan recovery and resuming

```
open StateManager
  ├─ PRAGMAs, schema check
  └─ recover_orphans():  UPDATE chunks SET status='PENDING', retry_count=retry_count+1
                         WHERE status='IN_PROGRESS'            (one transaction)
```

A chunk found IN_PROGRESS when the manager opens belongs to a process that died before
finishing it, so it is reverted to PENDING and picked up again, in order, by the next
`claim_next_pending()`. COMPLETED and FAILED chunks are never touched by recovery.

**Resume from the stored plan; do not re-plan.** The chunk size depends on the RAM free
when `ChunkingEngine.plan()` runs, so planning again after a restart can produce different
boundaries. `initialize()` therefore treats an existing plan as authoritative: an
identical plan is a no-op returning `False` (progress preserved), a different one raises
`StateStoreException`. The intended flow is:

```python
with StateManager("data/run-001.sqlite3") as state:
    if not state.is_initialized():                      # first run only
        state.initialize(engine.plan(path, profile))
    while (chunk := state.claim_next_pending()) is not None:
        process(path, chunk.start_byte, chunk.end_byte)  # idempotent
        state.mark_completed(chunk.chunk_id)
```

The manager is single-writer: opening it recovers orphans, which would take work from a
live worker sharing the database. Observers can open with `recover_orphans=False`.
Connections belong to the creating thread.

### Security and privacy

- **Location.** The database path must resolve inside `.scratch/` or `data/` under the
  working directory (configurable via `allowed_roots`). Paths outside them, including
  `..` traversal and system temporary folders, raise `InvalidConfigurationException`
  before anything is created. The file and its directory are created owner-only (`0600` /
  `0700`; a protected ACL on Windows).
- **Parameterised SQL only.** Every statement is a module-level constant and all values
  are bound with `?`. A test parses the module's AST to enforce that no executed SQL is
  built at runtime.
- **UTC only.** Timestamps come from `datetime.now(UTC)` (or an injected clock), are
  normalised to UTC, and a naive clock is refused.
- **Error text.** Exceptions and log events carry the file name, chunk ids and counts
  only, never paths, SQL or data.

### State events

| Event | Level | Payload (`data`) |
| --- | --- | --- |
| `state.opened` | INFO | file name, recovered orphans |
| `state.initialized` | INFO | file name, chunk count |
| `state.orphans_recovered` | WARNING | file name, chunk count |
| `state.requeued` | INFO | chunk count |
| `state.claimed`, `state.transition` | DEBUG | chunk id, new status |

### Validation

`python scripts/simulate_crash_resume.py` runs 100 dummy chunks in a worker process that is
killed with `os._exit` (no cleanup of any kind; `--crash-mode sys` uses `sys.exit`) while
chunk 50 is IN_PROGRESS, restarts it, and verifies that processing resumes at chunk 50 and
that rows 1-49 are byte-for-byte unchanged. It also runs inside the pytest suite.


## Mining pipeline: worker, aggregator, orchestrator

```
Profiler ─▶ ChunkingEngine ─▶ StateManager.initialize          (first run only)
                                    │
        ┌───────────────────────────┘      resume: reuse the stored plan
        ▼
   claim_next_pending() ─▶ MinerWorker ─▶ chunk_{id}.tmp ─▶ ResultAggregator ─▶ output.csv/jsonl
        ▲                  (read+write)   (scratch file)     (truncate + append)       │
        └────── mark_completed(chunk_id, output_end) ◀─────────────────────────────────┘
                (one transaction: status + commit ledger)
```

### Responsibilities

| Component | Does | Does not |
| --- | --- | --- |
| `MinerWorker` | Seeks to `start_byte`, reads line by line until exactly `end_byte`, passes each line to the strategy, writes the formatted records to `chunk_{id}.tmp`. | Interpret data, touch the output file, or know about state. |
| `ExtractionStrategy` | Pure function: one line in, zero or more records out (`RegexExtractor` is the reference implementation). | Perform I/O or keep per-file state. |
| `RecordFormatter` | Serialises a record to CSV or JSONL bytes. | Decide what is extracted. |
| `ResultAggregator` | Moves a finished scratch file onto the end of the output, idempotently. | Mark chunks complete. |
| `MiningOrchestrator` | The single-threaded loop: claim, mine, merge, commit; failure isolation; resume checks. | Read or write data itself. |

The worker reads through `StreamReader.range_lines`, which caps every read at the bytes
left in the range, so it stops *exactly* at `end_byte` even if a boundary were
misaligned. A chunk that does not start right after a newline is refused, and chunk 1
skips ahead to `FileProfile.data_offset` so a BOM and header row are never mined.

**Memory is O(1).** Lines come from a generator; each record is formatted and written
immediately; nothing accumulates. A test processes a 16 MiB chunk with under 1 MiB of
traced allocation. Lines longer than the reader's cap are skipped and counted, never
passed on truncated, since a cut line could yield a wrong match.

### File-level idempotency: TMP -> APPEND with a commit ledger

The hard part is that "append to the output" and "mark the chunk COMPLETED" are two
different durable writes, and a crash can fall between or inside them. Re-running a chunk
whose rows are already in the output would duplicate them. The design removes the problem
by making the append **repeatable**: the output's valid length is always known, and every
merge starts by cutting the file back to it.

**The ledger.** `mark_completed(chunk_id, output_end=N)` records "after this chunk the
output is exactly `N` bytes long" in the `chunk_commits` table, in the **same SQLite
transaction** that sets `COMPLETED`. So the status and the length can never disagree, and
the latest `N` is the exact size of the valid output.

**The merge** (`ResultAggregator.merge`), for a chunk whose worker finished:

1. `truncate(committed_length)` - discard anything past the last commit: a half-written
   append, or a whole chunk appended just before a crash.
2. Check the scratch file holds the bytes the worker reported.
3. Append the scratch file to the output and `fsync`.
4. Delete the scratch file.
5. Return the new length; the orchestrator calls `mark_completed(..., output_end=...)`.

At startup `prepare()` does step 1 as well (and, when nothing is committed yet, recreates
the output with just its header), so a restart repairs the file before anything else
happens.

**Why every crash point is safe.** After a restart the chunk is IN_PROGRESS, so orphan
recovery makes it PENDING and it is redone from the start.

| Crash point | State left behind | What the restart does |
| --- | --- | --- |
| While reading / writing the scratch file | Partial `chunk_N.tmp`; output untouched | Scratch files are cleared at start and `open_tmp` truncates; the chunk is mined again. |
| Mid-append | Output has a half-written tail past the last commit | `truncate(committed)` cuts the tail; the chunk is appended whole. |
| After the append, before `mark_completed` | Output contains the whole chunk, but it is not committed | `truncate(committed)` removes it; the re-run appends it once. |
| After `mark_completed` | Chunk COMPLETED and ledger updated; at most a stale scratch file | Nothing to redo; the stale scratch file is cleared. |

No record is lost (a chunk is only committed after its bytes are durable) and none is
written twice (uncommitted bytes are always removed before an append). Results are
appended in chunk order, so a normal or resumed run produces the same file an
uninterrupted run would.

`OutputIntegrityException` is raised, instead of guessing, when the output is missing or
shorter than the ledger says (it was deleted or replaced) or a scratch file is not the
size its worker reported.

### Orchestrated loop

```
profile source; reject output == source
if stored plan exists:  verify source size unchanged  (else StateStoreException)
else:                   refuse to replace an existing output (unless overwrite_output);
                        initialize(plan)
clear stale scratch files; requeue FAILED chunks with attempts left; aggregator.prepare()
loop: claim_next_pending()
        retry_count >= max_attempts  -> mark FAILED (abandoned), continue
        try   worker.process -> aggregator.merge
        except Exception            -> discard scratch, roll output back to the last commit,
                                       mark FAILED, continue
        mark_completed(chunk_id, output_end)
```

- **Crash vs. error.** Only `Exception` is caught per chunk. `SystemExit`,
  `KeyboardInterrupt` and a hard kill propagate and leave the chunk IN_PROGRESS for
  recovery, which is the behaviour the crash tests rely on.
- **Poison chunks.** Orphan recovery counts the abandoned attempt in `retry_count`, so a
  chunk that repeatedly kills the process (for example by exhausting memory) is marked
  FAILED after `max_attempts` instead of crash-looping.
- **Retried chunks land at the end.** A FAILED chunk that succeeds on a later run is
  appended after the chunks already committed, so output order can differ from file order
  in that case; no record is lost or duplicated.
- **Single writer, enforced.** The commit ledger assumes appends are serialised, and a second
  process would steal in-flight chunks during orphan recovery and truncate the output under the
  first. `run_mining` (and so the CLI and the MCP tool) therefore holds a `JobLock` for the job's
  whole lifetime: an OS file lock (`msvcrt.locking` / `flock`) on `.scratch/mining-<hash>.lock`,
  taken *before* `--overwrite` deletes anything. A second process fails at once with
  `JobLockedException`; the kernel drops the lock if the holder dies, however abruptly. The lock
  file is deliberately never deleted, so a late arrival cannot lock a fresh inode while another
  process still holds the old one. Code that wires `create_orchestrator` itself bypasses this guard.
- **Shrinking sources.** A plan is made for a specific file size. `range_lines` raises
  `DataSourceUnavailableException` when the file is shorter than the range it is asked to read,
  both up front and if the end of file arrives mid-range, so a truncated or replaced source fails
  the affected chunks instead of silently losing their records.
- **Failure reasons.** The first failed chunk's reason is carried in `MiningSummary.first_error`
  (the library's own message, an OS error's description without its path, or just the exception
  type: arbitrary exception text can quote the data). Sources in UTF-16/32 are refused before any
  planning, since every chunk would fail the same way.

### Security

- **Path confinement.** The scratch directory must resolve inside `.scratch/` or `data/`
  (`resolve_within`, which follows `..` and symlinks). Scratch file names are built only
  from an integer chunk id (`chunk_{id}.tmp`), so no caller-supplied text reaches a path;
  non-integer, negative and boolean ids are rejected; a scratch path that is a symlink is
  refused (and `O_NOFOLLOW` is used where available).
- **Permissions.** Files are created `0600` and directories `0700` on POSIX. On Windows,
  where new objects inherit their parent's ACL (often readable by every local user), a
  protected ACL for the current user, SYSTEM and Administrators is applied to what this
  package creates. See [privacy-and-security.md](privacy-and-security.md#file-permissions-least-privilege).
- **Source safety.** The source is only ever opened read-only, and the orchestrator
  refuses an output path that is the same file as the source.
- **Existing results.** A fresh run will not replace an existing non-empty output unless
  `overwrite_output=True`.
- **Regex safety.** `RegexExtractor` only compiles the pattern with `re`. The bundled
  e-mail pattern uses bounded quantifiers and a look-behind so that scanning is linear in
  line length (tested against million-character hostile lines). Patterns come from the
  operator, but an unbounded one can still backtrack catastrophically on hostile input.
- **CSV injection.** Mined text starting with `= + - @` can be interpreted as a formula by
  spreadsheets. `csv_formula_guard=True` prefixes such cells with an apostrophe. It is off
  by default because it changes values.

### Mining events

| Event | Level | Payload (`data`) |
| --- | --- | --- |
| `mining.started` | INFO | file name, resumed, recovered orphans, chunk totals |
| `chunk.completed` | INFO | chunk id, lines, records, oversized lines, duration |
| `chunk.failed` | WARNING | chunk id, error type, attempts |
| `chunk.abandoned` | WARNING | chunk id, attempts |
| `mining.completed` | INFO | run summary |
| `chunk.merged` | DEBUG | chunk id, appended bytes, output length |

Events carry names, ids and counts only; mined content and paths are never logged.

### Validation

`python scripts/simulate_mining_crash.py` builds a 50 MiB CSV with about 80,000 unique
e-mail addresses hidden among noise and decoys, mines it in a child process that is killed
(`os._exit`, no cleanup) at chunk 3, restarts it, and checks the output equals the planted
list exactly (same order, no duplicates, nothing missing, every chunk COMPLETED, chunks
1-2 untouched, `retry_count` 1 on chunk 3). It repeats this for three crash points
(`mid-chunk`, `mid-append`, `after-append`); for the last two it first confirms the output
really carries uncommitted bytes, so recovery is exercised. `--crash-mode sys` uses
`sys.exit` instead. The same scenarios run in-process in the pytest suite.


## MCP server and command line

`datamining-skill mcp` runs a Model Context Protocol server over stdio so an AI assistant
can profile and mine local files. It is a delivery mechanism around the existing use cases
and adds no data-processing logic of its own.

```
 MCP client ──stdin──▶  McpServer  (JSON-RPC 2.0 framing, era detection, dispatch)
 (assistant) ◀─stdout──     │
                            ▼
                       MiningTools  (declarations, argument validation, WorkspacePolicy)
                            │
                            ▼
              profile_dataset ─▶ create_profiler().profile_as_dict
              mine_dataset ────▶ run_mining(...)  ─▶ StateManager + MiningOrchestrator
```

| Module | Responsibility |
| --- | --- |
| `infrastructure/mcp_server.py` | Standard-library JSON-RPC 2.0 over newline-delimited stdio; protocol lifecycle; error codes; progress notifications. |
| `infrastructure/mcp_tools.py` | The `profile_dataset` and `mine_dataset` declarations (JSON Schema + behaviour annotations), argument validation, and `WorkspacePolicy` path confinement. Runners are injected, so it depends on neither the CLI nor the composition root. |
| `bootstrap.py` | `run_mining` (state/scratch layout, resume, overwrite) and `create_mcp_server`; shared by the `mine` command and the MCP tool. |
| `cli.py` | `profile`, `mine` and `mcp` commands; exit codes; allowed-directory resolution. |

### Protocol: one server, two eras

MCP changed shape in revision 2026-07-28, so the server answers both generations from the
same process, chosen per request:

| | Legacy (2024-11-05 ... 2025-11-25) | Modern (2026-07-28) |
| --- | --- | --- |
| Opening | `initialize` request + `notifications/initialized`; the negotiated revision applies to the process | none: every request carries `params._meta["io.modelcontextprotocol/protocolVersion"]` and `.../clientCapabilities` |
| Discovery | `initialize` result | `server/discover` |
| `ping` | answered with `{}` | not part of the protocol (`-32601`) |
| Results | plain | add `resultType: "complete"` and `_meta` server info; list results add `ttlMs` / `cacheScope` |
| Unsupported version | negotiation falls back to the newest legacy revision | `-32022` with `data.supported` and `data.requested` |
| Tool results | `structuredContent` only from 2025-06-18 | always |

A request with modern metadata is served statelessly; anything else requires a completed
`initialize` and gets `-32602` otherwise. `server/discover` is advertised in both eras.

JSON-RPC handling follows the specification: `-32700` parse error (id `null`), `-32600`
invalid request (non-object, wrong `jsonrpc`, missing/`null`/boolean/fractional id, batches,
oversized messages), `-32601` unknown method, `-32602` invalid params and unknown tools,
`-32603` internal error without detail. Notifications never receive a response. Messages are
one per line (compact JSON, ASCII-escaped so no console code page can corrupt it); oversized
lines are read in bounded pieces and rejected without losing synchronisation.

**Tool failures versus protocol failures.** Anything a model can fix (a bad path, a missing
argument, a refused overwrite, a library error such as an unsupported format) is returned as
a normal result with `isError: true` and an explanatory message, so the model can retry. Only
structurally invalid calls and unknown tools are JSON-RPC errors.

**stdout is the protocol channel.** Nothing else is ever written to it: logs go to stderr, and
`serve_stdio` points `sys.stdout` at stderr for the duration so a stray `print` cannot corrupt
the stream. The streams are reconfigured to UTF-8 with `\n` line endings (Windows would
otherwise translate and use the ANSI code page).

### Progress

When the request carries `_meta.progressToken`, the orchestrator's `on_progress` callback is
turned into `notifications/progress` messages (`progress` = chunks done, `total` = chunks,
`message` with record counts), written before the final response. Values strictly increase and
start at the chunks already completed by earlier runs, so a resumed job shows where it picks up.

### Limits of a single-threaded server

Requests are handled one at a time. A `notifications/cancelled` for a running call cannot be
acted on, because the server is busy inside the call; it is logged and ignored. This is safe
by design: killing the process mid-call leaves a consistent checkpoint, and calling
`mine_dataset` again resumes. There is no batch support (removed from MCP) and the server never
sends requests of its own.

### Mining jobs from the outside

`run_mining` derives a deterministic layout from the resolved source and output paths:
`<workspace>/.scratch/mining-<hash>.sqlite3` and `<workspace>/.scratch/mining-<hash>/`. The same
(source, output) pair therefore finds its previous progress, so repeating a call resumes it, while
different jobs never collide. `overwrite` deletes only that job's own state and scratch files,
then starts fresh. A source whose size changed since the plan was made is refused rather than
mined inconsistently.

### Command-line exit codes

`0` success, `1` unexpected internal error, `2` unsupported format, `3` unavailable source / invalid
configuration / file-system error / low memory / job already running / other library error, `4`
mining finished with failed chunks, `130` interrupted, `141` stdout pipe closed.

The CLI never shows a raw traceback unless asked. `main()` turns library exceptions, `OSError`
(its system description plus the file *name*, never the path), invalid UTF-8, and any other
exception (as `internal error (<Type>)`) into one `error:` line; `--debug` appends the traceback.
A closed output pipe exits quietly with 141 after pointing stdout at the null device, so the
interpreter's exit-time flush cannot fail a second time.

### Continuous integration

`.github/workflows/ci.yml` runs on every push and pull request: `mypy --strict` and the full
test suite on a matrix of Linux, macOS and Windows with Python 3.11 to 3.14, then builds the
sdist and wheel, installs the wheel in a clean virtual environment, asserts it brings in no
third-party packages, and runs `scripts/mcp_smoke_test.py` against the installed command.

