"""v1.7 迁移：odds_asian 里 crown/william 的水位档位 → 中点水位（幂等，可回退）。

用法：
  .venv/bin/python scripts/migrate_v1_7_water.py --dry-run   # 只统计
  .venv/bin/python scripts/migrate_v1_7_water.py             # 执行（重复执行 0 行）
  .venv/bin/python scripts/migrate_v1_7_water.py --rollback  # 用 extras_json.water_tier_raw 还原档位

只处理 book ∈ {crown, william} 且 water_src IS NULL 且有水位的行；macau 不动。
odds_raw.odds_json 是导入时的原始 JSON 整包，保持不动（作为审计 / 回退源）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import SCHEMA_DIR, apply_sql_idempotent, connect  # noqa: E402
from app.water import convert_tier_pair  # noqa: E402

SQL = SCHEMA_DIR / "v1_7_water_tier_midpoint.sql"
TIER_BOOKS = ("crown", "william")


def stats(conn) -> dict:
    out = {}
    for r in conn.execute(
        """SELECT book, COALESCE(water_src,'(null)') AS src, COUNT(*) AS rows,
                  SUM(COALESCE(water_censored,0)) AS censored_rows,
                  SUM(CASE WHEN json_extract(extras_json,'$.censored.home')=1 THEN 1 ELSE 0 END)
                + SUM(CASE WHEN json_extract(extras_json,'$.censored.away')=1 THEN 1 ELSE 0 END) AS censored_sides
           FROM odds_asian GROUP BY book, water_src ORDER BY book"""
    ):
        out[f"{r['book']}:{r['src']}"] = {"rows": r["rows"], "censored_rows": r["censored_rows"],
                                          "censored_sides": r["censored_sides"]}
    return out


def migrate(conn, dry_run: bool) -> int:
    apply_sql_idempotent(conn, SQL)
    rows = conn.execute(
        f"""SELECT id, home_water, away_water FROM odds_asian
            WHERE book IN ({','.join('?' * len(TIER_BOOKS))}) AND water_src IS NULL
              AND (home_water IS NOT NULL OR away_water IS NOT NULL)""",
        TIER_BOOKS,
    ).fetchall()
    if dry_run:
        conn.rollback()
        return len(rows)
    for r in rows:
        cv = convert_tier_pair(r["home_water"], r["away_water"])
        conn.execute(
            "UPDATE odds_asian SET home_water=?, away_water=?, water_src=?, water_censored=?, extras_json=?"
            " WHERE id=? AND water_src IS NULL",
            (cv["home_water"], cv["away_water"], cv["water_src"], cv["water_censored"],
             json.dumps(cv["extras"], ensure_ascii=False), r["id"]),
        )
    conn.commit()
    return len(rows)


def rollback(conn) -> int:
    rows = conn.execute(
        "SELECT id, extras_json FROM odds_asian WHERE water_src='tier_midpoint'"
    ).fetchall()
    for r in rows:
        raw = (json.loads(r["extras_json"] or "{}").get("water_tier_raw") or {})
        conn.execute(
            "UPDATE odds_asian SET home_water=?, away_water=?, water_src=NULL, water_censored=NULL,"
            " extras_json=NULL WHERE id=?",
            (raw.get("home"), raw.get("away"), r["id"]),
        )
    conn.commit()
    return len(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rollback", action="store_true")
    ap.add_argument("--db", default=None)
    a = ap.parse_args()
    conn = connect(Path(a.db) if a.db else None)
    if a.rollback:
        n = rollback(conn)
        print(json.dumps({"rolled_back_rows": n, "stats": stats(conn)}, ensure_ascii=False, indent=1))
        return
    n = migrate(conn, a.dry_run)
    print(json.dumps({"dry_run": a.dry_run, "converted_rows": n, "stats": stats(conn) if not a.dry_run else None},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
