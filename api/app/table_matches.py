"""GET /table/matches — 数据表页（AG Grid）只读批量平铺接口（API 0.3.16）。

0.3.16（2026-10-08，§12 五条 + 算法顾问补充，见术语文档末两节）：
- 例外场 [23:00, 次日 11:30]（两端都含）；非例外场 mid/close 目标 = T−8h/T−1h 精确到分钟（分钟不明按整点）。
- 返还率基准剔除旧档位中点换算水位（water_source=tier_midpoint）；真实样本 <20 → baseline_method=fixed_fallback
  （兜底阈值见 hl_v0.1 §4/§5，放 config.return_rate_fallback）。
- 凯利多家平均基准改为「不含本家」（leave-one-out），格子带 n_avg；参考列 multi_avg 仍含全部机构。
- 欧赔 api_closing 不进 close / 返还率 / 凯利 / 高亮；完赛后单独以 x1x2[book].api_closing（label=api_closing）返回。

口径来源（不在本模块里自创）：
- 阶段术语：docs/schema/v2_0-odds-phase-terminology.md
  open=各公司最早一条；mid/close=规则值（collection_schedule.rule）；例外场另给 mid_real/close_real
  （赛前 8h/1h，as-of）；live = 即时盘口（include_live 控制）；last_prematch = 开赛前最后一条。
- 高亮/返还率/凯利：docs/schema/v2_0-data-table-highlight-rules.md（hl_v0）。
  后端只算返还率、按公司滚动中位数基准、凯利；高亮判定在前端。
- 结算：复用 app.backtest（settle_pnl / resolve_juice），不改默认 settlement_version，
  盘口取 odds_asian(settle_book) —— 与 /strategies/{id}/validate 同一取数。
- 防泄漏：比赛未结束（无完整比分或 as_of < 开赛+3h）不返回 result / settlement；
  返还率基准只用竞彩日严格早于本场竞彩日的数据。

只读：连接用 SQLite URI mode=ro，不写任何表。
"""
from __future__ import annotations

import bisect
import json
import sqlite3
import statistics
from datetime import date as date_cls, datetime, timedelta
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app import backtest as bt
from app import collection_schedule as cs
from app import db as app_db
from app import kickoff_drift as kd
from app import leak_suspect as leak
from app import fundamentals as fund  # 0.3.21
from app import open_status as osx  # opening status and source fields (user rule 2026-10-10)

router = APIRouter(tags=["table"])

CONFIG_VERSION = "hl_v0.3.1"  # 0.3.20 起 hl_v0.3.1；0.3.21 竞彩/基本面；0.3.22 并列 ah.macau_5df
# 0.3.19 = hl_v0.3（凯利 +0.02 过渡、返还率兜底按公司 P25、水位异动初→临）；只管上色
# hl_v0.3（v2_0-data-table-highlight-rules.md「hl_v0.3 临时调整」）：只影响显示上色，不进特征和策略
KELLY_HL_MARGIN = 0.02  # 过渡版：轻/中档阈值在 hl_v0.2 基础上各抬 0.02；hl_v0.4 换成按主/平/客分位数
KELLY_HL = {"light": {"rule": "kelly - return_rate >= margin", "margin": KELLY_HL_MARGIN},
            "medium": {"rule": "kelly >= 1.00 + margin", "threshold": round(1.0 + KELLY_HL_MARGIN, 4)},
            "heavy": {"rule": "hl_v0.2 无重档定义 → 仍未定义", "threshold": None}}
FALLBACK_P25_Q = 0.25
FALLBACK_P25_MIN_N = 100  # 某公司真实水位返还率样本 < 100 → 不上色（灰字「样本不足，暂不判断」）
# hl_v0.3.1（0.3.20，高亮文档「0.3.19 后续四件」第 2、3 条）：按 (盘种, 公司, 阶段) 各算分位数，n = 场次数
FALLBACK_QS: dict[str, float] = {"p10": 0.10, "p25": 0.25, "p90": 0.90}
FALLBACK_MIN_N = FALLBACK_P25_MIN_N  # n ≥ 100 场才上色（P10 / P25 / P90 同一个 n）
FALLBACK_HL_LEVELS = {
    "medium": "return_rate < fallback_p10（橙）",
    "light": "fallback_p10 <= return_rate < fallback_p25（黄）",
    "high": "return_rate >= fallback_p90（中性色，只悬停「返还率偏高」；不进告警 / 日核对 / 导出风险统计）",
    "heavy": None,  # hl_v0.3 不设重档；hl_v0.4 和凯利 P97 一起定
}
FALLBACK_ALERT_LEVELS: tuple[str, ...] = ("light", "medium")  # high 不是告警
RETURN_HL_WATER = "real_only"
BOOKS: tuple[str, ...] = ("macau", "crown", "william", "pinnacle")
PHASES: tuple[str, ...] = ("open", "mid", "close")
REAL_PHASES: tuple[str, ...] = ("mid_real", "close_real")
CALC_PHASES = PHASES  # 返还率 / 凯利只按规则值（open/mid/close）计算；*_real 仅原始值（术语文档未写）
RESULT_VISIBLE_AFTER_KICKOFF = timedelta(hours=3)  # 对齐 v2_0-result-ingest-schedule.md「开赛+3h 主拉」
LAST_PREMATCH_FRESH_MINUTES = 15  # 2026-10-08 定：minutes_before_kickoff ∈ [0,15] → stale=false
# 竞彩官网本地采集写入的 source 名称：sporttery_local 为公开名称，sporttery_msi 为旧名称，两者等价。
_JC_LOCAL_SOURCES = ("sporttery_local", "sporttery_msi")
LIVE_RULE_1110 = (11, 10)  # 竞彩日 11:10（即时（11:10），label=rule_1110）
DEFAULT_BASELINE_MIN_N = 20
PHASE_TARGET = cs.PHASE_TARGET  # "exact_minute"（旧导出 = "hour_floor"）
BASELINE_EXCLUDED_WATER_SOURCES = ("tier_midpoint",)  # 旧手工档位中点换算水位：不进返还率基准
BASELINE_METHOD_EMPIRICAL = "empirical"
BASELINE_METHOD_FALLBACK = "fixed_fallback"
# hl_v0.1 §4/§5「基准没算出来前的兜底绝对值」（v2_0-data-table-highlight-rules.md）；前端按此判色
_FB_AH_STD = {"low_light": 0.93, "low_medium": 0.91, "high_light": 0.97}
_FB_X_STD = {"low_light": 0.90, "low_medium": 0.88, "high_light": None}
RETURN_RATE_FALLBACK: dict[str, dict[str, dict[str, float | None]]] = {
    "ah": {"macau": _FB_AH_STD, "crown": _FB_AH_STD, "william": _FB_AH_STD,
           "pinnacle": {"low_light": 0.955, "low_medium": 0.94, "high_light": None}},
    "x1x2": {"macau": _FB_X_STD, "crown": _FB_X_STD, "william": _FB_X_STD,
             "pinnacle": {"low_light": 0.95, "low_medium": 0.93, "high_light": None}},
}
API_CLOSING_LABEL = "api_closing"
API_CLOSING_DISPLAY = "收盘（时间未知）"
DEFAULT_LIMIT = 500
MAX_LIMIT = 2000
FALLBACK_JUICE = 0.95

SCOPE_ALIASES = {"jc": "jingcai", "jingcai": "jingcai", "ext": "extra", "extra": "extra", "all": "all"}
WATER_SRC_MAP = {"actual": "actual", "tier_midpoint": "tier_midpoint"}
# 0.3.18 第二批口径 E：欧赔 / 竞彩格子 water_source。直接报出的赔率 = actual；档位换算 = tier_midpoint；查不到 = null。
# - odds_snapshot / odds_timeline_seg 的 euro_1x2：5DF 接口直接报价（行上 water_src 为空）→ actual；行上有 water_src 以行为准
# - 旧手工 odds_euro_home / odds_jc_home：核实为逐位录入的赔率（207/184 个不同值、两位小数；
#   v2d3 初盘与 5DF api opening 完全相等 澳门 154/175、威廉 126/175），不是档位码 → actual
X1X2_DIRECT_QUOTE_SOURCES = frozenset({"5df_odds_snap", "5dollar_history"})
X1X2_LEGACY_WATER_SOURCE = "actual"
X1X2_RETURN_HL_WATER = "real_only"  # 欧赔返还率基准 / 上色只认 water_source=actual（与亚盘一致）

# 0.3.22：澳门 5DF 补数并列列 ah.macau_5df（不盖 ah.macau / 不写 odds_asian）
# 识别：odds_snapshot.source 或 odds_asian.extras.source ∈ 下列；通常 water_src=actual
MACAU_5DF_SOURCES = frozenset({"5df_macauslot_history", "5dollar_history"})
MACAU_5DF_BOOK_KEY = "macau_5df"
MACAU_5DF_SOURCE_LABEL = "5df"


def is_5df_macau_row(r: Any) -> bool:
    """0.3.22：判定快照 / timeline / odds_asian 行是否为 5DF 澳门补数（非纯手工）。

    有 5df_macauslot_history / 5dollar_history / snapshot_source / from_channel 派生标记，
    或 fill 写入的 water_src=actual+from_* → 5DF。纯 legacy_import 且无上述标记 → 手工。
    存疑时偏 5DF（进 macau_5df，不进主列水位）。
    """
    if r is None:
        return False
    keys = r.keys() if hasattr(r, "keys") and not isinstance(r, dict) else None
    def _get(k, default=None):
        if isinstance(r, dict):
            return r.get(k, default)
        if keys is not None and k in keys:
            return r[k]
        return default
    src = _get("source")
    if src in MACAU_5DF_SOURCES:
        return True
    if isinstance(src, str) and (
        src.startswith("5df") or "5df_" in src or src.startswith("5dollar")
        or src.endswith("/5df_macauslot_history") or "/5df_" in src
    ):
        return True
    ex = _loads(_get("extras_json"))
    if ex.get("source") in MACAU_5DF_SOURCES or ex.get("snapshot_source") in MACAU_5DF_SOURCES:
        return True
    if isinstance(ex.get("source"), str) and (
        ex["source"].startswith("5df") or ex["source"].startswith("5dollar")
    ):
        return True
    # fill / resync 派生：from_channel + actual 水位（或 from_point）
    ws = _get("water_src")
    if ws == "actual" and (ex.get("from_channel") or ex.get("from_point") or ex.get("source")):
        if ex.get("from_channel") or ex.get("from_point") or ex.get("source") in MACAU_5DF_SOURCES:
            return True
    return False


def _manual_snap_points(phase: str) -> list[tuple[str, str]]:
    """主列 ah.macau：优先 rule_legacy（legacy_import），绝不用 rule/5DF。"""
    if phase == "open":
        return [("rule_legacy", "open"), ("actual", "open")]
    if phase == "mid":
        return [("rule_legacy", "mid")]
    if phase == "close":
        return [("rule_legacy", "close")]
    if phase == "mid_real":
        return [("actual", "t8")]
    if phase == "close_real":
        return [("actual", "t1")]
    return list(SNAP_POINTS.get(phase, []))


def _x1x2_water_source(r: sqlite3.Row) -> Optional[str]:
    ws = r["water_src"] if "water_src" in r.keys() else None
    if ws:
        return WATER_SRC_MAP.get(ws)
    src = r["source"] if "source" in r.keys() else None
    return "actual" if src in X1X2_DIRECT_QUOTE_SOURCES else None

# 快照点：phase → [(channel, point)] 按优先级
SNAP_POINTS = {
    "open": [("rule", "open"), ("actual", "open")],
    "mid": [("rule", "mid")],
    "close": [("rule", "close")],
    "mid_real": [("actual", "t8")],
    "close_real": [("actual", "t1")],
}


# ----------------------------------------------------------------------------- helpers

def connect_ro() -> sqlite3.Connection:
    """只读连接（mode=ro）；DB 路径取 app.db.DB_PATH（测试可 monkeypatch）。"""
    path = app_db.DB_PATH
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _M():  # lazy import，避免与 app.main 循环引用
    from app import main as M
    return M


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _to_cn(raw: Any) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    return _M().parse_iso_dt(raw)


def _r(v: Any, nd: int = 4) -> float | None:
    if v is None:
        return None
    try:
        return round(float(v), nd)
    except (TypeError, ValueError):
        return None


def _loads(raw: Any) -> dict:
    if not raw:
        return {}
    try:
        v = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


# 0.3.20：占位/阶段字段（分析师自采 ingest 写进副本；现网不强制）
# 来源：odds_snapshot 列或 extras_json → phase_pending / features_ok / phase_assign_late；
#       matches.kickoff_rev。列尚未建时按默认值返回，不改库、不算第二套。
PHASE_FEATURE_DEFAULTS = {"phase_pending": False, "features_ok": True, "phase_assign_late": False}


def _as_bool(v: Any, default: bool) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return default


def phase_feature_flags(src: Any = None) -> dict:
    """从 snapshot 行（列优先）或 extras dict 读三字段；缺省 = PHASE_FEATURE_DEFAULTS。"""
    out = dict(PHASE_FEATURE_DEFAULTS)
    if src is None:
        return out
    keys = src.keys() if hasattr(src, "keys") and not isinstance(src, dict) else None
    ex = {}
    if isinstance(src, dict):
        ex = src
    elif keys is not None:
        for k in ("phase_pending", "features_ok", "phase_assign_late"):
            if k in keys and src[k] is not None:
                out[k] = _as_bool(src[k], out[k])
        ex = _loads(src["extras_json"] if "extras_json" in keys else None)
    for k, default in PHASE_FEATURE_DEFAULTS.items():
        if k in ex and ex[k] is not None:
            # 列已有非空值时不让 extras 覆盖（上面已写）；这里只补列缺失的
            if keys is None or k not in keys or src[k] is None:
                out[k] = _as_bool(ex[k], default)
    return out


def kickoff_rev_of(row: Any) -> int:
    """matches.kickoff_rev；列缺失 / NULL → 0（前端 >0 才悬停）。不读 state 文件、不重算。"""
    if row is None:
        return 0
    keys = row.keys() if hasattr(row, "keys") else None
    if keys is not None and "kickoff_rev" in keys and row["kickoff_rev"] is not None:
        try:
            return max(0, int(row["kickoff_rev"]))
        except (TypeError, ValueError):
            return 0
    return 0


def _neg(v: Any) -> float | None:
    """odds_snapshot / timeline 的亚盘 line 为 API 记法（负=主让）；本接口统一为 odds_asian 记法（正=主让）。"""
    if v is None:
        return None
    f = -float(v)
    return 0.0 if f == 0 else f


def ah_return_rate(home_water: Any, away_water: Any) -> float | None:
    """hl_v0 §4：1 ÷ (1/(1+主水) + 1/(1+客水))（港盘水位）。"""
    try:
        h, a = float(home_water), float(away_water)
    except (TypeError, ValueError):
        return None
    if h <= 0 or a <= 0:
        return None
    return 1.0 / (1.0 / (1.0 + h) + 1.0 / (1.0 + a))


def x1x2_return_rate(home: Any, draw: Any, away: Any) -> float | None:
    """hl_v0 §5：1 ÷ (1/胜 + 1/平 + 1/负)。"""
    try:
        o = [float(home), float(draw), float(away)]
    except (TypeError, ValueError):
        return None
    if any(x <= 1.0 for x in o):
        return None
    return 1.0 / sum(1.0 / x for x in o)


def devig(home: Any, draw: Any, away: Any) -> tuple[float, float, float] | None:
    """hl_v0 §6：p_i = (1/odds_i) / Σ(1/odds)。"""
    try:
        o = [float(home), float(draw), float(away)]
    except (TypeError, ValueError):
        return None
    if any(x <= 1.0 for x in o):
        return None
    inv = [1.0 / x for x in o]
    s = sum(inv)
    return (inv[0] / s, inv[1] / s, inv[2] / s)


def _empty_ah(phase: str) -> dict:
    return {"line": None, "home_water": None, "away_water": None, "water_source": None,
            "water_src": None, "book_lane": None,  # 0.3.22
            "water_censored": None, "recorded_at": None, "target_at": None, "basis": None,
            "source": None, "available": False, "hidden_reason": None,
            "minutes_since_open": None, "return_rate": None, "return_rate_baseline": None,
            "return_rate_baseline_n": None, "return_rate_dev": None, "baseline_method": None,
            "water_move_eligible": None, "tier_cross": None, "tier_cross_mid": None,
            **PHASE_FEATURE_DEFAULTS}  # 0.3.20：无快照时默认；有快照行时由 phase_feature_flags 覆盖


def _empty_x(phase: str) -> dict:
    return {"home": None, "draw": None, "away": None, "complete": False,
            "recorded_at": None, "fetched_at": None, "target_at": None, "basis": None,
            "source": None, "available": False, "hidden_reason": None,
            "minutes_since_open": None, "return_rate": None, "return_rate_baseline": None,
            "return_rate_baseline_n": None, "return_rate_dev": None, "baseline_method": None,
            "kelly": None, "kelly_base": None, "kelly_base_n_books": None,
            "kelly_multi_avg": None, "multi_avg_n_books": None, "n_avg": None, "water_source": None,
            **PHASE_FEATURE_DEFAULTS}


def _empty_jc() -> dict:
    return {"home": None, "draw": None, "away": None, "complete": False,
            "jc_1x2_incomplete": None,  # 0.3.21：True=缺项；仅 legacy home_only / 半套 had
            "recorded_at": None, "fetched_at": None, "target_at": None, "basis": None,
            "source": None, "available": False, "hidden_reason": None, "water_source": None,
            "out_of_window": None,  # 0.3.21：竞彩 11:10 与亚盘同窗
            "fetch_lag_min": None,
            "missing_reason": None,  # collector_empty | legacy_home_only | …
            **PHASE_FEATURE_DEFAULTS}


# ----------------------------------------------------------------------------- data load

class _Data:
    """一次请求内的批量读取（全部只读 SELECT）。"""

    def __init__(self, conn: sqlite3.Connection, page_ids: list[int]):
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
        self.has_snapshot = "odds_snapshot" in names
        self.has_timeline = "odds_timeline_seg" in names

        self.legacy_asian: dict[tuple, sqlite3.Row] = {}
        for r in conn.execute(
            "SELECT match_id, book, phase, handicap, home_water, away_water, water_src,"
            " water_censored, extras_json FROM odds_asian"):
            self.legacy_asian[(r["match_id"], r["book"], r["phase"])] = r
        self.legacy_euro: dict[tuple, Any] = {}
        for r in conn.execute("SELECT match_id, book, phase, home_win FROM odds_euro_home"):
            self.legacy_euro[(r["match_id"], r["book"], r["phase"])] = r["home_win"]
        self.legacy_jc: dict[tuple, Any] = {}
        for r in conn.execute("SELECT match_id, phase, home_win FROM odds_jc_home"):
            self.legacy_jc[(r["match_id"], r["phase"])] = r["home_win"]

        # 0.3.21：完整竞彩 had / hhad（副本）；主表当前线 + hist 决策时刻选线
        self.has_jc_had = "odds_jc_had" in names
        self.has_jc_hhad = "odds_jc_hhad" in names
        self.has_jc_hhad_hist = "odds_jc_hhad_line_hist" in names
        self.jc_had: dict[tuple, sqlite3.Row] = {}
        if self.has_jc_had:
            for r in conn.execute("SELECT * FROM odds_jc_had"):
                # 同 (match, phase) 多 source 时优先 sporttery_local，其次任意非 legacy
                key = (r["match_id"], r["phase"])
                prev = self.jc_had.get(key)
                if prev is None or (r["source"] in _JC_LOCAL_SOURCES) or (
                    prev["source"] not in _JC_LOCAL_SOURCES and r["source"] != "legacy_home_only"
                ):
                    self.jc_had[key] = r
        self.jc_hhad: dict[tuple, sqlite3.Row] = {}
        if self.has_jc_hhad:
            for r in conn.execute("SELECT * FROM odds_jc_hhad"):
                self.jc_hhad[(r["match_id"], r["phase"])] = r
        self.jc_hhad_hist: dict[tuple, list[dict]] = {}
        if self.has_jc_hhad_hist:
            for r in conn.execute("SELECT * FROM odds_jc_hhad_line_hist"):
                self.jc_hhad_hist.setdefault((r["match_id"], r["phase"]), []).append(dict(r))

        self.snap: dict[tuple, sqlite3.Row] = {}
        self.snap_by_match: dict[int, list[sqlite3.Row]] = {}
        if self.has_snapshot:
            for r in conn.execute("SELECT * FROM odds_snapshot"):
                self.snap[(r["match_id"], r["book"], r["market"], r["channel"], r["point"])] = r
                self.snap_by_match.setdefault(r["match_id"], []).append(r)

        self.timeline: dict[int, list[sqlite3.Row]] = {}
        if self.has_timeline:  # 0.3.16：全库加载（open 的 first_tick / earliest_ts_quote_at 对基准样本也要一致）
            for r in conn.execute(
                "SELECT * FROM odds_timeline_seg ORDER BY match_id, book, market, seg_start_at"):
                self.timeline.setdefault(r["match_id"], []).append(r)


# ----------------------------------------------------------------------------- cells

def _snap_basis(r: sqlite3.Row, phase: str) -> tuple[str, bool]:
    """(basis, recorded_at_is_quote_time)。"""
    ex = _loads(r["extras_json"])
    api_phase = ex.get("api_phase")
    if api_phase in ("opening", "closing"):
        return f"api_{api_phase}", False
    if phase == "open":
        return "first_record", True
    if phase in ("mid_real", "close_real"):
        return "asof_real", True
    return "asof_rule", True


OPEN_UNUSABLE_REASON = "open_time_unknown_after_decision_possible"


RESCUE_SOURCES = frozenset({"5df_hist_asof"})
RESCUE_CAPTURES = frozenset({"asof_hist"})


def _is_rescue_row(r: sqlite3.Row) -> bool:
    """A row written by the rescue for a missed stage (one as-of history tick, for example point=mid):
    source=5df_hist_asof, capture=asof_hist or asof_backfill=true. It is never an opening quote candidate
    (analyst finding 2026-10-10), the same way as our own captures."""
    ex = _loads(r["extras_json"])
    src = r["source"] if "source" in r.keys() else None
    return (src in RESCUE_SOURCES or ex.get("source") in RESCUE_SOURCES
            or ex.get("capture") in RESCUE_CAPTURES or ex.get("asof_backfill") is True)


def _is_own_capture(r: sqlite3.Row) -> bool:
    """我们自己抓的快照（如竞彩日 11:10）：计入 earliest_ts_quote_at，但不当 first_tick 初盘（术语文档 api_opening 节）。"""
    ex = _loads(r["extras_json"])
    return (r["point"] == "rule_1110" or ex.get("label") == "rule_1110" or ex.get("capture") == "own")


def _ts_quotes(d: "_Data", mpk: int, book: str, market: str, as_of: datetime
               ) -> tuple[tuple[datetime, str, sqlite3.Row] | None, datetime | None]:
    """(first_tick 候选, earliest_ts_quote_at)。只看带真实报价时刻、≤ as_of、非赛中的报价。

    first_tick 候选 = API 历史 tick（odds_timeline_seg 变化点 ∪ 有真实报价时刻的 odds_snapshot，排除 api_* 与自抓）；
    earliest_ts_quote_at = 上述 ∪ 自抓快照（11:10 等）中最早的时刻。
    """
    first: tuple[datetime, str, sqlite3.Row] | None = None
    earliest: datetime | None = None
    for s in d.timeline.get(mpk, []):
        if s["book"] != book or s["market"] != market or s["is_inplay"]:
            continue
        ts = _to_cn(s["seg_start_at"])
        if ts is None or ts > as_of:
            continue
        if first is None or ts < first[0]:
            first = (ts, "seg", s)
    for r in d.snap_by_match.get(mpk, []):
        if r["book"] != book or r["market"] != market or r["channel"] == "rule_legacy":
            continue
        if _loads(r["extras_json"]).get("api_phase") in ("opening", "closing"):
            continue
        ts = _to_cn(r["recorded_at"])
        if ts is None or ts > as_of:
            continue
        if _is_own_capture(r) or _is_rescue_row(r):
            earliest = ts if earliest is None or ts < earliest else earliest
            continue
        if first is None or ts < first[0]:
            first = (ts, "snap", r)
    if first is not None and (earliest is None or first[0] < earliest):
        earliest = first[0]
    return first, earliest


# Bases that are only our own first capture, never a real opening quote (user rule 2026-10-10).
OWN_FIRST_SEEN_BASES = frozenset({"first_seen_capture"})


def _open_source_kind(basis: str | None) -> str | None:
    """manual for the user's hand-recorded opening quotes, official_open for interface or official data,
    None when the value is only our own first capture (that value must not replace the opening quote)."""
    if basis is None or basis in OWN_FIRST_SEEN_BASES:
        return None
    return osx.SOURCE_MANUAL if basis == "legacy_import" else osx.SOURCE_OFFICIAL_OPEN


def _open_flags(cell: dict, first_tick: bool, earliest: datetime | None, sched: dict) -> dict:
    """Opening cell fields: open_basis, earliest_ts_quote_at, usable_at_mid, usable_at_close, unusable_reason,
    plus the opening status fields from app.open_status (status, source_kind, first_captured_at, open_time,
    open_time_known, backtest_eligible).

    first_tick: both usable flags are always true and open_time is the first record time.
    legacy_import (the user's hand-recorded opening quote, source_kind=manual): both usable flags are always true,
    because by the user's rule it is the book's data at opening and opening is always earlier than the mid and
    close stages. The opening time is unknown, so open_time is null and open_time_known is false. No 11:10 time is
    assumed any more; earliest_ts_quote_at is a real timestamped quote of this book or null.
    api_opening: the opening time is unknown and the interface does not guarantee the value was available before
    the decision, so usable_at_<phase> is true only when earliest_ts_quote_at is not later than that phase's
    decision time.
    """
    basis = cell.get("basis")
    if not cell.get("available"):
        ob = None
    elif first_tick:
        ob = "first_tick"
    elif basis == "api_opening":
        ob = "api_opening"
    else:
        ob = basis  # legacy_import and other bases
    ts_inferred = None
    if ob is not None:
        ts_inferred = False  # nothing is inferred any more (user rule 2026-10-10)
    ob_reason = None if ob is not None else (
        "after_as_of" if cell.get("hidden_reason") == "after_as_of" else "no_open_data")
    out = {"open_basis": ob, "open_basis_reason": ob_reason,
           "earliest_ts_quote_at": _iso(earliest), "ts_inferred": ts_inferred,
           "usable_at_mid": None, "usable_at_close": None, "unusable_reason": None}
    if ob in ("first_tick", "legacy_import"):
        # legacy_import is the user's hand-recorded opening quote: by the user's rule it is the book's data at
        # opening, and opening is always earlier than the mid and close stages, so both flags are true even though
        # the opening time itself is unknown (analyst decision 2026-10-10). Possible recording errors are checked
        # by comparing sources (source_kind), not by marking the value unusable.
        out["usable_at_mid"] = out["usable_at_close"] = True
    elif ob == "api_opening":
        for ph in ("mid", "close"):
            tgt = _to_cn(sched.get(f"{ph}_target_time"))
            out[f"usable_at_{ph}"] = bool(earliest is not None and tgt is not None and earliest <= tgt)
        if not (out["usable_at_mid"] and out["usable_at_close"]):
            out["unusable_reason"] = OPEN_UNUSABLE_REASON
    cell.update(out)
    kind = _open_source_kind(basis) if cell.get("available") else None
    open_time = cell.get("recorded_at") if ob == "first_tick" else None
    cell.update(osx.open_status(available=bool(cell.get("available")) and kind is not None,
                                source_kind=kind, first_captured_at=cell.get("first_captured_at"),
                                open_time=open_time))
    return cell


def _first_own_capture_at(d: "_Data", mpk: int, book: str, market: str, as_of: datetime) -> datetime | None:
    """Earliest moment we captured this book and market ourselves (own capture rows), not later than as_of."""
    best = None
    for r in d.snap_by_match.get(mpk, []):
        if r["book"] != book or r["market"] != market or not _is_own_capture(r):
            continue
        ts = _to_cn(r["recorded_at"])
        if ts is not None and ts <= as_of and (best is None or ts < best):
            best = ts
    return best


def finalize_ah_open(d: "_Data", mpk: int, cell: dict | None, book: str, kickoff: datetime | None,
                     league: str | None, as_of: datetime, table: dict | None, market: str = "asian",
                     match_uid: str | None = None) -> None:
    """Add first_captured_at and the truncation check to an Asian handicap opening cell.

    A first record from the 5DollarFootballAPI history that is later than the 95th percentile of the external
    quantile table marks the cell suspect_truncated and backtest_eligible=false. No table, no check."""
    if not cell or cell.get("status") is None:
        return
    own = _first_own_capture_at(d, mpk, "macau" if book == MACAU_5DF_BOOK_KEY else book, market, as_of)
    cell["first_captured_at"] = _iso(own)
    trunc = None
    if cell.get("open_basis") == "first_tick" and str(match_uid or "").startswith("probe:"):
        # Probe matches are test imports, not regular collection; they are not part of the truncation check.
        trunc = {"checked": False, "reason": "probe_match", "suspect": False}
    elif cell.get("open_basis") == "first_tick":
        tb = "macau" if book in ("macau", MACAU_5DF_BOOK_KEY) else book
        trunc = osx.truncation_check(table, tb, league, _to_cn(cell.get("open_time")), kickoff, market)
    cell.update(osx.open_status(available=cell.get("status") != osx.STATUS_MISSING,
                                source_kind=cell.get("source_kind"), first_captured_at=cell["first_captured_at"],
                                open_time=cell.get("open_time"), truncation=trunc))


def _fill_from_seg(cell: dict, s: sqlite3.Row, market: str) -> None:
    asian = market == "asian"
    cell.update({
        "recorded_at": _iso(_to_cn(s["seg_start_at"])), "target_at": None, "basis": "first_record",
        "source": f"odds_timeline_seg/{s['source']}", "available": True, "hidden_reason": None,
    })
    if asian:
        cell.update({"line": _neg(s["line"]), "home_water": _r(s["water_home"]),
                     "away_water": _r(s["water_away"]),
                     "water_source": WATER_SRC_MAP.get(s["water_src"]) if s["water_src"] else None,
                     "water_censored": None})
    else:
        cell.update({"home": _r(s["price_home"], 4), "draw": _r(s["price_draw"], 4),
                     "away": _r(s["price_away"], 4), "water_source": _x1x2_water_source(s)})
        if "fetched_at" in cell:
            cell["fetched_at"] = None
        cell["complete"] = all(cell[k] is not None for k in ("home", "draw", "away"))


def _mark_no_data(cell: dict | None) -> dict | None:
    """缺数据：available=false 且未被 as_of 隐藏 → hidden_reason=no_data（平博两库都无数据即此标记）。"""
    if cell is not None and not cell.get("available") and not cell.get("hidden_reason"):
        cell["hidden_reason"] = "no_data"
    return cell


def _hide_by_as_of(cell: dict, ts: datetime | None, as_of: datetime) -> bool:
    if ts is not None and ts > as_of:
        cell.update({k: None for k in cell if k not in ("available", "hidden_reason")})
        cell["available"] = False
        if "complete" in cell:
            cell["complete"] = False
        cell["hidden_reason"] = "after_as_of"
        return True
    return False


WATER_MOVE_PREV = {"mid": "open", "close": "mid"}  # hl_v0.2（0.3.18）口径，hl_v0.3 起不用
WATER_MOVE_PAIR = ("open", "close")  # hl_v0.3：水位异动统一比初盘 → 临盘；中盘只用来判断有没有换过盘


def apply_water_move_flags(cells: dict, d: "_Data", mpk: int, book: str) -> None:
    """hl_v0.3（0.3.19）§2 水位异动：统一比初盘 → 临盘，结果只挂在 close 格；open / mid 格三字段恒 null。

    close.water_move_eligible：open、close 都可见且盘口相同 → 两格 water_source 都是 actual 时 true，否则 false；
      盘口不同 / 任一格不可见 → null（不比水位）。
    close.tier_cross：
      - 初盘与临盘盘口不同 → {kind: "line", from: {phase: open, line, home, away}, to: {phase: close, ...}, sides: ["line"]}
        （只放悬停，不上色）；
      - 盘口相同、两格都是档位换算（legacy_import tier_midpoint）且档位变了 → kind="water_tier"（悬停「跨档」，不上色）；
      - 其它 null。
    close.tier_cross_mid：初盘与临盘盘口相同、中盘可见且盘口不同 → true（水位照常比、照常上色，悬停「中盘曾换盘，
      已回到初盘盘口」）；同盘但中盘没换 / 中盘不可见 → false；初/临不同盘或不可见 → null。
    只管上色，不进特征和策略（生成器不读这些字段）。
    """
    def tier(ph: str) -> dict | None:
        r = d.legacy_asian.get((mpk, book, ph))
        raw = _loads(r["extras_json"]).get("water_tier_raw") if r is not None else None
        return raw if isinstance(raw, dict) else None

    for ph in PHASES:
        c = cells.get(ph)
        if c:
            c["water_move_eligible"] = None
            c["tier_cross"] = None
            c["tier_cross_mid"] = None
    o, c, m = cells.get("open"), cells.get("close"), cells.get("mid")
    if not o or not c or not (o.get("available") and c.get("available")):
        return
    if o.get("line") is None or c.get("line") is None:
        return
    if float(o["line"]) != float(c["line"]):
        c["tier_cross"] = {"kind": "line",
                           "from": {"phase": "open", "line": o["line"], "home": o.get("home_water"), "away": o.get("away_water")},
                           "to": {"phase": "close", "line": c["line"], "home": c.get("home_water"), "away": c.get("away_water")},
                           "sides": ["line"]}
        return
    ws = (o.get("water_source"), c.get("water_source"))
    c["water_move_eligible"] = ws == ("actual", "actual")
    c["tier_cross_mid"] = bool(m and m.get("available") and m.get("line") is not None
                               and float(m["line"]) != float(o["line"]))
    if ws == ("tier_midpoint", "tier_midpoint") and o.get("basis") == c.get("basis") == "legacy_import":
        t0, t1 = tier("open"), tier("close")
        if t0 and t1:
            sides = [k for k in ("home", "away") if t0.get(k) != t1.get(k)]
            if sides:
                c["tier_cross"] = {"kind": "water_tier",
                                   "from": {"phase": "open", "line": o["line"], "home": t0.get("home"), "away": t0.get("away")},
                                   "to": {"phase": "close", "line": c["line"], "home": t1.get("home"), "away": t1.get("away")},
                                   "sides": sides}


def _set_water_fields(cell: dict, water_src_raw: Any) -> None:
    """同步 water_source 与 water_src（0.3.22 前端二选一）。"""
    mapped = WATER_SRC_MAP.get(water_src_raw) if water_src_raw else None
    if cell.get("home_water") is None and cell.get("away_water") is None:
        mapped = None
    cell["water_source"] = mapped
    cell["water_src"] = mapped


def build_ah_cell(d: _Data, mpk: int, book: str, phase: str, sched: dict,
                  as_of: datetime, kickoff: datetime | None) -> dict:
    """亚盘格。book=macau 时走手工主列（book_lane=macau_manual），5DF 水位不进主列。"""
    if book == "macau":
        return build_ah_macau_manual_cell(d, mpk, phase, sched, as_of, kickoff)
    cell = _empty_ah(phase)
    snap = None
    first = earliest = None
    if phase == "open":
        first, earliest = _ts_quotes(d, mpk, book, "asian", as_of)
    if first is not None and first[1] == "seg":
        _fill_from_seg(cell, first[2], "asian")
        _set_water_fields(cell, first[2]["water_src"] if "water_src" in first[2].keys() else None)
        cell["return_rate"] = _r(ah_return_rate(cell["home_water"], cell["away_water"]), 6)
        return _open_flags(cell, True, earliest, sched)
    if first is not None:
        snap = first[2]
    else:
        for ch, pt in SNAP_POINTS[phase]:
            snap = d.snap.get((mpk, book, "asian", ch, pt))
            if snap is not None:
                break
    if snap is not None:
        basis, quote_time = _snap_basis(snap, phase)
        rec = _to_cn(snap["recorded_at"]) if quote_time else None
        cell.update({
            "line": _neg(snap["line"]),
            "home_water": _r(snap["water_home"]), "away_water": _r(snap["water_away"]),
            "water_censored": (bool(snap["water_censored"]) if snap["water_censored"] is not None else None),
            "recorded_at": _iso(rec),
            "target_at": _iso(_to_cn(snap["target_at"])) if phase != "open" else None,
            "basis": basis, "source": f"odds_snapshot/{snap['channel']}/{snap['point']}",
            "available": True,
            **phase_feature_flags(snap),  # 0.3.20：列或 extras；缺省 features_ok=true
        })
        _set_water_fields(cell, snap["water_src"])
        if _hide_by_as_of(cell, rec, as_of):
            return _mark_no_data(cell)
    elif phase in PHASES:
        r = d.legacy_asian.get((mpk, book, phase))
        ex = _loads(r["extras_json"]) if r is not None else {}
        if r is not None and "from_channel" not in ex:  # 快照派生行（副本 v2d3）不走旧表回退
            cell.update({
                "line": _r(r["handicap"], 4),
                "home_water": _r(r["home_water"]), "away_water": _r(r["away_water"]),
                "water_censored": (bool(r["water_censored"]) if r["water_censored"] is not None else None),
                "recorded_at": None,
                "target_at": None,  # 旧手工导入：时钟为旧采集口径（≈rule_legacy），不冒充规则时刻
                "basis": "legacy_import", "source": "odds_asian", "available": True,
            })
            _set_water_fields(cell, r["water_src"])
            tgt = _to_cn(sched.get(f"{phase}_target_time")) if phase != "open" else None
            if _hide_by_as_of(cell, tgt, as_of):
                return _mark_no_data(cell)
    if phase in CALC_PHASES and cell["available"] and not cell["water_censored"]:
        cell["return_rate"] = _r(ah_return_rate(cell["home_water"], cell["away_water"]), 6)
    if phase == "open":
        _open_flags(cell, first is not None, earliest, sched)
    return _mark_no_data(cell)


def build_ah_macau_manual_cell(d: _Data, mpk: int, phase: str, sched: dict,
                               as_of: datetime, kickoff: datetime | None) -> dict:
    """0.3.22 主列 ah.macau.* = 仅手工（book_lane=macau_manual）。

    - 优先 odds_snapshot channel=rule_legacy（legacy_import，水位通常空）
    - 其次 odds_asian 无 5DF 标记的纯手工行
    - 若只有 5DF 派生行（from_channel / 5df source）：open/close 可保留盘口、水位强制 null；
      mid 若手工从未有过（仅 5DF 补出）→ no_data
    - 禁止用 5DF timeline / rule 快照当主列水位
    """
    cell = _empty_ah(phase)
    cell["book_lane"] = "macau_manual"
    first = earliest = None
    snap = None

    # open：非 5DF 的 first_tick 才进主列（现网/副本时间线多为 5DF → 跳过）
    if phase == "open":
        first, earliest = _ts_quotes(d, mpk, "macau", "asian", as_of)
        if first is not None and is_5df_macau_row(first[2]):
            first = None  # 5DF tick 留给 macau_5df
        if first is not None and first[1] == "seg":
            _fill_from_seg(cell, first[2], "asian")
            # 主列：若误带 actual 水且行被判手工，保留；5DF 已在上面过滤
            _set_water_fields(cell, first[2]["water_src"] if "water_src" in first[2].keys() else None)
            cell["book_lane"] = "macau_manual"
            cell["return_rate"] = _r(ah_return_rate(cell["home_water"], cell["away_water"]), 6)
            return _open_flags(cell, True, earliest, sched)
        if first is not None:
            snap = first[2]

    if snap is None:
        for ch, pt in _manual_snap_points(phase):
            s = d.snap.get((mpk, "macau", "asian", ch, pt))
            if s is not None and not is_5df_macau_row(s):
                snap = s
                break

    if snap is not None:
        basis, quote_time = _snap_basis(snap, phase)
        # rule_legacy 无 api_phase → basis 常为 asof_rule；对手工列改标 legacy 更贴切
        if snap["channel"] == "rule_legacy" or (snap["source"] if "source" in snap.keys() else None) == "legacy_import":
            basis, quote_time = "legacy_import", False
        rec = _to_cn(snap["recorded_at"]) if quote_time else None
        cell.update({
            "line": _neg(snap["line"]),
            "home_water": _r(snap["water_home"]), "away_water": _r(snap["water_away"]),
            "water_censored": (bool(snap["water_censored"]) if snap["water_censored"] is not None else None),
            "recorded_at": _iso(rec),
            "target_at": _iso(_to_cn(snap["target_at"])) if phase != "open" and quote_time else None,
            "basis": basis, "source": f"odds_snapshot/{snap['channel']}/{snap['point']}",
            "available": True, "book_lane": "macau_manual",
            **phase_feature_flags(snap),
        })
        # 手工主列：若行上意外带着 5DF actual 水，剥掉（防御）
        if is_5df_macau_row(snap):
            cell["home_water"] = cell["away_water"] = None
        _set_water_fields(cell, None if cell["home_water"] is None else snap["water_src"])
        if _hide_by_as_of(cell, rec, as_of):
            cell["book_lane"] = "macau_manual"
            return _mark_no_data(cell)
    elif phase in PHASES:
        r = d.legacy_asian.get((mpk, "macau", phase))
        if r is not None and not is_5df_macau_row(r):
            cell.update({
                "line": _r(r["handicap"], 4),
                "home_water": _r(r["home_water"]), "away_water": _r(r["away_water"]),
                "water_censored": (bool(r["water_censored"]) if r["water_censored"] is not None else None),
                "recorded_at": None, "target_at": None,
                "basis": "legacy_import", "source": "odds_asian", "available": True,
                "book_lane": "macau_manual",
            })
            _set_water_fields(cell, r["water_src"])
            tgt = _to_cn(sched.get(f"{phase}_target_time")) if phase != "open" else None
            if _hide_by_as_of(cell, tgt, as_of):
                cell["book_lane"] = "macau_manual"
                return _mark_no_data(cell)
        elif r is not None and is_5df_macau_row(r):
            # 5DF 曾写入 odds_asian：主列只保留盘口观感（open/close）；mid 手工本无 → no_data
            if phase == "mid":
                pass  # leave unavailable
            else:
                cell.update({
                    "line": _r(r["handicap"], 4),
                    "home_water": None, "away_water": None,
                    "water_censored": None, "recorded_at": None, "target_at": None,
                    "basis": "legacy_import", "source": "odds_asian", "available": True,
                    "book_lane": "macau_manual",
                })
                _set_water_fields(cell, None)
                tgt = _to_cn(sched.get(f"{phase}_target_time")) if phase != "open" else None
                if _hide_by_as_of(cell, tgt, as_of):
                    cell["book_lane"] = "macau_manual"
                    return _mark_no_data(cell)

    if phase in CALC_PHASES and cell["available"] and not cell["water_censored"]:
        cell["return_rate"] = _r(ah_return_rate(cell["home_water"], cell["away_water"]), 6)
    if phase == "open":
        _open_flags(cell, first is not None, earliest, sched)
    cell["book_lane"] = "macau_manual"
    return _mark_no_data(cell)


def build_macau_5df_cell(d: _Data, mpk: int, phase: str, sched: dict,
                         as_of: datetime, kickoff: datetime | None) -> dict:
    """0.3.22 并列列 ah.macau_5df.*：只投影 5DF 补数；source 固定 "5df"；book_lane=macau_5df。

    不盖 ah.macau；不写库。同 lane 内才算返还率 / 水位异动（调用方单独 apply_water_move）。
    """
    cell = _empty_ah(phase)
    cell["book_lane"] = "macau_5df"
    snap = None
    first = earliest = None

    if phase == "open":
        first, earliest = _ts_quotes(d, mpk, "macau", "asian", as_of)
        if first is not None and not is_5df_macau_row(first[2]):
            first = None
        if first is not None and first[1] == "seg":
            _fill_from_seg(cell, first[2], "asian")
            cell["source"] = MACAU_5DF_SOURCE_LABEL
            cell["book_lane"] = "macau_5df"
            _set_water_fields(cell, first[2]["water_src"] if "water_src" in first[2].keys() else "actual")
            cell["return_rate"] = _r(ah_return_rate(cell["home_water"], cell["away_water"]), 6)
            out = _open_flags(cell, True, earliest, sched)
            out["source"] = MACAU_5DF_SOURCE_LABEL
            out["book_lane"] = "macau_5df"
            return out
        if first is not None:
            snap = first[2]

    if snap is None:
        for ch, pt in SNAP_POINTS[phase]:
            s = d.snap.get((mpk, "macau", "asian", ch, pt))
            if s is not None and is_5df_macau_row(s):
                snap = s
                break

    if snap is not None:
        basis, quote_time = _snap_basis(snap, phase)
        rec = _to_cn(snap["recorded_at"]) if quote_time else None
        cell.update({
            "line": _neg(snap["line"]),
            "home_water": _r(snap["water_home"]), "away_water": _r(snap["water_away"]),
            "water_censored": (bool(snap["water_censored"]) if snap["water_censored"] is not None else None),
            "recorded_at": _iso(rec),
            "target_at": _iso(_to_cn(snap["target_at"])) if phase != "open" else None,
            "basis": basis,
            "source": MACAU_5DF_SOURCE_LABEL,
            "available": True,
            "book_lane": "macau_5df",
            **phase_feature_flags(snap),
        })
        _set_water_fields(cell, snap["water_src"] or "actual")
        if _hide_by_as_of(cell, rec, as_of):
            cell["book_lane"] = "macau_5df"
            cell["source"] = MACAU_5DF_SOURCE_LABEL
            return _mark_no_data(cell)
    elif phase in PHASES:
        r = d.legacy_asian.get((mpk, "macau", phase))
        if r is not None and is_5df_macau_row(r):
            cell.update({
                "line": _r(r["handicap"], 4),
                "home_water": _r(r["home_water"]), "away_water": _r(r["away_water"]),
                "water_censored": (bool(r["water_censored"]) if r["water_censored"] is not None else None),
                "recorded_at": None, "target_at": None,
                "basis": "asof_rule", "source": MACAU_5DF_SOURCE_LABEL, "available": True,
                "book_lane": "macau_5df",
            })
            _set_water_fields(cell, r["water_src"] or "actual")
            tgt = _to_cn(sched.get(f"{phase}_target_time")) if phase != "open" else None
            if _hide_by_as_of(cell, tgt, as_of):
                cell["book_lane"] = "macau_5df"
                cell["source"] = MACAU_5DF_SOURCE_LABEL
                return _mark_no_data(cell)

    if phase in CALC_PHASES and cell["available"] and not cell["water_censored"]:
        cell["return_rate"] = _r(ah_return_rate(cell["home_water"], cell["away_water"]), 6)
    if phase == "open":
        _open_flags(cell, first is not None, earliest, sched)
    cell["book_lane"] = "macau_5df"
    if cell["available"]:
        cell["source"] = MACAU_5DF_SOURCE_LABEL
    return _mark_no_data(cell)


def _is_api_closing(r: sqlite3.Row) -> bool:
    return _loads(r["extras_json"]).get("api_phase") == "closing"


def _x_from_snap(d: _Data, mpk: int, book: str, phase: str) -> sqlite3.Row | None:
    """欧赔快照；api_closing（报价时间未知）一律不进 open/mid/close/*_real（0.3.16）。"""
    for ch, pt in SNAP_POINTS[phase]:
        r = d.snap.get((mpk, book, "euro_1x2", ch, pt))
        if r is not None and not _is_api_closing(r):
            return r
    return None


def build_api_closing(d: _Data, mpk: int, book: str) -> dict | None:
    """欧赔 api_closing 单独对象（label=api_closing，「收盘（时间未知）」）；只读参考，不算返还率/凯利/高亮。

    调用方只在赛果可见（完赛）后调用；赛前不返回。
    """
    for r in d.snap_by_match.get(mpk, []):
        if r["book"] != book or r["market"] != "euro_1x2" or not _is_api_closing(r):
            continue
        out = {"label": API_CLOSING_LABEL, "display_name": API_CLOSING_DISPLAY,
               "home": _r(r["price_home"], 4), "draw": _r(r["price_draw"], 4),
               "away": _r(r["price_away"], 4), "complete": False,
               "quote_time_known": False, "fetched_at": _iso(_to_cn(r["recorded_at"])),
               "basis": "api_closing", "source": f"odds_snapshot/{r['channel']}/{r['point']}",
               "reference_only": True}
        out["complete"] = all(out[k] is not None for k in ("home", "draw", "away"))
        return out
    return None


def _fill_x_from_snap(cell: dict, r: sqlite3.Row, phase: str, as_of: datetime,
                      kickoff: datetime | None) -> None:
    basis, quote_time = _snap_basis(r, phase)
    rec_raw = _to_cn(r["recorded_at"])
    rec = rec_raw if quote_time else None
    cell.update({
        "home": _r(r["price_home"], 4), "draw": _r(r["price_draw"], 4), "away": _r(r["price_away"], 4),
        "recorded_at": _iso(rec), "fetched_at": None if quote_time else _iso(rec_raw),
        "target_at": _iso(_to_cn(r["target_at"])) if (quote_time and phase != "open") else None,
        "basis": basis, "source": f"odds_snapshot/{r['channel']}/{r['point']}", "available": True,
        "water_source": _x1x2_water_source(r),
        **phase_feature_flags(r),  # 0.3.20
    })
    cell["complete"] = all(cell[k] is not None for k in ("home", "draw", "away"))
    # api_closing 的报价时刻未知：开赛前一律视为未知 → as_of < 开赛则隐藏
    ts = rec if quote_time else (kickoff if basis == "api_closing" else None)
    _hide_by_as_of(cell, ts, as_of)


def build_x_cell(d: _Data, mpk: int, book: str, phase: str, sched: dict,
                 as_of: datetime, kickoff: datetime | None) -> dict:
    cell = _empty_x(phase)
    first = earliest = None
    if phase == "open":
        first, earliest = _ts_quotes(d, mpk, book, "euro_1x2", as_of)
    r = _x_from_snap(d, mpk, book, phase) if first is None else None
    if first is not None and first[1] == "seg":
        _fill_from_seg(cell, first[2], "euro_1x2")
    elif first is not None:
        _fill_x_from_snap(cell, first[2], phase, as_of, kickoff)
    elif r is not None:
        _fill_x_from_snap(cell, r, phase, as_of, kickoff)
    elif phase in PHASES:
        hw = d.legacy_euro.get((mpk, book, phase))
        if hw is not None:
            cell.update({"home": _r(hw, 4), "basis": "legacy_import",
                         "source": "odds_euro_home(home_only)", "available": True,
                         "target_at": None, "complete": False,
                         "water_source": X1X2_LEGACY_WATER_SOURCE})
            tgt = _to_cn(sched.get(f"{phase}_target_time")) if phase != "open" else None
            _hide_by_as_of(cell, tgt, as_of)
    if not cell["available"]:
        cell["water_source"] = None
    if phase in CALC_PHASES and cell["available"] and cell["complete"]:
        cell["return_rate"] = _r(x1x2_return_rate(cell["home"], cell["draw"], cell["away"]), 6)
    if phase == "open":
        _open_flags(cell, first is not None, earliest, sched)
    if phase in REAL_PHASES:
        for k in ("kelly", "kelly_base", "kelly_base_n_books", "kelly_multi_avg", "multi_avg_n_books",
                  "n_avg", "return_rate", "return_rate_baseline", "return_rate_baseline_n",
                  "return_rate_dev", "baseline_method"):
            cell.pop(k, None)
    return _mark_no_data(cell)


# Markers that say a Jingcai opening row is the official first published odds (for example read from the
# China Sports Lottery odds history, the earliest entry). Rows without such a marker are only our first capture.
JC_OFFICIAL_FIRST_KINDS = frozenset({"official_first"})


def is_jc_official_first(row: dict) -> bool:
    ex = _loads(row.get("extras_json")) if isinstance(row.get("extras_json"), str) else (row.get("extras_json") or {})
    return (row.get("quote_kind") in JC_OFFICIAL_FIRST_KINDS
            or (isinstance(ex, dict) and ex.get("quote_kind") in JC_OFFICIAL_FIRST_KINDS))


def _row_to_jc_dict(r: sqlite3.Row | dict) -> dict:
    if isinstance(r, sqlite3.Row):
        r = dict(r)
    return r


def build_jc_cell(d: _Data, mpk: int, phase: str, sched: dict, as_of: datetime,
                  kickoff: datetime | None, *, jingcai_date: str | None = None) -> dict:
    """0.3.21：优先 odds_jc_had；仅 odds_jc_home → jc_1x2_incomplete；禁止单边当完整盘。

    Since 2026-10-10 (user rule) the Jingcai opening quote is the official first published odds only.
    A row captured by us at 11:10 (or at any other moment) is not an opening quote: when the opening row is
    not marked as the official first quote, the cell is status=missing, the captured value is kept only in
    alt, and its capture time goes to first_captured_at. The old 11:00 to 11:20 window check is removed.
    """
    cell = _empty_jc()
    # 1) 完整 had 表
    had = d.jc_had.get((mpk, phase)) if getattr(d, "jc_had", None) is not None else None
    if had is not None:
        had = _row_to_jc_dict(had)
        home, draw, away = had.get("home_odds"), had.get("draw_odds"), had.get("away_odds")
        incomplete = bool(had.get("jc_1x2_incomplete") or 0) or fund.jc_incomplete(home, draw, away)
        cap = had.get("captured_at")
        oow = None
        lag = None
        if phase == "open" and not is_jc_official_first(had):
            cell.update({
                "available": False, "complete": False, "jc_1x2_incomplete": incomplete,
                "captured_at": cap, "recorded_at": None, "target_at": had.get("target_at"),
                "source": had.get("source"), "basis": "first_seen_capture",
                "missing_reason": "jc_official_first_quote_missing", "first_captured_at": cap,
                "home": None, "draw": None, "away": None,
                "alt": {"home": _r(home, 4), "draw": _r(draw, 4), "away": _r(away, 4),
                        "captured_at": cap, "kind": "first_seen_capture"},
            })
            _open_flags(cell, False, None, sched)
            return _mark_no_data(cell)
        cell.update({
            "home": _r(home, 4), "draw": _r(draw, 4), "away": _r(away, 4),
            "complete": not incomplete, "jc_1x2_incomplete": incomplete,
            "available": True, "basis": "odds_jc_had",
            "source": had.get("source") or "odds_jc_had",
            "recorded_at": cap, "fetched_at": cap, "target_at": had.get("target_at"),
            "water_source": X1X2_LEGACY_WATER_SOURCE,
            "out_of_window": False if oow is False else oow,
            "fetch_lag_min": lag,
            "usable_at_mid": None if had.get("usable_at_mid") is None else bool(had.get("usable_at_mid")),
            "usable_at_close": None if had.get("usable_at_close") is None else bool(had.get("usable_at_close")),
        })
        if incomplete:
            cell["missing_reason"] = "jc_1x2_incomplete"
        tgt = _to_cn(had.get("target_at") or sched.get(f"{phase}_target_time")) if phase != "open" else None
        if tgt is not None:
            _hide_by_as_of(cell, tgt, as_of)
        if phase == "open":
            cell["first_captured_at"] = cap
            cell["open_time"] = had.get("open_time")
            _open_flags(cell, False, _to_cn(had.get("open_time")), sched)
            if had.get("open_time"):
                cell.update({"open_time": had.get("open_time"), "open_time_known": True})
        return _mark_no_data(cell)

    # 2) 旧探针 snapshot jc/euro_1x2：不当正式竞彩官方（仍可读，但不标 complete 官方）
    first = earliest = None
    if phase == "open":
        first, earliest = _ts_quotes(d, mpk, "jc", "euro_1x2", as_of)
    r = _x_from_snap(d, mpk, "jc", phase) if first is None else None
    if first is not None and first[1] == "seg":
        _fill_from_seg(cell, first[2], "euro_1x2")
        cell["jc_1x2_incomplete"] = not cell.get("complete")
        cell["missing_reason"] = "probe_not_sporttery_official"
    elif first is not None:
        _fill_x_from_snap(cell, first[2], phase, as_of, kickoff)
        cell["jc_1x2_incomplete"] = not cell.get("complete")
        cell["missing_reason"] = "probe_not_sporttery_official"
    elif r is not None:
        _fill_x_from_snap(cell, r, phase, as_of, kickoff)
        cell["jc_1x2_incomplete"] = not cell.get("complete")
        cell["missing_reason"] = "probe_not_sporttery_official"
    elif phase in ("open", "close", "mid"):
        # 3) legacy home_only → incomplete，禁止当完整盘
        hw = d.legacy_jc.get((mpk, phase))
        if hw is not None:
            cell.update({"home": _r(hw, 4), "draw": None, "away": None,
                         "basis": "legacy_import",
                         "source": "odds_jc_home(home_only)", "available": True,
                         "target_at": None, "complete": False, "jc_1x2_incomplete": True,
                         "missing_reason": "legacy_home_only",
                         "water_source": X1X2_LEGACY_WATER_SOURCE})
            tgt = _to_cn(sched.get(f"{phase}_target_time")) if phase != "open" else None
            _hide_by_as_of(cell, tgt, as_of)
        else:
            cell["missing_reason"] = "collector_empty"
            cell["jc_1x2_incomplete"] = None
    else:
        cell["missing_reason"] = "collector_empty"
    if not cell["available"]:
        cell["water_source"] = None
        if cell.get("missing_reason") is None:
            cell["missing_reason"] = "collector_empty"
    if phase == "open":
        _open_flags(cell, first is not None, earliest, sched)
    return _mark_no_data(cell)


def _empty_jc_hhad() -> dict:
    return {
        "goal_line": None, "home": None, "draw": None, "away": None,
        "complete": False, "jc_1x2_incomplete": None, "line_rev": 0,
        "goal_line_raw": None, "available": False, "missing_reason": None,
        "source": None, "captured_at": None, "target_at": None,
        "recorded_at": None, "basis": None, "water_source": None,
        "out_of_window": None, "fetch_lag_min": None,
        "post_decision_line_change": False, "current_line": None,
        "from_hist": False, "decision_line": None,
        "usable_at_mid": None, "usable_at_close": None,
        "hidden_reason": None, **PHASE_FEATURE_DEFAULTS,
    }


def build_jc_hhad_cell(d: _Data, mpk: int, phase: str, sched: dict, as_of: datetime,
                       *, jingcai_date: str | None = None) -> dict:
    """0.3.21：决策时刻选线（主表当前行 + hist）；禁止默认读当前主行当特征。"""
    cell = _empty_jc_hhad()
    main = d.jc_hhad.get((mpk, phase)) if getattr(d, "jc_hhad", None) else None
    hist = (d.jc_hhad_hist.get((mpk, phase)) if getattr(d, "jc_hhad_hist", None) else None) or []
    if main is None and not hist:
        cell["missing_reason"] = "collector_empty"
        return cell
    main_d = dict(main) if main is not None else None
    # 决策时刻 = as_of（表格页）；阶段目标可作对照
    t_dec = as_of
    sel = fund.select_hhad_at_decision(main_d, hist, t_dec)
    cell["current_line"] = sel.get("current_line")
    cell["post_decision_line_change"] = bool(sel.get("post_decision_line_change"))
    cell["from_hist"] = bool(sel.get("from_hist"))
    if sel.get("missing"):
        cell["missing_reason"] = sel.get("reason") or "no_line_visible_at_decision"
        cell["available"] = False
        # 悬停仍可带当前线（决策后换盘）
        if main_d and sel.get("post_decision_line_change"):
            cell["goal_line"] = None
            cell["decision_line"] = None
        return cell
    home, draw, away = sel.get("home"), sel.get("draw"), sel.get("away")
    incomplete = bool(sel.get("jc_1x2_incomplete")) or fund.jc_incomplete(home, draw, away)
    cap = sel.get("captured_at")
    oow = None
    lag = None
    tgt = (main_d or {}).get("target_at") if main_d else None
    # Since 2026-10-10 the opening quote of the handicap market is the official first published odds only.
    if phase == "open" and not is_jc_official_first(main_d or {}):
        cell.update({
            "available": False, "complete": False, "jc_1x2_incomplete": incomplete,
            "captured_at": cap, "first_captured_at": cap, "basis": "first_seen_capture",
            "missing_reason": "jc_official_first_quote_missing",
            "alt": {"goal_line": sel.get("decision_line"), "home": _r(home, 4), "draw": _r(draw, 4),
                    "away": _r(away, 4), "captured_at": cap, "kind": "first_seen_capture"},
        })
        cell.update(osx.open_status(available=False, source_kind=None, first_captured_at=cap, open_time=None))
        return cell
    cell.update({
        "goal_line": sel.get("decision_line"), "decision_line": sel.get("decision_line"),
        "home": _r(home, 4), "draw": _r(draw, 4), "away": _r(away, 4),
        "complete": not incomplete, "jc_1x2_incomplete": incomplete,
        "line_rev": int(sel.get("line_rev") or 0),
        "goal_line_raw": (main_d or {}).get("goal_line_raw") if main_d else None,
        "available": True, "basis": "hhad_at_decision",
        "source": sel.get("source") or ((main_d or {}).get("source") if main_d else None),
        "captured_at": cap, "recorded_at": cap,
        "target_at": tgt or sched.get(f"{phase}_target_time"),
        "water_source": X1X2_LEGACY_WATER_SOURCE,
        "out_of_window": False if oow is False else oow,
        "fetch_lag_min": lag,
        "missing_reason": ("jc_1x2_incomplete" if incomplete else None),
    })
    if main_d:
        if main_d.get("usable_at_mid") is not None:
            cell["usable_at_mid"] = bool(main_d["usable_at_mid"])
        if main_d.get("usable_at_close") is not None:
            cell["usable_at_close"] = bool(main_d["usable_at_close"])
    return cell


def _mean_probs(probs: dict[str, tuple[float, float, float]], exclude: str | None = None
                ) -> tuple[tuple[float, float, float] | None, int]:
    sel = [p for b, p in probs.items() if b != exclude]
    if not sel:
        return None, 0
    n = len(sel)
    return tuple(sum(p[i] for p in sel) / n for i in range(3)), n  # type: ignore[return-value]


def _apply_kelly(x1x2: dict) -> dict:
    """hl_v0.1 §6：凯利 = 公司赔率 × 基准概率。主基准平博去水；平博自身 / 缺平博 → 多家平均。

    0.3.16：凡「某家 vs 多家平均」一律 leave-one-out（不含本家），格子 n_avg = 参与平均的家数；
    x1x2_base.*.multi_avg（「多家平均（参考）」列）仍含全部机构。
    """
    base_out: dict[str, Any] = {}
    for phase in CALC_PHASES:
        probs: dict[str, tuple[float, float, float]] = {}
        for book in BOOKS:
            c = x1x2[book][phase]
            if c["available"] and c["complete"]:
                p = devig(c["home"], c["draw"], c["away"])
                if p:
                    probs[book] = p
        pin = probs.get("pinnacle")
        cons, n_all = _mean_probs(probs)
        base_out[phase] = {
            "pinnacle": ({"home": _r(pin[0], 6), "draw": _r(pin[1], 6), "away": _r(pin[2], 6)}
                         if pin else None),
            "multi_avg": ({"home": _r(cons[0], 6), "draw": _r(cons[1], 6), "away": _r(cons[2], 6),
                           "n_books": n_all, "n_avg": n_all, "books": sorted(probs),
                           "includes_self": True} if cons else None),
        }
        for book in BOOKS:
            c = x1x2[book][phase]
            if not (c["available"] and c["complete"]):
                continue
            odds = (float(c["home"]), float(c["draw"]), float(c["away"]))
            c["multi_avg_n_books"] = n_all if cons else None  # 参考列家数（含本家）
            loo, n_loo = _mean_probs(probs, exclude=book)  # 不含本家
            if loo:
                c["kelly_multi_avg"] = {k: _r(o * p, 4) for k, o, p in zip(("home", "draw", "away"), odds, loo)}
                c["n_avg"] = n_loo
            if book != "pinnacle" and pin:
                c["kelly"] = {k: _r(o * p, 4) for k, o, p in zip(("home", "draw", "away"), odds, pin)}
                c["kelly_base"], c["kelly_base_n_books"] = "pinnacle", 1
            elif loo:
                c["kelly"] = dict(c["kelly_multi_avg"])
                c["kelly_base"], c["kelly_base_n_books"] = "multi_avg", n_loo
    return base_out


_LIVE_MERGE_BLANK = {"origin": None, "odds_source": None, "capture": None, "captured_at": None,
                     "fetch_lag_min": None, "merge_rule": None, "alt": None, "instant_src_diff": None,
                     "own_capture_out_of_window": None, "main_source_reason": None}


def _timeline_entry(r: sqlite3.Row, label: str | None, target: datetime | None) -> dict:
    asian = r["market"] == "asian"
    return {
        "book": r["book"], "market": r["market"], "label": label,
        "target_at": _iso(target),
        "recorded_at": _iso(_to_cn(r["seg_start_at"])),
        "valid_until": _iso(_to_cn(r["seg_end_at"])),
        "line": _neg(r["line"]) if asian else _r(r["line"], 4),
        "home_water": _r(r["water_home"]), "away_water": _r(r["water_away"]),
        "home": _r(r["price_home"]) if not asian else None,
        "draw": _r(r["price_draw"]), "away": _r(r["price_away"]) if not asian else None,
        "over_water": _r(r["water_over"]), "under_water": _r(r["water_under"]),
        "water_source": WATER_SRC_MAP.get(r["water_src"]) if r["water_src"] else None,
        "is_inplay": bool(r["is_inplay"]),
        "source": f"odds_timeline_seg/{r['source']}",
        **_LIVE_MERGE_BLANK,
    }


LIVE_1110_MERGE_RULE = "own_capture_first;alt=timeline"  # 0.3.19：即时（11:10）两路来源合并口径（分析师 + 前端定）
# 0.3.20（高亮文档「0.3.19 后续四件」第 1 条）：自采 captured_at 落在 [11:00, 11:20]（|fetch_lag_min| ≤ 10，含端）才算主值；
# 超出 → 照存（进 alt，标实际抓取时间 + out_of_window=true），这一格退回时间线表的值
LIVE_1110_OWN_WINDOW_MIN = 10
LIVE_1110_MERGE_RULE_OOW = "own_capture_out_of_window;main=timeline;alt=own_capture"
INSTANT_SRC_WATER_TOL = 0.03  # 自采 vs 时间线：盘口不同或任一边水位差 > 0.03 → instant_src_diff


def _is_own_live_1110(r: sqlite3.Row) -> bool:
    """前向实时采集写入的 11:10 行：odds_snapshot extras capture=own 且 odds_source=live，point/label=rule_1110。"""
    ex = _loads(r["extras_json"])
    return (ex.get("capture") == "own" and ex.get("odds_source") == "live"
            and (r["point"] == "rule_1110" or ex.get("label") == "rule_1110"))


def _water_of(market: str, src: dict) -> dict:
    """alt.water / 比较用：亚盘 {home, away}；大小球 {over, under}；1X2 {home, draw, away}（赔率）。"""
    if market == "asian":
        return {"home": src.get("home_water"), "away": src.get("away_water")}
    if market in ("ou", "over_under", "totals"):
        return {"over": src.get("over_water"), "under": src.get("under_water")}
    return {"home": src.get("home"), "draw": src.get("draw"), "away": src.get("away")}


def _instant_diff(market: str, a: dict, b: dict) -> bool:
    if a.get("line") != b.get("line"):
        return True
    wa, wb = _water_of(market, a), _water_of(market, b)
    for k in wa:
        x, y = wa.get(k), wb.get(k)
        if x is None or y is None:
            if (x is None) != (y is None):
                return True
            continue
        if abs(float(x) - float(y)) > INSTANT_SRC_WATER_TOL + 1e-9:
            return True
    return False


def _own_1110_entry(r: sqlite3.Row, target: datetime) -> dict:
    asian = r["market"] == "asian"
    ts = _to_cn(r["recorded_at"])
    return {
        "book": r["book"], "market": r["market"], "label": "rule_1110",
        "target_at": _iso(target), "recorded_at": _iso(ts), "valid_until": None,
        "line": _neg(r["line"]) if asian else _r(r["line"], 4),
        "home_water": _r(r["water_home"]), "away_water": _r(r["water_away"]),
        "home": None if asian else _r(r["price_home"]),
        "draw": _r(r["price_draw"]), "away": None if asian else _r(r["price_away"]),
        "over_water": _r(r["water_over"]), "under_water": _r(r["water_under"]),
        "water_source": WATER_SRC_MAP.get(r["water_src"]) if r["water_src"] else "actual",
        "is_inplay": False, "source": f"odds_snapshot/{r['channel']}/{r['point']}/{r['source']}",
        **_LIVE_MERGE_BLANK,
        "origin": "own_capture", "odds_source": "live", "capture": "own", "captured_at": _iso(ts),
        "fetch_lag_min": round((ts - target).total_seconds() / 60.0, 2) if ts else None,
    }


def rule_1110_entries(d: _Data, mpk: int, jingcai_date: str, as_of: datetime) -> list[dict]:
    """即时（11:10）格子（0.3.19，分析师 + 前端定的口径）：

    - 同一场、同一 (公司, 市场)：自采行（odds_snapshot extras odds_source=live、capture=own、rule_1110）和时间线表
      （开始时刻 ≤ 竞彩日 11:10 的最后一段）都有 → 以自采为准；时间线表的值放进 alt={line, water, tick_at}。
    - 只有时间线表 → 照旧用它（odds_source=hist、capture=null），alt=null；只有自采 → 用自采，alt=null。
    - 每格带 odds_source / capture / captured_at（实际抓取或报价时刻）/ origin / fetch_lag_min（自采：抓取 − 11:10）。
    - 盘口不同，或任一边水位差 > 0.03 → instant_src_diff=true（进日核对），两边都保留、谁也不覆盖。
    - alt 只是对照：不参与升降盘判断、不进策略计算（本接口和生成器都不读 alt）。
    - as-of：11:10 ≤ as_of 才给；自采行 recorded_at > as_of 不用，时间线段开始 > as_of 不用。
    """
    try:
        base = datetime.strptime(jingcai_date, "%Y-%m-%d").replace(tzinfo=cs.TZ_CN)
        target = base.replace(hour=LIVE_RULE_1110[0], minute=LIVE_RULE_1110[1])
    except (TypeError, ValueError):
        return []
    if target > as_of:
        return []
    last: dict[tuple, sqlite3.Row] = {}
    for sg in d.timeline.get(mpk, []):  # 已按 seg_start_at 升序
        ts = _to_cn(sg["seg_start_at"])
        if ts is not None and ts <= target:
            last[(sg["book"], sg["market"])] = sg
    own: dict[tuple, tuple[datetime, sqlite3.Row]] = {}
    for r in d.snap_by_match.get(mpk, []):
        if not _is_own_live_1110(r):
            continue
        ts = _to_cn(r["recorded_at"])
        if ts is None or ts > as_of:
            continue
        k = (r["book"], r["market"])
        if k not in own or abs((ts - target).total_seconds()) < abs((own[k][0] - target).total_seconds()):
            own[k] = (ts, r)
    out: list[dict] = []
    for key in sorted(set(last) | set(own)):
        tl = last.get(key)
        tle = None
        if tl is not None:
            tle = {**_timeline_entry(tl, "rule_1110", target), "origin": "timeline", "odds_source": "hist",
                   "capture": None, "captured_at": _iso(_to_cn(tl["seg_start_at"]))}
        if key in own:
            oe = _own_1110_entry(own[key][1], target)
            if own_in_window(own[key][0], target):
                e = oe
                e["own_capture_out_of_window"] = False
                if tle is not None:
                    e["alt"] = _alt_of(key[1], tle, out_of_window=False)
                    e["instant_src_diff"] = _instant_diff(key[1], e, tle)
                e["merge_rule"] = LIVE_1110_MERGE_RULE
            else:
                # 0.3.20：自采超出 [11:00, 11:20] → 不进 11:10 主值；照存进 alt（实际抓取时间 + out_of_window=true）
                if tle is not None:
                    e = dict(tle)
                    e["main_source_reason"] = "own_capture_out_of_window"
                else:
                    e = {**_own_1110_entry(own[key][1], target), "recorded_at": None,
                         "line": None, "home_water": None, "away_water": None, "home": None, "draw": None,
                         "away": None, "over_water": None, "under_water": None, "water_source": None,
                         "source": None, "origin": None, "odds_source": None, "capture": None,
                         "captured_at": None, "fetch_lag_min": None,
                         "main_source_reason": "own_capture_out_of_window;no_timeline"}
                e["alt"] = _alt_of(key[1], oe, out_of_window=True)
                e["own_capture_out_of_window"] = True
                e["instant_src_diff"] = None  # 不同时刻的两路值不比（另进日核对 own_1110_out_of_window）
                e["merge_rule"] = LIVE_1110_MERGE_RULE_OOW
        else:
            e = tle
            e["merge_rule"] = LIVE_1110_MERGE_RULE
        out.append(e)
    return out


def own_in_window(captured_at: datetime | None, target: datetime) -> bool:
    """0.3.20：自采 captured_at ∈ [11:00, 11:20]（= |captured_at − 11:10| ≤ 10 分钟，两端都含）。"""
    if captured_at is None:
        return False
    return abs((captured_at - target).total_seconds()) <= LIVE_1110_OWN_WINDOW_MIN * 60 + 1e-6


def _alt_of(market: str, src: dict, *, out_of_window: bool) -> dict:
    """alt 统一结构：{line, water, tick_at, origin, captured_at, fetch_lag_min, out_of_window}。
    water 按盘种：亚盘 {home, away}；大小球 {over, under}；1X2 {home, draw, away}（0.3.20 前端确认）。"""
    return {"line": src.get("line"), "water": _water_of(market, src),
            "tick_at": src.get("captured_at") if src.get("origin") == "own_capture" else src.get("recorded_at"),
            "origin": src.get("origin"), "captured_at": src.get("captured_at"),
            "fetch_lag_min": src.get("fetch_lag_min"), "out_of_window": bool(out_of_window)}


def daily_check_summary(items: list[dict]) -> dict:
    """日核对清单计数（本页）：每个原因数场次；instant_src_diff / 自采超窗 另给 (公司, 市场) 格子数。
    返还率「偏高」(fallback_hl_level=high) 不是告警，不进日核对（0.3.20）。"""
    by: dict[str, int] = {}
    cells = oow = 0
    for it in items:
        for k in it["match"].get("daily_check") or []:
            by[k] = by.get(k, 0) + 1
        cells += sum(1 for e in (it.get("live") or []) if e.get("instant_src_diff"))
        oow += int(it["match"].get("own_1110_out_of_window_cells") or 0)
    return {"scope": "page", "n_matches": sum(1 for it in items if it["match"].get("daily_check")),
            "by_reason": dict(sorted(by.items())), "instant_src_diff_cells_in_live": cells,
            "own_1110_out_of_window_cells": oow}


def build_live(d: _Data, mpk: int, jingcai_date: str, mode: str, as_of: datetime,
               rule_1110: Optional[list[dict]] = None) -> list[dict]:
    """即时盘口：mode=all 时返回时间线全部变化点。

    0.1.9（用户 2026-10-10）：竞彩日 11:10 的自采快照不再作为一种对外盘口阶段返回；
    参数 rule_1110 只为兼容旧调用保留，不再使用。"""
    out: list[dict] = []
    if mode == "all":
        segs = [sg for sg in d.timeline.get(mpk, []) if (_to_cn(sg["seg_start_at"]) or as_of) <= as_of]
        out.extend(_timeline_entry(sg, None, None) for sg in segs)
    return out


def build_last_prematch(d: _Data, mpk: int, book: str, market: str,
                        kickoff: datetime | None, as_of: datetime) -> dict | None:
    """开赛前最后一条即时快照（timeline 变化点 ∪ 有真实报价时刻的快照）；开赛后的不算。"""
    if kickoff is None:
        return None
    cutoff = min(kickoff, as_of)
    best: tuple[datetime, dict] | None = None
    for s in d.timeline.get(mpk, []):
        if s["book"] != book or s["market"] != market or s["is_inplay"]:
            continue
        ts = _to_cn(s["seg_start_at"])
        if ts is None or ts > cutoff:
            continue
        if best is None or ts >= best[0]:
            best = (ts, _timeline_entry(s, None, None))
    for r in d.snap_by_match.get(mpk, []):
        if r["book"] != book or r["market"] != market:
            continue
        if _snap_basis(r, "mid")[0].startswith("api_"):
            continue  # 报价时刻未知，不能当最后一条
        ts = _to_cn(r["recorded_at"])
        if ts is None or ts > cutoff:
            continue
        if best is None or ts > best[0]:
            asian = market == "asian"
            best = (ts, {
                "book": book, "market": market, "label": None, "target_at": None,
                "recorded_at": _iso(ts), "valid_until": None,
                "line": _neg(r["line"]) if asian else _r(r["line"]),
                "home_water": _r(r["water_home"]), "away_water": _r(r["water_away"]),
                "home": None if asian else _r(r["price_home"]),
                "draw": _r(r["price_draw"]), "away": None if asian else _r(r["price_away"]),
                "over_water": _r(r["water_over"]), "under_water": _r(r["water_under"]),
                "water_source": WATER_SRC_MAP.get(r["water_src"]) if r["water_src"] else None,
                "is_inplay": False, "source": f"odds_snapshot/{r['channel']}/{r['point']}",
                **_LIVE_MERGE_BLANK,
            })
    if best is None:
        return None
    ts, e = best
    mins = (kickoff - ts).total_seconds() / 60.0
    e["minutes_before_kickoff"] = round(mins, 1)
    e["stale"] = not (0 <= mins <= LAST_PREMATCH_FRESH_MINUTES)
    return e


# ----------------------------------------------------------------------------- baseline

class _Baseline:
    """按 (market, book, phase) 的滚动中位数；样本只取竞彩日严格早于查询日的场（防泄漏铁律）。"""

    def __init__(self, window_days: int | None, min_n: int):
        self.window_days = window_days
        self.min_n = min_n
        self._raw: dict[tuple, list[tuple[str, float]]] = {}
        self._sorted: dict[tuple, tuple[list[str], list[float]]] = {}

    def add(self, key: tuple, jingcai_date: str, value: float | None) -> None:
        if value is None or not jingcai_date:
            return
        self._raw.setdefault(key, []).append((jingcai_date, float(value)))

    def freeze(self) -> None:
        for k, v in self._raw.items():
            v.sort()
            self._sorted[k] = ([d for d, _ in v], [x for _, x in v])

    def get(self, key: tuple, jingcai_date: str) -> tuple[float | None, int]:
        dates, vals = self._sorted.get(key, ([], []))
        hi = bisect.bisect_left(dates, jingcai_date)  # 严格早于本场竞彩日
        lo = 0
        if self.window_days:
            try:
                start = (date_cls.fromisoformat(jingcai_date) - timedelta(days=self.window_days)).isoformat()
                lo = bisect.bisect_left(dates, start)
            except ValueError:
                pass
        sample = vals[lo:hi]
        n = len(sample)
        if n < self.min_n or n == 0:
            return None, n
        return statistics.median(sample), n


class _FallbackP25(_Baseline):
    """hl_v0.3 返还率兜底线：按 (market, book)（三阶段合并）取真实水位返还率 P25；样本只取竞彩日严格早于本场的
    （as-of，不偷看未来），不设窗口；n < FALLBACK_P25_MIN_N 也给 p25，但 fallback_hl_eligible=false（前端灰字）。
    分位数用线性插值（numpy 默认 / Excel PERCENTILE.INC）。"""

    def __init__(self):
        super().__init__(None, 1)

    def get(self, key: tuple, jingcai_date: str) -> tuple[float | None, int]:
        dates, vals = self._sorted.get(key, ([], []))
        sample = sorted(vals[:bisect.bisect_left(dates, jingcai_date)])
        n = len(sample)
        if n == 0:
            return None, 0
        return _quantile(sample, FALLBACK_P25_Q), n


def _quantile(sample: list[float], q: float) -> float | None:
    """线性插值分位数（numpy 默认 / Excel PERCENTILE.INC）；sample 已升序。"""
    n = len(sample)
    if n == 0:
        return None
    pos = (n - 1) * q
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    return sample[lo] + (sample[hi] - sample[lo]) * (pos - lo)


class _FallbackQ:
    """hl_v0.3.1（0.3.20）返还率兜底分位数：按 (market, book, phase) 分开算 P10 / P25 / P90。

    - n = 场次数：同一场同一阶段只记 1 个值（按 match pk 去重），不按格子 / 主客两侧计。
    - as-of 不变：样本只取竞彩日严格早于本场竞彩日的场（不设窗口），与 0.3.19 P25 同一时间纪律。
    - 只收真实水位（water_source=actual）、人工复核场不进（调用方负责）。
    """

    def __init__(self):
        self._raw: dict[tuple, dict[int, tuple[str, float]]] = {}
        self._sorted: dict[tuple, tuple[list[str], list[float]]] = {}
        self._cache: dict[tuple, tuple[dict[str, float | None], int]] = {}

    def add(self, key: tuple, jingcai_date: str, match_pk: int, value: float | None) -> None:
        if value is None or not jingcai_date:
            return
        self._raw.setdefault(key, {})[int(match_pk)] = (jingcai_date, float(value))

    def freeze(self) -> None:
        for k, v in self._raw.items():
            pairs = sorted(v.values())
            self._sorted[k] = ([d for d, _ in pairs], [x for _, x in pairs])

    def get(self, key: tuple, jingcai_date: str) -> tuple[dict[str, float | None], int]:
        dates, vals = self._sorted.get(key, ([], []))
        hi = bisect.bisect_left(dates, jingcai_date)  # 严格早于本场竞彩日
        ck = (key, hi)
        if ck not in self._cache:
            sample = sorted(vals[:hi])
            self._cache[ck] = ({name: _quantile(sample, q) for name, q in FALLBACK_QS.items()}, len(sample))
        return self._cache[ck]


def fallback_hl_level(return_rate: float | None, water_source: str | None, qs: dict[str, float | None],
                      eligible: bool) -> str | None:
    """hl_v0.3.1 兜底档（只管上色）：< P10 medium、< P25 light、≥ P90 high（中性，不是告警）；
    只对真实水位（actual）的格子判，n < 100 / 缺返还率 → null。"""
    if return_rate is None or not eligible or water_source != "actual":
        return None
    p10, p25, p90 = qs.get("p10"), qs.get("p25"), qs.get("p90")
    if p10 is not None and return_rate < p10:
        return "medium"
    if p25 is not None and return_rate < p25:
        return "light"
    if p90 is not None and return_rate >= p90:
        return "high"
    return None


# ----------------------------------------------------------------------------- response models (OpenAPI)

class _M_(BaseModel):
    model_config = ConfigDict(extra="allow")


class Triple(_M_):
    home: Optional[float] = None
    draw: Optional[float] = None
    away: Optional[float] = None


class TierPoint(_M_):
    phase: Optional[str] = None
    line: Optional[float] = None  # hl_v0.3
    home: Optional[float] = None
    away: Optional[float] = None


class TierCross(_M_):
    kind: Optional[str] = None  # hl_v0.3：line（初/临盘口不同）| water_tier（同盘档位换算跨档）
    from_: TierPoint = Field(alias="from")
    to: TierPoint
    sides: Optional[list[str]] = None


class AhCell(_M_):
    line: Optional[float] = None
    home_water: Optional[float] = None
    away_water: Optional[float] = None
    water_source: Optional[Literal["actual", "tier_midpoint"]] = None
    water_src: Optional[Literal["actual", "tier_midpoint"]] = None  # 0.3.22：与 water_source 同义，前端二选一
    book_lane: Optional[Literal["macau_manual", "macau_5df"]] = None  # 0.3.22：仅澳门主列/并列
    water_move_eligible: Optional[bool] = None  # 0.3.18 F：mid/close 与上一阶段同盘且两格都是 actual → true
    tier_cross: Optional[TierCross] = None  # hl_v0.3：只在 close 格；初/临不同盘 kind=line，同盘档位跨档 kind=water_tier；只悬停
    tier_cross_mid: Optional[bool] = None  # hl_v0.3：初/临同盘但中盘换过盘 → true（照常比水位、照常上色）
    fallback_p10: Optional[float] = None  # hl_v0.3.1：本公司本阶段真实水位返还率 P10（竞彩日严格早于本场）
    fallback_p25: Optional[float] = None  # hl_v0.3.1：同上 P25（0.3.19 是三阶段合并，0.3.20 起按阶段）
    fallback_p90: Optional[float] = None  # hl_v0.3.1：同上 P90（≥ P90 = 返还率偏高，中性色，只悬停）
    fallback_n: Optional[int] = None  # 本公司本阶段样本场次数（不是格子数）
    fallback_hl_eligible: Optional[bool] = None  # fallback_n >= 100；false → 兜底判不上色（灰字）
    fallback_hl_level: Optional[Literal["light", "medium", "high"]] = None  # 后端按上面三条判好的档（前端可直接用）
    water_censored: Optional[bool] = None
    recorded_at: Optional[str] = None
    target_at: Optional[str] = None
    basis: Optional[str] = None
    source: Optional[str] = None
    available: bool = False
    hidden_reason: Optional[str] = None
    minutes_since_open: Optional[float] = None
    return_rate: Optional[float] = None
    return_rate_baseline: Optional[float] = None
    return_rate_baseline_n: Optional[int] = None
    return_rate_dev: Optional[float] = None
    baseline_method: Optional[Literal["empirical", "fixed_fallback"]] = None
    open_basis: Optional[str] = None  # 仅 open：first_tick | api_opening | legacy_import
    open_basis_reason: Optional[str] = None  # 0.3.17：open_basis=null 时 no_open_data | after_as_of
    earliest_ts_quote_at: Optional[str] = None
    ts_inferred: Optional[bool] = None  # Since 2026-10-10 nothing is inferred: false whenever open_basis is set
    status: Optional[str] = None  # only open: ok | missing | suspect_truncated (user rule 2026-10-10)
    source_kind: Optional[str] = None  # only open: official_open | manual | null
    first_captured_at: Optional[str] = None  # only open: first moment we captured this book and market ourselves
    open_time: Optional[str] = None  # only open: real opening time, null when unknown
    open_time_known: Optional[bool] = None  # only open
    backtest_eligible: Optional[bool] = None  # only open: false unless status is ok
    truncation_checked: Optional[bool] = None  # only Asian handicap open: whether the quantile table check ran
    truncation_reason: Optional[str] = None  # only open: why the check did not run
    truncation_group: Optional[str] = None  # only open: book_league | book_only
    first_record_lead_minutes: Optional[float] = None  # only open: minutes between the first record and kickoff
    late_p95_lead_minutes: Optional[float] = None  # only open: threshold read from the quantile table
    usable_at_mid: Optional[bool] = None
    usable_at_close: Optional[bool] = None
    unusable_reason: Optional[str] = None
    phase_pending: Optional[bool] = False  # 0.3.20：快照行；待归阶段
    features_ok: Optional[bool] = True  # 0.3.20：默认 true；false=占位推算不进特征
    phase_assign_late: Optional[bool] = False  # 0.3.20：开赛确认晚于目标


class InstantAlt(_M_):
    line: Optional[float] = None
    water: Optional[dict[str, Optional[float]]] = None  # 亚盘 {home,away}；大小球 {over,under}；1X2 {home,draw,away}
    tick_at: Optional[str] = None
    origin: Optional[str] = None  # 0.3.20：timeline（自采在窗内，时间线作对照）| own_capture（自采超窗，照存）
    captured_at: Optional[str] = None  # 0.3.20：实际抓取 / 报价时刻
    fetch_lag_min: Optional[float] = None  # 0.3.20：自采 captured_at − 11:10（分钟）；时间线 null
    out_of_window: Optional[bool] = None  # 0.3.20：自采超出 [11:00, 11:20] → true


class LiveEntry(_M_):
    book: str
    market: str
    label: Optional[str] = None
    target_at: Optional[str] = None
    recorded_at: Optional[str] = None
    valid_until: Optional[str] = None
    line: Optional[float] = None
    home_water: Optional[float] = None
    away_water: Optional[float] = None
    home: Optional[float] = None
    draw: Optional[float] = None
    away: Optional[float] = None
    over_water: Optional[float] = None
    under_water: Optional[float] = None
    water_source: Optional[str] = None
    is_inplay: bool = False
    source: Optional[str] = None
    # 0.3.19：即时（11:10）合并时间线 + 自采 live 行（只在 label=rule_1110 的条目上有值）
    origin: Optional[str] = None  # timeline | own_capture
    odds_source: Optional[str] = None  # hist（时间线回补）| live（前向自采）
    capture: Optional[str] = None  # own | null
    captured_at: Optional[str] = None  # 实际抓取时刻（自采 = fetched_at；时间线 = 该段报价时刻）
    fetch_lag_min: Optional[float] = None  # 自采：captured_at − 11:10（分钟）
    merge_rule: Optional[str] = None  # own_capture_first;alt=timeline
    alt: Optional[InstantAlt] = None  # 两边都有时，时间线表的值（只对照，不参与升降盘 / 策略）
    instant_src_diff: Optional[bool] = None  # 两边都有时：盘口不同或水位差 > 0.03 → true（进日核对）；自采超窗 → null
    own_capture_out_of_window: Optional[bool] = None  # 0.3.20：有自采时 true=超出 [11:00,11:20]（主值退回时间线）/ false=窗内
    main_source_reason: Optional[str] = None  # 0.3.20：own_capture_out_of_window | own_capture_out_of_window;no_timeline
    minutes_before_kickoff: Optional[float] = None
    stale: Optional[bool] = None


class AhBook(_M_):
    open: AhCell
    mid: AhCell
    close: AhCell
    mid_real: Optional[AhCell] = None
    close_real: Optional[AhCell] = None
    last_prematch: Optional[LiveEntry] = None


class X1x2Cell(_M_):
    home: Optional[float] = None
    draw: Optional[float] = None
    away: Optional[float] = None
    complete: bool = False
    recorded_at: Optional[str] = None
    fetched_at: Optional[str] = None
    target_at: Optional[str] = None
    basis: Optional[str] = None
    source: Optional[str] = None
    available: bool = False
    hidden_reason: Optional[str] = None
    minutes_since_open: Optional[float] = None
    return_rate: Optional[float] = None
    return_rate_baseline: Optional[float] = None
    return_rate_baseline_n: Optional[int] = None
    return_rate_dev: Optional[float] = None
    kelly: Optional[Triple] = None
    kelly_base: Optional[Literal["pinnacle", "multi_avg"]] = None
    kelly_base_n_books: Optional[int] = None
    baseline_method: Optional[Literal["empirical", "fixed_fallback"]] = None
    fallback_p10: Optional[float] = None  # hl_v0.3.1：欧赔同亚盘口径（按 公司 × 阶段，n = 场次）
    fallback_p25: Optional[float] = None
    fallback_p90: Optional[float] = None
    fallback_n: Optional[int] = None
    fallback_hl_eligible: Optional[bool] = None
    fallback_hl_level: Optional[Literal["light", "medium", "high"]] = None
    kelly_multi_avg: Optional[Triple] = None
    multi_avg_n_books: Optional[int] = None
    n_avg: Optional[int] = None
    water_source: Optional[Literal["actual", "tier_midpoint"]] = None  # 0.3.18 E：直接报价 actual / 档位换算 tier_midpoint / 未知 null
    open_basis: Optional[str] = None  # 仅 open：first_tick | api_opening | legacy_import
    open_basis_reason: Optional[str] = None  # 0.3.17：open_basis=null 时 no_open_data | after_as_of
    earliest_ts_quote_at: Optional[str] = None
    ts_inferred: Optional[bool] = None  # Since 2026-10-10 nothing is inferred: false whenever open_basis is set
    status: Optional[str] = None  # only open: ok | missing | suspect_truncated (user rule 2026-10-10)
    source_kind: Optional[str] = None  # only open: official_open | manual | null
    first_captured_at: Optional[str] = None  # only open: first moment we captured this book and market ourselves
    open_time: Optional[str] = None  # only open: real opening time, null when unknown
    open_time_known: Optional[bool] = None  # only open
    backtest_eligible: Optional[bool] = None  # only open: false unless status is ok
    truncation_checked: Optional[bool] = None  # only Asian handicap open: whether the quantile table check ran
    truncation_reason: Optional[str] = None  # only open: why the check did not run
    truncation_group: Optional[str] = None  # only open: book_league | book_only
    first_record_lead_minutes: Optional[float] = None  # only open: minutes between the first record and kickoff
    late_p95_lead_minutes: Optional[float] = None  # only open: threshold read from the quantile table
    usable_at_mid: Optional[bool] = None
    usable_at_close: Optional[bool] = None
    unusable_reason: Optional[str] = None
    phase_pending: Optional[bool] = False  # 0.3.20
    features_ok: Optional[bool] = True
    phase_assign_late: Optional[bool] = False


class ApiClosing(_M_):
    label: Literal["api_closing"] = "api_closing"
    display_name: str = API_CLOSING_DISPLAY
    home: Optional[float] = None
    draw: Optional[float] = None
    away: Optional[float] = None
    complete: bool = False
    quote_time_known: bool = False
    fetched_at: Optional[str] = None
    basis: str = "api_closing"
    source: Optional[str] = None
    reference_only: bool = True


class X1x2Book(_M_):
    open: X1x2Cell
    mid: X1x2Cell
    close: X1x2Cell
    mid_real: Optional[X1x2Cell] = None
    close_real: Optional[X1x2Cell] = None
    last_prematch: Optional[LiveEntry] = None
    api_closing: Optional[ApiClosing] = None  # 仅完赛（result 可见）后出现


class ConsensusProbs(Triple):
    n_books: int
    n_avg: Optional[int] = None
    books: list[str]
    includes_self: Optional[bool] = None


class BasePhase(_M_):
    pinnacle: Optional[Triple] = None
    multi_avg: Optional[ConsensusProbs] = None


class JcCell(_M_):
    home: Optional[float] = None
    draw: Optional[float] = None
    away: Optional[float] = None
    complete: bool = False
    jc_1x2_incomplete: Optional[bool] = None  # 0.3.21
    recorded_at: Optional[str] = None
    fetched_at: Optional[str] = None
    target_at: Optional[str] = None
    basis: Optional[str] = None
    source: Optional[str] = None
    available: bool = False
    hidden_reason: Optional[str] = None
    missing_reason: Optional[str] = None  # 0.3.21：collector_empty | legacy_home_only | jc_1x2_incomplete | …
    water_source: Optional[Literal["actual", "tier_midpoint"]] = None  # 0.3.18 E：竞彩同欧赔口径
    open_basis: Optional[str] = None  # 仅 open：first_tick | api_opening | legacy_import
    open_basis_reason: Optional[str] = None  # 0.3.17：open_basis=null 时 no_open_data | after_as_of
    earliest_ts_quote_at: Optional[str] = None
    ts_inferred: Optional[bool] = None  # Since 2026-10-10 nothing is inferred: false whenever open_basis is set
    status: Optional[str] = None  # only open: ok | missing | suspect_truncated (user rule 2026-10-10)
    source_kind: Optional[str] = None  # only open: official_open | manual | null
    first_captured_at: Optional[str] = None  # only open: first moment we captured this book and market ourselves
    open_time: Optional[str] = None  # only open: real opening time, null when unknown
    open_time_known: Optional[bool] = None  # only open
    backtest_eligible: Optional[bool] = None  # only open: false unless status is ok
    truncation_checked: Optional[bool] = None  # only Asian handicap open: whether the quantile table check ran
    truncation_reason: Optional[str] = None  # only open: why the check did not run
    truncation_group: Optional[str] = None  # only open: book_league | book_only
    first_record_lead_minutes: Optional[float] = None  # only open: minutes between the first record and kickoff
    late_p95_lead_minutes: Optional[float] = None  # only open: threshold read from the quantile table
    usable_at_mid: Optional[bool] = None
    usable_at_close: Optional[bool] = None
    unusable_reason: Optional[str] = None
    out_of_window: Optional[bool] = None  # 0.3.21：竞彩 11:10 同亚盘窗
    fetch_lag_min: Optional[float] = None
    alt: Optional[dict[str, Any]] = None  # 0.3.21：超窗对照
    phase_pending: Optional[bool] = False  # 0.3.20：竞彩格同快照口径
    features_ok: Optional[bool] = True
    phase_assign_late: Optional[bool] = False


class JcHhadCell(_M_):
    """0.3.21：让球胜平负；goal_line = 决策时刻可见线（可能来自 hist）。"""
    goal_line: Optional[float] = None
    decision_line: Optional[float] = None
    current_line: Optional[float] = None  # 主表当前线（悬停）
    home: Optional[float] = None
    draw: Optional[float] = None
    away: Optional[float] = None
    complete: bool = False
    jc_1x2_incomplete: Optional[bool] = None
    line_rev: int = 0
    goal_line_raw: Optional[str] = None
    available: bool = False
    missing_reason: Optional[str] = None
    source: Optional[str] = None
    captured_at: Optional[str] = None
    recorded_at: Optional[str] = None
    target_at: Optional[str] = None
    basis: Optional[str] = None
    water_source: Optional[Literal["actual", "tier_midpoint"]] = None
    out_of_window: Optional[bool] = None
    fetch_lag_min: Optional[float] = None
    post_decision_line_change: bool = False
    from_hist: bool = False
    usable_at_mid: Optional[bool] = None
    usable_at_close: Optional[bool] = None
    hidden_reason: Optional[str] = None
    alt: Optional[dict[str, Any]] = None
    phase_pending: Optional[bool] = False
    features_ok: Optional[bool] = True
    phase_assign_late: Optional[bool] = False


class MatchInfo(_M_):
    match_id: str
    match_pk: int
    jc_id: Optional[str] = None
    jc_no: Optional[int] = None
    jingcai_date: str
    kickoff_at: Optional[str] = None
    kickoff_hour: Optional[int] = None
    kickoff_minute_known: bool = False
    kickoff_source: Optional[str] = None  # 0.3.17：5df | jingcai（5DF 12:00 占位符改用竞彩时刻）
    kickoff_placeholder: Optional[str] = None  # 0.3.17：5df_1200 = 5DF 开赛疑为占位符
    kickoff_jc: Optional[str] = None  # 0.3.18：竞彩官方开赛时刻（整点；列缺失 = null，如现网）
    kickoff_jc_conflict: Optional[bool] = None  # 0.3.19：|kickoff_jc − kickoff_at| ≥ 90 分钟 → true（进日核对）
    postponed: bool = False  # 0.3.19：推迟场（kickoff_actual ≠ kickoff_original）
    kickoff_original: Optional[str] = None
    kickoff_actual: Optional[str] = None
    postponed_announced_at: Optional[str] = None
    postpone_ts_unknown: Optional[bool] = None  # 查不到公告时刻 → true，目标按原定算
    postpone_void_check: Optional[str] = None  # pending = 推迟 > 1h，暂不结算
    postpone_delay_minutes: Optional[float] = None
    kickoff_drift_min: Optional[int] = None  # 0.3.20：实际开赛（场中首笔报价推算）− 赛程开赛（kickoff_at），分钟；只作信息展示
    kickoff_drift_basis: Optional[str] = None  # inplay_tick_est（config/kickoff_drift.json）| null
    kickoff_drift_est_at: Optional[str] = None  # 推算的实际开赛时刻
    kickoff_rev: int = 0  # 0.3.20：matches.kickoff_rev；缺列/NULL→0；前端 >0 才悬停「开赛时间曾变更（第 N 次）」
    # 下面三字段：比赛级默认值（前端可绑）；格子级以 ah/x1x2/jc 格为准（快照行）。不读 state、不重算。
    phase_pending: bool = False
    features_ok: bool = True
    phase_assign_late: bool = False
    manual_review: bool = False  # 0.3.19：人工复核（不进特征 / 结算 / 基准）
    manual_review_reason: Optional[str] = None  # ah_sign_mismatch_probe
    daily_check: list[str] = []  # 进日核对的原因
    instant_src_diff: Optional[bool] = None  # 0.3.19：即时（11:10）自采 vs 时间线 盘口不同或水位差>0.03；null=没有两路同时有数
    own_1110_out_of_window_cells: Optional[int] = None  # 0.3.20：本场自采超出 [11:00,11:20] 的 (公司, 市场) 格子数；>0 进日核对
    scope: str
    league: Optional[str] = None
    league_canonical: Optional[str] = None
    competition_type: Optional[str] = None
    home_team: Optional[str] = None
    away_team: Optional[str] = None
    home_team_canonical: Optional[str] = None
    away_team_canonical: Optional[str] = None


class Schedule(_M_):
    phase_exception: bool
    phase_target: Optional[str] = None
    kickoff_minute_known: Optional[bool] = None
    kickoff_source: Optional[str] = None
    kickoff_placeholder: Optional[str] = None
    exception_rule: Optional[str] = None  # 0.3.18：jc_code_ge_2300（有竞彩编号）| time_window_2300_1130（无编号）
    kickoff_for_exception: Optional[str] = None  # 0.3.19：推迟场 = kickoff_original
    postpone_target_basis: Optional[dict[str, str]] = None  # 0.3.19：{mid,close}: original | announced_new | original_ts_unknown
    kickoff_check: Optional[str] = None  # not_applicable | agrees_jingcai | agrees_jingcai_hour | placeholder_use_jingcai | placeholder_no_jingcai
    open_target_time: Optional[str] = None
    mid_target_time: Optional[str] = None
    close_target_time: Optional[str] = None
    mid_real_target_time: Optional[str] = None
    close_real_target_time: Optional[str] = None
    source: str


class Prediction(_M_):
    leak_suspect: Optional[str] = None  # 0.3.20：config/leak_suspect.json（LEGACY V4–V7 = cutoff_plus24）；前端灰掉
    ledger_note: Optional[str] = None  # 0.3.19：旧冻结 S2/N4「触发依据是换算水位」（悬停用）
    ledger_note_reason: Optional[str] = None  # tier_water_trigger
    direction: Optional[str] = None
    strategy: Optional[str] = None
    settle_book: Optional[str] = None
    rationale: list[Any] = []
    confidence: Optional[float] = None
    stake: Optional[int] = None
    stake_rule: Optional[str] = None
    produced_at: Optional[str] = None
    updated_at: Optional[str] = None
    message_sent_at: Optional[str] = None
    produced_before_kickoff: Optional[bool] = None


class Result(_M_):
    home_goals: int
    away_goals: int
    total_goals: Optional[int] = None
    score: str
    wdl: Optional[str] = None


class Settlement(_M_):
    settlement_version: str
    settle_book: str
    line: Optional[float] = None
    side: Optional[str] = None
    juice: Optional[float] = None
    juice_source: Optional[str] = None
    juice_reason: Optional[str] = None
    stake_units: Optional[int] = None
    stake_units_source: Optional[str] = None
    code: Literal["win", "win_half", "push", "lose_half", "lose", "no_bet"]
    pnl_units: float
    source: str = "backend"


class TableRow(_M_):
    match_id: str
    match: MatchInfo
    phase_exception: bool
    schedule: Schedule
    ah: dict[str, AhBook]
    x1x2: dict[str, X1x2Book]
    x1x2_base: dict[str, BasePhase]
    multi_avg_prob: Optional[ConsensusProbs] = None
    jc_1x2: dict[str, JcCell]
    jc_hhad: Optional[dict[str, JcHhadCell]] = None  # 0.3.21
    live: list[LiveEntry]
    prediction: Optional[Prediction] = None
    prediction_hidden_reason: Optional[str] = None
    produced_at: Optional[str] = None
    as_of: str
    result: Optional[Result] = None
    result_hidden_reason: Optional[str] = None
    hidden_reason: Optional[str] = None
    settlement: Optional[Settlement] = None
    settlement_hidden_reason: Optional[str] = None


class DbMeta(_M_):
    db: Literal["live", "v2d3"]
    promoted: bool
    readonly: bool = False


class TableResponse(_M_):
    meta: Optional[DbMeta] = None  # 0.3.18：实例所指库；v2d3 副本 promoted=false
    api_version: str
    config_version: str
    format: str
    as_of: str
    as_of_requested: Optional[str] = None
    date_from: str
    date_to: str
    scope: str
    strategy: str
    channel: str
    include_live: str
    settlement_version: str
    books: list[str]
    config: dict[str, Any]
    data_sources: dict[str, Any]
    total: int
    limit: int
    offset: int
    count: int
    daily_check_summary: Optional[dict[str, Any]] = None  # 0.3.19：本页各日核对原因计数（全量见 scripts/daily_check_report.py）
    strategy_leak_suspect: Optional[str] = None  # 0.3.20：本次 strategy 参数的 leak_suspect（config overlay）
    items: list[TableRow]


# ----------------------------------------------------------------------------- assembly

def _minute_known(row: sqlite3.Row, has_ka: bool) -> bool:
    """开赛分钟是否可信：优先 matches.kickoff_minute_known；列缺失/NULL 时退回「有 kickoff_at」。"""
    v = row["kickoff_minute_known"] if "kickoff_minute_known" in row.keys() else None
    return bool(v) if v is not None else bool(has_ka)


def _kickoff_check(row: sqlite3.Row, kickoff: datetime | None, has_ka: bool, extras: dict) -> dict:
    """0.3.17 开赛占位符：5DF 恰为 12:00 时对竞彩时刻（extras.jingcai_kickoff_at 完整时刻 > matches.kickoff_hour 整点）。

    读时生效（现网库不可写）；规则见 collection_schedule.check_kickoff_placeholder。
    """
    jc_at = extras.get("jingcai_kickoff_at") if isinstance(extras, dict) else None
    jc_dt = _M().parse_iso_dt(jc_at) if isinstance(jc_at, str) and jc_at else None
    jc_h = extras.get("jingcai_kickoff_hour") if isinstance(extras, dict) else None
    jc_h = jc_h if isinstance(jc_h, int) and not isinstance(jc_h, bool) else row["kickoff_hour"]
    return cs.check_kickoff_placeholder(kickoff, row["jingcai_date"], jc_h, jc_dt,
                                        minute_known=_minute_known(row, has_ka))


def _rowv(row: sqlite3.Row, key: str) -> Any:
    return row[key] if key in row.keys() else None


def _parse_cn(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        return _to_cn(str(v))
    except (TypeError, ValueError):
        return None


KICKOFF_JC_CONFLICT_MIN = 90  # 0.3.19：|kickoff_jc − kickoff_at| ≥ 90 分钟 → kickoff_jc_conflict=true，进日核对


def postpone_info(row: sqlite3.Row) -> dict | None:
    """0.3.19 推迟场（v2d3 列；现网无列 → None）。kickoff_at 仍是实际开赛。"""
    ko = _parse_cn(_rowv(row, "kickoff_original"))
    if ko is None:
        return None
    ka = _parse_cn(_rowv(row, "kickoff_actual")) or _parse_cn(_rowv(row, "kickoff_at"))
    an = _parse_cn(_rowv(row, "postponed_announced_at"))
    ts_unknown = bool(_rowv(row, "postpone_ts_unknown")) or an is None
    void_check = _rowv(row, "postpone_void_check") or cs.postpone_void_check(ko, ka)
    void = None
    if cs.POSTPONE_VOID_HOURS is not None and ka is not None and \
            (ka - ko).total_seconds() / 3600.0 > float(cs.POSTPONE_VOID_HOURS):
        void = "void_postponed"
    return {"kickoff_original": ko, "kickoff_actual": ka, "announced_at": an, "ts_unknown": ts_unknown,
            "void_check": void_check, "void": void,
            "delay_minutes": round((ka - ko).total_seconds() / 60.0, 1) if ka else None}


def match_flags(row: sqlite3.Row, kickoff: datetime | None) -> dict:
    """match 级 0.3.19 字段：推迟场 / kickoff_jc_conflict / 人工复核 + 日核对原因。"""
    pp = postpone_info(row)
    jc = _parse_cn(_rowv(row, "kickoff_jc"))
    ka = _parse_cn(_rowv(row, "kickoff_at")) or kickoff
    conflict = None
    if jc is not None and ka is not None:
        conflict = abs((ka - jc).total_seconds()) / 60.0 >= KICKOFF_JC_CONFLICT_MIN
    mr = bool(_rowv(row, "manual_review"))
    out = {
        "postponed": pp is not None and pp["kickoff_actual"] != pp["kickoff_original"],
        "kickoff_original": _iso(pp["kickoff_original"]) if pp else None,
        "kickoff_actual": _iso(pp["kickoff_actual"]) if pp else None,
        "postponed_announced_at": _iso(pp["announced_at"]) if pp else None,
        "postpone_ts_unknown": pp["ts_unknown"] if pp else None,
        "postpone_void_check": pp["void_check"] if pp else None,
        "postpone_delay_minutes": pp["delay_minutes"] if pp else None,
        "kickoff_jc_conflict": conflict,
        "kickoff_rev": kickoff_rev_of(row),  # 0.3.20：列缺失 → 0
        # 比赛级占位/阶段默认值；格子级见各 cell（分析师写快照后才会变）
        **PHASE_FEATURE_DEFAULTS,
        "manual_review": mr,
        "manual_review_reason": _rowv(row, "manual_review_reason") if mr else None,
    }
    daily = []
    if pp and pp["ts_unknown"]:
        daily.append("postpone_ts_unknown")
    if pp and pp["void_check"] == "pending":
        daily.append("postpone_void_pending")
    if conflict:
        daily.append("kickoff_jc_conflict")
    if mr:
        daily.append("manual_review")
    out["daily_check"] = daily
    return out


def _schedule(row: sqlite3.Row, kickoff: datetime | None, has_ka: bool, ko: dict | None = None) -> dict:
    mk = (bool(ko["minute_known"]) if ko is not None else _minute_known(row, has_ka)) and kickoff is not None
    out = {"phase_exception": False, "exception_rule": None, "phase_target": PHASE_TARGET if mk else "hour_floor",
           "kickoff_minute_known": mk,
           "kickoff_source": ((ko or {}).get("kickoff_source")
                              or ("jingcai_hour_synth" if kickoff is not None else None)),
           "kickoff_placeholder": (ko or {}).get("kickoff_placeholder"),
           "kickoff_check": (ko or {}).get("status"),
           "open_target_time": None, "mid_target_time": None,
           "close_target_time": None, "mid_real_target_time": None, "close_real_target_time": None,
           "live_rule_1110_target_time": None,
           "kickoff_for_exception": None, "postpone_target_basis": None,  # 0.3.19：推迟场才有值
           "source": "app.collection_schedule.rule_targets (rule; exact_minute) + actual_targets (real)"}
    jd, kh = row["jingcai_date"], row["kickoff_hour"]
    try:
        base = datetime.strptime(jd, "%Y-%m-%d").replace(tzinfo=cs.TZ_CN)
        out["live_rule_1110_target_time"] = base.replace(hour=LIVE_RULE_1110[0], minute=LIVE_RULE_1110[1]).isoformat()
    except (TypeError, ValueError):
        pass
    if kh is None:
        return out
    # 0.3.18：例外场按竞彩编号（有编号：开赛 ≥ D 23:00，无上限；无编号：[D 23:00, D+1 11:30]）
    has_code = bool(row["jc_id"]) if "jc_id" in row.keys() else True
    out["exception_rule"] = cs.EXCEPTION_RULE if has_code else cs.EXCEPTION_RULE_LEGACY
    pp = postpone_info(row)
    if pp is not None:
        # 0.3.19 推迟场：例外场按编号 + kickoff_original 判；T−8h/T−1h 只用到目标时刻为止已公布的开赛时间
        k0 = pp["kickoff_original"]
        out["phase_exception"] = bool(cs.phase_exception(jd, k0, int(kh), mk, has_code))
        pt = cs.postpone_targets(k0, pp["kickoff_actual"], pp["announced_at"])
        out["kickoff_for_exception"] = _iso(k0)
        out["postpone_target_basis"] = dict(pt["basis"])
        if out["phase_exception"]:
            ats = cs.rule_targets(jd, int(kh), k0, mk, has_jc_code=has_code)
            out["mid_target_time"], out["close_target_time"] = ats[0].isoformat(), ats[1].isoformat()
            out["mid_real_target_time"] = pt["mid"].isoformat()
            out["close_real_target_time"] = pt["close"].isoformat()
        else:
            out["mid_target_time"], out["close_target_time"] = pt["mid"].isoformat(), pt["close"].isoformat()
        return out
    out["phase_exception"] = bool(cs.phase_exception(jd, kickoff, int(kh), mk, has_code))
    ats = cs.rule_targets(jd, int(kh), kickoff, mk, has_jc_code=has_code)
    if ats:
        out["mid_target_time"], out["close_target_time"] = ats[0].isoformat(), ats[1].isoformat()
    if out["phase_exception"] and kickoff is not None:
        k = kickoff if mk else kickoff.replace(minute=0, second=0, microsecond=0)
        act = cs.actual_targets(k)
        out["mid_real_target_time"] = act["t8"].isoformat()
        out["close_real_target_time"] = act["t1"].isoformat()
    return out


def _settle(pred: sqlite3.Row | None, res: dict | None, d: _Data, mpk: int,
            settlement_version: str) -> tuple[dict | None, str | None]:
    if pred is None:
        return None, "no_prediction"
    if res is None:
        return None, "result_hidden"
    settle_book = pred["settle_book"] or "macau_close"
    book, phase = _M()._parse_settle_book(settle_book)
    direction = (pred["direction"] or "").strip()
    units = None
    src = None
    if pred["stake"] is not None:
        units, src = max(0, int(float(pred["stake"]))), "prediction"
    else:
        units, src = int(_M().DEFAULT_STAKE_RULE["default_units"]), "default_units"
    base = {"settlement_version": settlement_version, "settle_book": settle_book,
            "line": None, "side": None, "juice": None, "juice_source": None, "juice_reason": None,
            "stake_units": None, "stake_units_source": None}
    if direction in bt.NO_BET_DIRECTIONS:
        return {**base, "code": "no_bet", "pnl_units": 0.0, "source": "backend"}, None
    side = bt.normalize_side(direction)
    if side is None:
        return None, "bad_direction"
    oa = d.legacy_asian.get((mpk, book, phase))  # 与 _load_strategy_bets 同源：odds_asian(settle_book)
    if oa is None or oa["handicap"] is None:
        return None, "no_settle_line"
    water = oa["home_water"] if side == "主" else oa["away_water"]
    juice, jsrc, jreason = bt.resolve_juice(water, FALLBACK_JUICE, book=book,
                                            water_src=oa["water_src"],
                                            settlement_version=settlement_version)
    code, pnl = bt.settle_pnl(int(res["home_goals"]), int(res["away_goals"]),
                              float(oa["handicap"]), side, float(units), juice)
    return {**base, "line": float(oa["handicap"]), "side": side, "juice": _r(juice, 4),
            "juice_source": jsrc, "juice_reason": jreason, "stake_units": units,
            "stake_units_source": src, "code": code,
            "pnl_units": round(pnl, 6), "source": "backend"}, None


def flatten(obj: Any, prefix: str = "", out: dict | None = None) -> dict:
    out = {} if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            flatten(v, f"{prefix}_{k}" if prefix else str(k), out)
    else:
        out[prefix] = obj
    return out


def _blank(model: type[BaseModel], drop: tuple[str, ...] = ()) -> dict:
    return {k: None for k in model.model_fields if k not in drop}


_RR_KEYS = ("return_rate", "return_rate_baseline", "return_rate_baseline_n", "return_rate_dev",
            "baseline_method", "fallback_p10", "fallback_p25", "fallback_p90", "fallback_n", "fallback_hl_eligible",
            "fallback_hl_level")
_OPEN_KEYS = ("open_basis", "open_basis_reason", "earliest_ts_quote_at", "ts_inferred", "usable_at_mid", "usable_at_close",
              "unusable_reason") + osx.OPEN_STATUS_FIELDS
_KELLY_KEYS = ("kelly", "kelly_base", "kelly_base_n_books", "kelly_multi_avg", "multi_avg_n_books", "n_avg")


def _stable(item: dict) -> dict:
    """flat 用：把为 null 的子对象展开成全 null 模板，保证每行列集合一致（AG Grid columnDefs 固定）。"""
    it = json.loads(json.dumps(item))
    live_blank = _blank(LiveEntry)
    tri = _blank(Triple)
    # Opening status fields (user rule 2026-10-10): every opening cell carries the same keys.
    _open_cells = [it["ah"][b].get("open") for b in it.get("ah", {}) if isinstance(it["ah"][b], dict)] + \
        [it["x1x2"][b].get("open") for b in it.get("x1x2", {}) if isinstance(it["x1x2"][b], dict)] + \
        [(it.get("jc_1x2") or {}).get("open"), (it.get("jc_hhad") or {}).get("open")]
    for c in _open_cells:
        if isinstance(c, dict):
            for fk, fv in osx.blank_open_status().items():
                c.setdefault(fk, fv)
    for b in BOOKS:
        for rp in REAL_PHASES:
            if it["ah"][b].get(rp) is None:
                it["ah"][b][rp] = _blank(AhCell, _RR_KEYS + _OPEN_KEYS)
            if it["x1x2"][b].get(rp) is None:
                it["x1x2"][b][rp] = _blank(X1x2Cell, _RR_KEYS + _KELLY_KEYS + _OPEN_KEYS)
        for ph in PHASES + REAL_PHASES:  # 0.3.18 F：tier_cross 为 null 时展开成全 null 模板（flat 列集合固定）
            c = it["ah"][b].get(ph)
            if isinstance(c, dict) and c.get("tier_cross") is None:
                c["tier_cross"] = {"kind": None, "from": {"phase": None, "line": None, "home": None, "away": None},
                                   "to": {"phase": None, "line": None, "home": None, "away": None}, "sides": None}
        for blk in (it["ah"][b], it["x1x2"][b]):
            if blk.get("last_prematch") is None:
                blk["last_prematch"] = dict(live_blank)
        for blk in (it["ah"][b], it["x1x2"][b]):
            for k in _OPEN_KEYS:
                blk["open"].setdefault(k, None)
        if it["x1x2"][b].get("api_closing") is None:  # flat 列集合固定；赛前全 null，不含任何数值
            it["x1x2"][b]["api_closing"] = _blank(ApiClosing)
        for ph in PHASES:
            c = it["x1x2"][b][ph]
            for k in ("kelly", "kelly_multi_avg"):
                if c.get(k) is None:
                    c[k] = dict(tri)
    # 0.3.22：flat 列固定 — macau_5df 三阶段 + 空 mid_real/close_real/last_prematch
    m5 = it.get("ah", {}).get(MACAU_5DF_BOOK_KEY)
    if not isinstance(m5, dict):
        m5 = {}
        it.setdefault("ah", {})[MACAU_5DF_BOOK_KEY] = m5
    for ph in PHASES:
        if not isinstance(m5.get(ph), dict):
            m5[ph] = _blank(AhCell)
            m5[ph]["book_lane"] = "macau_5df"
            m5[ph]["available"] = False
            m5[ph]["hidden_reason"] = "no_data"
        else:
            m5[ph].setdefault("book_lane", "macau_5df")
            m5[ph].setdefault("water_src", m5[ph].get("water_source"))
            if m5[ph].get("tier_cross") is None:
                m5[ph]["tier_cross"] = {"kind": None,
                    "from": {"phase": None, "line": None, "home": None, "away": None},
                    "to": {"phase": None, "line": None, "home": None, "away": None}, "sides": None}
    for rp in REAL_PHASES:
        if m5.get(rp) is None:
            m5[rp] = _blank(AhCell, _RR_KEYS + _OPEN_KEYS)
    if m5.get("last_prematch") is None:
        m5["last_prematch"] = _blank(LiveEntry)
    for k in _OPEN_KEYS:
        m5.setdefault("open", {}).setdefault(k, None)

    if isinstance(it.get("schedule"), dict) and it["schedule"].get("postpone_target_basis") is None:
        it["schedule"]["postpone_target_basis"] = {"mid": None, "close": None}  # 0.3.19：flat 列集合固定
    for ph in PHASES:
        bp = it["x1x2_base"][ph]
        if bp.get("pinnacle") is None:
            bp["pinnacle"] = dict(tri)
        if bp.get("multi_avg") is None:
            bp["multi_avg"] = _blank(ConsensusProbs)
    if it.get("multi_avg_prob") is None:
        it["multi_avg_prob"] = _blank(ConsensusProbs)
    if it.get("prediction") is None:
        it["prediction"] = _blank(Prediction)
    if it.get("result") is None:
        it["result"] = _blank(Result)
    if it.get("settlement") is None:
        it["settlement"] = _blank(Settlement)
    return it


def _flat_row(item: dict) -> dict:
    """扁平：嵌套键用 _ 连接；live / rationale / consensus.books 等列表原样保留；match.* 去掉前缀。"""
    src = _stable(item)
    live = src.pop("live", [])
    match = src.pop("match", {})
    pred = src.get("prediction")
    rationale = None
    if isinstance(pred, dict):
        pred = dict(pred)
        rationale = pred.pop("rationale", None)
        src["prediction"] = pred
    flat = {**{k: v for k, v in match.items()}, **flatten({k: v for k, v in src.items() if k != "match_id"})}
    flat["match_id"] = item["match_id"]
    flat["prediction_rationale"] = rationale
    flat["live"] = live
    return flat


def build_table(conn: sqlite3.Connection, *, date_from: str | None, date_to: str | None,
                scope: str, strategy: str, channel: str, include_live: str,
                settlement_version: str | None, as_of: str | None,
                baseline_window_days: int | None, baseline_min_n: int,
                limit: int, offset: int, fmt: str = "nested",
                now: datetime | None = None) -> dict:
    M = _M()
    scope_db = SCOPE_ALIASES.get(scope)
    if scope_db is None:
        raise HTTPException(400, f"unknown scope={scope!r}; use jc|ext|all")
    s_ver = M._resolve_settlement_version({"settlement_version": settlement_version})
    now = now or datetime.now(cs.TZ_CN)
    as_of_req = None
    eff_as_of = now
    if as_of:
        as_of_req = M.parse_iso_dt(as_of)
        if as_of_req is None:
            raise HTTPException(400, f"bad as_of={as_of!r}")
        eff_as_of = min(as_of_req, now)  # 未来 as_of 一律钳到 now

    # 日期默认：都不传 → date_to = 库内 ≤ as_of 日的最大竞彩日，date_from = date_to − 6 天
    for v, nm in ((date_from, "date_from"), (date_to, "date_to")):
        if v:
            try:
                date_cls.fromisoformat(v)
            except ValueError:
                raise HTTPException(400, f"bad {nm}={v!r}; want YYYY-MM-DD")
    if not date_to:
        r = conn.execute("SELECT MAX(jingcai_date) AS d FROM matches WHERE jingcai_date <= ?",
                         (eff_as_of.date().isoformat(),)).fetchone()
        date_to = (r["d"] if r and r["d"] else eff_as_of.date().isoformat())
    if not date_from:
        date_from = (date_cls.fromisoformat(date_to) - timedelta(days=6)).isoformat()
    if date_from > date_to:
        raise HTTPException(400, "date_from > date_to")

    where = "m.jingcai_date BETWEEN ? AND ?"
    args: list[Any] = [date_from, date_to]
    if scope_db != "all":
        where += " AND m.scope = ?"
        args.append(scope_db)
    total = conn.execute(f"SELECT COUNT(1) AS c FROM matches m WHERE {where}", args).fetchone()["c"]
    rows = conn.execute(
        f"""SELECT m.*, l.name_zh_canonical AS league_canonical,
                   th.name_zh_canonical AS home_canonical, ta.name_zh_canonical AS away_canonical
            FROM matches m
            LEFT JOIN leagues l ON l.id = m.league_id
            LEFT JOIN teams th ON th.id = m.home_team_id
            LEFT JOIN teams ta ON ta.id = m.away_team_id
            WHERE {where}
            ORDER BY m.jingcai_date, COALESCE(m.kickoff_hour, 99), COALESCE(m.jc_no, 999999), m.id
            LIMIT ? OFFSET ?""", args + [limit, offset]).fetchall()
    page_ids = [r["id"] for r in rows]
    d = _Data(conn, page_ids)

    # 基准样本：库内全部场（不受本次日期/scope/分页影响），按同一取数口径算 open/mid/close 返还率
    all_rows = conn.execute("SELECT * FROM matches").fetchall()
    meta = {r["match_id"]: M.loads_json(r["extras_json"]) or {}
            for r in conn.execute("SELECT match_id, extras_json FROM match_meta")}
    baseline = _Baseline(baseline_window_days, baseline_min_n)
    fb_q = _FallbackQ()  # hl_v0.3.1：返还率兜底分位数 P10/P25/P90 = 每家 × 每阶段真实水位返还率（as-of，n=场次）
    cells_cache: dict[int, tuple] = {}
    for mr in all_rows:
        extras = meta.get(mr["id"], {})
        kick = M.resolve_kickoff_dt(mr, extras)
        has_ka = bool((isinstance(extras.get("kickoff_at"), str) and extras.get("kickoff_at"))
                      or M.row_get(mr, "kickoff_at"))
        ko = _kickoff_check(mr, kick if has_ka else None, has_ka, extras) if has_ka else None
        if ko is not None:
            kick = ko["kickoff"]
        sched = _schedule(mr, kick, has_ka, ko)
        ah = {b: {p: build_ah_cell(d, mr["id"], b, p, sched, eff_as_of, kick) for p in PHASES} for b in BOOKS}
        # 0.3.22：澳门 5DF 并列列（与主列分 lane；基准/异动只在同 lane 内）
        ah_macau_5df = {p: build_macau_5df_cell(d, mr["id"], p, sched, eff_as_of, kick) for p in PHASES}
        x = {b: {p: build_x_cell(d, mr["id"], b, p, sched, eff_as_of, kick) for p in PHASES} for b in BOOKS}
        _mr_review = bool(_rowv(mr, "manual_review"))  # 0.3.19：人工复核场不进基准（不进任何特征）
        for b in (() if _mr_review else BOOKS):
            for p in PHASES:
                # hl_v0.2：empirical 基准与「≥20 场」计数只用真实水位（actual）；档位中点 / 来源不明一律不进
                if ah[b][p]["water_source"] == "actual" and \
                        ah[b][p]["water_source"] not in BASELINE_EXCLUDED_WATER_SOURCES:
                    baseline.add(("ah", b, p), mr["jingcai_date"], ah[b][p]["return_rate"])
                    fb_q.add(("ah", b, p), mr["jingcai_date"], mr["id"], ah[b][p]["return_rate"])
                # 0.3.18 E：欧赔基准同样 real_only（water_source=actual）
                if x[b][p].get("water_source") == "actual":
                    baseline.add(("x1x2", b, p), mr["jingcai_date"], x[b][p]["return_rate"])
                    fb_q.add(("x1x2", b, p), mr["jingcai_date"], mr["id"], x[b][p]["return_rate"])
        if not _mr_review:
            for p in PHASES:
                c5 = ah_macau_5df[p]
                if c5.get("water_source") == "actual":
                    baseline.add(("ah", MACAU_5DF_BOOK_KEY, p), mr["jingcai_date"], c5["return_rate"])
                    fb_q.add(("ah", MACAU_5DF_BOOK_KEY, p), mr["jingcai_date"], mr["id"], c5["return_rate"])
        cells_cache[mr["id"]] = (kick, has_ka, sched, ah, x, extras, ah_macau_5df)
    baseline.freeze()
    fb_q.freeze()

    preds = {r["match_id"]: r for r in conn.execute(
        "SELECT * FROM predictions WHERE strategy = ?", (strategy,))}
    results = {r["match_id"]: r for r in conn.execute("SELECT * FROM results")}

    items: list[dict] = []
    trunc_table = osx.load_quantile_table()  # None when the analyst has not supplied the table
    for row in rows:
        mpk = row["id"]
        kick, has_ka, sched, ah, x, extras, ah_macau_5df = cells_cache[mpk]
        exc = sched["phase_exception"]
        jd = row["jingcai_date"]
        # 返还率基准 + 偏离 + 跨度
        for market, block in (("ah", ah), ("x1x2", x)):
            for b in BOOKS:
                open_ts = _to_cn(block[b]["open"]["recorded_at"])
                for p in PHASES:
                    c = block[b][p]
                    med, n = baseline.get((market, b, p), jd)
                    c["return_rate_baseline"] = _r(med, 6)
                    c["return_rate_baseline_n"] = n
                    qs, fn = fb_q.get((market, b, p), jd)
                    c["fallback_p10"], c["fallback_p25"], c["fallback_p90"] = (
                        _r(qs["p10"], 6), _r(qs["p25"], 6), _r(qs["p90"], 6))
                    c["fallback_n"] = fn
                    c["fallback_hl_eligible"] = fn >= FALLBACK_MIN_N
                    c["fallback_hl_level"] = fallback_hl_level(c["return_rate"], c.get("water_source"), qs,
                                                               c["fallback_hl_eligible"])
                    # 0.3.17（前端 §13 对账）：返还率本身为空 → baseline_method 也为空
                    c["baseline_method"] = (None if c["return_rate"] is None
                                            else BASELINE_METHOD_EMPIRICAL if med is not None
                                            else BASELINE_METHOD_FALLBACK)
                    if med is not None and c["return_rate"] is not None:
                        c["return_rate_dev"] = _r(c["return_rate"] - med, 6)
                    ts = _to_cn(c["recorded_at"])
                    if p != "open" and ts and open_ts:
                        c["minutes_since_open"] = round((ts - open_ts).total_seconds() / 60.0, 1)
        x_base = _apply_kelly(x)
        ah_out, x_out = {}, {}
        for b in BOOKS:
            apply_water_move_flags(ah[b], d, mpk, b)  # 0.3.18 F
            ah_out[b] = dict(ah[b])
            x_out[b] = dict(x[b])
            for rp in REAL_PHASES:
                if exc:
                    ac = build_ah_cell(d, mpk, b, rp, sched, eff_as_of, kick)
                    for k in _RR_KEYS:
                        ac.pop(k, None)
                    if ac.get("target_at") is None:
                        ac["target_at"] = sched.get(f"{rp}_target_time")
                    ah_out[b][rp] = ac
                    xc = build_x_cell(d, mpk, b, rp, sched, eff_as_of, kick)
                    if xc.get("target_at") is None:
                        xc["target_at"] = sched.get(f"{rp}_target_time")
                    x_out[b][rp] = xc
                else:
                    ah_out[b][rp] = None
                    x_out[b][rp] = None
            open_ah, open_x = _to_cn(ah[b]["open"]["recorded_at"]), _to_cn(x[b]["open"]["recorded_at"])
            for rp in REAL_PHASES:
                for cell, o_ts in ((ah_out[b][rp], open_ah), (x_out[b][rp], open_x)):
                    ts = _to_cn(cell["recorded_at"]) if cell else None
                    if cell is not None and ts and o_ts:
                        cell["minutes_since_open"] = round((ts - o_ts).total_seconds() / 60.0, 1)
            ah_out[b]["last_prematch"] = build_last_prematch(d, mpk, b, "asian", kick, eff_as_of)
            x_out[b]["last_prematch"] = build_last_prematch(d, mpk, b, "euro_1x2", kick, eff_as_of)
        # 0.3.22：macau_5df 并列 — 同 lane 返还率基准 / 水位异动；绝不与 ah.macau 交叉比
        open_ts_5 = _to_cn(ah_macau_5df["open"]["recorded_at"])
        for p in PHASES:
            c = ah_macau_5df[p]
            med, n = baseline.get(("ah", MACAU_5DF_BOOK_KEY, p), jd)
            c["return_rate_baseline"] = _r(med, 6)
            c["return_rate_baseline_n"] = n
            qs, fn = fb_q.get(("ah", MACAU_5DF_BOOK_KEY, p), jd)
            c["fallback_p10"], c["fallback_p25"], c["fallback_p90"] = (
                _r(qs["p10"], 6), _r(qs["p25"], 6), _r(qs["p90"], 6))
            c["fallback_n"] = fn
            c["fallback_hl_eligible"] = fn >= FALLBACK_MIN_N
            c["fallback_hl_level"] = fallback_hl_level(c["return_rate"], c.get("water_source"), qs,
                                                       c["fallback_hl_eligible"])
            c["baseline_method"] = (None if c["return_rate"] is None
                                    else BASELINE_METHOD_EMPIRICAL if med is not None
                                    else BASELINE_METHOD_FALLBACK)
            if med is not None and c["return_rate"] is not None:
                c["return_rate_dev"] = _r(c["return_rate"] - med, 6)
            ts = _to_cn(c["recorded_at"])
            if p != "open" and ts and open_ts_5:
                c["minutes_since_open"] = round((ts - open_ts_5).total_seconds() / 60.0, 1)
        apply_water_move_flags(ah_macau_5df, d, mpk, "macau")  # tier 读 asian；异动只比 5df 格内 open→close
        ah_out[MACAU_5DF_BOOK_KEY] = dict(ah_macau_5df)
        ah_out[MACAU_5DF_BOOK_KEY]["mid_real"] = None
        ah_out[MACAU_5DF_BOOK_KEY]["close_real"] = None
        ah_out[MACAU_5DF_BOOK_KEY]["last_prematch"] = None
        for _b, _blk in ah_out.items():
            if isinstance(_blk, dict):
                finalize_ah_open(d, mpk, _blk.get("open"), _b, kick, row["competition_name"], eff_as_of, trunc_table,
                                 match_uid=row["match_uid"])
        for _b, _blk in x_out.items():
            if isinstance(_blk, dict):
                finalize_ah_open(d, mpk, _blk.get("open"), _b, kick, row["competition_name"], eff_as_of, trunc_table,
                                 market="euro_1x2", match_uid=row["match_uid"])
        jc = {p: build_jc_cell(d, mpk, p, sched, eff_as_of, kick, jingcai_date=jd)
              for p in PHASES}
        jc_hhad = {p: build_jc_hhad_cell(d, mpk, p, sched, eff_as_of, jingcai_date=jd)
                   for p in PHASES}

        # 预测（冻结只读；字段同 /matches/{id}/prediction）
        pr = preds.get(mpk)
        prediction, pred_hidden = None, None
        if pr is None:
            pred_hidden = "no_prediction"
        else:
            produced = _to_cn(pr["produced_at"])
            if as_of_req is not None and produced is not None and produced > eff_as_of:
                pred_hidden = "produced_after_as_of"
            else:
                prediction = M.build_prediction_from_row(pr)
                prediction["produced_before_kickoff"] = (
                    (produced < kick) if (produced is not None and kick is not None) else None)

        # 赛果（防泄漏）
        res_row = results.get(mpk)
        result, res_hidden = None, None
        if kick is None:
            res_hidden = "kickoff_unknown"
        elif eff_as_of < kick:
            res_hidden = "before_kickoff"
        elif eff_as_of < kick + RESULT_VISIBLE_AFTER_KICKOFF:
            res_hidden = "not_visible_yet"  # 开赛后 3h 内不显示赛果
        elif res_row is None or res_row["home_goals"] is None or res_row["away_goals"] is None:
            res_hidden = "no_result"
        else:
            hg, ag = int(res_row["home_goals"]), int(res_row["away_goals"])
            result = {"home_goals": hg, "away_goals": ag,
                      "total_goals": res_row["total_goals"] if res_row["total_goals"] is not None else hg + ag,
                      "score": f"{hg}-{ag}", "wdl": res_row["wdl"]}
        if result is not None:  # 欧赔 api_closing：只在完赛（赛果可见）后单独返回
            for b in BOOKS:
                apc = build_api_closing(d, mpk, b)
                if apc is not None:
                    x_out[b]["api_closing"] = apc
        if result is None:
            settlement, set_hidden = None, ("no_prediction" if pr is None else res_hidden)
        else:
            settlement, set_hidden = _settle(pr if prediction is not None else None, result, d, mpk, s_ver)
            # 0.3.19：人工复核 / 推迟待核 → 不结算（validate 同口径单独计数）；void_postponed 钩子（时限未定 = 不生效）
            _mf = match_flags(row, kick)
            _pp = postpone_info(row)
            if settlement is not None and _mf["manual_review"]:
                settlement, set_hidden = None, "manual_review"
            elif settlement is not None and _pp and _pp["void"] == "void_postponed":
                settlement = {**settlement, "code": "void_postponed", "pnl_units": None}
            elif settlement is not None and _mf["postpone_void_check"] == "pending":
                settlement, set_hidden = None, "postpone_void_pending"
            if pr is not None and prediction is None:
                set_hidden = pred_hidden

        # 0.3.19：即时（11:10）两路来源；不论 include_live 都算，用于日核对 instant_src_diff
        r1110 = rule_1110_entries(d, mpk, jd, eff_as_of)
        _mf_row = match_flags(row, kick)
        _isd = [e for e in r1110 if e.get("instant_src_diff") is not None]
        _mf_row["instant_src_diff"] = (any(e["instant_src_diff"] for e in _isd) if _isd else None)
        # 0.1.9：11:10 快照不再是对外盘口阶段，两路来源不一致不再进日核对
        # 0.3.20：自采超出 [11:00, 11:20] → 主值已退回时间线；进日核对（采集延迟），不算 instant_src_diff
        _oow = sum(1 for e in r1110 if e.get("own_capture_out_of_window"))
        _mf_row["own_1110_out_of_window_cells"] = _oow if any(
            e.get("own_capture_out_of_window") is not None for e in r1110) else None
        # 0.1.9：自采超窗不再进日核对
        # 0.3.20：kickoff_drift_min（只作信息展示；不碰推迟字段 / 目标时刻 / 日核对）
        _mf_row.update(kd.drift_fields(row["match_uid"], kick, eff_as_of))
        items.append({
            "match_id": row["match_uid"],
            "match": {
                "match_id": row["match_uid"], "match_pk": mpk,
                "jc_id": row["jc_id"], "jc_no": row["jc_no"], "jingcai_date": jd,
                "kickoff_at": _iso(kick), "kickoff_hour": row["kickoff_hour"],
                "kickoff_minute_known": bool(sched.get("kickoff_minute_known")),
                "kickoff_source": sched.get("kickoff_source"),
                "kickoff_placeholder": sched.get("kickoff_placeholder"),
                "kickoff_jc": row["kickoff_jc"] if "kickoff_jc" in row.keys() else None,
                **_mf_row,
                "scope": row["scope"], "league": row["competition_name"],
                "league_canonical": row["league_canonical"],
                "competition_type": row["competition_type"],
                "home_team": row["home_team"], "away_team": row["away_team"],
                "home_team_canonical": row["home_canonical"], "away_team_canonical": row["away_canonical"],
            },
            "phase_exception": exc,
            "schedule": {k: v for k, v in sched.items() if k != "live_rule_1110_target_time"},
            "ah": ah_out,
            "x1x2": x_out,
            "x1x2_base": x_base,
            "multi_avg_prob": x_base["close"]["multi_avg"],
            "jc_1x2": jc,
            "jc_hhad": jc_hhad,
            "live": build_live(d, mpk, jd, include_live, eff_as_of, rule_1110=r1110),
            "prediction": prediction,
            "prediction_hidden_reason": pred_hidden,
            "produced_at": prediction["produced_at"] if prediction else None,
            "as_of": eff_as_of.isoformat(),
            "result": result,
            "result_hidden_reason": res_hidden,
            "hidden_reason": res_hidden,  # 前端名：赛果/结算为空的原因（= result_hidden_reason）
            "settlement": settlement,
            "settlement_hidden_reason": set_hidden,
        })

    out_items = items if fmt == "nested" else [_flat_row(i) for i in items]
    return {
        "meta": app_db.db_meta(),  # 0.3.18：db=live|v2d3、promoted、readonly
        "api_version": M.API_VERSION,
        "config_version": CONFIG_VERSION,
        "format": fmt,
        "as_of": eff_as_of.isoformat(),
        "as_of_requested": _iso(as_of_req),
        "date_from": date_from, "date_to": date_to,
        "scope": scope, "strategy": strategy, "channel": channel,
        "include_live": include_live,
        "settlement_version": s_ver,
        "books": list(BOOKS),
        "config": {
            "result_visible_after_kickoff_hours": RESULT_VISIBLE_AFTER_KICKOFF.total_seconds() / 3600,
            "last_prematch_fresh_minutes": LAST_PREMATCH_FRESH_MINUTES,
            "kickoff_drift": kd.config_block(),  # 0.3.20：只作信息展示
            "phase_feature_flags": {  # 0.3.20：副本自采；列或 extras；缺省如下（不读 state、不重算）
                "cell_fields": ["phase_pending", "features_ok", "phase_assign_late"],
                "match_fields": ["kickoff_rev", "phase_pending", "features_ok", "phase_assign_late"],
                "defaults": {**PHASE_FEATURE_DEFAULTS, "kickoff_rev": 0},
                "source": "odds_snapshot column|extras_json (cell); matches.kickoff_rev (match)",
                "wiring_note": "columns not yet on live/v2d3 as of 0.3.20 — defaults until analyst ingest writes them; "
                               "API never reads 5dollar/live/state"},
            "jc_fundamentals": {  # 0.3.21
                "odds_jc_had": "preferred for jc_1x2; odds_jc_home frozen home_only → jc_1x2_incomplete",
                "odds_jc_hhad": "decision-time line via select_hhad_at_decision (main+hist); never default current main for features",
                "hist_superseded_at": True,
                "jc_1110_window": "[11:00,11:20] same as asian; out_of_window → not main",
                "snapshot_projection_jc_spf_hhad": "skipped (local collector empty)",
                "stats_obs": "as_of+source; manual_seed tagged; constituent gate kickoff+3h≤T_decision",
                "injury_known_empty": "only when source explicitly empty; never for missing fetch",
            },
            "macau_5df": {  # 0.3.22
                "path": "ah.macau_5df.{open|mid|close}",
                "parallel_to": "ah.macau",
                "book_lane": {"ah.macau": "macau_manual", "ah.macau_5df": "macau_5df"},
                "source_label": "5df",
                "primary_rule": "ah.macau = manual only (rule_legacy / pure legacy_import); 5DF water never on primary",
                "identify": "odds_snapshot|odds_asian|timeline with source in 5df_macauslot_history|5dollar_history "
                            "or extras from_channel/from_point fill markers; doubt → macau_5df",
                "same_lane_only": "water_move/tier_cross/return_rate highlight never cross macau ↔ macau_5df",
                "live": "macau_5df unavailable until 5DF capture/promotion; FE may hide column",
                "hist_v3": "对照主列 macau_manual；5DF 水位在并列列",
                "does_not_enter": ["settlement", "frozen_predictions_direction"],
            },
            "baseline": {"method": "rolling_median", "group_by": ["market", "book", "phase"],
                         "sample": "jingcai_date strictly before this match's jingcai_date (all scopes, all DB rows)",
                         "window_days": baseline_window_days, "min_n": baseline_min_n,
                         "excludes": ["water_censored (ah)", "water_source=tier_midpoint (ah, 旧档位中点换算)",
                                      "incomplete 1x2", "api_closing 1x2"],
                         "baseline_method": {"empirical": f"company×phase rolling median, n>={baseline_min_n} real-water samples",
                                             "fixed_fallback": "n<min_n → baseline null; use config.return_rate_fallback (hl_v0.1 §4/§5)"}},
            "return_rate_fallback": RETURN_RATE_FALLBACK,  # hl_v0.2 固定兜底（留作对照）；hl_v0.3 起前端改用格子 fallback_p25
            "return_rate_fallback_hl_v03": {
                "version": "hl_v0.3.1",
                "rule": "per (market ah|x1x2, book, phase open|mid|close) separately: P10 / P25 / P90 of real-water "
                        "(water_source=actual) return rates, one value per match (n = matches, not cells/sides), "
                        "jingcai_date strictly before this match (as-of, no window); linear interpolation",
                "cell_fields": ["fallback_p10", "fallback_p25", "fallback_p90", "fallback_n", "fallback_hl_eligible",
                                "fallback_hl_level"],
                "levels": FALLBACK_HL_LEVELS, "alert_levels": list(FALLBACK_ALERT_LEVELS),
                "high_display": {"colour": "neutral", "hover": "返还率偏高", "alert": False, "daily_check": False,
                                 "export_risk_stats": False},
                "level_requires": "fallback_hl_eligible and water_source=actual and return_rate not null",
                "min_n": FALLBACK_MIN_N, "below_min_n": "no highlight, grey text 「样本不足，暂不判断」",
                "excludes": ["manual_review matches", "tier_midpoint / unknown water_source"],
                "changed_from_0_3_19": "0.3.19 pooled open+mid+close and counted cells; 0.3.20 per phase, n = matches"},
            "kelly_highlight": {"version": "hl_v0.3", "margin": KELLY_HL_MARGIN, "levels": KELLY_HL,  # hl_v0.3.1 未改凯利
                                "transitional": True,
                                "next": "hl_v0.4: per-outcome quantile thresholds kelly_thr_{h,d,a} / kelly_n_{h,d,a} (P85/P92/P97 of kelly/return_rate-1)"},
            "kelly": {"primary_base": "pinnacle_devig", "fallback_base": "multi_avg_loo",
                      "pinnacle_self_base": "multi_avg_loo", "devig": "p_i=(1/o_i)/sum(1/o)",
                      "multi_avg_loo": "mean of OTHER books' devig probs (leave-one-out), same phase; cell.n_avg = #books",
                      "multi_avg_reference": "x1x2_base.*.multi_avg includes all books (reference column only)",
                      "row_multi_avg_prob": "= x1x2_base.close.multi_avg"},
            "phase_exception_range": "jc-coded: kickoff >= D 23:00 (no upper bound); no code: [D 23:00, D+1 11:30] inclusive",
            "exception_rule": cs.EXCEPTION_RULE,
            "phase_target": PHASE_TARGET,
            "x1x2_return_hl_water": X1X2_RETURN_HL_WATER,  # 0.3.18 E：欧赔/竞彩格 water_source；基准只收 actual
            "water_move_rule": "hl_v0.3 §2: open -> close only (on close cell; open/mid null). same line & both actual -> "
                               "water_move_eligible=true; open!=close line -> tier_cross{kind:line} hover only (no colour); "
                               "same open/close line but mid changed -> tier_cross_mid=true (still compared & coloured); "
                               "tier_midpoint -> false; never used in strategies",
            "hl": "v0.3.1",
            "return_hl_water": RETURN_HL_WATER,  # hl_v0.2：返还率高亮只对 water_source=actual 的格子；基准/≥20 计数同口径（上色在前端）
            "kickoff_placeholder_rule": "5DF kickoff exactly 12:00: vs jingcai official time (meta.jingcai_kickoff_at, else matches.kickoff_hour hour-only). "
                                        "disagree -> use jingcai (schedule.kickoff_source=jingcai, kickoff_placeholder=5df_1200; hour-only -> hour_floor); "
                                        "no jingcai time -> minute unknown (hour_floor), kickoff_placeholder=5df_1200; hour-only agreement (12) -> keep 5DF "
                                        "(hour-only jingcai carries no natural date)",
            "open_basis": {"first_tick": "earliest timestamped API-history quote of this book/market (≤ as_of, pre-match); usable_at_mid/close = true",
                           "api_opening": "open time UNKNOWN (not guaranteed pre-decision); usable_at_X = earliest_ts_quote_at <= schedule.X_target_time (inclusive), else false + unusable_reason",
                           "legacy_import": "user hand-recorded opening quote (same definition as the interface opening quote, source_kind=manual, status=ok); the opening time is unknown (open_time=null, open_time_known=false) and no 11:10 time is assumed; usable_at_mid and usable_at_close are always true because opening is always earlier than the mid and close stages (analyst decision 2026-10-10); possible recording errors are checked by comparing sources",
                           "status": "open only: ok = real opening quote; missing = no real opening quote (our first capture never replaces it, it is kept in alt for Jingcai cells); suspect_truncated = Asian handicap first record of the 5DollarFootballAPI history is later than the 95th percentile in config/open_truncation_quantiles.json (book plus league group, falling back to book only when samples are fewer than min_samples); no table means no check",
                           "source_kind": "open only: official_open (interface or official opening data) | manual (user hand record) | null; manual and official_open are one definition of the opening quote, separated only for per-source backtest statistics",
                           "first_captured_at": "open only: first moment we captured this book and market ourselves; never used as the opening quote",
                           "open_time": "open only: real opening time; null when unknown (open_time_known=false)",
                           "backtest_eligible": "open only: false unless status = ok",
                           "earliest_ts_quote_at": "earliest timestamped quote incl. own captures (11:10); own captures never become first_tick",
                           "feature_rule": "any feature/strategy use of open-derived values MUST require usable_at_mid / usable_at_close; duration fields (minutes_since_open) are null for api_opening"},
            "api_closing": "1x2 api_closing never merged into close / return rate / kelly; "
                           "x1x2[book].api_closing (label=api_closing) only when result visible",
            "ah_line_convention": "home perspective; positive = home gives (odds_asian 记法；快照 API 记法已取反)",
            "real_phases_calc": "raw only (return_rate/kelly not computed for *_real — 术语文档未写)",
        },
        "data_sources": {"odds_snapshot": d.has_snapshot, "odds_timeline_seg": d.has_timeline,
                         "books_without_data": [b for b in BOOKS if not any(
                             i["ah"][b][p]["available"] or i["x1x2"][b][p]["available"]
                             for i in items for p in PHASES)],
                         "legacy_tables": ["odds_asian", "odds_euro_home", "odds_jc_home"]},
        "total": total, "limit": limit, "offset": offset, "count": len(items),
        "daily_check_summary": daily_check_summary(items),
        "strategy_leak_suspect": leak.leak_suspect_for(strategy),  # 0.3.20
        "items": out_items,
    }


@router.get(
    "/table/matches",
    summary="数据表页批量平铺（只读）",
    responses={200: {"model": TableResponse,
                     "description": "format=nested 的结构；format=flat 时 items 为扁平字典（键 = 嵌套路径用 _ 连接）"}},
)
def table_matches(
    date_from: Optional[str] = Query(None, description="竞彩日起 YYYY-MM-DD；缺省 = date_to − 6 天"),
    date_to: Optional[str] = Query(None, description="竞彩日止 YYYY-MM-DD；缺省 = 库内 ≤ as_of 日的最大竞彩日"),
    scope: str = Query("jc", pattern="^(jc|ext|all|jingcai|extra)$",
                       description="jc=竞彩(scope=jingcai)｜ext=非竞彩(scope=extra)｜all"),
    strategy: str = Query("CFFXDJ_5_V3", description="predictions.strategy；默认 V3"),
    channel: Literal["rule"] = Query("rule", description="快照通道；目前仅 rule（*_real 自动取 actual t8/t1）"),
    include_live: Literal["none", "all"] = Query(
        "none", description="none=live 空数组｜all=全部即时盘口（时间线变化点）。rule_1110 取值已于 0.1.9 删除"),
    settlement_version: Optional[str] = Query(
        None, description="缺省 = 现网默认 ah_v4_water_midpoint；可选 ah_v4_macau_actual_or_095"),
    as_of: Optional[str] = Query(None, description="截止时刻（ISO；无时区按 +08:00）；缺省/未来 = now"),
    baseline_window_days: Optional[int] = Query(
        None, ge=1, le=3650, description="返还率基准滚动窗口（天）；缺省 = 本场竞彩日之前全部（hl_v0 未定）"),
    baseline_min_n: int = Query(DEFAULT_BASELINE_MIN_N, ge=1, le=10000,
                                description="基准最少样本场数；不足返回 null"),
    format: Literal["nested", "flat"] = Query("nested", description="nested | flat（AG Grid 直接用）"),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
) -> dict:
    conn = connect_ro()
    try:
        return build_table(conn, date_from=date_from, date_to=date_to, scope=scope,
                           strategy=strategy, channel=channel, include_live=include_live,
                           settlement_version=settlement_version, as_of=as_of,
                           baseline_window_days=baseline_window_days, baseline_min_n=baseline_min_n,
                           limit=limit, offset=offset, fmt=format)
    finally:
        conn.close()
