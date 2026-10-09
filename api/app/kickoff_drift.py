"""kickoff_drift_min（0.3.20，高亮文档「0.3.19 后续四件」第 4 条）：只作信息展示。

kickoff_drift_min = 实际开赛 − 赛程开赛（分钟，四舍五入取整）。
- 赛程开赛 = 本接口用的 kickoff_at（推迟场 = 已公布的新开赛时间；所以推迟本身不算 drift，推迟看 postpone_* 字段）。
- 实际开赛 = 澳门亚盘场中首笔报价时刻 − 当时比赛分钟（est_kickoff_at），来自 config/kickoff_drift.json
  （scripts/build_kickoff_drift.py 由推迟排查清单 postpone_scan_all.csv 生成；精度约 1 分钟）。
- as-of：只在 as_of ≥ 场中首笔报价时刻（first_inplay_at）后才给，赛前不给。
- 不进推迟口径、不改目标时刻、不进日核对、不进特征和策略。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
# 公开仓：真实文件 config/kickoff_drift.json 由你自己的数据生成（已 .gitignore）；不存在时退回合成示例
_REAL_PATH = ROOT / "config" / "kickoff_drift.json"
EXAMPLE_PATH = ROOT / "config" / "kickoff_drift.example.json"
CONFIG_PATH = _REAL_PATH if _REAL_PATH.exists() else EXAMPLE_PATH
BASIS = "inplay_tick_est"

_cache: dict[str, Any] = {"mtime": None, "data": None}


def _parse(v: Any) -> Optional[datetime]:
    if not v or not isinstance(v, str):
        return None
    try:
        return datetime.fromisoformat(v)
    except ValueError:
        return None


def load() -> dict:
    try:
        mt = CONFIG_PATH.stat().st_mtime
    except FileNotFoundError:
        return {"items": {}}
    if _cache["mtime"] == mt and _cache["data"] is not None:
        return _cache["data"]
    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    _cache.update(mtime=mt, data=data)
    return data


def drift_fields(match_uid: str, kickoff: Optional[datetime], as_of: datetime) -> dict:
    blank = {"kickoff_drift_min": None, "kickoff_drift_basis": None, "kickoff_drift_est_at": None}
    it = (load().get("items") or {}).get(match_uid)
    if not it or kickoff is None:
        return blank
    est, seen = _parse(it.get("est_kickoff_at")), _parse(it.get("first_inplay_at"))
    if est is None or seen is None or as_of < seen:
        return blank
    return {"kickoff_drift_min": int(round((est - kickoff).total_seconds() / 60.0)),
            "kickoff_drift_basis": BASIS, "kickoff_drift_est_at": est.isoformat()}


def config_block() -> dict:
    d = load()
    return {"field": "match.kickoff_drift_min", "unit": "minutes (int)", "info_only": True,
            "definition": "est actual kickoff (first macau in-play tick − match minute) − kickoff_at",
            "as_of": "only when as_of >= first in-play tick", "source": "config/kickoff_drift.json",
            "n_items": len(d.get("items") or {}), "generated_from": d.get("generated_from"),
            "not_affecting": ["postponed / postpone_* fields", "phase targets", "daily_check", "features / strategies"]}
