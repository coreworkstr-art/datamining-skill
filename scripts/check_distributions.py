"""Check that a built wheel and source distribution contain what a release needs.

    python scripts/check_distributions.py dist

Exits non-zero and lists the problems if the wheel lacks the typing marker, the CLI module or
the licence, or if the source distribution lacks the files a user needs to build, test and
understand the package, or contains local run artefacts.
"""

from __future__ import annotations

import sys
import tarfile
import zipfile
from pathlib import Path

WHEEL_REQUIRED = ("datamining_skill/py.typed", "datamining_skill/cli.py", "datamining_skill/__main__.py")
SDIST_REQUIRED = (
    "LICENSE",
    "README.md",
    "CHANGELOG.md",
    "SECURITY.md",
    "pyproject.toml",
    "tests/test_cli.py",
    "scripts/mcp_smoke_test.py",
    "skills/datamining/SKILL.md",
    ".claude-plugin/plugin.json",
    ".claude-plugin/marketplace.json",
    "examples/contacts.csv",
)
UNWANTED_MARKERS = (".scratch", ".venv", "__pycache__", ".sqlite3")


def check(dist: Path) -> list[str]:
    """Return what is wrong with the distributions in ``dist``; empty if all is well."""
    problems: list[str] = []
    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted(dist.glob("*.tar.gz"))
    if len(wheels) != 1:
        problems.append(f"expected one wheel, found {len(wheels)}")
    if len(sdists) != 1:
        problems.append(f"expected one source distribution, found {len(sdists)}")
    if problems:
        return problems

    with zipfile.ZipFile(wheels[0]) as archive:
        names = archive.namelist()
    problems += [f"wheel: {name} is missing" for name in WHEEL_REQUIRED if name not in names]
    if not any(name.endswith(".dist-info/licenses/LICENSE") for name in names):
        problems.append("wheel: the licence is not packaged")
    problems += [f"wheel: unwanted {name}" for name in names if _unwanted(name)]

    with tarfile.open(sdists[0]) as tar:
        members = tar.getnames()
    problems += [
        f"sdist: {needed} is missing"
        for needed in SDIST_REQUIRED
        if not any(member.endswith("/" + needed) for member in members)
    ]
    problems += [f"sdist: unwanted {member}" for member in members if _unwanted(member)]
    return problems


def _unwanted(name: str) -> bool:
    return any(marker in name for marker in UNWANTED_MARKERS)


def main(argv: list[str]) -> int:
    dist = Path(argv[1] if len(argv) > 1 else "dist")
    problems = check(dist)
    for problem in problems:
        print(f"error: {problem}", file=sys.stderr)
    if not problems:
        print("the distributions look complete")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
