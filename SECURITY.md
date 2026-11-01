# Security policy

Fixes go into the latest minor release, which for now is the 0.3 series.

## Reporting a problem

If you think you have found a vulnerability, tell us privately. Open the Security tab of the
repository and choose "Report a vulnerability". Please don't open a public issue for it, and don't
send real data. A small made-up file that triggers the problem is the best thing to attach.

It helps to know which command, API call or MCP request is involved, what input triggers it, what
you expected and what happened instead, and which operating system and Python version you use.

This is a volunteer project, so a reply can take a while. Every report is read, and a confirmed
problem is fixed as soon as we can manage. If you want credit we will name you in the release notes;
if you would rather stay anonymous, say so.

## What counts

The program is meant to run on your own machine, on files you choose, and to send nothing anywhere.
Anything that breaks that is what we want to hear about:

- Reading or writing outside the allowed directories, whether through a symlink or junction, a
  crafted file name, a command-line or MCP argument, or a Windows UNC path that makes the system
  contact a network share while a path is being checked. Overwriting any file other than the
  named result file belongs here too.
- State, scratch or result files that other users of the machine can read, or that are created
  with looser permissions than the documentation says.
- Input that makes memory or CPU use run away: memory that grows with the size of a file or an MCP
  message, or a built-in pattern that backtracks without end.
- Anything that evaluates the data or runs what a dataset contains.
- A compressed or UTF-16 file that makes the conversion use more memory or disk than documented,
  leaves a partial copy others can read, or writes outside the job's scratch folder.
- File contents or absolute paths in logs, error messages or MCP results, and files left in shared
  temporary directories.
- A series of crashes that loses, repeats or corrupts mined records.
- MCP messages that crash the server, corrupt the stdio channel or get past argument validation.

Masking of personal data in previews is a best-effort safety net, so a number it misses is a bug
worth reporting, but not a vulnerability.

## What doesn't

Vulnerabilities in Python, the operating system or the MCP clients themselves. Attacks that already
need code running as the same user. The behaviour of regular expressions that an operator turned on
with `--allow-custom-patterns`, which is documented as a risk. Races between a check and a use by
someone who can already write to the allowed directories.

More background, including the threat model for MCP clients, is in
[docs/privacy-and-security.md](docs/privacy-and-security.md).
