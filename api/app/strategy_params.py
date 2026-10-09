"""策略/模型的「调优参数」统一入口（公开仓）。

公开仓只提供结构：`config/strategy_params.example.json`（全部为 null 占位）。
你自己的参数写在 `config/strategy_params.json`（已被 .gitignore 排除，永不提交），
或用环境变量 STRATEGY_PARAMS_PATH 指向任意位置的 JSON。

取值为 null（未配置）时，调用 get() 会抛 StrategyParamsMissing，相关方案拒绝运行，
而不是悄悄用某个默认数字——避免把未经你验证的阈值当成结论。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_PATH = REPO_ROOT / "config" / "strategy_params.example.json"
DEFAULT_PATH = REPO_ROOT / "config" / "strategy_params.json"


class StrategyParamsMissing(RuntimeError):
    """需要的策略参数未配置（公开仓默认如此）。"""


def params_path() -> Path:
    env = os.environ.get("STRATEGY_PARAMS_PATH", "").strip()
    if env:
        p = Path(env).expanduser()
        return p if p.is_absolute() else REPO_ROOT / p
    return DEFAULT_PATH if DEFAULT_PATH.exists() else EXAMPLE_PATH


_cache: dict[str, Any] = {"key": None, "data": None}


def load() -> dict:
    p = params_path()
    try:
        key = (str(p), p.stat().st_mtime)
    except FileNotFoundError:
        return {}
    if _cache["key"] != key:
        _cache.update(key=key, data=json.loads(p.read_text(encoding="utf-8")))
    return _cache["data"] or {}


def get_or_none(section: str, key: str) -> Any:
    v = (load().get(section) or {}).get(key)
    return None if isinstance(v, dict) else v


def get(section: str, key: str) -> Any:
    v = get_or_none(section, key)
    if v is None:
        raise StrategyParamsMissing(
            f"策略参数 {section}.{key} 未配置：请在 config/strategy_params.json（参照 "
            f"config/strategy_params.example.json）中填写你自己的值，或设置 STRATEGY_PARAMS_PATH")
    return v


def is_configured() -> bool:
    """所有段的所有键都已填写（非 null）→ True。"""
    data = load()
    if not data or params_path() == EXAMPLE_PATH:
        return False
    for sec, kv in data.items():
        if sec.startswith("_") or not isinstance(kv, dict):
            continue
        for k, v in kv.items():
            if not k.startswith("_") and v is None:
                return False
    return True
