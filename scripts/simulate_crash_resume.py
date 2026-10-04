"""Validate crash-safe resumption of a 100-chunk run.

A dummy worker "processes" 100 synthetic chunks (no data is read or written, only
byte ranges and state transitions). It is halted abruptly while chunk 50 is
IN_PROGRESS, then restarted. The script verifies that the restart:

* recovers chunk 50 as an orphan and resumes exactly there,
* never touches chunks 1-49 (their rows are byte-for-byte identical), and
* finishes with all 100 chunks COMPLETED.

Run::

    python scripts/simulate_crash_resume.py            # hard stop: os._exit, no cleanup
    python scripts/simulate_crash_resume.py --crash-mode sys   # sys.exit(1)

The state database lives under the project's ``.scratch/`` directory and is
deleted afterwards. The default ``hard`` mode uses ``os._exit`` so no ``finally``
block, context-manager exit or connection close runs: the closest in-process
equivalent of ``SIGKILL`` or a power cut.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from datamining_skill import ChunkMetadata, ChunkStatus, StateManager

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRATCH = PROJECT_ROOT / ".scratch"
TOTAL_CHUNKS = 100
CRASH_AT = 50
CHUNK_BYTES = 1_000_000


def synthetic_chunks() -> list[ChunkMetadata]:
    return [
        ChunkMetadata(i, (i - 1) * CHUNK_BYTES, i * CHUNK_BYTES, estimated_records=10_000)
        for i in range(1, TOTAL_CHUNKS + 1)
    ]


def emit(event: str, **data: object) -> None:
    print(json.dumps({"event": event, **data}), flush=True)


def run_worker(db: Path, crash_at: int | None, crash_mode: str) -> int:
    """The dummy mining loop."""
    with StateManager(db, allowed_roots=[SCRATCH]) as state:
        emit("opened", recovered_orphans=state.recovered_orphans)
        if not state.is_initialized():  # first run only: a resumed run reuses the stored plan
            state.initialize(synthetic_chunks())
            emit("initialized", chunks=TOTAL_CHUNKS)
        while (chunk := state.claim_next_pending()) is not None:
            if chunk.chunk_id == crash_at:
                emit("crashing", chunk_id=chunk.chunk_id)
                if crash_mode == "hard":
                    os._exit(1)  # no cleanup whatsoever
                sys.exit(1)
            _ = chunk.end_byte - chunk.start_byte  # stand-in for real work
            state.mark_completed(chunk.chunk_id)
            emit("completed", chunk_id=chunk.chunk_id)
    return 0


def run_child(db: Path, crash_at: int | None, crash_mode: str) -> tuple[int, list[dict[str, object]]]:
    command = [sys.executable, str(Path(__file__).resolve()), "--worker", "--db", str(db)]
    if crash_at is not None:
        command += ["--crash-at", str(crash_at), "--crash-mode", crash_mode]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    if result.returncode not in (0, 1):
        print(result.stderr, file=sys.stderr)
    return result.returncode, events


def check(condition: bool, message: str) -> None:
    print(f"  [{'ok' if condition else 'FAIL'}] {message}")
    if not condition:
        raise SystemExit(1)


def orchestrate(crash_mode: str) -> int:
    workdir = SCRATCH / f"crash-sim-{uuid.uuid4().hex[:8]}"
    db = workdir / "state.sqlite3"
    try:
        print(f"Phase 1: process {TOTAL_CHUNKS} chunks, halt hard at chunk {CRASH_AT} ({crash_mode})")
        code, events = run_child(db, CRASH_AT, crash_mode)
        completed = [int(str(e["chunk_id"])) for e in events if e["event"] == "completed"]
        check(code != 0, f"worker terminated abnormally (exit code {code})")
        check(completed == list(range(1, CRASH_AT)), f"chunks 1-{CRASH_AT - 1} completed before the halt")

        # Inspect without recovery so the crash aftermath is observed as-is.
        with StateManager(db, allowed_roots=[SCRATCH], recover_orphans=False) as observer:
            summary = observer.summary()
            check(observer.get(CRASH_AT).status is ChunkStatus.IN_PROGRESS, f"chunk {CRASH_AT} left IN_PROGRESS")
            check(summary[ChunkStatus.COMPLETED] == CRASH_AT - 1, f"{CRASH_AT - 1} chunks COMPLETED")
            check(summary[ChunkStatus.PENDING] == TOTAL_CHUNKS - CRASH_AT, f"{TOTAL_CHUNKS - CRASH_AT} chunks PENDING")
            before = [observer.get(i) for i in range(1, CRASH_AT)]

        print("Phase 2: restart")
        code, events = run_child(db, None, crash_mode)
        completed = [int(str(e["chunk_id"])) for e in events if e["event"] == "completed"]
        opened = next(e for e in events if e["event"] == "opened")
        check(code == 0, "restarted worker exited cleanly")
        check(opened["recovered_orphans"] == 1, "exactly one orphan recovered on startup")
        check(not any(e["event"] == "initialized" for e in events), "stored plan reused, not re-created")
        check(completed == list(range(CRASH_AT, TOTAL_CHUNKS + 1)), f"resumed at chunk {CRASH_AT}, processed {CRASH_AT}-{TOTAL_CHUNKS}")

        with StateManager(db, allowed_roots=[SCRATCH], recover_orphans=False) as observer:
            after = [observer.get(i) for i in range(1, CRASH_AT)]
            check(after == before, f"chunks 1-{CRASH_AT - 1} untouched (identical rows incl. timestamps)")
            check(observer.get(CRASH_AT).retry_count == 1, f"chunk {CRASH_AT} retry_count == 1")
            check(observer.is_complete(), f"all {TOTAL_CHUNKS} chunks COMPLETED")
            print("  durability:", observer.diagnostics())
        print("PASS")
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        try:
            SCRATCH.rmdir()
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--crash-mode", choices=["hard", "sys"], default="hard")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--db", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--crash-at", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        return run_worker(args.db, args.crash_at, args.crash_mode)
    return orchestrate(args.crash_mode)


if __name__ == "__main__":
    sys.exit(main())
