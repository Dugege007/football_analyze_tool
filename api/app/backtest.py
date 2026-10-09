"""M5 回测引擎（纯函数，无 DB）：简化亚盘结算 + 资金曲线注额 + 多方案 stack。

口径（settlement_version；默认 SETTLEMENT_VERSION）：
- 亚盘：主队视角 handicap；四分盘拆半；赢付 stake×water，输全损。
- 水位（juice_source；v1.7 起库里只存真实水位，不再做档位映射）：
  * 默认 ah_v4_water_midpoint：macau → 仓库固定口径 0.95（fixed_macau）；
    其它书用库里 home_water/away_water（∈ (0, 2]），juice_source 取 water_src
   （tier_midpoint | actual）；截断值记 water_censored；exclude_censored 可剔除；
    缺失/异常 → fallback。
  * 可选 ah_v4_macau_actual_or_095：macau 有 water_src=actual 且水位∈(0,2] → actual；
    否则回落 0.95 → fallback（计数 juice_fallback_count）；其它书同默认。
- 注额（复用 /bankroll/calc 规则，按资金曲线动态）：
  * 1 份 = 每个竞彩周（ISO 周）首个比赛日开盘时的权益 × unit_fraction（对齐「周一基数」）。
  * 份数：单条 ≤ per_match；同场多方案合计 ≤ per_match、同日合计 ≤ per_day，超限等比例向下取整（可到 0）。
  * 金额：份 × 1 份金额；单注 ≤ 当前剩余(权益−当日已下) × max_pct，超出按整份向下取整（可到 0）；
    低于 min_stake：below_min=skip（默认，不下）或 raise（抬到 min，同 /bankroll/calc）；
    raise 后仍超上限 → 不下。
- 同日内先按序分配注额，再逐场结算累加权益。
"""
from __future__ import annotations

import math
from datetime import date
from typing import Any

SETTLEMENT_VERSION = "ah_v4_water_midpoint"  # 默认：macau 固定 0.95（日用 / V3 对照）
SETTLEMENT_MACAU_ACTUAL = "ah_v4_macau_actual_or_095"  # 可选：macau actual 或回落 0.95
KNOWN_SETTLEMENT_VERSIONS = frozenset({SETTLEMENT_VERSION, SETTLEMENT_MACAU_ACTUAL})

NO_BET_DIRECTIONS = ("不下注", "skip", "none", "")
HOME_DIRECTIONS = ("主", "home", "H")
AWAY_DIRECTIONS = ("客", "away", "A")


def quarter_split(line: float) -> list[tuple[float, float]]:
    twox = line * 2.0
    if abs(twox - round(twox)) < 1e-9:
        return [(float(line), 1.0)]
    lo = math.floor(line * 2.0) / 2.0
    return [(lo, 0.5), (lo + 0.5, 0.5)]


def _half_outcome(hg: int, ag: int, line: float, side: str) -> str:
    margin = (hg - ag) - line
    if side == "客":
        margin = -margin
    if margin > 1e-12:
        return "win"
    if margin < -1e-12:
        return "lose"
    return "push"


def settle_code(hg: int, ag: int, handicap: float, side: str) -> tuple[str, float]:
    """Return (result_code, factor_on_win_unit) where pnl = stake × (win_w×juice − lose_w)."""
    win_w = lose_w = 0.0
    for line, w in quarter_split(float(handicap)):
        o = _half_outcome(hg, ag, line, side)
        if o == "win":
            win_w += w
        elif o == "lose":
            lose_w += w
    if win_w >= 0.999:
        code = "win"
    elif lose_w >= 0.999:
        code = "lose"
    elif win_w > 1e-9 and lose_w < 1e-9:
        code = "win_half"
    elif lose_w > 1e-9 and win_w < 1e-9:
        code = "lose_half"
    else:
        code = "push"
    return code, win_w - lose_w  # sign helper only


def settle_pnl(hg: int, ag: int, handicap: float, side: str, stake: float, juice: float) -> tuple[str, float]:
    win_w = lose_w = 0.0
    for line, w in quarter_split(float(handicap)):
        o = _half_outcome(hg, ag, line, side)
        if o == "win":
            win_w += w
        elif o == "lose":
            lose_w += w
    code, _ = settle_code(hg, ag, handicap, side)
    return code, stake * (win_w * juice - lose_w)


def resolve_juice(
    water: Any,
    fallback: float,
    *,
    book: str = "macau",
    water_src: str | None = None,
    settlement_version: str | None = None,
) -> tuple[float, str, str | None]:
    """Return (juice, juice_source, reason)。v1.7：不再做档位映射。

    settlement_version:
      - ah_v4_water_midpoint（默认）：macau → fixed_macau；其它书用库水位。
      - ah_v4_macau_actual_or_095：macau 在 water_src=actual 且水位∈(0,2] 时用真实水位，
        否则回落 fallback → juice_source=fallback。
    """
    ver = settlement_version or SETTLEMENT_VERSION
    if book == "macau" and ver != SETTLEMENT_MACAU_ACTUAL:
        return float(fallback), "fixed_macau", None
    # macau actual_or_095：必须 water_src=actual；否则走 fallback（不读档位/裸水位）
    if book == "macau" and ver == SETTLEMENT_MACAU_ACTUAL and water_src != "actual":
        reason = "water_src_not_actual" if water_src else "water_missing"
        return float(fallback), "fallback", reason
    try:
        w = float(water)
    except (TypeError, ValueError):
        return float(fallback), "fallback", "water_missing"
    if 0.0 < w <= 2.0:
        if water_src == "tier_midpoint":
            return w, "tier_midpoint", None
        return w, "actual", None
    return float(fallback), "fallback", "water_invalid"


def normalize_side(direction: str | None) -> str | None:
    d = (direction or "").strip()
    if d in HOME_DIRECTIONS:
        return "主"
    if d in AWAY_DIRECTIONS:
        return "客"
    return None


def resolve_units(stake_rule: dict[str, Any], pred_stake: Any) -> int:
    use_pred = bool(stake_rule.get("use_prediction_stake", True))
    if use_pred and pred_stake is not None:
        try:
            return max(0, int(math.floor(float(pred_stake))))
        except (TypeError, ValueError):
            pass
    return max(0, int(math.floor(float(stake_rule.get("default_units", 1)))))


def _week_key(d: str | None) -> str:
    try:
        y, m, dd = (int(x) for x in (d or "")[:10].split("-"))
        iy, iw, _ = date(y, m, dd).isocalendar()
        return f"{iy}-W{iw:02d}"
    except Exception:
        return "_undated"


def _scale_floor(entries: list[dict], cap: int, reason: str) -> None:
    total = sum(e["units"] for e in entries)
    if total <= cap:
        return
    for e in entries:
        if not e["units"]:
            continue
        new = int(math.floor(e["units"] * cap / total))
        if new != e["units"]:
            e["units"] = new
            e["reasons"].append(reason)


def max_drawdown(equity: list[float], labels: list[Any] | None = None) -> dict[str, Any]:
    peak = equity[0] if equity else 0.0
    peak_i = 0
    best = {"amount": 0.0, "pct": 0.0, "peak_equity": peak, "trough_equity": peak,
            "peak_index": 0, "trough_index": 0}
    for i, v in enumerate(equity):
        if v > peak:
            peak, peak_i = v, i
        dd = peak - v
        if dd > best["amount"] + 1e-12:
            best = {
                "amount": dd,
                "pct": (dd / peak) if peak > 0 else 0.0,
                "peak_equity": peak,
                "trough_equity": v,
                "peak_index": peak_i,
                "trough_index": i,
            }
    best["amount"] = round(best["amount"], 2)
    best["pct"] = round(best["pct"], 6)
    best["peak_equity"] = round(best["peak_equity"], 2)
    best["trough_equity"] = round(best["trough_equity"], 2)
    if labels is not None:
        # index 0 = initial point; labels align with points 1..n
        def lab(i: int) -> Any:
            return "start" if i == 0 else (labels[i - 1] if i - 1 < len(labels) else None)
        best["peak_at"] = lab(best["peak_index"])
        best["trough_at"] = lab(best["trough_index"])
    return best


def simulate(
    bets: list[dict[str, Any]],
    *,
    rules: dict[str, Any],
    strategy_order: list[str],
) -> dict[str, Any]:
    """Run shared-bankroll simulation.

    bets: each {strategy_key, match_id, match_uid, jingcai_date, order_key, direction,
                pred_stake, stake_rule(dict), home_goals, away_goals, handicap,
                home_water, away_water, fallback_juice}
    rules: {initial_bankroll, unit_fraction, per_match, per_day, min_stake, max_pct, below_min}
    """
    s_idx = {k: i for i, k in enumerate(strategy_order)}
    entries: list[dict[str, Any]] = []
    for b in bets:
        side = normalize_side(b.get("direction"))
        units_req = resolve_units(b.get("stake_rule") or {}, b.get("pred_stake")) if side else 0
        e = {**b, "side": side, "units_req": units_req, "units": units_req, "reasons": []}
        cens = (b.get("censored_home") if side == "主" else b.get("censored_away")) if side else None
        e["water_censored"] = bool(cens) if (side and rules.get("book", "macau") != "macau") else False
        if e["water_censored"]:
            e["reasons"].append("water_censored")
            if rules.get("exclude_censored"):
                e["units"] = 0
                e["reasons"].append("water_censored_excluded")
        entries.append(e)
    entries.sort(key=lambda e: (e.get("jingcai_date") or "", e.get("order_key") or 0,
                                e.get("match_id") or 0, s_idx.get(e["strategy_key"], 99)))

    per_match = int(rules["per_match"])
    per_day = int(rules["per_day"])
    min_stake = rules.get("min_stake")
    max_pct = rules.get("max_pct")
    below_min = rules.get("below_min", "raise")
    book = rules.get("book", "macau")
    frac = float(rules["unit_fraction"])
    equity = float(rules["initial_bankroll"])

    by_day: dict[str, list[dict]] = {}
    for e in entries:
        by_day.setdefault(e.get("jingcai_date") or "_undated", []).append(e)

    current_week = None
    unit_amount = round(equity * frac, 2)
    for day in sorted(by_day):
        group = by_day[day]
        wk = _week_key(day)
        if wk != current_week:
            current_week = wk
            unit_amount = round(equity * frac, 2)
        # caps (units)
        for e in group:
            if e["units"] > per_match:
                e["units"] = per_match
                e["reasons"].append("per_match")
        by_match: dict[Any, list[dict]] = {}
        for e in group:
            by_match.setdefault(e["match_id"], []).append(e)
        for g in by_match.values():
            if len(g) > 1:
                _scale_floor(g, per_match, "per_match_total")
        _scale_floor(group, per_day, "per_day")

        # amounts (sequential within day)
        allocated = 0.0
        for e in group:
            e["unit_amount"] = unit_amount
            e["equity_day_start"] = round(equity, 2)
            amt = e["units"] * unit_amount if e["units"] else 0.0
            if amt > 0:
                remaining = max(0.0, equity - allocated)
                max_amt = remaining * float(max_pct) if max_pct is not None else None
                if max_amt is not None and amt > max_amt + 1e-9:
                    fit_units = int(math.floor(max_amt / unit_amount)) if unit_amount > 0 else 0
                    amt = fit_units * unit_amount
                    e["reasons"].append("max_pct_of_remaining")
                if min_stake is not None and amt < min_stake - 1e-9:
                    if below_min == "raise":
                        if max_amt is None or min_stake <= max_amt + 1e-9:
                            amt = float(min_stake)
                            e["reasons"].append("min_stake_raised")
                        else:
                            amt = 0.0
                            e["reasons"].append("bankrupt_guard")
                    else:
                        amt = 0.0
                        e["reasons"].append("below_min_skip")
            e["amount"] = round(amt, 2)
            allocated += e["amount"]

        # settle
        for e in group:
            juice, src, jr = resolve_juice(
                e.get("home_water") if e["side"] == "主" else e.get("away_water"),
                float(e.get("fallback_juice") or 0.95),
                book=book, water_src=e.get("water_src"),
                settlement_version=rules.get("settlement_version"),
            )
            e["juice_used"], e["juice_source"] = (juice, src) if e["side"] else (None, None)
            e["water_raw"] = (e.get("home_water") if e["side"] == "主" else e.get("away_water")) if e["side"] else None
            if e["side"] and jr:
                e["reasons"].append(jr)
            if not e["side"]:
                e["result_code"], e["pnl"] = "skip", 0.0
            else:
                code, pnl = settle_pnl(int(e["home_goals"]), int(e["away_goals"]),
                                       float(e["handicap"]), e["side"], e["amount"], juice)
                e["settle_code"] = code
                if e["amount"] <= 0:
                    e["result_code"], e["pnl"] = "no_stake", 0.0
                else:
                    e["result_code"], e["pnl"] = code, round(pnl, 2)
            equity += e["pnl"]
            e["equity_after"] = round(equity, 2)
            e["pnl_units"] = round(e["pnl"] / e["unit_amount"], 6) if e.get("unit_amount") else 0.0

    return {"entries": entries, "final_equity": round(equity, 2)}


def summarize(entries: list[dict[str, Any]], initial: float,
              *, ledger_hit_mode: str | None = None) -> dict[str, Any]:
    staked = [e for e in entries if e.get("amount", 0) > 0]
    signals = [e for e in entries if e.get("side")]
    stake_total = sum(e["amount"] for e in staked)
    pnl_total = sum(e["pnl"] for e in entries)
    eq = [initial] + [e["equity_after"] for e in entries]
    labels = [e.get("match_uid") or e.get("match_id") for e in entries]
    codes: dict[str, int] = {}
    for e in staked:
        codes[e["result_code"]] = codes.get(e["result_code"], 0) + 1
    n_eligible = len(entries)
    # filter_skip（S8）：hits = 触发跳过的场（filter_hit）；不下注也可计 hit
    mode = ledger_hit_mode or next((e.get("ledger_hit_mode") for e in entries if e.get("ledger_hit_mode")), None)
    if mode == "filter_skip":
        hits = sum(1 for e in entries if e.get("filter_hit"))
    else:
        hits = len(signals)
    n_actual_water = sum(1 for e in signals if e.get("juice_source") == "actual")
    n_fallback_095 = sum(1 for e in signals if e.get("juice_source") == "fallback")
    juice_denom = n_actual_water + n_fallback_095
    fallback_rate = round(n_fallback_095 / juice_denom, 6) if juice_denom > 0 else None
    return {
        "n": n_eligible,
        "sample_count": n_eligible,
        "signal_count": len(signals),
        # 影子台账三键（mutex-buckets-min-n.md §1）；S8 用 filter_skip 口径
        "n_eligible": n_eligible,
        "hits": hits,
        "coverage": (hits / n_eligible) if n_eligible > 0 else None,
        "multi_hit_count": None,  # 单方案默认 null；多规则互斥分桶后另计
        "ledger_hit_mode": mode,
        "bet_count": len(staked),
        "no_stake_count": sum(1 for e in entries if e.get("result_code") == "no_stake"),
        "skip_dir_count": sum(1 for e in entries if e.get("result_code") == "skip"),
        "stake_total_amount": round(stake_total, 2),
        "pnl_amount": round(pnl_total, 2),
        "pnl_units": round(sum(e.get("pnl_units") or 0 for e in entries), 6),
        "roi": round(pnl_total / stake_total, 6) if stake_total > 0 else None,
        "initial_bankroll": round(initial, 2),
        "final_bankroll": round(eq[-1], 2),
        "max_drawdown": max_drawdown(eq, labels),
        "juice_actual_count": sum(1 for e in signals if e.get("juice_source") == "actual"),
        "juice_fixed_macau_count": sum(1 for e in signals if e.get("juice_source") == "fixed_macau"),
        "juice_tier_midpoint_count": sum(1 for e in signals if e.get("juice_source") == "tier_midpoint"),
        "juice_fallback_count": sum(1 for e in signals if e.get("juice_source") == "fallback"),
        # 公平对照副表（方案卡）：真实水位灵敏度计数（按有方向场）
        "n_actual_water": n_actual_water,
        "n_fallback_095": n_fallback_095,
        "fallback_rate": fallback_rate,
        "water_censored_count": sum(1 for e in signals if e.get("water_censored")),
        "water_censored_bet_count": sum(1 for e in signals if e.get("water_censored") and e.get("amount", 0) > 0),
        "water_censored_excluded_count": sum(1 for e in signals if "water_censored_excluded" in e["reasons"]),
        "min_stake_raised_count": sum(1 for e in entries if "min_stake_raised" in e["reasons"]),
        "bankrupt_guard_count": sum(1 for e in entries if "bankrupt_guard" in e["reasons"]),
        "result_counts": codes,
        "capped_count": sum(1 for e in entries if any(
            r in ("per_match", "per_match_total", "per_day", "max_pct_of_remaining") for r in e["reasons"])),
        "below_min_skipped_count": sum(1 for e in entries if any(
            r in ("below_min_skip", "bankrupt_guard") for r in e["reasons"])),
    }


def series_by_match(entries: list[dict[str, Any]], initial: float) -> dict[str, Any]:
    """Aggregate entries per match (stack: several strategies on one match)."""
    pts: list[dict[str, Any]] = []
    idx: dict[Any, dict] = {}
    for e in entries:
        p = idx.get(e["match_id"])
        if p is None:
            p = {"x": e.get("jingcai_date") or "", "match_id": e["match_id"],
                 "match_uid": e.get("match_uid"), "pnl": 0.0, "stake": 0.0,
                 "sides": {}, "conflict": False}
            idx[e["match_id"]] = p
            pts.append(p)
        p["pnl"] += e["pnl"]
        p["stake"] += e.get("amount", 0.0)
        if e.get("side"):
            p["sides"][e["strategy_key"]] = e["side"]
        p["equity_after"] = e["equity_after"]
    for p in pts:
        p["conflict"] = len(set(p["sides"].values())) > 1
    eq = [initial] + [p["equity_after"] for p in pts]
    peak = initial
    dd_pct: list[float] = []
    for v in eq[1:]:
        peak = max(peak, v)
        dd_pct.append(round((peak - v) / peak, 6) if peak > 0 else 0.0)
    cum = []
    run = 0.0
    for p in pts:
        run += p["pnl"]
        cum.append(round(run, 2))
    return {
        "x": [p["x"] for p in pts],
        "match_ids": [p["match_id"] for p in pts],
        "pnl": [round(p["pnl"], 2) for p in pts],
        "stake": [round(p["stake"], 2) for p in pts],
        "cumulative_pnl": cum,
        "bankroll": [p["equity_after"] for p in pts],
        "drawdown_pct": dd_pct,
        "conflict": [p["conflict"] for p in pts],
        "sides": [p["sides"] for p in pts],
        "initial_bankroll": round(initial, 2),
    }


def find_conflicts(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_match: dict[Any, list[dict]] = {}
    for e in entries:
        if e.get("side"):
            by_match.setdefault(e["match_id"], []).append(e)
    out = []
    for mid, g in by_match.items():
        if len({e["side"] for e in g}) > 1:
            out.append({
                "match_id": mid,
                "match_uid": g[0].get("match_uid"),
                "jc_id": g[0].get("jc_id"),
                "home_team": g[0].get("home_team"),
                "away_team": g[0].get("away_team"),
                "jingcai_date": g[0].get("jingcai_date"),
                "kickoff_at": g[0].get("kickoff_at"),
                "legs": [{"strategy_key": e["strategy_key"], "side": e["side"],
                          "amount": e.get("amount", 0.0), "result_code": e["result_code"],
                          "pnl": e["pnl"]} for e in g],
                "staked_both": sum(1 for e in g if e.get("amount", 0) > 0) > 1,
                "net_pnl": round(sum(e["pnl"] for e in g), 2),
                "net_stake": round(sum(e.get("amount", 0.0) for e in g), 2),
            })
    return out
