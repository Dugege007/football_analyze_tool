"""影子预测「不可评估」记账（N5 / N5-PIN 口径卡 v1 §6，2026-10-08）。

存法（不动表结构）：predictions.rationale_json 里 `{"feature_snapshot": {...}}` 那一项：
  evaluable=false, not_evaluable_reason=<四种之一>, not_evaluable_subreason=<可选>,
  以及口径卡「快照与 validate 字段」：n_books / tick_age(按机构) / line_gap / q_model_pin /
  q_model_macau / edge_pin / edge_macau / edge_median(N5)。
  不可评估行 direction 一律写「不下注」。

validate 口径：
  - 不可评估行不进回测 entries（不计入 n_eligible，也不算「未触发」）。
  - 判定顺序在「无赛果 / 无结算盘」之后：这两类照旧进 skipped_detail，
    所以 n_not_evaluable 与 n_eligible 是同一批「已完赛且有结算盘」的场。
  - summary 增 n_not_evaluable、n_not_evaluable_by_reason（四键恒在）、
    n_not_evaluable_by_subreason、not_evaluable_items（match_id/uid/reason/subreason，供 11-06 配对单列）。
已有策略（S1/S2/S8/N1/N4/V3）没有 evaluable=false 的快照 → 数字不变（tests/test_not_evaluable.py 回归）。

来源分账（口径卡「接入细则」6，2026-10-08）：
  快照 odds_source=live|hist；validate summary 另给 by_odds_source{live,hist,unknown}，每个来源
  单独模拟资金曲线、单独 summarize（字段同顶层），顶层 summary 不变；odds_source_split=true。
  没有 odds_source（或值不认识）的行 → unknown（现有策略全部落这里）。hits 不跨来源合并。
"""
from __future__ import annotations

import json
import math
from typing import Any

PINNACLE_MISSING = "pinnacle_missing"
MARKET_INSUFFICIENT = "market_insufficient"
MODEL_INSUFFICIENT = "model_insufficient"
MODEL_SCOPE = "model_scope"
REASONS: tuple[str, ...] = (PINNACLE_MISSING, MARKET_INSUFFICIENT, MODEL_INSUFFICIENT, MODEL_SCOPE)
# N1 专用（0316 follow-up 决策 3，0.3.17）：初盘在决策阶段不可用（usable_at_<phase>=false）。
# 不进上面四键恒在的 REASONS（N5 口径卡 §6 四键不变）；出现时 by_reason 另列。
OPEN_UNUSABLE = "open_unusable"
# N1 专用（第二批口径 C，0.3.18）：临盘（威廉 1X2 收盘）只有 api_closing（报价时间未知）→ 不能当规则临盘用。
CLOSE_UNUSABLE = "close_unusable"
CLOSE_UNUSABLE_NOTE = "用到了时间未知的收盘价，只作历史参考"
# 0.3.19（决议「0.3.18 的五件事」4）：人工复核场（match 级 manual_review=true）→ validate 判不可评估。
# 不写进快照（不进 REASONS_BY_LEDGER）；由 validate 按 matches.manual_review 判，所有台账通用。
MANUAL_REVIEW = "manual_review"
# 0.3.19 推迟场：postpone_void_check=pending（推迟 > 1h，澳门时限未核实）/ void_postponed（时限写死后）
# 不算 hit、不进 n_eligible、盈亏记空；不算「不可评估」，单独计数（n_postpone_pending / n_void_postponed）。
POSTPONE_PENDING = "postpone_void_pending"
VOID_POSTPONED = "void_postponed"
UNSPECIFIED = "unspecified"  # evaluable=false 但没写原因（库内兜底计数，写入端会拒绝）

# 口径卡 §1/§3/§6 的子原因（写入端严格校验；要加新子原因先改口径卡再改这里）
SUBREASONS: dict[str, frozenset[str]] = {
    PINNACLE_MISSING: frozenset({
        "no_tick",          # 目标时点之前（含）无平博亚盘 tick
        "stale_tick",       # 最后一个 tick 早于目标时点 > max_tick_age_min（v1=60）
        "water_missing",    # 有一边水位缺失
        "water_invalid",    # 港赔不在 0.50–1.50
        "fixture_unmapped", # 映射不到 5DF fixture
    }),
    # 有效机构 < 3；hl_v0.2 / 0.3.17：真实水位（water_source=actual）机构 < 3 → real_water_lt_3（N5）
    MARKET_INSUFFICIENT: frozenset({"books_lt_min", "real_water_lt_3"}),
    # 每队<min_team_n / 联赛<min_league_n（场次）；修订 2026-10-08：拟合不收敛 / 时间衰减权重和 球队Σw<min_team_wn、联赛Σw<min_league_wn
    MODEL_INSUFFICIENT: frozenset({"team_n_lt_min", "league_n_lt_min",
                                   "fit_not_converged", "team_wn_lt_min", "league_wn_lt_min"}),
    MODEL_SCOPE: frozenset({"cup", "international", "friendly", "non_league"}),
    # N1：决策阶段初盘不可用（同 /table/matches open.unusable_reason）
    OPEN_UNUSABLE: frozenset({"open_time_unknown_after_decision_possible"}),
    # N1：临盘只有 api_closing（收盘报价时间未知，可能晚于规则临盘时刻）
    CLOSE_UNUSABLE: frozenset({"close_time_unknown_api_closing"}),
    # 0.3.19：探针 209/210 亚盘符号与快照不一致，归分析师查
    MANUAL_REVIEW: frozenset({"ah_sign_mismatch_probe"}),
}

# 哪条规则允许哪些原因（口径卡 §6）
REASONS_BY_LEDGER: dict[str, frozenset[str]] = {
    "N5-PIN": frozenset({PINNACLE_MISSING, MODEL_INSUFFICIENT, MODEL_SCOPE}),
    "N5": frozenset({MARKET_INSUFFICIENT, MODEL_INSUFFICIENT, MODEL_SCOPE}),
    "N1": frozenset({OPEN_UNUSABLE, CLOSE_UNUSABLE}),
}

# N5 口径卡修订（2026-10-08）：指纹新增键（接受并原样记录；值固定，不符即拒绝）
from app.strategy_params import get_or_none as _sp_get  # noqa: E402  公开仓：调优参数从 config 读取
_sp_int = lambda v: None if v is None else int(v)  # noqa: E731
N5_FINGERPRINT_REVISION: dict[str, Any] = {"min_team_wn": _sp_int(_sp_get("N5", "min_team_wn")),
                                            "min_league_wn": _sp_int(_sp_get("N5", "min_league_wn")), "converge_check": True,
                                            "n5_water": "real_only"}  # n5_water：hl_v0.2 只认真实水位
# 0.3.18（N5 口径卡「修订」ρ 全局合并）：N5 / N5-PIN 指纹必须带、且值固定
N5_FINGERPRINT_REQUIRED: dict[str, Any] = {"rho": "global_pooled_ivw", "rho_se_method": "profile_kish"}
# 快照必须带的 ρ 字段（键必须存在；可评估行必须是有限数，|ρ|<1，se>0；不可评估行允许 null = 没拟合到）
N5_RHO_FIELDS: tuple[str, ...] = ("rho_global", "rho_league", "rho_league_se")
# 0.3.19（N5 口径卡：ρ 合并异质性，每竞彩日一组）：Cochran Q、自由度、I²（小数 0–1）、Q 的 p 值。
# 指纹 rho_pool=fe|re_dl：Q 的 p < 0.05 → DerSimonian–Laird 随机效应（re_dl），否则固定效应（fe）。
# 新快照必须带；旧快照不改（只在写入端 build_snapshot 校验）。
N5_RHO_DAY_FIELDS: tuple[str, ...] = ("rho_Q", "rho_df", "rho_I2", "rho_Q_p")
N5_RHO_POOLS: frozenset[str] = frozenset({"fe", "re_dl"})
N5_RHO_Q_P_ALPHA = 0.05
# 快照按机构水位来源（N5 只把 actual 计入 n_books）
WATER_SOURCES: frozenset[str] = frozenset({"actual", "tier_midpoint"})
WATER_SOURCE_REAL = "actual"
N5_LEDGERS: frozenset[str] = frozenset({"N5", "N5-PIN"})

# 口径卡「快照与 validate 字段」
SNAPSHOT_FIELDS: tuple[str, ...] = (
    "n_books", "tick_age", "line_gap", "q_model_pin", "q_model_macau",
    "edge_pin", "edge_macau", "edge_median", "evaluable", "not_evaluable_reason",
    "not_evaluable_subreason", "odds_source", "tick_age_rule", "decision_phase",
    "open_basis", "open_features_used", "usable_at_mid", "usable_at_close",
    "fit_message", "home_w_n", "away_w_n", "league_w_n", "fingerprint", "water_source",
    "close_basis", "rho_global", "rho_league", "rho_league_se",
    "rho_Q", "rho_df", "rho_I2", "rho_Q_p",
)
NO_BET_DIRECTION = "不下注"

# 来源分账（口径卡「接入细则」5/6）
ODDS_SOURCE_LIVE = "live"   # 挂载后实时采集；fetched_at 离目标时点 >60 分钟 → stale_tick
ODDS_SOURCE_HIST = "hist"   # 5DF 历史回补；取目标时点仍有效的那条盘，tick_age 只记录不剔除
ODDS_SOURCES: tuple[str, ...] = (ODDS_SOURCE_LIVE, ODDS_SOURCE_HIST)
ODDS_SOURCE_UNKNOWN = "unknown"
ODDS_SOURCE_BUCKETS: tuple[str, ...] = (ODDS_SOURCE_LIVE, ODDS_SOURCE_HIST, ODDS_SOURCE_UNKNOWN)
TICK_AGE_RULE = {ODDS_SOURCE_LIVE: "live_fetched_at", ODDS_SOURCE_HIST: "hist_in_effect"}


def odds_source_of(snapshot_or_rationale: Any) -> str:
    """live / hist；缺失或不认识的值 → unknown。"""
    snap = snapshot_or_rationale if isinstance(snapshot_or_rationale, dict) \
        else snapshot_from_rationale(snapshot_or_rationale)
    v = (snap or {}).get("odds_source")
    v = str(v).strip().lower() if isinstance(v, str) else None
    return v if v in ODDS_SOURCES else ODDS_SOURCE_UNKNOWN


# 初盘口径分账（open_basis）：first_tick = 各家第一条 tick；api_opening = 接口给的 opening 字段。
# api_opening 不保证在决策时点之前就可得（分析师 2026-10-08 更正）→ 用作特征必须带 usable_at_<phase>=true。
OPEN_BASES: tuple[str, ...] = ("first_tick", "api_opening")
OPEN_BASIS_UNKNOWN = "unknown"
OPEN_BASIS_BUCKETS: tuple[str, ...] = ("first_tick", "api_opening", OPEN_BASIS_UNKNOWN)
# 写入端另接受 legacy_import（0.3.17）；validate 分账桶不变 → legacy_import 归 unknown
SNAPSHOT_OPEN_BASES: tuple[str, ...] = OPEN_BASES + ("legacy_import",)
# 开盘时刻未知、需按 usable_at_<phase> 判可用的口径。legacy_import（用户手工记录的开盘数据）自 2026-10-10 起
# 不再推定最早时间为竞彩日 11:10：开盘时间未知时 earliest_ts_quote_at 为空，usable_at_<phase> 为 false。
OPEN_BASES_TIME_UNKNOWN: frozenset[str] = frozenset({"api_opening", "legacy_import"})
# 由初盘派生、算「用到初盘」的特征名
OPEN_DERIVED_FEATURES: frozenset[str] = frozenset({
    "open_line", "open_odds", "open_return_rate", "open_kelly", "open_multi_avg",
})
DECISION_PHASES: tuple[str, ...] = ("mid", "close")


def open_basis_of(snapshot_or_rationale: Any) -> str:
    """first_tick / api_opening；缺失或不认识 → unknown。"""
    snap = snapshot_or_rationale if isinstance(snapshot_or_rationale, dict) \
        else snapshot_from_rationale(snapshot_or_rationale)
    v = (snap or {}).get("open_basis")
    v = str(v).strip().lower() if isinstance(v, str) else None
    return v if v in OPEN_BASES else OPEN_BASIS_UNKNOWN


# 临盘口径分账（close_basis，0.3.18 第二批口径 C）：
#   rule_tick   = 规则临盘时刻（rule close target_at）之前最后一笔报价（前向实时采集 / 5DF tick）
#   api_closing = 接口给的 closing 字段，报价时间未知 → 只作历史参考，单独分组，不和 live 合算
CLOSE_BASES: tuple[str, ...] = ("rule_tick", "api_closing")
CLOSE_BASIS_UNKNOWN = "unknown"
CLOSE_BASIS_BUCKETS: tuple[str, ...] = ("rule_tick", "api_closing", CLOSE_BASIS_UNKNOWN)


def close_basis_of(snapshot_or_rationale: Any) -> str:
    """rule_tick / api_closing；缺失或不认识 → unknown。

    旧冻结 N1 条目（0.3.18 前写入，无 close_basis 字段）：快照 feature_1x2_book 存在且 source=5df_odds_snap
    → 用的就是 5DF 威廉 1X2 api_closing（v2d3 核实：库内威廉 1X2 收盘全部 api_phase=closing）→ 归 api_closing。
    条目本身不改。
    """
    snap = snapshot_or_rationale if isinstance(snapshot_or_rationale, dict) \
        else snapshot_from_rationale(snapshot_or_rationale)
    snap = snap or {}
    v = snap.get("close_basis")
    v = str(v).strip().lower() if isinstance(v, str) else None
    if v in CLOSE_BASES:
        return v
    if v is None and snap.get("feature_1x2_book") and snap.get("source") == "5df_odds_snap":
        return "api_closing"
    return CLOSE_BASIS_UNKNOWN


# 分账维度：snapshot 分类函数 + 桶（unknown 恒在最后）
DIMENSIONS: dict[str, tuple[Any, tuple[str, ...]]] = {
    "odds_source": (lambda snap: odds_source_of(snap), ODDS_SOURCE_BUCKETS),
    "open_basis": (lambda snap: open_basis_of(snap), OPEN_BASIS_BUCKETS),
    "close_basis": (lambda snap: close_basis_of(snap), CLOSE_BASIS_BUCKETS),
}


def snapshot_from_rationale(rationale_json: Any) -> dict[str, Any]:
    """取 rationale[] 里第一个 feature_snapshot（同 main._load_strategy_bets 的取法）。"""
    rat = rationale_json
    if isinstance(rat, (str, bytes)):
        try:
            rat = json.loads(rat)
        except (TypeError, ValueError):
            return {}
    if isinstance(rat, list):
        for item in rat:
            if isinstance(item, dict) and isinstance(item.get("feature_snapshot"), dict):
                return item["feature_snapshot"]
    return {}


def not_evaluable_of(snapshot_or_rationale: Any) -> tuple[str, str | None] | None:
    """返回 (reason, subreason)；可评估或无标记 → None。

    判定：evaluable is False，或 not_evaluable_reason 非空（两者任一即算不可评估）。
    """
    snap = snapshot_or_rationale if isinstance(snapshot_or_rationale, dict) \
        else snapshot_from_rationale(snapshot_or_rationale)
    if not snap:
        return None
    reason = snap.get("not_evaluable_reason")
    reason = str(reason).strip() if reason not in (None, "") else None
    if snap.get("evaluable") is False or reason:
        sub = snap.get("not_evaluable_subreason")
        sub = str(sub).strip() if sub not in (None, "") else None
        return (reason or UNSPECIFIED, sub)
    return None


def new_tally() -> dict[str, Any]:
    return {"items": []}


def tally_add(tally: dict[str, Any], ne: tuple[str, str | None], *,
              match_id: Any = None, match_uid: Any = None,
              odds_source: str | None = None, open_basis: str | None = None,
              close_basis: str | None = None) -> None:
    tally.setdefault("items", []).append(
        {"match_id": match_id, "match_uid": match_uid, "reason": ne[0], "subreason": ne[1],
         "odds_source": odds_source or ODDS_SOURCE_UNKNOWN,
         "open_basis": open_basis or OPEN_BASIS_UNKNOWN,
         "close_basis": close_basis or CLOSE_BASIS_UNKNOWN})


def postpone_add(tally: dict[str, Any], status: str, *, match_id: Any = None, match_uid: Any = None,
                 delay_minutes: Any = None, odds_source: str | None = None,
                 open_basis: str | None = None, close_basis: str | None = None) -> None:
    """0.3.19：推迟待核 / 推迟作废的场（不进 bets、不进 n_eligible、不算不可评估）。"""
    tally.setdefault("postpone_items", []).append(
        {"match_id": match_id, "match_uid": match_uid, "status": status, "delay_minutes": delay_minutes,
         "odds_source": odds_source or ODDS_SOURCE_UNKNOWN,
         "open_basis": open_basis or OPEN_BASIS_UNKNOWN,
         "close_basis": close_basis or CLOSE_BASIS_UNKNOWN})


def summary_fields(tally: dict[str, Any] | None) -> dict[str, Any]:
    """validate summary 增量键。by_reason 四键恒在（0 也给），库里出现的其它原因另列。

    0.3.19：另给 n_postpone_pending / n_void_postponed / postpone_items（推迟场单独计数，不在 n_not_evaluable 里）。"""
    items = list((tally or {}).get("items") or [])
    pitems = list((tally or {}).get("postpone_items") or [])
    by_reason: dict[str, int] = {r: 0 for r in REASONS}
    by_sub: dict[str, dict[str, int]] = {}
    for it in items:
        r = it["reason"]
        by_reason[r] = by_reason.get(r, 0) + 1
        if it.get("subreason"):
            d = by_sub.setdefault(r, {})
            d[it["subreason"]] = d.get(it["subreason"], 0) + 1
    return {
        "n_not_evaluable": len(items),
        "n_not_evaluable_by_reason": by_reason,
        "n_not_evaluable_by_subreason": by_sub,
        "not_evaluable_items": items,
        "n_postpone_pending": sum(1 for x in pitems if x.get("status") == POSTPONE_PENDING),
        "n_void_postponed": sum(1 for x in pitems if x.get("status") == VOID_POSTPONED),
        "postpone_items": pitems,
    }


def empty_summary_fields() -> dict[str, Any]:
    return summary_fields(None)


# ---------------- 写入端（生成器用；本次不产出任何预测） ----------------

def _finite_or_none(v: Any, name: str) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be numeric or null, got {v!r}")
    if not math.isfinite(f):
        raise ValueError(f"{name} must be finite, got {v!r}")
    return f


def build_snapshot(*, ledger_id: str, evaluable: bool,
                   odds_source: str | None = None,
                   open_basis: str | None = None,
                   open_features_used: list[str] | tuple[str, ...] | None = None,
                   usable_at_mid: bool | None = None,
                   usable_at_close: bool | None = None,
                   decision_phase: str = "close",
                   not_evaluable_reason: str | None = None,
                   not_evaluable_subreason: str | None = None,
                   n_books: int | None = None,
                   tick_age: dict[str, float | None] | None = None,
                   line_gap: float | None = None,
                   q_model_pin: float | None = None,
                   q_model_macau: float | None = None,
                   edge_pin: float | None = None,
                   edge_macau: float | None = None,
                   edge_median: float | None = None,
                   fit_message: str | None = None,
                   home_w_n: float | None = None,
                   away_w_n: float | None = None,
                   league_w_n: float | None = None,
                   fingerprint: dict[str, Any] | None = None,
                   water_source: dict[str, str] | None = None,
                   close_basis: str | None = None,
                   **extra: Any) -> dict[str, Any]:
    """按口径卡拼 feature_snapshot，并做一致性校验（N5 / N5-PIN 生成器统一走这里）。

    tick_age：{book: 分钟}，分钟 = 目标时点 − 该家最后一个 tick 的抓取/变盘时间（≥0）。
    """
    if ledger_id not in REASONS_BY_LEDGER:
        raise ValueError(f"unknown ledger_id {ledger_id!r}; known={sorted(REASONS_BY_LEDGER)}")
    # N5 / N5-PIN 必须标来源（接入细则 6）：live | hist，其它值拒绝
    if odds_source not in ODDS_SOURCES:
        raise ValueError(f"{ledger_id}: odds_source must be one of {list(ODDS_SOURCES)}, got {odds_source!r}")
    # 初盘口径（可选）：first_tick | api_opening
    if open_basis is not None and open_basis not in SNAPSHOT_OPEN_BASES:
        raise ValueError(f"open_basis must be one of {list(SNAPSHOT_OPEN_BASES)} or omitted, got {open_basis!r}")
    if decision_phase not in DECISION_PHASES:
        raise ValueError(f"decision_phase must be one of {list(DECISION_PHASES)}, got {decision_phase!r}")
    feats = sorted(set(open_features_used or ()))
    unknown_feats = [f for f in feats if f not in OPEN_DERIVED_FEATURES]
    if unknown_feats:
        raise ValueError(f"open_features_used has unknown names {unknown_feats}; "
                         f"known={sorted(OPEN_DERIVED_FEATURES)}")
    if feats and open_basis is None:
        raise ValueError("open_features_used requires open_basis (first_tick|api_opening)")
    for nm, fv in (("usable_at_mid", usable_at_mid), ("usable_at_close", usable_at_close)):
        if fv is not None and not isinstance(fv, bool):
            raise ValueError(f"{nm} must be bool or null, got {fv!r}")
    # api_opening / legacy_import 不保证决策前可得：用了初盘派生特征 → 决策阶段对应的 usable_at_* 必须为 true
    if open_basis in OPEN_BASES_TIME_UNKNOWN and feats:
        flag = usable_at_mid if decision_phase == "mid" else usable_at_close
        if flag is not True:
            raise ValueError(
                f"open_basis={open_basis} with open-derived features {feats} requires "
                f"usable_at_{decision_phase}=true (api opening not guaranteed before decision)")
    # hist 只按「目标时点仍有效的那条盘」取值，不按 tick 时效剔除（接入细则 5）
    if odds_source == ODDS_SOURCE_HIST and not_evaluable_subreason == "stale_tick":
        raise ValueError("odds_source=hist must not be excluded as stale_tick (tick_age is record-only)")
    if evaluable:
        if not_evaluable_reason or not_evaluable_subreason:
            raise ValueError("evaluable=True must not carry not_evaluable_reason/subreason")
    else:
        if not_evaluable_reason not in REASONS_BY_LEDGER[ledger_id]:
            raise ValueError(f"{ledger_id}: reason {not_evaluable_reason!r} not allowed; "
                             f"allowed={sorted(REASONS_BY_LEDGER[ledger_id])}")
        if not_evaluable_subreason is not None and \
                not_evaluable_subreason not in SUBREASONS[not_evaluable_reason]:
            raise ValueError(f"subreason {not_evaluable_subreason!r} not in "
                             f"{sorted(SUBREASONS[not_evaluable_reason])}")
    if ledger_id == "N5-PIN" and edge_median is not None:
        raise ValueError("edge_median is N5-only")
    # N1 open_unusable：必须带初盘口径且决策阶段 usable=false（不可拿 11:10 自抓冒充初盘）
    if not evaluable and not_evaluable_reason == OPEN_UNUSABLE:
        flag = usable_at_mid if decision_phase == "mid" else usable_at_close
        if open_basis is None or flag is not False:
            raise ValueError(f"{OPEN_UNUSABLE} requires open_basis and usable_at_{decision_phase}=false")
    # N1 close_unusable（0.3.18 C）：临盘只有 api_closing（时间未知）
    if close_basis is not None and close_basis not in CLOSE_BASES:
        raise ValueError(f"close_basis must be one of {list(CLOSE_BASES)} or omitted, got {close_basis!r}")
    if not evaluable and not_evaluable_reason == CLOSE_UNUSABLE and close_basis != "api_closing":
        raise ValueError(f"{CLOSE_UNUSABLE} requires close_basis=api_closing")
    if evaluable and close_basis == "api_closing":
        raise ValueError("close_basis=api_closing cannot be evaluable (close quote time unknown)")
    # N5 修订：拟合信息 / 有效样本（时间衰减权重和）
    if fit_message is not None and not isinstance(fit_message, str):
        raise ValueError(f"fit_message must be str (res.message verbatim) or null, got {fit_message!r}")
    if not_evaluable_subreason == "fit_not_converged" and not fit_message:
        raise ValueError("fit_not_converged requires fit_message (res.message verbatim)")
    wn: dict[str, float | None] = {}
    for nm, v in (("home_w_n", home_w_n), ("away_w_n", away_w_n), ("league_w_n", league_w_n)):
        if isinstance(v, bool):
            raise ValueError(f"{nm} must be a non-negative number, got {v!r}")
        f = _finite_or_none(v, nm)
        if f is not None and f < 0:
            raise ValueError(f"{nm} must be ≥ 0, got {v!r}")
        wn[nm] = f
    fp = None
    if fingerprint is not None:
        if not isinstance(fingerprint, dict):
            raise ValueError("fingerprint must be a dict")
        if ledger_id in N5_LEDGERS:
            for k, want in N5_FINGERPRINT_REVISION.items():
                if want is None and k in fingerprint:
                    from app.strategy_params import StrategyParamsMissing
                    raise StrategyParamsMissing(f"N5.{k} 未配置（见 config/strategy_params.example.json）")
                if k in fingerprint and (fingerprint[k] != want or type(fingerprint[k]) is not type(want)):
                    raise ValueError(f"fingerprint {k}={fingerprint[k]!r}, spec revision fixes {want!r}")
        fp = dict(fingerprint)
    # 按机构水位来源 {book: actual|tier_midpoint}；N5 的 n_books 只能数 actual 的机构（hl_v0.2 real_only）
    ws: dict[str, str] | None = None
    if water_source is not None:
        if not isinstance(water_source, dict):
            raise ValueError("water_source must be {book: actual|tier_midpoint}")
        ws = {}
        for b, v in water_source.items():
            if v not in WATER_SOURCES:
                raise ValueError(f"water_source[{b}]={v!r} not in {sorted(WATER_SOURCES)}")
            ws[str(b)] = v
    if ledger_id == "N5" and ws is not None:
        real_books = {b for b, v in ws.items() if v == WATER_SOURCE_REAL}
        if n_books is not None and isinstance(n_books, int) and n_books > len(real_books):
            raise ValueError(f"N5 n_books={n_books} counts non-actual water books "
                             f"(actual={sorted(real_books)}, water_source={ws})")
        if isinstance(tick_age, dict):
            fake = sorted(str(b) for b in tick_age if ws.get(str(b)) != WATER_SOURCE_REAL)
            if fake:
                raise ValueError(f"N5 counted books {fake} have water_source != actual")
        if evaluable and len(real_books) < 3:
            raise ValueError(f"N5 evaluable requires ≥3 actual-water books, got {sorted(real_books)} "
                             "(mark market_insufficient/real_water_lt_3)")
    if not_evaluable_subreason == "real_water_lt_3" and ledger_id != "N5":
        raise ValueError("real_water_lt_3 is N5-only")
    if n_books is not None and (not isinstance(n_books, int) or n_books < 0):
        raise ValueError(f"n_books must be int ≥ 0, got {n_books!r}")
    ta: dict[str, float | None] | None = None
    if tick_age is not None:
        if not isinstance(tick_age, dict):
            raise ValueError("tick_age must be {book: minutes}")
        ta = {}
        for b, v in tick_age.items():
            f = _finite_or_none(v, f"tick_age[{b}]")
            if f is not None and f < 0:
                raise ValueError(f"tick_age[{b}] < 0 (tick after target time = leak)")
            ta[str(b)] = f
    snap: dict[str, Any] = {
        "ledger_id": ledger_id,
        "odds_source": odds_source,
        "tick_age_rule": TICK_AGE_RULE[odds_source],
        "decision_phase": decision_phase,
        "evaluable": bool(evaluable),
        "not_evaluable_reason": None if evaluable else not_evaluable_reason,
        "not_evaluable_subreason": None if evaluable else not_evaluable_subreason,
        "n_books": n_books,
        "tick_age": ta,
        "line_gap": _finite_or_none(line_gap, "line_gap"),
        "q_model_pin": _finite_or_none(q_model_pin, "q_model_pin"),
        "q_model_macau": _finite_or_none(q_model_macau, "q_model_macau"),
        "edge_pin": _finite_or_none(edge_pin, "edge_pin"),
        "edge_macau": _finite_or_none(edge_macau, "edge_macau"),
        "edge_median": _finite_or_none(edge_median, "edge_median"),
    }
    if ledger_id in N5_LEDGERS or fit_message is not None or any(v is not None for v in wn.values()):
        snap.update({"fit_message": fit_message, **wn})
    if fp is not None:
        snap["fingerprint"] = fp
    if ws is not None:
        snap["water_source"] = ws
    if close_basis is not None:
        snap["close_basis"] = close_basis
        if close_basis == "api_closing":
            snap["ledger_note"] = CLOSE_UNUSABLE_NOTE
    if open_basis is not None:  # 可选：只在给了初盘口径时写（缺省 → validate 归 unknown）
        snap.update({"open_basis": open_basis, "open_features_used": feats,
                     "usable_at_mid": usable_at_mid, "usable_at_close": usable_at_close})
    # 0.3.18：N5 / N5-PIN 的 ρ 字段（rho_global / rho_league / rho_league_se）与指纹 rho / rho_se_method 强制
    # （放在其它校验之后：原有拒写原因优先报）
    rho_vals = _check_n5_rho(ledger_id, evaluable, fingerprint, extra)
    for k in extra:
        if k in snap:
            raise ValueError(f"duplicate snapshot key {k}")
    snap.update(extra)
    snap.update(rho_vals)
    return snap


def _check_n5_rho(ledger_id: str, evaluable: bool, fingerprint: Any, extra: dict[str, Any]) -> dict[str, Any]:
    """N5 / N5-PIN：缺任何一项或值不对 → ValueError（拒写）。非 N5 台账带了 ρ 字段也拒（防串台账）。"""
    present = {k: extra.pop(k) for k in N5_RHO_FIELDS + N5_RHO_DAY_FIELDS if k in extra}
    if ledger_id not in N5_LEDGERS:
        if present:
            raise ValueError(f"{sorted(present)} are N5/N5-PIN-only")
        return {}
    missing = [k for k in N5_RHO_FIELDS + N5_RHO_DAY_FIELDS if k not in present]
    if missing:
        raise ValueError(f"{ledger_id}: snapshot missing {missing} (rho=global_pooled_ivw; 0.3.19 每竞彩日 Q/df/I2/Q_p)")
    if not isinstance(fingerprint, dict):
        raise ValueError(f"{ledger_id}: fingerprint required with {N5_FINGERPRINT_REQUIRED}")
    for k, want in N5_FINGERPRINT_REQUIRED.items():
        if fingerprint.get(k) != want:
            raise ValueError(f"{ledger_id}: fingerprint {k}={fingerprint.get(k)!r}, spec requires {want!r}")
    out: dict[str, Any] = {}
    for k in N5_RHO_FIELDS:
        v = present[k]
        if v is None:
            if evaluable:
                raise ValueError(f"{ledger_id}: evaluable snapshot requires numeric {k}")
            out[k] = None
            continue
        if isinstance(v, bool):
            raise ValueError(f"{k} must be numeric, got {v!r}")
        f = _finite_or_none(v, k)
        if k == "rho_league_se":
            if f <= 0:
                raise ValueError(f"rho_league_se must be > 0 (profile_kish), got {v!r}")
        elif not -1.0 < f < 1.0:
            raise ValueError(f"{k} must be in (-1, 1), got {v!r}")
        out[k] = f
    out.update(_check_n5_rho_day(ledger_id, evaluable, fingerprint, present))
    return out


def _check_n5_rho_day(ledger_id: str, evaluable: bool, fingerprint: dict, present: dict[str, Any]) -> dict[str, Any]:
    """0.3.19：rho_Q ≥ 0；rho_df 非负整数；rho_I2 ∈ [0, 1]（小数，不是百分数）；rho_Q_p ∈ [0, 1]；
    指纹 rho_pool ∈ {fe, re_dl}，且与 Q 检验一致（p < 0.05 ⇔ re_dl）。不可评估行允许 null。"""
    pool = fingerprint.get("rho_pool")
    # 不可评估且当日没算出 Q（rho_Q_p=null）→ 允许 rho_pool=null；其余必须 fe|re_dl
    if not (pool is None and not evaluable and present.get("rho_Q_p") is None) and pool not in N5_RHO_POOLS:
        raise ValueError(f"{ledger_id}: fingerprint rho_pool={pool!r}, spec requires one of {sorted(N5_RHO_POOLS)}")
    out: dict[str, Any] = {}
    for k in N5_RHO_DAY_FIELDS:
        v = present[k]
        if v is None:
            if evaluable:
                raise ValueError(f"{ledger_id}: evaluable snapshot requires numeric {k}")
            out[k] = None
            continue
        if isinstance(v, bool):
            raise ValueError(f"{k} must be numeric, got {v!r}")
        f = _finite_or_none(v, k)
        if k == "rho_Q" and f < 0:
            raise ValueError(f"rho_Q must be >= 0, got {v!r}")
        if k == "rho_df":
            if f < 0 or f != int(f):
                raise ValueError(f"rho_df must be a non-negative integer, got {v!r}")
            f = int(f)
        if k in ("rho_I2", "rho_Q_p") and not 0.0 <= f <= 1.0:
            raise ValueError(f"{k} must be in [0, 1] (fraction), got {v!r}")
        out[k] = f
    if out.get("rho_Q_p") is not None:
        want = "re_dl" if out["rho_Q_p"] < N5_RHO_Q_P_ALPHA else "fe"
        if pool != want:
            raise ValueError(f"{ledger_id}: rho_Q_p={out['rho_Q_p']} → rho_pool must be {want!r}, got {pool!r}")
    return out


def assert_prediction_consistent(direction: str, snapshot: dict[str, Any]) -> None:
    """不可评估行必须是「不下注」；生成器写库前调用。"""
    if not_evaluable_of(snapshot) is not None and (direction or "").strip() != NO_BET_DIRECTION:
        raise ValueError(f"not-evaluable prediction must have direction={NO_BET_DIRECTION!r}, got {direction!r}")


# ---------------- 来源分账 summary ----------------

def split_by_dimension(dim: str, bets: list[dict[str, Any]],
                       tally: dict[str, Any] | None) -> dict[str, tuple[list, dict]]:
    """bets（带 feature_snapshot）和不可评估 tally 按维度分桶；所有桶恒在。"""
    classify, buckets = DIMENSIONS[dim]
    unknown = buckets[-1]
    out: dict[str, tuple[list, dict]] = {k: ([], new_tally()) for k in buckets}
    for b in bets:
        out[classify(b.get("feature_snapshot") or {})][0].append(b)
    for it in (tally or {}).get("items") or []:
        v = it.get(dim)
        out[v if v in out else unknown][1]["items"].append(it)
    for it in (tally or {}).get("postpone_items") or []:
        v = it.get(dim)
        out[v if v in out else unknown][1].setdefault("postpone_items", []).append(it)
    return out


def split_by_odds_source(bets, tally):
    return split_by_dimension("odds_source", bets, tally)


def breakdown_summaries(dim: str, bets: list[dict[str, Any]], tally: dict[str, Any] | None, *,
                        rules: dict[str, Any], strategy_order: list[str],
                        ledger_hit_mode: str | None = None,
                        finalize=None) -> dict[str, dict[str, Any]]:
    """每个桶单独 simulate（各自资金曲线，同一本金起算）+ summarize + 不可评估计数。

    finalize：可选回调（main._attach_ledger_metrics），对每个子 summary 补台账键。
    多方案（stack）时子 summary 另带 per_strategy 精简键。
    """
    from app import backtest as bt  # 局部导入，避免模块级依赖
    res: dict[str, dict[str, Any]] = {}
    for key, (sub_bets, sub_tally) in split_by_dimension(dim, bets, tally).items():
        sim = bt.simulate(sub_bets, rules=rules, strategy_order=strategy_order)
        s = bt.summarize(sim["entries"], rules["initial_bankroll"], ledger_hit_mode=ledger_hit_mode)
        s.update(summary_fields(sub_tally))
        s[dim] = key
        if dim == "close_basis" and key == "api_closing":
            s["ledger_note"] = CLOSE_UNUSABLE_NOTE
        if len(strategy_order) > 1:
            ps: dict[str, Any] = {}
            for k in strategy_order:
                es = [e for e in sim["entries"] if e["strategy_key"] == k]
                st = sum(e.get("amount", 0.0) for e in es)
                pn = sum(e["pnl"] for e in es)
                n_el = len(es)
                hits_k = sum(1 for e in es if e.get("side"))
                ne_k = summary_fields({"items": [i for i in sub_tally["items"]
                                                 if i.get("strategy_key") == k]})
                ps[k] = {
                    "n": n_el, "bet_count": sum(1 for e in es if e.get("amount", 0) > 0),
                    "n_eligible": n_el, "hits": hits_k,
                    "coverage": (hits_k / n_el) if n_el > 0 else None,
                    "multi_hit_count": None,
                    "stake_total_amount": round(st, 2), "pnl_amount": round(pn, 2),
                    "roi": round(pn / st, 6) if st > 0 else None,
                    "n_not_evaluable": ne_k["n_not_evaluable"],
                    "n_not_evaluable_by_reason": ne_k["n_not_evaluable_by_reason"],
                }
            s["per_strategy"] = ps
        if finalize is not None:
            s = finalize(s)
        res[key] = s
    return res


def by_odds_source_summaries(bets, tally, **kw):
    return breakdown_summaries("odds_source", bets, tally, **kw)


def by_open_basis_summaries(bets, tally, **kw):
    return breakdown_summaries("open_basis", bets, tally, **kw)


def attach_breakdowns(summary: dict[str, Any], bets: list[dict[str, Any]],
                      tally: dict[str, Any] | None, **kw) -> dict[str, Any]:
    """给 summary 挂 by_odds_source / by_open_basis 和两个 *_split=true 标记。"""
    for dim in DIMENSIONS:
        summary[f"by_{dim}"] = breakdown_summaries(dim, bets, tally, **kw)
        summary[f"{dim}_split"] = True
    return summary


def _is_sub_summary(summary: dict[str, Any]) -> bool:
    return any(dim in summary for dim in DIMENSIONS)


def backfill_breakdowns(summary: dict[str, Any], finalize=None) -> dict[str, Any]:
    """旧缓存 run（分账之前算的）读出时补 by_odds_source / by_open_basis。

    改动前两库没有任何带 odds_source / open_basis 的预测 → 全部行都在 unknown：
    unknown = 顶层 summary 的同名字段原样拷贝（同一批 bets、同一条资金曲线，数值完全一致），
    其它桶 = 空 summary。标 <dim>_split_backfilled=true。
    子 summary（带维度键）和多方案 stack（带 per_strategy）不处理。
    """
    if _is_sub_summary(summary) or "per_strategy" in summary or "initial_bankroll" not in summary:
        return summary
    from app import backtest as bt
    init = float(summary.get("initial_bankroll") or 0.0)
    template = bt.summarize([], init)
    keys = list(template) + list(empty_summary_fields())
    for dim, (_cls, buckets) in DIMENSIONS.items():
        if f"by_{dim}" in summary:
            continue
        out: dict[str, dict[str, Any]] = {}
        for b in buckets:
            if b == buckets[-1]:
                s = {k: json.loads(json.dumps(summary.get(k))) for k in keys if k in summary}
                for k, v in empty_summary_fields().items():
                    s.setdefault(k, v)
            else:
                s = {**template, **empty_summary_fields()}
            s[dim] = b
            out[b] = finalize(s) if finalize is not None else s
        summary[f"by_{dim}"] = out
        summary[f"{dim}_split"] = True
        summary[f"{dim}_split_backfilled"] = True
    return summary


def backfill_by_odds_source(summary, finalize=None):  # 兼容旧名
    return backfill_breakdowns(summary, finalize=finalize)
