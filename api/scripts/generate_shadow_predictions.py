#!/usr/bin/env python3
"""按 prediction-landing-cards.md 为 SHADOW_S1 / S2 / N1 / N4 / S8 挂 predictions（幂等）。

- 只删/改本 strategy 的 predictions 行
- 不改 V3 / TEST_V3_MIRROR；不写 odds_asian
- S1：澳门 open/mid/close 真实水位；不触发不写行；建议只写副本（如 data/v2d3）
- N1：威廉完整 1X2 + 澳门 open 上盘顺分布；不触发不写行；建议只写副本 v2d3
- S8：凡可评判场都写行（direction=不下注）；skip_v3=true 计为 filter hit

用法：
  .venv/bin/python scripts/generate_shadow_predictions.py
  .venv/bin/python scripts/generate_shadow_predictions.py --only S2,N4,S8 --db data/app.db
  .venv/bin/python scripts/generate_shadow_predictions.py --only S1 --db data/v2d3/app.db
  .venv/bin/python scripts/generate_shadow_predictions.py --only N1 --db data/v2d3/app.db
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import connect  # noqa: E402
from app.main import config_fingerprint, normalize_strategy_config  # noqa: E402
from app.ledger_registry import assert_appendable  # noqa: E402
from app.strategy_params import get as _sp  # noqa: E402  公开仓：调优参数从 config 读取

LANDING_DOC = "prediction-landing-cards.md"
VERSION = "2026.10.06-shadow-landing"
VERSION_S1 = "2026.10.07-shadow-s1-macau-actual-175"
VERSION_N1 = "2026.10.07-shadow-n1-william-1x2-175"
BASELINE_BOOK = "crown"
FEATURE_WATER_BOOK = "crown"
FEATURE_BOOK_S1 = "macau"
FEATURE_1X2_BOOK = "william"
# N1 阈值：公开仓不内置，见 config/strategy_params.json · N1（升 version 才可改）
UNAVAILABLE_MACAU_AH = frozenset({38, 43})  # macau_ah_unavailable / euro_1x2_unavailable
V3 = "CFFXDJ_5_V3"

STRATEGIES = {
    "S1": "SHADOW_S1",
    "S2": "SHADOW_S2",
    "N1": "SHADOW_N1",
    "N4": "SHADOW_N4",
    "S8": "SHADOW_S8",
}


def upper_side(handicap: float | None) -> str | None:
    """主让为负 → 主队上盘；主受让为正 → 客队上盘。"""
    if handicap is None:
        return None
    h = float(handicap)
    if h < 0:
        return "主"
    if h > 0:
        return "客"
    return None


def depth(handicap: float | None) -> float | None:
    if handicap is None:
        return None
    return abs(float(handicap))


def opposite(side: str) -> str:
    return "客" if side == "主" else "主"


def upper_water(handicap: float | None, home_w, away_w) -> float | None:
    u = upper_side(handicap)
    if u == "主":
        return None if home_w is None else float(home_w)
    if u == "客":
        return None if away_w is None else float(away_w)
    return None


def side_to_ah(side: str) -> str:
    return "home_ah" if side == "主" else "away_ah"


TIER_WATER_EXCLUDED = {"rows": 0}


def load_asian(conn, real_water_only: bool = True) -> dict[tuple[int, str, str], dict]:
    """0.3.18 第二批口径 F：档位换算水位（water_src=tier_midpoint）不进方案计算 →
    水位置空（盘口保留），按「水位缺失」处理，计数在 TIER_WATER_EXCLUDED。
    real_water_only=False（--allow-tier-water）只用于复现旧行。"""
    out = {}
    for r in conn.execute(
        "SELECT match_id, book, phase, handicap, home_water, away_water, water_src, extras_json FROM odds_asian"
    ):
        d = dict(r)
        if real_water_only and d.get("water_src") == "tier_midpoint":
            d["home_water"] = d["away_water"] = None
            d["water_tier_excluded"] = True
            TIER_WATER_EXCLUDED["rows"] += 1
        out[(int(r["match_id"]), r["book"], r["phase"])] = d
    return out


def load_euro(conn) -> dict[tuple[int, str, str], float]:
    out = {}
    for r in conn.execute(
        "SELECT match_id, book, phase, home_win FROM odds_euro_home"
    ):
        if r["home_win"] is not None:
            out[(int(r["match_id"]), r["book"], r["phase"])] = float(r["home_win"])
    return out


def load_euro_1x2_snap(conn) -> dict[tuple[int, str, str], dict]:
    """完整胜平负：odds_snapshot euro_1x2 · channel=rule · point=open|close。"""
    out: dict[tuple[int, str, str], dict] = {}
    for r in conn.execute(
        """
        SELECT match_id, book, point, price_home, price_draw, price_away, source, extras_json
        FROM odds_snapshot
        WHERE market='euro_1x2' AND channel='rule'
          AND price_home IS NOT NULL AND price_draw IS NOT NULL AND price_away IS NOT NULL
        """
    ):
        try:
            ex = json.loads(r["extras_json"] or "{}")
        except (TypeError, ValueError):
            ex = {}
        # 0.3.18 C：close 的口径 api_closing（api_phase=closing，报价时间未知）| rule_tick；同键优先 rule_tick
        basis = None
        if r["point"] == "close":
            basis = "api_closing" if ex.get("api_phase") == "closing" else "rule_tick"
        key = (int(r["match_id"]), r["book"], r["point"])
        if key in out and out[key].get("close_basis") == "rule_tick" and basis == "api_closing":
            continue
        out[key] = {
            "home": float(r["price_home"]),
            "draw": float(r["price_draw"]),
            "away": float(r["price_away"]),
            "source": r["source"],
            "close_basis": basis,
        }
    return out


def load_jc(conn) -> dict[tuple[int, str], float]:
    out = {}
    for r in conn.execute("SELECT match_id, phase, home_win FROM odds_jc_home"):
        if r["home_win"] is not None:
            out[(int(r["match_id"]), r["phase"])] = float(r["home_win"])
    return out


def load_stats(conn) -> dict[int, dict]:
    out = {}
    for r in conn.execute("SELECT match_id, rank_home, rank_away FROM stats"):
        out[int(r["match_id"])] = {
            "rank_home": r["rank_home"],
            "rank_away": r["rank_away"],
        }
    return out


def match_ids(conn) -> list[int]:
    """探针场不进；0.3.19：人工复核场（matches.manual_review=1，v2d3 列）也不进任何特征。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(matches)")}
    mr = " AND COALESCE(manual_review, 0) = 0" if "manual_review" in cols else ""
    return [
        int(r["id"])
        for r in conn.execute(
            f"SELECT id FROM matches WHERE match_uid NOT LIKE 'probe:%'{mr} ORDER BY id"
        )
    ]


def rise_up(h_open, h_close) -> tuple[bool | None, str | None]:
    """返回 (RISE_UP, skip_reason)。身份翻转 → (None, 'upper_flip')。"""
    uo, uc = upper_side(h_open), upper_side(h_close)
    if uo is None or uc is None:
        return False, "flat_or_missing"
    if uo != uc:
        return None, "upper_flip"
    do, dc = depth(h_open), depth(h_close)
    if do is None or dc is None:
        return False, "missing_depth"
    return (dc > do), None


def upper_side_s1(handicap: float | None) -> str | None:
    """S1 B4：平手盘约定主队为热门侧（上盘）。"""
    if handicap is None:
        return None
    h = float(handicap)
    if h < 0:
        return "主"
    if h > 0:
        return "客"
    return "主"


def upper_water_s1(handicap: float | None, home_w, away_w) -> float | None:
    u = upper_side_s1(handicap)
    if u == "主":
        return None if home_w is None else float(home_w)
    if u == "客":
        return None if away_w is None else float(away_w)
    return None


def gen_s1(asian, mids: list[int]) -> tuple[list[dict], dict]:
    """澳门三段真实水位 · 实力相符·水位平稳买下。不触发不写行。"""
    rows = []
    stats = {
        "feature_ready": 0,
        "trigger": 0,
        "fail_b1": 0,
        "fail_b2": 0,
        "fail_b3": 0,
        "fail_b4": 0,
        "approx_on_ready": 0,
        "approx_on_trigger": 0,
    }
    for mid in mids:
        mo = asian.get((mid, FEATURE_BOOK_S1, "open"))
        mm = asian.get((mid, FEATURE_BOOK_S1, "mid"))
        mc = asian.get((mid, FEATURE_BOOK_S1, "close"))
        co = asian.get((mid, BASELINE_BOOK, "open"))
        if not mo or not mm or not mc or not co:
            continue
        if (
            mo["handicap"] is None
            or mm["handicap"] is None
            or mc["handicap"] is None
            or co["handicap"] is None
        ):
            continue
        if (
            mo.get("home_water") is None
            or mo.get("away_water") is None
            or mm.get("home_water") is None
            or mm.get("away_water") is None
            or mc.get("home_water") is None
            or mc.get("away_water") is None
        ):
            continue
        stats["feature_ready"] += 1
        lo, lm, lc = float(mo["handicap"]), float(mm["handicap"]), float(mc["handicap"])
        baseline = float(co["handicap"])
        wo = upper_water_s1(lo, mo.get("home_water"), mo.get("away_water"))
        wm = upper_water_s1(lm, mm.get("home_water"), mm.get("away_water"))
        wc = upper_water_s1(lc, mc.get("home_water"), mc.get("away_water"))
        # mid approx from extras if present on asian row (filled by fill script)
        approx = False
        ex_raw = mm.get("extras_json")
        if ex_raw:
            try:
                ex = json.loads(ex_raw) if isinstance(ex_raw, str) else (ex_raw or {})
                approx = bool(ex.get("approx"))
            except Exception:
                approx = False
        if approx:
            stats["approx_on_ready"] += 1
        b1 = abs(lo - baseline) <= _sp("S1", "line_dev_max")
        b2 = lo == lm == lc
        b3 = all(w is not None and _sp("S1", "upper_water_lo") <= float(w) <= _sp("S1", "upper_water_hi") for w in (wo, wm, wc))
        u = upper_side_s1(lc)
        b4 = u is not None
        if not b1:
            stats["fail_b1"] += 1
            continue
        if not b2:
            stats["fail_b2"] += 1
            continue
        if not b3:
            stats["fail_b3"] += 1
            continue
        if not b4:
            stats["fail_b4"] += 1
            continue
        stats["trigger"] += 1
        if approx:
            stats["approx_on_trigger"] += 1
        lower = opposite(u)
        snap = {
            "config_fingerprint": f"shadow-s1-{VERSION_S1}-macau-actual",
            "baseline_book": BASELINE_BOOK,
            "baseline_phase": "open",
            "baseline_line": baseline,
            "feature_book": FEATURE_BOOK_S1,
            "feature_water_book": FEATURE_BOOK_S1,
            "water_src": "actual",
            "source": "5df_macauslot_history",
            "macau_open_line": lo,
            "macau_mid_line": lm,
            "macau_close_line": lc,
            "upper_water_open": wo,
            "upper_water_mid": wm,
            "upper_water_close": wc,
            "B1": True,
            "B2": True,
            "B3": True,
            "B4": True,
            "approx": approx,
            "decided_channel": "rule",
            "decided_point": "close",
            "branch": "steady_lower",
            "side": side_to_ah(lower),
            "settle_book": "macau_close",
            "subset": "158_of_177",
            "test_only": True,
        }
        rows.append(
            _pred_row(
                mid,
                "SHADOW_S1",
                lower,
                snap,
                ledger="S1",
                branch="steady_lower",
            )
        )
    return rows, stats


def upper_side_n1(handicap: float | None) -> str | None:
    """N1 上盘：仓库 odds_asian 为「主让为正」（与 5DF 欧式 line 反号）。

    主让(h>0)→主上盘；主受(h<0)→客上盘；平手盘约定主队为热门侧（与 S1 平手约定同）。
    勿直接套用 upper_side_s1(原始库盘)——后者按欧式「主让为负」，会把顺分布打成反分布。
    """
    if handicap is None:
        return None
    h = float(handicap)
    if h > 0:
        return "主"
    if h < 0:
        return "客"
    return "主"


def n1_open_flags(conn) -> dict[int, dict]:
    """威廉 1X2 open 的 open_basis / usable_at_mid / usable_at_close（与 /table/matches 同口径，0.3.17）。"""
    from app import table_matches as tm
    out = tm.build_table(conn, date_from=None, date_to=None, scope="all", strategy="CFFXDJ_5_V3",
                         channel="rule", include_live="none", settlement_version=None, as_of=None,
                         baseline_window_days=None, baseline_min_n=20, limit=100000, offset=0, fmt="nested")
    flags = {}
    for it in out["items"]:
        o = it["x1x2"][FEATURE_1X2_BOOK]["open"]
        flags[int(it["match"]["match_pk"])] = {k: o.get(k) for k in (
            "open_basis", "earliest_ts_quote_at", "ts_inferred", "usable_at_mid", "usable_at_close",
            "unusable_reason")}
    return flags


def gen_n1(asian, euro_1x2, mids: list[int], open_flags: dict[int, dict] | None = None
           ) -> tuple[list[dict], dict]:
    """顺分布抬水排除让球 · 威廉 1X2 + 澳门 open 上盘。不触发不写行。

    0.3.17（0316 follow-up 决策 3）：传入 open_flags（n1_open_flags）时，威廉 1X2 初盘在决策阶段（close）
    usable_at_close=false → 写不可评估行（not_evaluable_reason=open_unusable，direction=不下注），不判触发；
    绝不拿 11:10 自抓当初盘顶替。open_flags=None → 旧行为（仅供复现冻结行，不再用于写库）。
    """
    from app import shadow_evaluable as se
    rows = []
    stats = {
        "feature_ready": 0,
        "trigger": 0,
        "skip_unavailable": 0,
        "skip_fav_tie": 0,
        "fail_aligned": 0,
        "fail_mid_open": 0,
        "fail_high_close": 0,
        "fail_draw_press": 0,
        "not_evaluable_open_unusable": 0,
        "not_evaluable_close_unusable": 0,
    }
    for mid in mids:
        if mid in UNAVAILABLE_MACAU_AH:
            stats["skip_unavailable"] += 1
            continue
        o = euro_1x2.get((mid, FEATURE_1X2_BOOK, "open"))
        c = euro_1x2.get((mid, FEATURE_1X2_BOOK, "close"))
        mo = asian.get((mid, FEATURE_BOOK_S1, "open"))
        if not o or not c or not mo or mo.get("handicap") is None:
            continue
        stats["feature_ready"] += 1
        fl = (open_flags or {}).get(mid) if open_flags is not None else None
        # 0.3.18 C：临盘（威廉 1X2 收盘）只有 api_closing → close_unusable 不可评估（优先于 open_unusable；
        # 初盘可用性仍原样记在快照里）。台账备注「用到了时间未知的收盘价，只作历史参考」。
        if open_flags is not None and c.get("close_basis") == "api_closing":
            fl = fl or {}
            ob = fl.get("open_basis") if fl.get("open_basis") in se.SNAPSHOT_OPEN_BASES else None
            snap = se.build_snapshot(
                ledger_id="N1", evaluable=False, odds_source=se.ODDS_SOURCE_HIST, decision_phase="close",
                open_basis=ob,
                usable_at_mid=fl.get("usable_at_mid") if (ob and isinstance(fl.get("usable_at_mid"), bool)) else None,
                usable_at_close=fl.get("usable_at_close") if (ob and isinstance(fl.get("usable_at_close"), bool)) else None,
                not_evaluable_reason=se.CLOSE_UNUSABLE,
                not_evaluable_subreason="close_time_unknown_api_closing",
                close_basis="api_closing",
                config_fingerprint=f"shadow-n1-{VERSION_N1}-william-1x2", feature_1x2_book=FEATURE_1X2_BOOK,
                decided_channel="rule", decided_point="close", settle_book="macau_close", test_only=True)
            se.assert_prediction_consistent(se.NO_BET_DIRECTION, snap)
            stats["not_evaluable_close_unusable"] += 1
            rows.append(_pred_row(mid, "SHADOW_N1", se.NO_BET_DIRECTION, snap, ledger="N1",
                                  branch="not_evaluable_close_unusable"))
            continue
        if open_flags is not None and (fl is None or fl.get("usable_at_close") is not True):
            fl = fl or {}
            snap = se.build_snapshot(
                ledger_id="N1", evaluable=False, odds_source=se.ODDS_SOURCE_HIST, decision_phase="close",
                open_basis=fl.get("open_basis") if fl.get("open_basis") in se.SNAPSHOT_OPEN_BASES else "api_opening",
                usable_at_mid=fl.get("usable_at_mid") if isinstance(fl.get("usable_at_mid"), bool) else False,
                usable_at_close=False,
                not_evaluable_reason=se.OPEN_UNUSABLE,
                not_evaluable_subreason="open_time_unknown_after_decision_possible",
                config_fingerprint=f"shadow-n1-{VERSION_N1}-william-1x2", feature_1x2_book=FEATURE_1X2_BOOK,
                open_earliest_ts_quote_at=fl.get("earliest_ts_quote_at"), open_ts_inferred=fl.get("ts_inferred"),
                open_substitute="none (11:10 own capture never used as open)",
                close_basis=c.get("close_basis") or None,
                decided_channel="rule", decided_point="close", settle_book="macau_close", test_only=True)
            se.assert_prediction_consistent(se.NO_BET_DIRECTION, snap)
            stats["not_evaluable_open_unusable"] += 1
            rows.append(_pred_row(mid, "SHADOW_N1", se.NO_BET_DIRECTION, snap, ledger="N1",
                                  branch="not_evaluable_open_unusable"))
            continue
        ho, ao = float(o["home"]), float(o["away"])
        if ho == ao:
            stats["skip_fav_tie"] += 1
            continue
        fav = "主" if ho < ao else "客"
        fav_open = ho if fav == "主" else ao
        fav_close = float(c["home"]) if fav == "主" else float(c["away"])
        draw_open, draw_close = float(o["draw"]), float(c["draw"])
        lo = float(mo["handicap"])
        u = upper_side_n1(lo)  # 仓库主让为正；平手→主（与 S1 平手约定同）
        aligned = fav == u
        mid_open = _sp("N1", "home_mid_open_lo") <= fav_open <= _sp("N1", "home_mid_open_hi")
        high_close = fav_close >= _sp("N1", "home_high_close")
        draw_press = draw_close < draw_open
        if not aligned:
            stats["fail_aligned"] += 1
            continue
        if not mid_open:
            stats["fail_mid_open"] += 1
            continue
        if not high_close:
            stats["fail_high_close"] += 1
            continue
        if not draw_press:
            stats["fail_draw_press"] += 1
            continue
        stats["trigger"] += 1
        lower = opposite(u)
        snap = {
            "config_fingerprint": f"shadow-n1-{VERSION_N1}-william-1x2",
            "feature_1x2_book": FEATURE_1X2_BOOK,
            "source": "5df_odds_snap",
            "subset": "175_of_177",
            "aligned_dist": "v0_fav_eq_upper",
            "upper_convention": "home_gives_positive",
            "HOME_MID_OPEN": [_sp("N1", "home_mid_open_lo"), _sp("N1", "home_mid_open_hi")],
            "HOME_HIGH_CLOSE": _sp("N1", "home_high_close"),
            "DRAW_PRESS": "draw_close < draw_open",
            "fav": fav,
            "upper": u,
            "fav_open": fav_open,
            "fav_close": fav_close,
            "draw_open": draw_open,
            "draw_close": draw_close,
            "macau_open_line": lo,
            "HOME_MID_OPEN_ok": True,
            "HOME_HIGH_CLOSE_ok": True,
            "DRAW_PRESS_ok": True,
            "aligned_ok": True,
            "decided_channel": "rule",
            "decided_point": "close",
            "branch": "exclude_upper_lower_ah",
            "side": side_to_ah(lower),
            "settle_book": "macau_close",
            "juice": 0.95,
            "test_only": True,
        }
        if open_flags is not None:
            snap.update({"odds_source": "hist", "evaluable": True, "decision_phase": "close",
                         "close_basis": c.get("close_basis"),
                         "open_basis": fl.get("open_basis"), "open_features_used": ["open_odds"],
                         "usable_at_mid": fl.get("usable_at_mid"), "usable_at_close": True})
        rows.append(
            _pred_row(
                mid,
                "SHADOW_N1",
                lower,
                snap,
                ledger="N1",
                branch="exclude_upper_lower_ah",
            )
        )
    return rows, stats



def gen_s2(asian, mids: list[int]) -> tuple[list[dict], dict]:
    rows = []
    stats = {"eligible": 0, "a": 0, "b": 0, "b_multi_skip": 0, "flip_skip": 0, "no_trigger": 0}
    for mid in mids:
        mo = asian.get((mid, "macau", "open"))
        mc = asian.get((mid, "macau", "close"))
        co = asian.get((mid, BASELINE_BOOK, "open"))
        cc = asian.get((mid, FEATURE_WATER_BOOK, "close"))
        if not mo or not mc or not co or mo["handicap"] is None or mc["handicap"] is None or co["handicap"] is None:
            continue
        stats["eligible"] += 1
        baseline = float(co["handicap"])
        mac_o = float(mo["handicap"])
        mac_c = float(mc["handicap"])
        dev = mac_o - baseline
        rise, flip = rise_up(mac_o, mac_c)
        if rise is None:
            stats["flip_skip"] += 1
            continue
        w_close = upper_water(cc["handicap"], cc.get("home_water"), cc.get("away_water")) if cc else None
        # use macau close for upper identity when picking side
        u_close = upper_side(mac_c)
        if u_close is None:
            stats["no_trigger"] += 1
            continue
        lower = opposite(u_close)
        trig_a = (dev <= -_sp("S2", "dev_abs")) and bool(rise)
        trig_b = (dev >= _sp("S2", "dev_abs")) and bool(rise) and (w_close is not None and w_close >= _sp("S2", "close_upper_water_min"))
        if trig_a and trig_b:
            branch, direction, multi = "a_low_open_rise", lower, True
            stats["a"] += 1
            stats["b_multi_skip"] += 1
        elif trig_a:
            branch, direction, multi = "a_low_open_rise", lower, False
            stats["a"] += 1
        elif trig_b:
            branch, direction, multi = "b_high_open_rise_water", u_close, False
            stats["b"] += 1
        else:
            stats["no_trigger"] += 1
            continue
        snap = {
            "config_fingerprint": f"shadow-s2-{VERSION}-baseline_crown-water_crown",
            "baseline_book": BASELINE_BOOK,
            "baseline_phase": "open",
            "baseline_line": baseline,
            "macau_open_line": mac_o,
            "macau_close_line": mac_c,
            "DEV_O": round(dev, 6),
            "RISE_UP": bool(rise),
            "feature_water_book": FEATURE_WATER_BOOK,
            "upper_water_close": w_close,
            "feature_proxy": ["baseline_crown", "water_crown"],
            "decided_channel": "rule",
            "decided_point": "close",
            "branch": branch,
            "multi_hit": multi,
            "side": side_to_ah(direction),
        }
        rows.append(_pred_row(mid, "SHADOW_S2", direction, snap, ledger="S2", branch=branch))
    return rows, stats


def gen_n4(asian, mids: list[int]) -> tuple[list[dict], dict]:
    rows = []
    stats = {"eligible": 0, "deep": 0, "shallow": 0, "no_trigger": 0}
    for mid in mids:
        mo = asian.get((mid, "macau", "open"))
        co = asian.get((mid, BASELINE_BOOK, "open"))
        wo = asian.get((mid, FEATURE_WATER_BOOK, "open"))
        if not mo or not co or mo["handicap"] is None or co["handicap"] is None:
            continue
        if not wo or wo["handicap"] is None:
            continue
        stats["eligible"] += 1
        baseline = float(co["handicap"])
        mac_o = float(mo["handicap"])
        dev = mac_o - baseline
        w_o = upper_water(wo["handicap"], wo.get("home_water"), wo.get("away_water"))
        high = w_o is not None and w_o >= _sp("N4", "open_upper_water_min")
        u = upper_side(mac_o)  # static open identity
        if u is None:
            stats["no_trigger"] += 1
            continue
        lower = opposite(u)
        if high and dev >= _sp("N4", "dev_abs"):
            branch, direction = "deep", u
            stats["deep"] += 1
        elif high and dev <= -_sp("N4", "dev_abs"):
            branch, direction = "shallow", lower
            stats["shallow"] += 1
        else:
            stats["no_trigger"] += 1
            continue
        snap = {
            "config_fingerprint": f"shadow-n4-{VERSION}-baseline_crown-water_crown",
            "baseline_book": BASELINE_BOOK,
            "baseline_phase": "open",
            "baseline_line": baseline,
            "macau_open_line": mac_o,
            "DEV_O": round(dev, 6),
            "feature_water_book": FEATURE_WATER_BOOK,
            "upper_water_open": w_o,
            "HIGH": high,
            "feature_proxy": ["baseline_crown", "water_crown"],
            "decided_channel": "rule",
            "decided_point": "close",
            "branch": branch,
            "side": side_to_ah(direction),
        }
        rows.append(_pred_row(mid, "SHADOW_N4", direction, snap, ledger="N4", branch=branch))
    return rows, stats


def gen_s8(asian, euro, jc, stats_map, mids: list[int]) -> tuple[list[dict], dict]:
    """每场可评写一行；skip_v3 标记过滤是否触发。"""
    rows = []
    stats = {
        "eligible": 0,
        "skip": 0,
        "pass": 0,
        "skip1": 0,
        "skip2": 0,
        "skip3": 0,
        "branch_2_disabled": True,
    }
    for mid in mids:
        co = asian.get((mid, "crown", "open"))
        cm = asian.get((mid, "crown", "mid"))
        mc = asian.get((mid, "macau", "close"))
        if not co or not cm or co["handicap"] is None or cm["handicap"] is None:
            continue
        # need euro william open/close for SKIP_1
        wo = euro.get((mid, "william", "open"))
        wc = euro.get((mid, "william", "close"))
        if wo is None or wc is None or wo <= 0:
            continue
        stats["eligible"] += 1
        uw_o = upper_water(co["handicap"], co.get("home_water"), co.get("away_water"))
        uw_m = upper_water(cm["handicap"], cm.get("home_water"), cm.get("away_water"))
        ah_flat = (float(co["handicap"]) == float(cm["handicap"])) and (
            uw_o is not None and uw_m is not None and abs(uw_m - uw_o) <= _sp("S8", "upper_water_change_max")
        )
        euro_flat = abs(wc - wo) / wo < _sp("S8", "euro_flat_rel")
        jc_o, jc_c = jc.get((mid, "open")), jc.get((mid, "close"))
        jc_missing = jc_o is None or jc_c is None or jc_o <= 0
        if jc_missing:
            jc_flat = False
            jc_ok = True  # ignore clause
        else:
            jc_flat = abs(jc_c - jc_o) / jc_o < _sp("S8", "jc_flat_rel")
            jc_ok = jc_flat
        skip1 = bool(ah_flat and euro_flat and jc_ok)
        skip2 = False  # branch_2_disabled
        st = stats_map.get(mid) or {}
        rh, ra = st.get("rank_home"), st.get("rank_away")
        skip3 = False
        if mc and mc["handicap"] is not None and rh is not None and ra is not None:
            abs_h = abs(float(mc["handicap"]))
            skip3 = abs_h in (0.0, 1.0) and abs(int(rh) - int(ra)) <= _sp("S8", "level_rank_diff_max")
        reasons = []
        if skip1:
            reasons.append("flat_mid")
            stats["skip1"] += 1
        if skip2:
            reasons.append("flip_2h")
            stats["skip2"] += 1
        if skip3:
            reasons.append("integer_rank")
            stats["skip3"] += 1
        skip = skip1 or skip2 or skip3
        if skip:
            stats["skip"] += 1
        else:
            stats["pass"] += 1
        snap = {
            "config_fingerprint": f"shadow-s8-{VERSION}-flat_integer",
            "baseline_book": BASELINE_BOOK,
            "feature_water_book": FEATURE_WATER_BOOK,
            "feature_proxy": ["crown_ah_mid", "william_euro_home", "stats_rank"],
            "decided_channel": "rule",
            "decided_point": "close",
            "branch": "+".join(reasons) if reasons else "pass",
            "skip_reasons": reasons,
            "skip_v3": skip,
            "filter_hit": skip,
            "filter_target": V3,
            "s8_branches": ["flat", "integer_rank"],
            "branch_2_disabled": True,
            "AH_FLAT": ah_flat,
            "EURO_FLAT": euro_flat,
            "JC_FLAT": None if jc_missing else jc_flat,
            "jc_missing": jc_missing,
            "side": "skip",
        }
        rows.append(
            _pred_row(
                mid,
                "SHADOW_S8",
                "不下注",
                snap,
                ledger="S8",
                branch=snap["branch"],
            )
        )
    return rows, stats


def _pred_row(mid, strategy, direction, snap, *, ledger, branch) -> dict:
    rationale = [
        f"ledger_id={ledger}",
        f"branch={branch}",
        f"landing={LANDING_DOC}",
        {"feature_snapshot": snap},
    ]
    return {
        "match_id": mid,
        "strategy": strategy,
        "direction": direction,
        "settle_book": "macau_close",
        "stake": 1,
        "stake_rule": "shadow_landing_v0",
        "rationale_json": json.dumps(rationale, ensure_ascii=False),
        "snap": snap,
    }


def upsert_def(
    conn, key: str, ledger: str, *, extras_extra: dict, version: str | None = None
) -> int:
    ver = version or VERSION
    row = conn.execute(
        "SELECT id, config_json FROM strategy_defs WHERE strategy_key=? ORDER BY id DESC LIMIT 1",
        (key,),
    ).fetchone()
    if not row:
        raise SystemExit(f"missing strategy_def {key}; run seed_shadow_strategy_defs.py first")
    cfg = normalize_strategy_config(json.loads(row["config_json"] or "{}"))
    ex = dict(cfg.get("extras") or {})
    feature_water = extras_extra.get("feature_water_book", FEATURE_WATER_BOOK)
    feature_book = extras_extra.get("feature_book", feature_water)
    ex.update(
        {
            "test_only": True,
            "test_note": "仅影子对照，不进日用白名单",
            "ledger_id": ledger,
            "landing_doc": LANDING_DOC,
            "baseline_book": BASELINE_BOOK,
            "baseline_phase": "open",
            "feature_book": feature_book,
            "feature_water_book": feature_water,
            "predictions": "ready",
            "blocked_reason": None,
        }
    )
    ex.update(extras_extra)
    cfg["extras"] = ex
    cfg = normalize_strategy_config(cfg)
    fp = config_fingerprint(cfg)
    # upsert version
    ver_row = conn.execute(
        "SELECT id FROM strategy_defs WHERE strategy_key=? AND version=?",
        (key, ver),
    ).fetchone()
    notes = f"影子落地 {ledger} · {LANDING_DOC} · predictions=ready · {ver}"
    payload = (
        json.dumps(cfg, ensure_ascii=False),
        fp,
        notes,
    )
    if ver_row:
        conn.execute(
            """UPDATE strategy_defs SET config_json=?, config_fingerprint=?, status='shadow',
               is_default=0, notes=?, updated_at=datetime('now') WHERE id=?""",
            (*payload, int(ver_row["id"])),
        )
        return int(ver_row["id"])
    # update latest + insert new version row keeping history
    conn.execute(
        """UPDATE strategy_defs SET config_json=?, config_fingerprint=?, status='shadow',
           is_default=0, notes=?, version=?, updated_at=datetime('now') WHERE id=?""",
        (*payload, ver, int(row["id"])),
    )
    return int(row["id"])


def append_predictions(conn, rows: list[dict], *, protect_existing: str) -> int:
    """只 INSERT 新场；本策略已有行（冻结）不删不改。插入前后核对旧行 id 集合不变。"""
    assert_appendable(protect_existing)  # 0.3.19：旧 S2/N4 禁止追加
    before = [r[0] for r in conn.execute("SELECT id FROM predictions WHERE strategy=? ORDER BY id", (protect_existing,))]
    have = {int(r[0]) for r in conn.execute("SELECT match_id FROM predictions WHERE strategy=?", (protect_existing,))}
    n = 0
    for r in rows:
        if int(r["match_id"]) in have:
            continue
        conn.execute("""INSERT INTO predictions (match_id, strategy, direction, settle_book, rationale_json,
                        stake, stake_rule) VALUES (?,?,?,?,?,?,?)""",
                     (r["match_id"], r["strategy"], r["direction"], r["settle_book"], r["rationale_json"],
                      r["stake"], r["stake_rule"]))
        n += 1
    after = [r[0] for r in conn.execute("SELECT id FROM predictions WHERE strategy=? ORDER BY id LIMIT ?",
                                        (protect_existing, len(before)))]
    if after != before:
        raise RuntimeError(f"frozen {protect_existing} rows changed")
    return n


def replace_predictions(conn, strategy: str, rows: list[dict]) -> int:
    assert_appendable(strategy)  # 0.3.19：旧 S2/N4 禁止重建 / 追加
    conn.execute("DELETE FROM predictions WHERE strategy=?", (strategy,))
    for r in rows:
        conn.execute(
            """
            INSERT INTO predictions (match_id, strategy, direction, settle_book,
              rationale_json, stake, stake_rule)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                r["match_id"],
                r["strategy"],
                r["direction"],
                r["settle_book"],
                r["rationale_json"],
                r["stake"],
                r["stake_rule"],
            ),
        )
    return len(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=ROOT / "data" / "app.db")
    ap.add_argument("--only", default="S8", help="comma ledger ids（0.3.19：S2/N4 旧 key 冻结，只能 --dry-run）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--allow-tier-water", action="store_true",
                    help="复现旧行用：允许档位换算水位（tier_midpoint）进计算（0.3.18 起默认不允许）")
    ap.add_argument("--n1-since", default=None,
                    help="N1 append-only: only matches with jingcai_date >= this (frozen rows untouched)")
    args = ap.parse_args()
    only = {x.strip().upper() for x in args.only.split(",") if x.strip()}
    if not args.dry_run:  # 0.3.19：旧 S2 / N4 key 冻结，写库前就拦（dry-run 只算不写，仍可复现）
        for led in sorted(only & {"S2", "N4"}):
            assert_appendable(STRATEGIES[led])
    conn = connect(args.db)
    try:
        asian, euro, jc, st = load_asian(conn, real_water_only=not args.allow_tier_water), load_euro(conn), load_jc(conn), load_stats(conn)
        euro_1x2 = load_euro_1x2_snap(conn)
        mids = match_ids(conn)
        report: dict[str, Any] = {
            "version": VERSION,
            "version_s1": VERSION_S1,
            "version_n1": VERSION_N1,
            "dry_run": args.dry_run,
            "strategies": {},
            "db": str(args.db), "tier_water_excluded_rows": TIER_WATER_EXCLUDED["rows"], "real_water_only": not args.allow_tier_water,
        }
        v3_before = conn.execute(
            "SELECT COUNT(*) AS c FROM predictions WHERE strategy=?", (V3,)
        ).fetchone()["c"]

        if "S1" in only:
            rows, st_s1 = gen_s1(asian, mids)
            ready = max(int(st_s1.get("feature_ready") or 0), 1)
            approx_n = int(st_s1.get("approx_on_ready") or 0)
            approx_rate = f"{approx_n}/{st_s1.get('feature_ready', 0)}"
            subset = f"{st_s1.get('feature_ready', 0)}_of_{len(mids)}"
            for r in rows:
                snap = r.get("snap") or {}
                snap["subset"] = subset
                try:
                    rat = json.loads(r["rationale_json"])
                    for part in rat:
                        if isinstance(part, dict) and "feature_snapshot" in part:
                            part["feature_snapshot"]["subset"] = subset
                    r["rationale_json"] = json.dumps(rat, ensure_ascii=False)
                except Exception:
                    pass
            if not args.dry_run:
                sid = upsert_def(
                    conn,
                    "SHADOW_S1",
                    "S1",
                    version=VERSION_S1,
                    extras_extra={
                        "mutex_bucket": "C",
                        "water_src": "actual",
                        "source": "5df_macauslot_history",
                        "approx_rate": approx_rate,
                        "feature_book": FEATURE_BOOK_S1,
                        "feature_water_book": FEATURE_BOOK_S1,
                        "settle_book": "macau_close",
                        "subset": subset,
                        "test_only": True,
                        "branches": ["steady_lower"],
                        "no_trigger_no_row": True,
                    },
                )
                n = replace_predictions(conn, "SHADOW_S1", rows)
            else:
                sid, n = None, len(rows)
            report["version_s1"] = VERSION_S1
            report["strategies"]["SHADOW_S1"] = {
                "def_id": sid,
                "written": n,
                "stats": st_s1,
                "approx_rate": approx_rate,
                "subset": subset,
            }

        if "N1" in only:
            # 0.3.17：N1 只追加「未来」场（jingcai_date >= --n1-since），已有 SHADOW_N1 行（冻结）一律不删不改，
            # strategy_def 也不动；初盘 usable_at_close=false → open_unusable 不可评估行。
            if not args.n1_since:
                raise SystemExit("N1 is frozen: pass --n1-since YYYY-MM-DD (append-only future rows)")
            existing = {int(r["match_id"]) for r in conn.execute(
                "SELECT match_id FROM predictions WHERE strategy='SHADOW_N1'")}
            fut = [m for m in mids if m not in existing and conn.execute(
                "SELECT jingcai_date FROM matches WHERE id=?", (m,)).fetchone()["jingcai_date"] >= args.n1_since]
            rows, st_n1 = gen_n1(asian, euro_1x2, fut, open_flags=n1_open_flags(conn))
            st_n1["frozen_rows_kept"] = len(existing)
            st_n1["future_candidates"] = len(fut)
            subset = f"{st_n1.get('feature_ready', 0)}_of_{len(mids)}"
            # 底座口径固定 175_of_177（排除 unavailable）；snap 内已写死，此处与 def extras 对齐
            subset_note = "175_of_177"
            for r in rows:
                snap = r.get("snap") or {}
                snap["subset"] = subset_note
                try:
                    rat = json.loads(r["rationale_json"])
                    for part in rat:
                        if isinstance(part, dict) and "feature_snapshot" in part:
                            part["feature_snapshot"]["subset"] = subset_note
                    r["rationale_json"] = json.dumps(rat, ensure_ascii=False)
                except Exception:
                    pass
            if False:  # 0.3.17：冻结的 SHADOW_N1 def 不再更新（原 upsert 保留在下方仅供对照）
                sid = upsert_def(
                    conn,
                    "SHADOW_N1",
                    "N1",
                    version=VERSION_N1,
                    extras_extra={
                        "mutex_bucket": "D",
                        "feature_1x2_book": FEATURE_1X2_BOOK,
                        "source": "5df_odds_snap",
                        "subset": subset_note,
                        "aligned_dist": "v0_fav_eq_upper",
                        "upper_convention": "home_gives_positive",
                        "HOME_MID_OPEN": [_sp("N1", "home_mid_open_lo"), _sp("N1", "home_mid_open_hi")],
                        "HOME_HIGH_CLOSE": _sp("N1", "home_high_close"),
                        "DRAW_PRESS": "draw_close < draw_open",
                        "feature_book": FEATURE_1X2_BOOK,
                        "feature_water_book": FEATURE_BOOK_S1,
                        "settle_book": "macau_close",
                        "juice": 0.95,
                        "test_only": True,
                        "branches": ["exclude_upper_lower_ah"],
                        "no_trigger_no_row": True,
                        "unavailable_match_ids": sorted(UNAVAILABLE_MACAU_AH),
                    },
                )
                n = replace_predictions(conn, "SHADOW_N1", rows)
            elif not args.dry_run:
                sid, n = None, append_predictions(conn, rows, protect_existing="SHADOW_N1")
            else:
                sid, n = None, len(rows)
            report["version_n1"] = VERSION_N1
            report["strategies"]["SHADOW_N1"] = {
                "def_id": sid,
                "written": n,
                "stats": st_n1,
                "subset": subset_note,
                "feature_ready_dynamic": subset,
            }

        if "S2" in only:
            rows, st_s2 = gen_s2(asian, mids)
            if not args.dry_run:
                sid = upsert_def(
                    conn,
                    "SHADOW_S2",
                    "S2",
                    extras_extra={
                        "mutex_bucket": "B",
                        "branches": ["a_low_open_rise", "b_high_open_rise_water"],
                    },
                )
                n = replace_predictions(conn, "SHADOW_S2", rows)
            else:
                sid, n = None, len(rows)
            report["strategies"]["SHADOW_S2"] = {"def_id": sid, "written": n, "stats": st_s2}

        if "N4" in only:
            rows, st_n4 = gen_n4(asian, mids)
            if not args.dry_run:
                sid = upsert_def(
                    conn,
                    "SHADOW_N4",
                    "N4",
                    extras_extra={
                        "mutex_bucket": "A",
                        "branches": ["deep", "shallow"],
                    },
                )
                n = replace_predictions(conn, "SHADOW_N4", rows)
            else:
                sid, n = None, len(rows)
            report["strategies"]["SHADOW_N4"] = {"def_id": sid, "written": n, "stats": st_n4}

        if "S8" in only:
            rows, st_s8 = gen_s8(asian, euro, jc, st, mids)
            if not args.dry_run:
                sid = upsert_def(
                    conn,
                    "SHADOW_S8",
                    "S8",
                    extras_extra={
                        "mutex_bucket": "F",
                        "s8_branches": ["flat", "integer_rank"],
                        "filter_layer": True,
                        "ledger_hit_mode": "filter_skip",
                        "filter_target": V3,
                    },
                )
                n = replace_predictions(conn, "SHADOW_S8", rows)
            else:
                sid, n = None, len(rows)
            report["strategies"]["SHADOW_S8"] = {"def_id": sid, "written": n, "stats": st_s8}

        if not args.dry_run:
            conn.commit()
        v3_after = conn.execute(
            "SELECT COUNT(*) AS c FROM predictions WHERE strategy=?", (V3,)
        ).fetchone()["c"]
        report["predictions_cffxdj5v3"] = {"before": v3_before, "after": v3_after, "unchanged": v3_before == v3_after}
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
