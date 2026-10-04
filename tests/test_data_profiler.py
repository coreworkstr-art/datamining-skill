"""Functional tests for :class:`DataProfiler`: metadata extraction and failure modes."""

from __future__ import annotations

import codecs
import gzip
import json
import random
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest

from datamining_skill import (
    DataFormat,
    DataMiningException,
    DataProfiler,
    DataSourceUnavailableException,
    ProfilerConfig,
    UnsupportedDataFormatException,
    create_profiler,
)
from datamining_skill.application.config import KIB, MIB
from tests.conftest import WriteFile



def test_csv_metadata_is_extracted_correctly(profiler: DataProfiler, write_file: WriteFile) -> None:
    rows = "\n".join(f"{i},acct{i},{i * 1.5:.2f}" for i in range(1, 501))
    content = f"id,name,amount\n{rows}\n"
    path = write_file("orders.csv", content)

    result = profiler.profile_as_dict(path)

    assert result["file"]["name"] == "orders.csv"
    assert result["file"]["size_bytes"] == len(content.encode())
    assert result["format"] == "csv"
    assert result["encoding"]["name"] == "ascii"
    assert result["structure"]["fields"] == ["id", "name", "amount"]
    assert result["structure"]["delimiter"] == ","
    assert result["structure"]["has_header"] is True
    assert result["records"]["count"] == 500  # header excluded
    assert result["records"]["is_exact"] is True
    json.dumps(result)  # must be JSON-serialisable


@pytest.mark.parametrize(
    ("delimiter", "name"), [(";", "semi.csv"), ("\t", "tabs.tsv"), ("|", "pipes.txt")]
)
def test_csv_delimiters_are_detected(
    profiler: DataProfiler, write_file: WriteFile, delimiter: str, name: str
) -> None:
    lines = [delimiter.join(["id", "city", "score"])]
    lines += [delimiter.join([str(i), f"city{i}", str(i * 3)]) for i in range(50)]
    path = write_file(name, "\n".join(lines) + "\n")

    profile = profiler.profile(path)

    assert profile.data_format is DataFormat.CSV
    assert profile.structure.delimiter == delimiter
    assert profile.structure.fields == ("id", "city", "score")
    assert profile.records.count == 50


def test_csv_without_header_gets_positional_names(
    profiler: DataProfiler, write_file: WriteFile
) -> None:
    path = write_file("raw.csv", "1,2,3\n4,5,6\n7,8,9\n")

    profile = profiler.profile(path)

    assert profile.structure.has_header is False
    assert profile.structure.fields == ("column_1", "column_2", "column_3")
    assert profile.records.count == 3


def test_jsonl_keys_and_types_are_discovered(profiler: DataProfiler, write_file: WriteFile) -> None:
    lines = [json.dumps({"id": i, "tag": "x", "score": i / 2, "extra": None if i % 2 else i}) for i in range(100)]
    path = write_file("events.jsonl", "\n".join(lines) + "\n")

    profile = profiler.profile(path)

    assert profile.data_format is DataFormat.JSONL
    assert profile.structure.fields == ("id", "tag", "score", "extra")
    assert profile.structure.field_types is not None
    assert profile.structure.field_types["id"] == ("integer",)
    assert profile.structure.field_types["extra"] == ("integer", "null")
    assert profile.records.count == 100


def test_jsonl_tolerates_corrupt_and_pathological_lines(
    profiler: DataProfiler, write_file: WriteFile
) -> None:
    good = [json.dumps({"k": i}) for i in range(40)]
    deeply_nested = '{"k": ' + "[" * 200_000  # would overflow a naive recursive parser
    path = write_file("noisy.jsonl", "\n".join([*good, "{broken", deeply_nested]) + "\n")

    profile = profiler.profile(path)

    assert profile.data_format is DataFormat.JSONL
    assert profile.structure.fields == ("k",)


def test_apache_access_log_is_recognised(profiler: DataProfiler, write_file: WriteFile) -> None:
    line = '203.0.113.9 - - [10/Oct/2025:13:55:36 +0000] "GET /index.html HTTP/1.1" 200 2326 "-" "curl/8.0"'
    path = write_file("access.log", "\n".join([line] * 30) + "\n")

    profile = profiler.profile(path)

    assert profile.data_format is DataFormat.LOG
    assert profile.structure.pattern == "apache_access"
    assert "status" in profile.structure.fields
    assert profile.records.count == 30


def test_application_log_tolerates_stack_trace_lines(
    profiler: DataProfiler, write_file: WriteFile
) -> None:
    entries: list[str] = []
    for i in range(50):
        entries.append(f"2025-10-04 12:00:{i % 60:02d},123 ERROR Something failed #{i}")
        if i % 10 == 0:  # ~9% continuation lines, within the 80% match tolerance
            entries.append("    at module.function(file.py:10)")
    path = write_file("app.log", "\n".join(entries) + "\n")

    profile = profiler.profile(path)

    assert profile.data_format is DataFormat.LOG
    assert profile.structure.pattern == "iso_timestamp"
    assert profile.structure.fields[:2] == ("timestamp", "level")


def test_log4j_timestamps_are_not_taken_for_csv(
    profiler: DataProfiler, write_file: WriteFile
) -> None:
    # Each line contains exactly one comma (the millisecond separator).
    lines = [f"2025-10-04 12:00:{i % 60:02d},{i:03d} INFO Worker {i} started" for i in range(60)]
    path = write_file("worker.log", "\n".join(lines) + "\n")

    assert profiler.profile(path).data_format is DataFormat.LOG


def test_text_only_header_is_still_recognised(profiler: DataProfiler, write_file: WriteFile) -> None:
    path = write_file("people.csv", "name,city,country\nAda,London,UK\nAlan,Wilmslow,UK\nGrace,NYC,US\n")

    profile = profiler.profile(path)

    assert profile.structure.has_header is True
    assert profile.structure.fields == ("name", "city", "country")




def test_utf8_bom_is_excluded_from_headers_and_counts(
    profiler: DataProfiler, write_file: WriteFile
) -> None:
    body = "id,city\n1,İstanbul\n2,Zürich\n3,São Paulo\n"
    path = write_file("bom.csv", codecs.BOM_UTF8 + body.encode("utf-8"))

    profile = profiler.profile(path)

    assert profile.encoding.name == "utf-8"
    assert profile.encoding.has_bom is True
    assert profile.structure.fields == ("id", "city")
    assert profile.records.count == 3


def test_utf16_with_bom_is_streamed_via_incremental_decoder(
    profiler: DataProfiler, write_file: WriteFile
) -> None:
    body = "id,city\r\n" + "".join(f"{i},Köln\r\n" for i in range(25))
    path = write_file("excel.csv", codecs.BOM_UTF16_LE + body.encode("utf-16-le"))

    profile = profiler.profile(path)

    assert profile.encoding.name == "utf-16-le"
    assert profile.structure.fields == ("id", "city")
    assert profile.records.count == 25
    assert profile.records.is_exact is True


def test_legacy_single_byte_encoding_is_reported(
    profiler: DataProfiler, write_file: WriteFile
) -> None:
    path = write_file("legacy.csv", b"id,name\n1,caf\xe9\n2,na\xefve\n3,r\xe9sum\xe9\n")

    profile = profiler.profile(path)

    assert profile.encoding.name == "cp1252"
    assert profile.records.count == 3


def test_file_without_trailing_newline_counts_last_record(
    profiler: DataProfiler, write_file: WriteFile
) -> None:
    path = write_file("tail.csv", "a,b\n1,2\n3,4")

    assert profiler.profile(path).records.count == 2




def test_large_file_record_count_is_extrapolated_accurately(write_file: WriteFile) -> None:
    config = ProfilerConfig(exact_count_max_bytes=1 * MIB, window_bytes=128 * KIB)
    profiler = create_profiler(config)
    rng = random.Random(7)
    rows = 120_000
    header = "id,payload,flag\n"
    body = "".join(f"{i},{'x' * rng.randint(5, 80)},{i % 2}\n" for i in range(rows))
    path = write_file("big.csv", header + body)
    assert path.stat().st_size > 4 * config.exact_count_max_bytes

    started = time.perf_counter()
    profile = profiler.profile(path)
    elapsed = time.perf_counter() - started

    assert profile.records.is_exact is False
    assert profile.records.method == "stratified-window-extrapolation"
    assert abs(profile.records.count - rows) / rows < 0.02
    assert elapsed < 1.0
    assert profile.structure.fields == ("id", "payload", "flag")


def test_oversized_lines_are_skipped_without_corrupting_the_count(write_file: WriteFile) -> None:
    profiler = create_profiler(ProfilerConfig(max_line_bytes=64 * KIB))
    giant = "x," + "y" * (3 * MIB)
    lines = ["id,name", *[f"{i},n{i}" for i in range(10)], giant, *[f"{i},n{i}" for i in range(10)]]
    path = write_file("giant.csv", "\n".join(lines) + "\n")

    profile = profiler.profile(path)

    assert profile.structure.fields == ("id", "name")
    assert profile.records.count == 21  # 20 normal rows + 1 oversized row


def test_single_gigantic_line_is_rejected_not_buffered(write_file: WriteFile) -> None:
    profiler = create_profiler(ProfilerConfig(max_line_bytes=64 * KIB))
    path = write_file("blob.txt", b"x" * (5 * MIB))

    with pytest.raises(UnsupportedDataFormatException):
        profiler.profile(path)




@pytest.mark.parametrize(
    ("name", "content", "reason"),
    [
        pytest.param("empty.csv", b"", "empty", id="empty"),
        pytest.param("binary.dat", bytes(range(256)) * 64, "binary", id="binary"),
        pytest.param("archive.csv", gzip.compress(b"a,b\n1,2\n" * 100), "gzip", id="gzip"),
        pytest.param("bundle.zip", b"PK\x03\x04" + b"\x00" * 64, "zip", id="zip"),
        pytest.param(
            "prose.txt",
            b"This is just a sentence\nwith no structure at all\nreally\n",
            "no supported format",
            id="prose",
        ),
    ],
)
def test_unrecognised_content_raises_custom_exception(
    profiler: DataProfiler, write_file: WriteFile, name: str, content: bytes, reason: str
) -> None:
    path = write_file(name, content)

    with pytest.raises(UnsupportedDataFormatException) as caught:
        profiler.profile(path)

    assert reason in caught.value.reason
    assert name in str(caught.value)
    assert str(path.parent) not in str(caught.value)  # no directory leakage
    assert isinstance(caught.value, DataMiningException)


def test_missing_file_raises_data_source_unavailable(profiler: DataProfiler, workspace: Path) -> None:
    with pytest.raises(DataSourceUnavailableException):
        profiler.profile(workspace / "does-not-exist.csv")


def test_directory_is_rejected(profiler: DataProfiler, workspace: Path) -> None:
    with pytest.raises(DataSourceUnavailableException):
        profiler.profile(workspace)




def test_profiling_leaves_no_files_and_source_untouched(
    profiler: DataProfiler, write_file: WriteFile, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("profiler must not create temporary files")

    for name in ("mkstemp", "mkdtemp", "NamedTemporaryFile", "TemporaryFile", "SpooledTemporaryFile"):
        monkeypatch.setattr(tempfile, name, forbidden)

    path = write_file("data.csv", "a,b\n" + "1,2\n" * 1000)
    before = (path.read_bytes(), path.stat().st_mtime_ns)

    profiler.profile(path)

    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert sorted(p.name for p in path.parent.iterdir()) == ["data.csv"]
