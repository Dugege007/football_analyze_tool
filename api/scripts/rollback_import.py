#!/usr/bin/env python3
"""Rollback one import_batches row: delete its matches (CASCADE children) and mark rolled_back."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import DB_PATH, connect  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--batch-id", type=int, required=True)
    args = ap.parse_args()

    conn = connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT id, source_file, month, status, row_count FROM import_batches WHERE id=?",
            (args.batch_id,),
        ).fetchone()
        if not row:
            raise SystemExit(f"batch {args.batch_id} not found")
        if row["status"] == "rolled_back":
            print(f"batch {args.batch_id} already rolled_back")
            return

        cur = conn.execute(
            "DELETE FROM matches WHERE import_batch_id = ?", (args.batch_id,)
        )
        deleted = cur.rowcount
        conn.execute(
            "UPDATE import_batches SET status='rolled_back' WHERE id=?",
            (args.batch_id,),
        )
        conn.commit()
        print(
            f"rolled_back batch_id={args.batch_id} source={row['source_file']} "
            f"month={row['month']} deleted_matches={deleted}"
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
