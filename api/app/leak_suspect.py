"""leak_suspect 叠加配置（0.3.20）。

依据：docs/schema/v2_0-repo-jc-order-fix-plan.md「V4–V7 历史数字的处理」（足球分析师采纳算法顾问）：
LEGACY V4、V5、V6、V7 的 cutoff 钟点被套了「0–10 点 +24」规则（cutoff_plus24），回测 / 复盘数字疑似泄漏，
修好前不引用、不和 V3 比；前端看到 leak_suspect 就灰掉。V3 和其它策略为 null。

做法：不写库。配置在 config/leak_suspect.json，API 读出后叠加到 /strategies、/strategies/{key}/versions、
/strategies/compare、/strategies/{id}/validate、/strategies/runs/{id}、/table/matches、/matches/{id}/prediction(s)。
现网 8787 和 v2d3 8788 读同一份文件，行为一致。

匹配：strategy_key 精确匹配，或条目 aliases 里列出的名字精确匹配；不做前缀匹配
（修好后重跑的 V4-F…V7-F 是新版本码，不能被误标）。
可选：若以后经批准给 strategy_defs 加了 leak_suspect 列（scripts/migrate_leak_suspect_live.py），
配置里没有的 key 才回退读列值；配置永远优先。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
# 公开仓：真实文件 config/leak_suspect.json 由你自己的数据生成（已 .gitignore）；不存在时退回合成示例
_REAL_PATH = ROOT / "config" / "leak_suspect.json"
EXAMPLE_PATH = ROOT / "config" / "leak_suspect.example.json"
CONFIG_PATH = _REAL_PATH if _REAL_PATH.exists() else EXAMPLE_PATH
KNOWN_VALUES = frozenset({"cutoff_plus24"})

_cache: dict[str, Any] = {"mtime": None, "path": None, "data": None}


def load(path: Path | None = None) -> dict:
    p = Path(path or CONFIG_PATH)
    try:
        mt = p.stat().st_mtime
    except FileNotFoundError:
        return {"entries": []}
    if _cache["path"] == str(p) and _cache["mtime"] == mt and _cache["data"] is not None:
        return _cache["data"]
    data = json.loads(p.read_text(encoding="utf-8"))
    validate(data)
    _cache.update(mtime=mt, path=str(p), data=data)
    return data


def validate(data: dict) -> None:
    seen: set[str] = set()
    for e in data.get("entries") or []:
        v = e.get("leak_suspect")
        if v not in KNOWN_VALUES:
            raise ValueError(f"leak_suspect.json: unknown value {v!r} for {e.get('strategy_key')!r}")
        for k in [e["strategy_key"], *(e.get("aliases") or [])]:
            if k in seen:
                raise ValueError(f"leak_suspect.json: duplicate key {k!r}")
            seen.add(k)


def _index(data: dict) -> dict[str, dict]:
    idx: dict[str, dict] = {}
    for e in data.get("entries") or []:
        for k in [e["strategy_key"], *(e.get("aliases") or [])]:
            idx[k] = e
    return idx


def entry_for(strategy_key: Optional[str]) -> Optional[dict]:
    if not strategy_key:
        return None
    return _index(load()).get(str(strategy_key))


def leak_suspect_for(strategy_key: Optional[str], db_value: Optional[str] = None) -> Optional[str]:
    e = entry_for(strategy_key)
    if e is not None:
        return e["leak_suspect"]
    return db_value or None


def fields_for(strategy_key: Optional[str], db_value: Optional[str] = None) -> dict:
    e = entry_for(strategy_key)
    if e is not None:
        return {"leak_suspect": e["leak_suspect"], "leak_suspect_note": e.get("note"),
                "leak_suspect_source": "config/leak_suspect.json"}
    if db_value:
        return {"leak_suspect": db_value, "leak_suspect_note": None, "leak_suspect_source": "strategy_defs.leak_suspect"}
    return {"leak_suspect": None, "leak_suspect_note": None, "leak_suspect_source": None}


def registry(db_keys: set[str] | None = None) -> dict:
    """完整登记表（含不在本库 strategy_defs 里的 key）；in_db = 本实例库里有没有这个 strategy_key / alias。"""
    data = load()
    items = []
    for e in data.get("entries") or []:
        names = [e["strategy_key"], *(e.get("aliases") or [])]
        items.append({**e, "in_db": (bool(set(names) & db_keys) if db_keys is not None else None)})
    return {"config_path": str(CONFIG_PATH.relative_to(ROOT)), "version": data.get("version"),
            "source": data.get("source"), "match_rule": data.get("match_rule"),
            "values": data.get("values"), "items": items}
