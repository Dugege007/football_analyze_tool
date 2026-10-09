#!/usr/bin/env python3
"""0.3.18 第二批口径 D：v2d3 odds_asian 澳门派生行按 exact_minute 快照同步（open / mid / close）。只写副本。

特征和结算必须是同一个盘：以 odds_snapshot（book=macau, market=asian, channel=rule, point=<phase>）为准。
- 只处理 extras.from_channel=rule、from_point=<phase>、book=macau、非 probe 的派生行（旧手工 / legacy_import 不动）；
- 期望值：handicap = −snapshot.line（odds_asian 主让为正；快照 API 记法主让为负，0 仍为 0），水位、water_src 原样；
- 只改与快照不一致的行；extras 记 resync_0318 {before, snapshot_target_at, phase_target, exception_rule}；
- 写前备份由调用方负责；写后逐 phase 跑 validate_ah_sign.validate_conn（exit=2 / blocked → 整体回滚）。
用法：.venv/bin/python scripts/resync_odds_asian_from_snapshot.py --db data/v2d3/app.db --phases open,close [--apply] --out <csv>
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
PHASES = ("open", "mid", "close")


def plan(c: sqlite3.Connection, phases: tuple[str, ...]) -> list[dict]:
    c.row_factory = sqlite3.Row
    probes = {r[0] for r in c.execute("SELECT id FROM matches WHERE match_uid LIKE 'probe:%'")}
    uid = {r[0]: r[1] for r in c.execute("SELECT id, match_uid FROM matches")}
    out = []
    for ph in phases:
        snap = {r["match_id"]: r for r in c.execute(
            "SELECT * FROM odds_snapshot WHERE market='asian' AND channel='rule' AND point=? AND book='macau'", (ph,))}
        for r in c.execute("SELECT * FROM odds_asian WHERE book='macau' AND phase=? ORDER BY match_id", (ph,)):
            ex = json.loads(r["extras_json"] or "{}")
            if ex.get("from_channel") != "rule" or ex.get("from_point") != ph or r["match_id"] in probes:
                continue
            s = snap.get(r["match_id"])
            if s is None:
                continue
            want_line = None if s["line"] is None else (-float(s["line"]) if s["line"] else 0.0)
            cur = (r["handicap"], r["home_water"], r["away_water"], r["water_src"])
            want = (want_line, s["water_home"], s["water_away"], s["water_src"])
            if cur == want:
                continue
            sx = json.loads(s["extras_json"] or "{}")
            out.append({"odds_asian_id": r["id"], "match_id": r["match_id"], "match_uid": uid.get(r["match_id"]),
                        "phase": ph, "line_changed": cur[0] != want[0], "water_changed": cur[1:3] != want[1:3],
                        "before_handicap": cur[0], "before_home_water": cur[1], "before_away_water": cur[2],
                        "before_water_src": cur[3],
                        "after_handicap": want[0], "after_home_water": want[1], "after_away_water": want[2],
                        "after_water_src": want[3],
                        "snapshot_line_api": s["line"], "snapshot_recorded_at": s["recorded_at"],
                        "snapshot_target_at": s["target_at"],
                        "snapshot_phase_target": sx.get("phase_target"),
                        "snapshot_exception_rule": sx.get("exception_rule"), "_extras": ex})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, required=True)
    ap.add_argument("--phases", default="open,close")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    if a.db.resolve() == LIVE:
        print("REFUSE: live app.db is never written", file=sys.stderr)
        return 2
    phases = tuple(p.strip() for p in a.phases.split(",") if p.strip())
    bad = [p for p in phases if p not in PHASES]
    if bad:
        raise SystemExit(f"unknown phases {bad}")
    c = sqlite3.connect(a.db)
    rows = plan(c, phases)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    with a.out.open("w", newline="", encoding="utf-8") as f:
        cols = [k for k in (rows[0] if rows else {"odds_asian_id": 0}) if not k.startswith("_")]
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    rep = {"n": len(rows), "applied": False,
           "by_phase": {ph: {"rows": sum(r["phase"] == ph for r in rows),
                             "line_changed": sum(r["line_changed"] for r in rows if r["phase"] == ph),
                             "water_only": sum((not r["line_changed"]) and r["water_changed"]
                                               for r in rows if r["phase"] == ph)} for ph in phases}}
    if a.apply and rows:
        for r in rows:
            ex = dict(r["_extras"])
            ex["resync_0318"] = {"before": {"handicap": r["before_handicap"], "home_water": r["before_home_water"],
                                            "away_water": r["before_away_water"], "water_src": r["before_water_src"]},
                                 "snapshot_target_at": r["snapshot_target_at"],
                                 "phase_target": r["snapshot_phase_target"],
                                 "exception_rule": r["snapshot_exception_rule"], "at": "2026-10-08",
                                 "decision": "second-batch D: settlement = feature snapshot"}
            ex["snapshot_target_at"] = r["snapshot_target_at"]
            ex["sign_convention"] = "positive_home_gives"
            c.execute("UPDATE odds_asian SET handicap=?, home_water=?, away_water=?, water_src=?, extras_json=? "
                      "WHERE id=?", (r["after_handicap"], r["after_home_water"], r["after_away_water"],
                                     r["after_water_src"], json.dumps(ex, ensure_ascii=False), r["odds_asian_id"]))
        rep["validate"] = {}
        bad_v = False
        for ph in PHASES:
            v = validate_conn(c, book="macau", phase=ph)
            rep["validate"][ph] = {"exit_code": v.get("exit_code"), "blocked": v.get("blocked"),
                                   "review": len(v.get("review") or [])}
            bad_v |= v.get("exit_code", 0) == 2 or bool(v.get("blocked"))
        if bad_v:
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
