"""Data-quality checks: malformed records, empty values, value types and schema drift."""

from __future__ import annotations

import bz2
import csv
import gzip
import json
import random
import statistics
import threading
from pathlib import Path
from typing import Any

import pytest

from datamining_skill import UnsupportedDataFormatException, check_quality
from datamining_skill.application.data_quality import (
    MAX_EXAMPLES,
    MAX_TRACKED_FIELDS,
    QualityAccumulator,
    classify_json,
    classify_text,
    unique_names,
)
from datamining_skill.application.record_formats import CsvFormatter
from datamining_skill.application.stream_statistics import (
    HyperLogLogSketch,
    OnlineMoments,
    P2QuantileEstimator,
    QuantileSummary,
)
from datamining_skill.cli import main
from datamining_skill.domain.models import TextLine
from datamining_skill.infrastructure.handlers.csv_handler import CsvHandler
from tests.test_mcp_server import Client, call


def by_name(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {entry["name"]: entry for entry in report["fields"]}


def good_rows(count: int, start: int = 1) -> str:
    """Rows of a three-column CSV. The format detector needs a file that is mostly well formed."""
    return "".join(f"{i},name{i},{i}.5\n" for i in range(start, start + count))


def good_json_lines(count: int) -> str:
    return "".join(json.dumps({"id": i, "name": f"n{i}", "age": 20 + i % 40}) + "\n" for i in range(count))


# ------------------------------------------------------------------ value types


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        ("", "empty"),
        ("   ", "empty"),
        ("NULL", "null"),
        ("None", "null"),
        ("n/a", "null"),
        ("NaN", "null"),
        ("\\N", "null"),
        ("42", "integer"),
        ("-7", "integer"),
        ("+3", "integer"),
        ("3.14", "float"),
        (".5", "float"),
        ("1e-9", "float"),
        ("true", "boolean"),
        ("FALSE", "boolean"),
        ("2026-10-09", "date"),
        ("2026-10-09T12:30:00Z", "datetime"),
        ("2026-10-09 12:30:00.123+03:00", "datetime"),
        ("2026-10-09 garbage", "string"),
        ("12abc", "string"),
        ("-", "string"),
        ("hello", "string"),
        ("00012345678901234567890123456789012", "float"),  # too many digits for an integer, still a number
    ],
)
def test_csv_cells_are_typed_from_their_text(value: str, kind: str) -> None:
    assert classify_text(value) == kind


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        (None, "null"),
        (True, "boolean"),
        (0, "integer"),
        (1.5, "float"),
        ("12", "string"),  # a number inside quotes is a string in JSON, and that is the point
        ("", "empty"),
        ("  ", "empty"),
        ({}, "object"),
        ([], "array"),
    ],
)
def test_json_values_are_typed_from_their_json_type(value: object, kind: str) -> None:
    assert classify_json(value) == kind


def test_repeated_and_blank_column_names_are_made_distinct() -> None:
    assert unique_names(["a", "a", " ", "a", "b"]) == ["a", "a#2", "column_3", "a#3", "b"]


# ------------------------------------------------------------------ CSV


def test_a_clean_csv_has_no_issues(state_dir: Path) -> None:
    (state_dir / "ok.csv").write_text("id,name,score\n1,ada,3.5\n2,bob,4\n3,cy,5.25\n", encoding="utf-8")

    report = check_quality(state_dir / "ok.csv")

    assert report["format"] == "csv" and report["issues"] == []
    assert report["summary"] == {
        "records_valid": 3, "records_malformed": 0, "fields": 3, "fields_with_drift": 0, "issues": 0
    }  # fmt: skip
    assert report["scope"]["complete"] is True and report["scope"]["records_examined"] == 3
    assert by_name(report)["score"]["types"] == {"float": 2, "integer": 1}
    assert by_name(report)["score"]["drift"] is False  # an integer among floats is not drift
    assert report["file"]["name"] == "ok.csv"


def test_rows_with_the_wrong_number_of_columns_are_malformed_with_their_line(state_dir: Path) -> None:
    (state_dir / "ragged.csv").write_text("a,b,c\n1,2,3\n4,5\n6,7,8,9\n" + good_rows(80, 10), encoding="utf-8")

    report = check_quality(state_dir / "ragged.csv")

    assert report["records"]["valid"] == 81 and report["records"]["malformed"] == 2
    assert report["malformed"]["by_kind"] == {"too_few_columns": 1, "too_many_columns": 1}
    assert report["malformed"]["examples"] == [
        {"line": 3, "problem": "too_few_columns"},
        {"line": 4, "problem": "too_many_columns"},
    ]
    assert "2 of 83 records are malformed" in report["issues"][0]


def test_type_drift_names_the_field_and_the_line_where_the_other_type_appears(state_dir: Path) -> None:
    rows = "".join(f"{i},{i * 10}\n" for i in range(1, 6)) + "6,n/a-ish\n7,70\n"
    (state_dir / "drift.csv").write_text("id,amount\n" + rows, encoding="utf-8")

    report = check_quality(state_dir / "drift.csv")

    amount = by_name(report)["amount"]
    assert amount["drift"] is True and amount["types"] == {"integer": 6, "string": 1}
    assert amount["first_seen_line"]["string"] == 7
    assert report["summary"]["fields_with_drift"] == 1
    assert any("field 'amount' mixes number (6) and string (1); string first appears on line 7" in i for i in report["issues"])


def test_empty_cells_and_null_tokens_lower_completeness_without_counting_as_drift(state_dir: Path) -> None:
    (state_dir / "gaps.csv").write_text("id,city\n1,Ankara\n2,\n3,NULL\n4,\n5,Izmir\n", encoding="utf-8")

    report = check_quality(state_dir / "gaps.csv")

    city = by_name(report)["city"]
    assert city["empty_or_null"] == 3 and city["completeness"] == 0.4 and city["drift"] is False
    assert any("'city' is empty or missing in 60% of records" in issue for issue in report["issues"])


def test_a_field_that_never_has_a_value_is_called_out(state_dir: Path) -> None:
    (state_dir / "dead.csv").write_text("id,unused\n1,\n2,\n", encoding="utf-8")

    assert any("never has a value" in issue for issue in check_quality(state_dir / "dead.csv")["issues"])


def test_a_broken_quote_is_malformed_and_the_scan_goes_on(state_dir: Path) -> None:
    (state_dir / "quote.csv").write_text('id,name,score\n1,"fine",2\n2,"bad"x,3\n' + good_rows(60, 10), encoding="utf-8")

    report = check_quality(state_dir / "quote.csv")

    assert report["malformed"]["by_kind"] == {"bad_quoting": 1}
    assert report["records"]["valid"] == 61 and report["malformed"]["examples"][0]["line"] == 3


def test_a_quoted_value_that_spans_lines_is_one_record_and_numbers_the_next_correctly(state_dir: Path) -> None:
    (state_dir / "multi.csv").write_text('id,name,score\n1,"two\nlines",5\n2\n' + good_rows(60, 10), encoding="utf-8")

    report = check_quality(state_dir / "multi.csv")

    assert report["records"]["valid"] == 61  # the two-line record counts once
    assert report["malformed"]["examples"] == [{"line": 4, "problem": "too_few_columns"}]


def test_blank_lines_are_counted_and_skipped(state_dir: Path) -> None:
    (state_dir / "blank.csv").write_text("id,name,score\n" + good_rows(30) + "\n" + good_rows(30, 40) + "\n", encoding="utf-8")

    report = check_quality(state_dir / "blank.csv")

    assert report["records"]["valid"] == 60 and report["records"]["blank_lines"] == 2


def test_a_tab_separated_file_is_read_with_its_own_delimiter(state_dir: Path) -> None:
    rows = "".join(f"{i}\tx,y {i}\n" for i in range(60))
    (state_dir / "t.tsv").write_text("a\tb\n" + rows, encoding="utf-8")

    report = check_quality(state_dir / "t.tsv")

    assert report["records"]["valid"] == 60 and [f["name"] for f in report["fields"]] == ["a", "b"]


def test_a_utf8_byte_order_mark_does_not_end_up_in_the_first_column_name(state_dir: Path) -> None:
    (state_dir / "bom.csv").write_bytes(b"\xef\xbb\xbfid,name\n1,ada\n2,bob\n")

    assert [f["name"] for f in check_quality(state_dir / "bom.csv")["fields"]] == ["id", "name"]


def test_utf16_text_is_checked_without_a_conversion(state_dir: Path) -> None:
    (state_dir / "u16.csv").write_bytes(b"\xff\xfe" + "id,name\r\n1,müşteri\r\n2,bob\r\n".encode("utf-16-le"))

    report = check_quality(state_dir / "u16.csv")

    assert report["records"]["valid"] == 2 and report["issues"] == []


def test_a_utf8_file_with_corrupt_bytes_is_reported_as_an_encoding_guess(state_dir: Path) -> None:
    body = "".join(f"{i},müşteri {i},{i}\n" for i in range(80)).encode("utf-8")
    (state_dir / "bad.csv").write_bytes(b"id,name,score\n" + body + b"81,broken \xff\xfe bytes,81\n")

    report = check_quality(state_dir / "bad.csv")

    assert report["encoding"]["name"] == "cp1252" and report["encoding"]["confidence"] < 0.9
    assert report["issues"][0].startswith("the encoding is a guess (cp1252")
    assert "re-save it as UTF-8" in report["issues"][0]


def test_bytes_that_the_detected_encoding_cannot_decode_are_counted(state_dir: Path) -> None:
    # utf-8 is chosen from the sampled start of the file, so a corrupt byte far beyond it
    # reaches the scan as U+FFFD
    head = "".join(f"{i},müşteri {i},{i}\n" for i in range(80))
    filler = "".join(f"{i},plain text number {i},{i}\n" for i in range(80, 120_000))
    tail = "120000,broken\n"
    (state_dir / "late.csv").write_bytes(
        b"id,name,score\n" + head.encode() + filler.encode() + b"120000,broken \xff\xfe,1\n" + tail.encode()
    )

    report = check_quality(state_dir / "late.csv", max_records=1_000_000)

    assert report["encoding"]["name"] == "utf-8"
    assert report["records"]["undecodable_lines"] == 1
    assert any("not valid in the detected encoding" in issue for issue in report["issues"])


def test_a_line_over_the_size_cap_is_counted_and_does_not_derail_the_scan(state_dir: Path) -> None:
    giant = "x" * (1024 * 1024 + 10)
    (state_dir / "big.csv").write_text(
        f"id,name,score\n{good_rows(40)}99,{giant},1\n{good_rows(40, 100)}", encoding="utf-8"
    )

    report = check_quality(state_dir / "big.csv")

    assert report["malformed"]["by_kind"] == {"line_too_long": 1}
    assert report["records"]["valid"] == 80 and report["records"]["blank_lines"] == 0


def test_a_cell_over_the_csv_default_field_limit_is_still_read(state_dir: Path) -> None:
    cell = "y" * 200_000
    (state_dir / "wide.csv").write_text(f'id,note\n1,"{cell[:100_000]}\n{cell[100_000:]}"\n2,b\n', encoding="utf-8")

    assert check_quality(state_dir / "wide.csv")["records"]["valid"] == 2


def test_max_records_stops_the_scan_and_says_so(state_dir: Path) -> None:
    (state_dir / "many.csv").write_text("id,name,score\n" + good_rows(100), encoding="utf-8")

    report = check_quality(state_dir / "many.csv", max_records=10)

    assert report["records"]["valid"] == 10 and report["scope"]["complete"] is False
    assert "stopped after 10 records" in report["scope"]["note"]


def test_reaching_the_limit_exactly_at_the_end_still_counts_as_complete(state_dir: Path) -> None:
    (state_dir / "exact.csv").write_text("id,name,score\n" + good_rows(50), encoding="utf-8")

    assert check_quality(state_dir / "exact.csv", max_records=50)["scope"]["complete"] is True
    assert check_quality(state_dir / "exact.csv", max_records=49)["scope"]["complete"] is False


def test_a_header_only_file_has_fields_and_no_records(state_dir: Path) -> None:
    (state_dir / "head.csv").write_text("a,b\n", encoding="utf-8")

    with pytest.raises(UnsupportedDataFormatException):  # one line is not enough to call it a table
        check_quality(state_dir / "head.csv")


# ------------------------------------------------------------------ JSON Lines


JSONL_HEAD = (
    '{"id": 1, "age": 30, "name": "ada", "tags": ["a"]}\n'
    '{"id": 2, "age": "31", "name": "bob"}\n'
    '{"id": 3, "age": null, "name": ""}\n'
    "this is not json\n"
    "[1, 2]\n"
    '{"id": 6, "age": 33, "extra": true}\n'
    "\n"
)


def test_json_lines_report_malformed_lines_missing_keys_and_drift(state_dir: Path) -> None:
    (state_dir / "r.jsonl").write_text(JSONL_HEAD + good_json_lines(60), encoding="utf-8")

    report = check_quality(state_dir / "r.jsonl")

    assert report["format"] == "jsonl"
    assert report["records"]["valid"] == 64 and report["records"]["blank_lines"] == 1
    assert report["malformed"]["by_kind"] == {"invalid_json": 1, "not_an_object": 1}
    assert report["malformed"]["examples"] == [{"line": 4, "problem": "invalid_json"}, {"line": 5, "problem": "not_an_object"}]
    fields = by_name(report)
    assert fields["age"]["types"] == {"integer": 62, "string": 1, "null": 1} and fields["age"]["drift"] is True
    assert fields["age"]["first_seen_line"]["string"] == 2
    assert fields["tags"]["missing"] == 63 and fields["extra"]["missing"] == 63
    assert fields["name"]["empty_or_null"] == 1 and fields["name"]["missing"] == 1
    assert fields["id"]["drift"] is False and fields["id"]["completeness"] == 1.0


def test_a_number_written_as_a_string_is_drift_in_json(state_dir: Path) -> None:
    (state_dir / "s.jsonl").write_text(good_json_lines(60) + '{"id": "61", "name": "x", "age": 5}\n', encoding="utf-8")

    report = check_quality(state_dir / "s.jsonl")

    assert report["summary"]["fields_with_drift"] == 1 and by_name(report)["id"]["drift"] is True


def test_json_nested_too_deep_for_the_parser_is_a_malformed_line_not_a_crash(state_dir: Path) -> None:
    (state_dir / "d.jsonl").write_text(
        good_json_lines(30) + "[" * 100_000 + "]" * 100_000 + "\n" + good_json_lines(30), encoding="utf-8"
    )

    report = check_quality(state_dir / "d.jsonl")

    assert report["records"]["valid"] == 60 and sum(report["malformed"]["by_kind"].values()) == 1


def test_many_distinct_keys_are_bounded(state_dir: Path) -> None:
    accumulator = QualityAccumulator("jsonl")
    for line in range(1, 5):
        accumulator.add_object(line, {f"k{line}_{i}": i for i in range(MAX_TRACKED_FIELDS)})

    report = accumulator.report()

    assert len(report["fields"]) == MAX_TRACKED_FIELDS and accumulator.untracked_values > 0
    assert any("more than" in issue for issue in report["issues"])


def test_only_a_few_examples_are_listed(state_dir: Path) -> None:
    lines = [json.dumps({"id": i, "name": f"n{i}", "age": 30}) if i % 10 else "nope" for i in range(1, 221)]
    (state_dir / "m.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    report = check_quality(state_dir / "m.jsonl")

    assert report["malformed"]["by_kind"] == {"invalid_json": 22} and len(report["malformed"]["examples"]) == MAX_EXAMPLES


def test_no_value_is_ever_part_of_the_report(state_dir: Path) -> None:
    lines = [f'{{"email": "user{i}@internal.corp.test", "note": "very-recognisable-secret"}}\n' for i in range(60)]
    (state_dir / "p.jsonl").write_text("".join(lines) + '{"email": 5}\n', encoding="utf-8")

    text = json.dumps(check_quality(state_dir / "p.jsonl"))

    assert "user1@internal" not in text and "recognisable" not in text


# ------------------------------------------------------------------ formats and sources


def test_other_formats_are_refused_with_a_reason(state_dir: Path) -> None:
    (state_dir / "app.log").write_text("".join(f"2026-10-09 12:00:0{i % 10} ERROR something broke {i}\n" for i in range(50)))

    with pytest.raises(UnsupportedDataFormatException, match="quality checks cover CSV, TSV and JSON Lines"):
        check_quality(state_dir / "app.log")


@pytest.mark.parametrize("compress", [gzip.compress, bz2.compress])
def test_a_compressed_file_is_checked_through_a_temporary_sample(state_dir: Path, compress: Any) -> None:
    text = "id,name,score\n1,a\n" + good_rows(60, 2)
    (state_dir / "z.csv.gz").write_bytes(compress(text.encode()))

    report = check_quality(state_dir / "z.csv.gz", workspace=state_dir)

    assert report["compression"] in ("gzip", "bzip2") and report["file"]["name"] == "z.csv.gz"
    assert report["malformed"]["by_kind"] == {"too_few_columns": 1}
    assert report["scope"]["complete"] is True
    assert [p for p in (state_dir / ".scratch").rglob("*") if p.is_file()] == []  # the sample is gone


def test_a_compressed_file_larger_than_the_sample_says_it_was_cut(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import datamining_skill.bootstrap as bootstrap

    monkeypatch.setattr(bootstrap, "QUALITY_SAMPLE_BYTES", 2048)
    (state_dir / "big.csv.gz").write_bytes(gzip.compress(("id,name,score\n" + good_rows(2000)).encode()))

    report = check_quality(state_dir / "big.csv.gz", workspace=state_dir)

    assert report["scope"]["complete"] is False and "first" in report["scope"]["note"]


# ------------------------------------------------------------------ the accumulator


def test_the_accumulator_keeps_counters_and_line_numbers_not_values() -> None:
    accumulator = QualityAccumulator("csv")
    accumulator.set_columns(["a", "b"])
    for line in range(2, 5002):
        accumulator.add_row(line, [str(line), "x" * 1000])

    entry = accumulator.report()["fields"][0]

    assert entry["types"] == {"integer": 5000} and entry["first_seen_line"] == {"integer": 2}


# ------------------------------------------------------------------ command line and server


def test_the_command_line_prints_the_report(state_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (state_dir / "c.csv").write_text("id,name,score\n1,x\n" + good_rows(60, 2), encoding="utf-8")

    assert main(["quality", str(state_dir / "c.csv")]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["summary"]["records_malformed"] == 1


def test_the_command_line_validates_the_record_limit(state_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (state_dir / "c.csv").write_text("id,name,score\n" + good_rows(60), encoding="utf-8")

    assert main(["quality", str(state_dir / "c.csv"), "--max-records", "0"]) == 3
    assert "--max-records must be between" in capsys.readouterr().err


def test_the_command_line_can_mask_field_names(state_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (state_dir / "h.csv").write_text("ada@internal.corp.test,name,score\n" + good_rows(60), encoding="utf-8")

    assert main(["quality", str(state_dir / "h.csv"), "--mask"]) == 0

    out = capsys.readouterr().out
    assert "internal.corp.test" not in out and json.loads(out)["pii_masked"] == {"EMAIL": 1}


def test_the_server_offers_the_tool_and_masks_names_by_default(state_dir: Path) -> None:
    from datamining_skill import create_mcp_server

    (state_dir / "h.csv").write_text("ada@internal.corp.test,name,score\n5\n" + good_rows(60), encoding="utf-8")
    client = Client(create_mcp_server([state_dir]))

    tools, response = client.handshake_and(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, call(3, "check_quality", {"path": "h.csv"})
    )

    quality = next(t for t in tools["result"]["tools"] if t["name"] == "check_quality")
    assert quality["annotations"]["readOnlyHint"] is True
    report = response["result"]["structuredContent"]
    assert report["summary"]["records_malformed"] == 1
    assert "internal.corp.test" not in json.dumps(response["result"]) and report["pii_masked"] == {"EMAIL": 1}


def test_the_server_validates_max_records(state_dir: Path) -> None:
    from datamining_skill import create_mcp_server

    (state_dir / "h.csv").write_text("id,name,score\n" + good_rows(60), encoding="utf-8")

    (response,) = Client(create_mcp_server([state_dir])).handshake_and(
        call(2, "check_quality", {"path": "h.csv", "max_records": 0})
    )

    assert response["result"]["isError"] is True


def test_the_server_refuses_a_path_outside_the_workspace(state_dir: Path) -> None:
    from datamining_skill import create_mcp_server

    (response,) = Client(create_mcp_server([state_dir])).handshake_and(
        call(2, "check_quality", {"path": "../../../etc/passwd"})
    )

    assert response["result"]["isError"] is True


def test_csv_code_run_on_eight_threads_at_once_does_not_trip_over_itself(state_dir: Path) -> None:
    """The csv module's field limit is shared by the whole process, and one formatter serves every thread."""
    big = "x" * 300_000  # over the module's default limit of 128 KiB, under the one the code sets
    path = state_dir / "wide.csv"
    path.write_text("id,note\n" + "".join(f'{n},"{big}"\n' for n in range(3)), encoding="utf-8")
    sample = [TextLine(text, len(text) + 1) for text in ("id,note", f"1,{big}", f"2,{big}", "3,plain")]
    formatter = CsvFormatter(["a", "b", "c"])
    cells = [
        ["plain", "with,comma", 'with "quote"'],
        ["", "line\nbreak", "tab\tseparated"],
        ["clean", "values", "only"],
    ]
    expected = [CsvFormatter(["a", "b", "c"]).format(row) for row in cells]
    failures: list[str] = []
    start = threading.Barrier(8)

    def work(worker: int) -> None:
        start.wait()
        try:
            for round_ in range(8):
                analysis = CsvHandler().analyze(iter(sample), 200)
                if analysis is None or analysis.structure.fields != ("id", "note"):
                    failures.append(f"worker {worker} round {round_}: the table was not recognised")
                report = check_quality(path)
                kinds = json.dumps(report)
                if "field_too_large" in kinds or "bad_quoting" in kinds or report["summary"]["records_valid"] != 3:
                    failures.append(f"worker {worker} round {round_}: a field was refused")
                for row, wanted in zip(cells, expected, strict=True):
                    if formatter.format(row) != wanted:
                        failures.append(f"worker {worker} round {round_}: {row!r} was written wrongly")
        except Exception as exc:
            failures.append(f"worker {worker}: {type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []


def test_a_csv_value_over_the_scan_limit_is_reported_once_the_limit_has_been_raised(state_dir: Path) -> None:
    giant = ("y" * 1000 + "\n") * 4300  # one quoted value over 4 MiB, in lines the reader accepts
    (state_dir / "giant.csv").write_text(f'id,note\n1,ok\n2,"{giant}"\n3,ok\n', encoding="utf-8")
    CsvHandler().analyze(iter([TextLine("a,b", 4), TextLine("1,2", 4)]), 10)  # sets the shared limit

    report = check_quality(state_dir / "giant.csv")

    assert "field_too_large" in json.dumps(report)


def test_the_shared_csv_field_limit_is_raised_and_left_raised(state_dir: Path) -> None:
    """Putting an earlier, smaller limit back would undo the raise another thread depends on."""
    (state_dir / "ok.csv").write_text("id,name\n1,ada\n2,bob\n", encoding="utf-8")
    previous = csv.field_size_limit(200_000)
    try:
        CsvHandler().analyze(iter([TextLine("a,b", 4), TextLine("1,2", 4)]), 10)
        check_quality(state_dir / "ok.csv")

        assert csv.field_size_limit() >= 4 * 1024 * 1024  # both raised it, and neither put 200 000 back
    finally:
        csv.field_size_limit(previous)


# ------------------------------------------------------------------ streaming statistics


def test_online_moments_match_the_standard_library() -> None:
    rng = random.Random(7)
    values = [rng.gauss(50, 10) for _ in range(500)] + [rng.expovariate(0.2) for _ in range(500)]
    moments = OnlineMoments()
    for value in values:
        moments.update(value)

    result = moments.to_dict()

    assert result["mean"] == pytest.approx(statistics.mean(values), abs=1e-4)
    assert result["stddev"] == pytest.approx(statistics.stdev(values), abs=1e-4)
    centred = [v - statistics.mean(values) for v in values]
    m2 = sum(c**2 for c in centred) / len(values)
    assert result["skewness"] == pytest.approx((sum(c**3 for c in centred) / len(values)) / m2**1.5, abs=1e-3)
    assert result["excess_kurtosis"] == pytest.approx((sum(c**4 for c in centred) / len(values)) / m2**2 - 3, abs=1e-3)


def test_online_moments_survive_a_large_offset() -> None:
    """The textbook sum of squares loses every digit of values like 1 000 000 000 + x."""
    rng = random.Random(3)
    noise = [rng.gauss(0, 2) for _ in range(5000)]
    moments = OnlineMoments()
    for value in noise:
        moments.update(1e9 + value)

    result = moments.to_dict()

    assert result["stddev"] == pytest.approx(statistics.stdev(noise), abs=1e-4)
    assert result["mean"] == pytest.approx(1e9 + statistics.mean(noise), abs=1e-3)


def test_online_moments_ignore_nan_and_infinity_and_report_what_is_defined() -> None:
    moments = OnlineMoments()
    assert moments.to_dict() == {"mean": 0.0, "stddev": None, "skewness": None, "excess_kurtosis": None}
    for value in (float("nan"), float("inf"), -float("inf")):
        moments.update(value)
    assert moments.count == 0
    moments.update(4.0)
    moments.update(4.0)
    moments.update(4.0)
    moments.update(4.0)

    assert moments.to_dict() == {"mean": 4.0, "stddev": 0.0, "skewness": None, "excess_kurtosis": None}


def test_quantile_summary_is_close_on_2000_numbers() -> None:
    rng = random.Random(11)
    values = [rng.gauss(100, 15) for _ in range(2000)]
    summary = QuantileSummary()
    for value in values:
        summary.update(value)
    exact = statistics.quantiles(values, n=4, method="inclusive")

    result = summary.to_dict()

    assert set(result) == {"p25", "median", "p75", "iqr"}
    assert result["p25"] == pytest.approx(exact[0], abs=1.5)
    assert result["median"] == pytest.approx(exact[1], abs=1.5)
    assert result["p75"] == pytest.approx(exact[2], abs=1.5)
    assert result["iqr"] == pytest.approx(exact[2] - exact[0], abs=2.5)


def test_quantiles_of_sorted_and_repeated_input() -> None:
    ascending = P2QuantileEstimator(0.5)
    for n in range(1, 1002):
        ascending.update(float(n))
    constant = P2QuantileEstimator(0.5)
    for _ in range(100):
        constant.update(7.0)

    assert ascending.value() == pytest.approx(501, abs=10)
    assert constant.value() == 7.0
    assert P2QuantileEstimator(0.5).value() is None
    with pytest.raises(ValueError, match="between 0 and 1"):
        P2QuantileEstimator(1.0)


def test_hyperloglog_counts_small_sets_closely_and_nothing_as_zero() -> None:
    assert HyperLogLogSketch().estimate() == 0
    for size in (1, 2, 10, 50):
        sketch = HyperLogLogSketch()
        for n in range(size):
            sketch.add(f"value {n}".encode())
            sketch.add(f"value {n}".encode())  # repeats do not count
        assert abs(sketch.estimate() - size) <= max(1, size * 0.12), size


def test_hyperloglog_is_within_its_error_on_5000_distinct_items() -> None:
    """With 256 registers the standard error is 6.5 %: a single set is held to three of them, the mean of many to 5 %."""
    errors = []
    for trial in range(20):
        sketch = HyperLogLogSketch()
        for n in range(5000):
            sketch.add(f"item-{trial}-{n}".encode())
        errors.append(sketch.estimate() / 5000 - 1)

    assert all(abs(error) < 0.20 for error in errors)
    assert abs(statistics.mean(errors)) < 0.05


def test_the_sketches_have_a_fixed_size() -> None:
    sketch = HyperLogLogSketch()
    for n in range(50_000):
        sketch.add(str(n).encode())
    assert len(sketch._registers) == 256


def test_quality_report_has_distinct_counts_and_number_statistics() -> None:
    rows = "".join(f"{n},{n % 5},name{n},{n * 1.5}\n" for n in range(50))
    path_text = "id,bucket,name,price\n" + rows
    accumulator = QualityAccumulator("csv")
    accumulator.set_columns(["id", "bucket", "name", "price"])
    for line, row in enumerate(path_text.splitlines()[1:], 2):
        accumulator.add_row(line, row.split(","))
    fields = {f["name"]: f for f in accumulator.report()["fields"]}

    assert fields["id"]["approx_distinct"] == pytest.approx(50, abs=6)
    assert fields["id"]["uniqueness_ratio"] == pytest.approx(1.0, abs=0.12)
    assert fields["bucket"]["approx_distinct"] == 5
    assert fields["bucket"]["uniqueness_ratio"] == 0.1
    assert fields["id"]["numeric_stats"]["mean"] == 24.5
    assert fields["id"]["numeric_stats"]["median"] == pytest.approx(24.5, abs=3)
    assert fields["price"]["numeric_stats"]["mean"] == pytest.approx(36.75)
    assert fields["name"]["numeric_stats"] is None  # a string column has none
    assert fields["name"]["dominant_type"] == "string"


def test_number_statistics_need_ten_numeric_values(state_dir: Path) -> None:
    (state_dir / "few.csv").write_text("n,s\n" + "".join(f"{i},x{i}\n" for i in range(9)), encoding="utf-8")
    (state_dir / "ten.csv").write_text("n,s\n" + "".join(f"{i},x{i}\n" for i in range(10)), encoding="utf-8")

    few = {f["name"]: f for f in check_quality(state_dir / "few.csv")["fields"]}
    ten = {f["name"]: f for f in check_quality(state_dir / "ten.csv")["fields"]}

    assert few["n"]["numeric_stats"] is None
    assert ten["n"]["numeric_stats"] is not None and ten["n"]["numeric_stats"]["mean"] == 4.5
    assert ten["s"]["numeric_stats"] is None


def test_json_lines_get_the_same_statistics(state_dir: Path) -> None:
    lines = [json.dumps({"id": n, "score": n / 4, "tag": f"t{n % 3}", "code": str(n)}) for n in range(60)]
    (state_dir / "data.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    fields = {f["name"]: f for f in check_quality(state_dir / "data.jsonl")["fields"]}

    assert fields["id"]["numeric_stats"]["mean"] == 29.5
    assert fields["score"]["numeric_stats"]["mean"] == pytest.approx(7.375)
    assert fields["tag"]["approx_distinct"] == 3 and fields["tag"]["numeric_stats"] is None
    assert fields["code"]["numeric_stats"] is None  # a string that looks like a number stays a string


def test_a_field_that_is_mostly_text_gets_no_number_statistics(state_dir: Path) -> None:
    body = "".join(f"{i}\n" for i in range(12)) + "".join(f"word{i}\n" for i in range(30))
    (state_dir / "mixed.csv").write_text("v,w\n" + "".join(f"{line.strip()},1\n" for line in body.splitlines()), encoding="utf-8")

    fields = {f["name"]: f for f in check_quality(state_dir / "mixed.csv")["fields"]}

    assert fields["v"]["dominant_type"] == "string" and fields["v"]["numeric_stats"] is None


def test_the_exact_smallest_and_largest_values_are_never_reported(state_dir: Path) -> None:
    marker = 987654.321
    values = [marker] + [float(n) for n in range(1, 40)]
    (state_dir / "m.csv").write_text("x,y\n" + "".join(f"{v},1\n" for v in values), encoding="utf-8")

    report = check_quality(state_dir / "m.csv")
    stats = next(f for f in report["fields"] if f["name"] == "x")["numeric_stats"]

    assert set(stats) == {"mean", "stddev", "skewness", "excess_kurtosis", "p25", "median", "p75", "iqr"}
    assert "987654.321" not in json.dumps(report)


def test_one_estimator_answers_for_all_three_quartiles() -> None:
    rng = random.Random(21)
    values = [rng.expovariate(0.1) for _ in range(5000)]
    single = P2QuantileEstimator(0.5)
    dedicated = [P2QuantileEstimator(p) for p in (0.25, 0.5, 0.75)]
    for value in values:
        single.push(value)
        for estimator in dedicated:
            estimator.push(value)

    markers = single.marker_values()

    assert markers is not None
    for marker, estimator in zip(markers, dedicated, strict=True):
        assert marker == pytest.approx(estimator.value(), rel=0.03)
    assert P2QuantileEstimator(0.5).marker_values() is None
    few = P2QuantileEstimator(0.5)
    for value in (3.0, 1.0, 2.0):
        few.update(value)
    assert few.marker_values() == (1.0, 2.0, 3.0)
