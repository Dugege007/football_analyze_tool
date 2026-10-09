"""0.3.19：N5 快照每竞彩日 ρ 异质性字段 rho_Q / rho_df / rho_I2 / rho_Q_p + 指纹 rho_pool=fe|re_dl。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import shadow_evaluable as se  # noqa: E402

RHO = {"rho_global": -0.05, "rho_league": -0.03, "rho_league_se": 0.02}
DAY = {"rho_Q": 4.1, "rho_df": 6, "rho_I2": 0.0, "rho_Q_p": 0.66}
FP = {"rho": "global_pooled_ivw", "rho_se_method": "profile_kish", "rho_pool": "fe"}


def _b(ledger="N5", evaluable=True, fp=None, **kw):
    args = dict(ledger_id=ledger, evaluable=evaluable, odds_source="hist",
                fingerprint=dict(fp if fp is not None else FP), **RHO, **DAY)
    if not evaluable:
        args.update(not_evaluable_reason="model_insufficient", not_evaluable_subreason="league_n_lt_min")
    args.update(kw)
    return se.build_snapshot(**args)


@pytest.mark.parametrize("ledger", ["N5", "N5-PIN"])
def test_day_fields_written(ledger):
    s = _b(ledger)
    assert {k: s[k] for k in DAY} == DAY and s["fingerprint"]["rho_pool"] == "fe"
    assert set(se.N5_RHO_DAY_FIELDS) <= set(se.SNAPSHOT_FIELDS)


@pytest.mark.parametrize("drop", list(DAY))
def test_missing_day_field_rejected(drop):
    args = dict(ledger_id="N5", evaluable=True, odds_source="hist", fingerprint=dict(FP), **RHO,
                **{k: v for k, v in DAY.items() if k != drop})
    with pytest.raises(ValueError, match="missing"):
        se.build_snapshot(**args)


@pytest.mark.parametrize("pool", [None, "", "re", "random", "FE"])
def test_rho_pool_required(pool):
    with pytest.raises(ValueError, match="rho_pool"):
        _b(fp={**FP, "rho_pool": pool})


def test_re_dl_when_q_significant():
    s = _b(fp={**FP, "rho_pool": "re_dl"}, rho_Q=21.5, rho_df=6, rho_I2=0.72, rho_Q_p=0.0015)
    assert s["fingerprint"]["rho_pool"] == "re_dl" and s["rho_I2"] == 0.72
    with pytest.raises(ValueError, match="rho_pool must be 're_dl'"):
        _b(rho_Q=21.5, rho_I2=0.72, rho_Q_p=0.0015)  # p<0.05 却写 fe
    with pytest.raises(ValueError, match="rho_pool must be 'fe'"):
        _b(fp={**FP, "rho_pool": "re_dl"})  # p=0.66 却写 re_dl
    # 边界：p = 0.05 不算显著 → fe
    assert _b(rho_Q_p=0.05)["fingerprint"]["rho_pool"] == "fe"


@pytest.mark.parametrize("k,bad", [
    ("rho_Q", -0.1), ("rho_Q", float("nan")), ("rho_df", -1), ("rho_df", 2.5), ("rho_I2", 1.2),
    ("rho_I2", 72), ("rho_I2", -0.01), ("rho_Q_p", 1.01), ("rho_Q_p", True), ("rho_Q", "x"),
])
def test_bad_day_values_rejected(k, bad):
    with pytest.raises(ValueError):
        _b(**{k: bad})


def test_not_evaluable_allows_null_day_fields():
    s = _b(evaluable=False, fp={**FP, "rho_pool": None}, rho_Q=None, rho_df=None, rho_I2=None, rho_Q_p=None,
           rho_global=None, rho_league=None, rho_league_se=None)
    assert all(s[k] is None for k in DAY)
    with pytest.raises(ValueError):
        _b(rho_Q=None)  # 可评估行不能空


def test_day_fields_rejected_on_non_n5():
    with pytest.raises(ValueError, match="N5"):
        se.build_snapshot(ledger_id="N1", evaluable=True, odds_source="hist", rho_Q=1.0)


def test_old_snapshot_reads_unaffected():
    old = [{"feature_snapshot": {"ledger_id": "N5", "evaluable": True, "rho_global": -0.05}}]
    assert se.not_evaluable_of(old) is None  # 只读路径不校验新字段（旧快照不改）
