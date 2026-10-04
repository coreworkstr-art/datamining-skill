## What and why

<!-- What does this change do, and why is it needed? Link the issue it addresses. -->

## Checklist

- [ ] `python -m mypy` passes (strict)
- [ ] `python -m pytest` passes locally; new behaviour has tests (including failure and abuse cases)
- [ ] No new runtime dependency, no network access, no files written outside `.scratch/` / `data/` or the named output
- [ ] Anything that touches data streams in bounded memory; nothing logs file contents or full paths
- [ ] Python 3.11-compatible syntax; works with both `/` and `\` path separators
- [ ] Documentation and `CHANGELOG.md` updated for user-visible changes
- [ ] No personal information in code, comments, metadata or the commit message
