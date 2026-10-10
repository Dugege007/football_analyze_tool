"""比赛列表接口 0.3.26：每场返回正式方案冻结预测的各玩法方向（picks），份数为空时给出推算值并标注。"""
from __future__ import annotations

import sqlite3

from app.main import estimate_stake_from_rationale, formal_prediction_picks


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(
        """
        CREATE TABLE strategy_defs (strategy_key TEXT, status TEXT, config_json TEXT);
        CREATE TABLE predictions (match_id INTEGER, strategy TEXT, direction TEXT, stake INTEGER,
                                  rationale_json TEXT);
        CREATE TABLE prediction_legs (id INTEGER PRIMARY KEY, match_id INTEGER, market TEXT, side TEXT,
                                      line REAL, line_text TEXT, stake REAL, strategy TEXT, status TEXT);
        CREATE TABLE odds_asian (match_id INTEGER, book TEXT, phase TEXT, handicap REAL);
        INSERT INTO strategy_defs VALUES ('FORMAL_A', 'active', '{}');
        INSERT INTO strategy_defs VALUES ('SHADOW_X', 'shadow', '{}');
        INSERT INTO odds_asian VALUES (1, 'macau', 'close', 2.0);
        INSERT INTO predictions VALUES (1, 'FORMAL_A', '主', NULL, '["dir_sum=3"]');
        INSERT INTO predictions VALUES (1, 'SHADOW_X', '客', 1, '[]');
        INSERT INTO prediction_legs VALUES (1, 1, 'jc_hhad', '负', -1, '-1', 2, 'FORMAL_A', 'active');
        INSERT INTO prediction_legs VALUES (2, 1, 'ou', '大', 2.5, NULL, 1, 'SHADOW_X', 'active');
        """
    )
    return c


def test_only_formal_picks_and_estimated_stake():
    picks = formal_prediction_picks(_conn(), 1)
    assert picks == [
        {"market": "ah", "strategy": "FORMAL_A", "side": "主", "line": 2.0, "line_text": None,
         "stake": 1, "stake_estimated": True},
        {"market": "jc_hhad", "strategy": "FORMAL_A", "side": "负", "line": -1.0, "line_text": "-1",
         "stake": 2, "stake_estimated": False},
    ]


def test_estimate_stake_rule():
    assert estimate_stake_from_rationale(["dir_sum=-5"]) == 2
    assert estimate_stake_from_rationale(["bucket=SUM=+3"]) == 1
    assert estimate_stake_from_rationale(["dir_sum=4"]) is None
    assert estimate_stake_from_rationale([]) is None
