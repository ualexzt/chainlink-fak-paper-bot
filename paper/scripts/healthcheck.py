from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path
from typing import Sequence


def healthy(db_path: str | Path, max_age_seconds: float, *, now_ms: int | None = None) -> bool:
    if max_age_seconds <= 0:
        raise ValueError("max age must be positive")
    path = Path(db_path).absolute()
    observed_now_ms = time.time_ns() // 1_000_000 if now_ms is None else now_ms
    if isinstance(observed_now_ms, bool) or not isinstance(observed_now_ms, int) or observed_now_ms < 0:
        raise ValueError("now_ms must be a nonnegative integer")
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, isolation_level=None)
    try:
        db.execute("PRAGMA query_only=ON")
        if db.execute("PRAGMA quick_check(1)").fetchone()[0] != "ok":
            return False
        row = db.execute(
            "SELECT snapshot_ts_ms FROM dashboard_snapshots WHERE snapshot_id=1"
        ).fetchone()
        if row is None or isinstance(row[0], bool) or not isinstance(row[0], int):
            return False
        age_ms = observed_now_ms - row[0]
        return 0 <= age_ms <= int(max_age_seconds * 1000)
    finally:
        db.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--max-age", required=True, type=float)
    args = parser.parse_args(argv)
    try:
        return 0 if healthy(args.db, args.max_age) else 1
    except (OSError, sqlite3.Error, ValueError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
