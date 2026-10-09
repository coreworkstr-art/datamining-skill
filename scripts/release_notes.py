"""Print the changelog section of one release, for use as the GitHub release notes.

    python scripts/release_notes.py 0.2.0

The section is the text under the ``## [0.2.0] - date`` heading of CHANGELOG.md, up to the next
version heading. The command fails if there is none, so a release cannot be tagged without notes.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

HEADING = re.compile(r"^## \[(?P<version>[^\]]+)\][^\n]*$", re.MULTILINE)
DEFAULT_CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"


def extract_release_notes(changelog: str, version: str) -> str:
    """The notes under ``version``'s heading, without the heading; raises ``LookupError`` if absent."""
    headings = list(HEADING.finditer(changelog))
    for index, heading in enumerate(headings):
        if heading["version"] == version:
            end = headings[index + 1].start() if index + 1 < len(headings) else len(changelog)
            notes = changelog[heading.end() : end].strip()
            if not notes:
                raise LookupError(f"the changelog section for {version} is empty")
            return notes
    raise LookupError(f"the changelog has no section for {version}")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: release_notes.py VERSION", file=sys.stderr)
        return 2
    try:
        notes = extract_release_notes(DEFAULT_CHANGELOG.read_text(encoding="utf-8"), argv[1])
    except LookupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(notes)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
