#!/usr/bin/env python3
"""SHADOW_S2_V2 / SHADOW_N4_V2（决议「0.3.18 的五件事」3，0.3.19）：输入口径变了 → 新规则新 key。

- 规则与旧 S2 / N4 相同（prediction-landing-cards.md；gen_s2 / gen_n4 原样复用），只认真实水位：
  档位换算（water_src=tier_midpoint）水位置空 → 依赖水位的分支不触发（S2 b 分支、N4 全部）。
- odds_source=hist（5DF 历史回补；澳门 open/close 已在 0.3.18 D 与 exact_minute 快照同步）。
- 人工复核场（matches.manual_review=1）、探针场不进特征（match_ids 已排除）。
- 旧 SHADOW_S2 / SHADOW_N4 冻结：本脚本只删/写 *_V2 自己的行，写前后核对旧 key 行数不变。
- 不走种子脚本；拒绝写现网 data/app.db。N4-V2 现在 0 行（只挂定义），等前向采集写入真实水位后再积累。
用法：.venv/bin/python scripts/generate_shadow_s2n4_v2.py --db data/v2d3/app.db [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app.db import connect  # noqa: E402
from app.collection_schedule import EXCEPTION_RULE, POSTPONE_VOID_HOURS  # noqa: E402
from app.ledger_registry import FROZEN_NO_APPEND, V2_WATER_RULE  # noqa: E402
from app.main import config_fingerprint, normalize_strategy_config  # noqa: E402
import generate_shadow_predictions as g  # noqa: E402

VERSION = "2026.10.08-shadow-v2-real-water-hist"
LIVE = (ROOT / "data" / "app.db").resolve()
SPECS = {
    "SHADOW_S2_V2": {"ledger": "S2-V2", "old": "SHADOW_S2", "gen": "gen_s2", "mutex_bucket": "B",
                     "branches": ["a_low_open_rise", "b_high_open_rise_water"],
                     "display": "影子 S2-V2（真实水位口径）"},
    "SHADOW_N4_V2": {"ledger": "N4-V2", "old": "SHADOW_N4", "gen": "gen_n4", "mutex_bucket": "A",
                     "branches": ["deep", "shallow"], "display": "影子 N4-V2（真实水位口径）"},
}
assert {v["old"]: k for k, v in SPECS.items()} == FROZEN_NO_APPEND


def build(conn) -> dict[str, Any]:
    g.TIER_WATER_EXCLUDED["rows"] = 0
    asian = g.load_asian(conn, real_water_only=True)
    mids = g.match_ids(conn)
    out: dict[str, Any] = {"tier_water_rows_excluded": g.TIER_WATER_EXCLUDED["rows"], "n_matches": len(mids)}
    for key, sp in SPECS.items():
        rows, st = getattr(g, sp["gen"])(asian, mids)
        for r in rows:
            rat = json.loads(r["rationale_json"])
            snap = next(x["feature_snapshot"] for x in rat if isinstance(x, dict) and "feature_snapshot" in x)
            snap.update({"ledger_id": sp["ledger"], "strategy_version": VERSION,
                         "config_fingerprint": f"shadow-{sp['ledger'].lower()}-{VERSION}",
                         "odds_source": "hist", "tick_age_rule": "hist_in_effect",
                         "water_rule": V2_WATER_RULE, "derived_from": f"{sp['old']} (frozen)",
                         "exception_rule": EXCEPTION_RULE, "settle_odds_synced_to_snapshot": "resync_0318"})
            rat = [f"ledger_id={sp['ledger']}" if (isinstance(x, str) and x.startswith("ledger_id=")) else x
                   for x in rat]
            rat.insert(3, f"version={VERSION}")
            r.update({"strategy": key, "rationale_json": json.dumps(rat, ensure_ascii=False)})
            r.pop("snap", None)
        out[key] = {"rows": rows, "stats": st}
    return out


def upsert_def(conn, key: str, sp: dict, stats: dict) -> int:
    src = conn.execute("SELECT * FROM strategy_defs WHERE strategy_key=? ORDER BY id DESC LIMIT 1",
                       (sp["old"],)).fetchone()
    cfg = normalize_strategy_config(json.loads(src["config_json"] or "{}")) if src else normalize_strategy_config({})
    ex = dict(cfg.get("extras") or {})
    ex.update({"ledger_id": sp["ledger"], "test_only": True, "test_note": "仅影子对照，不进日用白名单",
               "predictions": "ready", "blocked_reason": None,
               "landing_doc": g.LANDING_DOC, "baseline_book": g.BASELINE_BOOK, "baseline_phase": "open",
               "feature_book": g.FEATURE_WATER_BOOK, "feature_water_book": g.FEATURE_WATER_BOOK,
               "water_rule": V2_WATER_RULE, "odds_source": "hist",
               "derived_from": f"{sp['old']} (frozen；旧 key 禁止追加；旧条目「触发依据是换算水位」不和本版比)",
               "mutex_bucket": sp["mutex_bucket"], "branches": sp["branches"], "no_trigger_no_row": True,
               "exception_rule": EXCEPTION_RULE,
               "settle_odds_synced_to_snapshot": "resync_0318",
               "postpone_void_hours": POSTPONE_VOID_HOURS,
               "eligible": stats.get("eligible")})
    cfg["extras"] = ex
    cfg = normalize_strategy_config(cfg)
    fp = config_fingerprint(cfg)
    notes = f"影子 {sp['ledger']} · 真实水位口径 · hist · {VERSION}（不走种子脚本）"
    row = conn.execute("SELECT id FROM strategy_defs WHERE strategy_key=? AND version=?", (key, VERSION)).fetchone()
    if row:
        conn.execute("UPDATE strategy_defs SET config_json=?, config_fingerprint=?, notes=?, status='shadow', "
                     "is_default=0, updated_at=datetime('now') WHERE id=?",
                     (json.dumps(cfg, ensure_ascii=False), fp, notes, row["id"]))
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO strategy_defs (strategy_key, version, display_name, markets_json, config_json, "
        "config_fingerprint, status, is_default, notes) VALUES (?,?,?,?,?,?, 'shadow', 0, ?)",
        (key, VERSION, sp["display"], src["markets_json"] if src else '["ah"]',
         json.dumps(cfg, ensure_ascii=False), fp, notes))
    return int(cur.lastrowid)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, required=True)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.db.resolve() == LIVE:
        print("REFUSE: live app.db is never written", file=sys.stderr)
        return 2
    conn = connect(a.db)
    try:
        def frozen():
            return {sp["old"]: tuple(conn.execute("SELECT COUNT(*), group_concat(id) FROM predictions WHERE strategy=?",
                                                  (sp["old"],)).fetchone()) for sp in SPECS.values()}
        before = frozen()
        res = build(conn)
        rep: dict[str, Any] = {"version": VERSION, "dry_run": a.dry_run,
                               "tier_water_rows_excluded": res["tier_water_rows_excluded"],
                               "n_matches": res["n_matches"], "strategies": {}}
        for key, sp in SPECS.items():
            rows = res[key]["rows"]
            rep["strategies"][key] = {"stats": res[key]["stats"], "n_rows": len(rows),
                                      "rows": [{"match_id": r["match_id"], "direction": r["direction"],
                                                "branch": json.loads(r["rationale_json"])[1]} for r in rows]}
            if not a.dry_run:
                rep["strategies"][key]["def_id"] = upsert_def(conn, key, sp, res[key]["stats"])
                conn.execute("DELETE FROM predictions WHERE strategy=?", (key,))
                for r in rows:
                    conn.execute("INSERT INTO predictions (match_id, strategy, direction, settle_book, rationale_json, "
                                 "stake, stake_rule) VALUES (?,?,?,?,?,?,?)",
                                 (r["match_id"], key, r["direction"], r["settle_book"], r["rationale_json"],
                                  r["stake"], r["stake_rule"]))
        after = frozen()
        if after != before:
            conn.rollback()
            raise SystemExit(f"frozen S2/N4 rows changed — rolled back: {before} → {after}")
        if not a.dry_run:
            conn.commit()
        rep["frozen_old"] = {k: {"count": v[0], "ids": v[1]} for k, v in before.items()}
        print(json.dumps(rep, ensure_ascii=False, indent=1))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
