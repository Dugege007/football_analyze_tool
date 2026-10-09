"""0.3.17：N1 open_unusable + N5 口径卡修订（fit_not_converged / *_wn_lt_min / fit_message / *_w_n / 指纹新键）。纯单测，不碰库。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import shadow_evaluable as se  # noqa: E402

# 0.3.18：N5 / N5-PIN 快照必须带 ρ 字段 + 指纹 rho / rho_se_method（shadow_evaluable._check_n5_rho）
N5RHO = {"rho_global": -0.05, "rho_league": -0.04, "rho_league_se": 0.03,
         "rho_Q": 4.1, "rho_df": 6, "rho_I2": 0.0, "rho_Q_p": 0.66}  # 0.3.19 每竞彩日 Q/df/I2/Q_p
N5FP = {"rho": "global_pooled_ivw", "rho_se_method": "profile_kish", "rho_pool": "fe"}


def _b5(**kw):
    """N5 / N5-PIN 快照：未给时补 ρ 字段与指纹 rho / rho_se_method（0.3.18 强制）。"""
    if kw.get("ledger_id") in ("N5", "N5-PIN"):
        for k, v in N5RHO.items():
            kw.setdefault(k, v)
        kw["fingerprint"] = {**N5FP, **(kw.get("fingerprint") or {})}
    return se.build_snapshot(**kw)


# 公开仓：N5 调好的参数从 config/strategy_params.json 读取（未配置时相关用例 skip）
from app.strategy_params import get_or_none as _sp_get  # noqa: E402
FP = {"ledger_id": "N5", "p_model_method": "dixon_coles_v1", "min_team_n": _sp_get("N5", "min_team_n"),
      "min_league_n": _sp_get("N5", "min_league_n"),
      "min_team_wn": se.N5_FINGERPRINT_REVISION["min_team_wn"], "min_league_wn": se.N5_FINGERPRINT_REVISION["min_league_wn"],
      "converge_check": True, **N5FP}


@pytest.mark.parametrize("ledger", ["N5", "N5-PIN"])
@pytest.mark.parametrize("sub", ["fit_not_converged", "team_wn_lt_min", "league_wn_lt_min",
                                 "team_n_lt_min", "league_n_lt_min"])
def test_model_insufficient_new_subreasons_accepted(ledger, sub):
    snap = _b5(ledger_id=ledger, evaluable=False, odds_source="hist",
                             not_evaluable_reason="model_insufficient", not_evaluable_subreason=sub,
                             fit_message="Desired error not necessarily achieved due to precision loss."
                             if sub == "fit_not_converged" else None,
                             home_w_n=4.25, away_w_n=12.0, league_w_n=71.5, fingerprint=dict(FP, ledger_id=ledger))
    assert se.not_evaluable_of(snap) == ("model_insufficient", sub)
    assert snap["home_w_n"] == 4.25 and snap["away_w_n"] == 12.0 and snap["league_w_n"] == 71.5
    assert snap["fingerprint"]["min_team_wn"] == se.N5_FINGERPRINT_REVISION["min_team_wn"]
    assert snap["fingerprint"]["min_league_wn"] == se.N5_FINGERPRINT_REVISION["min_league_wn"]
    assert snap["fingerprint"]["converge_check"] is True


def test_fit_message_stored_verbatim_and_required_for_not_converged():
    msg = "ABNORMAL_TERMINATION_IN_LNSRCH  \n(iter=1000)"
    snap = se.build_snapshot(**N5RHO, fingerprint=N5FP, ledger_id="N5", evaluable=False, odds_source="live",
                             not_evaluable_reason="model_insufficient",
                             not_evaluable_subreason="fit_not_converged", fit_message=msg)
    assert snap["fit_message"] == msg
    with pytest.raises(ValueError, match="fit_message"):
        se.build_snapshot(**N5RHO, fingerprint=N5FP, ledger_id="N5", evaluable=False, odds_source="live",
                          not_evaluable_reason="model_insufficient", not_evaluable_subreason="fit_not_converged")
    with pytest.raises(ValueError, match="fit_message"):
        se.build_snapshot(**N5RHO, fingerprint=N5FP, ledger_id="N5", evaluable=True, odds_source="live", fit_message=123)


@pytest.mark.parametrize("bad", [-0.1, float("nan"), float("inf"), "x", True])
def test_w_n_must_be_non_negative_finite(bad):
    with pytest.raises(ValueError):
        se.build_snapshot(**N5RHO, fingerprint=N5FP, ledger_id="N5", evaluable=True, odds_source="hist", league_w_n=bad)


def test_w_n_int_coerced_float_and_n5_keys_present_even_when_null():
    snap = se.build_snapshot(**N5RHO, fingerprint=N5FP, ledger_id="N5-PIN", evaluable=True, odds_source="hist", home_w_n=7)
    assert snap["home_w_n"] == 7.0 and isinstance(snap["home_w_n"], float)
    assert snap["away_w_n"] is None and snap["league_w_n"] is None and snap["fit_message"] is None


@pytest.mark.parametrize("k,v", [("min_team_wn", -1), ("min_league_wn", -1), ("converge_check", False),
                                 ("converge_check", 1)])
def test_fingerprint_revision_values_fixed(k, v):
    with pytest.raises(ValueError, match="spec revision"):
        se.build_snapshot(**N5RHO, ledger_id="N5", evaluable=True, odds_source="hist", fingerprint=dict(FP, **{k: v}))


def test_unknown_subreason_still_rejected():
    with pytest.raises(ValueError, match="subreason"):
        se.build_snapshot(**N5RHO, fingerprint=N5FP, ledger_id="N5", evaluable=False, odds_source="hist",
                          not_evaluable_reason="model_insufficient", not_evaluable_subreason="team_wn_lt_5")


def test_by_subreason_counts_include_new_subreasons():
    t = se.new_tally()
    for sub in ("fit_not_converged", "team_wn_lt_min", "team_wn_lt_min", "league_wn_lt_min", "team_n_lt_min"):
        snap = se.build_snapshot(**N5RHO, fingerprint=N5FP, ledger_id="N5", evaluable=False, odds_source="hist",
                                 not_evaluable_reason="model_insufficient", not_evaluable_subreason=sub,
                                 fit_message="m" if sub == "fit_not_converged" else None)
        se.tally_add(t, se.not_evaluable_of(snap), match_id=1, odds_source="hist")
    s = se.summary_fields(t)
    assert s["n_not_evaluable_by_reason"]["model_insufficient"] == 5
    assert s["n_not_evaluable_by_subreason"]["model_insufficient"] == {
        "fit_not_converged": 1, "team_wn_lt_min": 2, "league_wn_lt_min": 1, "team_n_lt_min": 1}
    assert set(se.REASONS) <= set(s["n_not_evaluable_by_reason"])


# ---------------- N1 open_unusable ----------------

def _n1(**kw):
    base = dict(ledger_id="N1", evaluable=False, odds_source="hist", decision_phase="close",
                open_basis="api_opening", usable_at_mid=False, usable_at_close=False,
                not_evaluable_reason="open_unusable",
                not_evaluable_subreason="open_time_unknown_after_decision_possible")
    base.update(kw)
    return _b5(**base)


def test_n1_open_unusable_allowed_and_counted():
    snap = _n1()
    se.assert_prediction_consistent("不下注", snap)
    t = se.new_tally()
    se.tally_add(t, se.not_evaluable_of(snap), match_id=9, odds_source="hist", open_basis="api_opening")
    s = se.summary_fields(t)
    assert s["n_not_evaluable"] == 1 and s["n_not_evaluable_by_reason"]["open_unusable"] == 1
    for r in se.REASONS:  # 四键恒在不变
        assert s["n_not_evaluable_by_reason"][r] == 0


def test_n1_open_unusable_requires_usable_false_and_open_basis():
    with pytest.raises(ValueError, match="usable_at_close=false"):
        _n1(usable_at_close=True)
    with pytest.raises(ValueError, match="usable_at_close=false"):
        _n1(open_basis=None, usable_at_mid=None, usable_at_close=None)
    assert se.not_evaluable_of(_n1(open_basis="legacy_import"))[0] == "open_unusable"


def test_open_unusable_scope_is_n1_only():
    with pytest.raises(ValueError, match="not allowed"):
        se.build_snapshot(**N5RHO, fingerprint=N5FP, ledger_id="N5", evaluable=False, odds_source="hist", open_basis="api_opening",
                          usable_at_close=False, not_evaluable_reason="open_unusable")
    with pytest.raises(ValueError, match="not allowed"):
        _n1(not_evaluable_reason="model_scope", not_evaluable_subreason=None)


def test_legacy_import_open_features_need_usable_true():
    with pytest.raises(ValueError, match="usable_at_close=true"):
        se.build_snapshot(ledger_id="N1", evaluable=True, odds_source="hist", open_basis="legacy_import",
                          open_features_used=["open_odds"], usable_at_close=False)
    snap = se.build_snapshot(ledger_id="N1", evaluable=True, odds_source="hist", open_basis="legacy_import",
                             open_features_used=["open_odds"], usable_at_close=True)
    assert se.open_basis_of(snap) == "unknown"  # validate 分账桶不变：legacy_import 归 unknown


# ---------------- hl_v0.2：N5 只认真实水位 ----------------

WS = {"pinnacle": "actual", "macau": "actual", "crown": "tier_midpoint", "william": "actual"}


def test_n5_real_water_lt_3_subreason_and_by_subreason():
    snap = se.build_snapshot(**N5RHO, ledger_id="N5", evaluable=False, odds_source="hist",
                             not_evaluable_reason="market_insufficient", not_evaluable_subreason="real_water_lt_3",
                             n_books=2, water_source={"pinnacle": "actual", "macau": "actual",
                                                      "crown": "tier_midpoint", "william": "tier_midpoint"},
                             fingerprint=dict(FP, n5_water="real_only"))
    assert snap["water_source"]["crown"] == "tier_midpoint" and snap["fingerprint"]["n5_water"] == "real_only"
    t = se.new_tally()
    se.tally_add(t, se.not_evaluable_of(snap), match_id=1, odds_source="hist")
    s = se.summary_fields(t)
    assert s["n_not_evaluable_by_subreason"] == {"market_insufficient": {"real_water_lt_3": 1}}


def test_n5_real_water_lt_3_scope():
    with pytest.raises(ValueError):
        se.build_snapshot(**N5RHO, fingerprint=N5FP, ledger_id="N5-PIN", evaluable=False, odds_source="hist",
                          not_evaluable_reason="market_insufficient", not_evaluable_subreason="real_water_lt_3")


def test_n5_n_books_must_not_count_non_actual():
    ok = se.build_snapshot(**N5RHO, fingerprint=N5FP, ledger_id="N5", evaluable=True, odds_source="hist", n_books=3, water_source=WS,
                           tick_age={"pinnacle": 5.0, "macau": 3.0, "william": 0.0})
    assert ok["n_books"] == 3
    with pytest.raises(ValueError, match="non-actual"):
        se.build_snapshot(**N5RHO, fingerprint=N5FP, ledger_id="N5", evaluable=True, odds_source="hist", n_books=4, water_source=WS)
    with pytest.raises(ValueError, match="water_source != actual"):
        se.build_snapshot(**N5RHO, fingerprint=N5FP, ledger_id="N5", evaluable=True, odds_source="hist", n_books=3, water_source=WS,
                          tick_age={"pinnacle": 5.0, "macau": 3.0, "crown": 1.0})
    with pytest.raises(ValueError, match="≥3 actual"):
        se.build_snapshot(**N5RHO, fingerprint=N5FP, ledger_id="N5", evaluable=True, odds_source="hist", n_books=2,
                          water_source={"pinnacle": "actual", "macau": "actual", "crown": "tier_midpoint"})


@pytest.mark.parametrize("bad", [{"macau": "estimated"}, ["actual"]])
def test_water_source_values_validated(bad):
    with pytest.raises(ValueError, match="water_source"):
        se.build_snapshot(**N5RHO, fingerprint=N5FP, ledger_id="N5", evaluable=True, odds_source="hist", water_source=bad)


def test_fingerprint_n5_water_fixed():
    with pytest.raises(ValueError, match="spec revision"):
        se.build_snapshot(**N5RHO, ledger_id="N5", evaluable=True, odds_source="hist", fingerprint=dict(FP, n5_water="all"))


# ---------------- N1 生成器路径（不写库） ----------------

def _gen():
    sys.path.insert(0, str(ROOT / "scripts"))
    import generate_shadow_predictions as g
    return g


def _n1_inputs():
    euro = {(5, "william", "open"): {"home": 1.9, "draw": 3.4, "away": 4.0},
            (5, "william", "close"): {"home": 2.3, "draw": 3.2, "away": 3.2}}
    asian = {(5, "macau", "open"): {"handicap": 0.5}}
    return asian, euro


def test_gen_n1_open_unusable_marks_not_evaluable():
    g = _gen()
    asian, euro = _n1_inputs()
    flags = {5: {"open_basis": "api_opening", "usable_at_mid": False, "usable_at_close": False,
                 "earliest_ts_quote_at": None, "ts_inferred": False}}
    rows, st = g.gen_n1(asian, euro, [5], open_flags=flags)
    assert st["not_evaluable_open_unusable"] == 1 and st["trigger"] == 0
    assert rows[0]["direction"] == "不下注"
    ne = se.not_evaluable_of(rows[0]["rationale_json"])
    assert ne == ("open_unusable", "open_time_unknown_after_decision_possible")
    assert se.odds_source_of(rows[0]["rationale_json"]) == "hist"


def test_gen_n1_usable_open_evaluates_and_legacy_behavior_unchanged():
    g = _gen()
    asian, euro = _n1_inputs()
    flags = {5: {"open_basis": "first_tick", "usable_at_mid": True, "usable_at_close": True}}
    rows, st = g.gen_n1(asian, euro, [5], open_flags=flags)
    assert st["trigger"] == 1 and rows[0]["snap"]["usable_at_close"] is True
    rows0, st0 = g.gen_n1(asian, euro, [5])  # 旧路径（复现冻结行）不受影响
    assert st0["trigger"] == 1 and "usable_at_close" not in rows0[0]["snap"]


def test_n1_append_protects_frozen_rows(tmp_path):
    import sqlite3
    g = _gen()
    c = sqlite3.connect(str(tmp_path / "t.db"))
    c.execute("""CREATE TABLE predictions (id INTEGER PRIMARY KEY, match_id INTEGER, strategy TEXT, direction TEXT,
                 settle_book TEXT, rationale_json TEXT, stake INTEGER, stake_rule TEXT)""")
    c.execute("INSERT INTO predictions (match_id, strategy, direction) VALUES (1, 'SHADOW_N1', '客')")
    row = {"match_id": 1, "strategy": "SHADOW_N1", "direction": "不下注", "settle_book": "macau_close",
           "rationale_json": "[]", "stake": 1, "stake_rule": "x"}
    n = g.append_predictions(c, [row, dict(row, match_id=2)], protect_existing="SHADOW_N1")
    assert n == 1
    assert c.execute("SELECT direction FROM predictions WHERE match_id=1").fetchone()[0] == "客"
