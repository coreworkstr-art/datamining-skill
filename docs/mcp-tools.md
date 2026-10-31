# MCP tool reference

`datamining-skill mcp` serves six tools over standard input/output. This page describes each
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
| [`check_quality`](#check_quality) | the source, once (up to `max_records`) | nothing (a compressed file: a short-lived sample in `.scratch/`) | Find malformed rows, gaps and type drift |
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
- Data-derived names in `structure` are [masked](#masking-and-the-audit-log) by default, and
  `pii_masked` counts the replacements.
- A **compressed file** is judged by its first 4 MiB once decompressed. The result then carries
  `compression` (for example `gzip`) and `sample.scope`, and `records.count` is `null`.
- UTF-16/32 files are profiled in place; their count is estimated from the head.

Errors: the file is empty, binary, in no recognised format, damaged, or outside the allowed
directories.

## `check_quality`

Read a CSV, TSV or JSON Lines file once, in constant memory, and report what is wrong with it.

| Argument | Type | Default | Description |
| --- | --- | --- | --- |
| `path` | string, required | | CSV, TSV or JSON Lines file; also gzip, bzip2, xz or zip compressed (checked on its first 64 MiB once decompressed) |
| `max_records` | integer | 1,000,000 | Stop after this many records, 1 to 10,000,000. The call waits until the check is done |

Returns, for example:

```json
{
  "format": "csv",
  "summary": {"records_valid": 999990, "records_malformed": 10, "fields": 6, "fields_with_drift": 1, "issues": 3},
  "records": {"valid": 999990, "malformed": 10, "blank_lines": 0, "undecodable_lines": 0},
  "malformed": {"by_kind": {"too_few_columns": 10}, "examples": [{"line": 100000, "problem": "too_few_columns"}]},
  "fields": [
    {"name": "age", "present": 999990, "missing": 0, "empty_or_null": 20000, "completeness": 0.98,
     "types": {"integer": 979989, "empty": 20000, "string": 1}, "dominant_type": "number",
     "drift": true, "first_seen_line": {"integer": 2, "empty": 3, "string": 700002}}
  ],
  "issues": [
    "10 of 1,000,000 records are malformed: too_few_columns (10)",
    "field 'age' mixes number (979,989) and string (1); string first appears on line 700,002"
  ],
  "scope": {"records_examined": 1000000, "lines_read": 1000001, "bytes_read": 65011234, "max_records": 1000000, "complete": true, "note": null},
  "encoding": {"name": "utf-8", "has_bom": false, "confidence": 0.99},
  "file": {"name": "export.csv", "size_bytes": 65011301, "size_human": "62.0 MiB"}
}
```

- **Malformed** records are, for CSV: a row with fewer or more cells than the header
  (`too_few_columns`, `too_many_columns`), a broken quote (`bad_quoting`) and a value over 4 MiB
  (`field_too_large`); for JSON Lines: a line that is not JSON (`invalid_json`) or is JSON but not
  an object (`not_an_object`); for both, a line over 1 MiB (`line_too_long`). Malformed rows are
  left out of the field statistics. `examples` lists the first five lines, never their content.
- **Types** of a CSV cell are read from its text: `integer`, `float`, `boolean` (`true` or
  `false`), `date`, `datetime`, `string`, `empty`, and `null` for tokens such as `NULL`, `n/a` or
  `\N`. A JSON value keeps its JSON type, so `"42"` is a `string`; `object` and `array` are also
  types. **Drift** means a field holds values of more than one kind once integers and floats count
  as one kind (numbers) and dates and datetimes as another (points in time); empty and null do not
  count. `first_seen_line` says where each type first appeared, which is where to start looking.
- `completeness` is the share of valid records with a real value; `missing` counts JSON records
  that lack the key altogether.
- An `encoding` with a confidence below 0.9 is a guess (the file is not valid UTF-8) and comes
  with a first entry in `issues`. `undecodable_lines` counts lines with bytes that the detected
  encoding could not decode.
- At most 512 fields are followed (the rest is noted in `issues`), and `issues` lists at most 25
  findings, those about the whole file first.
- `scope.complete` is false when the check stopped at `max_records`, or looked only at the
  sample of a compressed file.
- The field names, which come from the data, are [masked](#masking-and-the-audit-log) by default
  and `pii_masked` counts the replacements.

Errors: the file is empty, binary, in no recognised format, or in a format other than CSV, TSV or
JSON Lines (a log, a single JSON document). The format is detected before anything else, so a file
that is mostly broken is refused as "no supported format" rather than checked.

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
  "columns": ["email"], "rows": [["[EMAIL]"]],
  "rows_returned": 1, "has_more": true, "pii_masked": {"EMAIL": 1}
}
```

CSV rows come back as lists, JSON Lines records as objects. At most 256 KiB of the file is read,
so any size is safe. The rows are the mined data: handle them as you would the source.

By default the rows are [masked](#masking-and-the-audit-log): the example above shows what a
result of e-mail addresses looks like, and `pii_masked` counts the replacements per kind. The
result file itself is never changed.

## Masking and the audit log

Two settings of `datamining-skill mcp` protect what the assistant sees and record what it did.

**Masking** is on by default. In `preview_result` rows and in the `structure.fields`,
`structure.field_types` and `structure.pattern` of `profile_dataset` (a CSV without a header row
makes its first data row the "column names"), these are replaced by a tag:

| Found | Tag | Check applied |
| --- | --- | --- |
| E-mail address | `[EMAIL]` | the built-in e-mail pattern |
| IPv4 address | `[IP]` | four octets from 0 to 255 |
| Payment card number, 13 to 19 digits | `[CARD]` | Luhn |
| Turkish national identity number (T.C. kimlik no) | `[TCKN]` | the two check digits |
| IBAN | `[IBAN]` | ISO 7064 mod 97 |

`pii_masked` in the result counts the replacements per tag. The checks keep ordinary numbers from
being hidden, but masking is best effort: names, postal addresses, phone numbers, IPv6 addresses
and free text are not recognised, and a number with a mistyped digit is left alone. It is a safety
net, not anonymisation. `--unmasked-preview` turns it off and prints a warning on stderr.

**The audit log** (`--audit-log FILE`) appends one JSON line per tool call: time, tool, whether it
worked, duration in milliseconds, workspace-relative paths, the switches used (`unique`,
`lowercase`, ...), counts from the result, and the error text of a failed call. It never holds file
contents, patterns or rows; a custom pattern shows only as `"custom_pattern": true`, and a path
the server refused as `"<rejected>"`. The file must lie **outside** the allowed directories, or
the server refuses to start: inside them the assistant could read it with `preview_result` or
overwrite it with `mine_dataset`. It is created owner-only and written with append semantics. A
failure to write a record is reported in the server log and does not fail the call.

```json
{"time":"2026-10-09T12:00:01.250+00:00","event":"tool_call","tool":"mine_dataset","ok":true,"ms":812,"path":"export.csv","output_path":"emails.csv","unique":true,"records_written":1754,"duplicates_skipped":4246}
```

## Limits worth knowing

- One request is handled at a time; a waiting `mine_dataset` call blocks the others. Use
  `wait=false` for long runs.
- At most four background jobs run at once.
- A background job cannot report progress notifications (there is no request to attach them to);
  poll `mining_status` instead.
- A match longer than 4 KiB that lies inside a line longer than 1 MiB can be cut in two.
