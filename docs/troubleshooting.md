# Troubleshooting

Start with `datamining-skill --version`, then add `--debug` to a failing command to see the
traceback behind its one-line `error:` message. Exit codes are listed in the
[README](../README.md#command-line).

## Installing and connecting

**`datamining-skill: command not found`.** The console script is in the `Scripts` (Windows) or
`bin` folder of the environment you installed into. Activate that environment, or call the
interpreter instead: `python -m datamining_skill --help`.

**`uvx: command not found` when a client starts the plugin.** The Claude Code plugin starts the
server with [uv](https://docs.astral.sh/uv/). Install uv, or skip the plugin and register the
server by hand with a normal `pip install` (see the README: *Claude Code*, *Claude Desktop*,
*Cursor*).

**The server does not show up in the client.** Run the same command the client runs, in a
terminal: `datamining-skill mcp --allow-dir /your/data`. It should wait silently for input
(press Ctrl+D or Ctrl+Z to end it). Then run `python scripts/mcp_smoke_test.py`, which performs a
complete handshake, profile and mine against the installed command and names the step that fails.
In a JSON configuration file, Windows paths need doubled backslashes (`"C:\\Users\\you\\data"`);
forward slashes also work. Use the full path of the command when it is not on the client's `PATH`.

**A tool says the path is outside the allowed directories.** Every file argument must lie inside
a directory passed with `--allow-dir` (or listed in `DATAMINING_SKILL_ALLOWED_DIRS`), after
symbolic links are resolved. Relative paths start at the first allowed directory.

## Messages

| Message | What it means and what to do |
| --- | --- |
| `Unsupported data format ... content matches no supported format` | The head of the file looks like no supported format. The message lists the confidence per format. Check that the file is text and not a database export or an Excel workbook (`.xlsx` is a zip of XML files: export it to CSV first). |
| `... binary content (NUL bytes ...)` | The file is not text. |
| `... gzip-compressed content; decompress or convert it ...` | Only appears for formats that cannot be converted (zstandard, 7-zip, Parquet, PDF, ...). gzip, bzip2, xz and single-file zip are handled for you. |
| `... zip archive with N files` | Extract the one file you want to mine first. |
| `... expands to more than N GiB` | The unpacked size passes `--max-expanded-gib` (default 64). Raise it if the size is expected. |
| `only N of disk space is left for the converted copy` | A compressed or UTF-16 source needs a temporary UTF-8 copy in `.scratch/`. Free space or use another `--workspace`. |
| `the output file already exists; choose another path or allow overwriting` | A result from another job is in the way. Pick another output path, or add `--overwrite` to replace it. Changing a setting (pattern, `--unique`, ...) counts as another job. |
| `this mining job is already running in another process` | Another process holds the job's lock. Wait for it, or stop it; a killed process releases the lock by itself. |
| `the pattern repeats a capturing group` | Use `(?:...)` for groups you only repeat: `(?:\d{1,3}\.){3}\d{1,3}`. With capturing groups, each group becomes a column. |
| `the source changed since this run began` | The file was edited after the run started. Start over with `--overwrite`. |
| `the file is shorter than the byte range being read` | The source was truncated or replaced while it was mined. The affected chunks failed; restore the file and run the same command again. |
| `N chunk(s) failed; the result is incomplete` | The first reason is printed. Fix it and repeat the command: only the failed chunks run again. |
| `write-ahead logging could not be enabled` | The workspace is on a network drive. SQLite needs a local disk: use `--workspace` on a local folder. |
| `available memory ... is below the critical minimum` | The machine has under 100 MiB of free RAM. Close programs and retry. |

## The result is not what I expected

**Fewer rows than lines in the file.** The built-in extraction writes one row per address, not
per line. Lines without an `@` produce nothing, and with `--unique` repeats are dropped
(`duplicates_skipped` says how many).

**An address is missing.** The extraction looks for `local@domain.tld` with a top-level domain of
letters. It does not match addresses with a quoted local part, with characters such as `!#$&'*/=?^{|}~`
in it, or with a bare host (`user@localhost`). For JSON files it decodes `\uXXXX` escapes first
(`--json-escapes`); for other sources it searches the text as written, so an address that is
HTML-escaped or base64-encoded is not found. A custom `--pattern` can be written for any of these.

**Different case, same address.** Add `--lowercase` together with `--unique`.

**A record count that does not match the number of CSV rows.** The count of a CSV is in lines.
A quoted value with a line break makes a record span several lines; `profile` flags this as
`multiline_records`.

**Output order differs from file order.** A chunk that failed and succeeded on a retry is
appended after the others. No record is lost or repeated.

**A spreadsheet shows odd values.** If mined text starts with `=`, `+`, `-` or `@`, a spreadsheet
may treat it as a formula. Mine with `--csv-formula-guard`.

## Speed

A rough guide, on a laptop-class machine with an SSD: a log file where one line in a hundred has
an address is mined at 60 to 90 MiB/s; a CSV with an address on every row at 15 to 40 MiB/s.
The work is single-threaded and bound by the interpreter, so a faster disk does not help much.
What does:

- Keep the source and the workspace on a **local** disk.
- On Windows, exclude the workspace's `.scratch` folder from real-time antivirus scanning if it
  slows the run noticeably: it holds many small writes.
- Prefer the built-in extraction to a custom pattern: it skips lines without an `@` outright.
- Do not run with `--log-level DEBUG` for a large file.

Interrupting a run is always safe: repeat the command and it resumes.

## Disk space and clean-up

Each job leaves a small state database and scratch folder under `<workspace>/.scratch/`; a
finished job can be removed with `datamining-skill clean`, which never touches a result file or a
running job. `--dry-run` lists what would go. A job that did not finish is kept so that it can
resume; `clean --all` removes it as well.

A compressed or UTF-16 source is converted to a temporary UTF-8 copy while it is mined. It is
removed when the job finishes and stays only while a run is unfinished.

## Windows

- Paths longer than 260 characters work when Windows long-path support is enabled; otherwise
  the operating system's error is reported as a one-line message.
- UNC (`\\host\share`) and device paths are refused by the MCP server on purpose.
- If an interrupted run leaves `.scratch` files you want gone, run `datamining-skill clean --all`.

## Reporting a problem

Open an issue with the output of `datamining-skill --version`, your operating system and Python
version, the exact command, and a small **synthetic** file that reproduces it. Please do not
attach real data. Security problems go to the private channel described in
[SECURITY.md](../SECURITY.md).
