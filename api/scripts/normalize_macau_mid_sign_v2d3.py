#!/usr/bin/env python3
"""v2d3 副本一次性：odds_asian 澳门 mid（5df_macauslot_history 派生行）API 记法(负=主让) → 正数=主让。

2026-10-08 §12 第 5 条。只动副本；拒绝现网；先备份（调用方负责）；幂等（extras.sign_convention 已标则跳过）。
校验：改后每行 handicap == −odds_snapshot(rule, mid).line；改后跑 validate_ah_sign 必须 pass。
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from validate_ah_sign import validate_conn  # noqa: E402

LIVE_DB = (ROOT / "data" / "app.db").resolve()
SOURCE = "5df_macauslot_history"
MARK = "positive_home_gives"
STAMP = "2026-10-08 table-12-decisions (api_negative→positive_home_gives)"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--csv", required=True)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    db = Path(a.db).resolve()
    if db == LIVE_DB:
        raise SystemExit("拒绝写现网 data/app.db")
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT oa.id, oa.match_id, m.match_uid, oa.handicap, oa.extras_json, s.line AS snap_line
           FROM odds_asian oa JOIN matches m ON m.id = oa.match_id
           LEFT JOIN odds_snapshot s ON s.match_id = oa.match_id AND s.book = oa.book
                AND s.market = 'asian' AND s.channel = 'rule' AND s.point = 'mid'
           WHERE oa.book = 'macau' AND oa.phase = 'mid' AND m.match_uid NOT LIKE 'probe:%'
           ORDER BY oa.match_id""").fetchall()
    stats = {"scope_rows": len(rows), "changed": 0, "flat_unchanged": 0, "already_marked": 0,
             "other_source_skipped": 0, "snapshot_mismatch_before": 0}
    out = []
    for r in rows:
        ex = json.loads(r["extras_json"]) if r["extras_json"] else {}
        if ex.get("source") != SOURCE:
            stats["other_source_skipped"] += 1
            continue
        if ex.get("sign_convention") == MARK:
            stats["already_marked"] += 1
            continue
        h = r["handicap"]
        if r["snap_line"] is None or float(h) != float(r["snap_line"]):
            stats["snapshot_mismatch_before"] += 1  # 期望：派生行 = 快照原值（API 记法）
        new_h = 0.0 if float(h) == 0 else -float(h)
        ex["sign_convention"] = MARK
        if new_h != float(h):
            ex["sign_normalized"] = STAMP
            ex["handicap_before_sign_normalize"] = h
            stats["changed"] += 1
        else:
            stats["flat_unchanged"] += 1
        out.append({"odds_asian_id": r["id"], "match_id": r["match_id"], "match_uid": r["match_uid"],
                    "handicap_before": h, "handicap_after": new_h, "snapshot_rule_mid_line_api": r["snap_line"]})
        conn.execute("UPDATE odds_asian SET handicap = ?, extras_json = ? WHERE id = ?",
                     (new_h, json.dumps(ex, ensure_ascii=False), r["id"]))
    # 改后核对：handicap == −snapshot line
    bad = conn.execute(
        """SELECT COUNT(*) FROM odds_asian oa JOIN matches m ON m.id = oa.match_id
           JOIN odds_snapshot s ON s.match_id = oa.match_id AND s.book = oa.book AND s.market = 'asian'
                AND s.channel = 'rule' AND s.point = 'mid'
           WHERE oa.book = 'macau' AND oa.phase = 'mid' AND m.match_uid NOT LIKE 'probe:%'
             AND oa.handicap != -s.line""").fetchone()[0]
    stats["post_mismatch_vs_neg_snapshot"] = bad
    rep = validate_conn(conn, book="macau")
    stats["validator_after_status"] = rep["status"]
    with open(a.csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0].keys()) if out else ["odds_asian_id"])
        w.writeheader()
        w.writerows(out)
    if a.dry_run or bad or rep["status"] != "pass":
        conn.rollback()
        stats["committed"] = False
    else:
        conn.commit()
        stats["committed"] = True
    conn.close()
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0 if stats["committed"] or a.dry_run else 3


if __name__ == "__main__":
    sys.exit(main())
