"""0.3.18：N5 / N5-PIN 快照 ρ 字段（rho_global / rho_league / rho_league_se）+ 指纹 rho=global_pooled_ivw、
rho_se_method=profile_kish。缺任何一项或值不对 → 拒写。dc_model 不在本测试范围（归分析师）。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import shadow_evaluable as se  # noqa: E402

RHO = {"rho_global": -0.052, "rho_league": -0.031, "rho_league_se": 0.024,
       "rho_Q": 4.1, "rho_df": 6, "rho_I2": 0.0, "rho_Q_p": 0.66}  # 0.3.19 每竞彩日字段
FP = {"rho": "global_pooled_ivw", "rho_se_method": "profile_kish", "rho_pool": "fe", "n5_water": "real_only"}


def _ok(ledger="N5", evaluable=True, **kw):
    args = dict(ledger_id=ledger, evaluable=evaluable, odds_source="hist", fingerprint=dict(FP), **RHO)
    args.update(kw)
    return se.build_snapshot(**args)


@pytest.mark.parametrize("ledger", ["N5", "N5-PIN"])
def test_rho_fields_written(ledger):
    s = _ok(ledger)
    assert (s["rho_global"], s["rho_league"], s["rho_league_se"]) == (-0.052, -0.031, 0.024)
    assert s["fingerprint"]["rho"] == "global_pooled_ivw" and s["fingerprint"]["rho_se_method"] == "profile_kish"
    assert set(se.N5_RHO_FIELDS) <= set(se.SNAPSHOT_FIELDS)


@pytest.mark.parametrize("ledger", ["N5", "N5-PIN"])
@pytest.mark.parametrize("drop", ["rho_global", "rho_league", "rho_league_se"])
def test_missing_rho_field_rejected(ledger, drop):
    args = dict(ledger_id=ledger, evaluable=True, odds_source="hist", fingerprint=dict(FP),
                **{k: v for k, v in RHO.items() if k != drop})
    with pytest.raises(ValueError, match="missing"):
        se.build_snapshot(**args)


@pytest.mark.parametrize("fp", [
    None, {}, {"rho": "global_pooled_ivw"}, {"rho_se_method": "profile_kish"},
    {"rho": "per_league", "rho_se_method": "profile_kish"},
    {"rho": "global_pooled_ivw", "rho_se_method": "lbfgsb_inv_hessian"},
])
def test_fingerprint_rho_required_and_fixed(fp):
    with pytest.raises(ValueError, match="fingerprint"):
        _ok(fingerprint=fp)


@pytest.mark.parametrize("k,bad", [
    ("rho_global", 1.0), ("rho_global", -1.2), ("rho_global", float("nan")), ("rho_global", "x"),
    ("rho_global", True), ("rho_league", 1.5), ("rho_league_se", 0.0), ("rho_league_se", -0.01),
    ("rho_league_se", float("inf")),
])
def test_bad_rho_values_rejected(k, bad):
    with pytest.raises(ValueError):
        _ok(**{k: bad})


def test_null_rho_only_for_not_evaluable():
    with pytest.raises(ValueError, match="evaluable snapshot requires numeric"):
        _ok(rho_league=None)
    s = _ok(evaluable=False, not_evaluable_reason="model_scope", not_evaluable_subreason="cup",
            rho_league=None, rho_league_se=None)
    assert s["rho_league"] is None and s["rho_global"] == -0.052


def test_rho_fields_rejected_on_other_ledgers():
    with pytest.raises(ValueError, match="N5/N5-PIN-only"):
        se.build_snapshot(ledger_id="N1", evaluable=False, odds_source="hist",
                          not_evaluable_reason=se.CLOSE_UNUSABLE, close_basis="api_closing", rho_global=-0.05)
