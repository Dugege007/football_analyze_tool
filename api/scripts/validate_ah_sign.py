#!/usr/bin/env python3
"""亚盘盘口正负号校验（odds_asian 记法：正数 = 主让）。2026-10-08 §12 第 5 条 + 算法顾问补充。

规则（术语文档「目标时刻与几处默认值」5 + 「补充」）：
  - 单场：同一场、同一家公司，被检阶段（默认 mid）的正负号跟 open、close **都相反**，
    且 |被检| == |open| 或 |被检| == |close| → 拦下人工复核（review），不直接入库。
  - 平手（盘口线 = 0）不参与判断：被检、open、close 任一为 0 的场不计入分母、也不判。
  - 整批：按「来源 × 阶段」分组，「正负号跟 open、close 都相反」的场次（只看正负号）
    超过该批非平手场次的 20% → 整批拦下（block），不逐场复核。

退出码：0 = 通过；1 = 有单场待人工复核（无整批拦截）；2 = 有整批拦截。

用法：
  .venv/bin/python scripts/validate_ah_sign.py --db data/v2d3/app.db
  .venv/bin/python scripts/validate_ah_sign.py --db data/v2d3/app.db --book macau --phase mid --json out.json

也可在导入/同步脚本里调用：
  from validate_ah_sign import validate_conn
  rep = validate_conn(conn)          # 同一事务内可见未提交的行
  if rep["blocked"] or rep["review"]: conn.rollback(); raise SystemExit(...)
只读：本脚本不写库（连接用 mode=ro；validate_conn 只 SELECT）。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterable

BATCH_BLOCK_RATIO = 0.20
PHASES = ("open", "mid", "close")


def _sign(v: float) -> int:
    return (v > 0) - (v < 0)


def _source_of(extras_json: Any) -> str:
    if not extras_json:
        return "legacy_import"
    try:
        ex = json.loads(extras_json)
    except (TypeError, ValueError):
        return "unknown"
    return str(ex.get("source") or "legacy_import") if isinstance(ex, dict) else "unknown"


def check_rows(rows: Iterable[dict], *, phase: str = "mid",
               block_ratio: float = BATCH_BLOCK_RATIO) -> dict:
    """rows: dict(match_id, book, phase, handicap, extras_json[, match_uid])。返回报告 dict。"""
    if phase not in PHASES:
        raise ValueError(f"phase must be one of {PHASES}")
    others = tuple(p for p in PHASES if p != phase)
    by_mb: dict[tuple, dict[str, dict]] = {}
    for r in rows:
        by_mb.setdefault((r["match_id"], r["book"]), {})[r["phase"]] = r
    batches: dict[tuple, dict] = {}
    review: list[dict] = []
    for (mid, book), ph in sorted(by_mb.items()):
        x = ph.get(phase)
        if x is None or x["handicap"] is None:
            continue
        src = _source_of(x.get("extras_json"))
        key = (book, src, phase)
        bt = batches.setdefault(key, {"book": book, "source": src, "phase": phase, "rows": 0,
                                      "non_flat": 0, "flat_or_missing_skipped": 0,
                                      "opposite_both": 0, "review": 0})
        bt["rows"] += 1
        a, b = ph.get(others[0]), ph.get(others[1])
        vals = [x["handicap"], a["handicap"] if a else None, b["handicap"] if b else None]
        if any(v is None for v in vals) or any(float(v) == 0 for v in vals):
            bt["flat_or_missing_skipped"] += 1
            continue
        hx, ha, hb = (float(v) for v in vals)
        bt["non_flat"] += 1
        if _sign(hx) != _sign(ha) and _sign(hx) != _sign(hb):
            bt["opposite_both"] += 1
            if abs(hx) == abs(ha) or abs(hx) == abs(hb):
                bt["review"] += 1
                review.append({"match_id": mid, "match_uid": x.get("match_uid"), "book": book,
                               "source": src, phase: hx, others[0]: ha, others[1]: hb})
    blocked = []
    for bt in batches.values():
        bt["opposite_ratio"] = round(bt["opposite_both"] / bt["non_flat"], 4) if bt["non_flat"] else 0.0
        bt["blocked"] = bool(bt["non_flat"]) and bt["opposite_both"] / bt["non_flat"] > block_ratio
        if bt["blocked"]:
            blocked.append(f"{bt['book']}|{bt['source']}|{bt['phase']}")
    # 被整批拦下的批次不再逐场复核
    review_open = [r for r in review if f"{r['book']}|{r['source']}|{phase}" not in blocked]
    status = "block" if blocked else ("review" if review_open else "pass")
    return {"rule": "sign(phase) opposite to BOTH others & |phase| equals one of them → review; "
                    f"batch (book×source×phase) opposite_both/non_flat > {block_ratio:.0%} → block; flat(0) skipped",
            "phase": phase, "status": status, "exit_code": {"pass": 0, "review": 1, "block": 2}[status],
            "blocked": blocked, "review": review_open, "review_in_blocked_batches": len(review) - len(review_open),
            "batches": sorted(batches.values(), key=lambda b: (b["book"], b["source"]))}


def load_rows(conn: sqlite3.Connection, *, book: str | None = None,
              include_probe: bool = False) -> list[dict]:
    q = ("SELECT oa.match_id, oa.book, oa.phase, oa.handicap, oa.extras_json, m.match_uid"
         " FROM odds_asian oa JOIN matches m ON m.id = oa.match_id WHERE oa.phase IN ('open','mid','close')")
    args: list[Any] = []
    if not include_probe:
        q += " AND m.match_uid NOT LIKE 'probe:%'"
    if book:
        q += " AND oa.book = ?"
        args.append(book)
    cur = conn.execute(q, args)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def validate_conn(conn: sqlite3.Connection, *, book: str | None = None, phase: str = "mid",
                  include_probe: bool = False, block_ratio: float = BATCH_BLOCK_RATIO) -> dict:
    return check_rows(load_rows(conn, book=book, include_probe=include_probe),
                      phase=phase, block_ratio=block_ratio)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True)
    ap.add_argument("--book", default=None, help="只查某家（默认全部）")
    ap.add_argument("--phase", default="mid", choices=PHASES)
    ap.add_argument("--include-probe", action="store_true")
    ap.add_argument("--block-ratio", type=float, default=BATCH_BLOCK_RATIO)
    ap.add_argument("--json", default=None, help="报告写到该路径")
    a = ap.parse_args(argv)
    conn = sqlite3.connect(f"file:{Path(a.db).resolve()}?mode=ro", uri=True)
    try:
        rep = validate_conn(conn, book=a.book, phase=a.phase, include_probe=a.include_probe,
                            block_ratio=a.block_ratio)
    finally:
        conn.close()
    rep["db"] = str(Path(a.db).resolve())
    if a.json:
        Path(a.json).write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    brief = {k: v for k, v in rep.items() if k != "review"}
    brief["review_count"] = len(rep["review"])
    brief["review_sample"] = rep["review"][:5]
    print(json.dumps(brief, ensure_ascii=False, indent=2))
    return rep["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
