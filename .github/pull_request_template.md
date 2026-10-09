## What and why

<!-- What does this change do, and why is it needed? Link the issue it addresses. -->

## Checklist

- [ ] `python -m ruff check .` and `python -m mypy` pass (strict)
- [ ] `python -m pytest` passes locally; new behaviour has tests (including failure and abuse cases)
- [ ] No new runtime dependency, no network access, no files written outside `.scratch/` / `data/` or the named output
- [ ] Anything that touches data streams in bounded memory; nothing logs file contents or full paths
- [ ] Python 3.11-compatible syntax; works with both `/` and `\` path separators
- [ ] Documentation and `CHANGELOG.md` updated for user-visible changes (new options and tools are
      also listed in the README, `docs/mcp-tools.md` and `skills/datamining/SKILL.md`)
- [ ] No personal information in code, comments, metadata or the commit message
