"""Validate crash-safe, duplicate-free mining on a 50 MiB file.

A synthetic CSV hides unique e-mail addresses among noise (plus decoys that must NOT
match). The orchestrator mines it in a child process that is killed at chunk 3, then
restarted. After the second run, ``results.csv`` must equal the planted addresses
exactly: same order, zero duplicates, zero missing, every chunk COMPLETED.

Three crash points are exercised, each leaving the output file in a different
state, which is what the truncate-then-append protocol has to repair:

* ``mid-chunk``    - dies while reading chunk 3 (partial ``chunk_3.tmp``; output clean);
* ``mid-append``   - dies while appending chunk 3 to the output (half-written tail);
* ``after-append`` - dies after the whole of chunk 3 was appended but before it was
                     marked COMPLETED (a complete, *uncommitted* duplicate-to-be).

Run::

    python scripts/simulate_mining_crash.py                       # 50 MiB, all crash points
    python scripts/simulate_mining_crash.py --size-mib 8 --crash-mode sys

``--crash-mode hard`` (default) uses ``os._exit``: no ``finally`` block, context
manager or connection close runs, like ``SIGKILL`` or a power cut at process level.
``sys`` uses ``sys.exit(1)``. Everything lives under the project's ``.scratch/``
directory and is deleted afterwards; the dataset is fabricated (reserved ``.test`` domains).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any, BinaryIO, cast

from datamining_skill import (
    ChunkingConfig,
    ChunkStatus,
    StateManager,
    create_orchestrator,
)
from datamining_skill.domain.models import EncodingInfo, TextBlock, TextLine
from datamining_skill.domain.ports import ChunkStateStore, StreamReader
from datamining_skill.infrastructure import FileStreamReader

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRATCH = PROJECT_ROOT / ".scratch"
CRASH_CHUNK = 3
CRASH_POINTS = ("mid-chunk", "mid-append", "after-append")

_WORDS = ["login", "timeout", "retry", "upstream", "gateway", "session", "refresh", "token", "queue", "flush", "cache", "miss", "replica", "lag", "checkpoint", "rotate", "certificate", "renewal", "handshake", "backoff", "throttle", "audit"]
_GIVEN = ("amara", "tomas", "li", "priya", "jonas", "farid", "elena", "kofi", "marta", "sven")
_FAMILY = ("hollis", "okafor", "lindqvist", "nair", "brandt", "haddad", "ruiz", "tanaka", "weber", "osei")
_DECOYS = ("@handle", "name@host", "svc@@nowhere.test", "a@b", "@@", "mail@.test")
_DOMAINS = ("internal.corp.test", "mail.corp.test", "eu.corp.test", "partner.test")


def build_email_dataset(path: Path, size_mib: int, seed: int = 20250101) -> list[str]:
    """Write a CSV of roughly ``size_mib`` MiB and return the planted e-mails in file order."""
    rng = random.Random(seed)
    expected: list[str] = []
    target = size_mib * 1024 * 1024
    written = 0
    row_id = 0
    unique = 0  # makes every planted address distinct
    with path.open("wb") as sink:
        header = b"id,timestamp,source,message\n"
        sink.write(header)
        written += len(header)
        while written < target:
            rows: list[str] = []
            for _ in range(5_000):
                row_id += 1
                words = rng.choices(_WORDS, k=rng.randint(4, 14))
                if rng.random() < 0.3:
                    words.insert(rng.randrange(len(words) + 1), rng.choice(_DECOYS))
                row_emails: list[str] = []
                if rng.random() < 0.12:
                    for _ in range(rng.choice((1, 1, 1, 2))):
                        email = f"{rng.choice(_GIVEN)}.{rng.choice(_FAMILY)}{unique}@{rng.choice(_DOMAINS)}"
                        unique += 1
                        row_emails.append(email)
                        words.insert(rng.randrange(len(words) + 1), email)
                message = " ".join(words)
                row_emails.sort(key=message.index)  # file order, not generation order
                expected.extend(row_emails)
                stamp = f"2025-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}T{rng.randint(0, 23):02d}:00:00Z"
                rows.append(f"{row_id},{stamp},{rng.choice(_WORDS)},{message}\n")
            block = "".join(rows).encode("ascii")
            sink.write(block)
            written += len(block)
    return expected




class FixedMemory:
    """Simulated free RAM, so the plan yields about 30-40 chunks for the source file."""

    def __init__(self, available: int) -> None:
        self._available = available

    def available_bytes(self) -> int:
        return self._available


class MidChunkCrash:
    """StreamReader proxy that kills the process half-way through the Nth chunk read."""

    def __init__(self, inner: FileStreamReader, crash_on_call: int, crash: Callable[[], None]) -> None:
        self._inner = inner
        self._crash_on = crash_on_call
        self._crash = crash
        self._calls = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def range_lines(
        self, path: Path, encoding: EncodingInfo, start: int, end: int, *, check_alignment: bool = False
    ) -> Generator[TextLine, None, None]:
        self._calls += 1
        doomed = self._calls == self._crash_on
        consumed = 0
        for line in self._inner.range_lines(path, encoding, start, end, check_alignment=check_alignment):
            consumed += line.byte_length
            if doomed and consumed >= (end - start) // 2:
                self._crash()
            yield line

    def range_blocks(
        self, path: Path, encoding: EncodingInfo, start: int, end: int, *, check_alignment: bool = False
    ) -> Generator[TextBlock, None, None]:
        self._calls += 1  # a chunk is read either by lines or by blocks, never both
        doomed = self._calls == self._crash_on
        consumed = 0
        for block in self._inner.range_blocks(path, encoding, start, end, check_alignment=check_alignment):
            consumed += block.byte_length
            if doomed and consumed >= (end - start) // 2:
                self._crash()
            yield block


class CrashBeforeCompletion:
    """State proxy that kills the process just before the Nth ``mark_completed``."""

    def __init__(self, inner: StateManager, crash_on_call: int, crash: Callable[[], None]) -> None:
        self._inner = inner
        self._crash_on = crash_on_call
        self._crash = crash
        self._calls = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def mark_completed(self, chunk_id: int, output_end: int | None = None) -> None:
        self._calls += 1
        if self._calls == self._crash_on:
            self._crash()
        self._inner.mark_completed(chunk_id, output_end)


def install_half_append_crash(crash_on_call: int, crash: Callable[[], None]) -> None:
    """Make the Nth append write only half of its payload, then die."""
    calls = 0
    real_copy = shutil.copyfileobj  # the sink looks this up on the shutil module at call time

    def copy_half_then_crash(source: BinaryIO, target: BinaryIO, length: int = 0) -> None:
        nonlocal calls
        calls += 1
        if calls != crash_on_call:
            real_copy(source, target, length)
            return
        payload = source.read()
        target.write(payload[: len(payload) // 2])
        target.flush()
        crash()

    shutil.copyfileobj = copy_half_then_crash  # type: ignore[assignment]




def run_worker(workdir: Path, crash_point: str, crash_mode: str) -> int:
    """One mining process; optionally rigged to die at chunk ``CRASH_CHUNK``."""

    def crash() -> None:
        sys.stdout.flush()
        if crash_mode == "hard":
            os._exit(1)  # no cleanup whatsoever
        sys.exit(1)

    source = workdir / "source.csv"
    streams: StreamReader | None = None
    if crash_point == "mid-chunk":
        streams = cast(StreamReader, MidChunkCrash(FileStreamReader(65_536, 1_048_576), CRASH_CHUNK, crash))
    elif crash_point == "mid-append":
        install_half_append_crash(CRASH_CHUNK, crash)

    config = ChunkingConfig(
        critical_available_bytes=1 << 20, min_chunk_bytes=64 << 10, fallback_available_bytes=1 << 20
    )
    with StateManager(workdir / "state.sqlite3", allowed_roots=[SCRATCH]) as state:
        store: ChunkStateStore = state
        if crash_point == "after-append":
            store = cast(ChunkStateStore, CrashBeforeCompletion(state, CRASH_CHUNK, crash))
        orchestrator = create_orchestrator(
            output_path=workdir / "results.csv",
            state=store,
            scratch_dir=workdir / "tmp",
            allowed_roots=[SCRATCH],
            chunking_config=config,
            memory_provider=FixedMemory(source.stat().st_size // 5),
            stream_reader=streams,
        )
        summary = orchestrator.run(source)
    print(json.dumps(summary.to_dict()), flush=True)
    return 0 if summary.succeeded else 4


def run_child(workdir: Path, crash_point: str, crash_mode: str) -> tuple[int, dict[str, Any] | None]:
    command = [
        sys.executable, str(Path(__file__).resolve()), "--worker",
        "--workdir", str(workdir), "--crash-point", crash_point, "--crash-mode", crash_mode,
    ]  # fmt: skip
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    summary = next((json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")), None)
    if result.returncode not in (0, 1) or (result.returncode == 0 and summary is None):
        print(result.stdout, result.stderr, file=sys.stderr)
    return result.returncode, summary




def check(condition: bool, message: str) -> None:
    print(f"    [{'ok' if condition else 'FAIL'}] {message}")
    if not condition:
        raise SystemExit(1)


def validate_crash_point(base: Path, crash_point: str, crash_mode: str, expected: list[str]) -> None:
    workdir = base / crash_point
    workdir.mkdir()
    shutil.copy(base / "source.csv", workdir / "source.csv")
    output = workdir / "results.csv"
    print(f"  crash point: {crash_point} ({crash_mode} exit at chunk {CRASH_CHUNK})")

    code, _ = run_child(workdir, crash_point, crash_mode)
    check(code != 0, f"first run terminated abnormally (exit code {code})")

    with StateManager(workdir / "state.sqlite3", allowed_roots=[SCRATCH], recover_orphans=False) as observer:
        summary = observer.summary()
        committed = observer.committed_output_end()
        done_before = [observer.get(i) for i in range(1, CRASH_CHUNK)]
        check(summary[ChunkStatus.COMPLETED] == CRASH_CHUNK - 1, f"chunks 1-{CRASH_CHUNK - 1} COMPLETED")
        check(observer.get(CRASH_CHUNK).status is ChunkStatus.IN_PROGRESS, f"chunk {CRASH_CHUNK} left IN_PROGRESS")
        total_chunks = sum(summary.values())
    dirty = output.stat().st_size - (committed or 0)
    if crash_point == "mid-chunk":
        check((workdir / "tmp" / f"chunk_{CRASH_CHUNK}.tmp").exists(), "partial chunk_3.tmp left behind")
        check(dirty == 0, "output holds only committed data")
    else:
        check(dirty > 0, f"output carries {dirty:,} uncommitted bytes past the last commit")

    code, summary_json = run_child(workdir, "none", crash_mode)
    check(code == 0 and summary_json is not None, "restarted run finished cleanly")
    assert summary_json is not None
    check(summary_json["recovered_orphans"] == 1, "one orphan recovered on startup")
    check(summary_json["chunks_previously_completed"] == CRASH_CHUNK - 1, "chunks 1-2 not reprocessed")
    check(summary_json["chunks_processed"] == total_chunks - (CRASH_CHUNK - 1), "all remaining chunks processed")

    lines = output.read_text(encoding="utf-8").splitlines()
    mined = lines[1:]
    check(lines[0] == "email", "header written exactly once")
    check(len(mined) == len(set(mined)), "zero duplicate addresses")
    check(mined == expected, f"output equals the {len(expected):,} planted addresses (order too)")
    with StateManager(workdir / "state.sqlite3", allowed_roots=[SCRATCH], recover_orphans=False) as observer:
        check(observer.is_complete(), f"all {total_chunks} chunks COMPLETED")
        check(observer.get(CRASH_CHUNK).retry_count == 1, f"chunk {CRASH_CHUNK} retry_count == 1")
        check([observer.get(i) for i in range(1, CRASH_CHUNK)] == done_before, "chunks 1-2 untouched")
    check(not list((workdir / "tmp").glob("chunk_*.tmp")), "no scratch files left")


def orchestrate(size_mib: int, crash_mode: str, crash_points: tuple[str, ...]) -> int:
    base = SCRATCH / f"mining-sim-{uuid.uuid4().hex[:8]}"
    base.mkdir(parents=True)
    try:
        started = time.perf_counter()
        print(f"Generating a {size_mib} MiB dataset with hidden e-mail addresses ...")
        expected = build_email_dataset(base / "source.csv", size_mib)
        print(f"  {(base / 'source.csv').stat().st_size / 2**20:.1f} MiB, {len(expected):,} planted addresses "
              f"({time.perf_counter() - started:.1f}s)")
        for crash_point in crash_points:
            validate_crash_point(base, crash_point, crash_mode, expected)
        print("PASS")
        return 0
    finally:
        shutil.rmtree(base, ignore_errors=True)
        try:
            SCRATCH.rmdir()
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--size-mib", type=int, default=50)
    parser.add_argument("--crash-mode", choices=["hard", "sys"], default="hard")
    parser.add_argument("--crash-point", choices=[*CRASH_POINTS, "all", "none"], default="all")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--workdir", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        return run_worker(args.workdir, args.crash_point, args.crash_mode)
    points = CRASH_POINTS if args.crash_point == "all" else (args.crash_point,)
    return orchestrate(args.size_mib, args.crash_mode, points)


if __name__ == "__main__":
    sys.exit(main())
