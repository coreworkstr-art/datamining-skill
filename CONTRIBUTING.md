# Contributing to DataMining Skill

Thank you for helping improve DataMining Skill. Contributions of every size are welcome:
bug reports, documentation, tests, new format handlers, new extraction strategies and
features.

You may contribute under any name or pseudonym. No real-name or identity verification is
required, and none of the project's files should contain personal information about
contributors; credit goes to "DataMining Skill Contributors".

By participating you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md). Please
report security problems privately, as described in [SECURITY.md](SECURITY.md), not in a
public issue.

## Ground rules

These properties are the point of the project; changes must preserve them.

1. **Local-only.** No network access, no telemetry, no calls to external services.
2. **Zero runtime dependencies.** The package installs with nothing but the standard
   library. Development tools (`pytest`, `mypy`, `memory-profiler`) live under the `dev`
   extra only. A new runtime dependency needs a strong justification in the pull request.
3. **Constant memory.** Anything that touches data must stream: generators, bounded
   buffers, no accumulation proportional to file size.
4. **No stray data.** Source files are read-only. Writes happen only to the result file the
   user names and to confined `.scratch/` / `data/` locations. Never use the system
   temporary directory, and never log file *contents* or full paths.
5. **Crash safety.** A process may be killed at any instant. State changes go through the
   `StateManager`, and anything appended to the output must stay idempotent (see the
   TMP -> APPEND design in [docs/architecture.md](docs/architecture.md)).
6. **Untrusted input.** Parse, never evaluate. Validate every path that comes from outside
   (CLI users, MCP clients) against the allowed directories.

## Development setup

Python 3.11 or newer is required.

```bash
git clone <your fork>
cd datamining-skill
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

Run the same checks as CI before opening a pull request:

```bash
python -m mypy                     # strict type checking: src, tests, scripts
python -m pytest                   # the full suite
python -m pytest -m "not memory"   # skip the one 256 MiB memory test while iterating
```

CI runs both on Linux, macOS and Windows with Python 3.11-3.14, then builds the package and
smoke-tests the installed wheel. A pull request must be green on all of them.

## Architecture in one minute

Dependencies point inward only:

```
infrastructure  ->  application  ->  domain
   (adapters)        (use cases)      (models, ports, exceptions)
```

* `domain/` has no dependencies. Add new abstractions as `Protocol`s in `ports.py`.
* `application/` holds the logic and talks to the outside world only through ports.
* `infrastructure/` implements the ports (files, SQLite, MCP, logging).
* `bootstrap.py` is the composition root; `cli.py` is the command line.

Read [docs/architecture.md](docs/architecture.md) before changing the profiler, chunking
engine, state manager or the mining pipeline.

### Common extension points

* **A new file format:** implement `FormatHandler` (`analyze()` over a line iterator), return
  a confidence, and register it through `create_profiler(extra_handlers=...)`.
* **New mining logic:** implement `ExtractionStrategy` (`fields` and `extract(line)`); it must
  do no I/O. Prefer bounded regex quantifiers: strategies run on hostile input.
* **A new output format:** implement `RecordFormatter` and select it in `create_orchestrator`.
* **A new MCP tool:** declare it in `infrastructure/mcp_tools.py`, validate its arguments,
  confine every path through `WorkspacePolicy`, and add tests, including abuse cases.

## Code standards

* **Type hints everywhere**; `mypy --strict` must pass with no new `# type: ignore` unless
  unavoidable and explained.
* **Python 3.11 syntax only.** Do not use syntax introduced later (for example nested
  same-type quotes inside f-string expressions).
* **Cross-platform.** Use `pathlib`, never hard-code separators, and do not assume POSIX
  permissions or signals. CI includes Windows and macOS; handle platform differences
  explicitly and test them (`pytest.mark.skipif` for genuinely platform-specific checks).
* **Line endings are LF** (`.gitattributes` enforces it).
* Comments explain *why*; keep them impersonal and precise.

## Tests

* Add tests with every change: a failing test first when fixing a bug.
* Test data goes to the `workspace` / `state_dir` / `write_file` fixtures in
  `tests/conftest.py`, which use a project-local `.scratch/` directory. The `state_dir`
  teardown deletes its directory **without** `ignore_errors`, so a leaked file handle or
  database connection fails the test on Windows; keep it that way.
* Crash behaviour is tested by injecting failures at specific points, including a real
  hard exit in a child process. New persistence code needs the same treatment.
* Never put raw binary data in `parametrize` values (use `pytest.param(..., id=...)`).

## Pull requests

1. Open an issue first for anything larger than a small fix, so the approach can be agreed.
2. Keep each pull request focused; describe what changed and why.
3. Update documentation (`README.md`, `docs/`) and `CHANGELOG.md` for user-visible changes.
4. Make sure CI passes. Maintainers review as time allows; please be patient and kind.

## Reporting bugs

Include the command or call you ran, what you expected, what happened, your OS and Python
version, and the output of `datamining-skill --help | head -1` (it shows the installed
command). **Do not attach real data**; reduce the problem to a small synthetic file.
