"""JSON documents, very large CSV cells, multi-line CSV records and the record count's unit."""

from __future__ import annotations

import json

import pytest

from datamining_skill import DataFormat, DataProfiler, UnsupportedDataFormatException, run_mining
from tests.conftest import WriteFile

PEOPLE = [{"id": i, "name": f"Ada {i}", "email": f"emp{i}@internal.corp.test"} for i in range(300)]


def test_a_pretty_printed_json_array_is_recognised(profiler: DataProfiler, write_file: WriteFile) -> None:
    path = write_file("people.json", json.dumps(PEOPLE, indent=2))

    profile = profiler.profile(path)

    assert profile.data_format is DataFormat.JSON
    assert profile.structure.fields == ("id", "name", "email")


def test_a_minified_json_array_on_one_line_is_recognised(profiler: DataProfiler, write_file: WriteFile) -> None:
    path = write_file("people.json", json.dumps(PEOPLE))

    profile = profiler.profile(path)

    assert profile.data_format is DataFormat.JSON
    assert profile.structure.fields == ("id", "name", "email")


def test_a_wrapped_json_document_is_recognised(profiler: DataProfiler, write_file: WriteFile) -> None:
    path = write_file("people.json", json.dumps({"people": PEOPLE}, indent=1))

    assert profiler.profile(path).structure.fields[:4] == ("people", "id", "name", "email")


def test_a_json_document_larger_than_the_line_cap_is_recognised(profiler: DataProfiler, write_file: WriteFile) -> None:
    people = [{"id": i, "email": f"emp{i}@internal.corp.test", "pad": "x" * 60} for i in range(40_000)]
    path = write_file("big.json", json.dumps(people))  # about 4 MB on one line

    assert profiler.profile(path).data_format is DataFormat.JSON


def test_json_lines_are_still_reported_as_json_lines(profiler: DataProfiler, write_file: WriteFile) -> None:
    path = write_file("people.jsonl", "".join(json.dumps(p) + "\n" for p in PEOPLE))

    profile = profiler.profile(path)

    assert profile.data_format is DataFormat.JSONL
    assert profile.records.unit == "records" and profile.records.count == len(PEOPLE)


def test_text_that_only_starts_with_a_bracket_is_not_json(profiler: DataProfiler, write_file: WriteFile) -> None:
    path = write_file("notes.txt", "[draft] remember to call back\nand then email the team\n")

    with pytest.raises(UnsupportedDataFormatException):
        profiler.profile(path)


def test_a_json_document_is_mined_in_full_including_escaped_addresses(write_file: WriteFile, state_dir: object) -> None:
    from pathlib import Path

    workspace = Path(str(state_dir))
    text = json.dumps({"people": PEOPLE}).replace("@", "\\u0040")  # every address spelled with an escape
    source = write_file("people.json", text)

    summary = run_mining(source, workspace / "out.csv", workspace=workspace, unique=True)

    found = (workspace / "out.csv").read_text().splitlines()[1:]
    assert found == [p["email"] for p in PEOPLE] and summary.succeeded


def test_a_csv_cell_larger_than_the_csv_modules_default_limit_is_accepted(
    profiler: DataProfiler, write_file: WriteFile
) -> None:
    blob = "A" * 600_000  # beyond the 128 KiB limit of the csv module
    rows = "id,payload,email\n" + "".join(f"{i},{blob if i == 5 else 'x'},emp{i}@internal.corp.test\n" for i in range(20))
    path = write_file("blob.csv", rows)

    profile = profiler.profile(path)

    assert profile.data_format is DataFormat.CSV and profile.structure.fields == ("id", "payload", "email")


def test_every_address_after_a_giant_cell_is_still_mined(write_file: WriteFile, state_dir: object) -> None:
    from pathlib import Path

    workspace = Path(str(state_dir))
    blob = "A" * 3_000_000  # far beyond the 1 MiB line cap, with an address after it on the same line
    rows = "id,payload,email\n" + "".join(f"{i},{blob if i == 5 else 'x'},emp{i}@internal.corp.test\n" for i in range(20))
    source = write_file("blob.csv", rows)

    summary = run_mining(source, workspace / "out.csv", workspace=workspace)

    found = (workspace / "out.csv").read_text().splitlines()[1:]
    assert found == [f"emp{i}@internal.corp.test" for i in range(20)]
    assert summary.oversized_lines == 1


def test_multi_line_csv_records_are_flagged_and_counted_in_lines(profiler: DataProfiler, write_file: WriteFile) -> None:
    rows = "id,name,notes\n" + "".join(f'{i},Ada {i},"first line\nsecond line\nthird line"\n' for i in range(100))
    path = write_file("crm.csv", rows)

    profile = profiler.profile(path)

    assert profile.structure.multiline_records is True
    assert profile.records.unit == "lines" and profile.records.count == 300  # 100 records of 3 lines each
    assert profile.to_dict()["records"]["unit"] == "lines"


def test_single_line_csv_records_are_not_flagged(profiler: DataProfiler, write_file: WriteFile) -> None:
    path = write_file("flat.csv", "id,name\n" + "".join(f"{i},Ada {i}\n" for i in range(50)))

    assert profiler.profile(path).structure.multiline_records is False
