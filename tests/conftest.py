"""Shared fixtures.

Privacy by design: test datasets are written to a project-local ``.scratch``
directory (git-ignored) and removed when the session ends, rather than being left
behind in the operating system's shared temporary folder.
"""

from __future__ import annotations

import shutil
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from datamining_skill import DataProfiler, StateManager, create_profiler

SCRATCH_ROOT = Path(__file__).resolve().parent.parent / ".scratch"

WriteFile = Callable[[str, bytes | str], Path]


@pytest.fixture(scope="session")
def workspace() -> Iterator[Path]:
    """Session-wide scratch directory, deleted on teardown."""
    SCRATCH_ROOT.mkdir(exist_ok=True)
    root = SCRATCH_ROOT / f"session-{uuid.uuid4().hex[:8]}"
    root.mkdir()
    yield root
    shutil.rmtree(root, ignore_errors=True)
    try:
        SCRATCH_ROOT.rmdir()
    except OSError:
        pass  # another session is still using it


@pytest.fixture
def write_file(workspace: Path) -> Iterator[WriteFile]:
    """Factory writing ``content`` to a uniquely named directory; cleaned per test."""
    directory = workspace / uuid.uuid4().hex
    directory.mkdir()

    def _write(name: str, content: bytes | str) -> Path:
        path = directory / name
        data = content.encode("utf-8") if isinstance(content, str) else content
        path.write_bytes(data)
        return path

    yield _write
    shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def profiler() -> DataProfiler:
    return create_profiler()


OpenState = Callable[..., StateManager]


@pytest.fixture
def state_dir(workspace: Path) -> Iterator[Path]:
    """A private directory for one test's state database, output and scratch files.

    Teardown removes it *without* ``ignore_errors``: on Windows a connection or
    file handle that a test leaked keeps the files open and fails the removal, so
    a leak surfaces as a test error instead of passing silently.
    """
    directory = workspace / f"run-{uuid.uuid4().hex[:8]}"
    directory.mkdir()
    yield directory
    shutil.rmtree(directory)


@pytest.fixture
def open_state(state_dir: Path) -> Iterator[OpenState]:
    """Factory for ``StateManager`` instances, all closed on teardown (even on failure)."""
    opened: list[StateManager] = []

    def _open(name: str = "state.sqlite3", **kwargs: Any) -> StateManager:
        kwargs.setdefault("allowed_roots", [SCRATCH_ROOT])
        manager = StateManager(state_dir / name, **kwargs)
        opened.append(manager)
        return manager

    try:
        yield _open
    finally:
        for manager in opened:
            manager.close()
