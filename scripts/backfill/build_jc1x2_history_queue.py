#!/usr/bin/env python3
"""Build the separate Jingcai win draw loss history backfill queue (user decision via the analyst, 2026-10-10).

This script only writes a queue file. It never calls the interface, never runs the queue and never writes any
database.

Queue content: every match that is already mapped to a 5DollarFootballAPI fixture but has no local
<fixture>_chinasportslottery_1x2.json history file yet. Every queued match will later need exactly one call:
GET /fixtures/{fixture_id}/odds/history?bookmaker=chinasportslottery&market=1x2

Mapping sources (all read only):
1. the full-match backfill queue (queue/done.jsonl and queue/pending.jsonl: fixture_id and match_uid);
2. match_meta.extras_json (ids.5df_fixture_id or fixture_id) of the research copy and of the live database.
Local files are searched for in every raw data directory under the odds data directory (recursively).

Output (default directory odds-data/backfill/5df-jc1x2-history-queue/queue/):
- pending.jsonl: one line per match {fixture_id, match_uid, jingcai_date, month, mapped_by, request}.
- summary.json: total and distribution by month.

Usage:
  .venv/bin/python scripts/backfill/build_jc1x2_history_queue.py [--out-dir DIR]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ODDS_DATA = Path(os.environ.get("ODDS_DATA_DIR") or "/workspace/odds-data")
FULLMATCH_QUEUE = ODDS_DATA / "backfill" / "5df-fullmatch-history-queue" / "queue"
DEFAULT_DBS = [Path(os.environ.get("V2D3_DB_PATH") or "/workspace/match-analysis-api/data/v2d3/app.db"),
               Path(os.environ.get("LIVE_DB_PATH") or "/workspace/match-analysis-api/data/app.db")]
OUT_DIR = ODDS_DATA / "backfill" / "5df-jc1x2-history-queue" / "queue"
FILE_SUFFIX = "_chinasportslottery_1x2.json"
TZ = timezone(timedelta(hours=8))


def local_fixture_files(root: Path) -> set[str]:
    """Fixture ids that already have a Jingcai win draw loss history file anywhere under root."""
    out = set()
    for p in root.rglob(f"*{FILE_SUFFIX}"):
        out.add(p.name.split("_", 1)[0])
    return out


def mapped_fixtures(queue_dir: Path, dbs: list[Path]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for name in ("done.jsonl", "pending.jsonl"):
        p = queue_dir / name
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            fid = str(r.get("fixture_id") or "")
            if fid:
                out.setdefault(fid, {"match_uid": r.get("match_uid"), "jingcai_date": r.get("jingcai_date"),
                                     "mapped_by": f"fullmatch_queue/{name}"})
    for db in dbs:
        if not db.exists():
            continue
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            rows = c.execute("SELECT m.match_uid, m.jingcai_date, mm.extras_json FROM match_meta mm"
                             " JOIN matches m ON m.id = mm.match_id").fetchall()
        finally:
            c.close()
        for uid, jd, ex in rows:
            e = json.loads(ex or "{}")
            fid = str((e.get("ids") or {}).get("5df_fixture_id") or e.get("fixture_id") or "")
            if fid:
                out.setdefault(fid, {"match_uid": uid, "jingcai_date": jd, "mapped_by": f"match_meta/{db}"})
    return out


def build(queue_dir: Path, dbs: list[Path], search_root: Path) -> tuple[list[dict], dict]:
    have = local_fixture_files(search_root)
    rows = []
    for fid, m in sorted(mapped_fixtures(queue_dir, dbs).items(), key=lambda x: (x[1].get("jingcai_date") or "", x[0])):
        if fid in have:
            continue
        jd = m.get("jingcai_date") or (m.get("match_uid") or "").split("|")[0] or None
        rows.append({"fixture_id": fid, "match_uid": m.get("match_uid"), "jingcai_date": jd,
                     "month": jd[:7] if jd else "unknown", "mapped_by": m["mapped_by"],
                     "request": f"/fixtures/{fid}/odds/history?bookmaker=chinasportslottery&market=1x2",
                     "calls_needed": 1, "status": "pending"})
    summary = {"generated_at": datetime.now(TZ).isoformat(timespec="seconds"), "total": len(rows),
               "already_local_files": len(have), "by_month": dict(sorted(Counter(r["month"] for r in rows).items())),
               "note": "Queue only; not run. Each match needs exactly one history call."}
    return rows, summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out-dir", default=str(OUT_DIR))
    a = ap.parse_args(argv)
    rows, summary = build(FULLMATCH_QUEUE, DEFAULT_DBS, ODDS_DATA)
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "pending.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
