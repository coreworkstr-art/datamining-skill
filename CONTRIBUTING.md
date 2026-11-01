# Contributing

Bug reports, documentation fixes, tests, new format handlers and new extraction strategies are all
welcome, down to a single corrected typo.

You can contribute under any name or pseudonym. Nobody checks identities, and no file in the
project should hold personal details about contributors; credit goes to "DataMining Skill
Contributors". Please follow the [Code of Conduct](CODE_OF_CONDUCT.md), and report security problems
privately, as [SECURITY.md](SECURITY.md) describes, not in a public issue.

## What the project promises

A few properties are the reason this tool exists, so a change should not weaken them.

1. Nothing leaves the machine. No network access, no telemetry, no calls to outside services.
2. No runtime dependencies. Running it needs only the standard library. Development tools
   (`pytest`, `mypy`, `memory-profiler`, `ruff`) live in the `dev` extra. A new runtime dependency
   needs a good argument in the pull request.
3. Memory does not grow with the input. Whatever touches data streams it, with generators and
   bounded buffers, never with something proportional to the size of the file.
4. No stray files. Sources are only read. The program writes the result file the user named and
   files under `.scratch/` or `data/`, never anything in the system temp directory, and file
   contents and full paths stay out of the logs.
5. Killing the process is safe. State changes go through `StateManager`, and whatever is appended
   to the output has to stay idempotent (the TMP -> APPEND design in
   [docs/architecture.md](docs/architecture.md)).
6. Input is untrusted. Parse it, never evaluate it, and check every path that comes from outside
   (the command line, an MCP client) against the allowed directories.

## Setting up

You need Python 3.11 or newer.

```bash
git clone <your fork>
cd datamining-skill
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

Before you open a pull request, run what CI runs:

```bash
python -m ruff check .             # lint
python -m mypy                     # strict type checking: src, tests, scripts
python -m pytest                   # the full suite
python -m pytest -m "not memory"   # skips the one 256 MiB memory test while you iterate
```

CI runs these on Linux, macOS and Windows with Python 3.11 to 3.14, then builds the package and
smoke-tests the installed wheel. A pull request has to be green everywhere.

## How the code is laid out

Dependencies point inward only:

```
infrastructure  ->  application  ->  domain
   (adapters)        (use cases)      (models, ports, exceptions)
```

`domain/` depends on nothing; new abstractions go in `ports.py` as `Protocol`s. `application/`
holds the logic and reaches the outside world only through ports. `infrastructure/` implements the
ports (files, SQLite, MCP, logging). `bootstrap.py` wires it together and `cli.py` is the command
line. Read [docs/architecture.md](docs/architecture.md) before you change the profiler, the
chunking engine, the state manager or the mining pipeline.

### Adding things

- A new file format: implement `FormatHandler` (`analyze()` over a line iterator), return a
  confidence, and register it with `create_profiler(extra_handlers=...)`.
- New mining logic: implement `ExtractionStrategy` (`fields` and `extract(line, start=0, stop=None)`).
  It must do no I/O. If it accepts `start` and `stop` it is exact even on lines longer than the
  reader's cap, reporting only matches that begin in `[start, stop)`; `extract(line)` alone also
  works. Prefer bounded regex quantifiers, because strategies run on hostile input. A strategy
  whose matches can never contain a line break may set `line_independent = True` to receive blocks
  of lines, but it then has to give the same result as searching line by line.
  `tests/test_blocks.py` has the differential test to copy.
- A new output format: implement `RecordFormatter` and select it in `create_orchestrator`.
- A new MCP tool: declare it in `infrastructure/mcp_tools.py`, validate its arguments, confine
  every path through `WorkspacePolicy`, and test it, abuse cases included. Document it in
  `docs/mcp-tools.md` and `skills/datamining/SKILL.md`; `tests/test_packaging.py` fails until both
  mention every tool and argument.
- A new command-line option: add it to `cli.py`, to the option table in the README (a test checks
  that every option is documented) and to `CHANGELOG.md`.

## Code standards

- Type hints everywhere. `mypy --strict` must pass, with no new `# type: ignore` unless it cannot be
  avoided and says why.
- Python 3.11 syntax only. Don't use anything introduced later, for example nested same-type quotes
  inside an f-string expression.
- It has to work on every platform. Use `pathlib`, don't hard-code separators, and don't assume
  POSIX permissions or signals. CI includes Windows and macOS, so handle the differences openly and
  test them (`pytest.mark.skipif` for checks that really are platform-specific).
- Line endings are LF; `.gitattributes` enforces it.
- Comments explain why, not what. Keep them short and impersonal.

## Tests

- Add tests with every change, and for a bug fix write the failing test first.
- Test data goes through the `workspace`, `state_dir` and `write_file` fixtures in
  `tests/conftest.py`, which use a project-local `.scratch/` directory. The `state_dir` teardown
  deletes its directory without `ignore_errors`, so a leaked file handle or database connection
  fails the test on Windows. Please keep it that way.
- Crash behaviour is tested by injecting failures at specific points, including a real hard exit in
  a child process. New persistence code needs the same treatment.
- Never put raw binary data in `parametrize` values; use `pytest.param(..., id=...)`.

## Releasing

Only maintainers do this. A release is a tag, and the rest is automated.

1. Move the `[Unreleased]` entries of `CHANGELOG.md` under a new `## [X.Y.Z] - date` heading, and
   set `__version__` in `src/datamining_skill/_version.py` and `"version"` in
   `.claude-plugin/plugin.json` to `X.Y.Z`. A test checks that they agree.
2. Merge to `main` once CI is green, then tag it: `git tag vX.Y.Z && git push origin vX.Y.Z`.
3. The `Release` workflow builds the sdist and the wheel, checks the tag against the version and the
   changelog, creates the GitHub release with the changelog section as its notes, and publishes to
   PyPI if the repository variable `PUBLISH_TO_PYPI` is `true`.

PyPI publishing uses [trusted publishing](https://docs.pypi.org/trusted-publishers/), so no token is
stored anywhere. To set it up once: on PyPI add a pending publisher for the project
`datamining-skill` (owner `coreworkstr-art`, repository `datamining-skill`, workflow `release.yml`,
environment `pypi`); create the `pypi` environment under the repository's *Settings, Environments*,
with a required reviewer if you want a manual gate; and set the variable `PUBLISH_TO_PYPI` to `true`
under *Settings, Secrets and variables, Actions, Variables*.

## Pull requests

Open an issue first for anything bigger than a small fix, so we can agree on the approach before you
spend time on it. Keep each pull request to one thing and say what changed and why. Update the
documentation (`README.md`, `docs/`) and `CHANGELOG.md` when users will notice the change, and make
sure CI passes. Maintainers review when they can, so a little patience goes a long way.

## Reporting bugs

Tell us the command or call you ran, what you expected, what happened, your operating system and
Python version, and the output of `datamining-skill --version`. Please don't attach real data;
reduce the problem to a small made-up file.
