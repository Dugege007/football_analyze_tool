"""规定通道钟点（v2.0 / API 0.3.9+；早场带 2026-10-07 扩至 [00:00, 11:30]）。

channel:
  rule        — 日用默认：开赛 ≥23:00 或早场特殊带 → mid=竞彩日 15:00、close=当天 22:00；
                其它白天场 → T−8h / T−1h（时钟落在 jingcai_date）。
  rule_legacy — 旧口径对照：仅早场特殊带 → 竞彩日 16:00 / 23:00；不覆盖旧手工 mid/close。
  actual      — T−8h / T−1h 目标时刻（真实 tick 另由 timeline 取 last ≤ target）。

早场特殊带（与 作者的私有分析仓 私有仓 PR / analysis_window_cutoff 同谓词；只管「属于哪个竞彩日」）：
  - 有分钟：开赛时刻 ∈ [00:00, 11:30]（含端）
  - 仅整点：0 <= h <= 11（11:31–11:59 需分钟才能排除）

例外场（phase_exception；只管中盘/临盘规则值；术语文档「例外场范围」2026-10-08 终版）：
  - 开赛 ∈ [23:00, 次日 11:30]，两端都含（与竞彩日 cutoff [00:00, 11:30] 一致；11:30 整 → 例外）
  - 仅整点：h >= 23 或 0 <= h <= 11
  - 例外场：中盘（规则）= 竞彩日 15:00，临盘（规则）= 竞彩日 22:00
  - 非例外场：T−8h / T−1h **精确到分钟**（开赛分钟已知时；分钟不明仍按整点）

依据：v2_0-odds-timeline-storage-design.md §1.2 / §3.3；
      v2_0-jingcai-day-early-kickoff.md；（日用通知节奏文档，未公开）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

TZ_CN = timezone(timedelta(hours=8))

# 早场特殊带右端：11:30（含）。仅有整点时无法排除 11:31–11:59，整点 11 全部视为早场。
EARLY_BAND_END_MINUTE_OF_DAY = 11 * 60 + 30

# 整点回退下的「固定钟点」小时集合：早场 0–11 ∪ {23}
# 有分钟时请用 is_early_kickoff_band / is_rule_fixed_clock(..., minute=...)。
OVERNIGHT_OR_LATE_HOURS = set(range(0, 12)) | {23}


def is_early_kickoff_band(kickoff_hour: int, minute: Optional[int] = None) -> bool:
    """连续窗／竞彩日早场特殊带：开赛时刻 ∈ [00:00, 11:30]（含端点）。

    minute 为 None 时只能用整点：`0 <= h <= 11`。
    11:31–11:59 需要分钟字段才能排除；补齐 kickoff_minute / kickoff_at 后再收紧。
    """
    h = int(kickoff_hour)
    if minute is not None:
        t = h * 60 + int(minute)
        return 0 <= t <= EARLY_BAND_END_MINUTE_OF_DAY
    # 仅有整点：11:31–11:59 无法排除，暂把整点 11 全部视为早场
    return 0 <= h <= 11


# 例外场右端：11:30（含）。2026-10-08 足球分析师终版 [23:00, 次日 11:30]（两端都含）。
PHASE_EXCEPTION_END_MINUTE_OF_DAY = EARLY_BAND_END_MINUTE_OF_DAY


def is_phase_exception(kickoff_hour: int, minute: Optional[int] = None) -> bool:
    """例外场：开赛 ∈ [23:00, 次日 11:30]（两端都含；= ≥23 ∪ 竞彩日早场带）。

    minute 为 None（分钟不明）时按整点：h >= 23 或 0 <= h <= 11。
    """
    h = int(kickoff_hour)
    if h >= 23:
        return True
    if minute is not None:
        return 0 <= h * 60 + int(minute) <= PHASE_EXCEPTION_END_MINUTE_OF_DAY
    return 0 <= h <= 11


# ---------------------------------------------------------------- 例外场按竞彩编号（0.3.18）
# 术语文档末节（2026-10-08，取代 11:30 上限）：有竞彩编号 → 属于竞彩日 D 且开赛 ≥ D 日 23:00 即例外，无上限；
# 无编号 → 开赛 ∈ [D 23:00, D+1 11:30]（两端都含）。竞彩日归属一律以编号为准，不拿开赛时间反推。
EXCEPTION_RULE = "jc_code_ge_2300"
EXCEPTION_RULE_LEGACY = "time_window_2300_1130"


def phase_exception(
    jingcai_date: Optional[str],
    kickoff: Optional[datetime],
    kickoff_hour: Optional[int],
    minute_known: bool = True,
    has_jc_code: bool = True,
) -> bool:
    """0.3.18 例外场判定（exception_rule=jc_code_ge_2300）。

    - 有编号 + 有开赛时刻：kickoff ≥ D 23:00（分钟不明按整点）。
    - 无编号 + 有开赛时刻：D 23:00 ≤ kickoff ≤ D+1 11:30。
    - 没有开赛时刻（只有整点）：整点 ≥23 → 例外；0–11（只能是 D+1 早场）→ 例外；
      其余（12–22，按编号视为 D 日白天）→ 非例外。
    """
    base = _parse_jingcai_base(jingcai_date) if jingcai_date else None
    if kickoff is not None and base is not None:
        k = kickoff if kickoff.tzinfo is not None else kickoff.replace(tzinfo=TZ_CN)
        k = k.astimezone(TZ_CN).replace(second=0, microsecond=0)
        if not minute_known:
            k = k.replace(minute=0)
        start = base.replace(hour=23, minute=0, second=0, microsecond=0)
        if has_jc_code:
            return k >= start
        end = (base + timedelta(days=1)).replace(hour=11, minute=30, second=0, microsecond=0)
        return start <= k <= end
    if kickoff_hour is None:
        return False
    return is_phase_exception(int(kickoff_hour), None)


_WEEKDAY_CN = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


def jingcai_date_from_code(jc_code: Optional[str], kickoff: datetime) -> Optional[str]:
    """竞彩日归属按编号（0.3.18 铁律）：编号里的星期（周六008 / 六008）= 竞彩日 D 的星期；
    D = 开赛自然日往前推到最近一个同星期的日子（0–6 天）。拿不到编号 → None（调用方再决定回退）。"""
    if not jc_code:
        return None
    code = str(jc_code).strip().lstrip("周")
    wd = _WEEKDAY_CN.get(code[:1])
    if wd is None:
        return None
    k = kickoff if kickoff.tzinfo is not None else kickoff.replace(tzinfo=TZ_CN)
    k = k.astimezone(TZ_CN)
    back = (k.weekday() - wd) % 7
    return (k - timedelta(days=back)).strftime("%Y-%m-%d")


def is_rule_fixed_clock(kickoff_hour: int, minute: Optional[int] = None) -> bool:
    """True → rule 用竞彩日 15:00/22:00；False → rule = T−8h/T−1h（精确到分钟）。

    = is_phase_exception（[23:00, 次日 11:30] 两端都含；与 0.3.15 行为一致）。
    """
    return is_phase_exception(kickoff_hour, minute)


def _hhmm(total_minutes: int) -> str:
    total_minutes %= 24 * 60
    return f"{total_minutes // 60:02d}:{total_minutes % 60:02d}"


def is_rule_legacy_applicable(kickoff_hour: int, minute: Optional[int] = None) -> bool:
    """rule_legacy 仅对早场特殊带有独立钟点（旧 0–10 已扩至与 A 同谓词）。"""
    return is_early_kickoff_band(int(kickoff_hour), minute)


def calc_collection_times(
    kickoff_hour: int, minute: Optional[int] = None
) -> dict[str, str]:
    """日用 rule：返回 mid_time / final_time（竞彩日当天时钟字符串）。"""
    h = int(kickoff_hour)
    if is_rule_fixed_clock(h, minute):
        return {"mid_time": "15:00", "final_time": "22:00"}
    # 0.3.16：精确到分钟（分钟不明 → 整点）
    t = h * 60 + (int(minute) if minute is not None else 0)
    return {
        "mid_time": _hhmm(t - 8 * 60),
        "final_time": _hhmm(t - 60),
    }


def calc_collection_times_legacy(
    kickoff_hour: int, minute: Optional[int] = None
) -> dict[str, str] | None:
    """旧规定 rule_legacy；非早场特殊带返回 None（无独立对照列）。"""
    h = int(kickoff_hour)
    if not is_rule_legacy_applicable(h, minute):
        return None
    return {"mid_time": "16:00", "final_time": "23:00"}


def _parse_jingcai_base(jingcai_date: str) -> datetime | None:
    try:
        return datetime.strptime(jingcai_date, "%Y-%m-%d").replace(tzinfo=TZ_CN)
    except (TypeError, ValueError):
        return None


def resolve_collection_ats(
    jingcai_date: str, kickoff_hour: int, minute: Optional[int] = None
) -> tuple[datetime, datetime] | None:
    """rule 通道绝对 mid/close（+08:00，落在 jingcai_date 当天）。"""
    if not jingcai_date or kickoff_hour is None:
        return None
    try:
        h = int(kickoff_hour)
    except (TypeError, ValueError):
        return None
    if h < 0 or h > 23:
        return None
    base = _parse_jingcai_base(jingcai_date)
    if base is None:
        return None
    times = calc_collection_times(h, minute)
    mh, mm = map(int, times["mid_time"].split(":"))
    ch, cm = map(int, times["final_time"].split(":"))
    mid_at = base.replace(hour=mh, minute=mm, second=0, microsecond=0)
    close_at = base.replace(hour=ch, minute=cm, second=0, microsecond=0)
    return mid_at, close_at


def resolve_collection_ats_legacy(
    jingcai_date: str, kickoff_hour: int, minute: Optional[int] = None
) -> tuple[datetime, datetime] | None:
    """rule_legacy 绝对 mid/close；不适用则 None。"""
    if not jingcai_date or kickoff_hour is None:
        return None
    try:
        h = int(kickoff_hour)
    except (TypeError, ValueError):
        return None
    times = calc_collection_times_legacy(h, minute)
    if times is None:
        return None
    base = _parse_jingcai_base(jingcai_date)
    if base is None:
        return None
    mh, mm = map(int, times["mid_time"].split(":"))
    ch, cm = map(int, times["final_time"].split(":"))
    return (
        base.replace(hour=mh, minute=mm, second=0, microsecond=0),
        base.replace(hour=ch, minute=cm, second=0, microsecond=0),
    )


def actual_targets(kickoff: datetime) -> dict[str, datetime]:
    """actual 通道目标：t8 = kickoff−8h，t1 = kickoff−1h。"""
    if kickoff.tzinfo is None:
        kickoff = kickoff.replace(tzinfo=TZ_CN)
    return {
        "t8": kickoff - timedelta(hours=8),
        "t1": kickoff - timedelta(hours=1),
    }


# ---------------------------------------------------------------- 推迟场（0.3.19，决议「0.3.18 的五件事」2 + 推迟场补充）
POSTPONE_PENDING_HOURS = 1.0  # 推迟 > 1h → postpone_void_check=pending（澳门时限核实前的临时门槛，决议原文）
POSTPONE_VOID_HOURS: Optional[float] = None  # 澳门推迟作废时限；null = 不生效（钩子，核实后写死并进指纹）
POSTPONE_DELTAS = {"mid": timedelta(hours=8), "close": timedelta(hours=1)}


def _cn(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return (dt if dt.tzinfo is not None else dt.replace(tzinfo=TZ_CN)).astimezone(TZ_CN)


def known_kickoff_target(
    kickoff_original: datetime,
    kickoff_actual: Optional[datetime],
    announced_at: Optional[datetime],
    delta: timedelta,
) -> tuple[datetime, str]:
    """目标时刻只用到「该时刻为止已经公布的开赛时间」：

    t_orig = 原定 − Δ。推迟消息时刻 ≤ t_orig（目标之前已公布）→ 用新开赛 − Δ（basis=announced_new）；
    消息晚于 t_orig → t_orig（basis=original）；查不到消息时刻 → t_orig（basis=original_ts_unknown）。
    """
    ko = _cn(kickoff_original)
    t_orig = ko - delta
    ka = _cn(kickoff_actual)
    if ka is None or ka == ko:
        return t_orig, "original"
    an = _cn(announced_at)
    if an is None:
        return t_orig, "original_ts_unknown"
    if an <= t_orig:
        return ka - delta, "announced_new"
    return t_orig, "original"


def postpone_targets(
    kickoff_original: datetime,
    kickoff_actual: Optional[datetime],
    announced_at: Optional[datetime],
) -> dict[str, Any]:
    """推迟场的 T−8h / T−1h（真实列；非例外场的规则列同此）：{"mid": dt, "close": dt, "basis": {...}}。"""
    out: dict[str, Any] = {"basis": {}}
    for k, d in POSTPONE_DELTAS.items():
        t, b = known_kickoff_target(kickoff_original, kickoff_actual, announced_at, d)
        out[k] = t
        out["basis"][k] = b
    return out


def postpone_void_check(kickoff_original: Optional[datetime], kickoff_actual: Optional[datetime]) -> Optional[str]:
    """推迟 > POSTPONE_PENDING_HOURS → pending（暂不结算）；否则 None。"""
    ko, ka = _cn(kickoff_original), _cn(kickoff_actual)
    if ko is None or ka is None:
        return None
    return "pending" if (ka - ko) > timedelta(hours=POSTPONE_PENDING_HOURS) else None


def rule_targets(
    jingcai_date: str,
    kickoff_hour: int,
    kickoff: Optional[datetime],
    minute_known: bool = True,
    has_jc_code: Optional[bool] = None,
) -> tuple[datetime, datetime] | None:
    """rule 通道中盘/临盘目标时刻（0.3.16，phase_target=exact_minute）。

    - 例外场 [23:00, 次日 11:30]：竞彩日 15:00 / 22:00（不变）
    - 其它：kickoff − 8h / kickoff − 1h，精确到分钟；minute_known=False → 开赛取整点再减
    - kickoff 缺失：回退 resolve_collection_ats（竞彩日 + 时钟）
    """
    if kickoff_hour is None:
        return None
    try:
        h = int(kickoff_hour)
    except (TypeError, ValueError):
        return None
    minute = int(kickoff.minute) if (kickoff is not None and minute_known) else None
    if has_jc_code is not None:
        # 0.3.18：例外场按编号（phase_exception）→ 竞彩日 D 15:00 / 22:00
        if phase_exception(jingcai_date, kickoff, h, minute_known, has_jc_code):
            base = _parse_jingcai_base(jingcai_date)
            if base is None:
                return None
            return (base.replace(hour=15, minute=0, second=0, microsecond=0),
                    base.replace(hour=22, minute=0, second=0, microsecond=0))
        if kickoff is None:
            return resolve_collection_ats(jingcai_date, h, minute)
    elif kickoff is None or is_phase_exception(h, minute):
        return resolve_collection_ats(jingcai_date, h, minute)
    if kickoff.tzinfo is None:
        kickoff = kickoff.replace(tzinfo=TZ_CN)
    k = kickoff.astimezone(TZ_CN).replace(second=0, microsecond=0)
    if not minute_known:
        k = k.replace(minute=0)
    return k - timedelta(hours=8), k - timedelta(hours=1)


PHASE_TARGET = "exact_minute"  # 指纹：0.3.16 起；旧导出 = "hour_floor"


# ---------------------------------------------------------------- 开赛占位符（0316 follow-up 决策 1，0.3.17）
KICKOFF_PLACEHOLDER_5DF_1200 = "5df_1200"
KICKOFF_SOURCE_5DF = "5df"
KICKOFF_SOURCE_JINGCAI = "jingcai"


def synth_jingcai_kickoff(jingcai_date: str, kickoff_hour: int) -> datetime | None:
    """竞彩时刻（仅整点）→ 自然日时刻：早场特殊带 0–11 → 次日；其余同竞彩日（与 main.resolve_kickoff_dt 合成同口径）。"""
    base = _parse_jingcai_base(jingcai_date)
    if base is None or kickoff_hour is None:
        return None
    h = int(kickoff_hour)
    if not 0 <= h <= 23:
        return None
    if is_early_kickoff_band(h):
        base = base + timedelta(days=1)
    return base.replace(hour=h, minute=0, second=0, microsecond=0)


def check_kickoff_placeholder(
    kickoff_5df: Optional[datetime],
    jingcai_date: Optional[str],
    jc_kickoff_hour: Optional[int],
    jc_kickoff_at: Optional[datetime] = None,
    minute_known: bool = True,
) -> dict[str, Any]:
    """5DF 开赛恰为 12:00 的占位符检查（导入前校验 + API 读时共用）。

    规则（决策文档 1）：
    - 5DF 开赛不是 12:00 → 不适用（kickoff_source=5df，不改）。
    - 5DF = 12:00 且有竞彩官方时刻：
        * 有完整竞彩时刻 jc_kickoff_at：相等 → 一致；不等 → 用竞彩（kickoff_source=jingcai，
          kickoff_placeholder=5df_1200，分钟可信）。
        * 只有竞彩整点 jc_kickoff_hour（库内现状）：钟点 = 12 → 一致（竞彩整点不带自然日，不拿合成日期判冲突，
          只在 date_differs_from_jingcai_synth 上记一笔）；钟点 ≠ 12 → 用竞彩整点合成时刻
          （kickoff_source=jingcai，分钟未知 → hour_floor，kickoff_placeholder=5df_1200）。
    - 5DF = 12:00 且没有竞彩时刻 → 分钟按未知（hour_floor），kickoff_placeholder=5df_1200。
    返回 dict：status / kickoff / minute_known / kickoff_source / kickoff_placeholder / phase_target / note 等。
    """
    out: dict[str, Any] = {
        "status": "not_applicable", "kickoff": kickoff_5df, "minute_known": bool(minute_known),
        "kickoff_source": KICKOFF_SOURCE_5DF if kickoff_5df is not None else None,
        "kickoff_placeholder": None, "phase_target": PHASE_TARGET if minute_known else "hour_floor",
        "jingcai_time": None, "date_differs_from_jingcai_synth": None,
    }
    if kickoff_5df is None:
        return out
    k = kickoff_5df if kickoff_5df.tzinfo is not None else kickoff_5df.replace(tzinfo=TZ_CN)
    k = k.astimezone(TZ_CN)
    if not (k.hour == 12 and k.minute == 0):
        return out
    if jc_kickoff_at is not None:
        jk = jc_kickoff_at if jc_kickoff_at.tzinfo is not None else jc_kickoff_at.replace(tzinfo=TZ_CN)
        jk = jk.astimezone(TZ_CN)
        out["jingcai_time"] = jk.isoformat()
        if jk.replace(second=0, microsecond=0) == k.replace(second=0, microsecond=0):
            out["status"] = "agrees_jingcai"
            return out
        out.update({"status": "placeholder_use_jingcai", "kickoff": jk, "minute_known": True,
                    "kickoff_source": KICKOFF_SOURCE_JINGCAI,
                    "kickoff_placeholder": KICKOFF_PLACEHOLDER_5DF_1200, "phase_target": PHASE_TARGET})
        return out
    if jc_kickoff_hour is not None:
        out["jingcai_time"] = f"{int(jc_kickoff_hour):02d}:00 (hour only)"
        synth = synth_jingcai_kickoff(jingcai_date, int(jc_kickoff_hour)) if jingcai_date else None
        if int(jc_kickoff_hour) == 12:
            out["status"] = "agrees_jingcai_hour"
            out["date_differs_from_jingcai_synth"] = (synth is not None and synth.date() != k.date())
            return out
        if synth is not None:
            out.update({"status": "placeholder_use_jingcai", "kickoff": synth, "minute_known": False,
                        "kickoff_source": KICKOFF_SOURCE_JINGCAI,
                        "kickoff_placeholder": KICKOFF_PLACEHOLDER_5DF_1200, "phase_target": "hour_floor"})
            return out
    out.update({"status": "placeholder_no_jingcai", "minute_known": False,
                "kickoff": k.replace(minute=0, second=0, microsecond=0),
                "kickoff_placeholder": KICKOFF_PLACEHOLDER_5DF_1200, "phase_target": "hour_floor"})
    return out


def channel_targets(
    *,
    jingcai_date: str,
    kickoff_hour: int,
    kickoff: datetime,
    minute_known: bool = True,
    has_jc_code: Optional[bool] = None,
    postpone: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """汇总三通道 target_at，供 snapshot 写入。

    postpone（0.3.19）：{"kickoff_original", "kickoff_actual", "announced_at"}；给定时例外按编号 + 原定开赛判，
    actual t8/t1（及非例外场的 rule mid/close）按「到目标时刻为止已公布的开赛时间」算（postpone_targets）。

    has_jc_code（0.3.18）：给定时例外场按编号判（exception_rule=jc_code_ge_2300）；None = 旧时间窗口径。

    返回::
      {
        "rule": {"mid": dt, "close": dt},
        "rule_legacy": {"mid": dt, "close": dt} | None,
        "actual": {"t8": dt, "t1": dt},
      }

    有 kickoff 时用其分钟做早场带判定（对齐 私有仓 PR 分钟感知谓词）。
    """
    if kickoff.tzinfo is None:
        kickoff = kickoff.replace(tzinfo=TZ_CN)
    minute = int(kickoff.minute) if minute_known else None
    rule = rule_targets(jingcai_date, kickoff_hour, kickoff, minute_known, has_jc_code=has_jc_code)
    legacy = resolve_collection_ats_legacy(jingcai_date, kickoff_hour, minute)
    act = actual_targets(kickoff if minute_known else kickoff.replace(minute=0, second=0, microsecond=0))
    if postpone and postpone.get("kickoff_original") is not None:
        k0 = _cn(postpone["kickoff_original"])
        pt = postpone_targets(k0, postpone.get("kickoff_actual"), postpone.get("announced_at"))
        exc = phase_exception(jingcai_date, k0, kickoff_hour, minute_known, bool(has_jc_code))
        rule = rule_targets(jingcai_date, kickoff_hour, k0, minute_known, has_jc_code=bool(has_jc_code)) if exc \
            else (pt["mid"], pt["close"])
        act = {"t8": pt["mid"], "t1": pt["close"]}
    out: dict[str, Any] = {
        "rule": {"mid": rule[0], "close": rule[1]} if rule else None,
        "rule_legacy": {"mid": legacy[0], "close": legacy[1]} if legacy else None,
        "actual": act,
    }
    return out
