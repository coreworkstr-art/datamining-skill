"""Command-line interface smoke tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from datamining_skill import InvalidConfigurationException, MiningSummary
from datamining_skill.cli import ALLOWED_DIRS_ENV, _allowed_dirs, main
from tests.conftest import WriteFile


def test_profile_command_prints_metadata_json(
    write_file: WriteFile, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_file("t.csv", "a,b\n1,2\n3,4\n")

    assert main(["profile", str(path), "--no-logs"]) == 0

    metadata = json.loads(capsys.readouterr().out)
    assert metadata["format"] == "csv"
    assert metadata["records"]["count"] == 2


def test_unsupported_input_exits_with_code_2(
    write_file: WriteFile, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_file("t.bin", b"\x00\x01" * 64)

    assert main(["profile", str(path), "--no-logs"]) == 2
    assert "Unsupported data format" in capsys.readouterr().err


def test_missing_file_exits_with_code_3(
    write_file: WriteFile, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_file("t.csv", "a,b\n1,2\n")
    path.unlink()

    assert main(["profile", str(path), "--no-logs"]) == 3
    assert "unavailable" in capsys.readouterr().err




def make_contacts(path: Path, rows: int = 3000) -> list[str]:
    path.write_text(
        "id,note\n" + "".join(f"{i},write to emp{i}@internal.corp.test soon\n" for i in range(rows)),
        encoding="utf-8",
    )
    return [f"emp{i}@internal.corp.test" for i in range(rows)]


def run_cli(*args: str) -> int:
    return main([*args, "--no-logs"])


def test_mine_writes_results_and_prints_a_summary(
    state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    expected = make_contacts(state_dir / "in.csv")

    code = run_cli("mine", str(state_dir / "in.csv"), str(state_dir / "out.csv"), "--workspace", str(state_dir))

    summary = json.loads(capsys.readouterr().out)
    assert code == 0
    assert summary["records_written"] == len(expected) and summary["resumed"] is False
    assert (state_dir / "out.csv").read_text().splitlines() == ["email", *expected]


def test_mine_is_idempotent_and_overwrite_starts_over(
    state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    expected = make_contacts(state_dir / "in.csv")
    args = ("mine", str(state_dir / "in.csv"), str(state_dir / "out.csv"), "--workspace", str(state_dir))

    assert run_cli(*args) == 0
    capsys.readouterr()
    assert run_cli(*args) == 0  # same job again: resumes, finds nothing left
    again = json.loads(capsys.readouterr().out)
    assert again["resumed"] is True and again["chunks_processed"] == 0
    assert run_cli(*args, "--overwrite") == 0
    fresh = json.loads(capsys.readouterr().out)
    assert fresh["resumed"] is False and fresh["records_written"] == len(expected)
    assert (state_dir / "out.csv").read_text().splitlines() == ["email", *expected]  # no duplicates


def test_mine_refuses_to_clobber_an_unrelated_output(
    state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_contacts(state_dir / "in.csv")
    (state_dir / "out.csv").write_text("retained export\n")

    code = run_cli("mine", str(state_dir / "in.csv"), str(state_dir / "out.csv"), "--workspace", str(state_dir))

    assert code == 3 and "already exists" in capsys.readouterr().err
    assert (state_dir / "out.csv").read_text() == "retained export\n"


def test_mine_with_a_custom_pattern_and_fields(state_dir: Path) -> None:
    make_contacts(state_dir / "in.csv", rows=50)

    code = run_cli(
        "mine", str(state_dir / "in.csv"), str(state_dir / "out.jsonl"), "--workspace", str(state_dir),
        "--pattern", r"emp(\d+)@", "--fields", "number",
    )

    assert code == 0
    rows = [json.loads(line) for line in (state_dir / "out.jsonl").read_text().splitlines()]
    assert [row["number"] for row in rows] == [str(i) for i in range(50)]


def test_mine_exit_codes_for_bad_input(state_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (state_dir / "blob.bin").write_bytes(b"\x00\x01\x02" * 100)

    unsupported = run_cli("mine", str(state_dir / "blob.bin"), str(state_dir / "o.csv"), "--workspace", str(state_dir))
    missing = run_cli("mine", str(state_dir / "nope.csv"), str(state_dir / "o2.csv"), "--workspace", str(state_dir))

    assert (unsupported, missing) == (2, 3)
    assert "error:" in capsys.readouterr().err


def test_mine_exits_4_when_chunks_failed(
    state_dir: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    failed = MiningSummary("o.csv", False, 0, 10, 0, 9, 1, 100, 0.5)
    monkeypatch.setattr("datamining_skill.cli.run_mining", lambda *a, **k: failed)

    code = run_cli("mine", "a.csv", "o.csv")

    assert code == 4
    assert json.loads(capsys.readouterr().out)["chunks_failed"] == 1




def test_allowed_dirs_come_from_flags_then_environment_then_cwd(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = state_dir / "a", state_dir / "b"
    monkeypatch.delenv(ALLOWED_DIRS_ENV, raising=False)
    monkeypatch.chdir(state_dir)

    assert _allowed_dirs([str(first)]) == [first]
    monkeypatch.setenv(ALLOWED_DIRS_ENV, os.pathsep.join([str(first), str(second)]))
    assert _allowed_dirs([]) == [first, second]
    assert _allowed_dirs([str(second)]) == [second]  # an explicit flag wins
    monkeypatch.delenv(ALLOWED_DIRS_ENV)
    assert _allowed_dirs([]) == [state_dir.resolve()]


def test_the_filesystem_root_is_never_an_implicit_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ALLOWED_DIRS_ENV, raising=False)
    monkeypatch.chdir(Path(tempfile.gettempdir()).anchor)

    with pytest.raises(InvalidConfigurationException, match="filesystem root"):
        _allowed_dirs([])


def test_the_module_entry_point_runs() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "datamining_skill.cli", "--help"],
        capture_output=True, text=True, check=False, timeout=60,
    )

    assert result.returncode == 0
    assert all(command in result.stdout.replace("\r\n", "\n").replace(\r\n, \n) for command in ("profile", "mine", "mcp"))




def assert_clean_failure(
    capsys: pytest.CaptureFixture[str], code: int, expected_code: int = 3, mentions: str = ""
) -> str:
    err = capsys.readouterr().err
    assert code == expected_code, err
    assert err.startswith("error: ") and "Traceback" not in err
    assert mentions in err
    return err


def test_a_read_only_output_is_a_clean_error_not_a_traceback(
    state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_contacts(state_dir / "in.csv")
    locked = state_dir / "locked.csv"
    locked.write_text("held by another team\n")
    locked.chmod(0o400)
    if os.access(locked, os.W_OK):
        pytest.skip("this account can write to read-only files (running as root?)")
    try:
        code = run_cli("mine", str(state_dir / "in.csv"), str(locked), "--workspace", str(state_dir), "--overwrite")
    finally:
        locked.chmod(0o600)

    assert_clean_failure(capsys, code, mentions="locked.csv")
    assert locked.read_text() == "held by another team\n"


@pytest.mark.parametrize("blocker", ["workspace-is-a-file", "scratch-is-a-file"])
def test_an_unusable_workspace_is_a_clean_error(
    state_dir: Path, capsys: pytest.CaptureFixture[str], blocker: str
) -> None:
    make_contacts(state_dir / "in.csv")
    workspace = state_dir / "ws"
    if blocker == "workspace-is-a-file":
        workspace.write_text("not a directory")
    else:
        workspace.mkdir()
        (workspace / ".scratch").write_text("not a directory")

    code = run_cli("mine", str(state_dir / "in.csv"), str(state_dir / "out.csv"), "--workspace", str(workspace))

    assert_clean_failure(capsys, code)


def test_a_config_that_is_not_utf8_is_a_clean_error(
    state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_contacts(state_dir / "in.csv")
    config = state_dir / "latin1.toml"
    config.write_bytes(b"[profiler]\nwindow_bytes = 262144\n# caf\xe9 \xff\xfe\n")

    code = run_cli("profile", str(state_dir / "in.csv"), "--config", str(config))

    assert_clean_failure(capsys, code, mentions="not valid UTF-8")


@pytest.mark.skipif(os.name != "nt", reason="a 260-character limit exists on Windows only")
def test_a_path_beyond_the_windows_limit_is_a_clean_error(
    state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_contacts(state_dir / "in.csv")
    too_deep = state_dir.joinpath(*["d" * 30] * 12)

    code = run_cli("mine", str(state_dir / "in.csv"), str(state_dir / "out.csv"), "--workspace", str(too_deep))

    assert_clean_failure(capsys, code)


def test_debug_adds_the_traceback_without_changing_the_exit_code(
    state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = state_dir / "absent.csv"

    quiet = run_cli("profile", str(missing))
    quiet_err = capsys.readouterr().err
    loud = run_cli("profile", str(missing), "--debug")
    loud_err = capsys.readouterr().err

    assert quiet == loud == 3
    assert "Traceback" not in quiet_err
    assert "Traceback" in loud_err and loud_err.count("error: ") == 1


def test_unexpected_exception_is_summarised_without_debug(
    state_dir: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("boom while reading j.okafor@ap-south.corp.test")

    monkeypatch.setattr("datamining_skill.cli.run_mining", explode)

    quiet = run_cli("mine", "in.csv", "out.csv")
    quiet_err = capsys.readouterr().err
    loud = run_cli("mine", "in.csv", "out.csv", "--debug")
    loud_err = capsys.readouterr().err

    assert quiet == loud == 1
    assert "internal error (RuntimeError)" in quiet_err and "--debug" in quiet_err
    assert "Traceback" not in quiet_err and "j.okafor" not in quiet_err  # no message text, which can quote data
    assert "Traceback" in loud_err and "boom while reading" in loud_err


def test_a_closed_output_pipe_exits_quietly_with_141(state_dir: Path) -> None:
    make_contacts(state_dir / "in.csv", rows=500)
    process = subprocess.Popen(
        [sys.executable, "-m", "datamining_skill.cli", "profile", str(state_dir / "in.csv"), "--no-logs"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout.replace("\r\n", "\n").replace(\r\n, \n) is not None and process.stderr.replace("\r\n", "\n").replace(\r\n, \n) is not None
    process.stdout.replace("\r\n", "\n").replace(\r\n, \n).close()  # the reader (think `| head -0`) goes away before anything is written
    stderr = process.stderr.replace("\r\n", "\n").replace(\r\n, \n).read()
    process.wait(timeout=60)
    process.stderr.replace("\r\n", "\n").replace(\r\n, \n).close()

    assert process.returncode == 141
    assert b"Traceback" not in stderr and b"Exception ignored" not in stderr


def test_partial_mining_failures_report_why(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    failed = MiningSummary("o.csv", False, 0, 10, 0, 9, 1, 100, 0.5, "chunk 4: the file is shorter than the byte range")
    monkeypatch.setattr("datamining_skill.cli.run_mining", lambda *a, **k: failed)

    code = run_cli("mine", "a.csv", "o.csv")

    err = capsys.readouterr().err
    assert code == 4 and "First failure: chunk 4: the file is shorter" in err
