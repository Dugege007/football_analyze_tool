#!/usr/bin/env python3
"""Import the Jingcai win draw loss odds history from 5DollarFootballAPI (bookmaker chinasportslottery,
market 1x2) into odds_timeline_seg as book=jc, market=euro_1x2 (analyst correction, 2026-10-10).

Input: the raw files of the full-match backfill queue,
odds-data/backfill/5df-fullmatch-history-queue/raw/hist/<fixture>_chinasportslottery_1x2.json
(the directory can be changed with --raw-dir; the live capture and other backfill scripts can call import_file).

Match mapping uses mappings that already exist in the database, in this order:
1. match_meta.extras_json ids.5df_fixture_id or fixture_id;
2. the match_uid recorded for the fixture in the backfill queue (queue/done.jsonl and queue/pending.jsonl).
Fixtures that cannot be mapped are skipped and listed in the report.

Home and away direction: match_meta.extras_json home_5df and away_5df are compared with matches.home_team and
matches.away_team. When they are reversed, the home and away odds are exchanged (the draw stays). When the
direction cannot be verified, the fixture is skipped and listed. A fixture without home_5df and away_5df is
treated as the same direction only when it was mapped by fixture id written by our own Jingcai plan (the plan
stores the Jingcai home and away order), otherwise it is skipped.

Segments follow import_odds_timeline_probe.py (change-point runs, pre-match ticks only). source is
5df_hist_jc_1x2. Repeatable, never writes odds_asian, never calls the interface, and only the v2d3 research
copy can be written.

Usage:
  .venv/bin/python api/scripts/import_jc_1x2_history_segments.py --db /workspace/match-analysis-api/data/v2d3/app.db [--dry-run]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from import_asof_history_segments import import_ticks  # noqa: E402

ODDS_DATA = Path(os.environ.get("ODDS_DATA_DIR") or "/workspace/odds-data")
QUEUE_DIR = ODDS_DATA / "backfill" / "5df-fullmatch-history-queue"
RAW_DIR = QUEUE_DIR / "raw" / "hist"
SOURCE = "5df_hist_jc_1x2"


def _kickoff(conn: sqlite3.Connection, match_id: int, fixture_raw: dict | None) -> datetime | None:
    if fixture_raw:
        k = (fixture_raw.get("data") or fixture_raw).get("kickoff_utc")
        if k:
            return datetime.fromisoformat(k.replace("Z", "+00:00"))
    r = conn.execute("SELECT kickoff_at FROM matches WHERE id=?", (match_id,)).fetchone()
    return datetime.fromisoformat(r[0]) if r and r[0] else None


def build_mapping(conn: sqlite3.Connection, queue_dir: Path = QUEUE_DIR) -> dict[str, dict]:
    """fixture id -> {match_id, orientation, via}."""
    teams = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT id, home_team, away_team FROM matches")}
    uid = {r[1]: r[0] for r in conn.execute("SELECT id, match_uid FROM matches")}
    out: dict[str, dict] = {}
    for mid, ex in conn.execute("SELECT match_id, extras_json FROM match_meta"):
        e = json.loads(ex or "{}")
        fid = (e.get("ids") or {}).get("5df_fixture_id") or e.get("fixture_id")
        if not fid or mid not in teams:
            continue
        h5, a5 = e.get("home_5df"), e.get("away_5df")
        if h5 and a5:
            o = "same" if (h5, a5) == teams[mid] else "swapped" if (a5, h5) == teams[mid] else (
                "same" if e.get("source") == "jc_match_rows_from_live_plan" else "unverified")
        else:
            o = "same" if e.get("source") == "jc_match_rows_from_live_plan" else "unverified"
        out.setdefault(str(fid), {"match_id": mid, "orientation": o, "via": "match_meta"})
    for name in ("done.jsonl", "pending.jsonl"):
        p = queue_dir / "queue" / name
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            fid, mu = str(r.get("fixture_id")), r.get("match_uid")
            if fid in out or mu not in uid:
                continue
            out[fid] = {"match_id": uid[mu], "orientation": "unverified", "via": "queue_match_uid"}
    return out


def import_file(conn: sqlite3.Connection, path: Path, mapping: dict[str, dict], dry_run: bool = False,
                fixture_dir: Path | None = None) -> dict:
    fid = path.name.split("_", 1)[0]
    m = mapping.get(fid)
    if m is None:
        return {"fixture_id": fid, "status": "skipped_unmapped", "segments": 0}
    if m["orientation"] not in ("same", "swapped"):
        return {"fixture_id": fid, "status": "skipped_orientation_unverified", "segments": 0,
                "match_id": m["match_id"]}
    data = json.loads(path.read_text(encoding="utf-8"))
    ticks = ((data.get("data") or {}).get("ticks") or []) if isinstance(data, dict) else []
    fx = None
    if fixture_dir is not None and (fixture_dir / f"{fid}.json").exists():
        fx = json.loads((fixture_dir / f"{fid}.json").read_text(encoding="utf-8"))
    kick = _kickoff(conn, m["match_id"], fx)
    if kick is None:
        return {"fixture_id": fid, "status": "skipped_kickoff_unknown", "segments": 0}
    r = import_ticks(conn, m["match_id"], "jc", "euro_1x2", ticks, kick, m["orientation"], str(path),
                     hashlib.sha256(path.read_bytes()).hexdigest(), dry_run, source=SOURCE)
    return {"fixture_id": fid, "match_id": m["match_id"], "orientation": m["orientation"], **r}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True)
    ap.add_argument("--raw-dir", default=str(RAW_DIR))
    ap.add_argument("--queue-dir", default=str(QUEUE_DIR))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    db = Path(a.db).resolve()
    if db.name != "app.db" or db.parent.name != "v2d3":
        raise SystemExit(f"Refusing to write {db}: only the v2d3 research copy (.../v2d3/app.db) is allowed.")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True) if a.dry_run else sqlite3.connect(str(db), timeout=60)
    conn.execute("PRAGMA busy_timeout=60000")
    mapping = build_mapping(conn, Path(a.queue_dir))
    files = sorted(Path(a.raw_dir).glob("*_chinasportslottery_1x2.json"))
    if not a.dry_run:
        conn.execute("BEGIN IMMEDIATE")
    res = [import_file(conn, p, mapping, a.dry_run, Path(a.raw_dir).parent / "fixture") for p in files]
    if not a.dry_run:
        conn.commit()
    report = {"db": str(db), "dry_run": a.dry_run, "files": len(files),
              "matches_imported": len({r["match_id"] for r in res if r.get("segments")}),
              "segments": sum(r["segments"] for r in res),
              "skipped": [r for r in res if r["status"].startswith("skipped")], "results": res}
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
