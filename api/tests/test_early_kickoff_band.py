"""早场特殊带 [00:00, 11:30]（对齐 作者的私有分析仓 私有仓 PR）。"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.collection_schedule import (  # noqa: E402
    calc_collection_times,
    is_early_kickoff_band,
    is_rule_fixed_clock,
    is_rule_legacy_applicable,
    resolve_collection_ats,
)


TZ_CN = timezone(timedelta(hours=8))


def test_hour_only_early_band():
    assert is_early_kickoff_band(0) is True
    assert is_early_kickoff_band(10) is True
    assert is_early_kickoff_band(11) is True  # 整点回退：11 全部早场
    assert is_early_kickoff_band(12) is False
    assert is_early_kickoff_band(22) is False
    assert is_early_kickoff_band(23) is False  # 23 不是早场带，是晚场固定钟点


def test_minute_aware_1130_1131():
    assert is_early_kickoff_band(11, 0) is True
    assert is_early_kickoff_band(11, 30) is True  # 含端
    assert is_early_kickoff_band(11, 31) is False
    assert is_early_kickoff_band(11, 59) is False
    assert is_early_kickoff_band(12, 0) is False


def test_rule_fixed_clock_keeps_23_and_expands_11():
    assert is_rule_fixed_clock(10) is True
    assert is_rule_fixed_clock(11) is True
    assert is_rule_fixed_clock(11, 30) is True
    assert is_rule_fixed_clock(11, 31) is False
    assert is_rule_fixed_clock(12) is False
    assert is_rule_fixed_clock(23) is True
    assert is_rule_fixed_clock(23, 59) is True


def test_rule_legacy_tracks_early_band():
    assert is_rule_legacy_applicable(11) is True
    assert is_rule_legacy_applicable(11, 31) is False
    assert is_rule_legacy_applicable(12) is False
    assert is_rule_legacy_applicable(23) is False


def test_collection_times_10_11_12_23():
    assert calc_collection_times(10) == {"mid_time": "15:00", "final_time": "22:00"}
    assert calc_collection_times(11) == {"mid_time": "15:00", "final_time": "22:00"}
    assert calc_collection_times(11, 30) == {"mid_time": "15:00", "final_time": "22:00"}
    assert calc_collection_times(11, 31) == {"mid_time": "03:31", "final_time": "10:31"}  # 0.3.16 精确到分钟
    assert calc_collection_times(12) == {"mid_time": "04:00", "final_time": "11:00"}
    assert calc_collection_times(23) == {"mid_time": "15:00", "final_time": "22:00"}


def test_resolve_ats_on_jingcai_date():
    mid, close = resolve_collection_ats("2026-06-19", 11)
    assert mid.isoformat() == "2026-06-19T15:00:00+08:00"
    assert close.isoformat() == "2026-06-19T22:00:00+08:00"
    mid12, close12 = resolve_collection_ats("2026-06-13", 12)
    assert mid12.hour == 4 and close12.hour == 11
