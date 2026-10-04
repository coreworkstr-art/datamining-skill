"""Generate a synthetic CSV file for validating the profiler's memory footprint.

The generator itself runs in constant memory: rows are produced in fixed-size
blocks and written straight to disk. All values are fabricated (reserved
``.test`` e-mail domain, seeded RNG); no real data is involved.

Example::

    python scripts/generate_dummy_csv.py --size-gb 2 --output data/dummy_2gb.csv
    python -m datamining_skill profile data/dummy_2gb.csv

The default output location is inside the project (``data/``, git-ignored), not
a system temporary directory.
"""

from __future__ import annotations

import argparse
import random
import shutil
import sys
import time
from pathlib import Path

HEADER = b"id,created_at,user_id,email,amount,status,country,description\n"
STATUSES = ("pending", "paid", "refunded", "failed", "cancelled")
COUNTRIES = ("US", "DE", "TR", "JP", "BR", "IN", "FR", "GB", "CA", "AU")
WORDS = (
    "invoice", "renewal", "shipment", "refund", "subscription", "chargeback", "upgrade", "quote",
    "reminder", "dispute", "settlement", "onboarding", "backorder", "credit", "warranty", "audit",
)  # fmt: skip

POOL_SIZE = 8192
BLOCK_ROWS = 100_000
DISK_MARGIN_BYTES = 256 * 1024 * 1024


def build_row_pool(rng: random.Random) -> list[bytes]:
    """Pre-render varied row tails (everything after the leading id column)."""
    pool: list[bytes] = []
    for _ in range(POOL_SIZE):
        user_id = rng.randrange(1, 5_000_000)
        timestamp = (
            f"2025-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}T"
            f"{rng.randint(0, 23):02d}:{rng.randint(0, 59):02d}:{rng.randint(0, 59):02d}Z"
        )
        description = " ".join(rng.choices(WORDS, k=rng.randint(2, 9)))
        row = (
            f"{timestamp},{user_id},emp{user_id}@internal.corp.test,"
            f"{rng.uniform(1, 9_999):.2f},{rng.choice(STATUSES)},{rng.choice(COUNTRIES)},"
            f'"{description}"\n'
        )
        pool.append(row.encode("ascii"))
    return pool


def generate(output: Path, target_bytes: int, seed: int) -> tuple[int, int]:
    """Write roughly ``target_bytes`` of CSV; return (bytes_written, rows)."""
    pool = build_row_pool(random.Random(seed))
    mask = POOL_SIZE - 1
    written = 0
    row_id = 1
    with output.open("wb", buffering=8 * 1024 * 1024) as sink:
        sink.write(HEADER)
        written += len(HEADER)
        while written < target_bytes:
            block = b"".join(
                b"%d," % (row_id + offset) + pool[(row_id + offset) & mask]
                for offset in range(BLOCK_ROWS)
            )
            row_id += BLOCK_ROWS
            sink.write(block)
            written += len(block)
    return written, row_id - 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--size-gb", type=float, default=2.0, help="approximate size (default: 2)")
    parser.add_argument("--output", type=Path, default=Path("data/dummy_2gb.csv"))
    parser.add_argument("--seed", type=int, default=20250101)
    parser.add_argument("--force", action="store_true", help="overwrite an existing file")
    args = parser.parse_args(argv)

    if args.size_gb <= 0:
        parser.error("--size-gb must be positive")
    target = int(args.size_gb * 1024**3)
    output: Path = args.output

    if output.exists() and not args.force:
        print(f"refusing to overwrite existing file: {output} (use --force)", file=sys.stderr)
        return 1
    output.parent.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(output.parent).free
    if free < target + DISK_MARGIN_BYTES:
        print(
            f"not enough free disk space: need ~{target / 1024**3:.1f} GiB, "
            f"have {free / 1024**3:.1f} GiB",
            file=sys.stderr,
        )
        return 1

    started = time.perf_counter()
    written, rows = generate(output, target, args.seed)
    elapsed = time.perf_counter() - started
    print(f"wrote {output} - {written / 1024**3:.2f} GiB, {rows:,} rows, {elapsed:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
