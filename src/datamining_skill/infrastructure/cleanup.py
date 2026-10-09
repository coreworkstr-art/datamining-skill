"""Housekeeping for a workspace's ``.scratch`` folder: finished jobs leave files behind.

Each mining job keeps a small state database (and a lock file) under ``<workspace>/.scratch`` so
that it can resume and so that repeating a finished call is answered at once. ``clean`` removes
the state and scratch files of finished jobs (or, with ``include_unfinished``, of all jobs that
are not running). It never touches a job that another process is running: that job's lock is
held, so it is skipped. Result files are never touched.
"""

from __future__ import annotations

import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from datamining_skill.domain.exceptions import JobLockedException, StateStoreException
from datamining_skill.infrastructure.job_lock import JobLock
from datamining_skill.infrastructure.paths import confined_child
from datamining_skill.infrastructure.state_manager import StateManager

_JOB_STEM = re.compile(r"mining-[0-9a-f]{16}")
_STATE_SUFFIXES = ("", "-wal", "-shm")
_SAMPLE_MAX_AGE_SECONDS = 3600


@dataclass(frozen=True, slots=True)
class JobOutcome:
    """What ``clean`` did, or would do, for one job."""

    job: str
    finished: bool
    size_bytes: int
    removed: bool
    reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "job": self.job,
            "finished": self.finished,
            "size_bytes": self.size_bytes,
            "removed": self.removed,
            "reason": self.reason,
        }


def clean_workspace(
    workspace: Path, *, include_unfinished: bool = False, dry_run: bool = False
) -> list[JobOutcome]:
    """Remove the files of finished jobs under ``workspace``; report every job found."""
    root = confined_child(workspace, ".scratch")
    if not root.is_dir():
        return []
    outcomes = [
        _clean_job(root, state_file.name.removesuffix(".sqlite3"), include_unfinished, dry_run)
        for state_file in sorted(root.glob("mining-*.sqlite3"))
        if _JOB_STEM.fullmatch(state_file.name.removesuffix(".sqlite3"))
    ]
    if not dry_run:
        _remove_old_samples(root)
    return outcomes


def _clean_job(root: Path, stem: str, include_unfinished: bool, dry_run: bool) -> JobOutcome:
    try:
        with JobLock(root / f"{stem}.lock"):
            finished = _is_finished(root / f"{stem}.sqlite3", root)
            size = _job_size(root, stem)
            if not finished and not include_unfinished:
                return JobOutcome(stem, False, size, False, "unfinished: it can still resume")
            if not dry_run:
                for suffix in _STATE_SUFFIXES:
                    Path(f"{root / stem}.sqlite3{suffix}").unlink(missing_ok=True)
                shutil.rmtree(root / stem, ignore_errors=True)
            return JobOutcome(stem, finished, size, not dry_run)
    except JobLockedException:
        return JobOutcome(stem, False, 0, False, "running in another process")


def _is_finished(state_file: Path, root: Path) -> bool:
    try:
        with StateManager(state_file, allowed_roots=[root], recover_orphans=False) as state:
            return state.is_initialized() and state.is_complete()
    except StateStoreException:
        return False  # unreadable: only removed on request


def _job_size(root: Path, stem: str) -> int:
    total = 0
    for suffix in _STATE_SUFFIXES:
        path = Path(f"{root / stem}.sqlite3{suffix}")
        if path.is_file():
            total += path.stat().st_size
    directory = root / stem
    if directory.is_dir():
        total += sum(item.stat().st_size for item in directory.rglob("*") if item.is_file())
    return total


def _remove_old_samples(root: Path) -> None:
    """Delete profile samples a crashed process left behind; recent ones may still be in use."""
    for sample in root.glob("profile-*.sample*"):
        try:
            if time.time() - sample.stat().st_mtime > _SAMPLE_MAX_AGE_SECONDS:
                sample.unlink(missing_ok=True)
        except OSError:
            continue
