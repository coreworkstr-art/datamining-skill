"""Tests for byte-range reading, extraction strategies, formatters and the MinerWorker."""

from __future__ import annotations

import csv
import io
import json
import time
import tracemalloc
from collections.abc import Callable
from itertools import pairwise
from pathlib import Path

import pytest

from datamining_skill import (
    ChunkMetadata,
    CsvFormatter,
    DataSourceUnavailableException,
    InvalidConfigurationException,
    JsonlFormatter,
    MinerWorker,
    RegexExtractor,
    SourceDescriptor,
    UnsupportedDataFormatException,
    create_profiler,
)
from datamining_skill.application.config import MIB
from datamining_skill.domain.models import EncodingInfo
from datamining_skill.infrastructure import FileStreamReader
from datamining_skill.infrastructure.scratch import LocalScratchStore
from tests.conftest import SCRATCH_ROOT, WriteFile

UTF8 = EncodingInfo(name="utf-8")


def reader(max_line: int = 1024 * 1024) -> FileStreamReader:
    return FileStreamReader(chunk_size_bytes=16, max_line_bytes=max_line)




def test_range_reads_exactly_the_lines_between_the_offsets(write_file: WriteFile) -> None:
    path = write_file("a.txt", "aaa\nbbb\nccc\nddd\n")  # four 4-byte lines

    lines = list(reader().range_lines(path, UTF8, 4, 12))

    assert [line.text for line in lines] == ["bbb", "ccc"]
    assert sum(line.byte_length for line in lines) == 8  # consumed exactly end - start


def test_reading_stops_at_the_byte_boundary_even_inside_a_line(write_file: WriteFile) -> None:
    path = write_file("a.txt", "aaa\nbbb\nccc\nddd\n")

    lines = list(reader().range_lines(path, UTF8, 4, 10))  # byte 10 is inside "ccc"

    assert [line.text for line in lines] == ["bbb", "cc"]  # "cc" = bytes 8-9 only
    assert sum(line.byte_length for line in lines) == 6


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (0, 16, ["aaa", "bbb", "ccc", "ddd"]),  # whole file
        (12, 16, ["ddd"]),  # last line
        (8, 8, []),  # empty range
        (0, 4, ["aaa"]),  # first line only
    ],
)
def test_range_edges(write_file: WriteFile, start: int, end: int, expected: list[str]) -> None:
    path = write_file("a.txt", "aaa\nbbb\nccc\nddd\n")

    assert [line.text for line in reader().range_lines(path, UTF8, start, end)] == expected


def test_last_line_without_a_newline_is_returned(write_file: WriteFile) -> None:
    path = write_file("a.txt", "aaa\nbbb")

    assert [line.text for line in reader().range_lines(path, UTF8, 4, 7)] == ["bbb"]


def test_consecutive_ranges_tile_the_file_without_overlap_or_gap(write_file: WriteFile) -> None:
    body = "".join(f"line-{i:03d}\n" for i in range(50))  # 9-byte lines
    path = write_file("a.txt", body)
    cuts = [0, 18, 90, 216, 450]  # all on line boundaries (multiples of 9)

    seen: list[str] = []
    for start, end in pairwise(cuts):
        seen.extend(line.text for line in reader().range_lines(path, UTF8, start, end))

    assert seen == [f"line-{i:03d}" for i in range(50)]


def test_alignment_check_rejects_a_start_inside_a_line(write_file: WriteFile) -> None:
    path = write_file("a.txt", "aaa\nbbb\nccc\n")

    assert [x.text for x in reader().range_lines(path, UTF8, 4, 12, check_alignment=True)] == [
        "bbb",
        "ccc",
    ]
    assert next(iter(reader().range_lines(path, UTF8, 0, 12, check_alignment=True))).text == "aaa"
    with pytest.raises(UnsupportedDataFormatException, match="record boundary"):
        list(reader().range_lines(path, UTF8, 5, 12, check_alignment=True))


def test_a_line_over_the_cap_is_kept_whole_when_it_fits_one_window(write_file: WriteFile) -> None:
    path = write_file("a.txt", "x" * 100 + "\nnext\n")

    first, second = reader(max_line=8).range_lines(path, UTF8, 0, 106)

    assert not first.truncated and first.text == "x" * 100 and first.byte_length == 101
    assert (second.text, second.byte_length, second.truncated) == ("next", 5, False)


def test_range_ending_in_oversized_line_stops_at_end(
    write_file: WriteFile,
) -> None:
    path = write_file("a.txt", "x" * 100 + "\nnext\n")

    (only,) = reader(max_line=8).range_lines(path, UTF8, 0, 40)

    assert only.text == "x" * 40 and only.byte_length == 40


def test_crlf_terminators_are_removed(write_file: WriteFile) -> None:
    path = write_file("a.txt", b"aa\r\nbb\r\n")

    assert [x.text for x in reader().range_lines(path, UTF8, 0, 8)] == ["aa", "bb"]


def test_multibyte_encodings_cannot_be_read_by_byte_range(write_file: WriteFile) -> None:
    path = write_file("a.txt", "a\nb\n".encode("utf-16-le"))

    with pytest.raises(UnsupportedDataFormatException, match="utf-16"):
        list(reader().range_lines(path, EncodingInfo("utf-16-le", ascii_compatible=False), 0, 8))




def test_email_extractor_finds_addresses_and_ignores_near_misses() -> None:
    extractor = RegexExtractor.emails()
    line = (
        "contact a.hollis@internal.corp.test or (g.okafor@hq.corp.test), "
        "ignore @handle name@host svc@@x.test a@b mail@.test; "
        "final: R.Dunn+audit@Sub.Corp.TEST."
    )

    found = [record[0] for record in extractor.extract(line)]

    assert found == [
        "a.hollis@internal.corp.test",
        "g.okafor@hq.corp.test",
        "R.Dunn+audit@Sub.Corp.TEST",
    ]
    assert extractor.fields == ("email",)


def test_regex_groups_become_columns() -> None:
    extractor = RegexExtractor(r"(\w+)=(\d+)(?:;(x))?", ("key", "value", "flag"))

    assert list(extractor.extract("a=1;x b=22")) == [("a", "1", "x"), ("b", "22", "")]


def test_regex_without_groups_defaults_to_a_single_match_column() -> None:
    extractor = RegexExtractor(r"\d{3}")

    assert extractor.fields == ("match",)
    assert list(extractor.extract("a 123 b 456")) == [("123",), ("456",)]


def test_bad_pattern_or_field_count_is_a_config_error() -> None:
    with pytest.raises(InvalidConfigurationException, match="invalid regular expression"):
        RegexExtractor("(unclosed")
    with pytest.raises(InvalidConfigurationException, match="field name"):
        RegexExtractor(r"(a)(b)", ("only_one",))


@pytest.mark.parametrize(
    "build_line",
    [
        pytest.param(lambda: "a" * 1_000_000, id="one-huge-local-part-run"),
        pytest.param(lambda: "a@" * 200_000, id="at-sign-after-every-char"),
        pytest.param(lambda: "a." * 400_000 + "@", id="dotted-run-ending-in-at"),
        pytest.param(lambda: "@" * 1_000_000, id="only-at-signs"),
        pytest.param(lambda: ("x" * 60 + "@" + "y" * 60 + ".") * 5_000, id="repeating-near-matches"),
    ],
)
def test_email_pattern_scans_hostile_lines_in_bounded_time(build_line: Callable[[], str]) -> None:
    extractor = RegexExtractor.emails()
    hostile = build_line()

    started = time.perf_counter()
    list(extractor.extract(hostile))

    assert time.perf_counter() - started < 3.0




def test_csv_formatter_quotes_per_rfc_4180() -> None:
    formatter = CsvFormatter(("a", "b"))

    assert formatter.header() == b"a,b\n"
    data = formatter.format(['has,comma and "quotes"', "multi\nline"])
    assert list(csv.reader(io.StringIO(data.decode("utf-8")))) == [
        ['has,comma and "quotes"', "multi\nline"]
    ]
    assert formatter.format(["é", "ü"]).decode("utf-8") == "é,ü\n"


def test_csv_formula_guard_neutralises_spreadsheet_formulas() -> None:
    guarded = CsvFormatter(("v",), formula_guard=True)
    plain = CsvFormatter(("v",))

    assert guarded.format(["=cmd|' /c calc'!A1"]) == b"'=cmd|' /c calc'!A1\n"
    assert guarded.format(["@SUM(1)"]) == b"'@SUM(1)\n"
    assert guarded.format(["safe"]) == b"safe\n"
    assert plain.format(["=1+1"]) == b"=1+1\n"  # opt-in only: values untouched by default


def test_jsonl_formatter_writes_one_object_per_line() -> None:
    formatter = JsonlFormatter(("email", "n"))

    assert formatter.header() == b""
    data = formatter.format(["é@mx.corp.test", "1"])
    assert json.loads(data) == {"email": "é@mx.corp.test", "n": "1"}
    assert data.endswith(b"\n") and data.count(b"\n") == 1


def test_formatters_reject_records_of_the_wrong_width() -> None:
    with pytest.raises(ValueError, match="expected 2"):
        CsvFormatter(("a", "b")).format(["only-one"])
    with pytest.raises(ValueError, match="expected 2"):
        JsonlFormatter(("a", "b")).format(["x", "y", "z"])




def make_worker(scratch: LocalScratchStore, max_line: int = 1024 * 1024) -> MinerWorker:
    return MinerWorker(
        strategy=RegexExtractor.emails(),
        formatter=CsvFormatter(("email",)),
        streams=FileStreamReader(64 * 1024, max_line),
        scratch=scratch,
    )


def line_text(i: int) -> str:
    return f"{i:04d},note,emp{i}@internal.corp.test,tail\n"


def test_worker_writes_only_its_chunks_matches_to_the_scratch_file(
    state_dir: Path, write_file: WriteFile
) -> None:
    lines = [line_text(i) for i in range(10)]
    path = write_file("data.csv", "".join(lines))
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    scratch = LocalScratchStore(state_dir, allowed_roots=[SCRATCH_ROOT])
    source = SourceDescriptor(path, UTF8)

    result = make_worker(scratch).process(source, ChunkMetadata(2, offsets[3], offsets[6]))

    assert (result.lines_read, result.records_written) == (3, 3)
    assert scratch.tmp_path(2).read_bytes() == b"".join(
        f"emp{i}@internal.corp.test\n".encode() for i in (3, 4, 5)
    )
    assert result.bytes_written == scratch.tmp_size(2)


def test_first_chunk_skips_bom_and_header(state_dir: Path, write_file: WriteFile) -> None:
    body = "id,contact\n" + "".join(f"{i},emp{i}@internal.corp.test\n" for i in range(5))
    path = write_file("data.csv", b"\xef\xbb\xbf" + body.replace("contact", "it.desk@header.corp.test").encode())
    profile = create_profiler().profile(path)
    assert profile.data_offset > 3  # BOM + header
    scratch = LocalScratchStore(state_dir, allowed_roots=[SCRATCH_ROOT])

    result = make_worker(scratch).process(
        SourceDescriptor(path, profile.encoding, profile.data_offset),
        ChunkMetadata(1, 0, profile.size_bytes),
    )

    mined = scratch.tmp_path(1).read_text().split()
    assert mined == [f"emp{i}@internal.corp.test" for i in range(5)]  # not it.desk@header.corp.test
    assert result.lines_read == 5


def test_stale_scratch_content_is_overwritten(
    state_dir: Path, write_file: WriteFile
) -> None:
    path = write_file("data.csv", line_text(1) + line_text(2))
    scratch = LocalScratchStore(state_dir, allowed_roots=[SCRATCH_ROOT])
    scratch.tmp_path(1).write_bytes(b"STALE-PARTIAL-DATA-" * 1000)  # longer than the new output

    make_worker(scratch).process(SourceDescriptor(path, UTF8), ChunkMetadata(1, 0, path.stat().st_size))

    assert scratch.tmp_path(1).read_bytes() == b"emp1@internal.corp.test\nemp2@internal.corp.test\n"


def test_a_chunk_with_no_matches_leaves_an_empty_scratch_file(
    state_dir: Path, write_file: WriteFile
) -> None:
    path = write_file("data.csv", "nothing here\nnor here\n")
    scratch = LocalScratchStore(state_dir, allowed_roots=[SCRATCH_ROOT])

    result = make_worker(scratch).process(SourceDescriptor(path, UTF8), ChunkMetadata(1, 0, 22))

    assert (result.lines_read, result.records_written, result.bytes_written) == (2, 0, 0)
    assert scratch.tmp_size(1) == 0


def test_a_chunk_that_starts_mid_line_is_refused(state_dir: Path, write_file: WriteFile) -> None:
    path = write_file("data.csv", line_text(1) + line_text(2))
    scratch = LocalScratchStore(state_dir, allowed_roots=[SCRATCH_ROOT])

    with pytest.raises(UnsupportedDataFormatException, match="record boundary"):
        make_worker(scratch).process(SourceDescriptor(path, UTF8), ChunkMetadata(2, 7, path.stat().st_size))


def test_oversized_lines_are_searched_in_full_and_counted(
    state_dir: Path, write_file: WriteFile
) -> None:
    giant = "pad " * 100 + "sys.admin@internal.corp.test"  # the address sits beyond the line cap
    path = write_file("data.csv", line_text(1) + giant + "\n" + line_text(2))
    scratch = LocalScratchStore(state_dir, allowed_roots=[SCRATCH_ROOT])

    result = make_worker(scratch, max_line=128).process(
        SourceDescriptor(path, UTF8), ChunkMetadata(1, 0, path.stat().st_size)
    )

    assert result.oversized_lines == 1 and result.lines_read == 3
    assert scratch.tmp_path(1).read_text().split() == [
        "emp1@internal.corp.test",
        "sys.admin@internal.corp.test",
        "emp2@internal.corp.test",
    ]


def test_worker_memory_is_constant_regardless_of_chunk_size(
    state_dir: Path, workspace: Path
) -> None:
    path = workspace / f"big-{state_dir.name}.csv"
    block = "".join(line_text(i) for i in range(20_000)).encode()
    with path.open("wb") as sink:
        for _ in range(16 * MIB // len(block) + 1):
            sink.write(block)
    try:
        scratch = LocalScratchStore(state_dir, allowed_roots=[SCRATCH_ROOT])
        worker = make_worker(scratch)
        size = path.stat().st_size
        assert size > 16 * MIB

        tracemalloc.start()
        try:
            result = worker.process(SourceDescriptor(path, UTF8), ChunkMetadata(1, 0, size))
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        assert result.records_written > 300_000
        assert peak < 1 * MIB, f"processing a {size / MIB:.0f} MiB chunk allocated {peak / MIB:.2f} MiB"
    finally:
        path.unlink(missing_ok=True)


def test_range_past_end_of_file_is_an_error(
    write_file: WriteFile,
) -> None:
    path = write_file("a.txt", "aaa\nbbb\n")

    with pytest.raises(DataSourceUnavailableException, match="shorter than the byte range"):
        list(reader().range_lines(path, UTF8, 0, 100))


def test_file_truncated_during_read_raises(
    write_file: WriteFile,
) -> None:
    path = write_file("a.txt", "0123456789\n" * 400_000)  # 4.4 MB: far beyond any read-ahead buffer
    lines = reader().range_lines(path, UTF8, 0, path.stat().st_size)
    assert next(lines).text == "0123456789"

    with path.open("r+b") as handle:
        handle.truncate(300_000)

    with pytest.raises(DataSourceUnavailableException, match="truncated or replaced"):
        list(lines)


def test_chunk_beyond_shrunken_file_reports_shrink(
    write_file: WriteFile,
) -> None:
    path = write_file("a.txt", "aaa\nbbb\n")

    with pytest.raises(DataSourceUnavailableException):
        list(reader().range_lines(path, UTF8, 40, 80, check_alignment=True))
