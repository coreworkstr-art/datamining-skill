"""Text that has no structure (a list of addresses, a web page, a message), and Turkish text.

A file in which no format is recognised is read as plain lines instead of being refused, so an
address list, an ``.eml`` or a log with a layout of its own can be mined. Legacy Turkish text
(Windows-1254) is told apart from Western European text (Windows-1252), and the Turkish capital
dotted I does not leave a stray combining dot on a lower-cased address.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from datamining_skill import (
    DataFormat,
    DataProfiler,
    ProfilerConfig,
    UnsupportedDataFormatException,
    check_quality,
    create_mcp_server,
    run_mining,
)
from datamining_skill.application.extraction_strategy import EmailExtractor, RegexExtractor
from datamining_skill.application.format_detection import FormatDetector
from datamining_skill.domain.models import EncodingInfo, TextLine
from datamining_skill.infrastructure.encoding import StdlibEncodingDetector
from datamining_skill.infrastructure.handlers import CsvHandler, TextHandler
from datamining_skill.infrastructure.streaming import FileStreamReader
from tests.conftest import WriteFile
from tests.test_mcp_server import Client, call

# the Turkish letters, by name: the tests stay ASCII and no tool turns an escape into a character
U_UML = "\N{LATIN SMALL LETTER U WITH DIAERESIS}"
S_CED = "\N{LATIN SMALL LETTER S WITH CEDILLA}"
G_BREVE = "\N{LATIN SMALL LETTER G WITH BREVE}"
DOTLESS_I = "\N{LATIN SMALL LETTER DOTLESS I}"
U_UML_CAP = "\N{LATIN CAPITAL LETTER U WITH DIAERESIS}"
S_CED_CAP = "\N{LATIN CAPITAL LETTER S WITH CEDILLA}"
DOTTED_I_CAP = "\N{LATIN CAPITAL LETTER I WITH DOT ABOVE}"
MUSTERI = f"m{U_UML}{S_CED}teri"
MUSTERI_CAPS = f"M{U_UML_CAP}{S_CED_CAP}TER{DOTTED_I_CAP}"


def mined(source: Path, state_dir: Path, *, unique: bool = False, lowercase: bool = False) -> list[str]:
    run_mining(source, state_dir / "out.csv", workspace=state_dir, unique=unique, lowercase=lowercase)
    return (state_dir / "out.csv").read_text(encoding="utf-8").splitlines()[1:]


# ------------------------------------------------------------------ plain text is mined, not refused


ADDRESSES = [f"kisi{n}@ornek.com" for n in range(6)]

SHAPES = {
    "one address per line": "".join(f"{a}\n" for a in ADDRESSES * 3),
    "prose": "".join(f"Write to {a} or call us anytime.\n" for a in ADDRESSES * 2),
    "name and address": "".join(f"Person {n} <{a}>\n" for n, a in enumerate(ADDRESSES * 2)),
    "web page": "<html><body>" + "".join(f'<a href="mailto:{a}">{a}</a>\n' for a in ADDRESSES * 3) + "</body></html>",
    "e-mail message": "From: ada@ornek.com\nTo: " + ", ".join(ADDRESSES) + "\nSubject: hi\n\nbody text\n" * 5,
    "contact cards": "".join(f"BEGIN:VCARD\nFN:Kisi {n}\nEMAIL:{a}\nEND:VCARD\n" for n, a in enumerate(ADDRESSES * 3)),
    "log without a known layout": "".join(f"application started user {a} signed in\n" for a in ADDRESSES * 5),
}

# tables with no header row: the first row is data, and its address is as good as any other
HEADERLESS = {
    "address first": "ada@ornek.com,Ada\nbob@ornek.org,Bob\ncarol@ornek.net,Carol\ndave@ornek.com,Dave\n",
    "address last": "Ada,ada@ornek.com\nBob,bob@ornek.org\nCarol,carol@ornek.net\nDave,dave@ornek.com\n",
    "surname, name <address>": "Lovelace, Ada <ada@ornek.com>\nTuring, Alan <alan@ornek.org>\nHopper, Grace <grace@ornek.net>\nKnuth, Donald <don@ornek.com>\n",
    "semicolons": "ada@ornek.com;Ada\nbob@ornek.org;Bob\ncarol@ornek.net;Carol\ndave@ornek.com;Dave\n",
    "prose with one comma a line": "".join(f"Write to {a} or call us, anytime.\n" for a in ADDRESSES),
}


@pytest.mark.parametrize("shape", list(SHAPES))
def test_text_without_structure_is_profiled_as_text_and_mined(
    shape: str, profiler: DataProfiler, write_file: WriteFile, state_dir: Path
) -> None:
    source = write_file("input.txt", SHAPES[shape])

    profile = profiler.profile(source)
    found = mined(source, state_dir, unique=True, lowercase=True)

    assert profile.data_format is DataFormat.TEXT and profile.structure.fields == ()
    assert profile.records.unit == "lines"
    expected = {"ada@ornek.com", *ADDRESSES} if shape == "e-mail message" else set(ADDRESSES)
    assert set(found) == expected and len(found) == len(expected)


@pytest.mark.parametrize("shape", list(HEADERLESS))
def test_a_list_without_a_header_row_keeps_its_first_address(
    shape: str, profiler: DataProfiler, write_file: WriteFile, state_dir: Path
) -> None:
    text = HEADERLESS[shape]
    source = write_file("list.csv", text)

    profile = profiler.profile(source)
    found = mined(source, state_dir)

    assert profile.structure.has_header is False and profile.data_offset == 0
    assert len(found) == text.count("@")  # the first line's address is not lost as a "header"


def test_a_real_header_row_is_still_skipped(profiler: DataProfiler, write_file: WriteFile, state_dir: Path) -> None:
    source = write_file("with.csv", "email,name\nada@ornek.com,Ada\nbob@ornek.org,Bob\ncarol@ornek.net,Carol\n")

    assert profiler.profile(source).structure.has_header is True
    assert mined(source, state_dir) == ["ada@ornek.com", "bob@ornek.org", "carol@ornek.net"]


def test_structured_formats_keep_priority_over_plain_text(profiler: DataProfiler, write_file: WriteFile) -> None:
    table = write_file("table.csv", "id,name\n1,a\n2,b\n3,c\n")
    log = write_file("events.txt", "".join(f"2026-11-05 10:00:0{n} INFO hello\n" for n in range(9)))
    records = write_file("records.txt", '{"a": 1}\n{"a": 2}\n{"a": 3}\n')

    formats = [profiler.profile(path).data_format for path in (table, log, records)]

    assert formats == [DataFormat.CSV, DataFormat.LOG, DataFormat.JSONL]


def test_a_table_too_broken_to_recognise_is_still_mined_as_text(profiler: DataProfiler, write_file: WriteFile, state_dir: Path) -> None:
    source = write_file("broken.csv", "".join(f"{n} ok mail kisi{n % 4}@ornek.com\n" for n in range(40)))

    assert profiler.profile(source).data_format is DataFormat.TEXT
    assert len(mined(source, state_dir, unique=True)) == 4


def test_quality_checks_say_that_text_has_no_columns(write_file: WriteFile) -> None:
    with pytest.raises(UnsupportedDataFormatException, match="this file looks like text"):
        check_quality(write_file("notes.txt", "just some words\nand some more\n"))


def test_text_with_only_blank_lines_or_control_characters_is_still_refused(
    profiler: DataProfiler, write_file: WriteFile
) -> None:
    for name, content in (("blank.txt", b"\n  \n\t\n"), ("control.bin", bytes(range(1, 9)) * 300)):
        with pytest.raises(UnsupportedDataFormatException, match="not readable text"):
            profiler.profile(write_file(name, content))


def test_ansi_coloured_output_is_text() -> None:
    coloured = [TextLine("\x1b[31mERROR\x1b[0m mail ada@ornek.com", 40), TextLine("\x1b[32mok\x1b[0m", 12)]

    analysis = TextHandler().analyze(iter(coloured), 10)

    assert analysis is not None and analysis.structure.sampled_records == 2


def test_the_detector_refuses_without_a_fallback_and_reads_text_with_one(write_file: WriteFile) -> None:
    path = write_file("plain.txt", "nothing to see\nhere at all\n")
    config = ProfilerConfig()
    streams = FileStreamReader(config.chunk_size_bytes, config.max_line_bytes)
    encoding = EncodingInfo(name="utf-8")

    with pytest.raises(UnsupportedDataFormatException, match="no supported format"):
        FormatDetector([CsvHandler()], streams, config).detect(path, encoding)
    match = FormatDetector([CsvHandler()], streams, config, fallback=TextHandler()).detect(path, encoding)

    assert match.data_format is DataFormat.TEXT


def test_the_server_profiles_and_mines_text(state_dir: Path) -> None:
    (state_dir / "list.txt").write_text("".join(f"{a}\n" for a in ADDRESSES * 2), encoding="utf-8")
    client = Client(create_mcp_server([state_dir]))

    (profile,) = client.handshake_and(call(2, "profile_dataset", {"path": "list.txt"}))
    (mine,) = client.handshake_and(call(3, "mine_dataset", {"path": "list.txt", "output_path": "found.csv", "unique": True}))

    assert profile["result"]["structuredContent"]["format"] == "text"
    assert mine["result"]["structuredContent"]["records_written"] == len(ADDRESSES)


# ------------------------------------------------------------------ Windows-1254 against Windows-1252


def detect(data: bytes) -> EncodingInfo:
    return StdlibEncodingDetector().detect(iter([data]))


def test_turkish_windows_text_is_recognised_and_read_correctly(profiler: DataProfiler, write_file: WriteFile) -> None:
    header = f"M{U_UML}{S_CED}teri Ad{DOTLESS_I};E-posta;{S_CED_CAP}ehir\r\n"
    rows = "".join(f"{MUSTERI} {n};kisi{n}@firma.com.tr;{DOTTED_I_CAP}stanbul\r\n" for n in range(20))
    path = write_file("turkce.csv", (header + rows).encode("cp1254"))

    profile = profiler.profile(path)

    assert profile.encoding.name == "cp1254"
    assert profile.structure.fields == (f"M{U_UML}{S_CED}teri Ad{DOTLESS_I}", "E-posta", f"{S_CED_CAP}ehir")


@pytest.mark.parametrize(
    "text",
    [
        b"caf\xe9 r\xe9sum\xe9 d\xe9j\xe0 vu \xe0 la cr\xe8me br\xfbl\xe9e na\xefve",  # French
        b"\xdeorsteinn Gu\xf0mundsson b\xfdr \xe1 \xcdslandi, \xfear er kalt",  # Icelandic
        b"M\xfcller Stra\xdfe Gr\xf6\xdfe \xfcber den Fl\xfcssen",  # German
        b"ni\xf1o a\xf1o se\xf1or m\xe1s all\xe1",  # Spanish
    ],
)
def test_western_european_text_stays_windows_1252(text: bytes) -> None:
    assert detect(text).name == "cp1252"


def test_turkish_text_that_holds_only_letters_both_encodings_share_is_read_the_same_either_way() -> None:
    data = f"{U_UML_CAP}z{U_UML}m {S_CED_CAP}".encode("cp1254").replace(S_CED_CAP.encode("cp1254"), b"S")

    assert detect(data).name == "cp1252"  # the same letters in both: nothing to tell them apart
    assert data.decode("cp1252") == data.decode("cp1254")


def test_valid_utf_8_is_not_taken_for_a_legacy_encoding() -> None:
    assert detect(f"M{U_UML}{S_CED}teri {S_CED_CAP}{U_UML}kr{U_UML} {DOTTED_I_CAP}smail".encode()).name == "utf-8"


def test_an_address_with_turkish_letters_survives_a_windows_1254_file(write_file: WriteFile, state_dir: Path) -> None:
    address = f"{S_CED}{U_UML}kr{U_UML}@firma.com.tr"
    source = write_file("liste.csv", f"ad;eposta\r\nSukru;{address}\r\nAli;ali@firma.com.tr\r\n".encode("cp1254"))

    assert mined(source, state_dir, unique=True) == [address, "ali@firma.com.tr"]


# ------------------------------------------------------------------ the Turkish capital dotted I


def test_lowercase_gives_a_plain_i_for_the_dotted_capital_and_no_stray_dot() -> None:
    address = f"{MUSTERI_CAPS}@FIRMA.COM.TR"

    for extractor in (EmailExtractor(lowercase=True), RegexExtractor.emails(lowercase=True)):
        assert list(extractor.extract(f"mail {address} now")) == [(f"{MUSTERI}@firma.com.tr",)]
    grouped = RegexExtractor(r"(\w+)@(\w+)", lowercase=True)
    assert list(grouped.extract(address)) == [(MUSTERI, "firma")]
    assert list(RegexExtractor(r"\w+@\w+", lowercase=True).extract(address, 0, 10_000)) == [(f"{MUSTERI}@firma",)]


def test_one_address_in_capitals_and_in_lower_case_is_one_unique_address(write_file: WriteFile, state_dir: Path) -> None:
    source = write_file("liste.csv", f"ad,eposta\n1,{MUSTERI_CAPS}@FIRMA.COM.TR\n2,{MUSTERI}@firma.com.tr\n3,INFO@FIRMA.COM\n")

    assert mined(source, state_dir, unique=True, lowercase=True) == [f"{MUSTERI}@firma.com.tr", "info@firma.com"]
