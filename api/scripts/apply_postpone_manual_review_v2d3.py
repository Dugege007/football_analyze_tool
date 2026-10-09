#!/usr/bin/env python3
"""0.3.19：v2d3 副本加推迟场 / 人工复核列并填值（只写副本；先导出前后清单）。

列（matches）：kickoff_original / kickoff_actual / postponed_announced_at（ISO +08:00）、postpone_ts_unknown(0/1)、
postpone_void_check（pending | NULL）、postpone_evidence（JSON 文本）、manual_review(0/1)、manual_review_reason。
- kickoff_at 仍是实际开赛（结算 / 赛果 / 时间线照实际）；目标时刻由 API 按 kickoff_original + 公告时刻推。
- 推迟场名单与证据：POSTPONED（决议 0.3.18 五件事 2 + 推迟场补充；扫描见 postpone-v2-2026-10-08/reports）。
- 人工复核：MANUAL_REVIEW（探针 209/210 亚盘符号与快照不一致，归分析师查）。
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.collection_schedule import postpone_void_check  # noqa: E402

LIVE = (ROOT / "data" / "app.db").resolve()
COLS = {"kickoff_original": "TEXT", "kickoff_actual": "TEXT", "postponed_announced_at": "TEXT",
        "postpone_ts_unknown": "INTEGER", "postpone_void_check": "TEXT", "postpone_evidence": "TEXT",
        "manual_review": "INTEGER", "manual_review_reason": "TEXT"}
POSTPONED = {
    "2026-07-05|日092": {
        "kickoff_original": "2026-07-06T08:00:00+08:00",
        "kickoff_actual": "2026-07-06T09:00:00+08:00",
        "postponed_announced_at": "2026-07-06T07:10:00+08:00",
        "evidence": {
            "public": ["ESPN: 雷暴，FIFA 宣布延至当地 19:00；17:10（墨城 UTC−6）确认 → 北京 07:10",
                       "BBC Sport: 'delayed by one hour ... 02:00 BST (19:00 local)'",
                       "FIFA match centre: Round of 16 · Match 92, Mexico City Stadium"],
            "ticks": "澳门首笔场中 07-06 09:01:11（1′），末笔赛前 07:35:23",
            "jingcai": "竞彩 kickoff_hour=8（kickoff_jc 07-06 08:00）",
        },
    },
}
MANUAL_REVIEW = {"probe:1263863300": "ah_sign_mismatch_probe", "probe:515799156": "ah_sign_mismatch_probe"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    db = Path(a.db).resolve()
    if db == LIVE:
        raise SystemExit("拒绝写现网 data/app.db")
    c = sqlite3.connect(str(db))
    c.row_factory = sqlite3.Row
    have = {r[1] for r in c.execute("PRAGMA table_info(matches)")}
    missing = [k for k in COLS if k not in have]
    if not a.dry_run:
        for k in missing:
            c.execute(f"ALTER TABLE matches ADD COLUMN {k} {COLS[k]}")
    sel = ", ".join(k if (k in have or not a.dry_run) else f"NULL AS {k}" for k in COLS)
    rows = []
    for uid, v in POSTPONED.items():
        r = c.execute(f"SELECT id, match_uid, kickoff_at, {sel} FROM matches WHERE match_uid=?", (uid,)).fetchone()
        if r is None:
            continue
        ko = datetime.fromisoformat(v["kickoff_original"]); ka = datetime.fromisoformat(v["kickoff_actual"])
        if r["kickoff_at"] and datetime.fromisoformat(r["kickoff_at"]) != ka:
            raise SystemExit(f"{uid}: kickoff_at {r['kickoff_at']} != kickoff_actual {v['kickoff_actual']}")
        new = {"kickoff_original": v["kickoff_original"], "kickoff_actual": v["kickoff_actual"],
               "postponed_announced_at": v.get("postponed_announced_at"),
               "postpone_ts_unknown": 0 if v.get("postponed_announced_at") else 1,
               "postpone_void_check": postpone_void_check(ko, ka),
               "postpone_evidence": json.dumps(v["evidence"], ensure_ascii=False)}
        rows.append({"match_id": r["id"], "match_uid": uid, "kind": "postponed",
                     **{f"before_{k}": r[k] for k in new}, **{f"after_{k}": x for k, x in new.items()}})
        if not a.dry_run:
            c.execute("UPDATE matches SET " + ", ".join(f"{k}=?" for k in new) + " WHERE id=?",
                      (*new.values(), r["id"]))
    for uid, reason in MANUAL_REVIEW.items():
        r = c.execute(f"SELECT id, match_uid, {sel} FROM matches WHERE match_uid=?", (uid,)).fetchone()
        if r is None:
            continue
        rows.append({"match_id": r["id"], "match_uid": uid, "kind": "manual_review",
                     "before_manual_review": r["manual_review"], "after_manual_review": 1,
                     "before_manual_review_reason": r["manual_review_reason"], "after_manual_review_reason": reason})
        if not a.dry_run:
            c.execute("UPDATE matches SET manual_review=1, manual_review_reason=? WHERE id=?", (reason, r["id"]))
    keys = sorted({k for x in rows for k in x}, key=lambda k: (k not in ("match_id", "match_uid", "kind"), k))
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    with open(a.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)
    if a.dry_run:
        c.rollback()
    else:
        c.commit()
    c.close()
    print(json.dumps({"columns_added": [] if a.dry_run else missing, "rows": len(rows), "dry_run": a.dry_run},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
