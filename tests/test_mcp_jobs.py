"""Background mining jobs, status, cancellation and result previews through the MCP tools."""

from __future__ import annotations

import io
import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from datamining_skill import MiningProgress, MiningSummary, create_mcp_server
from datamining_skill.domain.exceptions import StateStoreException
from datamining_skill.infrastructure.mcp_tools import MiningTools, WorkspacePolicy

Runner = Callable[..., MiningSummary]


def summary(**changes: Any) -> MiningSummary:
    values: dict[str, Any] = {
        "output_name": "out.csv",
        "resumed": False,
        "recovered_orphans": 0,
        "chunks_total": 3,
        "chunks_previously_completed": 0,
        "chunks_processed": 3,
        "chunks_failed": 0,
        "records_written": 30,
        "duration_seconds": 0.1,
    }
    values.update(changes)
    return MiningSummary(**values)


def make_tools(state_dir: Path, mine: Runner) -> MiningTools:
    (state_dir / "in.csv").write_text("id,email\n1,a@internal.corp.test\n", encoding="utf-8")
    return MiningTools(WorkspacePolicy([state_dir]), profile=lambda path: {}, mine=mine)


def start(tools: MiningTools, **extra: Any) -> dict[str, Any]:
    outcome = tools.call("mine_dataset", {"path": "in.csv", "output_path": "out.csv", "wait": False, **extra})
    assert not outcome.is_error, outcome.error
    assert outcome.data is not None
    return outcome.data


def wait_for(tools: MiningTools, job_id: str, status: str) -> dict[str, Any]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        outcome = tools.call("mining_status", {"job_id": job_id})
        assert outcome.data is not None
        if outcome.data["status"] == status:
            return outcome.data
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} never reached {status}")


class Gate:
    """A runner that reports progress, then waits until the test lets it finish or cancels it."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.reported = threading.Event()

    def __call__(self, source: Path, output: Path, *, on_progress: Callable[[MiningProgress], None], **_: Any) -> MiningSummary:
        on_progress(MiningProgress(1, 3, 10))
        self.reported.set()
        while not self.release.wait(0.005):
            on_progress(MiningProgress(1, 3, 10))  # a cancelled job raises from here
        return summary()


def test_a_background_job_reports_progress_and_then_its_summary(state_dir: Path) -> None:
    gate = Gate()
    tools = make_tools(state_dir, gate)

    job = start(tools)
    assert job["status"] == "running" and job["job"] == "in.csv -> out.csv"
    assert gate.reported.wait(5)
    running = wait_for(tools, job["job_id"], "running")
    assert (running["chunks_done"], running["chunks_total"], running["records_written"]) == (1, 3, 10)

    gate.release.set()
    done = wait_for(tools, job["job_id"], "succeeded")

    assert done["result"]["records_written"] == 30 and done["result"]["output_path"] == "out.csv"
    assert done["error"] is None and done["finished_at"] is not None


def test_listing_without_an_id_shows_every_job(state_dir: Path) -> None:
    gate = Gate()
    tools = make_tools(state_dir, gate)
    job = start(tools)

    listing = tools.call("mining_status", {})

    assert listing.data is not None and [j["job_id"] for j in listing.data["jobs"]] == [job["job_id"]]
    gate.release.set()
    wait_for(tools, job["job_id"], "succeeded")


def test_cancelling_stops_the_job_and_says_how_to_resume(state_dir: Path) -> None:
    gate = Gate()
    tools = make_tools(state_dir, gate)
    job = start(tools)
    assert gate.reported.wait(5)

    cancelled = tools.call("cancel_mining", {"job_id": job["job_id"]})
    assert cancelled.data is not None

    final = wait_for(tools, job["job_id"], "cancelled")
    assert "resume" in final["error"] and final["result"] is None


def test_the_same_job_cannot_be_started_twice_while_it_runs(state_dir: Path) -> None:
    gate = Gate()
    tools = make_tools(state_dir, gate)
    job = start(tools)

    second = tools.call("mine_dataset", {"path": "in.csv", "output_path": "out.csv", "wait": False})
    other_settings = tools.call("mine_dataset", {"path": "in.csv", "output_path": "out.csv", "wait": False, "unique": True})

    assert second.is_error and job["job_id"] in str(second.error) and "already running" in str(second.error)
    assert not other_settings.is_error  # other settings are another job
    gate.release.set()


def test_the_number_of_background_jobs_is_limited(state_dir: Path) -> None:
    gate = Gate()
    tools = make_tools(state_dir, gate)
    for index in range(4):
        (state_dir / f"out{index}.csv").touch()
        assert not tools.call("mine_dataset", {"path": "in.csv", "output_path": f"out{index}.csv", "wait": False}).is_error

    fifth = tools.call("mine_dataset", {"path": "in.csv", "output_path": "out4.csv", "wait": False})

    assert fifth.is_error and "already running" in str(fifth.error)
    gate.release.set()


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (StateStoreException("the state database is unusable"), "StateStoreException: the state database is unusable"),
        (PermissionError(13, "Permission denied", "C:/restricted/in.csv"), "file system error: Permission denied"),
        (RuntimeError("boom with /srv/hr/exports"), "internal error (RuntimeError); see the server log"),
    ],
)
def test_a_failing_job_reports_a_safe_error(state_dir: Path, failure: Exception, expected: str) -> None:
    def failing(*args: Any, **kwargs: Any) -> MiningSummary:
        raise failure

    tools = make_tools(state_dir, failing)

    job = start(tools)
    final = wait_for(tools, job["job_id"], "failed")

    assert final["error"] == expected and "restricted" not in final["error"] and "hr/exports" not in final["error"]


def test_a_job_whose_chunks_failed_is_failed_with_the_first_reason(state_dir: Path) -> None:
    def partial(*args: Any, **kwargs: Any) -> MiningSummary:
        return summary(chunks_failed=1, chunks_processed=2, first_error="chunk 2: the file is shorter than the byte range")

    tools = make_tools(state_dir, partial)

    final = wait_for(tools, start(tools)["job_id"], "failed")

    assert "1 chunk(s) failed" in final["error"] and "First failure: chunk 2" in final["error"]
    assert final["result"]["succeeded"] is False


def test_unknown_jobs_are_reported_to_the_caller(state_dir: Path) -> None:
    tools = make_tools(state_dir, Gate())

    assert "unknown job 'nope'" in str(tools.call("mining_status", {"job_id": "nope"}).error)
    assert "unknown job 'nope'" in str(tools.call("cancel_mining", {"job_id": "nope"}).error)
    assert tools.call("cancel_mining", {}).is_error  # job_id is required


def test_wait_must_be_a_boolean(state_dir: Path) -> None:
    tools = make_tools(state_dir, Gate())

    outcome = tools.call("mine_dataset", {"path": "in.csv", "output_path": "out.csv", "wait": "no"})

    assert "'wait' must be true or false" in str(outcome.error)


def test_cancelling_a_finished_job_changes_nothing(state_dir: Path) -> None:
    tools = make_tools(state_dir, lambda *a, **k: summary())
    job = start(tools)
    wait_for(tools, job["job_id"], "succeeded")

    again = tools.call("cancel_mining", {"job_id": job["job_id"]})

    assert again.data is not None and again.data["status"] == "succeeded"


def test_an_invalid_pattern_fails_at_once_not_inside_the_background_job(state_dir: Path) -> None:
    (state_dir / "in.csv").write_text("id,email\n1,a@b.test\n")
    server = create_mcp_server([state_dir], allow_custom_patterns=True)
    tools = server._tools

    outcome = tools.call("mine_dataset", {"path": "in.csv", "output_path": "o.csv", "pattern": r"(\d+\.){3}", "wait": False})

    assert outcome.is_error and "non-capturing group" in str(outcome.error)
    assert tools.call("mining_status", {}).data == {"jobs": []}


def test_the_full_flow_over_the_protocol_with_the_real_miner(state_dir: Path) -> None:
    rows = "id,note\n" + "".join(f"{i},mail Emp{i % 10}@Internal.Corp.TEST\n" for i in range(500))
    (state_dir / "in.csv").write_text(rows, encoding="utf-8")
    server = create_mcp_server([state_dir])

    def exchange(*messages: dict[str, Any]) -> list[dict[str, Any]]:
        stdin = io.StringIO("\n".join(json.dumps(m) for m in messages) + "\n")
        stdout = io.StringIO()
        server.serve(stdin, stdout)
        return [json.loads(line) for line in stdout.getvalue().splitlines()]

    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}

    def call(request_id: int, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        message = {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
        result: dict[str, Any] = exchange(init, message)[1]["result"]
        return result

    started = call(2, "mine_dataset", {"path": "in.csv", "output_path": "out.csv", "unique": True, "lowercase": True, "wait": False})
    job_id = started["structuredContent"]["job_id"]
    deadline = time.monotonic() + 15
    while True:
        status = call(3, "mining_status", {"job_id": job_id})["structuredContent"]
        if status["status"] != "running":
            break
        assert time.monotonic() < deadline
        time.sleep(0.02)

    assert status["status"] == "succeeded" and status["result"]["records_written"] == 10
    preview = call(4, "preview_result", {"path": "out.csv", "rows": 3})["structuredContent"]
    assert preview["columns"] == ["email"] and preview["rows"][0] == ["emp0@internal.corp.test"] and preview["has_more"] is True


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"path": "r.csv", "rows": 0}, "'rows' must be at least 1"),
        ({"path": "r.csv", "rows": 1000}, "'rows' must be at most 200"),
        ({"path": "r.csv", "rows": "3"}, "'rows' must be an integer"),
        ({"path": "r.csv", "rows": True}, "'rows' must be an integer"),
        ({"path": "notes.txt"}, "must be a .csv, .jsonl or .ndjson result file"),
        ({"path": "../elsewhere.csv"}, "allowed directories"),
        ({"path": "ghost.csv"}, "existing regular file"),
    ],
)
def test_preview_rejects_bad_arguments(state_dir: Path, arguments: dict[str, Any], message: str) -> None:
    (state_dir / "r.csv").write_text("email\na@b.test\n")
    (state_dir / "notes.txt").write_text("not a result\n")
    tools = make_tools(state_dir, lambda *a, **k: summary())

    outcome = tools.call("preview_result", arguments)

    assert outcome.is_error and message in str(outcome.error), outcome


def test_preview_returns_a_json_lines_result(state_dir: Path) -> None:
    (state_dir / "r.jsonl").write_text('{"email": "a@b.test"}\n{"email": "c@d.test"}\n')
    tools = make_tools(state_dir, lambda *a, **k: summary())

    outcome = tools.call("preview_result", {"path": "r.jsonl", "rows": 1})

    assert outcome.data is not None and outcome.data["rows"] == [{"email": "a@b.test"}] and outcome.data["has_more"] is True
