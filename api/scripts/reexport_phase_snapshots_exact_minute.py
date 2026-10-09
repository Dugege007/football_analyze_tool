#!/usr/bin/env python3
"""v2d3 副本：按「精确到分钟」的开赛时刻重导澳门亚盘阶段快照（phase_target=exact_minute）。

2026-10-08 §12 第 2 条 + 算法顾问补充：
  - rule.mid / rule.close：例外场 [23:00, 次日 11:30] 仍为竞彩日 15:00 / 22:00；其它 = T−8h / T−1h 精确到分钟
    （kickoff_minute_known=0 → 整点）。actual.t8 / actual.t1 = T−8h / T−1h 精确到分钟。
  - 数据：本地原始 tick（RAW_DIR，5DF macauslot，0 次 API 调用），取 last tick ≤ target（与 fill_macau_mid_water 同口径）。
  - 旧导出先整表导出为 CSV/JSONL 并标 phase_target=hour_floor（对照用）；新行 extras_json 写 phase_target=exact_minute。
  - 只改 odds_snapshot 里 source=5df_macauslot_history 的 rule.mid/close + actual.t8/t1；
    不改 odds_asian（派生行仍是 hour_floor 口径，回测分开记账）、不动 predictions、拒绝现网。
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

from app.collection_schedule import EXCEPTION_RULE, PHASE_TARGET, TZ_CN, channel_targets  # noqa: E402
import fill_macau_mid_water as F  # noqa: E402

LIVE_DB = (ROOT / "data" / "app.db").resolve()
POINTS = (("rule", "mid"), ("rule", "close"), ("actual", "t8"), ("actual", "t1"))
COLS = ("id", "match_id", "book", "market", "channel", "point", "recorded_at", "target_at", "lag_hours",
        "stale_gap", "line", "price_home", "price_away", "water_home", "water_away", "water_src", "source",
        "extras_json")


def _postpone(m) -> dict | None:
    """0.3.19：v2d3 推迟场列（无列 / 空 → None）。"""
    keys = m.keys()
    if "kickoff_original" not in keys or not m["kickoff_original"]:
        return None
    from datetime import datetime
    return {"kickoff_original": datetime.fromisoformat(m["kickoff_original"]),
            "kickoff_actual": datetime.fromisoformat(m["kickoff_actual"]) if m["kickoff_actual"] else None,
            "announced_at": datetime.fromisoformat(m["postponed_announced_at"]) if m["postponed_announced_at"] else None}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--dry-run", action="store_true")
    # 0.3.18：只重导指定场（例外场按编号判后目标变化的场），并在 extras 记 exception_rule
    ap.add_argument("--match-ids", default="", help="逗号分隔 match_id；空 = 全部")
    ap.add_argument("--tag", default="2026-10-08 table-12-decisions")
    a = ap.parse_args()
    only = {int(x) for x in a.match_ids.split(",") if x.strip()}
    db = Path(a.db).resolve()
    if db == LIVE_DB:
        raise SystemExit("拒绝写现网 data/app.db")
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row

    old = {(r["match_id"], r["channel"], r["point"]): dict(r) for r in conn.execute(
        f"SELECT {', '.join(COLS)} FROM odds_snapshot WHERE book='macau' AND market='asian' AND source=?"
        " AND ((channel='rule' AND point IN ('mid','close')) OR (channel='actual' AND point IN ('t8','t1')))",
        (F.SOURCE,)) if not only or r["match_id"] in only}
    # 1) 旧导出留档（hour_floor）
    with open(out / "snapshot_export_hour_floor.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=("phase_target",) + COLS)
        w.writeheader()
        for k in sorted(old):
            w.writerow({"phase_target": "hour_floor", **old[k]})

    items = {it["match_id"]: it for it in json.loads(F.MAP_PATH.read_text(encoding="utf-8"))["items"]}
    stats = {"old_rows": len(old), "rows_reexported": 0, "rows_target_changed": 0, "rows_value_changed": 0,
             "matches": 0, "matches_no_raw": 0, "by_point": {}, "matches_mid_or_close_value_changed": 0,
             "matches_mid_value_changed": 0, "matches_close_value_changed": 0,
             "matches_t8_or_t1_value_changed": 0, "matches_target_changed": 0}
    diff_rows = []
    mids = sorted({k[0] for k in old})
    for mid in mids:
        it = items.get(mid)
        path = F.RAW_DIR / f"{it['fixture_id']}_macauslot_asian.json" if it else None
        if not path or not path.exists():
            stats["matches_no_raw"] += 1
            continue
        stats["matches"] += 1
        ticks_all = ((json.loads(path.read_text(encoding="utf-8")).get("data") or {}).get("ticks")) or []
        if it.get("swapped"):
            fl = []
            for t in ticks_all:
                t2 = dict(t)
                if t2.get("line") is not None:
                    t2["line"] = -float(t2["line"])
                t2["home"], t2["away"] = t2.get("away"), t2.get("home")
                fl.append(t2)
            ticks_all = fl
        ticks = F.pre_ticks(ticks_all)
        m = conn.execute("SELECT * FROM matches WHERE id=?",
                         (mid,)).fetchone()
        kick = F._parse_dt(m["kickoff_at"]).astimezone(TZ_CN)
        tg = channel_targets(jingcai_date=m["jingcai_date"], kickoff_hour=int(m["kickoff_hour"]), kickoff=kick,
                             minute_known=bool(m["kickoff_minute_known"]), has_jc_code=bool(m["jc_id"]),
                             postpone=_postpone(m))
        want = {("rule", "mid"): tg["rule"]["mid"], ("rule", "close"): tg["rule"]["close"],
                ("actual", "t8"): tg["actual"]["t8"], ("actual", "t1"): tg["actual"]["t1"]}
        changed_pts = set()
        tgt_changed = False
        for ch, pt in POINTS:
            o = old.get((mid, ch, pt))
            if o is None:
                continue
            tgt = want[(ch, pt)]
            tick = F.last_tick_at_or_before(ticks, tgt) if ticks else None
            if tick is None and (ch, pt) == ("rule", "close") and ticks:
                tick = ticks[-1]  # 与 fill_macau_mid_water 同：close 无 ≤target 的 tick → 最后一条赛前 tick
            s = F.snap_row(tick, target=tgt, point=pt, channel=ch) if tick else None
            if s is None:
                continue
            ex = json.loads(s["extras_json"])
            old_ex = json.loads(o["extras_json"] or "{}")
            ex.update({"phase_target": PHASE_TARGET, "reexported": a.tag,
                       "target_at_hour_floor": old_ex.get("target_at_hour_floor", o["target_at"]),
                       "exception_rule": EXCEPTION_RULE})
            if _postpone(m):
                ex["postpone_rule"] = "known_kickoff_at_target (0.3.19)"
            if only:
                ex["target_at_before_reexport"] = o["target_at"]
            new = {"recorded_at": s["recorded_at"], "target_at": s["target_at"], "lag_hours": s["lag_hours"],
                   "stale_gap": s["stale_gap"], "line": s["line"], "price_home": s["price_home"],
                   "price_away": s["price_away"], "water_home": s["water_home"], "water_away": s["water_away"]}
            t_ch = F._parse_dt(o["target_at"]) != F._parse_dt(new["target_at"])
            v_ch = any((o[k] if o[k] is None else float(o[k])) != (new[k] if new[k] is None else float(new[k]))
                       for k in ("line", "water_home", "water_away"))
            bp = stats["by_point"].setdefault(f"{ch}.{pt}", {"rows": 0, "target_changed": 0, "value_changed": 0,
                                                              "tick_changed": 0})
            bp["rows"] += 1
            bp["target_changed"] += int(t_ch)
            bp["value_changed"] += int(v_ch)
            bp["tick_changed"] += int(o["recorded_at"] != new["recorded_at"])
            stats["rows_reexported"] += 1
            stats["rows_target_changed"] += int(t_ch)
            stats["rows_value_changed"] += int(v_ch)
            tgt_changed |= t_ch
            if v_ch:
                changed_pts.add(pt)
            if t_ch or v_ch or o["recorded_at"] != new["recorded_at"]:
                diff_rows.append({"match_id": mid, "channel": ch, "point": pt,
                                  "target_hour_floor": o["target_at"], "target_exact_minute": new["target_at"],
                                  "recorded_hour_floor": o["recorded_at"], "recorded_exact_minute": new["recorded_at"],
                                  "line_api_old": o["line"], "line_api_new": new["line"],
                                  "wh_old": o["water_home"], "wh_new": new["water_home"],
                                  "wa_old": o["water_away"], "wa_new": new["water_away"],
                                  "value_changed": v_ch})
            unchanged = not (t_ch or v_ch or o["recorded_at"] != new["recorded_at"])
            if only and unchanged:
                stats["rows_unchanged_skipped"] = stats.get("rows_unchanged_skipped", 0) + 1
            elif not a.dry_run:
                conn.execute(
                    "UPDATE odds_snapshot SET recorded_at=?, target_at=?, lag_hours=?, stale_gap=?, line=?,"
                    " price_home=?, price_away=?, water_home=?, water_away=?, extras_json=? WHERE id=?",
                    (new["recorded_at"], new["target_at"], new["lag_hours"], new["stale_gap"], new["line"],
                     new["price_home"], new["price_away"], new["water_home"], new["water_away"],
                     json.dumps(ex, ensure_ascii=False), o["id"]))
        stats["matches_target_changed"] += int(tgt_changed)
        stats["matches_mid_value_changed"] += int("mid" in changed_pts)
        stats["matches_close_value_changed"] += int("close" in changed_pts)
        stats["matches_mid_or_close_value_changed"] += int(bool({"mid", "close"} & changed_pts))
        stats["matches_t8_or_t1_value_changed"] += int(bool({"t8", "t1"} & changed_pts))
    with open(out / "snapshot_reexport_diff.csv", "w", newline="", encoding="utf-8") as f:
        if diff_rows:
            w = csv.DictWriter(f, fieldnames=list(diff_rows[0].keys()))
            w.writeheader()
            w.writerows(diff_rows)
    meta = {"phase_target_old": "hour_floor", "phase_target_new": PHASE_TARGET, "db": str(db),
            "exception_rule": EXCEPTION_RULE, "match_ids": sorted(only) or "all", "tag": a.tag,
            "dry_run": a.dry_run, "api_calls": 0, "odds_asian_modified": False, "predictions_modified": False,
            "fingerprint_note": "回测/影子台账按 phase_target 分开记账；旧冻结预测 = hour_floor，不重算", **stats}
    if a.dry_run:
        conn.rollback()
    else:
        conn.commit()
    conn.close()
    (out / ("reexport_meta_dryrun.json" if a.dry_run else "reexport_meta.json")).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
