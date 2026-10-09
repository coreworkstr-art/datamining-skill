# MCP tool reference

`datamining-skill mcp` serves five tools over standard input/output. This page describes each
one: what it is for, its arguments, what it returns and what can go wrong. For how to connect
a client see the [README](../README.md#use-it-from-an-ai-assistant); for the threat model see
[privacy-and-security.md](privacy-and-security.md).

All tools run on this machine and never use the network. Every file argument must lie inside a
directory the server was started with (`--allow-dir`); a relative path starts at the first of
them, the **workspace**. Results come back as structured content (and as JSON text for clients
that predate it). A failed call returns `isError: true` with a message that never contains file
contents or full paths.

| Tool | Reads | Writes | Typical use |
| --- | --- | --- | --- |
| [`profile_dataset`](#profile_dataset) | the source (a bounded sample) | nothing (a compressed file: a short-lived sample in `.scratch/`) | Look before mining |
| [`mine_dataset`](#mine_dataset) | the source | the result file, `.scratch/` | Extract data |
| [`mining_status`](#mining_status) | nothing | nothing | Follow a background run |
| [`cancel_mining`](#cancel_mining) | nothing | nothing | Stop a background run |
| [`preview_result`](#preview_result) | the result file (at most 256 KiB) | nothing | Check the result |

## `profile_dataset`

Inspect a file without reading it fully into memory.

| Argument | Type | Description |
| --- | --- | --- |
| `path` | string, required | CSV, TSV, JSON, JSONL/NDJSON or log file; also gzip, bzip2, xz or zip compressed |

Returns, for example:

```json
{
  "file": {"name": "events.csv", "size_bytes": 542404586, "size_human": "517.28 MiB", "data_offset_bytes": 62},
  "format": "csv",
  "encoding": {"name": "ascii", "has_bom": false, "confidence": 1.0},
  "structure": {
    "fields": ["id", "created_at", "user_id", "email"],
    "field_count": 4, "delimiter": ",", "has_header": true,
    "multiline_records": false
  },
  "records": {"count": 4000000, "unit": "lines", "is_exact": false, "method": "stratified-window-extrapolation"},
  "profiling": {"duration_ms": 41.2}
}
```

- `format` is `csv`, `json`, `jsonl` or `log`. `structure.fields` lists the columns, the keys of
  the JSON, or the fields of the recognised log layout.
- `records.count` is in `records.unit`: `records` for JSON Lines (one line, one record), `lines`
  elsewhere, because a quoted CSV value can span several lines (`structure.multiline_records`).
  A count is exact for files up to 16 MiB and extrapolated from evenly spread windows above that.
- A **compressed file** is judged by its first 4 MiB once decompressed. The result then carries
  `compression` (for example `gzip`) and `sample.scope`, and `records.count` is `null`.
- UTF-16/32 files are profiled in place; their count is estimated from the head.

Errors: the file is empty, binary, in no recognised format, damaged, or outside the allowed
directories.

## `mine_dataset`

Extract data from a large file into a result file, in memory-bounded chunks with crash-safe
checkpoints.

| Argument | Type | Default | Description |
| --- | --- | --- | --- |
| `path` | string, required | | Source file (read-only) |
| `output_path` | string, required | | Result file. Its suffix selects the format: `.csv`, `.jsonl` or `.ndjson` |
| `overwrite` | boolean | `false` | Start over: discard earlier progress for this job and replace an existing result |
| `csv_formula_guard` | boolean | `false` | Prefix CSV cells that start with `= + - @` with an apostrophe so spreadsheets do not run them (changes those values) |
| `lowercase` | boolean | `false` | Lower-case every extracted value |
| `unique` | boolean | `false` | Keep only the first occurrence of each record, across the file and across resumed runs |
| `wait` | boolean | `true` | Return the summary when the run is done. With `false` the run continues in the background and the result is a job id |
| `pattern` | string | | Python regular expression to extract instead of e-mail addresses. **Only offered when the server was started with `--allow-custom-patterns`** |
| `fields` | array of strings | | Output column names, one per capture group of `pattern`. Same condition |

The built-in extraction finds e-mail addresses (including international ones such as
`müşteri@firma.com.tr`) and writes one `email` column.

**What identifies a job.** The source path, the output path and the settings `pattern`, `fields`,
`lowercase`, `unique` and `csv_formula_guard`. Calling again with the same values resumes an
interrupted run, or reports a finished one at once. Changing any of them starts a new job, which
is refused if the output file already exists (set `overwrite` or choose another path).

Returns (with `wait` true):

```json
{
  "output_path": "emails.csv", "succeeded": true, "resumed": false, "already_complete": false,
  "chunks_total": 34, "chunks_processed": 34, "chunks_failed": 0, "chunks_previously_completed": 0,
  "records_written": 1754, "duplicates_skipped": 4246, "oversized_lines": 0,
  "source_transform": null, "first_error": null, "recovered_orphans": 0, "duration_ms": 8120.4
}
```

- `records_written` and `duplicates_skipped` cover this run only.
- `oversized_lines` counts lines longer than 1 MiB, which were searched in overlapping windows.
- `source_transform` names a conversion applied first (`gzip`, `bzip2`, `xz`, `zip`, `utf-16-le`,
  `gzip+utf-16-le`, ...).
- If some chunks failed the call is an error whose text carries `first_error`; calling again
  retries them.

With `wait` false the result is the job description (see [`mining_status`](#mining_status)) with
`status: "running"`. Progress notifications are streamed while a call waits, when the client sent a
`progressToken`.

Errors include: the output exists and `overwrite` is not set; the output is the source; the same
job is already running (in another process, or as a background job of this server); a pattern
with a repeated capturing group, nested unbounded quantifiers or a bad regular expression; a
compressed file that expands beyond the limit; the source changed since the run began.

## `mining_status`

| Argument | Type | Description |
| --- | --- | --- |
| `job_id` | string | A job id from `mine_dataset`. Omit it to list every job of this server session |

Returns the job, or `{"jobs": [...]}` for the list:

```json
{
  "job_id": "3f9c1a7be2d4", "job": "events.csv -> emails.csv", "status": "running",
  "chunks_done": 12, "chunks_total": 34, "records_written": 52318,
  "started_at": "2026-10-08T11:02:44+00:00", "finished_at": null, "result": null, "error": null
}
```

`status` is `running`, `succeeded`, `failed` or `cancelled`. A finished job carries the summary
above in `result`; a failed or cancelled one carries a message in `error`. The server keeps the
last 20 finished jobs, and job ids last as long as the server process: after a restart call
`mine_dataset` again, which resumes from the checkpoints on disk.

## `cancel_mining`

| Argument | Type | Description |
| --- | --- | --- |
| `job_id` | string, required | The job to stop |

The job stops after the chunk it is working on (a chunk is at most 512 MiB). Nothing is lost:
`mine_dataset` with the same arguments resumes from the last checkpoint. Cancelling a finished job
changes nothing.

## `preview_result`

| Argument | Type | Default | Description |
| --- | --- | --- | --- |
| `path` | string, required | | A result file (`.csv`, `.jsonl` or `.ndjson`) |
| `rows` | integer | 20 | Records to return, 1 to 200 |

```json
{
  "file": "emails.csv", "format": "csv", "size_bytes": 53112,
  "columns": ["email"], "rows": [["ada.hollis@internal.corp.test"]],
  "rows_returned": 1, "has_more": true
}
```

CSV rows come back as lists, JSON Lines records as objects. At most 256 KiB of the file is read,
so any size is safe. The rows are the mined data: handle them as you would the source.

## Limits worth knowing

- One request is handled at a time; a waiting `mine_dataset` call blocks the others. Use
  `wait=false` for long runs.
- At most four background jobs run at once.
- A background job cannot report progress notifications (there is no request to attach them to);
  poll `mining_status` instead.
- A match longer than 4 KiB that lies inside a line longer than 1 MiB can be cut in two.
