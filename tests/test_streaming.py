"""Unit tests for the generator-based stream reader."""

from __future__ import annotations

import inspect

from datamining_skill.domain.models import EncodingInfo
from datamining_skill.infrastructure.streaming import FileStreamReader
from tests.conftest import WriteFile

UTF8 = EncodingInfo(name="utf-8")


def test_readers_are_lazy_generators(write_file: WriteFile) -> None:
    path = write_file("a.txt", "one\ntwo\n")
    reader = FileStreamReader(chunk_size_bytes=4, max_line_bytes=1024)

    assert inspect.isgenerator(reader.chunks(path, 0, 100))
    assert inspect.isgenerator(reader.lines(path, UTF8, 0, 100))


def test_chunks_respect_length_and_chunk_size(write_file: WriteFile) -> None:
    path = write_file("a.bin", b"0123456789")
    reader = FileStreamReader(chunk_size_bytes=4, max_line_bytes=1024)

    assert list(reader.chunks(path, 2, 7)) == [b"2345", b"678"]
    assert list(reader.chunks(path, 8, 100)) == [b"89"]


def test_aligned_reads_skip_partial_first_line(write_file: WriteFile) -> None:
    path = write_file("a.txt", "aaa\nbbb\nccc\n")
    reader = FileStreamReader(chunk_size_bytes=4, max_line_bytes=1024)

    mid_line = [line.text for line in reader.lines(path, UTF8, 2, 100, align=True)]
    on_boundary = [line.text for line in reader.lines(path, UTF8, 4, 100, align=True)]

    assert mid_line == ["bbb", "ccc"]
    assert on_boundary == ["bbb", "ccc"]


def test_lines_stop_after_length_budget_is_consumed(write_file: WriteFile) -> None:
    path = write_file("a.txt", "aaa\nbbb\nccc\nddd\n")
    reader = FileStreamReader(chunk_size_bytes=4, max_line_bytes=1024)

    lines = list(reader.lines(path, UTF8, 0, 5))

    assert [line.text for line in lines] == ["aaa", "bbb"]


def test_overlong_line_is_truncated_but_fully_accounted(write_file: WriteFile) -> None:
    path = write_file("a.txt", "x" * 100 + "\nnext\n")
    reader = FileStreamReader(chunk_size_bytes=16, max_line_bytes=8)

    first, second = reader.lines(path, UTF8, 0, 1_000)

    assert first.truncated is True
    assert first.text == "x" * 8
    assert first.byte_length == 101
    assert (second.text, second.byte_length, second.truncated) == ("next", 5, False)


def test_undecodable_bytes_are_replaced_not_raised(write_file: WriteFile) -> None:
    path = write_file("a.txt", b"ok\xff\xfe\n")
    reader = FileStreamReader(chunk_size_bytes=16, max_line_bytes=1024)

    (line,) = reader.lines(path, UTF8, 0, 100)

    assert line.text.startswith("ok")
    assert "�" in line.text
