#!/usr/bin/env python3
"""CFFXDJ_5_V3 自检骨架：对照本地 DB 与（可选）本机 API。

用法:
  python scripts/check_cffxdj5v3_smoke.py
  python scripts/check_cffxdj5v3_smoke.py --api http://127.0.0.1:8787
  python scripts/check_cffxdj5v3_smoke.py --expect-min 30

不修改数据；失败以非 0 退出，便于 CI / 里程碑验收。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "app.db"
STRATEGY = "CFFXDJ_5_V3"


def get_json(url: str) -> dict | list | None:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as e:
        print(f"[warn] API {url}: {e}")
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description="CFFXDJ_5_V3 smoke self-check")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--api", default="http://127.0.0.1:8787", help="API base; empty to skip")
    ap.add_argument("--expect-min", type=int, default=1, help="min prediction rows for strategy")
    ap.add_argument("--sample-day", default=None, help="optional jingcai_date YYYY-MM-DD to sample")
    args = ap.parse_args()

    errors: list[str] = []
    if not args.db.exists():
        print(f"[fail] DB missing: {args.db}")
        return 2

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row

    n = con.execute(
        "SELECT COUNT(*) c FROM predictions WHERE strategy=?", (STRATEGY,)
    ).fetchone()["c"]
    print(f"[db] {STRATEGY} predictions: {n}")
    if n < args.expect_min:
        errors.append(f"predictions count {n} < expect-min {args.expect_min}")

    dirs = Counter(
        r["direction"]
        for r in con.execute(
            "SELECT direction FROM predictions WHERE strategy=?", (STRATEGY,)
        )
    )
    print(f"[db] direction breakdown: {dict(dirs)}")

    by_day = list(
        con.execute(
            """
            SELECT m.jingcai_date AS d, COUNT(*) c,
                   SUM(CASE WHEN p.direction!='不下注' THEN 1 ELSE 0 END) AS active
            FROM predictions p
            JOIN matches m ON m.id = p.match_id
            WHERE p.strategy=?
            GROUP BY 1 ORDER BY 1
            """,
            (STRATEGY,),
        )
    )
    print(f"[db] days covered: {len(by_day)}")
    for r in by_day[:12]:
        print(f"  {r['d']}: n={r['c']} active={r['active']}")
    if len(by_day) > 12:
        print(f"  ... ({len(by_day) - 12} more days)")

    sample_day = args.sample_day or (by_day[0]["d"] if by_day else None)
    sample_ids: list[int] = []
    if sample_day:
        rows = list(
            con.execute(
                """
                SELECT p.match_id, p.direction, m.jc_id, m.home_team, m.away_team
                FROM predictions p
                JOIN matches m ON m.id = p.match_id
                WHERE p.strategy=? AND m.jingcai_date=?
                ORDER BY m.jc_no
                """,
                (STRATEGY, sample_day),
            )
        )
        print(f"[db] sample day {sample_day}: {len(rows)} rows")
        for r in rows[:8]:
            print(
                f"  match_id={r['match_id']} {r['jc_id']} "
                f"{r['home_team']}-{r['away_team']} → {r['direction']}"
            )
            sample_ids.append(int(r["match_id"]))
        if not rows:
            errors.append(f"no predictions on sample day {sample_day}")

    # API checks (optional)
    if args.api:
        health = get_json(f"{args.api.rstrip('/')}/health")
        if not health or not health.get("ok"):
            errors.append("API /health not ok or unreachable")
            print(f"[api] health={health}")
        else:
            print(f"[api] health ok version={health.get('version')}")

        for mid in sample_ids[:3]:
            pred = get_json(f"{args.api.rstrip('/')}/matches/{mid}/prediction")
            if not pred:
                errors.append(f"API /matches/{mid}/prediction failed")
                continue
            if pred.get("strategy") != STRATEGY:
                errors.append(
                    f"match {mid} API strategy {pred.get('strategy')!r} != {STRATEGY}"
                )
            db_dir = con.execute(
                "SELECT direction FROM predictions WHERE match_id=? AND strategy=?",
                (mid, STRATEGY),
            ).fetchone()
            if db_dir and pred.get("direction") != db_dir["direction"]:
                errors.append(
                    "match %s API direction %r != DB %r"
                    % (mid, pred.get("direction"), db_dir["direction"])
                )
            else:
                print(
                    f"[api] match {mid} direction={pred.get('direction')} "
                    f"aligned with DB"
                )

    con.close()

    if errors:
        print("[fail]")
        for e in errors:
            print(" -", e)
        return 1
    print("[ok] CFFXDJ_5_V3 smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
