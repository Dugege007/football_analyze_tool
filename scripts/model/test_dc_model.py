# -*- coding: utf-8 -*-
"""dc_model.py 单测（pytest）。运行：$ODDS_DATA_DIR/python -m pytest -q scripts/test_dc_model.py"""
from __future__ import annotations
# --- public repo: paths are env-overridable (see config.example.env) ---
import os as _rp_os
from pathlib import Path as _RpPath
_REPO_ROOT = _RpPath(__file__).resolve().parents[2]
def _rp_load_dotenv(path=_REPO_ROOT / '.env'):
    # 读仓库根 .env（KEY=VALUE；已存在的环境变量优先，不覆盖）；不打印任何值
    if _rp_os.environ.get('FAT_DISABLE_DOTENV') == '1':  # 测试时由 conftest 设置，避免本机 .env 干扰
        return
    try:
        for _ln in path.read_text(encoding='utf-8').splitlines():
            _ln = _ln.strip()
            if not _ln or _ln.startswith('#') or '=' not in _ln:
                continue
            _k, _v = _ln.split('=', 1)
            _k = _k.strip().removeprefix('export ').strip()
            _v = _v.strip().strip('"').strip("'")
            if _v.startswith('YOUR_'):  # config.example.env 占位值视为未填写
                continue
            if _k and _k not in _rp_os.environ:
                _rp_os.environ[_k] = _v
    except FileNotFoundError:
        pass
_rp_load_dotenv()

# --- public repo: N5 调好的参数不随仓库公开；从 config/strategy_params.json 读取，未配置则 skip ---
def _P(key):
    import pytest as _pt
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location("_fat_sp", _REPO_ROOT / "api/app/strategy_params.py")
    _m = _ilu.module_from_spec(_spec); _spec.loader.exec_module(_m)
    v = _m.get_or_none("N5", key)
    if v is None:
        _pt.skip("strategy params not configured (N5.%s)；见 docs/STRATEGY_PARAMS.md" % key)
    return v
def _rp_env(name, default, base=None):
    v = _rp_os.environ.get(name, '').strip()
    p = _RpPath(v).expanduser() if v else default
    return p if p.is_absolute() else (base or _REPO_ROOT) / p
_MA_API_ROOT = _rp_env('MA_API_ROOT', _REPO_ROOT / 'api')
_ODDS_DATA_DIR = _rp_env('ODDS_DATA_DIR', _REPO_ROOT / 'data' / 'odds-data')
_APP_DB = _rp_env('APP_DB_PATH', _MA_API_ROOT / 'data' / 'app.db', _MA_API_ROOT)
_V2D3_DB = _rp_env('V2D3_DB_PATH', _MA_API_ROOT / 'data' / 'v2d3' / 'app.db', _MA_API_ROOT)
_BACKUP_DIR = _rp_env('BACKUP_DIR', _REPO_ROOT / 'backups' / 'football')
# --- end path config ---


import math
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dc_model as dc  # noqa: E402

BJ = dc.BJ
CUTOFF = datetime(2026, 10, 8, 0, 0, tzinfo=BJ)


# ---------------------------------------------------------------- 合成数据


def make_true_params(n_teams=20, seed=1):
    rng = np.random.default_rng(seed)
    att = rng.normal(0, 0.30, n_teams)
    att -= att.mean()
    dfn = rng.normal(0, 0.20, n_teams) + math.log(1.25)
    teams = [f"T{i:02d}" for i in range(n_teams)]
    return teams, dict(zip(teams, att)), dict(zip(teams, dfn))


def simulate_league(league_id="L1", n_teams=20, seasons=2, home_adv=0.25, rho=-0.10, seed=1,
                    cutoff=CUTOFF, rounds_per_season=2, span_days=700):
    """生成一个联赛的完赛场次：每季双循环（或更多轮），比分按 DC 矩阵抽样。"""
    teams, att, dfn = make_true_params(n_teams, seed)
    rng = np.random.default_rng(seed + 100)
    pairs = [(h, a) for h in teams for a in teams if h != a]
    fixtures = []
    for _s in range(seasons):
        for _r in range(rounds_per_season // 2):
            fixtures.extend(pairs)
    n = len(fixtures)
    # 场次均匀铺在 [cutoff − span_days, cutoff − 1 天]
    rows = []
    for k, (h, a) in enumerate(fixtures):
        age = 1 + (span_days - 1) * (n - 1 - k) / max(n - 1, 1)
        ko = cutoff - timedelta(days=age)
        lam = math.exp(att[h] + dfn[a] + home_adv)
        mu = math.exp(att[a] + dfn[h])
        m, _, _ = dc.dc_matrix(lam, mu, rho, 10)
        flat = m.ravel()
        idx = rng.choice(flat.size, p=flat / flat.sum())
        hg, ag = divmod(int(idx), 11)
        rows.append(dict(league_id=league_id, kickoff_at=ko, home_team=h, away_team=a,
                         home_goals=hg, away_goals=ag, status="finished"))
    return rows, dict(att=att, dfn=dfn, home_adv=home_adv, rho=rho, teams=teams)


@pytest.fixture(scope="module")
def league_rows():
    rows, truth = simulate_league(seed=7, rounds_per_season=4)  # 2 季 × 4 轮 × 380 = 1520 场
    return rows, truth


@pytest.fixture(scope="module")
def fitted(league_rows):
    rows, truth = league_rows
    return dc.fit(rows, CUTOFF), truth


# ---------------------------------------------------------------- (a) 约束与稳定


def test_attack_mean_zero_and_refit_stable(league_rows, fitted):
    model, _ = fitted
    lf = model.leagues["L1"]
    assert lf.converged, lf.message
    assert abs(np.mean(list(lf.attack.values()))) < 1e-10
    rows, _ = league_rows
    m2 = dc.fit(rows, CUTOFF)
    l2 = m2.leagues["L1"]
    for t in lf.teams:
        assert l2.attack[t] == lf.attack[t] and l2.defence[t] == lf.defence[t]
    assert l2.rho == lf.rho and l2.home_adv == lf.home_adv
    # 打乱输入顺序再拟合：结果只在优化器容差内浮动
    shuffled = rows[:]
    random.Random(3).shuffle(shuffled)
    l3 = dc.fit(shuffled, CUTOFF).leagues["L1"]
    for t in lf.teams:
        assert abs(l3.attack[t] - lf.attack[t]) < 1e-4
        assert abs(l3.defence[t] - lf.defence[t]) < 1e-4
    assert abs(l3.rho - lf.rho) < 1e-4 and abs(l3.home_adv - lf.home_adv) < 1e-4


def test_weights_recomputed_per_cutoff(league_rows):
    rows, _ = league_rows
    m1 = dc.fit(rows, CUTOFF)
    m2 = dc.fit(rows, CUTOFF + timedelta(days=30))
    assert m1.leagues["L1"].weight_sum != m2.leagues["L1"].weight_sum
    # 半衰期：恰好 half_life_days 天前的一场权重 = 0.5
    one = [dict(league_id="X", kickoff_at=CUTOFF - timedelta(days=_P("half_life_days")), home_team="a", away_team="b",
                home_goals=1, away_goals=0, status="finished")]
    by, _, _ = dc._select(one, CUTOFF, dc.DEFAULT_PARAMS)
    assert abs(by["X"][0][4] - 0.5) < 1e-12


# ---------------------------------------------------------------- (b) 比分矩阵合法


@pytest.mark.parametrize("rho", [-0.3, -0.2, -0.1, -0.05, 0.0, 0.05, 0.1, 0.2, 0.3, -0.9, 0.9])
@pytest.mark.parametrize("lam,mu", [(0.3, 0.2), (1.4, 1.1), (2.8, 0.6), (4.5, 3.5), (0.9, 4.0)])
def test_dc_matrix_nonneg_sum1(lam, mu, rho):
    m, rho_used, clipped = dc.dc_matrix(lam, mu, rho, 10)
    assert m.shape == (11, 11)
    assert (m >= 0).all()
    assert abs(m.sum() - 1.0) < 1e-12
    lo, hi = dc._rho_feasible_interval(lam, mu)
    assert lo - 1e-15 <= rho_used <= hi + 1e-15
    assert clipped == (rho_used != rho)
    tau = [1 - lam * mu * rho_used, 1 + lam * rho_used, 1 + mu * rho_used, 1 - rho_used]
    assert min(tau) >= -1e-12


@pytest.mark.parametrize("true_rho", [-0.2, -0.05, 0.0, 0.12])
def test_fitted_matrices_valid_over_rho_range(true_rho):
    rows, truth = simulate_league(seed=11, rho=true_rho, rounds_per_season=2)
    model = dc.fit(rows, CUTOFF)
    lf = model.leagues["L1"]
    lo, hi = model.params["rho_bounds"]
    assert lo <= lf.rho <= hi
    for h in truth["teams"][:6]:
        for a in truth["teams"][-6:]:
            if h == a:
                continue
            m = dc.score_matrix(model, "L1", h, a)
            assert (m >= 0).all() and abs(m.sum() - 1) < 1e-9


# ---------------------------------------------------------------- (c) 亚盘有效赢盘概率


def tiny_matrix():
    m = np.zeros((11, 11))
    m[1, 0] = 0.45  # 净胜 +1
    m[0, 0] = 0.27  # 平
    m[0, 1] = 0.28  # 净负 −1
    return m


def test_quarter_line_combined():
    q = dc.ah_cover_prob(tiny_matrix(), 0.25)
    assert abs(q - 0.90 / 1.73) < 1e-12
    assert abs(q - 0.520) < 0.0005
    # 不是对子盘取平均（0.533）
    assert abs(q - (0.45 / 0.73 + 0.45) / 2) > 0.01


def test_integer_and_half_lines():
    m = tiny_matrix()
    assert abs(dc.ah_cover_prob(m, 0) - 0.45 / 0.73) < 1e-12
    assert abs(dc.ah_cover_prob(m, 0.5) - 0.45) < 1e-12
    assert abs(dc.ah_cover_prob(m, -0.5) - 0.72) < 1e-12  # 主受让半球：赢或平都赢盘
    assert abs(dc.ah_cover_prob(m, 1.0) - 0.0) < 1e-12  # 主让一球：+1 走盘，其余输
    # 主受让 0.25（−0.25 拆成 −0.5 和 0）：W = 0.72 + 0.45，L = 0.28 + 0.28
    assert abs(dc.ah_cover_prob(m, -0.25) - 1.17 / (1.17 + 0.56)) < 1e-12
    # 0.75 拆成 0.5 和 1：W = 0.45 + 0，L = 0.55 + 0.55
    assert abs(dc.ah_cover_prob(m, 0.75) - 0.45 / 1.55) < 1e-12
    with pytest.raises(ValueError):
        dc.ah_cover_prob(m, 0.3)


def test_sub_lines_match_backend_quarter_split():
    """与后端结算 app/backtest.py::quarter_split 拆法一致（后端不可导入时跳过）。"""
    sys.path.insert(0, str(_MA_API_ROOT))
    bt = pytest.importorskip("app.backtest")
    for k in range(-16, 17):
        line = k / 4
        ours = sorted(dc.ah_sub_lines(line))
        theirs = sorted(l for l, _w in bt.quarter_split(line))
        assert ours == theirs, line
        # 逐个比分结算方向一致：W/L 计数等价于 settle_code 的 win_w/lose_w
        for hg in range(5):
            for ag in range(5):
                m = np.zeros((11, 11))
                m[hg, ag] = 1.0
                W, _P, L = dc.ah_win_push_lose(m, line)
                code, _ = bt.settle_code(hg, ag, line, "主")
                n = len(ours)
                exp = {"win": (n, 0), "lose": (0, n), "win_half": (1, 0), "lose_half": (0, 1), "push": (0, 0)}[code]
                assert (round(W), round(L)) == exp, (line, hg, ag, code)


def test_devig_and_consensus():
    ph, pa = dc.devig_2way(dc.hk_to_decimal(0.90), dc.hk_to_decimal(0.98))
    assert abs(ph + pa - 1) < 1e-12
    assert abs(ph - (1 / 1.90) / (1 / 1.90 + 1 / 1.98)) < 1e-12
    assert dc.median_consensus([0.01, 0.05, 0.02]) == 0.02
    assert abs(dc.median_consensus([0.01, 0.05, 0.02, 0.04]) - 0.03) < 1e-12
    assert dc.n5_consensus({"macau": 0.01, "pinnacle": 0.04}) == (None, 2)
    e, n = dc.n5_consensus({"macau": 0.01, "pinnacle": 0.04, "crown": 0.02, "william": None})
    assert n == 3 and e == 0.02
    _d = _P("edge_delta")
    assert dc.decide_direction(_d) == "主" and dc.decide_direction(-_d) == "客"
    assert dc.decide_direction(_d - 1e-4) == "不下注"


# ---------------------------------------------------------------- (d) 防泄漏


def test_leakage_recent_match_ignored(league_rows):
    rows, _ = league_rows
    base = dc.fit(rows, CUTOFF).leagues["L1"]
    leak = dict(league_id="L1", kickoff_at=CUTOFF - timedelta(hours=2, minutes=59), home_team="T00",
                away_team="T01", home_goals=9, away_goals=0, status="finished")
    future = dict(leak, kickoff_at=CUTOFF + timedelta(days=1))
    unfinished = dict(leak, kickoff_at=CUTOFF - timedelta(days=3), status="postponed")
    m2 = dc.fit(rows + [leak, future, unfinished], CUTOFF)
    l2 = m2.leagues["L1"]
    assert m2.n_dropped["too_recent"] == 2 and m2.n_dropped["not_finished"] == 1
    for t in base.teams:
        assert l2.attack[t] == base.attack[t] and l2.defence[t] == base.defence[t]
    assert l2.rho == base.rho and l2.home_adv == base.home_adv
    assert l2.team_n == base.team_n
    # 边界：开赛 + 3h 恰好 = cutoff 的场要进训练
    edge = dict(leak, kickoff_at=CUTOFF - timedelta(hours=3))
    l3 = dc.fit(rows + [edge], CUTOFF).leagues["L1"]
    assert l3.n_matches == base.n_matches + 1
    assert l3.attack["T00"] != base.attack["T00"]


def test_window_and_tz_handling(league_rows):
    rows, _ = league_rows
    base = dc.fit(rows, CUTOFF).leagues["L1"]
    old = dict(league_id="L1", kickoff_at=CUTOFF - timedelta(days=731), home_team="T00",
               away_team="T01", home_goals=9, away_goals=0, status="finished")
    assert dc.fit(rows + [old], CUTOFF).leagues["L1"].attack == base.attack
    # 不带时区按北京时间；UTC 时间等价
    naive = (CUTOFF - timedelta(hours=2)).replace(tzinfo=None)
    assert dc.to_bj(naive) == CUTOFF - timedelta(hours=2)
    assert dc.to_bj("2026-10-07T16:00:00Z") == CUTOFF
    assert dc.jc_day_training_cutoff("2026-10-08") == CUTOFF


# ---------------------------------------------------------------- (e) 样本门槛


def test_model_insufficient_gates(league_rows):
    rows, _ = league_rows
    # 联赛 < min_league_n 场
    _ln = int(_P("min_league_n"))
    small = [dict(r, league_id="S") for r in rows[-(_ln - 1):]]
    # 升班马：窗口内只有 6 场
    newbie = [dict(league_id="L1", kickoff_at=CUTOFF - timedelta(days=10 + i), home_team="NEW",
                   away_team=f"T{i:02d}", home_goals=1, away_goals=1, status="finished") for i in range(6)]
    model = dc.fit(rows + small + newbie, CUTOFF)
    assert "S" in model.insufficient and "S" not in model.leagues
    pr = dc.predict(model, "S", rows[-1]["home_team"], rows[-1]["away_team"])
    assert not pr.evaluable and pr.reason == "model_insufficient" and pr.subreason == "league_n_lt_min"
    assert pr.league_n == _ln - 1
    pr = dc.predict(model, "L1", "NEW", "T05")
    assert not pr.evaluable and pr.reason == "model_insufficient" and pr.subreason == "team_n_lt_min"
    assert pr.home_n == 6
    pr = dc.predict(model, "L1", "T00", "UNKNOWN")
    assert not pr.evaluable and pr.subreason == "team_n_lt_min"
    pr = dc.predict(model, "NOPE", "T00", "T01")
    assert not pr.evaluable and pr.subreason == "league_n_lt_min"
    with pytest.raises(dc.NotEvaluable) as ei:
        dc.score_matrix(model, "L1", "NEW", "T05")
    assert ei.value.reason == "model_insufficient"
    pr = dc.predict(model, "CUP", "T00", "T01", league_whitelist=["L1"])
    assert pr.reason == "model_scope"
    # 恰好 min_league_n 场的联赛可以拟合
    ok_min = [dict(r, league_id="M") for r in rows[-_ln:]]
    assert "M" in dc.fit(ok_min, CUTOFF).leagues
    assert dc.predict(model, "L1", "T00", "T01").evaluable


# ---------------------------------------------------------------- (f) 参数回收


def test_synthetic_recovery(fitted):
    model, truth = fitted
    lf = model.leagues["L1"]
    teams = truth["teams"]
    ta = np.array([truth["att"][t] for t in teams])
    fa = np.array([lf.attack[t] for t in teams])
    td = np.array([truth["dfn"][t] for t in teams])
    fd = np.array([lf.defence[t] for t in teams])
    assert np.corrcoef(ta, fa)[0, 1] > 0.85
    assert np.corrcoef(td, fd)[0, 1] > 0.7
    assert np.abs(ta - fa).max() < 0.3
    assert abs(td.mean() - fd.mean()) < 0.1  # 总体进球水平
    assert abs(lf.home_adv - truth["home_adv"]) < 0.1
    assert abs(lf.rho - truth["rho"]) < 0.1


def test_fingerprint_fields():
    fp = dc.FINGERPRINT
    for k, v in dict(p_model_method="dixon_coles_v1", window_days=_P("window_days"), half_life_days=_P("half_life_days"), min_team_n=_P("min_team_n"),
                     min_league_n=_P("min_league_n"), scope="league_only", quarter_line="combined_wl",
                     fit_cadence="per_jc_day_prev_days", devig_method="multiplicative_2way",
                     compare_space="ah_effective_cover", max_tick_age_min=60, delta=_P("edge_delta")).items():
        assert fp[k] == v, k


# ---------------------------------------------------------------- 独立似然核对（不依赖模块里的解析梯度）


def _independent_nll(rows, cutoff, attack, defence, gamma, rho):
    xi = math.log(2) / _P("half_life_days")
    tot = wsum = 0.0
    for r in rows:
        age = (cutoff - r["kickoff_at"]).total_seconds() / 86400
        w = math.exp(-xi * age)
        h, a, x, y = r["home_team"], r["away_team"], r["home_goals"], r["away_goals"]
        lam = math.exp(attack[h] + defence[a] + gamma)
        mu = math.exp(attack[a] + defence[h])
        tau = {(0, 0): 1 - lam * mu * rho, (0, 1): 1 + lam * rho, (1, 0): 1 + mu * rho, (1, 1): 1 - rho}.get((x, y), 1.0)
        ll = x * math.log(lam) - lam + y * math.log(mu) - mu + math.log(tau)
        tot += w * ll
        wsum += w
    return -tot / wsum


def test_fit_is_local_optimum_of_independent_likelihood(league_rows, fitted):
    rows, _ = league_rows
    model, _ = fitted
    lf = model.leagues["L1"]
    base = _independent_nll(rows, CUTOFF, lf.attack, lf.defence, lf.home_adv, lf.rho)
    assert abs(base - lf.nll) < 1e-9
    eps = 1e-3
    for sgn in (1, -1):
        assert _independent_nll(rows, CUTOFF, lf.attack, lf.defence, lf.home_adv + sgn * eps, lf.rho) >= base - 1e-10
        assert _independent_nll(rows, CUTOFF, lf.attack, lf.defence, lf.home_adv, lf.rho + sgn * eps) >= base - 1e-10
        for t in ["T00", "T05", "T13"]:
            att = dict(lf.attack)
            att[t] += sgn * eps
            att["T19"] -= sgn * eps  # 保持进攻均值 0
            assert _independent_nll(rows, CUTOFF, att, lf.defence, lf.home_adv, lf.rho) >= base - 1e-10
            dfn = dict(lf.defence)
            dfn[t] += sgn * eps
            assert _independent_nll(rows, CUTOFF, lf.attack, dfn, lf.home_adv, lf.rho) >= base - 1e-10


# ---------------------------------------------------------------- 修订 2026-10-08（算法顾问审查）


def test_fit_not_converged_is_not_evaluable(league_rows, monkeypatch):
    rows, _ = league_rows
    real = dc.minimize

    def fake_minimize(*a, **k):
        res = real(*a, **dict(k, options={"maxiter": 2}))
        res.success = False
        res.message = "STOP: TOTAL NO. OF ITERATIONS REACHED LIMIT"
        return res

    monkeypatch.setattr(dc, "minimize", fake_minimize)
    model = dc.fit(rows, CUTOFF)
    lf = model.leagues["L1"]
    assert not lf.converged
    pr = dc.predict(model, "L1", "T00", "T01")
    assert not pr.evaluable
    assert pr.reason == "model_insufficient" and pr.subreason == "fit_not_converged"
    assert pr.fit_message.startswith("STOP: TOTAL NO. OF ITERATIONS REACHED LIMIT")
    with pytest.raises(dc.NotEvaluable) as ei:
        dc.score_matrix(model, "L1", "T00", "T01")
    assert ei.value.subreason == "fit_not_converged" and "ITERATIONS" in ei.value.fit_message


def test_team_weighted_gate_old_matches(league_rows):
    rows, _ = league_rows
    # 19 场都在约 600 天前：场次够 min_team_n，但 Σw 低于 min_team_wn
    _twn = _P("min_team_wn")
    old = [dict(league_id="L1", kickoff_at=CUTOFF - timedelta(days=600 + i * 0.5), home_team="OLD" if i % 2 else f"T{i:02d}",
                away_team=f"T{i:02d}" if i % 2 else "OLD", home_goals=1, away_goals=1, status="finished") for i in range(19)]
    model = dc.fit(rows + old, CUTOFF)
    lf = model.leagues["L1"]
    assert lf.team_n["OLD"] == 19 and lf.team_wn["OLD"] < _twn
    pr = dc.predict(model, "L1", "OLD", "T05")
    assert not pr.evaluable and pr.reason == "model_insufficient" and pr.subreason == "team_wn_lt_min"
    assert pr.home_n == 19 and abs(pr.home_w_n - lf.team_wn["OLD"]) < 1e-12 and pr.away_w_n >= _twn
    ok = dc.predict(model, "L1", "T00", "T05")
    assert ok.evaluable and ok.home_w_n >= _twn and ok.league_w_n == lf.weight_sum


def test_league_weighted_gate():
    # 200 场（够 min_league_n），但全在 500–700 天前 → 联赛 Σw 远低于 min_league_wn
    _ln, _lwn = _P("min_league_n"), _P("min_league_wn")
    rows, _ = simulate_league(seed=5, n_teams=10, rounds_per_season=2, seasons=1, span_days=200)
    rows = [dict(r, kickoff_at=r["kickoff_at"] - timedelta(days=500)) for r in rows]
    rows = rows + [dict(r, kickoff_at=r["kickoff_at"] - timedelta(days=1)) for r in rows[:110]]
    model = dc.fit(rows, CUTOFF)
    lf = model.leagues["L1"]
    assert lf.n_matches >= _P("min_league_n") and lf.weight_sum < _P("min_league_wn")
    pr = dc.predict(model, "L1", "T00", "T01")
    assert not pr.evaluable and pr.subreason == "league_wn_lt_min" and pr.league_w_n == lf.weight_sum


def test_penalty_continuous_at_tau_floor():
    """观测 (0,0) 场的 τ = 1 − λμρ 穿过 TAU_FLOOR 时，目标函数连续（没有 +1 跳变）。"""
    n = 2
    hi, ai = np.array([0]), np.array([1])
    x, y, wn = np.array([0.0]), np.array([0.0]), np.array([1.0])
    f, _unpack = dc._make_objective(hi, ai, x, y, wn, n)
    # θ = [att0, def0, def1, γ, ρ]；取 λ = μ = 1 → τ00 = 1 − ρ，令 ρ 跨过 1 − TAU_FLOOR
    base = np.array([0.0, 0.0, 0.0, 0.0, 0.0])
    # τ 取 TAU_FLOOR×(1±1e-3)：两侧都只差 ~1e-3（log 的自然斜率），旧版会在越界一侧跳 +1
    def at_tau(tau):
        th = base.copy()
        th[4] = 1.0 - tau
        return f(th)[0]

    f0 = at_tau(dc.TAU_FLOOR)
    f_in = at_tau(dc.TAU_FLOOR * (1 + 1e-3))
    f_out = at_tau(dc.TAU_FLOOR * (1 - 1e-3))
    assert abs(f_in - f0) < 5e-3 and abs(f_out - f0) < 5e-3, (f_in, f0, f_out)
    # 越界越远，罚项单调变大
    th1, th2 = base.copy(), base.copy()
    th1[4], th2[4] = 1.0 + 1e-3, 1.0 + 1e-2
    assert f(th2)[0] > f(th1)[0] > f(base)[0]


def test_fingerprint_revision_fields():
    fp = dc.FINGERPRINT
    assert fp["min_team_wn"] == _P("min_team_wn") and fp["min_league_wn"] == _P("min_league_wn") and fp["converge_check"] is True


# ---------------------------------------------------------------- 修订 2026-10-08：全局 ρ（IVW）+ 罚项收尾


def test_fingerprint_rho_fields():
    fp = dc.FINGERPRINT
    assert fp["rho"] == "global_pooled_ivw" and fp["rho_se_method"] == "profile_kish"
    assert fp["tau_end_check"] is True and fp["rho_second_pass"] == "refit_fixed_rho_global"


def test_pool_rho_hand_computed_toy():
    from types import SimpleNamespace as NS
    fits = {
        "A": NS(rho_league=-0.10, rho_league_se=0.05, weight_sum=200.0, pass1_converged=True),
        "B": NS(rho_league=0.00, rho_league_se=0.10, weight_sum=_P("min_league_wn") * 2.0, pass1_converged=True),
        "C": NS(rho_league=0.05, rho_league_se=0.02, weight_sum=300.0, pass1_converged=True),
        "D": NS(rho_league=0.25, rho_league_se=0.01, weight_sum=_P("min_league_wn") * 0.66, pass1_converged=True),   # Σw < min_league_wn → 不进
        "E": NS(rho_league=-0.25, rho_league_se=0.01, weight_sum=500.0, pass1_converged=False),  # 第一遍没收敛 → 不进
        "F": NS(rho_league=0.10, rho_league_se=None, weight_sum=500.0, pass1_converged=True),   # SE 不可用 → 不进
    }
    w = {"A": 1 / 0.05**2, "B": 1 / 0.10**2, "C": 1 / 0.02**2}  # 400, 100, 2500
    hand = (w["A"] * -0.10 + w["B"] * 0.0 + w["C"] * 0.05) / sum(w.values())  # (−40 + 0 + 125) / 3000
    assert abs(hand - 85 / 3000) < 1e-15
    rg, se, used, excl, st = dc.pool_rho(fits, _P("min_league_wn"))
    assert abs(st["rho_global_fe"] - hand) < 1e-12 and abs(st["rho_global_fe_se"] - (1 / sum(w.values())) ** 0.5) < 1e-12
    # 这组 Q≈6.13、df=2、p≈0.047 < 0.05 → 按修订切到 DL 随机效应（见下面的异质性单测）
    assert st["rho_pool"] == "re_dl" and se > st["rho_global_fe_se"]
    assert sorted(used) == ["A", "B", "C"]
    assert excl == {"D": "league_wn_lt_min", "E": "pass1_not_converged", "F": "se_unavailable"}


@pytest.fixture(scope="module")
def three_leagues():
    rows = []
    for lg, seed, rho in (("LA", 21, -0.12), ("LB", 22, -0.02), ("LC", 23, 0.06)):
        r, _ = simulate_league(league_id=lg, seed=seed, rho=rho, n_teams=16, rounds_per_season=4)
        rows += r
    return rows, dc.fit(rows, CUTOFF)


def test_pooled_rho_equals_ivw_of_league_fits_and_used_everywhere(three_leagues):
    rows, model = three_leagues
    lfs = model.leagues
    assert sorted(model.rho_pool) == ["LA", "LB", "LC"]
    w = {lg: 1 / lfs[lg].rho_league_se ** 2 for lg in model.rho_pool}
    hand = sum(w[lg] * lfs[lg].rho_league for lg in w) / sum(w.values())
    assert abs(model.rho_global - hand) < 1e-12
    for lg, lf in lfs.items():
        assert lf.second_pass and lf.pooled and lf.converged and lf.tau_ok
        assert lf.rho == model.rho_global  # 出矩阵用的 ρ = ρ_global
        assert lf.rho_league_se > 0 and lf.rho_league_ci[0] < lf.rho_league < lf.rho_league_ci[1]
    pr = dc.predict(model, "LA", "T00", "T01")
    assert pr.evaluable and pr.rho_global == model.rho_global and pr.rho_league == lfs["LA"].rho_league
    assert pr.rho_league_se == lfs["LA"].rho_league_se and pr.rho_used == model.rho_global and not pr.rho_clipped


def test_second_pass_is_fixed_rho_refit(three_leagues):
    rows, model = three_leagues
    lf = model.leagues["LB"]
    by_lg, _, _ = dc._select(rows, CUTOFF, model.params)
    prob = dc._LeagueProblem("LB", by_lg["LB"], model.params)
    res = prob.solve(rho_fixed=model.rho_global)
    att, dfn, gamma, rho = prob.unpack(res.x)
    assert rho == model.rho_global
    assert max(abs(att[prob.idx[t]] - lf.attack[t]) for t in prob.teams) < 1e-4
    assert abs(gamma - lf.home_adv) < 1e-4
    # 固定 ρ 后的似然不应优于联合拟合（第一遍）
    res1 = prob.solve()
    assert res1.fun <= res.fun + 1e-12


def test_second_pass_not_converged(three_leagues, monkeypatch):
    rows, _ = three_leagues
    real_min, real_pool = dc.minimize, dc.pool_rho
    state = {"pooled": False}

    def pool(*a, **k):
        out = real_pool(*a, **k)
        state["pooled"] = True
        return out

    def fake(*a, **k):
        res = real_min(*a, **k)
        if state["pooled"]:
            res.success, res.message = False, "ABNORMAL_TERMINATION_IN_LNSRCH"
        return res

    monkeypatch.setattr(dc, "pool_rho", pool)
    monkeypatch.setattr(dc, "minimize", fake)
    model = dc.fit(rows, CUTOFF)
    lf = model.leagues["LA"]
    assert lf.pass1_converged and not lf.converged and model.rho_global is not None
    pr = dc.predict(model, "LA", "T00", "T01")
    assert not pr.evaluable and pr.subreason == "fit_not_converged"
    assert pr.fit_message.startswith("ABNORMAL_TERMINATION_IN_LNSRCH")


def test_league_wn_gate_excluded_from_pool(three_leagues):
    rows, _ = three_leagues
    old, _ = simulate_league(league_id="OLDL", seed=31, n_teams=10, rounds_per_season=2, seasons=1, span_days=150)
    old = [dict(r, kickoff_at=r["kickoff_at"] - timedelta(days=560)) for r in old]
    old += [dict(r, kickoff_at=r["kickoff_at"] - timedelta(days=1)) for r in old[:80]]
    model = dc.fit(rows + old, CUTOFF)
    lf = model.leagues["OLDL"]
    assert lf.n_matches >= _P("min_league_n") and lf.weight_sum < _P("min_league_wn")
    assert "OLDL" not in model.rho_pool and model.rho_pool_excluded["OLDL"] == "league_wn_lt_min"
    base = dc.fit(rows, CUTOFF)
    assert abs(model.rho_global - base.rho_global) < 1e-9  # 不进合成 → ρ_global 不受影响
    assert not lf.pooled and lf.rho == model.rho_global  # 仍按 ρ_global 第二遍拟合，predict 时 league_wn_lt_min


def test_profile_se_kish_scaling_and_deviance(league_rows):
    rows, _ = league_rows
    p = dict(dc.DEFAULT_PARAMS)
    by_lg, _, _ = dc._select(rows, CUTOFF, p)
    r1 = by_lg["L1"]
    prob1 = dc._LeagueProblem("L1", r1, p)
    w = np.array([r[4] for r in r1])
    assert abs(prob1.n_eff - w.sum() ** 2 / (w ** 2).sum()) < 1e-9 and prob1.n_eff < len(r1)
    res1 = prob1.solve()
    rho1 = prob1.unpack(res1.x)[3]
    se1, (lo, hi) = prob1.profile_se(res1.x, rho1)
    # 区间端点处 2·n_eff·Δf = χ²₁(0.95)
    f0 = prob1.solve(rho_fixed=rho1, theta_start=res1.x).fun
    for r in (lo, hi):
        d = 2 * prob1.n_eff * (prob1.solve(rho_fixed=r, theta_start=res1.x).fun - f0)
        assert abs(d - dc.CHI2_1_95) < 0.02
    # 每场复制一份（权重不变）→ n_eff 翻倍、ρ̂ 不变、SE 缩小约 √2
    prob2 = dc._LeagueProblem("L1", r1 + r1, p)
    assert abs(prob2.n_eff - 2 * prob1.n_eff) < 1e-6
    res2 = prob2.solve()
    rho2 = prob2.unpack(res2.x)[3]
    se2, _ = prob2.profile_se(res2.x, rho2)
    assert abs(rho2 - rho1) < 1e-4
    assert abs(se1 / se2 - math.sqrt(2)) < 0.05


def test_end_of_fit_tau_check():
    # 直接检查：观测 1-1 场在 ρ=1 时 τ11 = 0 ≤ TAU_FLOOR → 计 1 场
    rows = [("A", "B", 1, 1, 1.0), ("B", "A", 2, 0, 1.0), ("A", "B", 0, 3, 1.0)]
    prob = dc._LeagueProblem("X", rows, dict(dc.DEFAULT_PARAMS))
    th = prob.theta0.copy()
    th[-1] = 1.0
    assert prob.tau_violations(th) == 1
    th[-1] = 0.0
    assert prob.tau_violations(th) == 0


def test_end_of_fit_tau_violation_marks_not_converged(league_rows, monkeypatch):
    rows, _ = league_rows
    real = dc.minimize

    def fake(fun, x0, *a, bounds=None, **k):
        res = real(fun, x0, *a, bounds=bounds, **k)
        if bounds[-1][0] != bounds[-1][1]:  # 第一遍（ρ 自由）：把 ρ 强行放到 1.0 → 观测 1-1 场 τ = 0
            res.x = res.x.copy()
            res.x[-1] = 1.0
        return res

    monkeypatch.setattr(dc, "minimize", fake)
    model = dc.fit(rows, CUTOFF)
    lf = model.leagues["L1"]
    assert not lf.pass1_converged and "tau_floor_violation" in lf.pass1_message
    assert model.rho_global is None and model.rho_pool_excluded["L1"] == "pass1_not_converged"
    pr = dc.predict(model, "L1", "T00", "T01")
    assert not pr.evaluable and pr.subreason == "fit_not_converged"


def test_penalty_gradient_out_of_bounds_matches_finite_difference():
    """越界区（τ ≤ floor）：log τ 梯度置 0，只剩泊松项 + 罚项梯度 −2K·w·(floor−τ)·∂τ；与数值差分一致。"""
    n = 3
    hi, ai = np.array([0, 1, 2, 0]), np.array([1, 2, 0, 2])
    x, y = np.array([0.0, 0.0, 1.0, 2.0]), np.array([0.0, 1.0, 1.0, 1.0])
    wn = np.array([0.4, 0.3, 0.2, 0.1])
    f, unpack = dc._make_objective(hi, ai, x, y, wn, n)
    # θ = [att0, att1, def0, def1, def2, γ, ρ]：大进攻 → λμ 大，ρ=0.3 让 0-0 场 τ00 = 1 − λμρ < 0
    th = np.array([0.9, 0.2, 0.8, 0.7, 0.6, 0.3, 0.3])
    att, dfn, gamma, rho = unpack(th)
    lam = np.exp(att[hi] + dfn[ai] + gamma)
    mu = np.exp(att[ai] + dfn[hi])
    tau, *_ = dc._tau_terms(x, y, lam, mu, rho)
    assert tau[0] < 0  # 确实越界
    val, g = f(th)
    eps = 1e-7
    num = np.array([(f(th + eps * e)[0] - f(th - eps * e)[0]) / (2 * eps) for e in np.eye(len(th))])
    assert np.allclose(g, num, rtol=1e-5, atol=1e-3 * max(1.0, np.abs(num).max()) * 1e-3)


# ---------------------------------------------------------------- 修订 18:14：异质性 Q / I² / DL 随机效应


def _ns_fits(pairs):
    from types import SimpleNamespace as NS
    return {f"L{i}": NS(rho_league=r, rho_league_se=s, weight_sum=200.0, pass1_converged=True)
            for i, (r, s) in enumerate(pairs)}


def test_heterogeneity_hand_computed_fe_kept_when_homogeneous():
    fits = _ns_fits([(-0.10, 0.05), (0.00, 0.10), (0.05, 0.02)])
    # w = 400, 100, 2500；ρ_FE = 85/3000
    fe = 85 / 3000
    Q = 400 * (-0.10 - fe) ** 2 + 100 * (0.0 - fe) ** 2 + 2500 * (0.05 - fe) ** 2
    rg, se, used, excl, st = dc.pool_rho(fits, _P("min_league_wn"))
    assert abs(st["rho_Q"] - Q) < 1e-12 and st["rho_df"] == 2
    assert abs(st["rho_I2"] - max(0.0, (Q - 2) / Q)) < 1e-12
    assert abs(st["rho_Q_p"] - math.exp(-Q / 2)) < 1e-12  # χ²₂ 生存函数 = e^(−Q/2)
    # Q ≈ 6.13 → p ≈ 0.047 < 0.05 → 这组其实会切 RE；换一组同质的验证 FE
    fits2 = _ns_fits([(-0.05, 0.05), (-0.04, 0.06), (-0.06, 0.04), (-0.03, 0.07)])
    rg2, se2, _, _, st2 = dc.pool_rho(fits2, _P("min_league_wn"))
    w = np.array([1 / 0.05**2, 1 / 0.06**2, 1 / 0.04**2, 1 / 0.07**2])
    r = np.array([-0.05, -0.04, -0.06, -0.03])
    fe2 = float(np.dot(w, r) / w.sum())
    Q2 = float(np.dot(w, (r - fe2) ** 2))
    assert st2["rho_pool"] == "fe" and st2["rho_Q_p"] > 0.05 and st2["rho_I2"] == 0.0  # Q2 < df → I² 截到 0
    assert abs(st2["rho_Q"] - Q2) < 1e-12 and abs(rg2 - fe2) < 1e-12 and abs(se2 - w.sum() ** -0.5) < 1e-12
    assert st2["rho_tau2"] == 0.0


def test_heterogeneity_switches_to_dl_random_effects():
    pairs = [(-0.20, 0.03), (0.15, 0.03), (-0.05, 0.02), (0.10, 0.04)]
    rg, se, used, excl, st = dc.pool_rho(_ns_fits(pairs), _P("min_league_wn"))
    r = np.array([p[0] for p in pairs]); s = np.array([p[1] for p in pairs])
    w = 1 / s**2
    fe = float(np.dot(w, r) / w.sum())
    Q = float(np.dot(w, (r - fe) ** 2))
    tau2 = (Q - 3) / (w.sum() - (w**2).sum() / w.sum())
    ws = 1 / (s**2 + tau2)
    assert st["rho_Q_p"] < 0.05 and st["rho_pool"] == "re_dl"
    assert abs(st["rho_tau2"] - tau2) < 1e-12 and tau2 > 0
    assert abs(rg - float(np.dot(ws, r) / ws.sum())) < 1e-12
    assert abs(se - ws.sum() ** -0.5) < 1e-12 and se > st["rho_global_fe_se"]
    assert abs(st["rho_global_fe"] - fe) < 1e-12


def test_single_league_pool_df0_uses_fe():
    rg, se, used, excl, st = dc.pool_rho(_ns_fits([(-0.07, 0.05)]), _P("min_league_wn"))
    assert st["rho_df"] == 0 and st["rho_Q"] == 0.0 and st["rho_Q_p"] is None and st["rho_pool"] == "fe"
    assert rg == -0.07


def test_fit_records_heterogeneity_and_fingerprint(three_leagues, monkeypatch):
    rows, model = three_leagues
    snap = dc.rho_snapshot(model)
    for k in ("rho_Q", "rho_df", "rho_I2", "rho_Q_p", "rho_pool", "rho_global", "rho_global_se"):
        assert k in snap
    assert snap["rho_df"] == len(model.rho_pool) - 1 == 2
    assert model.fingerprint["rho_pool"] == snap["rho_pool"] in ("fe", "re_dl")
    assert "rho_pool" in dc.FINGERPRINT and dc.FINGERPRINT["rho_pool"] is None  # 模块级占位不被单次 fit 改写
    # RE 生效时：第二遍用的就是 RE 的 ρ
    real = dc.pool_rho

    def force_re(fits, mw, p=0.05):
        return real(fits, mw, 1.01)  # 阈值 > 1 → 必切 RE（只要 df ≥ 1）
    monkeypatch.setattr(dc, "pool_rho", force_re)
    m2 = dc.fit(rows, CUTOFF)
    assert m2.rho_pool_method == "re_dl" and m2.fingerprint["rho_pool"] == "re_dl"
    assert all(lf.rho == m2.rho_global for lf in m2.leagues.values())
    assert m2.rho_global_se >= m2.rho_global_fe_se
