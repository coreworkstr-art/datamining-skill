"""Cross-process locking of a mining job."""

from __future__ import annotations

import json
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from datamining_skill import JobLockedException, create_mcp_server, run_mining
from datamining_skill.bootstrap import run_layout
from datamining_skill.infrastructure.job_lock import JobLock
from tests.conftest import OpenState
from tests.test_mcp_server import Client, call

HOLDER = """
import sys, time
from pathlib import Path
from datamining_skill.infrastructure.job_lock import JobLock
with JobLock(Path(sys.argv[1])):
    print("locked", flush=True)
    time.sleep(60)
"""

SLOW_MINER = """
import json, sys, time
from pathlib import Path
from datamining_skill import run_mining
source, output, workspace, gate = map(Path, sys.argv[1:5])
announced = False
def hold_until_released(progress):
    global announced
    if not announced:
        announced = True
        print("started", flush=True)
        while not gate.exists():
            time.sleep(0.02)
summary = run_mining(source, output, workspace=workspace, on_progress=hold_until_released)
print(json.dumps(summary.to_dict()), flush=True)
"""


def write_contacts(path: Path, rows: int = 3000) -> list[str]:
    path.write_text(
        "id,note\n" + "".join(f"{i},escalate to sys.admin{i}@internal.corp.test\n" for i in range(rows)),
        encoding="utf-8",
    )
    return [f"sys.admin{i}@internal.corp.test" for i in range(rows)]


def test_a_second_holder_is_refused_while_the_first_is_inside(state_dir: Path) -> None:
    lock_file = state_dir / "job.lock"

    with JobLock(lock_file), pytest.raises(JobLockedException, match="already running"), JobLock(lock_file):
        pytest.fail("the second holder must never get in")


def test_lock_is_released_after_the_body_raises(state_dir: Path) -> None:
    lock_file = state_dir / "job.lock"

    with pytest.raises(RuntimeError), JobLock(lock_file):
        raise RuntimeError("worker failed")

    with JobLock(lock_file):  # acquirable again: nothing leaked
        assert lock_file.exists()


def test_lock_file_is_kept_after_release(state_dir: Path) -> None:
    lock_file = state_dir / "job.lock"
    with JobLock(lock_file):
        pass

    assert lock_file.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_the_lock_file_is_owner_only(state_dir: Path) -> None:
    lock_file = state_dir / "job.lock"
    with JobLock(lock_file):
        pass

    assert stat.S_IMODE(lock_file.stat().st_mode) == 0o600


def test_lock_is_dropped_when_the_holder_is_killed(state_dir: Path) -> None:
    lock_file = state_dir / "job.lock"
    holder = subprocess.Popen([sys.executable, "-c", HOLDER, str(lock_file)], stdout=subprocess.PIPE, text=True)
    assert holder.stdout is not None
    try:
        assert holder.stdout.readline().strip() == "locked"
        with pytest.raises(JobLockedException), JobLock(lock_file):
            pass

        holder.kill()  # no cleanup of any kind runs in the holder
        holder.wait(timeout=30)
        holder.stdout.close()

        for _ in range(100):  # the kernel releases handles a moment after the process dies
            try:
                with JobLock(lock_file):
                    break
            except JobLockedException:
                time.sleep(0.05)
        else:
            pytest.fail("the lock of a killed process was never released")
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=30)
            if holder.stdout:
                holder.stdout.close()


def test_second_process_is_refused_and_first_run_is_exact(
    state_dir: Path, open_state: OpenState
) -> None:
    del open_state  # only here so the fixture module is imported consistently
    source, output = state_dir / "contacts.csv", state_dir / "found.csv"
    expected = write_contacts(source)
    gate = state_dir / "release"
    first = subprocess.Popen(
        [sys.executable, "-c", SLOW_MINER, str(source), str(output), str(state_dir), str(gate)],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert first.stdout is not None
    try:
        assert first.stdout.readline().strip() == "started"
        command = [sys.executable, "-m", "datamining_skill.cli", "mine", str(source), str(output), "--workspace", str(state_dir), "--no-logs"]

        plain = subprocess.run(command, capture_output=True, text=True, check=False, timeout=120)
        overwrite = subprocess.run([*command, "--overwrite"], capture_output=True, text=True, check=False, timeout=120)
        (via_mcp,) = Client(create_mcp_server([state_dir])).handshake_and(
            call(2, "mine_dataset", {"path": "contacts.csv", "output_path": "found.csv"})
        )

        for refused in (plain, overwrite):
            assert refused.returncode == 3
            assert "already running in another process" in refused.stderr
            assert refused.stdout == "" and "Traceback" not in refused.stderr
        assert via_mcp["result"]["isError"] is True
        assert "JobLockedException" in via_mcp["result"]["content"][0]["text"]
        # --overwrite must not have deleted the live job's state while it was refused
        assert list((state_dir / ".scratch").glob("mining-*.sqlite3")), "a refused --overwrite wiped the running job"

        gate.write_text("go")
        final = json.loads(first.stdout.readline())
        first.wait(timeout=120)
    finally:
        if first.poll() is None:
            first.kill()
            first.wait(timeout=30)
        first.stdout.close()

    assert final["chunks_failed"] == 0 and final["records_written"] == len(expected)
    assert output.read_text(encoding="utf-8").splitlines() == ["email", *expected]  # exact: nothing lost or duplicated

    again = run_mining(source, output, workspace=state_dir)  # the lock is free once the first run ends
    assert again.resumed is True and again.chunks_processed == 0


def test_the_lock_file_sits_next_to_the_job_state(state_dir: Path) -> None:
    layout = run_layout(state_dir, state_dir / "a.csv", state_dir / "b.csv")

    assert layout.lock_path.parent == layout.state_path.parent
    assert layout.lock_path.suffix == ".lock" and layout.lock_path.stem == layout.state_path.stem
