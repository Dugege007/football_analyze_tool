"""台账版本登记（0.3.19，决议「0.3.18 的五件事」3 + 推迟场补充末条）。

- 输入口径变了就开新版本：SHADOW_S2 → SHADOW_S2_V2、SHADOW_N4 → SHADOW_N4_V2（只认真实水位，odds_source=hist）。
- 旧 key 冻结：不再追加新行（生成器 / 导入入口调用 assert_appendable 拦截），已冻结条目保留不动。
- 旧冻结条目里「触发依据是换算水位」的，读出时加备注（不改库、不改预测本身）：
    · SHADOW_N4：全部条目（HIGH 用的是 crown 初盘水位，crown 水位全是档位换算 tier_midpoint）
    · SHADOW_S2：branch=b_high_open_rise_water 的条目（用 crown 临盘水位 ≥1.00 触发；现网 2 条：五030、一044）
  备注放在 prediction.ledger_note（/table/matches、/matches/{id}/prediction(s) 都返回），前端做悬停。以后不和 V2 比。
"""
from __future__ import annotations

import json
from typing import Any

TIER_WATER_NOTE = "触发依据是换算水位"
TIER_WATER_NOTE_REASON = "tier_water_trigger"

# 旧 key → 新 key（旧 key 禁止追加）
FROZEN_NO_APPEND: dict[str, str] = {"SHADOW_S2": "SHADOW_S2_V2", "SHADOW_N4": "SHADOW_N4_V2"}
# V2 口径（新 key 的指纹 extras 原样带）
V2_WATER_RULE = "real_water_only"


class FrozenLedgerError(RuntimeError):
    pass


def assert_appendable(strategy_key: str) -> None:
    """旧 S2 / N4 key 禁止再写（追加或重建都不行）。"""
    if strategy_key in FROZEN_NO_APPEND:
        raise FrozenLedgerError(
            f"{strategy_key} 已冻结（0.3.19：输入口径改为真实水位 → 新版本 {FROZEN_NO_APPEND[strategy_key]}）；"
            f"旧 key 禁止追加，请用 scripts/generate_shadow_s2n4_v2.py")


def _branch_of(rationale: Any) -> str | None:
    if isinstance(rationale, str):
        try:
            rationale = json.loads(rationale)
        except (TypeError, ValueError):
            return None
    if not isinstance(rationale, list):
        return None
    for it in rationale:
        if isinstance(it, str) and it.startswith("branch="):
            return it.split("=", 1)[1]
        if isinstance(it, dict) and isinstance(it.get("feature_snapshot"), dict):
            b = it["feature_snapshot"].get("branch")
            if b:
                return str(b)
    return None


def ledger_note_for(strategy: str | None, rationale: Any) -> tuple[str | None, str | None]:
    """(note, reason)；没有备注 → (None, None)。"""
    if strategy == "SHADOW_N4":
        return TIER_WATER_NOTE, TIER_WATER_NOTE_REASON
    if strategy == "SHADOW_S2" and _branch_of(rationale) == "b_high_open_rise_water":
        return TIER_WATER_NOTE, TIER_WATER_NOTE_REASON
    return None, None
