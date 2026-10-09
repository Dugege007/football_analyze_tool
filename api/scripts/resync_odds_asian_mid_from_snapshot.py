#!/usr/bin/env python3
"""0.3.17 决策 5：v2d3 odds_asian 快照派生行（macau mid）按 exact_minute 快照重同步。只写副本。

- 只处理 extras.from_channel=rule、from_point=mid、book=macau、非 probe 的派生行；
- 期望值：handicap = −snapshot.line（odds_asian 主让为正；快照 API 记法主让为负），水位原样；
- 只改与快照不一致的行；extras 记 resync_0317 {before, snapshot_target_at, phase_target}；
- 写前备份由调用方负责；写后跑 validate_ah_sign.validate_conn（exit≠0 / blocked → 回滚）。
用法：.venv/bin/python scripts/resync_odds_asian_mid_from_snapshot.py --db data/v2d3/app.db [--apply] --out <csv>
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from validate_ah_sign import validate_conn  # noqa: E402

LIVE = (ROOT / "data" / "app.db").resolve()


def plan(c: sqlite3.Connection) -> list[dict]:
    c.row_factory = sqlite3.Row
    snap = {(r["match_id"], r["book"]): r for r in c.execute(
        "SELECT * FROM odds_snapshot WHERE market='asian' AND channel='rule' AND point='mid' AND book='macau'")}
    probes = {r[0] for r in c.execute("SELECT id FROM matches WHERE match_uid LIKE 'probe:%'")}
    out = []
    for r in c.execute("SELECT * FROM odds_asian WHERE book='macau' AND phase='mid' ORDER BY match_id"):
        ex = json.loads(r["extras_json"] or "{}")
        if ex.get("from_channel") != "rule" or ex.get("from_point") != "mid" or r["match_id"] in probes:
            continue
        s = snap.get((r["match_id"], "macau"))
        if s is None:
            continue
        want_line = None if s["line"] is None else (-float(s["line"]) if s["line"] else 0.0)
        cur = (r["handicap"], r["home_water"], r["away_water"], r["water_src"])
        want = (want_line, s["water_home"], s["water_away"], s["water_src"])
        if cur == want:
            continue
        sx = json.loads(s["extras_json"] or "{}")
        out.append({"odds_asian_id": r["id"], "match_id": r["match_id"],
                    "line_changed": cur[0] != want[0], "water_changed": cur[1:3] != want[1:3],
                    "before_handicap": cur[0], "before_home_water": cur[1], "before_away_water": cur[2],
                    "before_water_src": cur[3],
                    "after_handicap": want[0], "after_home_water": want[1], "after_away_water": want[2],
                    "after_water_src": want[3],
                    "snapshot_line_api": s["line"], "snapshot_target_at": s["target_at"],
                    "snapshot_phase_target": sx.get("phase_target"), "_extras": ex})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    if a.db.resolve() == LIVE:
        print("REFUSE: live app.db is never written", file=sys.stderr)
        return 2
    c = sqlite3.connect(a.db)
    rows = plan(c)
    with a.out.open("w", newline="", encoding="utf-8") as f:
        cols = [k for k in (rows[0] if rows else {"odds_asian_id": 0}) if not k.startswith("_")]
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    rep = {"n": len(rows), "line_changed": sum(r["line_changed"] for r in rows),
           "water_changed": sum(r["water_changed"] for r in rows), "applied": False}
    if a.apply and rows:
        for r in rows:
            ex = dict(r["_extras"])
            ex["resync_0317"] = {"before": {"handicap": r["before_handicap"], "home_water": r["before_home_water"],
                                            "away_water": r["before_away_water"]},
                                 "snapshot_target_at": r["snapshot_target_at"],
                                 "phase_target": r["snapshot_phase_target"], "at": "2026-10-08"}
            ex["snapshot_target_at"] = r["snapshot_target_at"]
            ex["sign_convention"] = "positive_home_gives"
            c.execute("UPDATE odds_asian SET handicap=?, home_water=?, away_water=?, water_src=?, extras_json=? "
                      "WHERE id=?", (r["after_handicap"], r["after_home_water"], r["after_away_water"],
                                     r["after_water_src"], json.dumps(ex, ensure_ascii=False), r["odds_asian_id"]))
        v = validate_conn(c, book="macau", phase="mid")
        rep["validate"] = {k: v.get(k) for k in ("exit_code", "blocked", "summary") if k in v} or v
        if v.get("exit_code", 0) == 2 or v.get("blocked"):
            c.rollback()
            rep["rolled_back"] = True
        else:
            c.commit()
            rep["applied"] = True
    c.close()
    print(json.dumps(rep, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
