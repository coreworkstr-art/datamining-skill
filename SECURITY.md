# Security policy

## Supported versions

Security fixes are made to the latest released minor version (currently the 0.1 series).

## Reporting a vulnerability

Please report suspected vulnerabilities **privately** using the repository's private
reporting feature (**Security** tab, then **Report a vulnerability**). Do not open a public
issue, and do not include real or sensitive data in a report: a small synthetic file that
reproduces the problem is ideal.

Helpful details: the affected command, API call or MCP request; the input that triggers it;
what you expected and what happened; your OS and Python version.

This is a volunteer project, so response times are best-effort. Reports are acknowledged,
investigated and fixed as quickly as practical, and reporters are credited in the release
notes unless they prefer to stay anonymous.

## Scope

The project is designed to run entirely on the local machine, on data the user chooses.
These areas are in scope:

* **Path confinement:** any way for an MCP client, CLI argument or crafted data file to make
  the toolkit read or write outside the allowed directories, follow a symlink or junction out of
  them, contact a network share while validating a path (Windows UNC), or overwrite a file other
  than the named result file.
* **File permissions:** state, scratch or result files readable by other local users, or created
  with broader permissions than documented.
* **Memory and CPU exhaustion** from crafted input (for example, a file or MCP message that
  makes memory grow with its size, or a built-in pattern that backtracks catastrophically).
* **Code execution:** anything that evaluates data or executes content of a dataset.
* **Data leakage:** file contents or absolute paths appearing in logs, error messages or
  MCP results; files left in shared temporary directories.
* **Crash-safety violations:** a sequence of crashes that loses, duplicates or corrupts
  mined records.
* **MCP protocol handling:** messages that crash the server, corrupt the stdio channel or
  bypass argument validation.

Out of scope: vulnerabilities in Python, the operating system or MCP clients themselves;
attacks that require an attacker who can already run code as the same user; the behaviour
of regular expressions that an operator deliberately enables with
`--allow-custom-patterns` (this opt-in is documented as a risk); and local time-of-check /
time-of-use races by a process that already has write access to the allowed directories.

## Hardening notes

See [docs/privacy-and-security.md](docs/privacy-and-security.md) for the guarantees and the
MCP threat model.
