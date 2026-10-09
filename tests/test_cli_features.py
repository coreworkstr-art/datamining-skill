"""CLI: version, preview, unique/lowercase/JSON-escape options and the finished-job note."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from datamining_skill import __version__
from datamining_skill.cli import main
from datamining_skill.infrastructure.result_preview import MAX_PREVIEW_ROWS, preview_result


def run_cli(*args: str) -> int:
    return main([*args, "--no-logs"])


def write_contacts(path: Path) -> None:
    rows = ["id,contact"] + [f"{i},{'Ada' if i % 2 else 'ADA'}.Hollis@Internal.Corp.TEST" for i in range(40)]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def test_version_flag_prints_the_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stop:
        main(["--version"])

    assert stop.value.code == 0
    assert capsys.readouterr().out.strip() == f"datamining-skill {__version__}"


def test_unique_and_lowercase_give_one_clean_address(state_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_contacts(state_dir / "in.csv")

    code = run_cli(
        "mine", str(state_dir / "in.csv"), str(state_dir / "out.csv"), "--workspace", str(state_dir),
        "--unique", "--lowercase",
    )

    summary = json.loads(capsys.readouterr().out)
    assert code == 0 and summary["records_written"] == 1 and summary["duplicates_skipped"] == 39
    assert (state_dir / "out.csv").read_text().splitlines() == ["email", "ada.hollis@internal.corp.test"]


def test_a_finished_job_prints_a_note_to_stderr(state_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_contacts(state_dir / "in.csv")
    args = ("mine", str(state_dir / "in.csv"), str(state_dir / "out.csv"), "--workspace", str(state_dir))
    assert run_cli(*args) == 0
    capsys.readouterr()

    assert run_cli(*args) == 0

    captured = capsys.readouterr()
    assert json.loads(captured.out)["already_complete"] is True
    assert "already finished" in captured.err and "--overwrite" in captured.err


def test_a_repeated_capturing_group_is_refused_with_the_fix(state_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_contacts(state_dir / "in.csv")

    code = run_cli(
        "mine", str(state_dir / "in.csv"), str(state_dir / "out.csv"), "--workspace", str(state_dir),
        "--pattern", r"(\d+\.){3}\d+",
    )

    assert code == 3 and "non-capturing group" in capsys.readouterr().err
    assert not (state_dir / "out.csv").exists()


def test_json_escapes_option_controls_decoding(state_dir: Path) -> None:
    (state_dir / "in.jsonl").write_text('{"mail": "a\\u0040internal.corp.test"}\n', encoding="utf-8")
    base = ("mine", str(state_dir / "in.jsonl"), str(state_dir / "{}.csv"), "--workspace", str(state_dir))

    def mine(mode: str) -> list[str]:
        args = [a.replace("{}", mode) for a in base]
        assert run_cli(*args, "--json-escapes", mode) == 0
        return (state_dir / f"{mode}.csv").read_text().splitlines()[1:]

    assert mine("auto") == ["a@internal.corp.test"]
    assert mine("on") == ["a@internal.corp.test"]
    assert mine("off") == []


def test_preview_command_prints_the_first_records(state_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (state_dir / "out.csv").write_text("email\n" + "".join(f"emp{i}@internal.corp.test\n" for i in range(50)))

    assert run_cli("preview", str(state_dir / "out.csv"), "--rows", "3") == 0

    shown = json.loads(capsys.readouterr().out)
    assert shown["columns"] == ["email"] and len(shown["rows"]) == 3 and shown["has_more"] is True
    assert shown["rows"][0] == ["emp0@internal.corp.test"]


@pytest.mark.parametrize(
    ("name", "content", "message"),
    [
        ("notes.txt", "x\n", "must end in"),
        ("missing.csv", None, "existing file"),
    ],
)
def test_preview_refuses_other_files(
    state_dir: Path, capsys: pytest.CaptureFixture[str], name: str, content: str | None, message: str
) -> None:
    if content is not None:
        (state_dir / name).write_text(content)

    assert run_cli("preview", str(state_dir / name)) == 3
    assert message in capsys.readouterr().err


def test_preview_of_a_csv_with_quoted_values_and_line_breaks(state_dir: Path) -> None:
    (state_dir / "r.csv").write_text('id,note\n1,"a, b"\n2,"two\nlines"\n', encoding="utf-8")

    shown = preview_result(state_dir / "r.csv", 10)

    assert shown["rows"] == [["1", "a, b"], ["2", "two\nlines"]] and shown["has_more"] is False


def test_preview_of_jsonl_returns_objects_and_keeps_unparsable_lines(state_dir: Path) -> None:
    (state_dir / "r.jsonl").write_text('{"a": 1}\nnot json\n{"a": 3}\n', encoding="utf-8")

    shown = preview_result(state_dir / "r.jsonl", 2)

    assert shown["rows"] == [{"a": 1}, "not json"] and shown["has_more"] is True and shown["format"] == "jsonl"


def test_preview_reads_at_most_256_kib_whatever_the_file_size(state_dir: Path) -> None:
    line = "emp@internal.corp.test\n"
    (state_dir / "huge.csv").write_text("email\n" + line * 200_000, encoding="utf-8")  # about 4.6 MB

    shown = preview_result(state_dir / "huge.csv", MAX_PREVIEW_ROWS)

    assert shown["rows_returned"] == MAX_PREVIEW_ROWS and shown["has_more"] is True
    assert shown["size_bytes"] > 4_000_000


def test_preview_clamps_the_row_count(state_dir: Path) -> None:
    (state_dir / "r.csv").write_text("email\na@b.test\nc@d.test\n")

    assert preview_result(state_dir / "r.csv", 0)["rows_returned"] == 1
    assert preview_result(state_dir / "r.csv", 10_000)["rows_returned"] == 2


def test_preview_of_an_empty_result_has_no_rows(state_dir: Path) -> None:
    (state_dir / "r.csv").write_text("email\n")

    shown = preview_result(state_dir / "r.csv")

    assert shown["columns"] == ["email"] and shown["rows"] == [] and shown["has_more"] is False


def test_progress_line_overwrites_itself_and_erases_when_finished() -> None:
    import io

    from datamining_skill import MiningProgress
    from datamining_skill.cli import ProgressLine

    stream = io.StringIO()
    line = ProgressLine(stream)

    line(MiningProgress(1, 34, 1_200))
    line(MiningProgress(2, 34, 2_500_000))
    line.finish()
    line.finish()  # nothing left to erase

    written = stream.getvalue()
    assert "mining: 1/34 chunks, 1,200 records" in written and "2/34 chunks, 2,500,000 records" in written
    assert written.count("\r") == 4  # two updates, then one erase that returns to the line start
    assert written.endswith("\r")
    assert written.split("\r")[-2].strip() == ""  # the erase wrote only spaces


def test_the_progress_line_appears_only_on_a_terminal(
    state_dir: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_contacts(state_dir / "in.csv")
    args = ("mine", str(state_dir / "in.csv"), str(state_dir / "{}.csv"), "--workspace", str(state_dir))

    assert run_cli(*[a.replace("{}", "quiet") for a in args]) == 0
    assert "mining:" not in capsys.readouterr().err  # stderr is not a terminal under pytest

    monkeypatch.setattr("datamining_skill.cli.sys.stderr.isatty", lambda: True, raising=False)
    assert run_cli(*[a.replace("{}", "shown") for a in args]) == 0
    assert "mining: " in capsys.readouterr().err

    assert run_cli(*[a.replace("{}", "hidden") for a in args], "--no-progress") == 0
    assert "mining:" not in capsys.readouterr().err


def test_interrupting_a_mine_says_how_to_resume(
    state_dir: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupted(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("datamining_skill.cli.run_mining", interrupted)

    code = run_cli("mine", str(state_dir / "in.csv"), str(state_dir / "out.csv"))

    assert code == 130 and "run the same command again to resume" in capsys.readouterr().err


def test_mine_is_quiet_by_default_and_logs_on_request(state_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_contacts(state_dir / "in.csv")

    assert main(["mine", str(state_dir / "in.csv"), str(state_dir / "a.csv"), "--workspace", str(state_dir)]) == 0
    assert capsys.readouterr().err == ""

    assert main(["mine", str(state_dir / "in.csv"), str(state_dir / "b.csv"), "--workspace", str(state_dir), "--log-level", "INFO"]) == 0
    assert '"event":"mining.completed"' in capsys.readouterr().err


def test_a_platform_that_cannot_report_free_memory_is_still_quiet(
    state_dir: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # macOS: sysconf has no SC_AVPHYS_PAGES, so the probe answers "unknown" on every run
    monkeypatch.setattr("datamining_skill.infrastructure.system_memory._read_available", lambda: None)
    write_contacts(state_dir / "in.csv")

    assert main(["mine", str(state_dir / "in.csv"), str(state_dir / "a.csv"), "--workspace", str(state_dir)]) == 0
    assert capsys.readouterr().err == ""

    assert main(["mine", str(state_dir / "in.csv"), str(state_dir / "b.csv"), "--workspace", str(state_dir), "--log-level", "INFO"]) == 0
    assert '"event":"chunking.memory_unavailable"' in capsys.readouterr().err
