"""Masking of personal data before it reaches an assistant, and the audit log of tool calls."""

from __future__ import annotations

import json
import logging
import shutil
import time
import uuid
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from datamining_skill import InvalidConfigurationException, create_mcp_server
from datamining_skill.application.pii_mask import (
    iban_valid,
    luhn_valid,
    mask_profile,
    mask_text,
    mask_value,
    shannon_entropy,
    tckn_valid,
)
from datamining_skill.cli import main
from datamining_skill.infrastructure.audit_log import AuditLog
from datamining_skill.infrastructure.result_preview import preview_result
from tests.test_mcp_server import Client, call

CARD = "4111 1111 1111 1111"  # the standard Visa test number
IBAN = "GB82 WEST 1234 5698 7654 32"  # the example in the IBAN standard
TCKN = "12345678950"


def tckn_with_check_digits(first_nine: str) -> str:
    d = [int(char) for char in first_nine]
    tenth = ((d[0] + d[2] + d[4] + d[6] + d[8]) * 7 - (d[1] + d[3] + d[5] + d[7])) % 10
    eleventh = (sum(d) + tenth) % 10
    return f"{first_nine}{tenth}{eleventh}"


# ------------------------------------------------------------------ the checks themselves


def test_the_check_functions_accept_valid_numbers_and_reject_a_changed_digit() -> None:
    assert luhn_valid("4111111111111111") and not luhn_valid("4111111111111112")
    assert iban_valid(IBAN.replace(" ", "")) and not iban_valid("GB82WEST12345698765433")
    assert tckn_valid(TCKN) and tckn_valid(tckn_with_check_digits("987654321"))
    assert not tckn_valid("12345678951") and not tckn_valid("02345678950") and not tckn_valid("1234567895")


@pytest.mark.parametrize("first_nine", ["123456789", "987654321", "555555555", "102030405", "999999999"])
def test_generated_identity_numbers_pass_the_check(first_nine: str) -> None:
    assert tckn_valid(tckn_with_check_digits(first_nine))


# ------------------------------------------------------------------ text


@pytest.mark.parametrize(
    ("text", "expected", "tag"),
    [
        ("write to ada.hollis@internal.corp.test now", "write to [EMAIL] now", "EMAIL"),
        ("müşteri@firma.com.tr", "[EMAIL]", "EMAIL"),
        ("from 10.20.30.40 port 22", "from [IP] port 22", "IP"),
        (f"card {CARD}.", "card [CARD].", "CARD"),
        ("card 4111-1111-1111-1111", "card [CARD]", "CARD"),
        ("card 4111111111111111", "card [CARD]", "CARD"),
        (f"id {TCKN};", "id [TCKN];", "TCKN"),
        (f"pay {IBAN} today", "pay [IBAN] today", "IBAN"),
        ("TR330006100519786457841326", "[IBAN]", "IBAN"),
    ],
)
def test_each_kind_is_replaced_by_its_tag(text: str, expected: str, tag: str) -> None:
    counts: Counter[str] = Counter()

    assert mask_text(text, counts) == expected
    assert counts == {tag: 1}


@pytest.mark.parametrize(
    "text",
    [
        "999.1.1.1",
        "version 1.2.3.4.5 shipped",
        "order 4111 1111 1111 1112",  # fails Luhn
        "id 12345678951",  # fails the identity check digits
        "GB82 WEST 1234 5698 7654 33",  # fails mod 97
        "year 2026, total 1234567",
        "no personal data here",
        "",
    ],
)
def test_ordinary_text_and_numbers_that_fail_their_check_are_left_alone(text: str) -> None:
    counts: Counter[str] = Counter()

    assert mask_text(text, counts) == text
    assert sum(counts.values()) == 0


def test_a_number_inside_a_longer_run_of_digits_is_not_masked() -> None:
    longer = "9" + TCKN + "9"

    assert mask_text(longer) == longer


def test_several_kinds_in_one_line_are_all_replaced_and_counted() -> None:
    counts: Counter[str] = Counter()
    line = f"{TCKN},ada@internal.corp.test,{CARD},10.0.0.1,{IBAN},ada@internal.corp.test"

    assert mask_text(line, counts) == "[TCKN],[EMAIL],[CARD],[IP],[IBAN],[EMAIL]"
    assert counts == {"TCKN": 1, "EMAIL": 2, "CARD": 1, "IP": 1, "IBAN": 1}


def test_text_without_candidates_comes_back_unchanged() -> None:
    text = "just words"

    assert mask_text(text) is text


def test_masking_is_not_slow_on_hostile_megabyte_lines() -> None:
    lines = [
        "1 " * 500_000,
        "0" * 1_000_000,
        "a@" * 500_000,
        "AB12 " + "A" * 1_000_000,
        "1.1.1." * 160_000,
        "4-" * 500_000,
        ":" * 1_000_000,
        "a:" * 500_000,
        "1:" * 500_000,
        "::1" * 330_000,
        "ffff:" * 200_000,
        "1:2:3:4:5:6:7:8:" * 62_500,
        "eyJ" * 330_000,
        "eyJ" + "a" * 1_000_000,
        ("eyJ" + "a" * 20 + ".") * 45_000,
        "AKIA" * 250_000,
        "ghp_" * 250_000,
        "sk-" * 330_000,
        "sk-" + "a" * 1_000_000,
        "xoxb-" * 200_000,
        "Bearer " * 140_000,
        "Bearer " + "a" * 1_000_000,
        "bearer" + " " * 1_000_000 + "a",
        "Bearer a" * 120_000,
    ]
    for line in lines:
        started = time.perf_counter()
        mask_text(line)
        assert time.perf_counter() - started < 2.0, line[:12]


# ------------------------------------------------------------------ IPv6


@pytest.mark.parametrize(
    "address",
    [
        "2001:0db8:85a3:0000:0000:8a2e:0370:7334",
        "2001:db8::1",
        "fe80::1ff:fe23:4567:890a",
        "::1",
        "1::",
        "2001:db8:0:0:0:0:2:1",
        "::ffff:192.0.2.128",
        "64:ff9b::192.0.2.33",
        "ABCD:EF01:2345:6789:ABCD:EF01:2345:6789",
    ],
)
def test_ipv6_addresses_are_masked(address: str) -> None:
    counts: Counter[str] = Counter()

    assert mask_text(f"peer {address} up", counts) == "peer [IP] up"
    assert counts == {"IP": 1}


@pytest.mark.parametrize(
    "text",
    [
        "12:34:56",
        "2026:10:31",
        "at 12:34:56:78",
        "1:2:3:4:5:6:7",  # seven groups and no "::"
        "1:2:3:4:5:6:7:8:9",  # nine groups
        "1::2::3",  # two "::"
        "12345::1",  # a group of five digits
        "2001:db8::g",
        "::",  # the unspecified address holds no digit
        "a::b",  # no decimal digit: a name in C++ or Rust
        "dead::beef",
        "std::vector",
        "1:2:3:4:5:6:7::8:9",
        "::ffff:999.0.2.128",
        "x1::1",  # glued to a word
        "mac 00:1a:2b:3c:4d:5e",  # six groups
    ],
)
def test_text_that_only_looks_like_ipv6_is_left_alone(text: str) -> None:
    counts: Counter[str] = Counter()

    assert mask_text(text, counts) == text
    assert sum(counts.values()) == 0


def test_an_ipv6_address_among_other_kinds_is_counted_with_them() -> None:
    counts: Counter[str] = Counter()

    assert mask_text("10.0.0.1 and 2001:db8::1, at 12:34:56", counts) == "[IP] and [IP], at 12:34:56"
    assert counts == {"IP": 2}


# ------------------------------------------------------------------ secrets
# The tokens are put together at run time, so that no complete one stands in the source.

_B64 = "AbCdEfGhIjKlMnOpQrSt"
JWT = "ey" + "J" + _B64 + ".ey" + "J" + _B64 + "." + _B64
AWS_KEY = "AK" + "IA" + "IOSFODNN7EXAMPLE"
GITHUB_TOKEN = "gh" + "p_" + "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bD1fH3jL5"
GITHUB_PAT = "github" + "_pat_" + "11AAAAAAA0" + "aB3dE5gH7jK9mN1pQ3sT5vW7"
OPENAI_KEY = "s" + "k-" + "proj-" + "aB3dE5gH7jK9mN1pQ3sT5vW7"
ANTHROPIC_KEY = "s" + "k-" + "ant-" + "aB3dE5gH7jK9mN1pQ3sT5vW7"
SLACK_TOKEN = "xo" + "xb-" + "123456789012-aB3dE5gH7jK9mN1pQ3sT"
BEARER_TOKEN = "q8Zr3KfV0xL2mN7pT4wYc9Hd1Gb6Js5A"


@pytest.mark.parametrize(
    "token",
    [JWT, AWS_KEY, "AS" + "IA" + "IOSFODNN7EXAMPLE", GITHUB_TOKEN, GITHUB_PAT, OPENAI_KEY, ANTHROPIC_KEY, SLACK_TOKEN],
)
def test_tokens_of_known_formats_are_masked(token: str) -> None:
    counts: Counter[str] = Counter()

    assert mask_text(f"key={token};", counts) == "key=[SECRET];"
    assert counts == {"SECRET": 1}


def test_a_high_entropy_bearer_token_is_masked_and_the_word_stays() -> None:
    counts: Counter[str] = Counter()

    assert mask_text(f"Authorization: Bearer {BEARER_TOKEN}", counts) == "Authorization: Bearer [SECRET]"
    assert mask_text(f"authorization: bearer   {BEARER_TOKEN}") == "authorization: bearer   [SECRET]"
    assert counts == {"SECRET": 1}


@pytest.mark.parametrize(
    "text",
    [
        "Bearer 12345",
        "Bearer password",
        "Bearer " + "a" * 40,  # long, but a single character
        "Bearer 12345678901234567890",  # digits only
        "Bearer abcdefghijklmnopqrstuvwxyz",  # one kind of character
        "Bearer 1111111111aaaaaaaaaa",  # two kinds, but entropy 1
        "Bearer token_for_the_test_environment",  # words
        "bearer of bad news",
        "Bearerxyz" + BEARER_TOKEN,  # not a separate word
    ],
)
def test_bearer_values_that_are_not_random_stay_readable(text: str) -> None:
    counts: Counter[str] = Counter()

    assert mask_text(text, counts) == text
    assert sum(counts.values()) == 0


@pytest.mark.parametrize(
    "text",
    [
        "sk-learn is a library",
        "task-" + "a" * 30,  # "sk-" inside a word
        "eyJhbGciOiJIUzI1NiJ9",  # a header alone is not a token
        "AKIA" + "A" * 10,  # too short
        "X" + AWS_KEY,  # glued to a word
        "ghp_short",
    ],
)
def test_text_that_only_resembles_a_secret_is_left_alone(text: str) -> None:
    assert mask_text(text) == text


def test_secrets_next_to_other_kinds_are_all_replaced() -> None:
    counts: Counter[str] = Counter()
    line = f"{AWS_KEY},ada@internal.corp.test,{JWT},2001:db8::1"

    assert mask_text(line, counts) == "[SECRET],[EMAIL],[SECRET],[IP]"
    assert counts == {"SECRET": 2, "EMAIL": 1, "IP": 1}


def test_shannon_entropy_follows_the_definition() -> None:
    assert shannon_entropy("") == 0.0
    assert shannon_entropy("aaaa") == 0.0
    assert shannon_entropy("ab") == pytest.approx(1.0)
    assert shannon_entropy("abcd") == pytest.approx(2.0)
    assert shannon_entropy("aabc") == pytest.approx(1.5)
    assert shannon_entropy(BEARER_TOKEN) >= 3.8


def test_masking_does_not_skip_text_that_holds_only_a_secret() -> None:
    for token in (JWT, AWS_KEY, GITHUB_TOKEN, OPENAI_KEY, SLACK_TOKEN, "Bearer " + BEARER_TOKEN):
        assert "[SECRET]" in mask_text(token)


# ------------------------------------------------------------------ structures


def test_mask_value_reaches_lists_object_values_and_object_keys() -> None:
    counts: Counter[str] = Counter()
    value = {"ada@internal.corp.test": ["10.0.0.1", {"n": 5, "ok": True, "none": None}], "plain": 1.5}

    assert mask_value(value, counts) == {"[EMAIL]": ["[IP]", {"n": 5, "ok": True, "none": None}], "plain": 1.5}
    assert counts == {"EMAIL": 1, "IP": 1}


def test_mask_value_stops_at_a_depth_no_response_would_carry() -> None:
    nested: Any = "ada@internal.corp.test"
    for _ in range(100):
        nested = [nested]

    assert mask_value(nested) is not None  # no RecursionError


def test_mask_profile_hides_data_derived_names_and_keeps_the_rest() -> None:
    profile: dict[str, Any] = {
        "file": {"name": "export.csv", "size_bytes": 10},
        "format": "csv",
        "structure": {
            "fields": ["ada@internal.corp.test", "n"],
            "field_count": 2,
            "field_types": {"ada@internal.corp.test": ["string"], "n": ["integer"]},
            "pattern": None,
        },
        "records": {"count": 3, "unit": "lines"},
    }

    masked = mask_profile(profile)

    assert masked["structure"]["fields"] == ["[EMAIL]", "n"]
    assert masked["structure"]["field_types"] == {"[EMAIL]": ["string"], "n": ["integer"]}
    assert masked["structure"]["field_count"] == 2 and masked["records"] == profile["records"]
    assert masked["pii_masked"] == {"EMAIL": 2}
    assert profile["structure"]["fields"][0] == "ada@internal.corp.test"  # the input is not modified


# ------------------------------------------------------------------ preview


def test_preview_masks_csv_cells_and_the_header_row(state_dir: Path) -> None:
    (state_dir / "r.csv").write_text(f"id,ada@internal.corp.test\n1,{CARD}\n2,plain\n", encoding="utf-8")

    shown = preview_result(state_dir / "r.csv", mask=True)

    assert shown["columns"] == ["id", "[EMAIL]"]
    assert shown["rows"] == [["1", "[CARD]"], ["2", "plain"]]
    assert shown["pii_masked"] == {"CARD": 1, "EMAIL": 1}


def test_preview_masks_json_lines_records(state_dir: Path) -> None:
    (state_dir / "r.jsonl").write_text(
        '{"email": "ada@internal.corp.test", "ips": ["10.0.0.1"], "n": 7}\nnot json 10.0.0.2\n', encoding="utf-8"
    )

    shown = preview_result(state_dir / "r.jsonl", mask=True)

    assert shown["rows"] == [{"email": "[EMAIL]", "ips": ["[IP]"], "n": 7}, "not json [IP]"]
    assert shown["pii_masked"] == {"EMAIL": 1, "IP": 2}


def test_preview_without_masking_is_unchanged_and_has_no_masking_field(state_dir: Path) -> None:
    (state_dir / "r.csv").write_text("email\nada@internal.corp.test\n", encoding="utf-8")

    shown = preview_result(state_dir / "r.csv")

    assert shown["rows"] == [["ada@internal.corp.test"]] and "pii_masked" not in shown


def test_the_command_line_masks_a_preview_on_request(state_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (state_dir / "r.csv").write_text("email\nada@internal.corp.test\n", encoding="utf-8")

    assert main(["preview", str(state_dir / "r.csv"), "--mask"]) == 0
    masked = capsys.readouterr().out
    assert main(["preview", str(state_dir / "r.csv")]) == 0
    plain = capsys.readouterr().out

    assert "[EMAIL]" in masked and "ada@internal" not in masked
    assert "ada@internal.corp.test" in plain


# ------------------------------------------------------------------ the server


def write_source(directory: Path) -> Path:
    path = directory / "contacts.csv"
    rows = "".join(f"{i},note {i} mail emp{i}@internal.corp.test ip 10.0.0.{i}\n" for i in range(1, 40))
    path.write_text("id,note\n" + rows, encoding="utf-8")
    return path


def structured(response: dict[str, Any]) -> dict[str, Any]:
    assert not response["result"].get("isError"), response
    data: dict[str, Any] = response["result"]["structuredContent"]
    return data


def mine_and_preview(client: Client) -> dict[str, Any]:
    mined = client.handshake_and(call(2, "mine_dataset", {"path": "contacts.csv", "output_path": "found.csv"}))
    assert structured(mined[0])["records_written"] == 39
    (shown,) = client.handshake_and(call(3, "preview_result", {"path": "found.csv", "rows": 3}))
    return structured(shown)


def test_the_server_masks_previews_by_default(state_dir: Path) -> None:
    write_source(state_dir)

    shown = mine_and_preview(Client(create_mcp_server([state_dir])))

    assert shown["columns"] == ["email"] and shown["rows"] == [["[EMAIL]"]] * 3
    assert shown["pii_masked"] == {"EMAIL": 3}
    assert "emp1@internal.corp.test" in (state_dir / "found.csv").read_text(encoding="utf-8")  # the file is untouched


def test_the_operator_can_turn_masking_off(state_dir: Path) -> None:
    write_source(state_dir)

    shown = mine_and_preview(Client(create_mcp_server([state_dir], mask_preview=False)))

    assert shown["rows"][0] == ["emp1@internal.corp.test"] and "pii_masked" not in shown


def test_an_assistant_cannot_turn_masking_off(state_dir: Path) -> None:
    write_source(state_dir)
    client = Client(create_mcp_server([state_dir]))
    mine_and_preview(client)

    (refused,) = client.handshake_and(call(2, "preview_result", {"path": "found.csv", "mask": False}))

    assert refused["result"]["isError"] is True and "unknown argument 'mask'" in refused["result"]["content"][0]["text"]


def test_profile_names_that_come_from_the_data_are_masked_by_default(state_dir: Path) -> None:
    (state_dir / "nohead.csv").write_text(
        "ada@internal.corp.test,1\nbob@internal.corp.test,2\ncy@internal.corp.test,3\n", encoding="utf-8"
    )

    (response,) = Client(create_mcp_server([state_dir])).handshake_and(call(2, "profile_dataset", {"path": "nohead.csv"}))

    assert "internal.corp.test" not in json.dumps(response["result"])
    assert "pii_masked" in structured(response)


# ------------------------------------------------------------------ the audit log


@pytest.fixture
def audit_file(workspace: Path) -> Iterator[Path]:
    directory = workspace / f"audit-{uuid.uuid4().hex[:8]}"
    directory.mkdir()
    yield directory / "audit.jsonl"
    shutil.rmtree(directory)


def records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="ascii").splitlines()]


def test_every_tool_call_leaves_one_record_without_contents(state_dir: Path, audit_file: Path) -> None:
    write_source(state_dir)
    client = Client(create_mcp_server([state_dir], audit_log=audit_file))

    client.handshake_and(
        call(2, "profile_dataset", {"path": "contacts.csv"}),
        call(3, "mine_dataset", {"path": "contacts.csv", "output_path": "found.csv", "unique": True}),
        call(4, "preview_result", {"path": "found.csv", "rows": 2}),
        call(5, "profile_dataset", {"path": "../outside.csv"}),
        call(6, "mining_status", {"job_id": "nothing"}),
    )

    log = records(audit_file)
    assert [r["event"] for r in log] == ["server_started"] + ["tool_call"] * 5
    assert log[0]["masking"] is True and log[0]["custom_patterns"] is False
    profile, mine, preview, rejected, status = log[1:]
    assert profile["tool"] == "profile_dataset" and profile["ok"] is True and profile["path"] == "contacts.csv"
    assert mine["output_path"] == "found.csv" and mine["unique"] is True and mine["records_written"] == 39
    assert preview["rows"] == 2 and preview["rows_returned"] == 2 and preview["pii_masked"] == {"EMAIL": 2}
    assert rejected["ok"] is False and rejected["path"] == "<rejected>"
    assert status["ok"] is False and "unknown job" in status["error"]
    text = audit_file.read_text(encoding="ascii")
    assert "emp1@" not in text and "internal.corp.test" not in text and "10.0.0." not in text
    assert all(isinstance(r["ms"], int) and r["time"].endswith("+00:00") for r in log[1:])


def test_a_custom_pattern_is_noted_but_never_recorded(state_dir: Path, audit_file: Path) -> None:
    write_source(state_dir)
    secret_pattern = r"note \d{1,3} mail"
    client = Client(create_mcp_server([state_dir], allow_custom_patterns=True, audit_log=audit_file))

    client.handshake_and(
        call(2, "mine_dataset", {"path": "contacts.csv", "output_path": "p.csv", "pattern": secret_pattern, "fields": []})
    )

    entry = records(audit_file)[1]
    assert entry["custom_pattern"] is True
    assert "note" not in audit_file.read_text(encoding="ascii").replace("event", "")


def test_an_unknown_tool_is_recorded_and_still_refused(state_dir: Path, audit_file: Path) -> None:
    client = Client(create_mcp_server([state_dir], audit_log=audit_file))

    (response,) = client.handshake_and(call(2, "delete_everything", {}))

    assert "error" in response or response["result"].get("isError")
    last = records(audit_file)[-1]
    assert last["tool"] == "delete_everything" and last["ok"] is False


def test_a_hostile_tool_name_cannot_forge_a_line_in_the_log(state_dir: Path, audit_file: Path) -> None:
    client = Client(create_mcp_server([state_dir], audit_log=audit_file))

    client.handshake_and(call(2, 'x"}\n{"event":"server_started","ok":true', {}))

    lines = audit_file.read_text(encoding="ascii").splitlines()
    assert len(lines) == 2 and json.loads(lines[1])["event"] == "tool_call"


def test_the_log_must_lie_outside_the_allowed_directories(state_dir: Path) -> None:
    with pytest.raises(InvalidConfigurationException, match="outside the allowed directories"):
        create_mcp_server([state_dir], audit_log=state_dir / "audit.jsonl")
    nested = state_dir / "deeper"
    nested.mkdir()
    with pytest.raises(InvalidConfigurationException, match="outside the allowed directories"):
        create_mcp_server([state_dir], audit_log=nested / "audit.jsonl")


@pytest.mark.parametrize("problem", ["missing folder", "a directory", "control characters"])
def test_unusable_log_paths_are_refused(workspace: Path, audit_file: Path, problem: str) -> None:
    if problem == "missing folder":
        path = audit_file.parent / "nope" / "audit.jsonl"
    elif problem == "a directory":
        path = audit_file.parent
    else:
        path = audit_file.parent / "a\nb.jsonl"

    with pytest.raises(InvalidConfigurationException):
        AuditLog(path)


def test_a_symbolic_link_is_not_written_through(audit_file: Path) -> None:
    victim = audit_file.parent / "victim.txt"
    victim.write_text("keep me")
    try:
        audit_file.symlink_to(victim)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not available to this user")

    with pytest.raises(InvalidConfigurationException, match="regular file"):
        AuditLog(audit_file)
    assert victim.read_text() == "keep me"


def test_records_are_appended_and_the_file_stays_closed_between_them(audit_file: Path) -> None:
    log = AuditLog(audit_file)

    log.record(event="one", n=1)
    audit_file.unlink()  # possible on Windows only if no handle is left open
    log.record(event="two", n=2)

    assert [r["event"] for r in records(audit_file)] == ["two"]
    log.record(event="three")
    assert [r["event"] for r in records(audit_file)] == ["two", "three"]


def test_non_ascii_text_is_escaped_in_the_log(audit_file: Path) -> None:
    AuditLog(audit_file).record(event="x", note="müşteri ‮")

    raw = audit_file.read_text(encoding="ascii")
    assert "\\u202e" in raw and "m\\u00fc" in raw


def test_a_failing_audit_write_is_reported_but_does_not_fail_the_call(
    state_dir: Path, audit_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_source(state_dir)
    seen: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(record.getMessage())

    logger = logging.getLogger(f"tests.audit.{uuid.uuid4().hex[:6]}")
    logger.addHandler(Capture())
    server = create_mcp_server([state_dir], audit_log=audit_file, logger=logger)

    def broken(self: AuditLog, **fields: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(AuditLog, "record", broken)
    (response,) = Client(server).handshake_and(call(2, "profile_dataset", {"path": "contacts.csv"}))

    assert response["result"].get("isError") in (None, False)
    assert any("audit log could not be written" in message for message in seen)


def test_the_command_line_refuses_an_audit_log_inside_the_workspace(
    state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["mcp", "--allow-dir", str(state_dir), "--audit-log", str(state_dir / "audit.jsonl")])

    assert code == 3
    assert "outside the allowed directories" in capsys.readouterr().err
