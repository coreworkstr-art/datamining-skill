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

**`Failed to spawn` with `Application Control policy` (`os error 4551`) on Windows.** Smart App
Control, or a company's application policy, blocks programs it does not know, and uv makes a small
`.exe` launcher for the command it installs or runs. The plugin avoids this by starting the server
as `python -m datamining_skill`, which runs only the interpreter. If you start the server yourself
and meet the error, do the same: after a `pip install`, use `"command": "python", "args": ["-m",
"datamining_skill", "mcp", "--allow-dir", "..."]`, or with uv
`uvx --from <the repository folder> python -m datamining_skill mcp --allow-dir ...`.

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
| `Unsupported data format ... is not readable text either` | The file is empty of text (only blank lines) or mostly control characters, so it is not text either. Check that it is a text file and not a database export or an Excel workbook (`.xlsx` is a zip of XML files: export it to CSV first). Any other text is accepted: when no table, log or JSON layout fits, it is read as plain lines (`format` is `text`), which is enough to mine addresses out of a list, a web page or an e-mail message. |
| `... quality checks cover CSV, TSV and JSON Lines files` | `quality` was given a log, a single JSON document or plain text (`this file looks like text`: it has no columns to check). Use `profile` for those; `mine` still works on them. A table in which most rows are damaged ends up here too, because the layout is detected before anything is checked: look at its first lines, or cut a good sample out of it, and run `quality` on that. |
| `... binary content (NUL bytes ...)` | The file is not text. |
| `... gzip-compressed content; decompress or convert it ...` | Only appears for formats that cannot be converted (zstandard, 7-zip, Parquet, PDF, ...). gzip, bzip2, xz and single-file zip are handled for you. |
| `... zip archive with N files` | Extract the one file you want to mine first. |
| `... expands to more than N GiB` | The unpacked size passes `--max-expanded-gib` (default 64). Raise it if the size is expected. |
| `only N of disk space is left for the converted copy` | A compressed or UTF-16 source needs a temporary UTF-8 copy in `.scratch/`. Free space or use another `--workspace`. |
| `the output file already exists; choose another path or allow overwriting` | A result from another job is in the way. Pick another output path, or add `--overwrite` to replace it. Changing a setting (pattern, `--unique`, ...) counts as another job. |
| `this mining job is already running in another process` | Another process holds the job's lock. Wait for it, or stop it; a killed process releases the lock by itself. |
| `the pattern repeats a capturing group` | Use `(?:...)` for groups you only repeat: `(?:\d{1,3}\.){3}\d{1,3}`. With capturing groups, each group becomes a column. |
| `the source changed since this run began` | The file's size, modification time or content (checked at its start, middle and end) differs from what the run began with, even if only the time was touched. Start over with `--overwrite`. A result made from the old content is not resumed. |
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

**Different case, same address.** Add `--lowercase` together with `--unique`. (The Turkish dotted
capital I is turned into a plain i, so `MUSTERI@...` written with it matches the lower-case form.)

**The first address of a list is missing.** A table is taken to have a header row when its first
row holds short names that start with a letter and contain no `@`. A first row with an address in
it is data, so a list of `address,name` lines keeps its first address. A headerless table whose
first row is made of plain words (`Ada,Istanbul`) is still taken to have a header; give such a
file a header row of its own.

**A record count that does not match the number of CSV rows.** The count of a CSV is in lines.
A quoted value with a line break makes a record span several lines; `profile` flags this as
`multiline_records`.

**Output order differs from file order.** A chunk that failed and succeeded on a retry is
appended after the others. No record is lost or repeated.

**A spreadsheet shows odd values.** If mined text starts with `=`, `+`, `-` or `@`, a spreadsheet
may treat it as a formula. Mine with `--csv-formula-guard`.

## Reading a quality report

**`quality` says the encoding is a guess.** The file is not valid UTF-8, and the detector fell
back to cp1252, cp1254 (Turkish, chosen when the letters g-breve, dotted and dotless i and
s-cedilla appear without the accented vowels of the Western languages) or ISO-8859-1, which
decode every byte. A UTF-8 file with a few corrupt bytes ends up here, and its accented letters
are then read wrongly. Find the bad bytes and re-save the file as UTF-8. Addresses made of plain
ASCII letters are read correctly whatever the guess.

**A field is reported as drifting but looks fine.** Drift means the field holds more than one
kind of value: numbers, words, dates, booleans. A column of identifiers such as `00123`, `A-17`
and `4567` is numbers and words at once, and so is a JSON field that is sometimes `5` and
sometimes `"5"`. `first_seen_line` shows where the second kind starts.

**The counts are lower than the number of lines.** `records` counts records, not lines: a quoted
CSV value that spans lines is one record, blank lines are listed apart, and a malformed row is
counted under `malformed` and left out of the field statistics.

**`complete` is false.** The check stopped at `--max-records` (raise it), at the time limit
(`max_seconds`: 20 seconds in the MCP tool, none on the command line unless you pass
`--max-seconds`; the note says which), or the source is compressed and only its first 64 MiB
were read. What was read is reported correctly; it is the part of the file that was checked.

## Speed

A rough guide, on a laptop-class machine with an SSD: a log file with an address every few
kilobytes is mined at 200 MiB/s or more; a CSV with an address on every row at 15 to 25 MiB/s.
The work runs on one thread and is bound by the interpreter (a library caller can use more, but
the gain is small, see the README's Limitations), so a faster disk does not help much once the
rows are dense. What does:

- Keep the source and the workspace on a **local** disk.
- On Windows, exclude the workspace's `.scratch` folder from real-time antivirus scanning if it
  slows the run noticeably: it holds many small writes.
- Prefer the built-in extraction to a custom pattern: it finds each `@` with a plain string search
  and runs the pattern only around it, which is far quicker on text where addresses are rare.
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
