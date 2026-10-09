"""``clean``: finished jobs leave state behind, and clearing it must never hurt a live job."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from datamining_skill import InvalidConfigurationException, MiningProgress, run_mining
from datamining_skill.application.options import MiningOptions
from datamining_skill.bootstrap import RunLayout, run_layout
from datamining_skill.cli import main
from datamining_skill.infrastructure.cleanup import clean_workspace
from datamining_skill.infrastructure.job_lock import JobLock
from tests.support import load_simulation, make_directory_link, remove_link
from tests.test_orchestration import Crash
from tests.test_unique_mining import SMALL, build_source

sim = load_simulation()


def mine(workspace: Path, name: str = "out.csv", **kwargs: object) -> None:
    source = workspace / "in.csv"
    if not source.exists():
        build_source(source)
    run_mining(
        source,
        workspace / name,
        workspace=workspace,
        chunking_config=SMALL,
        memory_provider=sim.FixedMemory(source.stat().st_size // 5),
        **kwargs,  # type: ignore[arg-type]
    )


def layout(workspace: Path, name: str = "out.csv", options: MiningOptions | None = None) -> RunLayout:
    return run_layout(workspace, workspace / "in.csv", workspace / name, (options or MiningOptions()).fingerprint())


def test_a_finished_jobs_state_and_scratch_files_are_removed_but_not_its_result(state_dir: Path) -> None:
    mine(state_dir)
    paths = layout(state_dir)
    assert paths.state_path.exists()
    result_before = (state_dir / "out.csv").read_bytes()

    (outcome,) = clean_workspace(state_dir)

    assert outcome.finished and outcome.removed and outcome.size_bytes > 0
    assert not paths.state_path.exists() and not paths.scratch_dir.exists()
    assert (state_dir / "out.csv").read_bytes() == result_before
    assert paths.lock_path.exists()  # the lock file stays, so a racing process can never lock a copy


def test_an_unfinished_job_is_kept_unless_asked_for(state_dir: Path) -> None:
    build_source(state_dir / "in.csv")
    reports = 0

    def stop_early(_: MiningProgress) -> None:
        nonlocal reports
        reports += 1
        if reports == 3:
            raise Crash

    with pytest.raises(Crash):
        mine(state_dir, on_progress=stop_early)
    paths = layout(state_dir)

    (kept,) = clean_workspace(state_dir)
    assert not kept.finished and not kept.removed and "can still resume" in str(kept.reason)
    assert paths.state_path.exists()

    (removed,) = clean_workspace(state_dir, include_unfinished=True)
    assert removed.removed and not paths.state_path.exists()


def test_a_job_running_in_another_process_is_skipped(state_dir: Path) -> None:
    mine(state_dir)
    paths = layout(state_dir)

    with JobLock(paths.lock_path):  # what a running job holds
        (outcome,) = clean_workspace(state_dir, include_unfinished=True)

    assert not outcome.removed and outcome.reason == "running in another process"
    assert paths.state_path.exists()


def test_a_dry_run_removes_nothing_and_reports_the_size(state_dir: Path) -> None:
    mine(state_dir)
    paths = layout(state_dir)

    (outcome,) = clean_workspace(state_dir, dry_run=True)

    assert not outcome.removed and outcome.finished and outcome.size_bytes > 0 and outcome.reason is None
    assert paths.state_path.exists()


def test_other_files_in_the_scratch_folder_are_left_alone(state_dir: Path) -> None:
    mine(state_dir)
    scratch = state_dir / ".scratch"
    (scratch / "mining-notes.txt").write_text("mine")
    (scratch / "other.sqlite3").write_text("not a job")
    (scratch / "mining-zzzz.sqlite3").write_text("name does not match a job")

    clean_workspace(state_dir)

    assert sorted(p.name for p in scratch.iterdir() if p.suffix != ".lock") == [
        "mining-notes.txt",
        "mining-zzzz.sqlite3",
        "other.sqlite3",
    ]


def test_cleaning_an_empty_workspace_reports_no_jobs(state_dir: Path) -> None:
    assert clean_workspace(state_dir) == []


def test_two_finished_jobs_are_both_cleaned(state_dir: Path) -> None:
    mine(state_dir, "first.csv")
    mine(state_dir, "second.csv", unique=True)

    outcomes = clean_workspace(state_dir)

    assert len(outcomes) == 2 and all(o.removed for o in outcomes)


def test_a_repeated_call_after_cleaning_needs_overwrite_because_the_output_exists(state_dir: Path) -> None:
    mine(state_dir)
    clean_workspace(state_dir)

    with pytest.raises(InvalidConfigurationException, match="already exists"):
        mine(state_dir)
    mine(state_dir, overwrite=True)  # starts over and replaces the result


def test_a_scratch_folder_that_leads_out_of_the_workspace_is_refused(state_dir: Path) -> None:
    outside = state_dir / "outside"
    outside.mkdir()
    workspace = state_dir / "ws"
    workspace.mkdir()
    if not make_directory_link(workspace / ".scratch", outside):
        pytest.skip("symbolic links are not available to this user")
    try:
        with pytest.raises(InvalidConfigurationException, match="leads outside the workspace"):
            clean_workspace(workspace)
    finally:
        remove_link(workspace / ".scratch")


def test_the_clean_command_prints_what_it_removed(state_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    mine(state_dir)

    assert main(["clean", "--workspace", str(state_dir), "--dry-run", "--no-logs"]) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["dry_run"] is True and dry["removed_jobs"] == 0 and dry["would_free_bytes"] > 0

    assert main(["clean", "--workspace", str(state_dir), "--no-logs"]) == 0
    done = json.loads(capsys.readouterr().out)
    assert done["removed_jobs"] == 1 and done["freed_bytes"] == dry["would_free_bytes"]
    assert done["jobs"][0]["removed"] is True


def test_a_unique_job_gives_its_keys_back_when_it_finishes(state_dir: Path) -> None:
    options = MiningOptions.of(unique=True)
    mine(state_dir, unique=True)

    with closing(sqlite3.connect(layout(state_dir, options=options).state_path)) as raw:
        assert raw.execute("SELECT COUNT(*) FROM seen_records").fetchone()[0] == 0
        assert raw.execute("PRAGMA freelist_count").fetchone()[0] == 0  # compacted, not just emptied
