"""Lines longer than the reader's size cap are searched in full, window by window.

The reference result is always the one for the whole line held in memory; windows must
reproduce it exactly: every match once, none cut in two, bytes accounted for.
"""

from __future__ import annotations

import json
import random
import re
from pathlib import Path

import pytest

from datamining_skill.application.extraction_strategy import RegexExtractor
from datamining_skill.application.json_text import decode_json_escapes
from datamining_skill.application.miner_worker import MinerWorker, SourceDescriptor
from datamining_skill.application.record_formats import CsvFormatter
from datamining_skill.domain.models import ChunkMetadata, DataFormat, EncodingInfo, TextLine
from datamining_skill.infrastructure import FileStreamReader
from datamining_skill.infrastructure.scratch import LocalScratchStore
from tests.conftest import SCRATCH_ROOT, WriteFile

UTF8 = EncodingInfo("utf-8")
EXTRACTOR = RegexExtractor.emails()


def windows_of(path: Path, max_line: int, end: int | None = None) -> list[TextLine]:
    reader = FileStreamReader(chunk_size_bytes=4096, max_line_bytes=max_line)
    return list(reader.range_lines(path, UTF8, 0, end if end is not None else path.stat().st_size))


def found_in(lines: list[TextLine]) -> list[str]:
    found: list[str] = []
    for line in lines:
        if line.is_window:
            records = EXTRACTOR.extract(line.text, line.emit_from, line.emit_until)
        else:
            records = EXTRACTOR.extract(line.text)
        found.extend(record[0] for record in records)
    return found


def expected_in(data: bytes) -> list[str]:
    found: list[str] = []
    for raw in data.split(b"\n"):
        found.extend(r[0] for r in EXTRACTOR.extract(raw.decode("utf-8", errors="replace")))
    return found


def random_line(rng: random.Random, length: int) -> str:
    pieces = ["alpha", "beta", "x", ",", " ", "\t", ";", "é", "日本", "😀", "\\u0040", "#", "-", "."]
    out: list[str] = []
    size = 0
    while size < length:
        if rng.random() < 0.08:
            local = "".join(rng.choices("abcdefghij._-", k=rng.randint(1, 20)))
            domain = "".join(rng.choices("abcdefgh", k=rng.randint(1, 15)))
            piece = f" {local}@{domain}.test "  # set apart, as addresses are in real text
        else:
            piece = rng.choice(pieces)
        out.append(piece)
        size += len(piece.encode("utf-8"))
    return "".join(out)


@pytest.mark.parametrize("max_line", [1024, 3000, 8192])
def test_windows_find_exactly_what_the_whole_line_holds(write_file: WriteFile, max_line: int) -> None:
    rng = random.Random(max_line)
    lines = [random_line(rng, rng.choice([20, 500, max_line - 1, max_line, 3 * max_line, 40_000])) for _ in range(30)]
    data = ("\n".join(lines) + "\n").encode("utf-8")
    path = write_file("mixed.txt", data)

    windows = windows_of(path, max_line)

    assert found_in(windows) == expected_in(data)
    assert sum(window.byte_length for window in windows) == len(data)  # every byte accounted for once
    assert any(window.is_window for window in windows)  # the long lines really were split


@pytest.mark.parametrize("max_line", [1024, 4096])
def test_window_memory_stays_near_the_line_cap(write_file: WriteFile, max_line: int) -> None:
    path = write_file("giant.txt", ("word " * 200_000) + "\n")

    sizes = [len(window.text.encode("utf-8")) for window in windows_of(path, max_line)]

    assert max(sizes) <= max_line + 16
    assert len(sizes) > 1000 // (max_line // 1024)


@pytest.mark.parametrize("offset", range(500, 530))
def test_a_match_that_straddles_a_window_boundary_is_found_once(write_file: WriteFile, offset: int) -> None:
    # "#" is not a snap delimiter, so the first window ends exactly at byte 512
    line = "#" * offset + "boundary.case@internal.corp.test" + "#" * 3000
    path = write_file("edge.txt", line + "\n")

    assert found_in(windows_of(path, 1024)) == ["boundary.case@internal.corp.test"]


def test_a_look_behind_still_sees_the_text_before_a_window(write_file: WriteFile) -> None:
    # the address is preceded by a local-part character at the window edge: a naive split would
    # report the shorter, wrong address
    line = "#" * 505 + "ab" + "cdefgh@internal.corp.test" + "#" * 3000
    path = write_file("lookbehind.txt", line + "\n")

    assert found_in(windows_of(path, 1024)) == ["abcdefgh@internal.corp.test"]


def test_a_range_that_ends_inside_a_long_line_stops_there(write_file: WriteFile) -> None:
    path = write_file("cut.txt", ("a@internal.corp.test " * 2000) + "\nnext@internal.corp.test\n")

    windows = windows_of(path, 1024, end=5000)

    assert sum(window.byte_length for window in windows) == 5000
    assert sum(len(window.text) for window in windows) >= 5000 - 4096  # decoded text of the range only


def test_multibyte_characters_are_never_split_between_windows(write_file: WriteFile) -> None:
    data = ("日本語" * 5000 + "\n").encode("utf-8")
    path = write_file("cjk.txt", data)

    windows = windows_of(path, 1024)

    # the context and overlap parts may begin or end inside a character; the owned span may not
    spans = [window.text[window.emit_from : window.emit_until] for window in windows]
    assert all("\N{REPLACEMENT CHARACTER}" not in span for span in spans)
    assert "".join(spans) == "日本語" * 5000
    assert sum(window.byte_length for window in windows) == len(data)


def test_crlf_line_endings_are_removed_from_the_last_window(write_file: WriteFile) -> None:
    path = write_file("crlf.txt", b"x@internal.corp.test " * 500 + b"\r\nshort\r\n")

    windows = windows_of(path, 1024)

    assert not windows[-2].text.endswith("\r") and windows[-1].text == "short"


def test_json_escapes_are_decoded_with_the_window_bounds_moved_along(
    state_dir: Path, write_file: WriteFile
) -> None:
    documents = [{"id": i, "email": f"p{i}@acme-corp.test"} for i in range(600)]
    text = json.dumps(documents)  # one line of about 40 KB
    text = text.replace("@", "\\u0040")  # every address spelled with a JSON escape
    path = write_file("one_line.json", text + "\n")
    scratch = LocalScratchStore(state_dir, allowed_roots=[SCRATCH_ROOT])
    worker = MinerWorker(
        strategy=EXTRACTOR,
        formatter=CsvFormatter(("email",)),
        streams=FileStreamReader(4096, 1024),
        scratch=scratch,
        json_escapes="auto",
    )
    descriptor = SourceDescriptor(path, UTF8, 0, DataFormat.JSON)

    result = worker.process(descriptor, ChunkMetadata(1, 0, path.stat().st_size))

    rows = scratch.tmp_path(1).read_text().split()
    assert rows == [f"p{i}@acme-corp.test" for i in range(600)]
    assert result.oversized_lines == 1 and result.records_written == 600


def test_without_decoding_escaped_addresses_are_not_matched(
    state_dir: Path, write_file: WriteFile
) -> None:
    path = write_file("plain.jsonl", '{"email": "a\\u0040internal.corp.test", "b": "c@internal.corp.test"}\n')
    scratch = LocalScratchStore(state_dir, allowed_roots=[SCRATCH_ROOT])

    def mine(mode: str, fmt: DataFormat) -> list[str]:
        worker = MinerWorker(
            strategy=EXTRACTOR,
            formatter=CsvFormatter(("email",)),
            streams=FileStreamReader(4096, 1 << 20),
            scratch=scratch,
            json_escapes=mode,
        )
        worker.process(SourceDescriptor(path, UTF8, 0, fmt), ChunkMetadata(1, 0, path.stat().st_size))
        return scratch.tmp_path(1).read_text().split()

    assert mine("off", DataFormat.JSONL) == ["c@internal.corp.test"]
    assert mine("auto", DataFormat.JSONL) == ["a@internal.corp.test", "c@internal.corp.test"]
    assert mine("auto", DataFormat.CSV) == ["c@internal.corp.test"]  # auto is for JSON sources only
    assert mine("on", DataFormat.CSV) == ["a@internal.corp.test", "c@internal.corp.test"]


@pytest.mark.parametrize(
    ("raw", "decoded"),
    [
        ("a\\u0040b.test", "a@b.test"),
        ("a\\/b", "a/b"),
        ('say \\"hi\\"', 'say "hi"'),
        ("back\\\\slash", "back\\slash"),
        ("tab\\there", "tab here"),  # control escapes become a space: a value never holds a line break
        ("nl\\u000aend", "nl end"),
        ("smile \\ud83d\\ude00", "smile \N{GRINNING FACE}"),
        ("lone \\ud83d x", "lone \N{REPLACEMENT CHARACTER} x"),
        ("\\\\u0040", "\\u0040"),  # an escaped backslash followed by plain text stays literal
        ("no escapes", "no escapes"),
        ("\\q stays", "\\q stays"),
    ],
)
def test_decode_json_escapes(raw: str, decoded: str) -> None:
    assert decode_json_escapes(raw) == decoded


class LegacyStrategy:
    """A strategy written for ``extract(line)`` before windows existed."""

    fields = ("email",)

    def extract(self, line: str) -> list[tuple[str, ...]]:
        return [(m.group(),) for m in re.finditer(r"[a-z0-9.]+@[a-z0-9.]+\.test", line)]


def test_a_strategy_that_takes_only_a_line_still_searches_very_long_lines(state_dir: Path, write_file: WriteFile) -> None:
    emails = [f"u{i}@internal.corp.test" for i in range(300)]
    path = write_file("long.txt", ("filler text " * 40 + "{} ").join([""] * 301).format(*emails) + "\n")
    scratch = LocalScratchStore(state_dir, allowed_roots=[SCRATCH_ROOT])
    worker = MinerWorker(
        strategy=LegacyStrategy(),
        formatter=CsvFormatter(("email",)),
        streams=FileStreamReader(4096, 1024),
        scratch=scratch,
    )

    result = worker.process(SourceDescriptor(path, UTF8), ChunkMetadata(1, 0, path.stat().st_size))

    assert result.oversized_lines == 1
    assert scratch.tmp_path(1).read_text().split() == emails  # none lost, none repeated
