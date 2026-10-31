"""Data-quality checks: malformed records, empty values, value types and schema drift."""

from __future__ import annotations

import bz2
import gzip
import json
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
from datamining_skill.cli import main
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
