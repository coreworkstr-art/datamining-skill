"""Structured JSON logging contract."""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest

from datamining_skill import UnsupportedDataFormatException, create_profiler
from datamining_skill.infrastructure.logging import JsonLogFormatter
from tests.conftest import WriteFile

PAYLOAD_MARKER = "acct-7f3a91c2e4"


@pytest.fixture
def log_stream() -> Iterator[tuple[logging.Logger, io.StringIO]]:
    stream = io.StringIO()
    logger = logging.getLogger("tests.datamining_skill.observability")
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    yield logger, stream
    logger.removeHandler(handler)


def _events(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_successful_run_emits_lifecycle_events(
    log_stream: tuple[logging.Logger, io.StringIO], write_file: WriteFile
) -> None:
    logger, stream = log_stream
    path = write_file("log.csv", f"id,account_ref\n1,{PAYLOAD_MARKER}\n2,{PAYLOAD_MARKER}\n")

    create_profiler(logger=logger).profile(path)
    events = _events(stream)

    assert [event["event"] for event in events] == [
        "profile.started",
        "format.detected",
        "profile.completed",
    ]
    assert all(event["level"] == "INFO" for event in events)
    started, detected, completed = (event["data"] for event in events)
    assert started["file_name"] == "log.csv"
    assert "rss_bytes" in started
    assert detected["format"] == "csv"
    assert detected["encoding"] == "ascii"
    assert completed["estimated_records"] == 2
    assert completed["duration_ms"] >= 0
    for key in ("start_rss_bytes", "end_rss_bytes", "delta_rss_bytes", "peak_rss_bytes"):
        assert key in completed
    assert PAYLOAD_MARKER not in stream.getvalue(), "file contents must never be logged"


def test_failure_emits_a_warning_event_and_still_raises(
    log_stream: tuple[logging.Logger, io.StringIO], write_file: WriteFile
) -> None:
    logger, stream = log_stream
    path = write_file("junk.bin", b"\x00\x01\x02" * 100)

    with pytest.raises(UnsupportedDataFormatException):
        create_profiler(logger=logger).profile(path)
    events = _events(stream)

    assert [event["event"] for event in events] == ["profile.started", "profile.failed"]
    failed = events[-1]
    assert failed["level"] == "WARNING"
    assert failed["data"]["error_type"] == "UnsupportedDataFormatException"
    assert "end_rss_bytes" in failed["data"]


def test_each_log_record_is_a_single_json_line(
    log_stream: tuple[logging.Logger, io.StringIO], write_file: WriteFile
) -> None:
    logger, stream = log_stream
    create_profiler(logger=logger).profile(write_file("a.csv", "x,y\n1,2\n"))

    lines = stream.getvalue().splitlines()
    assert len(lines) == 3
    for line in lines:
        record = json.loads(line)
        assert {"timestamp", "level", "logger", "event"} <= record.keys()
