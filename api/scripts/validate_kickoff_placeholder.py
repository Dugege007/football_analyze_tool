#!/usr/bin/env python3
"""导入前校验：5DF 开赛恰为 12:00 的占位符（0316 follow-up 决策 1，0.3.17）。只读。

规则同 app.collection_schedule.check_kickoff_placeholder：
- 5DF 12:00 且与竞彩官方时刻不一致 → 用竞彩时刻（kickoff_source=jingcai），mid/close 目标重算；
- 没有竞彩时刻 → 分钟按未知（hour_floor），kickoff_placeholder=5df_1200；
- 竞彩只有整点且 = 12 → 一致（竞彩整点不带自然日，不按合成日期判冲突；date_differs_from_jingcai_synth 另记）。

竞彩时刻来源优先级：match_meta.extras.jingcai_kickoff_at（完整时刻）> --jingcai-json 月度导入 JSON 的
match.kickoff_hour（官方整点，**不受** kickoff 回填同步 kickoff_hour 影响）> matches.kickoff_hour。

导入路径（scripts/fill_kickoff_minutes.py、scripts/import_kickoff_minutes_prod.py）在写 kickoff_at 之前调
`plan_check()`；占位符行 action=hold_kickoff_placeholder，不写。

用法：
  .venv/bin/python scripts/validate_kickoff_placeholder.py --db data/v2d3/app.db \
      --jingcai-json '$ODDS_DATA_DIR/imports/2026/*.json' [--json]
退出码：0 无占位符；1 有占位符（需按规则处理/复核）；2 参数或读库错误。
"""
from __future__ import annotations

import argparse
import glob
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import collection_schedule as cs  # noqa: E402

PLACEHOLDER_STATUSES = ("placeholder_use_jingcai", "placeholder_no_jingcai")


def _dt(v: Any) -> datetime | None:
    if not isinstance(v, str) or not v:
        return None
    try:
        d = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=cs.TZ_CN)


def plan_check(new_kickoff: Any, jingcai_date: str | None, jc_hour: int | None,
               jc_kickoff_at: Any = None) -> dict[str, Any]:
    """导入计划用：返回 check 结果（JSON 可序列化）+ hold（占位符时不写 5DF 值）。"""
    k = new_kickoff if isinstance(new_kickoff, datetime) else _dt(new_kickoff)
    j = jc_kickoff_at if isinstance(jc_kickoff_at, datetime) else _dt(jc_kickoff_at)
    r = cs.check_kickoff_placeholder(k, jingcai_date, jc_hour, j)
    out = {key: (v.isoformat() if isinstance(v, datetime) else v) for key, v in r.items()}
    out["hold"] = r["status"] in PLACEHOLDER_STATUSES
    return out


def load_jingcai_hours(pattern: str | None) -> dict[str, int | None]:
    out: dict[str, int | None] = {}
    for f in sorted(glob.glob(pattern or "")):
        try:
            d = json.loads(Path(f).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for it in d if isinstance(d, list) else []:
            m = it.get("match") if isinstance(it, dict) else None
            if isinstance(m, dict) and m.get("date") and isinstance(m.get("jc"), dict) and m["jc"].get("id"):
                out[f"{m['date']}|{m['jc']['id']}"] = m.get("kickoff_hour")
    return out


def scan(db: Path, jc_hours: dict[str, int | None] | None = None) -> dict[str, Any]:
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    try:
        meta = {r["match_id"]: json.loads(r["extras_json"] or "{}")
                for r in c.execute("SELECT match_id, extras_json FROM match_meta")}
        rows = c.execute("SELECT id, match_uid, jingcai_date, kickoff_hour, kickoff_at FROM matches "
                         "WHERE match_uid NOT LIKE 'probe:%' ORDER BY id").fetchall()
    finally:
        c.close()
    items, counts = [], {}
    for r in rows:
        ex = meta.get(r["id"]) or {}
        uid = r["match_uid"]
        src = "matches.kickoff_hour"
        jc_h = r["kickoff_hour"]
        if jc_hours is not None and uid in jc_hours:
            jc_h, src = jc_hours[uid], "jingcai_json.kickoff_hour"
        if isinstance(ex.get("jingcai_kickoff_hour"), int):
            jc_h, src = ex["jingcai_kickoff_hour"], "meta.jingcai_kickoff_hour"
        jc_at = ex.get("jingcai_kickoff_at")
        if jc_at:
            src = "meta.jingcai_kickoff_at"
        chk = plan_check(r["kickoff_at"], r["jingcai_date"], jc_h, jc_at)
        counts[chk["status"]] = counts.get(chk["status"], 0) + 1
        if chk["status"] != "not_applicable":
            items.append({"match_id": r["id"], "match_uid": uid, "kickoff_at_5df": r["kickoff_at"],
                          "jingcai_time_source": src, **chk})
    return {"db": str(db), "n": len(rows), "counts": counts, "items": items,
            "n_placeholder": sum(1 for i in items if i["hold"])}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, required=True)
    ap.add_argument("--jingcai-json", default=None, help="glob of monthly import JSON (official jingcai hour)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.db.exists():
        print(f"no db {a.db}", file=sys.stderr)
        return 2
    rep = scan(a.db, load_jingcai_hours(a.jingcai_json) if a.jingcai_json else None)
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
    else:
        print(f"{rep['db']}: n={rep['n']} counts={rep['counts']} placeholder={rep['n_placeholder']}")
        for it in rep["items"]:
            print(f"  {it['match_id']} {it['match_uid']} 5df={it['kickoff_at_5df']} jc={it['jingcai_time']} "
                  f"({it['jingcai_time_source']}) -> {it['status']} src={it['kickoff_source']} "
                  f"placeholder={it['kickoff_placeholder']} date_differs={it['date_differs_from_jingcai_synth']}")
    return 1 if rep["n_placeholder"] else 0


if __name__ == "__main__":
    sys.exit(main())
