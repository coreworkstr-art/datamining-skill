"""The release metadata, plugin, skill, documentation and workflows must agree with the code."""

from __future__ import annotations

import argparse
import gzip
import importlib.util
import io
import json
import re
import tarfile
import zipfile
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

from datamining_skill import __version__
from datamining_skill.cli import _build_parser
from datamining_skill.infrastructure.mcp_tools import MineRunner, MiningTools, WorkspacePolicy

ROOT = Path(__file__).resolve().parent.parent
KEBAB = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_json(relative: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((ROOT / relative).read_text(encoding="utf-8"))
    return data


def tool_definitions() -> list[Any]:
    # the runner is never called: only the declarations are read
    tools = MiningTools(
        WorkspacePolicy([ROOT]), profile=lambda path: {}, mine=cast(MineRunner, None), allow_custom_patterns=True
    )
    return tools.definitions()


# ---------------------------------------------------------------- version and changelog


def test_the_version_is_a_release_version_with_a_changelog_section() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)
    notes = load_script("release_notes").extract_release_notes(
        (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), __version__
    )
    assert len(notes) > 100


def test_release_notes_are_cut_at_the_next_version_heading() -> None:
    extract = load_script("release_notes").extract_release_notes
    changelog = "# Changelog\n\n## [Unreleased]\n\nsoon\n\n## [1.1.0] - 2026-01-01\n\n### Added\n\n- b\n\n## [1.0.0] - 2025-01-01\n\n- a\n"

    assert extract(changelog, "1.1.0") == "### Added\n\n- b"
    assert extract(changelog, "1.0.0") == "- a"
    with pytest.raises(LookupError, match=r"no section for 2\.0\.0"):
        extract(changelog, "2.0.0")
    with pytest.raises(LookupError, match="empty"):
        extract("## [3.0.0] - x\n\n## [2.0.0] - y\n\n- z\n", "3.0.0")


def test_the_release_notes_command_prints_the_section_and_fails_when_it_is_missing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    main = load_script("release_notes").main

    assert main(["release_notes.py", __version__]) == 0
    assert capsys.readouterr().out.strip()
    assert main(["release_notes.py", "99.0.0"]) == 1
    assert "no section for 99.0.0" in capsys.readouterr().err
    assert main(["release_notes.py"]) == 2


# --------------------------------------------------------------------- plugin and skill


def test_the_plugin_manifest_matches_the_package() -> None:
    plugin = read_json(".claude-plugin/plugin.json")

    assert KEBAB.fullmatch(plugin["name"]) and plugin["name"] == "datamining-skill"
    assert plugin["version"] == __version__
    assert plugin["license"] == "MIT" and plugin["description"] and plugin["author"]["name"]
    server = plugin["mcpServers"]["datamining"]
    assert server["command"] == "uvx"
    assert server["args"][:3] == ["--from", "${CLAUDE_PLUGIN_ROOT}", "datamining-skill"]
    assert server["args"][3:] == ["mcp", "--allow-dir", "${CLAUDE_PROJECT_DIR}"]


def test_the_marketplace_lists_the_plugin_at_the_repository_root() -> None:
    marketplace = read_json(".claude-plugin/marketplace.json")
    plugin = read_json(".claude-plugin/plugin.json")

    assert KEBAB.fullmatch(marketplace["name"]) and marketplace["owner"]["name"]
    (entry,) = marketplace["plugins"]
    assert entry["name"] == plugin["name"] and entry["source"] == "./" and entry["description"]


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    assert text.startswith("---\n")
    header, body = text[4:].split("\n---\n", 1)
    fields = dict(line.split(": ", 1) for line in header.splitlines())
    return fields, body


def test_the_skill_follows_the_format_and_stays_concise() -> None:
    text = (ROOT / "skills" / "datamining" / "SKILL.md").read_text(encoding="utf-8")

    fields, body = split_frontmatter(text)

    assert fields["name"] == "datamining" and KEBAB.fullmatch(fields["name"])
    assert len(fields["description"]) <= 1024 and "Use when" in fields["description"]
    assert len(body.splitlines()) < 200  # the whole body is re-read on every use: keep it short


def test_the_skill_mentions_every_tool_and_every_argument() -> None:
    text = (ROOT / "skills" / "datamining" / "SKILL.md").read_text(encoding="utf-8")

    for definition in tool_definitions():
        assert f"`{definition.name}`" in text, definition.name
        for argument in definition.input_schema["properties"]:
            assert argument in text, (definition.name, argument)


# ------------------------------------------------------------------------- documentation


MARKDOWN_FILES = [
    "README.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "docs/architecture.md",
    "docs/privacy-and-security.md",
    "docs/mcp-tools.md",
    "docs/troubleshooting.md",
    "skills/datamining/SKILL.md",
]


@pytest.mark.parametrize("relative", MARKDOWN_FILES)
def test_relative_links_in_the_documentation_resolve(relative: str) -> None:
    path = ROOT / relative
    text = path.read_text(encoding="utf-8")
    targets = re.findall(r"\]\(([^)\s]+)\)", text)

    broken = [
        target
        for target in targets
        if not target.startswith(("http://", "https://", "mailto:", "#"))
        and not (path.parent / target.split("#")[0]).exists()
    ]

    assert broken == []


def test_the_readme_documents_every_command_and_option() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    parser = _build_parser()
    subparsers = next(action for action in parser._actions if action.dest == "command")

    choices = cast(dict[str, argparse.ArgumentParser], subparsers.choices)
    for command, subparser in choices.items():
        assert f"datamining-skill {command}" in readme, command
        for action in subparser._actions:
            for option in action.option_strings:
                if option.startswith("--") and option not in ("--help", "--log-level", "--no-logs", "--debug"):
                    assert option in readme, (command, option)


def test_the_tool_reference_covers_every_tool_and_argument() -> None:
    reference = (ROOT / "docs" / "mcp-tools.md").read_text(encoding="utf-8")

    for definition in tool_definitions():
        assert f"`{definition.name}`" in reference, definition.name
        for argument in definition.input_schema["properties"]:
            assert f"`{argument}`" in reference, (definition.name, argument)


def test_the_changelog_and_readme_agree_on_python_versions() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    versions = re.findall(r'"Programming Language :: Python :: (3\.\d+)"', pyproject)
    matrix = re.search(r"python-version: \[([^\]]+)\]", ci)

    assert matrix is not None
    assert versions == re.findall(r"\d+\.\d+", matrix.group(1))  # every supported version is tested
    assert re.search(r'requires-python = ">=' + re.escape(versions[0]), pyproject)


# --------------------------------------------------------------------------- workflows


WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_action_is_pinned_to_a_commit_and_permissions_are_declared(path: Path) -> None:
    text = path.read_text(encoding="utf-8")

    uses = re.findall(r"^\s*(?:-\s+)?uses:\s*(\S+)", text, flags=re.MULTILINE)

    assert uses, path.name
    assert all(re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", reference) for reference in uses), uses
    assert re.search(r"^permissions:", text, flags=re.MULTILINE), "declare permissions at the top level"
    assert "pull_request_target" not in text
    assert "persist-credentials: false" in text  # a checkout does not leave the token in .git/config


def test_the_release_workflow_publishes_through_trusted_publishing_only() -> None:
    text = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert "id-token: write" in text and "PYPI_API_TOKEN" not in text and "password:" not in text
    assert "vars.PUBLISH_TO_PYPI" in text  # publishing is an explicit opt-in


def test_community_files_exist() -> None:
    for relative in (
        ".github/dependabot.yml",
        ".github/ISSUE_TEMPLATE/bug_report.yml",
        ".github/ISSUE_TEMPLATE/feature_request.yml",
        ".github/ISSUE_TEMPLATE/config.yml",
        ".github/pull_request_template.md",
        "CODE_OF_CONDUCT.md",
        "LICENSE",
        "MANIFEST.in",
        "examples/contacts.csv",
    ):
        assert (ROOT / relative).is_file(), relative


# ---------------------------------------------------------------------- built distributions


def build_fake_distributions(directory: Path, *, wheel_files: list[str], sdist_files: list[str]) -> None:
    with zipfile.ZipFile(directory / "datamining_skill-1.0.0-py3-none-any.whl", "w") as wheel:
        for name in wheel_files:
            wheel.writestr(name, "x")
    with tarfile.open(directory / "datamining_skill-1.0.0.tar.gz", "w:gz") as sdist:
        for name in sdist_files:
            payload = b"x"
            member = tarfile.TarInfo(f"datamining_skill-1.0.0/{name}")
            member.size = len(payload)
            sdist.addfile(member, io.BytesIO(payload))


def test_a_complete_pair_of_distributions_passes_the_check(state_dir: Path) -> None:
    module = load_script("check_distributions")
    build_fake_distributions(
        state_dir,
        wheel_files=[*module.WHEEL_REQUIRED, "datamining_skill-1.0.0.dist-info/licenses/LICENSE"],
        sdist_files=list(module.SDIST_REQUIRED),
    )

    assert module.check(state_dir) == []
    assert module.main(["check_distributions.py", str(state_dir)]) == 0


def test_missing_and_unwanted_files_are_reported(state_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    module = load_script("check_distributions")
    build_fake_distributions(
        state_dir,
        wheel_files=["datamining_skill/cli.py", "datamining_skill/.scratch/run.sqlite3"],
        sdist_files=["README.md", ".scratch/leak.sqlite3"],
    )

    problems = module.check(state_dir)

    assert "wheel: datamining_skill/py.typed is missing" in problems
    assert "wheel: the licence is not packaged" in problems
    assert "sdist: LICENSE is missing" in problems
    assert any(problem.startswith("wheel: unwanted") for problem in problems)
    assert any(problem.startswith("sdist: unwanted") for problem in problems)
    assert module.main(["check_distributions.py", str(state_dir)]) == 1
    assert "error: " in capsys.readouterr().err


def test_the_check_needs_exactly_one_wheel_and_one_sdist(state_dir: Path) -> None:
    module = load_script("check_distributions")
    (state_dir / "stray.tar.gz").write_bytes(gzip.compress(b"x"))

    assert module.check(state_dir) == ["expected one wheel, found 0"]
