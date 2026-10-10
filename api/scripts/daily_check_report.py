#!/usr/bin/env python3
"""0.3.19 日核对清单（只读）：按 /table/matches 同一口径数各原因的场次，含 instant_src_diff（即时 11:10 两路来源不一致）；
0.3.20 起另列 own_1110_out_of_window（自采超出 [11:00, 11:20]）。返还率「偏高」不是告警，不进本清单。

用法：.venv/bin/python scripts/daily_check_report.py --db data/v2d3/app.db --from 2026-06-01 --to 2026-10-08 [--json out.json]
只以 mode=ro 打开库，不写任何表。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import table_matches as tm  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "v2d3" / "app.db"))
    ap.add_argument("--from", dest="date_from", required=True)
    ap.add_argument("--to", dest="date_to", required=True)
    ap.add_argument("--scope", default="all")
    ap.add_argument("--as-of", default=None)
    ap.add_argument("--json", default=None)
    a = ap.parse_args()
    conn = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    out = tm.build_table(conn, date_from=a.date_from, date_to=a.date_to, scope=a.scope, strategy="none",
                         channel="rule", include_live="none", settlement_version=None, as_of=a.as_of,
                         baseline_window_days=None, baseline_min_n=5, limit=100000, offset=0)
    items = out["items"]
    summ = tm.daily_check_summary(items)
    summ["scope"] = "all_rows"
    summ["n_rows"] = len(items)
    summ["instant_src_diff_detail"] = [
        {"match_id": it["match_id"], "book": e["book"], "market": e["market"], "line": e["line"],
         "captured_at": e["captured_at"], "alt": e["alt"]}
        for it in items for e in it["live"] if e.get("instant_src_diff")]
    # 0.3.20：自采超出 [11:00, 11:20] 的格子（主值已退回时间线，自采值在 alt）
    summ["own_1110_out_of_window_detail"] = [
        {"match_id": it["match_id"], "book": e["book"], "market": e["market"],
         "main_origin": e.get("origin"), "main_source_reason": e.get("main_source_reason"),
         "own_captured_at": (e.get("alt") or {}).get("captured_at"),
         "own_fetch_lag_min": (e.get("alt") or {}).get("fetch_lag_min"), "alt": e.get("alt")}
        for it in items for e in it["live"] if e.get("own_capture_out_of_window")]
    summ["by_reason_matches"] = {k: [it["match_id"] for it in items if k in (it["match"].get("daily_check") or [])]
                                 for k in summ["by_reason"]}
    txt = json.dumps(summ, ensure_ascii=False, indent=1)
    if a.json:
        Path(a.json).write_text(txt, encoding="utf-8")
    print(txt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
