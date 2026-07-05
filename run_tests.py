#!/usr/bin/env python
"""Run the mem4 test suite with per-file process isolation.

Each test file gets its own fresh ``python -m pytest <file>`` subprocess. This
mirrors how the upstream Hermes suite runs (``scripts/run_tests_parallel.py``)
and is *required* here for the same reason: the tests exercise SQLite FTS5
virtual tables and a background backfill thread, whose native/global state does
not reset cleanly between tests in a single long-lived interpreter. Running
every file together in one process can crash the interpreter (SIGSEGV); one
process per file keeps each run clean.

Exit code is non-zero if any file fails, so CI can gate on it.

Usage:
    python run_tests.py            # run all test files
    python run_tests.py -k recall  # extra args are forwarded to pytest
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent / "tests"


def main() -> int:
    extra = sys.argv[1:]
    files = sorted(TESTS_DIR.glob("test_*.py"))
    if not files:
        print("no test files found under tests/", file=sys.stderr)
        return 1

    failures: list[str] = []
    for f in files:
        print(f"\n=== {f.name} " + "=" * (60 - len(f.name)))
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", str(f), "-q", *extra],
        )
        if proc.returncode != 0:
            failures.append(f.name)

    print("\n" + "=" * 64)
    if failures:
        print(f"FAILED files ({len(failures)}): {', '.join(failures)}")
        return 1
    print(f"ALL GREEN — {len(files)} test files passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
