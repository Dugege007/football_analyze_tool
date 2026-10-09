"""水位 ↔ 档位（旧手工 JSON 的 crown/william home_water/away_water 是档位 t，不是水位）。

口径（用户确认 2026-10-06）：
- 档位 t ∈ [0, 10]，0.5 步进；每 0.5 档差 0.025 水。
- 中点水位 w = 0.70 + 0.05 × t（4.5→0.925，5→0.95，5.5→0.975，6→1.00，6.5→1.025）。
- t = 0 表示 w ≤ 0.70，t = 10 表示 w ≥ 1.20：两头是**截断值**（censored）。
- 反向 water_to_tier(w) = clamp(round_half_up((w − 0.70) / 0.025) / 2, 0, 10)。
  用 Decimal + ROUND_HALF_UP，避开 Python round() 的银行家舍入和二进制浮点误差
  （例如 0.7125 → x=0.5 → 1 → 0.5 档；用 round() 会得到 0）。
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

TIER_MIN = Decimal("0")
TIER_MAX = Decimal("10")
W0 = Decimal("0.70")
PER_TIER = Decimal("0.05")
HALF_STEP_WATER = Decimal("0.025")
TIER_FORMULA = "w = 0.70 + 0.05 * t"


def _dec(v: Any) -> Decimal:
    return Decimal(str(v))


def tier_to_mid(t: Any) -> float:
    """档位 → 中点水位（保留 4 位小数）。"""
    return float((W0 + PER_TIER * _dec(t)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def water_to_tier(w: Any) -> float:
    """真实水位 → 档位（0.5 步进，截断到 [0, 10]）。"""
    x = (_dec(w) - W0) / HALF_STEP_WATER
    steps = x.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    t = steps / Decimal("2")
    t = max(TIER_MIN, min(TIER_MAX, t))
    return float(t)


def is_censored_tier(t: Any) -> bool:
    """t ≤ 0 或 t ≥ 10：水位只知道 ≤0.70 / ≥1.20。"""
    d = _dec(t)
    return d <= TIER_MIN or d >= TIER_MAX


def convert_tier_pair(home_t: Any, away_t: Any) -> dict[str, Any]:
    """一行盘口的档位 → 中点水位 + 截断标记 + 原始档位（供迁移与导入共用）。"""
    def one(t: Any) -> tuple[float | None, bool | None]:
        if t is None:
            return None, None
        return tier_to_mid(t), is_censored_tier(t)
    hw, hc = one(home_t)
    aw, ac = one(away_t)
    return {
        "home_water": hw,
        "away_water": aw,
        "water_src": "tier_midpoint",
        "water_censored": 1 if (hc or ac) else 0,
        "extras": {
            "water_tier_raw": {"home": None if home_t is None else float(home_t),
                               "away": None if away_t is None else float(away_t)},
            "censored": {"home": hc, "away": ac},
            "conversion": TIER_FORMULA,
        },
    }
