# -*- coding: utf-8 -*-
"""Dixon-Coles（1997）拟合模块 · 影子规则 N5 / N5-PIN 共用（口径卡 v2_0-n5-poisson-spec-v1.md）。

只做「按 cutoff 取数拟合 → 出 0–10 球比分矩阵 → 亚盘有效赢盘概率」这一段纯计算：
- 不读写任何数据库，不取数，不联网；训练赛果由调用方传入（见 research/dc_train/README.md）。
- 依赖：numpy、scipy（match-analysis-api 的 .venv 目前没有这两个包，接入时需要安装）。

主要接口
--------
fit(results, cutoff, params=None) -> DCModel
    results：可迭代的 dict，键 league_id, kickoff_at, home_team, away_team, home_goals, away_goals, status。
    kickoff_at 可以是带时区的 datetime、不带时区的 datetime（按北京时间理解）或 ISO 字符串。
    只用 status 为完赛、kickoff_at + 3h <= cutoff、且 kickoff_at > cutoff − window_days 天的场次（window_days 见 config/strategy_params.json → N5）。
    权重 w = exp(−ξ·age)，ξ = ln2/half_life_days，age = (cutoff − kickoff_at) 折成小数天；每次调用按本次 cutoff 重算。
    每个联赛单独拟合：每队进攻/防守参数、每联赛一个主场优势、ρ；加权最大似然；约束 每联赛进攻参数均值 = 0。
    ρ（修订 2026-10-08）：第一遍联赛 ρ_league + profile 似然/Kish SE → 逆方差合成 ρ_global（过门槛联赛）→
    第二遍 ρ 固定为 ρ_global 重拟；两遍都要收敛且观测低比分场 τ > 1e-12。
predict(model, league_id, home, away, max_goals=10) -> Prediction（不可评估时 evaluable=False，reason='model_insufficient'）
    门槛（判定顺序）：联赛 ≥min_league_n 场 → 联赛 Σw ≥min_league_wn → 拟合收敛 → 双方各 ≥min_team_n 场 → 双方各 Σw ≥min_team_wn；
    子原因 league_n_lt_min / league_wn_lt_min / fit_not_converged / team_n_lt_min / team_wn_lt_min。
    输出带 home_n/away_n/league_n、home_w_n/away_w_n/league_w_n、fit_message（scipy res.message 原文）。
score_matrix(model, league_id, home, away, max_goals=10) -> np.ndarray（归一、无负数；不可评估时抛 NotEvaluable）
ah_cover_prob(matrix, line) -> float     主队视角盘口（正数 = 主让），四分之一盘合并算 combined_wl
devig_2way(odds_home, odds_away) -> (p_home, p_away)   两路比例法；hk_to_decimal(hk) = hk + 1
median_consensus(values) -> float         偶数取中间两家平均
n5_consensus(edges, min_books=3) -> (edge_median | None, n_books)
jc_day_training_cutoff(jc_date) -> datetime   接入细则 1：训练截止 = 本竞彩日 00:00（北京时间）
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Hashable, Iterable, Mapping, Sequence

import numpy as np
from scipy.optimize import minimize

# --- 公开仓：N5 调优参数从 config/strategy_params.json · N5 读取（模板全为 null；未配置时拟合拒绝运行） ---
import json as _sp_json
import os as _sp_os
from pathlib import Path as _SpPath


class StrategyParamsMissing(RuntimeError):
    """需要的策略参数未配置（公开仓默认如此）。"""


def _sp_load() -> dict:
    root = _SpPath(__file__).resolve().parents[2]
    env = _sp_os.environ.get("STRATEGY_PARAMS_PATH", "").strip()
    cands = [_SpPath(env) if env and _SpPath(env).is_absolute() else root / env] if env else []
    cands += [root / "config" / "strategy_params.json", root / "config" / "strategy_params.example.json"]
    for p in cands:
        if p.exists():
            return (_sp_json.loads(p.read_text(encoding="utf-8")).get("N5") or {})
    return {}


_N5 = _sp_load()
_i = lambda v: None if v is None else int(v)  # noqa: E731
_f = lambda v: None if v is None else float(v)  # noqa: E731
_TUNED_KEYS = ("window_days", "half_life_days", "min_team_n", "min_league_n", "min_team_wn", "min_league_wn")


def _require_tuned(p: Mapping[str, Any]) -> None:
    miss = [k for k in _TUNED_KEYS if p.get(k) is None]
    if miss:
        raise StrategyParamsMissing(f"N5 参数未配置: {miss}（见 config/strategy_params.example.json）")

BJ = timezone(timedelta(hours=8))

DEFAULT_PARAMS: dict[str, Any] = {
    "window_days": _i(_N5.get("window_days")),
    "half_life_days": _f(_N5.get("half_life_days")),
    "min_team_n": _i(_N5.get("min_team_n")),
    "min_league_n": _i(_N5.get("min_league_n")),
    "min_team_wn": _f(_N5.get("min_team_wn")),     # 球队时间衰减权重和 Σw 门槛
    "min_league_wn": _f(_N5.get("min_league_wn")),  # 联赛 Σw 门槛
    "finished_lag_hours": 3.0,
    "max_goals": 10,
    # ρ 的盒约束；拟合后对每一对球队再按 λ、μ 求可行区间并断言 τ ≥ 0（见 _rho_feasible_interval）
    "rho_bounds": (-0.3, 0.3),
    # 攻防参数盒约束（log 尺度），防止窗口里场次很少的队把参数推到无穷
    "param_bound": 4.0,
}

# 指纹：口径卡「指纹字段」+ 接入细则；改任何一项 = 新版本码
FINGERPRINT: dict[str, Any] = {
    "p_model_method": "dixon_coles_v1",
    "window_days": _i(_N5.get("window_days")),
    "half_life_days": _i(_N5.get("half_life_days")),
    "decay_xi_per_day": (round(math.log(2) / float(_N5["half_life_days"]), 6) if _N5.get("half_life_days") else None),
    "decay_age_unit": "fractional_days_from_cutoff",
    "min_team_n": _i(_N5.get("min_team_n")),
    "min_league_n": _i(_N5.get("min_league_n")),
    "min_team_wn": _i(_N5.get("min_team_wn")),
    "min_league_wn": _i(_N5.get("min_league_wn")),
    "converge_check": True,
    "team_n_scope": "same_league_in_window",
    "scope": "league_only",
    "league_whitelist": "research/dc_train/league_whitelist.csv",
    "max_goals": 10,
    "identifiability": "mean_attack_zero_per_league",
    "home_adv": "one_per_league",
    # 修订 2026-10-08（全局 ρ）：各联赛先联合拟合 ρ_league（只作诊断），profile 似然 95% 区间 + Kish 有效样本
    # 求 SE，过门槛的联赛按逆方差加权合成 ρ_global；再把 ρ 固定为 ρ_global 重拟攻防/主场（第二遍也要收敛 + τ 检查）
    "rho": "global_pooled_ivw",
    "rho_se_method": "profile_kish",
    "rho_pool_gates": "league_n_ge_min&league_wn_ge_min&pass1_converged&tau_ok&se_finite",
    "rho_second_pass": "refit_fixed_rho_global",
    "tau_end_check": True,
    # 修订 2026-10-08 18:14：异质性（Cochran Q / I² / p）。默认固定效应 IVW；rho_Q_p < 0.05 改 DerSimonian–Laird 随机效应。
    # 模块级是占位；每次 fit 在 model.fingerprint 里写当天实际用的方法 fe | re_dl。
    "rho_pool": None,
    "rho_re_switch_p": 0.05,
    "fit_cadence": "per_jc_day_prev_days",
    "training_cutoff": "jc_day_00:00_bj",
    "finished_rule": "kickoff_plus_3h_le_cutoff_and_status_finished",
    "compare_space": "ah_effective_cover",
    "quarter_line": "combined_wl",
    "devig_method": "multiplicative_2way",
    "feature_odds_phase": "close",
    "max_tick_age_min": 60,
    "tick_age_rule": "live_fetched_at|hist_in_effect",
    "delta": _f(_N5.get("edge_delta")),
    "settle_book": "macau_close",
    "juice": 0.95,
    # 下面三项每条规则各自填：line_source(pin|per_book)、market_prob_source(pinnacle_only|median_5book_min3)、clv_column
    "line_source": None,
    "market_prob_source": None,
    "clv_column": None,
}

FINISHED_STATUSES = {"finished", "ft", "full_time", "fulltime", "完赛", "已完赛", "完场"}

REASON_MODEL_INSUFFICIENT = "model_insufficient"
REASON_MODEL_SCOPE = "model_scope"
SUB_TEAM_N = "team_n_lt_min"
SUB_LEAGUE_N = "league_n_lt_min"
SUB_TEAM_WN = "team_wn_lt_min"
SUB_LEAGUE_WN = "league_wn_lt_min"
SUB_NOT_CONVERGED = "fit_not_converged"
SUB_NON_LEAGUE = "non_league"


# ---------------------------------------------------------------- 工具


def to_bj(ts: Any) -> datetime:
    """把 kickoff_at / cutoff 统一成带北京时区的 datetime；不带时区的按北京时间理解。"""
    if isinstance(ts, datetime):
        dt = ts
    elif isinstance(ts, date):
        dt = datetime(ts.year, ts.month, ts.day)
    elif isinstance(ts, str):
        s = ts.strip().replace("Z", "+00:00")
        if " " in s and "T" not in s:
            s = s.replace(" ", "T", 1)
        dt = datetime.fromisoformat(s)
    else:
        raise TypeError(f"无法解析时间: {ts!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BJ)
    return dt.astimezone(BJ)


def jc_day_training_cutoff(jc_date: date | str) -> datetime:
    """接入细则 1：训练集只用之前竞彩日的赛果，截止 = 本竞彩日 00:00（北京时间）。"""
    if isinstance(jc_date, str):
        jc_date = date.fromisoformat(jc_date)
    return datetime(jc_date.year, jc_date.month, jc_date.day, tzinfo=BJ)


def _is_finished(r: Mapping[str, Any]) -> bool:
    st = str(r.get("status") or "").strip().lower()
    if st not in FINISHED_STATUSES:
        return False
    hg, ag = r.get("home_goals"), r.get("away_goals")
    if hg is None or ag is None:
        return False
    try:
        hg, ag = int(hg), int(ag)
    except (TypeError, ValueError):
        return False
    return hg >= 0 and ag >= 0


# ---------------------------------------------------------------- 数据结构


class NotEvaluable(Exception):
    def __init__(self, reason: str, subreason: str | None = None, detail: str = "", fit_message: str | None = None):
        super().__init__(f"{reason}/{subreason}: {detail}")
        self.reason = reason
        self.subreason = subreason
        self.detail = detail
        self.fit_message = fit_message


@dataclass
class LeagueFit:
    """attack/defence/home_adv/rho/converged/message 是**最终用的那一遍**：有 ρ_global 时 = 第二遍（ρ 固定为 ρ_global）。
    rho_league / rho_league_se / pass1_* 是第一遍（ρ 联合拟合）的诊断值。"""
    league_id: Hashable
    teams: list[Hashable]
    attack: dict[Hashable, float]
    defence: dict[Hashable, float]  # 防守参数（越大越容易丢球）
    home_adv: float
    rho: float  # 出矩阵用的 ρ（= ρ_global；第二遍没做时 = ρ_league）
    n_matches: int
    team_n: dict[Hashable, int]
    weight_sum: float  # 联赛 Σw（= league_w_n）
    nll: float
    converged: bool  # scipy 收敛 且 观测低比分场 τ > TAU_FLOOR
    message: str = ""  # scipy res.message 原文（fit_message）；τ 越界时追加说明
    team_wn: dict[Hashable, float] = field(default_factory=dict)
    rho_league: float | None = None
    rho_league_se: float | None = None
    rho_league_ci: tuple[float | None, float | None] = (None, None)
    n_eff: float | None = None  # Kish (Σw)²/Σw²
    pass1_converged: bool | None = None
    pass1_message: str = ""
    tau_ok: bool = True
    n_tau_viol: int = 0
    pooled: bool = False  # 是否进了 ρ_global 的合成
    second_pass: bool = False

    def lambdas(self, home: Hashable, away: Hashable) -> tuple[float, float]:
        lam = math.exp(self.attack[home] + self.defence[away] + self.home_adv)
        mu = math.exp(self.attack[away] + self.defence[home])
        return lam, mu


@dataclass
class LeagueInsufficient:
    league_id: Hashable
    n_matches: int
    team_n: dict[Hashable, int]
    reason: str = REASON_MODEL_INSUFFICIENT
    subreason: str = SUB_LEAGUE_N
    weight_sum: float = 0.0
    team_wn: dict[Hashable, float] = field(default_factory=dict)


@dataclass
class DCModel:
    cutoff: datetime
    params: dict[str, Any]
    fingerprint: dict[str, Any]
    leagues: dict[Hashable, LeagueFit] = field(default_factory=dict)
    insufficient: dict[Hashable, LeagueInsufficient] = field(default_factory=dict)
    n_input: int = 0
    n_used: int = 0
    n_dropped: dict[str, int] = field(default_factory=dict)
    rho_global: float | None = None
    rho_global_se: float | None = None
    rho_global_fe: float | None = None
    rho_global_fe_se: float | None = None
    rho_Q: float | None = None
    rho_df: int | None = None
    rho_I2: float | None = None
    rho_Q_p: float | None = None
    rho_tau2: float | None = None
    rho_pool_method: str | None = None  # fe | re_dl
    rho_pool: list[Hashable] = field(default_factory=list)  # 进入合成的联赛
    rho_pool_excluded: dict[Hashable, str] = field(default_factory=dict)  # 未进合成的联赛 → 原因


@dataclass
class Prediction:
    evaluable: bool
    matrix: np.ndarray | None = None
    reason: str | None = None
    subreason: str | None = None
    lambda_home: float | None = None
    lambda_away: float | None = None
    rho_fitted: float | None = None  # = rho_league（兼容旧字段名）
    rho_used: float | None = None    # 本场实际用的 ρ（ρ_global 按本场 λ、μ 裁到 τ≥0 区间后）
    rho_clipped: bool = False
    rho_league: float | None = None
    rho_league_se: float | None = None
    rho_global: float | None = None
    home_n: int | None = None
    away_n: int | None = None
    league_n: int | None = None
    home_w_n: float | None = None
    away_w_n: float | None = None
    league_w_n: float | None = None
    fit_message: str | None = None


# ---------------------------------------------------------------- 拟合


def _select(results: Iterable[Mapping[str, Any]], cutoff: datetime, p: Mapping[str, Any]):
    _require_tuned(p)
    lag = timedelta(hours=float(p["finished_lag_hours"]))
    start = cutoff - timedelta(days=float(p["window_days"]))
    xi = math.log(2) / float(p["half_life_days"])
    by_league: dict[Hashable, list[tuple]] = {}
    dropped = {"not_finished": 0, "too_recent": 0, "out_of_window": 0, "bad_row": 0}
    n_in = 0
    for r in results:
        n_in += 1
        try:
            ko = to_bj(r["kickoff_at"])
            lg, h, a = r["league_id"], r["home_team"], r["away_team"]
        except (KeyError, TypeError, ValueError):
            dropped["bad_row"] += 1
            continue
        if h is None or a is None or h == a:
            dropped["bad_row"] += 1
            continue
        if not _is_finished(r):
            dropped["not_finished"] += 1
            continue
        if ko + lag > cutoff:  # 防泄漏：开赛 + 3h 必须 ≤ cutoff
            dropped["too_recent"] += 1
            continue
        if ko <= start:
            dropped["out_of_window"] += 1
            continue
        age_days = (cutoff - ko).total_seconds() / 86400.0
        w = math.exp(-xi * age_days)
        by_league.setdefault(lg, []).append((h, a, int(r["home_goals"]), int(r["away_goals"]), w))
    return by_league, n_in, dropped


def _tau_terms(x, y, lam, mu, rho):
    """返回 τ 以及 d log τ / d η1、d η2、d ρ（η = log λ / log μ）。"""
    tau = np.ones_like(lam)
    d1 = np.zeros_like(lam)
    d2 = np.zeros_like(lam)
    dr = np.zeros_like(lam)
    m00 = (x == 0) & (y == 0)
    m01 = (x == 0) & (y == 1)
    m10 = (x == 1) & (y == 0)
    m11 = (x == 1) & (y == 1)
    lm = lam * mu
    tau[m00] = 1.0 - lm[m00] * rho
    tau[m01] = 1.0 + lam[m01] * rho
    tau[m10] = 1.0 + mu[m10] * rho
    tau[m11] = 1.0 - rho
    safe = np.maximum(tau, 1e-12)
    d1[m00] = -lm[m00] * rho / safe[m00]
    d2[m00] = -lm[m00] * rho / safe[m00]
    dr[m00] = -lm[m00] / safe[m00]
    d1[m01] = lam[m01] * rho / safe[m01]
    dr[m01] = lam[m01] / safe[m01]
    d2[m10] = mu[m10] * rho / safe[m10]
    dr[m10] = mu[m10] / safe[m10]
    dr[m11] = -1.0 / safe[m11]
    return tau, d1, d2, dr


def _unpacker(n):
    # 参数向量：att[0..n-2]（att[n-1] = −Σ，保证均值严格为 0）、def[0..n-1]、γ、ρ
    def unpack(theta):
        att = np.empty(n)
        att[: n - 1] = theta[: n - 1]
        att[n - 1] = -theta[: n - 1].sum()
        dfn = theta[n - 1 : 2 * n - 1]
        gamma = theta[2 * n - 1]
        rho = theta[2 * n]
        return att, dfn, gamma, rho

    return unpack


TAU_FLOOR = 1e-12
PENALTY_K = 1e6


def _make_objective(hi, ai, x, y, wn, n):
    """加权负对数似然 + 解析梯度。观测低比分场 τ ≤ TAU_FLOOR 时 log τ 取常数 log(TAU_FLOOR)（梯度置 0），
    另加连续的二次罚项 P = K·Σ w·(TAU_FLOOR − τ)²，梯度 −2K·w·(TAU_FLOOR − τ)·∂τ（在边界处为 0，不加常数 +1）。
    修订 2026-10-08「罚项收尾」。"""
    unpack = _unpacker(n)

    def nll_grad(theta):
        att, dfn, gamma, rho = unpack(theta)
        eta1 = att[hi] + dfn[ai] + gamma
        eta2 = att[ai] + dfn[hi]
        lam = np.exp(eta1)
        mu = np.exp(eta2)
        tau, dt1, dt2, dtr = _tau_terms(x, y, lam, mu, rho)
        bad = tau <= TAU_FLOOR
        ll = x * eta1 - lam + y * eta2 - mu + np.log(np.maximum(tau, TAU_FLOOR))
        f = -float(np.dot(wn, ll))
        if bad.any():
            # _tau_terms 给的是 ∂log τ = ∂τ / max(τ, floor)；越界处 max = floor → ∂τ = ∂log τ · floor
            viol = TAU_FLOOR - tau[bad]
            f += PENALTY_K * float(np.dot(wn[bad], viol**2))
            coef = 2.0 * PENALTY_K * viol * TAU_FLOOR  # −∂P/∂· = 2K·w·viol·∂τ（g 是 −∂f，下面再统一取负）
            dt1, dt2, dtr = dt1.copy(), dt2.copy(), dtr.copy()
            dt1[bad] = coef * dt1[bad]
            dt2[bad] = coef * dt2[bad]
            dtr[bad] = coef * dtr[bad]
        g1 = wn * (x - lam + dt1)
        g2 = wn * (y - mu + dt2)
        g_att = np.bincount(hi, g1, n) + np.bincount(ai, g2, n)
        g_def = np.bincount(ai, g1, n) + np.bincount(hi, g2, n)
        g_gamma = g1.sum()
        g_rho = float(np.dot(wn, dtr))
        grad = np.empty_like(theta)
        grad[: n - 1] = -(g_att[: n - 1] - g_att[n - 1])
        grad[n - 1 : 2 * n - 1] = -g_def
        grad[2 * n - 1] = -g_gamma
        grad[2 * n] = -g_rho
        return f, grad

    return nll_grad, unpack


def _team_counts(rows):
    tn: dict[Hashable, int] = {}
    tw: dict[Hashable, float] = {}
    for r in rows:
        for t in (r[0], r[1]):
            tn[t] = tn.get(t, 0) + 1
            tw[t] = tw.get(t, 0.0) + r[4]
    return tn, tw


CHI2_1_95 = 3.841458820694124  # χ²(1) 95% 分位
Z_95 = 1.959963984540054


class _LeagueProblem:
    """单联赛的加权 DC 似然：第一遍联合拟合 ρ、profile 似然（ρ 固定）、第二遍（ρ = ρ_global）共用。"""

    def __init__(self, league_id, rows, p):
        self.league_id = league_id
        self.p = p
        self.teams = sorted({r[0] for r in rows} | {r[1] for r in rows}, key=lambda t: (str(type(t)), str(t)))
        self.idx = {t: i for i, t in enumerate(self.teams)}
        n = self.n = len(self.teams)
        self.hi = np.array([self.idx[r[0]] for r in rows])
        self.ai = np.array([self.idx[r[1]] for r in rows])
        self.x = np.array([r[2] for r in rows], dtype=float)
        self.y = np.array([r[3] for r in rows], dtype=float)
        w = np.array([r[4] for r in rows], dtype=float)
        self.wsum = float(w.sum())
        self.n_eff = float(w.sum() ** 2 / np.dot(w, w))  # Kish 有效样本
        self.wn = w / self.wsum  # 归一化只为数值条件，不改变 MLE
        self.n_rows = len(rows)
        self.team_n, self.team_wn = _team_counts(rows)
        self.nll_grad, self.unpack = _make_objective(self.hi, self.ai, self.x, self.y, self.wn, n)
        mean_goals = max(float(np.dot(self.wn, self.x + self.y)) / 2.0, 0.1)
        th = np.zeros(2 * n + 1)
        th[n - 1 : 2 * n - 1] = math.log(mean_goals)
        th[2 * n - 1] = 0.2
        th[2 * n] = 0.0
        self.theta0 = th
        pb = float(p["param_bound"])
        self.rho_bounds = (float(p["rho_bounds"][0]), float(p["rho_bounds"][1]))
        self.base_bounds = [(-pb, pb)] * (n - 1) + [(-pb, pb)] * n + [(-2.0, 2.0)]
        self.low = (self.x <= 1) & (self.y <= 1)

    def solve(self, rho_fixed: float | None = None, theta_start=None):
        th = np.array(self.theta0 if theta_start is None else theta_start, dtype=float).copy()
        if rho_fixed is None:
            rb = self.rho_bounds
        else:
            rb = (float(rho_fixed), float(rho_fixed))
            th[2 * self.n] = float(rho_fixed)
        res = minimize(self.nll_grad, th, jac=True, method="L-BFGS-B", bounds=self.base_bounds + [rb],
                       options={"maxiter": 5000, "ftol": 1e-14, "gtol": 1e-9, "maxcor": 30})
        return res

    def tau_violations(self, theta) -> int:
        """拟合结束后，观测到的低比分场（0-0/0-1/1-0/1-1）τ ≤ TAU_FLOOR 的场数（修订：>0 判 fit_not_converged）。"""
        att, dfn, gamma, rho = self.unpack(np.asarray(theta, dtype=float))
        lam = np.exp(att[self.hi] + dfn[self.ai] + gamma)
        mu = np.exp(att[self.ai] + dfn[self.hi])
        tau, *_ = _tau_terms(self.x, self.y, lam, mu, rho)
        return int(((tau <= TAU_FLOOR) & self.low).sum())

    def profile_se(self, theta_hat, rho_hat: float):
        """ρ 的 profile 似然 95% 区间：2·n_eff·[f_prof(ρ) − f_prof(ρ̂)] = χ²₁(0.95)，f 为归一化权重下的 NLL，
        n_eff = Kish (Σw)²/Σw²。SE = 区间宽 / (2·1.96)；只找到一侧时用该侧半宽 / 1.96；两侧都碰到 ρ 盒约束 → None。
        不用 L-BFGS-B 的 hess_inv（修订 2026-10-08，rho_se_method=profile_kish）。"""
        from scipy.optimize import brentq

        warm = {rho_hat: np.asarray(theta_hat, dtype=float)}

        def prof(r: float) -> float:
            near = min(warm, key=lambda k: abs(k - r))
            res = self.solve(rho_fixed=r, theta_start=warm[near])
            warm[r] = res.x
            return float(res.fun)

        f0 = prof(rho_hat)
        crit = CHI2_1_95 / (2.0 * self.n_eff)
        lo_b, hi_b = self.rho_bounds

        def root(direction: int) -> float | None:
            bound = hi_b if direction > 0 else lo_b
            step, a = 0.01, rho_hat
            while True:
                b = rho_hat + direction * step
                if (direction > 0 and b >= bound) or (direction < 0 and b <= bound):
                    b = bound
                gb = prof(b) - f0 - crit
                if gb > 0:
                    return float(brentq(lambda r: prof(r) - f0 - crit, min(a, b), max(a, b), xtol=1e-5))
                if b == bound:
                    return None
                a, step = b, step * 2.0

        lo, hi = root(-1), root(+1)
        if lo is not None and hi is not None:
            se = (hi - lo) / (2.0 * Z_95)
        elif hi is not None:
            se = (hi - rho_hat) / Z_95
        elif lo is not None:
            se = (rho_hat - lo) / Z_95
        else:
            se = None
        if se is not None and not (math.isfinite(se) and se > 0):
            se = None
        return se, (lo, hi)

    def to_fit(self, res, *, converged: bool, message: str, tau_viol: int, **extra) -> LeagueFit:
        att, dfn, gamma, rho = self.unpack(res.x)
        return LeagueFit(
            league_id=self.league_id, teams=self.teams,
            attack={t: float(att[i]) for t, i in self.idx.items()},
            defence={t: float(dfn[i]) for t, i in self.idx.items()},
            home_adv=float(gamma), rho=float(rho), n_matches=self.n_rows, team_n=self.team_n,
            weight_sum=self.wsum, nll=float(res.fun), converged=converged, message=message,
            team_wn=self.team_wn, n_eff=self.n_eff, tau_ok=tau_viol == 0, n_tau_viol=tau_viol, **extra)


def _status(res, tau_viol: int) -> tuple[bool, str]:
    msg = str(res.message)
    if tau_viol:
        msg += f" | tau_floor_violation: {tau_viol} observed low-score matches with tau<=1e-12"
    return bool(res.success) and tau_viol == 0, msg


def _fit_league(league_id, rows, p, with_se: bool = True):
    """第一遍：ρ 与攻防参数联合拟合（rho_league），并按 profile_kish 求 rho_league_se。返回 (problem, res, LeagueFit)。"""
    prob = _LeagueProblem(league_id, rows, p)
    res = prob.solve()
    tv = prob.tau_violations(res.x)
    ok, msg = _status(res, tv)
    rho1 = float(prob.unpack(res.x)[3])
    se, ci = (prob.profile_se(res.x, rho1) if (with_se and ok) else (None, (None, None)))
    lf = prob.to_fit(res, converged=ok, message=msg, tau_viol=tv, rho_league=rho1, rho_league_se=se,
                     rho_league_ci=ci, pass1_converged=ok, pass1_message=msg)
    return prob, res, lf


def pool_rho(fits: Mapping[Hashable, LeagueFit], min_league_wn: float, re_switch_p: float = 0.05
             ) -> tuple[float | None, float | None, list, dict, dict]:
    """合成 ρ_global。返回 (ρ_global, SE, 进合成的联赛, 未进的联赛→原因, 异质性统计)。

    只用：联赛场次过门槛（已在 fits 里）、联赛 Σw ≥ min_league_wn、第一遍收敛且 τ 检查通过、SE 有限 > 0 的联赛。
    固定效应 IVW：w_l = 1/se_l²，ρ_FE = Σw·ρ/Σw，SE_FE = (Σw)^−½。
    异质性：Q = Σw·(ρ_l − ρ_FE)²，df = k−1，I² = max(0, (Q−df)/Q)，p = P(χ²_df ≥ Q)。
    p < re_switch_p → DerSimonian–Laird：τ² = max(0, (Q−df)/(Σw − Σw²/Σw))，w* = 1/(se²+τ²)，ρ_RE = Σw*·ρ/Σw*，
    SE_RE = (Σw*)^−½（≥ SE_FE）。k=1 时 df=0，Q/I²/p 记 0/0/None，用 FE。"""
    from scipy.stats import chi2

    used, excluded, rs, ses = [], {}, [], []
    for lg, lf in fits.items():
        if lf.weight_sum < float(min_league_wn):
            excluded[lg] = SUB_LEAGUE_WN
        elif not lf.pass1_converged:
            excluded[lg] = "pass1_not_converged"
        elif lf.rho_league_se is None or not (lf.rho_league_se > 0 and math.isfinite(lf.rho_league_se)):
            excluded[lg] = "se_unavailable"
        else:
            used.append(lg)
            rs.append(float(lf.rho_league))
            ses.append(float(lf.rho_league_se))
    stats: dict[str, Any] = {"rho_Q": None, "rho_df": None, "rho_I2": None, "rho_Q_p": None, "rho_tau2": None,
                             "rho_pool": None, "rho_global_fe": None, "rho_global_fe_se": None, "k": len(used)}
    if not used:
        return None, None, used, excluded, stats
    r, se = np.array(rs), np.array(ses)
    w = 1.0 / se ** 2
    sw = float(w.sum())
    fe = float(np.dot(w, r) / sw)
    fe_se = math.sqrt(1.0 / sw)
    k = len(used)
    df = k - 1
    Q = float(np.dot(w, (r - fe) ** 2))
    I2 = max(0.0, (Q - df) / Q) if Q > 0 else 0.0
    pval = float(chi2.sf(Q, df)) if df >= 1 else None
    stats.update(rho_Q=Q, rho_df=df, rho_I2=I2, rho_Q_p=pval, rho_global_fe=fe, rho_global_fe_se=fe_se)
    if pval is not None and pval < float(re_switch_p):
        c = sw - float((w ** 2).sum()) / sw
        tau2 = max(0.0, (Q - df) / c) if c > 0 else 0.0
        ws = 1.0 / (se ** 2 + tau2)
        rg, rg_se = float(np.dot(ws, r) / ws.sum()), math.sqrt(1.0 / float(ws.sum()))
        stats.update(rho_tau2=tau2, rho_pool="re_dl")
        return rg, rg_se, used, excluded, stats
    stats.update(rho_tau2=0.0, rho_pool="fe")
    return fe, fe_se, used, excluded, stats


def rho_snapshot(model: "DCModel") -> dict[str, Any]:
    """拟合快照里的 ρ 合成字段（每个竞彩日一份）：rho_global/SE、异质性 rho_Q/rho_df/rho_I2/rho_Q_p、τ²、方法、合成联赛。"""
    return {"rho_global": model.rho_global, "rho_global_se": model.rho_global_se,
            "rho_global_fe": model.rho_global_fe, "rho_global_fe_se": model.rho_global_fe_se,
            "rho_Q": model.rho_Q, "rho_df": model.rho_df, "rho_I2": model.rho_I2, "rho_Q_p": model.rho_Q_p,
            "rho_tau2": model.rho_tau2, "rho_pool": model.rho_pool_method, "rho_pool_k": len(model.rho_pool),
            "rho_pool_leagues": list(model.rho_pool), "rho_pool_excluded": dict(model.rho_pool_excluded)}


def fit(results: Iterable[Mapping[str, Any]], cutoff: Any, params: Mapping[str, Any] | None = None) -> DCModel:
    """按 cutoff 拟合所有联赛。cutoff 一般用 jc_day_training_cutoff(竞彩日)。

    两遍（修订 2026-10-08，rho=global_pooled_ivw）：
      1) 每个联赛联合拟合 ρ_league + profile_kish SE；
      2) 过门槛联赛按逆方差合成 ρ_global（只用本次 cutoff 之前的数据，每次调用重算）；
      3) 每个联赛把 ρ 固定为 ρ_global 重拟攻防/主场；第二遍也要 scipy 收敛 + 观测低比分场 τ > TAU_FLOOR，
         否则 fit_not_converged。没有任何联赛可合成时 ρ_global=None，全部联赛判 fit_not_converged。
    """
    p = dict(DEFAULT_PARAMS)
    if params:
        unknown = set(params) - set(DEFAULT_PARAMS)
        if unknown:
            raise ValueError(f"未知参数: {sorted(unknown)}")
        p.update(params)
    _require_tuned(p)
    cutoff_bj = to_bj(cutoff)
    by_league, n_in, dropped = _select(results, cutoff_bj, p)
    fp = dict(FINGERPRINT)
    fp.update(
        window_days=p["window_days"],
        half_life_days=p["half_life_days"],
        decay_xi_per_day=round(math.log(2) / float(p["half_life_days"]), 6),
        min_team_n=p["min_team_n"],
        min_league_n=p["min_league_n"],
        min_team_wn=p["min_team_wn"],
        min_league_wn=p["min_league_wn"],
        max_goals=p["max_goals"],
    )
    model = DCModel(cutoff=cutoff_bj, params=p, fingerprint=fp, n_input=n_in, n_dropped=dropped)
    first: dict[Hashable, tuple] = {}
    for lg in sorted(by_league, key=lambda v: (str(type(v)), str(v))):
        rows = by_league[lg]
        model.n_used += len(rows)
        if len(rows) < int(p["min_league_n"]):
            tn, tw = _team_counts(rows)
            model.insufficient[lg] = LeagueInsufficient(lg, len(rows), tn, weight_sum=float(sum(r[4] for r in rows)),
                                                        team_wn=tw)
            continue
        # 联赛 Σw 不够也照样拟合（便宜），但不进 ρ 合成；predict 时按 league_wn_lt_min 判不可评估
        first[lg] = _fit_league(lg, rows, p, with_se=True)
    rg, rg_se, used, excluded, st = pool_rho({lg: v[2] for lg, v in first.items()}, float(p["min_league_wn"]),
                                             float(FINGERPRINT["rho_re_switch_p"]))
    model.rho_global, model.rho_global_se, model.rho_pool, model.rho_pool_excluded = rg, rg_se, used, excluded
    model.rho_global_fe, model.rho_global_fe_se = st["rho_global_fe"], st["rho_global_fe_se"]
    model.rho_Q, model.rho_df, model.rho_I2, model.rho_Q_p = st["rho_Q"], st["rho_df"], st["rho_I2"], st["rho_Q_p"]
    model.rho_tau2, model.rho_pool_method = st["rho_tau2"], st["rho_pool"]
    model.fingerprint["rho_pool"] = st["rho_pool"]  # 当天实际用的方法：fe | re_dl（无可合成联赛时 None）
    for lg, (prob, res1, lf1) in first.items():
        if rg is None:
            lf1.converged = False
            lf1.message = (lf1.message + " | rho_global_unavailable: no league passed pooling gates")
            lf1.pooled = False
            model.leagues[lg] = lf1
            continue
        res2 = prob.solve(rho_fixed=rg, theta_start=res1.x)
        tv2 = prob.tau_violations(res2.x)
        ok2, msg2 = _status(res2, tv2)
        model.leagues[lg] = prob.to_fit(
            res2, converged=ok2, message=msg2, tau_viol=tv2, rho_league=lf1.rho_league,
            rho_league_se=lf1.rho_league_se, rho_league_ci=lf1.rho_league_ci, pass1_converged=lf1.pass1_converged,
            pass1_message=lf1.pass1_message, pooled=lg in used, second_pass=True)
    return model


# ---------------------------------------------------------------- 比分矩阵


def _rho_feasible_interval(lam: float, mu: float) -> tuple[float, float]:
    """四个低比分修正项都 ≥ 0 的 ρ 区间：1−λμρ≥0, 1+λρ≥0, 1+μρ≥0, 1−ρ≥0。"""
    lo = max(-1.0 / lam, -1.0 / mu)
    hi = min(1.0, 1.0 / (lam * mu))
    return lo, hi


def dc_matrix(lam: float, mu: float, rho: float, max_goals: int = 10) -> tuple[np.ndarray, float, bool]:
    """独立泊松 × τ 修正，截到 0..max_goals 后归一。返回 (矩阵, 实际用的 ρ, 是否被裁剪)。"""
    if not (lam > 0 and mu > 0 and math.isfinite(lam) and math.isfinite(mu)):
        raise ValueError(f"λ/μ 非法: {lam}, {mu}")
    lo, hi = _rho_feasible_interval(lam, mu)
    rho_used = min(max(rho, lo), hi)
    clipped = rho_used != rho
    k = np.arange(max_goals + 1)
    logfact = np.array([math.lgamma(i + 1) for i in k])
    ph = np.exp(k * math.log(lam) - lam - logfact)
    pa = np.exp(k * math.log(mu) - mu - logfact)
    m = np.outer(ph, pa)
    tau = np.array(
        [1.0 - lam * mu * rho_used, 1.0 + lam * rho_used, 1.0 + mu * rho_used, 1.0 - rho_used]
    )
    assert (tau >= -1e-12).all(), f"τ 出现负值: {tau} (ρ={rho_used}, λ={lam}, μ={mu})"
    tau = np.maximum(tau, 0.0)
    m[0, 0] *= tau[0]
    m[0, 1] *= tau[1]
    m[1, 0] *= tau[2]
    m[1, 1] *= tau[3]
    s = m.sum()
    assert s > 0
    m = m / s
    assert (m >= 0).all() and abs(m.sum() - 1.0) < 1e-9
    return m, rho_used, clipped


def predict(model: DCModel, league_id: Hashable, home: Hashable, away: Hashable, max_goals: int | None = None,
            league_whitelist: Sequence[Hashable] | None = None) -> Prediction:
    """不可评估时 evaluable=False、reason/subreason 与后端 shadow_evaluable 子原因一致。

    league_whitelist：给了就先判范围，不在白名单 → model_scope/non_league（杯赛/国际赛由调用方细分子原因）。
    """
    mg = int(max_goals if max_goals is not None else model.params["max_goals"])
    if league_whitelist is not None and league_id not in set(league_whitelist):
        return Prediction(False, reason=REASON_MODEL_SCOPE, subreason=SUB_NON_LEAGUE)
    lf = model.leagues.get(league_id)
    if lf is None:
        ins = model.insufficient.get(league_id)
        return Prediction(False, reason=REASON_MODEL_INSUFFICIENT, subreason=SUB_LEAGUE_N,
                          league_n=ins.n_matches if ins else 0, league_w_n=ins.weight_sum if ins else 0.0,
                          rho_global=model.rho_global)
    hn, an = lf.team_n.get(home, 0), lf.team_n.get(away, 0)
    hw, aw = lf.team_wn.get(home, 0.0), lf.team_wn.get(away, 0.0)
    info = dict(home_n=hn, away_n=an, league_n=lf.n_matches, home_w_n=hw, away_w_n=aw,
                league_w_n=lf.weight_sum, fit_message=lf.message, rho_league=lf.rho_league,
                rho_league_se=lf.rho_league_se, rho_global=model.rho_global, rho_fitted=lf.rho_league)
    # 判定顺序：联赛场次（上面）→ 联赛 Σw → 收敛 → 球队场次 → 球队 Σw
    if lf.weight_sum < float(model.params["min_league_wn"]):
        return Prediction(False, reason=REASON_MODEL_INSUFFICIENT, subreason=SUB_LEAGUE_WN, **info)
    if not lf.converged:
        return Prediction(False, reason=REASON_MODEL_INSUFFICIENT, subreason=SUB_NOT_CONVERGED, **info)
    mn = int(model.params["min_team_n"])
    if hn < mn or an < mn:
        return Prediction(False, reason=REASON_MODEL_INSUFFICIENT, subreason=SUB_TEAM_N, **info)
    mw = float(model.params["min_team_wn"])
    if hw < mw or aw < mw:
        return Prediction(False, reason=REASON_MODEL_INSUFFICIENT, subreason=SUB_TEAM_WN, **info)
    lam, mu = lf.lambdas(home, away)
    # 出矩阵用 ρ_global（第二遍已固定 lf.rho = ρ_global），仍按本场 λ、μ 裁到 τ ≥ 0 区间
    m, rho_used, clipped = dc_matrix(lam, mu, lf.rho, mg)
    return Prediction(True, matrix=m, lambda_home=lam, lambda_away=mu, rho_used=rho_used,
                      rho_clipped=clipped, **info)


def score_matrix(model: DCModel, league_id: Hashable, home: Hashable, away: Hashable, max_goals: int = 10) -> np.ndarray:
    """(max_goals+1)×(max_goals+1) 矩阵，[i, j] = P(主 i 球, 客 j 球)，总和 1、无负数。不可评估抛 NotEvaluable。"""
    pr = predict(model, league_id, home, away, max_goals)
    if not pr.evaluable:
        detail = f"league={league_id} home={home} away={away}"
        if pr.subreason == SUB_NOT_CONVERGED:
            detail += f" fit_message={pr.fit_message}"
        raise NotEvaluable(pr.reason, pr.subreason, detail, fit_message=pr.fit_message)
    return pr.matrix


# ---------------------------------------------------------------- 亚盘 / 去水 / 共识


def ah_sub_lines(line: float) -> list[float]:
    """拆子盘：整数/半球盘一条；四分之一盘拆成 line−0.25、line+0.25 两条（与 backend quarter_split 同）。"""
    q4 = line * 4.0
    if abs(q4 - round(q4)) > 1e-9:
        raise ValueError(f"盘口必须是 0.25 的整数倍: {line}")
    if int(round(q4)) % 2 == 0:
        return [float(line)]
    return [line - 0.25, line + 0.25]


def goal_diff_probs(matrix: np.ndarray) -> dict[int, float]:
    m = np.asarray(matrix, dtype=float)
    out: dict[int, float] = {}
    for i in range(m.shape[0]):
        for j in range(m.shape[1]):
            if m[i, j]:
                out[i - j] = out.get(i - j, 0.0) + float(m[i, j])
    return out


def ah_win_push_lose(matrix: np.ndarray, line: float) -> tuple[float, float, float]:
    """主队一侧的 (W, P, L) 加总（四分之一盘为两条子盘之和，每条子盘权重 1 → 总量 2）。"""
    diffs = goal_diff_probs(matrix)
    W = P = L = 0.0
    for sl in ah_sub_lines(line):
        for d, pr in diffs.items():
            margin = d - sl  # 主队视角：margin = (主 − 客) − line
            if margin > 1e-12:
                W += pr
            elif margin < -1e-12:
                L += pr
            else:
                P += pr
    return W, P, L


def ah_cover_prob(matrix: np.ndarray, line: float) -> float:
    """主队有效赢盘概率 q（保本概率口径）。line 为主队视角，正数 = 主让。客队一侧 = 1 − q。

    四分之一盘合并算：q = (W1+W2) / ((W1+W2)+(L1+L2))，走盘不进分母（combined_wl）。
    """
    W, _P, L = ah_win_push_lose(matrix, line)
    if W + L <= 0:
        raise ValueError("W+L=0，无法计算有效赢盘概率")
    return W / (W + L)


def hk_to_decimal(hk: float) -> float:
    """港赔 → 欧赔小数（+1）。"""
    return float(hk) + 1.0


def devig_2way(odds_home_decimal: float, odds_away_decimal: float) -> tuple[float, float]:
    """两路比例法（multiplicative_2way）：p_home = (1/o_h) / (1/o_h + 1/o_a)。"""
    oh, oa = float(odds_home_decimal), float(odds_away_decimal)
    if not (oh > 1.0 and oa > 1.0):
        raise ValueError(f"欧赔必须 > 1: {oh}, {oa}")
    ih, ia = 1.0 / oh, 1.0 / oa
    s = ih + ia
    return ih / s, ia / s


def median_consensus(values: Iterable[float]) -> float:
    """中位数；偶数个取中间两家的平均。忽略 None / NaN；空则抛错。"""
    v = sorted(float(x) for x in values if x is not None and not (isinstance(x, float) and math.isnan(x)))
    if not v:
        raise ValueError("没有有效值")
    n = len(v)
    mid = n // 2
    return v[mid] if n % 2 else (v[mid - 1] + v[mid]) / 2.0


def n5_consensus(edges: Mapping[str, float | None] | Iterable[float | None], min_books: int = 3) -> tuple[float | None, int]:
    """N5：各家偏差 e_b 取中位数。返回 (edge_median, n_books)；不足 min_books 家返回 (None, n) → market_insufficient。"""
    vals = list(edges.values()) if isinstance(edges, Mapping) else list(edges)
    vals = [float(x) for x in vals if x is not None and not math.isnan(float(x))]
    if len(vals) < min_books:
        return None, len(vals)
    return median_consensus(vals), len(vals)


def decide_direction(edge: float, delta: float | None = None) -> str:
    """口径卡 §5：edge ≥ +δ 买主，≤ −δ 买客，其余未触发（可评估）。"""
    if delta is None:
        if _N5.get("edge_delta") is None:
            raise StrategyParamsMissing("N5.edge_delta 未配置（见 config/strategy_params.example.json）")
        delta = float(_N5["edge_delta"])
    if edge >= delta - 1e-12:
        return "主"
    if edge <= -delta + 1e-12:
        return "客"
    return "不下注"


__all__ = [
    "BJ", "DEFAULT_PARAMS", "FINGERPRINT", "StrategyParamsMissing", "DCModel", "LeagueFit", "LeagueInsufficient", "Prediction",
    "NotEvaluable", "fit", "predict", "pool_rho", "rho_snapshot", "score_matrix", "dc_matrix", "ah_sub_lines", "ah_win_push_lose",
    "ah_cover_prob", "hk_to_decimal", "devig_2way", "median_consensus", "n5_consensus", "decide_direction",
    "jc_day_training_cutoff", "to_bj",
]
