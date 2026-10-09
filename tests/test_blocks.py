"""Searching blocks of lines must give exactly the result of searching the lines one by one."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from datamining_skill import DataSourceUnavailableException, UnsupportedDataFormatException, run_mining
from datamining_skill.application.extraction_strategy import EMAIL_PATTERN, ExtractionStrategy, RegexExtractor
from datamining_skill.application.miner_worker import MinerWorker, SourceDescriptor
from datamining_skill.application.record_formats import CsvFormatter
from datamining_skill.domain.models import ChunkMetadata, EncodingInfo, TextBlock
from datamining_skill.infrastructure import FileStreamReader
from datamining_skill.infrastructure.scratch import LocalScratchStore
from tests.conftest import SCRATCH_ROOT, WriteFile
from tests.test_long_lines import expected_in, random_line
from tests.test_unique_mining import build_source

UTF8 = EncodingInfo("utf-8")


def blocks_of(path: Path, *, chunk: int, max_line: int, start: int = 0, end: int | None = None) -> list[TextBlock]:
    reader = FileStreamReader(chunk_size_bytes=chunk, max_line_bytes=max_line)
    return list(reader.range_blocks(path, UTF8, start, end if end is not None else path.stat().st_size))


def found_in_blocks(blocks: list[TextBlock]) -> list[str]:
    extractor = RegexExtractor.emails()
    found: list[str] = []
    for block in blocks:
        if block.emit_from or block.emit_until is not None:
            records = extractor.extract(block.text, block.emit_from, block.emit_until)
        else:
            records = extractor.extract(block.text)
        found.extend(record[0] for record in records)
    return found


def line_count(data: bytes) -> int:
    return data.count(b"\n") + (0 if data.endswith(b"\n") or not data else 1)


@pytest.mark.parametrize(("chunk", "max_line"), [(64, 1024), (1000, 2048), (4096, 4096), (65_536, 65_536), (300, 8192)])
def test_blocks_find_what_the_lines_hold_and_account_for_every_byte(
    write_file: WriteFile, chunk: int, max_line: int
) -> None:
    rng = random.Random(chunk * 31 + max_line)
    lengths = [3, 40, 700, max_line - 2, max_line, max_line + 1, 3 * max_line, 20_000]
    lines = [random_line(rng, rng.choice(lengths)) for _ in range(40)]
    data = ("\n".join(lines) + "\n").encode("utf-8")
    path = write_file("mixed.txt", data)

    blocks = blocks_of(path, chunk=chunk, max_line=max_line)

    assert found_in_blocks(blocks) == expected_in(data)
    assert sum(block.byte_length for block in blocks) == len(data)
    assert sum(block.line_count for block in blocks) == line_count(data)


@pytest.mark.parametrize("terminator", ["\n", "\r\n"])
@pytest.mark.parametrize("final_terminator", [True, False])
def test_every_line_ending_and_a_missing_last_one_are_handled(
    write_file: WriteFile, terminator: str, final_terminator: bool
) -> None:
    rows = [f"row {i} mail ada{i}@internal.corp.test and x{i}@mx.corp.test" for i in range(500)]
    data = (terminator.join(rows) + (terminator if final_terminator else "")).encode()
    path = write_file("rows.txt", data)

    blocks = blocks_of(path, chunk=512, max_line=4096)

    assert found_in_blocks(blocks) == expected_in(data)
    assert "".join(block.text for block in blocks) == data.decode()  # nothing lost, nothing repeated
    assert sum(block.line_count for block in blocks) == 500


def test_a_range_is_cut_at_its_end_and_starts_after_a_line_feed(write_file: WriteFile) -> None:
    data = b"".join(f"mail{i:03d}@internal.corp.test\n".encode() for i in range(100))
    path = write_file("range.txt", data)
    width = len(b"mail000@internal.corp.test\n")

    blocks = blocks_of(path, chunk=200, max_line=1024, start=10 * width, end=60 * width)

    assert sum(block.byte_length for block in blocks) == 50 * width
    assert found_in_blocks(blocks) == [f"mail{i:03d}@internal.corp.test" for i in range(10, 60)]


def test_a_start_inside_a_line_is_refused_when_alignment_is_checked(write_file: WriteFile) -> None:
    path = write_file("a.txt", "aaa\nbbb\nccc\n")
    reader = FileStreamReader(1024, 1024)

    with pytest.raises(UnsupportedDataFormatException, match="record boundary"):
        list(reader.range_blocks(path, UTF8, 5, 12, check_alignment=True))
    assert "".join(b.text for b in reader.range_blocks(path, UTF8, 4, 12, check_alignment=True)) == "bbb\nccc\n"


def test_a_file_shorter_than_the_range_is_an_error(write_file: WriteFile) -> None:
    path = write_file("a.txt", "aaa\nbbb\n")
    reader = FileStreamReader(1024, 1024)

    with pytest.raises(DataSourceUnavailableException, match="shorter than the byte range"):
        list(reader.range_blocks(path, UTF8, 0, 100))


def test_multibyte_encodings_cannot_be_read_by_blocks(write_file: WriteFile) -> None:
    path = write_file("a.txt", "a\nb\n".encode("utf-16-le"))

    with pytest.raises(UnsupportedDataFormatException, match="utf-16"):
        list(FileStreamReader(1024, 1024).range_blocks(path, EncodingInfo("utf-16-le", ascii_compatible=False), 0, 8))


def test_a_line_between_the_buffer_and_the_cap_arrives_whole_and_a_longer_one_in_windows(write_file: WriteFile) -> None:
    middle = "m" * 3000 + " mid@internal.corp.test"  # longer than the buffer, shorter than the cap
    giant = "g" * 3000 + " big@internal.corp.test " + "x " * 20_000  # longer than the cap
    data = f"a@internal.corp.test\n{middle}\n{giant}\nz@internal.corp.test\n".encode()
    path = write_file("long.txt", data)

    blocks = blocks_of(path, chunk=1024, max_line=16_384)

    whole = [b for b in blocks if len(b.text) > 3000 and b.emit_until is None and b.emit_from == 0]
    windows = [b for b in blocks if b.emit_from or b.emit_until is not None]
    assert any("mid@internal.corp.test" in b.text for b in whole)
    assert len(windows) > 1
    assert found_in_blocks(blocks) == [
        "a@internal.corp.test",
        "mid@internal.corp.test",
        "big@internal.corp.test",
        "z@internal.corp.test",
    ]
    assert sum(block.byte_length for block in blocks) == len(data)


def line_mode_extractor() -> ExtractionStrategy:
    """The same pattern as the built-in extraction, but a caller's pattern is searched line by line."""
    return RegexExtractor(EMAIL_PATTERN, ("email",))


@pytest.mark.parametrize("max_line", [1024, 4096])
def test_the_worker_writes_the_same_bytes_and_counts_by_blocks_and_by_lines(
    state_dir: Path, write_file: WriteFile, max_line: int
) -> None:
    rng = random.Random(max_line)
    lengths = [5, 90, 800, max_line - 1, max_line + 1, 4 * max_line]
    text = "\n".join(random_line(rng, rng.choice(lengths)) for _ in range(120)) + "\n"
    path = write_file("data.txt", text)
    scratch = LocalScratchStore(state_dir, allowed_roots=[SCRATCH_ROOT])

    def mine(strategy: ExtractionStrategy, chunk_id: int) -> tuple[bytes, tuple[int, ...]]:
        worker = MinerWorker(
            strategy=strategy,
            formatter=CsvFormatter(("email",)),
            streams=FileStreamReader(2048, max_line),
            scratch=scratch,
            json_escapes="on",
        )
        result = worker.process(SourceDescriptor(path, UTF8), ChunkMetadata(chunk_id, 0, path.stat().st_size))
        counts = (result.lines_read, result.records_written, result.bytes_written, result.oversized_lines)
        return scratch.tmp_path(chunk_id).read_bytes(), counts

    by_blocks = mine(RegexExtractor.emails(), 1)
    by_lines = mine(line_mode_extractor(), 2)

    assert by_blocks == by_lines
    assert by_blocks[1][3] > 0  # the very long lines were really in the data


def test_blocks_and_lines_agree_with_unique_and_lowercase(state_dir: Path) -> None:
    emails, _ = build_source(state_dir / "in.csv")

    run_mining(state_dir / "in.csv", state_dir / "blocks.csv", workspace=state_dir, unique=True, lowercase=True)
    # a caller's pattern is never searched by blocks, so this one goes line by line
    run_mining(
        state_dir / "in.csv",
        state_dir / "lines.csv",
        workspace=state_dir,
        unique=True,
        lowercase=True,
        pattern=EMAIL_PATTERN,
        fields=["email"],
    )

    assert (state_dir / "blocks.csv").read_bytes() == (state_dir / "lines.csv").read_bytes()
    assert len((state_dir / "blocks.csv").read_text().splitlines()) == len({e.lower() for e in emails}) + 1
