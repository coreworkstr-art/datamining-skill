"""Compressed and UTF-16/32 sources are converted to a plain UTF-8 copy and mined like any file."""

from __future__ import annotations

import bz2
import gzip
import lzma
import os
import struct
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from datamining_skill import (
    InvalidConfigurationException,
    MiningProgress,
    UnsupportedDataFormatException,
    run_mining,
)
from datamining_skill.application.options import MiningOptions
from datamining_skill.bootstrap import profile_source, run_layout
from datamining_skill.infrastructure import source_prep
from datamining_skill.infrastructure.source_prep import (
    _vet_zip_central_directory,
    convert_to_utf8,
    needs_conversion,
)
from tests.test_orchestration import Crash

ROWS = 600
EMAILS = [f"emp{i}@internal.corp.test" for i in range(ROWS)]
CSV_TEXT = "id,name,email\r\n" + "".join(f"{i},José Peña {i},{e}\r\n" for i, e in enumerate(EMAILS))
CSV_BYTES = CSV_TEXT.encode("utf-8")

Compressor = Callable[[Path], None]


def write_gzip(path: Path) -> None:
    path.write_bytes(gzip.compress(CSV_BYTES))


def write_bzip2(path: Path) -> None:
    path.write_bytes(bz2.compress(CSV_BYTES))


def write_xz(path: Path) -> None:
    path.write_bytes(lzma.compress(CSV_BYTES))


def write_zip(path: Path) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("export.csv", CSV_BYTES)


def write_utf16(path: Path) -> None:
    path.write_bytes(b"\xff\xfe" + CSV_TEXT.encode("utf-16-le"))


def write_utf16_be(path: Path) -> None:
    path.write_bytes(b"\xfe\xff" + CSV_TEXT.encode("utf-16-be"))


def write_utf32(path: Path) -> None:
    path.write_bytes(b"\xff\xfe\x00\x00" + CSV_TEXT.encode("utf-32-le"))


def write_gzip_utf16(path: Path) -> None:
    path.write_bytes(gzip.compress(b"\xff\xfe" + CSV_TEXT.encode("utf-16-le")))


CASES: list[tuple[str, Compressor, str]] = [
    ("export.csv.gz", write_gzip, "gzip"),
    ("export.csv.bz2", write_bzip2, "bzip2"),
    ("export.csv.xz", write_xz, "xz"),
    ("export.zip", write_zip, "zip"),
    ("excel.txt", write_utf16, "utf-16-le"),
    ("excel_be.txt", write_utf16_be, "utf-16-be"),
    ("wide.txt", write_utf32, "utf-32-le"),
    ("nested.csv.gz", write_gzip_utf16, "gzip+utf-16-le"),
]


def rows(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "email"
    return lines[1:]


@pytest.mark.parametrize(("name", "writer", "transform"), CASES, ids=[c[0] for c in CASES])
def test_a_converted_source_gives_the_same_result_as_the_plain_file(
    state_dir: Path, name: str, writer: Compressor, transform: str
) -> None:
    source = state_dir / name
    writer(source)

    summary = run_mining(source, state_dir / "out.csv", workspace=state_dir)

    assert summary.source_transform == transform and summary.succeeded
    assert rows(state_dir / "out.csv") == EMAILS


@pytest.mark.parametrize(("name", "writer", "transform"), CASES, ids=[c[0] for c in CASES])
def test_the_converted_copy_is_removed_when_the_job_finishes(
    state_dir: Path, name: str, writer: Compressor, transform: str
) -> None:
    source = state_dir / name
    writer(source)

    run_mining(source, state_dir / "out.csv", workspace=state_dir)

    leftovers = [p.name for p in (state_dir / ".scratch").rglob("*") if p.is_file() and p.suffix in (".utf8", ".partial", ".sample")]
    assert leftovers == []


def test_a_plain_utf8_file_with_a_bom_is_not_converted(state_dir: Path) -> None:
    source = state_dir / "plain.csv"
    source.write_bytes(b"\xef\xbb\xbf" + CSV_BYTES)

    assert not needs_conversion(source)
    assert run_mining(source, state_dir / "out.csv", workspace=state_dir).source_transform is None


def test_an_interrupted_job_reuses_its_converted_copy_and_finishes(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = state_dir / "big.csv.gz"
    text = "id,email\n" + "".join(f"{i},emp{i}@internal.corp.test\n" for i in range(60_000))
    source.write_bytes(gzip.compress(text.encode()))
    conversions = 0
    real = source_prep.convert_to_utf8

    def counting(*args: Any, **kwargs: Any) -> str | None:
        nonlocal conversions
        conversions += 1
        return real(*args, **kwargs)

    monkeypatch.setattr("datamining_skill.bootstrap.convert_to_utf8", counting)
    reports = 0

    def stop_after_the_first_chunk(_: MiningProgress) -> None:
        nonlocal reports
        reports += 1
        if reports == 2:
            raise Crash

    from tests.support import load_simulation
    from tests.test_unique_mining import SMALL

    sim = load_simulation()
    options: dict[str, Any] = {"chunking_config": SMALL, "memory_provider": sim.FixedMemory(source.stat().st_size * 4)}
    with pytest.raises(Crash):
        run_mining(source, state_dir / "out.csv", workspace=state_dir, on_progress=stop_after_the_first_chunk, **options)
    layout = run_layout(state_dir, source, state_dir / "out.csv", MiningOptions().fingerprint())
    assert (layout.scratch_dir / "source.utf8").is_file()  # kept while the job is unfinished

    resumed = run_mining(source, state_dir / "out.csv", workspace=state_dir, **options)

    assert conversions == 1  # the second run did not convert again
    assert resumed.resumed and resumed.succeeded and resumed.source_transform == "gzip"
    assert rows(state_dir / "out.csv") == [f"emp{i}@internal.corp.test" for i in range(60_000)]
    assert not (layout.scratch_dir / "source.utf8").exists()


def test_a_finished_compressed_job_is_reported_without_converting_again(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = state_dir / "export.csv.gz"
    write_gzip(source)
    run_mining(source, state_dir / "out.csv", workspace=state_dir)
    monkeypatch.setattr("datamining_skill.bootstrap.convert_to_utf8", lambda *a, **k: pytest.fail("converted again"))

    again = run_mining(source, state_dir / "out.csv", workspace=state_dir)

    assert again.already_complete and again.source_transform == "gzip"


def test_a_decompression_bomb_is_stopped_at_the_limit_and_leaves_nothing(state_dir: Path) -> None:
    source = state_dir / "bomb.csv.gz"
    source.write_bytes(gzip.compress(b"id,email\n" + b"1,a@internal.corp.test\n" * 200_000))  # about 4.5 MB

    with pytest.raises(UnsupportedDataFormatException, match="expands to more than"):
        run_mining(source, state_dir / "out.csv", workspace=state_dir, max_expanded_bytes=1_000_000)

    assert [p for p in (state_dir / ".scratch").rglob("*") if p.is_file() and p.suffix in (".utf8", ".partial")] == []
    assert not (state_dir / "out.csv").exists()


def test_a_planted_link_cannot_redirect_the_converted_copy(state_dir: Path) -> None:
    source = state_dir / "in.csv.gz"
    source.write_bytes(gzip.compress(CSV_BYTES))
    victim = state_dir / "victim.txt"
    victim.write_text("retained export")
    try:
        (state_dir / "converted.utf8.partial").symlink_to(victim)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not available to this user")

    with pytest.raises(InvalidConfigurationException, match="symbolic link"):
        convert_to_utf8(source, state_dir / "converted.utf8")

    assert victim.read_text() == "retained export"
    assert not (state_dir / "converted.utf8").exists()


def test_damaged_compressed_data_is_reported_plainly(state_dir: Path) -> None:
    source = state_dir / "cut.csv.gz"
    source.write_bytes(gzip.compress(CSV_BYTES)[:60])  # a truncated download

    with pytest.raises(UnsupportedDataFormatException, match="damaged or incomplete"):
        run_mining(source, state_dir / "out.csv", workspace=state_dir)


def test_a_zip_with_several_files_asks_for_the_one_to_mine(state_dir: Path) -> None:
    source = state_dir / "many.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("a.csv", "x\n")
        archive.writestr("b.csv", "y\n")

    with pytest.raises(UnsupportedDataFormatException, match="zip archive with 2 files"):
        run_mining(source, state_dir / "out.csv", workspace=state_dir)


# ---------------------------------------------------- the zip central directory is vetted first


def end_record(entries: int, directory_bytes: int, comment: bytes = b"", *, comment_length: int | None = None) -> bytes:
    length = len(comment) if comment_length is None else comment_length
    return struct.pack("<4sHHHHIIH", b"PK\x05\x06", 0, 0, entries, entries, directory_bytes, 0, length) + comment


def zip64_records(entries: int, directory_bytes: int) -> bytes:
    """A ZIP64 end record followed by its locator, as they sit right before the end record."""
    record = struct.pack("<4sQHHIIQQQQ", b"PK\x06\x06", 44, 45, 45, 0, 0, entries, entries, directory_bytes, 0)
    return record + struct.pack("<4sIQI", b"PK\x06\x07", 0, 0, 1)


def crafted_zip(path: Path, tail: bytes, *, padding: int = 200) -> Path:
    path.write_bytes(b"PK\x03\x04" + b"\x00" * padding + tail)
    return path


@pytest.fixture
def zipfile_must_not_open(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("zipfile was asked to read a central directory that should have been refused")

    monkeypatch.setattr(zipfile, "ZipFile", boom)


@pytest.mark.parametrize(
    ("entries", "directory_bytes"),
    [
        pytest.param(50_000, 2_300_000, id="fifty-thousand-entries"),
        pytest.param(1, 10 * 1024 * 1024, id="ten-mebibyte-directory"),
        pytest.param(17, 800, id="one-entry-too-many"),
        pytest.param(1, 65_537, id="directory-one-byte-too-large"),
        pytest.param(0xFFFF, 0xFFFFFFFF, id="zip64-sentinels-without-zip64-records"),
        pytest.param(0xFFFF, 100, id="entry-count-sentinel"),
        pytest.param(1, 0xFFFFFFFF, id="directory-size-sentinel"),
    ],
)
def test_a_zip_claiming_a_large_central_directory_is_refused_before_zipfile_reads_it(
    state_dir: Path, zipfile_must_not_open: None, entries: int, directory_bytes: int
) -> None:
    source = crafted_zip(state_dir / "bomb.zip", end_record(entries, directory_bytes))

    with pytest.raises(UnsupportedDataFormatException, match="too many entries; extract the file to mine first"):
        convert_to_utf8(source, state_dir / "out.utf8")

    assert not (state_dir / "out.utf8").exists() and not (state_dir / "out.utf8.partial").exists()


def test_the_zip64_records_are_read_when_the_archive_has_them(state_dir: Path, zipfile_must_not_open: None) -> None:
    tail = zip64_records(5_000_000, 300_000_000) + end_record(0xFFFF, 0xFFFFFFFF)
    source = crafted_zip(state_dir / "zip64.zip", tail)

    with pytest.raises(UnsupportedDataFormatException, match="too many entries"):
        convert_to_utf8(source, state_dir / "out.utf8")


def test_zip64_values_win_over_an_innocent_looking_end_record(state_dir: Path) -> None:
    """zipfile prefers the ZIP64 record, so the check must look at it and not at the small 32-bit fields."""
    source = crafted_zip(state_dir / "split.zip", zip64_records(5_000_000, 300_000_000) + end_record(1, 120))

    with pytest.raises(UnsupportedDataFormatException, match="too many entries"):
        _vet_zip_central_directory(source)


def test_small_zip64_values_pass_the_check(state_dir: Path) -> None:
    source = crafted_zip(state_dir / "small64.zip", zip64_records(1, 120) + end_record(0xFFFF, 0xFFFFFFFF))

    _vet_zip_central_directory(source)  # a real single-file ZIP64 archive is not turned away


def test_the_record_that_zipfile_will_use_is_the_one_checked(state_dir: Path) -> None:
    """The last end-record signature counts, even if its comment length does not add up."""
    decoy = end_record(1, 120)
    real = end_record(50_000, 2_300_000, comment_length=9999)  # claims a long comment that is not there
    source = crafted_zip(state_dir / "decoy.zip", decoy + b"\x00" * 30 + real)

    with pytest.raises(UnsupportedDataFormatException, match="too many entries"):
        _vet_zip_central_directory(source)


def test_an_end_record_buried_under_the_longest_comment_is_still_found(state_dir: Path) -> None:
    source = crafted_zip(state_dir / "comment.zip", end_record(50_000, 2_300_000, b"x" * 0xFFFF))

    with pytest.raises(UnsupportedDataFormatException, match="too many entries"):
        _vet_zip_central_directory(source)


def test_a_file_without_an_end_record_is_left_for_zipfile_to_report(state_dir: Path) -> None:
    source = state_dir / "cut.zip"
    source.write_bytes(b"PK\x03\x04" + b"\x00" * 5000)

    _vet_zip_central_directory(source)  # no verdict: zipfile says "damaged"
    with pytest.raises(UnsupportedDataFormatException, match="damaged or incomplete"):
        convert_to_utf8(source, state_dir / "out.utf8")


def test_the_check_reads_a_fixed_number_of_bytes_whatever_the_file_size(state_dir: Path) -> None:
    source = state_dir / "large.zip"
    with source.open("wb") as handle:
        handle.write(b"PK\x03\x04")
        handle.truncate(300 * 1024 * 1024)  # a sparse 300 MiB archive
        handle.seek(0, os.SEEK_END)
        handle.write(end_record(50_000, 2_300_000))
    reads: list[int] = []
    real_open = Path.open

    class CountingReader:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def __enter__(self) -> CountingReader:
            return self

        def __exit__(self, *_exc: object) -> None:
            self._inner.close()

        def seek(self, *args: Any) -> int:
            return int(self._inner.seek(*args))

        def read(self, size: int = -1) -> bytes:
            reads.append(size)
            return bytes(self._inner.read(size))

    def counting_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        handle = real_open(self, *args, **kwargs)
        return CountingReader(handle) if self == source else handle

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Path, "open", counting_open)
        with pytest.raises(UnsupportedDataFormatException):
            _vet_zip_central_directory(source)

    assert reads and max(reads) <= 70_000  # a few tens of kilobytes, not 300 MiB


def test_an_ordinary_single_file_zip_with_a_few_folder_entries_is_still_converted(state_dir: Path) -> None:
    source = state_dir / "ok.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("export/", "")
        archive.writestr("export/2026/", "")
        archive.writestr("export/2026/list.csv", CSV_TEXT)

    transform = convert_to_utf8(source, state_dir / "out.utf8")

    assert transform == "zip" and (state_dir / "out.utf8").read_bytes() == CSV_TEXT.encode("utf-8")


def test_a_zip_with_a_long_comment_is_still_converted(state_dir: Path) -> None:
    source = state_dir / "commented.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("list.csv", CSV_TEXT)
        archive.comment = b"c" * 60_000

    assert convert_to_utf8(source, state_dir / "out.utf8") == "zip"


def test_a_real_archive_of_many_files_is_refused_early(state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = state_dir / "many.zip"
    with zipfile.ZipFile(source, "w") as archive:
        for index in range(40):
            archive.writestr(f"part{index}.csv", "x\n")

    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("zipfile was asked to read an archive that should have been refused")

    monkeypatch.setattr(zipfile, "ZipFile", boom)  # only now: the archive above needed it

    with pytest.raises(UnsupportedDataFormatException, match="too many entries"):
        convert_to_utf8(source, state_dir / "out.utf8")


def test_the_pipeline_refuses_a_zip_bomb_header_with_the_plain_message(state_dir: Path) -> None:
    source = crafted_zip(state_dir / "bomb.zip", end_record(50_000, 2_300_000))

    with pytest.raises(UnsupportedDataFormatException) as caught:
        run_mining(source, state_dir / "out.csv", workspace=state_dir)

    assert "too many entries" in str(caught.value) and str(state_dir) not in str(caught.value)
    assert not (state_dir / "out.csv").exists()


def test_compressed_binary_content_is_still_refused(state_dir: Path) -> None:
    source = state_dir / "blob.gz"
    source.write_bytes(gzip.compress(bytes(range(256)) * 200))

    with pytest.raises(UnsupportedDataFormatException, match="binary"):
        run_mining(source, state_dir / "out.csv", workspace=state_dir)


def test_convert_to_utf8_keeps_characters_outside_the_basic_plane(state_dir: Path) -> None:
    source = state_dir / "emoji.txt"
    source.write_bytes(b"\xff\xfe" + "mail a@b.test \N{GRINNING FACE}\r\n".encode("utf-16-le"))
    target = state_dir / "emoji.utf8"

    assert convert_to_utf8(source, target) == "utf-16-le"

    assert target.read_bytes() == "mail a@b.test \N{GRINNING FACE}\r\n".encode()


def test_a_head_sample_stops_early(state_dir: Path) -> None:
    source = state_dir / "long.csv.gz"
    source.write_bytes(gzip.compress(b"x" * 5_000_000))
    target = state_dir / "sample.txt"

    convert_to_utf8(source, target, sample_bytes=100_000)

    assert 100_000 <= target.stat().st_size < 2_200_000  # one block past the target at most


def test_profiling_a_compressed_file_judges_a_sample_and_says_so(state_dir: Path) -> None:
    source = state_dir / "export.csv.gz"
    write_gzip(source)

    result = profile_source(source, workspace=state_dir)

    assert result["file"]["name"] == "export.csv.gz" and result["file"]["size_bytes"] == source.stat().st_size
    assert result["format"] == "csv" and result["structure"]["fields"] == ["id", "name", "email"]
    assert result["compression"] == "gzip" and "decompressed" in result["sample"]["scope"]
    assert result["records"]["count"] is None and result["records"]["is_exact"] is False
    assert list((state_dir / ".scratch").rglob("*.sample")) == []


def test_profiling_errors_name_the_original_file(state_dir: Path) -> None:
    source = state_dir / "empty.txt.gz"
    source.write_bytes(gzip.compress(b""))

    with pytest.raises(UnsupportedDataFormatException, match=r"empty\.txt\.gz"):
        profile_source(source, workspace=state_dir)


def test_a_utf16_file_is_profiled_in_place(state_dir: Path) -> None:
    source = state_dir / "excel.txt"
    write_utf16(source)

    result = profile_source(source, workspace=state_dir)

    assert result["encoding"]["name"] == "utf-16-le" and "compression" not in result


def test_a_nearly_full_disk_stops_the_conversion_and_leaves_nothing(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from datamining_skill import ResourceExhaustionError

    source = state_dir / "big.csv.gz"
    source.write_bytes(gzip.compress(b"id,email\n" + b"1,a@internal.corp.test\n" * 200_000))
    monkeypatch.setattr(source_prep, "_FREE_CHECK_INTERVAL", 1024 * 1024)
    monkeypatch.setattr(
        "datamining_skill.infrastructure.source_prep.shutil.disk_usage",
        lambda path: SimpleNamespace(free=10 * 1024 * 1024),
    )

    with pytest.raises(ResourceExhaustionError, match="disk space"):
        run_mining(source, state_dir / "out.csv", workspace=state_dir)

    assert [p for p in (state_dir / ".scratch").rglob("*") if p.is_file() and p.suffix in (".utf8", ".partial")] == []
