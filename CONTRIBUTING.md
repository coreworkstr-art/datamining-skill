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
   library. Development tools (`pytest`, `mypy`, `memory-profiler`, `ruff`) live under the `dev`
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
python -m ruff check .             # lint
python -m mypy                     # strict type checking: src, tests, scripts
python -m pytest                   # the full suite
python -m pytest -m "not memory"   # skip the one 256 MiB memory test while iterating
```

CI runs all three on Linux, macOS and Windows with Python 3.11-3.14, then builds the package and
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
* **New mining logic:** implement `ExtractionStrategy` (`fields` and
  `extract(line, start=0, stop=None)`); it must do no I/O. Accepting `start` and `stop` makes it
  exact on lines longer than the reader's cap (it reports only matches that begin in
  `[start, stop)`); `extract(line)` alone also works. Prefer bounded regex quantifiers:
  strategies run on hostile input. A strategy whose matches can never contain a line break may set
  `line_independent = True` to receive blocks of lines, but it must then give the same result as
  line-by-line searching; see `tests/test_blocks.py` for the differential test to copy.
* **A new output format:** implement `RecordFormatter` and select it in `create_orchestrator`.
* **A new MCP tool:** declare it in `infrastructure/mcp_tools.py`, validate its arguments,
  confine every path through `WorkspacePolicy`, and add tests, including abuse cases. Document it
  in `docs/mcp-tools.md` and `skills/datamining/SKILL.md`; `tests/test_packaging.py` fails until
  both mention every tool and argument.
* **A new command-line option:** add it to `cli.py`, to the option table of the README (a test
  checks that every option is documented) and to `CHANGELOG.md`.

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

## Releasing

Maintainers only. A release is a tag; everything else is automated.

1. Move the `[Unreleased]` entries of `CHANGELOG.md` under a new `## [X.Y.Z] - date` heading and
   set `__version__` in `src/datamining_skill/_version.py` and `"version"` in
   `.claude-plugin/plugin.json` to `X.Y.Z` (a test checks that they agree).
2. Merge to `main` once CI is green, then tag it: `git tag vX.Y.Z && git push origin vX.Y.Z`.
3. The `Release` workflow builds the sdist and wheel, verifies the tag against the version and the
   changelog, creates the GitHub release with the changelog section as its notes, and publishes
   to PyPI **if** the repository variable `PUBLISH_TO_PYPI` is `true`.

PyPI publishing uses [trusted publishing](https://docs.pypi.org/trusted-publishers/), so no token is
stored. Set it up once: on PyPI add a pending publisher for the project `datamining-skill`
(owner `coreworkstr-art`, repository `datamining-skill`, workflow `release.yml`, environment `pypi`),
create the `pypi` environment under the repository's *Settings, Environments* (add a required
reviewer if you want a manual gate), and set the variable `PUBLISH_TO_PYPI` to `true` under
*Settings, Secrets and variables, Actions, Variables*.

## Pull requests

1. Open an issue first for anything larger than a small fix, so the approach can be agreed.
2. Keep each pull request focused; describe what changed and why.
3. Update documentation (`README.md`, `docs/`) and `CHANGELOG.md` for user-visible changes.
4. Make sure CI passes. Maintainers review as time allows; please be patient and kind.

## Reporting bugs

Include the command or call you ran, what you expected, what happened, your OS and Python
version, and the output of `datamining-skill --help | head -1` (it shows the installed
command). **Do not attach real data**; reduce the problem to a small synthetic file.
