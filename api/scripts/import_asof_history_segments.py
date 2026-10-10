#!/usr/bin/env python3
"""Import the full 5DollarFootballAPI odds history that the missed-mid rescue already downloaded
(odds-data/5dollar/live/asof_backfill/raw/<fixture>_<bookmaker>_asian.json) into odds_timeline_seg.

Why (analyst finding, 2026-10-10): the rescue for a missed mid stage only wrote one as-of row into odds_snapshot
(source=5df_hist_asof, capture=asof_hist, point=mid). The full history stayed in the raw files, so the first
timeline record of those matches looked late and the opening cells were wrongly marked suspect_truncated.

Rules (same as import_odds_timeline_probe.py):
- Change-point runs: consecutive ticks with the same line and prices are merged into one segment; a segment ends
  where the next one starts, and the last one ends at kickoff. Only pre-match ticks are used (no minute, earlier
  than kickoff).
- Water is the decimal price minus 1. The line keeps the API notation (a negative number means the home team gives
  the handicap), the same as the existing segments. When the replica match has the home and away teams swapped
  relative to 5DollarFootballAPI (orientation=swapped), the line sign and the home and away prices are swapped,
  the same way the rescue itself does.
- source = 5df_hist_asof_full; extras_json records the raw file and its SHA-256 checksum.
- Repeatable: a (match, bookmaker, market) that already has any segment is skipped, and the table's unique key
  (match, bookmaker, market, segment start, compression) prevents duplicates.
- Never writes odds_asian and never calls the interface. Only the v2d3 research copy can be written.

Usage:
  .venv/bin/python api/scripts/import_asof_history_segments.py --db api/data/v2d3/app.db [--dry-run]
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
from import_odds_timeline_probe import pre_match_ticks, ticks_to_segments  # noqa: E402

REPO = HERE.parents[1]
ODDS_DATA = Path(os.environ.get("ODDS_DATA_DIR") or "/workspace/odds-data")
RAW_DIR = ODDS_DATA / "5dollar" / "live" / "asof_backfill" / "raw"
SOURCE = "5df_hist_asof_full"
BOOK_MAP = {"macauslot": "macau", "crown": "crown", "williamhill": "william", "pinnacle": "pinnacle",
            "bet365": "bet365"}
MARKET_MAP = {"asian": "asian", "goalline": "ou", "1x2": "euro_1x2"}


def _orient_ticks(ticks: list[dict], orientation: str) -> list[dict]:
    if orientation != "swapped":
        return ticks
    out = []
    for t in ticks:
        t = dict(t)
        if t.get("line") is not None:
            t["line"] = -float(t["line"])
        t["home"], t["away"] = t.get("away"), t.get("home")
        out.append(t)
    return out


def import_ticks(conn: sqlite3.Connection, match_id: int, book: str, market: str, ticks: list[dict],
                 kickoff: datetime, orientation: str, raw_path: str | None = None,
                 raw_sha256: str | None = None, dry_run: bool = False) -> dict:
    """Import one bookmaker and market history for one match. Returns a small report."""
    if orientation not in ("same", "swapped"):
        return {"status": "skipped_orientation_unverified", "segments": 0}
    if conn.execute("SELECT 1 FROM odds_timeline_seg WHERE match_id=? AND book=? AND market=? LIMIT 1",
                    (match_id, book, market)).fetchone():
        return {"status": "skipped_segments_exist", "segments": 0}
    segs = ticks_to_segments(pre_match_ticks(_orient_ticks(ticks, orientation), kickoff), kickoff, market)
    if dry_run:
        return {"status": "would_insert", "segments": len(segs)}
    ex = json.dumps({"raw_path": raw_path, "raw_sha256": raw_sha256, "orientation": orientation,
                     "importer": "import_asof_history_segments.py"}, ensure_ascii=False, sort_keys=True)
    n = 0
    for s in segs:
        cur = conn.execute(
            "INSERT OR IGNORE INTO odds_timeline_seg (match_id, book, market, seg_start_at, seg_end_at, line,"
            " price_home, price_away, price_draw, price_over, price_under, water_home, water_away, water_over,"
            " water_under, tick_count, compression, is_inplay, source, water_src, extras_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'change_point',0,?,?,?)",
            (match_id, book, market, s["seg_start_at"], s["seg_end_at"], s.get("line"), s.get("price_home"),
             s.get("price_away"), s.get("price_draw"), s.get("price_over"), s.get("price_under"),
             s.get("water_home"), s.get("water_away"), s.get("water_over"), s.get("water_under"),
             s["tick_count"], SOURCE, "actual" if market in ("asian", "ou") else None, ex))
        n += cur.rowcount
    return {"status": "inserted", "segments": n}


def import_raw_file(conn: sqlite3.Connection, path: Path, match_id: int, kickoff: datetime, orientation: str,
                    dry_run: bool = False) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    d = (data.get("data") or {}) if isinstance(data, dict) else {}
    book = BOOK_MAP.get(d.get("bookmaker"))
    market = MARKET_MAP.get(d.get("market"))
    if book is None or market is None:
        return {"status": "skipped_unknown_book_or_market", "segments": 0}
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    rep = import_ticks(conn, match_id, book, market, d.get("ticks") or [], kickoff, orientation,
                       str(path), sha, dry_run)
    return {**rep, "book": book, "market": market}


def rescue_targets(conn: sqlite3.Connection) -> dict[int, dict]:
    """fixture id -> {match_id, kickoff, orientation} from the rescue rows already in odds_snapshot."""
    out: dict[int, dict] = {}
    for r in conn.execute("SELECT match_id, extras_json FROM odds_snapshot WHERE source='5df_hist_asof'"):
        ex = json.loads(r[1] or "{}")
        fid = ex.get("fixture_id")
        if fid is None or not ex.get("kickoff_at_5df"):
            continue
        out.setdefault(int(fid), {"match_id": r[0], "kickoff": datetime.fromisoformat(ex["kickoff_at_5df"]),
                                  "orientation": ex.get("orientation"), "match_uid": ex.get("match_uid")})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True)
    ap.add_argument("--raw-dir", default=str(RAW_DIR))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    db = Path(a.db).resolve()
    if db.name != "app.db" or db.parent.name != "v2d3":
        raise SystemExit(f"Refusing to write {db}: only the v2d3 research copy (.../v2d3/app.db) is allowed.")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True) if a.dry_run else sqlite3.connect(str(db), timeout=60)
    conn.execute("PRAGMA busy_timeout=60000")
    targets = rescue_targets(conn)
    report = {"db": str(db), "dry_run": a.dry_run, "files": [], "matches": set(), "segments": 0}
    if not a.dry_run:
        conn.execute("BEGIN IMMEDIATE")
    for p in sorted(Path(a.raw_dir).glob("*.json")):
        if p.name.endswith(".err.json"):
            continue
        fid = int(p.name.split("_", 1)[0])
        t = targets.get(fid)
        if t is None:
            report["files"].append({"file": p.name, "status": "skipped_no_rescue_row"})
            continue
        r = import_raw_file(conn, p, t["match_id"], t["kickoff"], t["orientation"] or "", a.dry_run)
        report["files"].append({"file": p.name, "match_uid": t["match_uid"], **r})
        if r["segments"]:
            report["matches"].add(t["match_uid"])
            report["segments"] += r["segments"]
    if not a.dry_run:
        conn.commit()
    report["matches"] = sorted(report["matches"])
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
