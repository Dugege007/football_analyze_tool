"""基本面／竞彩读路径辅助（0.3.21）：as_of 闸门、hhad 决策时刻选线、recent/h2h 成分场过滤。

不实现 local collector／BSD 采盘；只提供纯函数与只读选行，供 /table/matches 与单测使用。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

TZ_CN = timezone(timedelta(hours=8))
XG_RESULT_GATE = timedelta(hours=3)  # 同 N5：kickoff+3h ≤ T_decision
DEFAULT_MIN_CONSTITUENTS = 3  # 过滤后样本过薄 → 当缺失
LIVE_1110_OWN_WINDOW_MIN = 10  # 与亚盘一致：[11:00, 11:20]


def parse_cn(ts: Any) -> Optional[datetime]:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        dt = ts
    else:
        s = str(ts).strip()
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ_CN)
    return dt.astimezone(TZ_CN)


def jc_1110_in_window(captured_at: Any, jingcai_date: str) -> tuple[bool, Optional[float]]:
    """竞彩 11:10 与亚盘同窗：captured_at ∈ [11:00, 11:20]（|lag|≤10 分钟，含端）。

    返回 (in_window, fetch_lag_min)。jingcai_date 无效 → (False, None)。
    """
    cap = parse_cn(captured_at)
    try:
        target = datetime.strptime(jingcai_date, "%Y-%m-%d").replace(
            hour=11, minute=10, second=0, tzinfo=TZ_CN
        )
    except (TypeError, ValueError):
        return False, None
    if cap is None:
        return False, None
    lag = (cap - target).total_seconds() / 60.0
    return abs(lag) <= LIVE_1110_OWN_WINDOW_MIN + 1e-9, round(lag, 3)


def filter_constituent_matches(
    payload: dict | None,
    t_decision: datetime,
    *,
    min_n: int = DEFAULT_MIN_CONSTITUENTS,
    list_keys: tuple[str, ...] = ("matches", "constituents", "items"),
) -> dict:
    """recent/h2h 成分场闸门（同 N5）：每场须 kickoff+3h ≤ T_decision；过滤后过薄 → missing。

    契约：
    - payload 若含 matches/constituents/items 列表，且元素带 kickoff_at（或 kickoff），按闸门过滤。
    - 过滤后保留列表写入 out["matches"]，并带 n_before / n_after / missing / reason。
    - 若无成分列表（当前手工底座多为聚合 last10/last6，无逐场 kickoff）→
      constituent_gate="unavailable"：调用方不得把 as_of≤T 当成「成分已闸」；特征层应待有逐场后再生效。
    - missing=True 时 usable_payload=None（当缺失，不是 0）。
    """
    out: dict[str, Any] = {
        "constituent_gate": "unavailable",
        "missing": False,
        "reason": None,
        "n_before": None,
        "n_after": None,
        "usable_payload": payload,
        "min_n": min_n,
        "gate": "kickoff_plus_3h_le_t_decision",
    }
    if not isinstance(payload, dict):
        out["missing"] = True
        out["reason"] = "no_payload"
        out["usable_payload"] = None
        return out

    items = None
    used_key = None
    for k in list_keys:
        v = payload.get(k)
        if isinstance(v, list):
            items, used_key = v, k
            break
    if items is None:
        out["reason"] = "no_constituent_list"
        return out

    out["constituent_gate"] = "applied"
    t_decision = parse_cn(t_decision) or t_decision
    kept: list[Any] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        ko = parse_cn(it.get("kickoff_at") or it.get("kickoff"))
        if ko is None:
            continue  # 无开赛时刻的成分不进可用集
        if ko + XG_RESULT_GATE <= t_decision:
            kept.append(it)
    out["n_before"] = len(items)
    out["n_after"] = len(kept)
    if len(kept) < min_n:
        out["missing"] = True
        out["reason"] = "sample_too_thin_after_constituent_gate"
        out["usable_payload"] = None
        return out
    usable = dict(payload)
    usable[used_key] = kept
    if used_key != "matches":
        usable["matches"] = kept
    out["usable_payload"] = usable
    return out


def select_hhad_at_decision(
    main_row: dict | None,
    hist_rows: list[dict],
    t_decision: datetime,
) -> dict:
    """决策时刻让球线：禁止默认读主表当前行。

    选 captured_at ≤ T_decision 且 (superseded_at is null OR superseded_at > T_decision) 的版本；
    主表当前行视为 superseded_at=null；hist 行必须带 superseded_at。

    返回：
      decision_line / decision_odds / from_hist / post_decision_line_change /
      current_line (主表，可悬停) / missing
    """
    t_decision = parse_cn(t_decision) or t_decision
    versions: list[dict] = []
    for h in hist_rows or []:
        versions.append({**h, "_from_hist": True})
    if main_row:
        versions.append({**main_row, "superseded_at": None, "_from_hist": False})

    eligible: list[tuple[datetime, dict]] = []
    for v in versions:
        cap = parse_cn(v.get("captured_at"))
        if cap is None or cap > t_decision:
            continue
        sup = parse_cn(v.get("superseded_at"))
        if sup is not None and sup <= t_decision:
            continue
        eligible.append((cap, v))
    eligible.sort(key=lambda x: x[0], reverse=True)

    current_line = (main_row or {}).get("goal_line")
    if not eligible:
        return {
            "missing": True,
            "decision_line": None,
            "home": None, "draw": None, "away": None,
            "from_hist": False,
            "post_decision_line_change": False,
            "current_line": current_line,
            "line_rev": (main_row or {}).get("line_rev"),
            "captured_at": None,
            "superseded_at": None,
            "reason": "no_line_visible_at_decision",
        }
    _, chosen = eligible[0]
    dec_line = chosen.get("goal_line")
    post_change = False
    if current_line is not None and dec_line is not None:
        try:
            post_change = float(current_line) != float(dec_line)
        except (TypeError, ValueError):
            post_change = current_line != dec_line
    elif main_row and parse_cn((main_row or {}).get("captured_at")) and \
            parse_cn(main_row.get("captured_at")) > t_decision:
        post_change = True

    return {
        "missing": False,
        "decision_line": dec_line,
        "home": chosen.get("home_odds", chosen.get("home")),
        "draw": chosen.get("draw_odds", chosen.get("draw")),
        "away": chosen.get("away_odds", chosen.get("away")),
        "from_hist": bool(chosen.get("_from_hist")),
        "post_decision_line_change": bool(post_change),
        "current_line": current_line,
        "line_rev": chosen.get("line_rev", (main_row or {}).get("line_rev")),
        "captured_at": chosen.get("captured_at"),
        "superseded_at": chosen.get("superseded_at"),
        "jc_1x2_incomplete": bool(chosen.get("jc_1x2_incomplete") or 0),
        "source": chosen.get("source"),
        "reason": None,
    }


def jc_incomplete(home: Any, draw: Any, away: Any) -> bool:
    """缺任一项或 ≤0 → incomplete。"""
    for x in (home, draw, away):
        try:
            if x is None or float(x) <= 0:
                return True
        except (TypeError, ValueError):
            return True
    return False
