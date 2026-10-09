"""settlement_version: default fixed 0.95 vs ah_v4_macau_actual_or_095."""
from app import backtest as bt


def test_default_macau_fixed():
    j, src, reason = bt.resolve_juice(1.02, 0.95, book="macau", water_src="actual")
    assert (j, src, reason) == (0.95, "fixed_macau", None)


def test_actual_or_095_uses_actual():
    j, src, reason = bt.resolve_juice(
        1.02, 0.95, book="macau", water_src="actual",
        settlement_version=bt.SETTLEMENT_MACAU_ACTUAL,
    )
    assert (j, src, reason) == (1.02, "actual", None)


def test_actual_or_095_fallback_missing():
    j, src, reason = bt.resolve_juice(
        None, 0.95, book="macau", water_src=None,
        settlement_version=bt.SETTLEMENT_MACAU_ACTUAL,
    )
    assert j == 0.95 and src == "fallback" and reason == "water_missing"


def test_actual_or_095_requires_water_src_actual():
    j, src, reason = bt.resolve_juice(
        1.02, 0.95, book="macau", water_src="tier_midpoint",
        settlement_version=bt.SETTLEMENT_MACAU_ACTUAL,
    )
    assert j == 0.95 and src == "fallback"


def test_summarize_fair_compare_fields():
    entries = [
        {"side": "主", "amount": 50, "pnl": 47.5, "pnl_units": 0.95, "result_code": "win",
         "juice_source": "actual", "water_censored": False, "reasons": [], "equity_after": 10047.5,
         "match_uid": "a", "match_id": 1},
        {"side": "客", "amount": 50, "pnl": -50, "pnl_units": -1, "result_code": "lose",
         "juice_source": "fallback", "water_censored": False, "reasons": ["water_missing"],
         "equity_after": 9997.5, "match_uid": "b", "match_id": 2},
        {"side": None, "amount": 0, "pnl": 0, "pnl_units": 0, "result_code": "skip",
         "juice_source": None, "water_censored": False, "reasons": [], "equity_after": 9997.5,
         "match_uid": "c", "match_id": 3},
    ]
    s = bt.summarize(entries, 10000.0)
    assert s["n_actual_water"] == 1
    assert s["n_fallback_095"] == 1
    assert s["fallback_rate"] == 0.5
