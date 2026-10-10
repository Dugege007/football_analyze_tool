"""比赛列表接口 0.3.24：每场返回当时冻结预测所用的正式方案代码，影子方案与测试方案不列出。"""
from __future__ import annotations

import sqlite3

from app.main import formal_strategies_for_match


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.executescript(
        """
        CREATE TABLE strategy_defs (strategy_key TEXT, status TEXT, config_json TEXT);
        CREATE TABLE predictions (match_id INTEGER, strategy TEXT, direction TEXT);
        INSERT INTO strategy_defs VALUES ('FORMAL_A', 'active', '{}');
        INSERT INTO strategy_defs VALUES ('FORMAL_B', 'active', '{}');
        INSERT INTO strategy_defs VALUES ('SHADOW_X', 'shadow', '{}');
        INSERT INTO strategy_defs VALUES ('TEST_Y', 'active', '{"extras": {"test_only": 1}}');
        INSERT INTO predictions VALUES (1, 'FORMAL_B', '主');
        INSERT INTO predictions VALUES (1, 'FORMAL_A', '不下注');
        INSERT INTO predictions VALUES (1, 'SHADOW_X', '客');
        INSERT INTO predictions VALUES (1, 'TEST_Y', '主');
        INSERT INTO predictions VALUES (2, 'SHADOW_X', '客');
        """
    )
    return c


def test_lists_only_formal_strategies_sorted():
    assert formal_strategies_for_match(_conn(), 1) == ["FORMAL_A", "FORMAL_B"]


def test_no_formal_prediction_returns_empty_list():
    c = _conn()
    assert formal_strategies_for_match(c, 2) == []
    assert formal_strategies_for_match(c, 3) == []
