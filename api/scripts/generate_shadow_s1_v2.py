#!/usr/bin/env python3
"""SHADOW_S1_V2（0316 follow-up 决策 2，0.3.17）：S1 规则不变，特征改用
「符号已校正 + exact_minute」的澳门 odds_snapshot（rule open/mid/close），hist 回补（odds_source=hist）。

- 新 strategy_key=SHADOW_S1_V2 / 新 version；旧 SHADOW_S1 两行冻结不动；不走种子脚本。
- 只删/写 SHADOW_S1_V2 自己的 predictions；拒绝写现网 data/app.db。
- 记法：快照为 API 记法（负 = 主让）；皇冠 open 基准来自 odds_asian（正 = 主让）→ 取反后比较。
- 规则（同 S1，prediction-landing-cards.md）：
  B1 |澳门 open 盘 − 皇冠 open 盘| ≤ S1.line_dev_max；B2 open = mid = close 盘；
  B3 上盘水位 open/mid/close 都在 [S1.upper_water_lo, S1.upper_water_hi]（阈值见 config/strategy_params.json）；B4 上盘可定（平手约定主队）→ 买下盘，按 macau_close 结算。
- 初盘口径：澳门 open = first_tick（usable 恒 true）；皇冠 open = legacy_import，earliest 推定竞彩日 11:10，
  usable_at_close = 11:10 ≤ close target_at（与 /table/matches 同规则），否则跳过并计数。
- as-of：mid/close 快照 recorded_at ≤ target_at < 开赛；open recorded_at ≤ close target_at；否则跳过。
用法：.venv/bin/python scripts/generate_shadow_s1_v2.py --db data/v2d3/app.db [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import connect  # noqa: E402
from app.strategy_params import get as _sp  # noqa: E402  公开仓：调优参数从 config 读取
from app.collection_schedule import EXCEPTION_RULE  # noqa: E402
from app.main import config_fingerprint, normalize_strategy_config, parse_iso_dt, resolve_kickoff_dt, loads_json  # noqa: E402

KEY = "SHADOW_S1_V2"
LEDGER = "S1-V2"
VERSION = "2026.10.08-shadow-s1v2-signfix-exact-minute-hist"
FROZEN_KEY = "SHADOW_S1"
LIVE = (ROOT / "data" / "app.db").resolve()
TZ = timezone(timedelta(hours=8))
UNAVAILABLE = frozenset({38, 43})


def upper_side_api(line: float) -> str:
    """API 记法：负 = 主让 → 主上盘；正 → 客上盘；平手 → 主（S1 B4 约定）。"""
    return "主" if line <= 0 else "客"


def upper_water(line: float, wh, wa):
    w = wh if upper_side_api(line) == "主" else wa
    return None if w is None else float(w)


def _dt(v):
    return parse_iso_dt(v) if isinstance(v, str) and v else None


def generate(conn) -> tuple[list[dict], dict]:
    snaps: dict[tuple[int, str], Any] = {}
    for r in conn.execute("SELECT * FROM odds_snapshot WHERE market='asian' AND channel='rule' AND book='macau'"):
        snaps[(r["match_id"], r["point"])] = r
    crown_open = {r["match_id"]: r for r in conn.execute(
        "SELECT * FROM odds_asian WHERE book='crown' AND phase='open'")}
    meta = {r["match_id"]: loads_json(r["extras_json"]) or {}
            for r in conn.execute("SELECT match_id, extras_json FROM match_meta")}
    stats = {k: 0 for k in ("matches", "skip_unavailable", "missing_feature", "not_exact_minute", "asof_violation",
                            "crown_open_unusable", "feature_ready", "fail_b1", "fail_b2", "fail_b3", "trigger")}
    rows = []
    cols = {r[1] for r in conn.execute("PRAGMA table_info(matches)")}
    mr = " AND COALESCE(manual_review, 0) = 0" if "manual_review" in cols else ""  # 0.3.19：人工复核场不进特征
    for m in conn.execute(f"SELECT * FROM matches WHERE match_uid NOT LIKE 'probe:%'{mr} ORDER BY id"):
        mid = m["id"]
        stats["matches"] += 1
        if mid in UNAVAILABLE:
            stats["skip_unavailable"] += 1
            continue
        so, sm, sc, co = snaps.get((mid, "open")), snaps.get((mid, "mid")), snaps.get((mid, "close")), crown_open.get(mid)
        if not (so and sm and sc and co) or co["handicap"] is None or any(
                s["line"] is None or s["water_home"] is None or s["water_away"] is None for s in (so, sm, sc)):
            stats["missing_feature"] += 1
            continue
        exm, exc = json.loads(sm["extras_json"] or "{}"), json.loads(sc["extras_json"] or "{}")
        if exm.get("phase_target") != "exact_minute" or exc.get("phase_target") != "exact_minute":
            stats["not_exact_minute"] += 1
            continue
        kick = resolve_kickoff_dt(m, meta.get(mid, {}))
        t_mid, t_close = _dt(sm["target_at"]), _dt(sc["target_at"])
        r_open, r_mid, r_close = _dt(so["recorded_at"]), _dt(sm["recorded_at"]), _dt(sc["recorded_at"])
        if not (kick and t_mid and t_close and r_open and r_mid and r_close) or not (
                r_mid <= t_mid and r_close <= t_close and t_close < kick and r_open <= t_close):
            stats["asof_violation"] += 1
            continue
        earliest_crown = datetime.fromisoformat(m["jingcai_date"]).replace(hour=11, minute=10, tzinfo=TZ)
        if not earliest_crown <= t_close:
            stats["crown_open_unusable"] += 1
            continue
        stats["feature_ready"] += 1
        lo, lm, lc = float(so["line"]), float(sm["line"]), float(sc["line"])
        baseline = -float(co["handicap"]) if co["handicap"] else 0.0  # odds_asian 正=主让 → API 记法
        wo, wm, wc = (upper_water(lo, so["water_home"], so["water_away"]),
                      upper_water(lm, sm["water_home"], sm["water_away"]),
                      upper_water(lc, sc["water_home"], sc["water_away"]))
        if not abs(lo - baseline) <= _sp("S1", "line_dev_max"):
            stats["fail_b1"] += 1
            continue
        if not (lo == lm == lc):
            stats["fail_b2"] += 1
            continue
        if not all(w is not None and _sp("S1", "upper_water_lo") <= w <= _sp("S1", "upper_water_hi") for w in (wo, wm, wc)):
            stats["fail_b3"] += 1
            continue
        stats["trigger"] += 1
        u = upper_side_api(lc)
        lower = "客" if u == "主" else "主"
        snap = {
            "ledger_id": LEDGER, "strategy_version": VERSION,
            "config_fingerprint": f"shadow-s1v2-{VERSION}",
            "odds_source": "hist", "tick_age_rule": "hist_in_effect", "decision_phase": "close",
            "line_convention": "api_negative_home_gives", "sign_corrected": True,
            "phase_target": "exact_minute",
            "exception_rule": EXCEPTION_RULE,  # 0.3.18：例外场按竞彩编号判
            "settle_odds_synced_to_snapshot": "resync_0318",  # 0.3.18 D：结算 odds_asian = 特征快照同一盘
            "baseline_book": "crown", "baseline_phase": "open", "baseline_line_api": baseline,
            "baseline_open_basis": "legacy_import", "baseline_earliest_ts_quote_at": earliest_crown.isoformat(),
            "baseline_ts_inferred": True, "baseline_usable_at_close": True,
            "feature_book": "macau", "water_src": "actual", "source": "odds_snapshot/rule (5df_macauslot_history)",
            "open_basis": "first_tick", "open_features_used": ["open_line", "open_odds"],
            "usable_at_mid": True, "usable_at_close": True,
            "macau_open_line": lo, "macau_mid_line": lm, "macau_close_line": lc,
            "macau_open_recorded_at": so["recorded_at"], "macau_mid_target_at": sm["target_at"],
            "macau_close_target_at": sc["target_at"],
            "upper_water_open": wo, "upper_water_mid": wm, "upper_water_close": wc,
            "B1": True, "B2": True, "B3": True, "B4": True,
            "decided_channel": "rule", "decided_point": "close", "branch": "steady_lower",
            "upper": u, "side": "AH_away" if lower == "客" else "AH_home",
            "settle_book": "macau_close", "test_only": True,
        }
        rationale = [f"ledger_id={LEDGER}", "branch=steady_lower", "landing=prediction-landing-cards.md",
                     f"version={VERSION}", {"feature_snapshot": snap}]
        rows.append({"match_id": mid, "direction": lower, "rationale_json": json.dumps(rationale, ensure_ascii=False)})
    return rows, stats


def upsert_def(conn, stats: dict) -> int:
    src = conn.execute("SELECT * FROM strategy_defs WHERE strategy_key=? ORDER BY id DESC LIMIT 1",
                       (FROZEN_KEY,)).fetchone()
    cfg = normalize_strategy_config(json.loads(src["config_json"] or "{}")) if src else normalize_strategy_config({})
    ex = dict(cfg.get("extras") or {})
    ex.update({"ledger_id": LEDGER, "test_only": True, "predictions": "ready", "blocked_reason": None,
               "source": "odds_snapshot rule (5df_macauslot_history), exact_minute",
               "line_convention": "api_negative_home_gives", "sign_corrected": True, "phase_target": "exact_minute",
               "odds_source": "hist", "derived_from": f"{FROZEN_KEY} (frozen, untouched)",
               "subset": f"{stats['feature_ready']}_of_{stats['matches']}",
               "approx_rate": None, "branches": ["steady_lower"], "no_trigger_no_row": True,
               "baseline_open_usable_rule": "crown legacy_import open: 竞彩日 11:10 <= close target_at",
               "exception_rule": EXCEPTION_RULE,
               "settle_odds_synced_to_snapshot": "resync_0318 (macau open/mid/close odds_asian = exact_minute snapshot)"})
    cfg["extras"] = ex
    cfg = normalize_strategy_config(cfg)
    fp = config_fingerprint(cfg)
    notes = f"影子 {LEDGER} · 符号校正+exact_minute 快照 · hist · {VERSION}（不走种子脚本）"
    row = conn.execute("SELECT id FROM strategy_defs WHERE strategy_key=? AND version=?", (KEY, VERSION)).fetchone()
    if row:
        conn.execute("UPDATE strategy_defs SET config_json=?, config_fingerprint=?, notes=?, status='shadow', "
                     "is_default=0, updated_at=datetime('now') WHERE id=?",
                     (json.dumps(cfg, ensure_ascii=False), fp, notes, row["id"]))
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO strategy_defs (strategy_key, version, display_name, markets_json, config_json, "
        "config_fingerprint, status, is_default, notes) VALUES (?,?,?,?,?,?, 'shadow', 0, ?)",
        (KEY, VERSION, "影子 S1-V2（三段不动买下盘·校正版）", src["markets_json"] if src else '["ah"]',
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
        frozen_before = conn.execute("SELECT COUNT(*), group_concat(id) FROM predictions WHERE strategy=?",
                                     (FROZEN_KEY,)).fetchone()
        rows, stats = generate(conn)
        rep: dict[str, Any] = {"version": VERSION, "stats": stats, "rows": [
            {"match_id": r["match_id"], "direction": r["direction"]} for r in rows], "dry_run": a.dry_run}
        if not a.dry_run:
            sid = upsert_def(conn, stats)
            conn.execute("DELETE FROM predictions WHERE strategy=?", (KEY,))
            for r in rows:
                conn.execute("INSERT INTO predictions (match_id, strategy, direction, settle_book, rationale_json, "
                             "stake, stake_rule) VALUES (?,?,?,?,?,1,'shadow_landing_v0')",
                             (r["match_id"], KEY, r["direction"], "macau_close", r["rationale_json"]))
            frozen_after = conn.execute("SELECT COUNT(*), group_concat(id) FROM predictions WHERE strategy=?",
                                        (FROZEN_KEY,)).fetchone()
            if tuple(frozen_after) != tuple(frozen_before):
                conn.rollback()
                raise SystemExit("frozen SHADOW_S1 rows changed — rolled back")
            conn.commit()
            rep["def_id"] = sid
        rep["frozen_s1"] = {"count": frozen_before[0], "ids": frozen_before[1]}
        print(json.dumps(rep, ensure_ascii=False, indent=1))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
