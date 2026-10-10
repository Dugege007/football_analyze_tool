"""FastAPI local API for 比赛分析工具 Phase 0/1 (+ v1.1–v1.6, M3 composer, M4 compare, M5 stack/bankroll backtest)."""
from __future__ import annotations

import os
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
import math
import re
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Path as PathParam, Query
from pydantic import BaseModel, Field
from fastapi.middleware.cors import CORSMiddleware

from app.db import connect
from app import backtest as bt
from app import shadow_evaluable as shadow_ev  # N5/N5-PIN 不可评估记账
from app import collection_schedule
from app import leak_suspect as _leak  # 0.3.20
from app import odds_timeline_chart as otc  # 0.3.23

DEFAULT_STRATEGY = "CFFXDJ_5_V3"
TZ_CN = timezone(timedelta(hours=8))

# dispatch：相对采集点的待发窗（分钟）
# 主列表: abs(now - collect_at) <= PENDING_ABS_MIN；补发: include_overdue → collect_at <= now <= kickoff
PENDING_ABS_MIN = 30

API_VERSION = "0.3.23"

# D2：现网双写开关（拍板默认关；副本演练用 scripts/sync_odds_asian_from_snapshot.py）
DUAL_WRITE_ODDS_ASIAN = os.environ.get("DUAL_WRITE_ODDS_ASIAN", "0").strip().lower() in (
    "1", "true", "yes", "on",
)


app = FastAPI(title="比赛分析工具 Local API", version=API_VERSION)

_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@app.middleware("http")
async def _readonly_guard(request, call_next):
    """0.3.18：APP_READONLY=1（v2d3 副本实例）→ 所有写方法一律 403（含会写缓存的 POST /strategies/{id}/validate）。
    选 403 而不是「不写缓存的纯计算」：不依赖逐个接口改造，新加的写接口也自动挡住；连接另有 mode=ro 兜底。
    先于 CORS 注册 → CORS 在外层，403 也带 CORS 头。"""
    import app.db as _adb
    if _adb.READONLY and request.method.upper() in _WRITE_METHODS:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=403, content={
            "detail": f"read-only instance (db={_adb.DB_LABEL}): {request.method} {request.url.path} is disabled",
            "meta": _adb.db_meta()})
    return await call_next(request)


# 跨源资源共享（Cross-Origin Resource Sharing，简称 CORS）允许的前端来源。
# 通过环境变量 APP_CORS_ORIGINS 配置，多个来源用英文逗号分隔；填写 * 表示允许任意来源（此时不携带凭据）。
# 默认只允许本机网页界面开发服务器（端口 5173）。
_cors_raw = os.environ.get(
    "APP_CORS_ORIGINS", "http://127.0.0.1:5173,http://localhost:5173"
)
_cors_origins = [o.strip() for o in _cors_raw.split(",") if o.strip()]
_cors_any = "*" in _cors_origins

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if _cors_any else _cors_origins,
    allow_credentials=not _cors_any,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_conn() -> sqlite3.Connection:
    return connect()


def loads_json(raw: str | None) -> Any:
    if raw is None or raw == "":
        return None
    return json.loads(raw)


def resolve_match_row(conn: sqlite3.Connection, match_id: str) -> sqlite3.Row:
    """Accept match_uid or numeric internal id."""
    if match_id.isdigit():
        row = conn.execute(
            "SELECT * FROM matches WHERE id = ?", (int(match_id),)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM matches WHERE match_uid = ?", (match_id,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="match not found")
    return row


def row_get(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    """Column-safe getter (DB 未迁到 v1.2 时不炸)."""
    return row[key] if key in row.keys() else default


def build_match_obj(row: sqlite3.Row, meta_extras: dict | None = None) -> dict:
    extras = meta_extras or {}
    kickoff_at = row_get(row, "kickoff_at") or extras.get("kickoff_at")
    minute_known = row_get(row, "kickoff_minute_known")
    jc = None
    if row["jc_id"]:
        no = f"{row['jc_no']:03d}" if row["jc_no"] is not None else None
        jc_id = row["jc_id"]
        if no is None and jc_id and len(jc_id) >= 2:
            digits = "".join(c for c in jc_id if c.isdigit())
            no = digits.zfill(3) if digits else None
        weekday = row["weekday"]
        if jc_id and not weekday and jc_id[0] not in "0123456789":
            weekday = jc_id[0]
        jc = {"id": jc_id, "weekday": weekday, "no": no}
    return {
        "date": row["jingcai_date"],
        "weekday": row["weekday"],
        "scope": row["scope"],
        "jc": jc,
        "competition": {
            "name": row["competition_name"],
            "type": row["competition_type"],
            "stage": row["competition_stage"],
        },
        "kickoff_hour": row["kickoff_hour"],
        "kickoff_at": kickoff_at,
        "kickoff_minute_known": bool(minute_known) if minute_known is not None else False,
        "teams": {"home": row["home_team"], "away": row["away_team"]},
        "home_team_id": row_get(row, "home_team_id"),
        "away_team_id": row_get(row, "away_team_id"),
        "league_id": row_get(row, "league_id"),
        "ids": extras.get("ids") or {},
    }


def build_result(conn: sqlite3.Connection, match_pk: int) -> dict | None:
    r = conn.execute("SELECT * FROM results WHERE match_id = ?", (match_pk,)).fetchone()
    if not r:
        return None
    return {
        "home_goals": r["home_goals"],
        "away_goals": r["away_goals"],
        "total_goals": r["total_goals"],
        "wdl": r["wdl"],
    }


def build_stats(conn: sqlite3.Connection, match_pk: int) -> dict:
    """0.3.21：投影字段带 source_*/as_of_*；可选 stats_obs 分 kind 最新行。"""
    keys = {r[1] for r in conn.execute("PRAGMA table_info(stats)")}
    s = conn.execute("SELECT * FROM stats WHERE match_id = ?", (match_pk,)).fetchone()
    if not s:
        return {}
    recent = loads_json(s["recent_json"]) or {}
    if s["home_streak_last6"] is not None and "home_streak_last6" not in recent:
        recent = dict(recent)
        recent["home_streak_last6"] = s["home_streak_last6"]
    extras = loads_json(s["extras_json"]) or {}
    out: dict[str, Any] = {}
    if recent:
        out["recent"] = recent
    h2h = loads_json(s["h2h_json"])
    if h2h is not None:
        out["h2h"] = h2h
    out["rank"] = {"home": s["rank_home"], "away": s["rank_away"]}
    out["popularity_diff"] = s["popularity_diff"]
    if "support_proxy_odds" in extras:
        out["support_proxy_odds"] = extras["support_proxy_odds"]
    injury = loads_json(s["injury_json"])
    weather = loads_json(s["weather_json"])
    out["injury"] = injury  # 空保持 null；不伪 [] / known_empty
    out["weather"] = weather
    # 出处 / as_of（列缺失 = 现网 → 省略）
    meta: dict[str, Any] = {}
    for kind, sk, ak in (
        ("recent", "source_recent", "as_of_recent"),
        ("h2h", "source_h2h", "as_of_h2h"),
        ("rank", "source_rank", "as_of_rank"),
        ("injury", "source_injury", "as_of_injury"),
        ("weather", "source_weather", "as_of_weather"),
        ("popularity", "source_popularity", "as_of_popularity"),
    ):
        if sk in keys or ak in keys:
            meta[kind] = {
                "source": s[sk] if sk in keys else None,
                "as_of": s[ak] if ak in keys else None,
            }
    if meta:
        out["meta"] = meta
    # stats_obs：按 kind 取 as_of 最新一行（只读；manual_seed / bsd 分源）
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "stats_obs" in names:
        obs_out: dict[str, Any] = {}
        for r in conn.execute(
            "SELECT kind, as_of, source, payload_json, quality, fetched_at FROM stats_obs "
            "WHERE match_id=? ORDER BY kind ASC, as_of DESC",
            (match_pk,),
        ):
            if r["kind"] in obs_out:
                continue  # 已是该 kind 最新 as_of
            payload = loads_json(r["payload_json"])
            known_empty = None
            if isinstance(payload, dict) and "known_empty" in payload:
                known_empty = bool(payload.get("known_empty"))
            obs_out[r["kind"]] = {
                "as_of": r["as_of"], "source": r["source"], "payload": payload,
                "quality": r["quality"], "fetched_at": r["fetched_at"],
                "known_empty": known_empty,
            }
        out["obs"] = obs_out
    return out


def _overlay_asian_water(conn: sqlite3.Connection, match_pk: int, odds: dict) -> dict:
    """v1.7：odds_raw 保留原始 JSON（旧手工 crown/william 为档位）；读接口用 odds_asian 的真实/中点水位覆盖，
    并附 water_src / water_censored / water_tier_raw。"""
    asian = odds.get("asian") if isinstance(odds, dict) else None
    if not isinstance(asian, dict):
        return odds
    for r in conn.execute(
        "SELECT book, phase, home_water, away_water, water_src, water_censored, extras_json"
        " FROM odds_asian WHERE match_id = ? AND water_src IS NOT NULL",
        (match_pk,),
    ):
        ph = (asian.get(r["book"]) or {}).get(r["phase"])
        if not isinstance(ph, dict):
            continue
        ph["home_water"] = r["home_water"]
        ph["away_water"] = r["away_water"]
        ph["water_src"] = r["water_src"]
        ph["water_censored"] = bool(r["water_censored"])
        ex = loads_json(r["extras_json"]) or {}
        if ex.get("water_tier_raw") is not None:
            ph["water_tier_raw"] = ex["water_tier_raw"]
    return odds



def _load_jc_odds_blocks(conn: sqlite3.Connection, match_pk: int) -> dict[str, Any]:
    """0.3.21：从 odds_jc_had / odds_jc_hhad / odds_jc_home 组装竞彩块（overlay 到 odds_raw 或表路径）。"""
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    jc_home: dict[str, Any] = {}
    for r in conn.execute(
        "SELECT phase, home_win FROM odds_jc_home WHERE match_id = ?", (match_pk,)
    ):
        jc_home[r["phase"]] = {
            "home": r["home_win"], "draw": None, "away": None,
            "complete": False, "jc_1x2_incomplete": True,
            "source": "odds_jc_home", "missing_reason": "legacy_home_only",
        }
    jc_had: dict[str, Any] = {}
    if "odds_jc_had" in names:
        for r in conn.execute("SELECT * FROM odds_jc_had WHERE match_id = ?", (match_pk,)):
            incomplete = bool(r["jc_1x2_incomplete"] or 0)
            jc_had[r["phase"]] = {
                "home": r["home_odds"], "draw": r["draw_odds"], "away": r["away_odds"],
                "complete": not incomplete, "jc_1x2_incomplete": incomplete,
                "source": r["source"], "captured_at": r["captured_at"],
                "target_at": r["target_at"],
                "usable_at_mid": r["usable_at_mid"], "usable_at_close": r["usable_at_close"],
            }
    hhad_cols = {r[1] for r in conn.execute("PRAGMA table_info(odds_jc_hhad)")} if "odds_jc_hhad" in names else set()
    jc_hhad: dict[str, Any] = {}
    if "odds_jc_hhad" in names:
        from app.fundamentals import jc_incomplete as _jc_inc
        for r in conn.execute("SELECT * FROM odds_jc_hhad WHERE match_id = ?", (match_pk,)):
            gl = r["goal_line"]
            if gl is None:
                gl_str = None
            elif float(gl) == int(gl):
                gl_str = str(int(gl))
            else:
                gl_str = str(gl)
            incomplete = bool(r["jc_1x2_incomplete"]) if "jc_1x2_incomplete" in hhad_cols and r["jc_1x2_incomplete"] is not None else False
            incomplete = incomplete or _jc_inc(r["home_odds"], r["draw_odds"], r["away_odds"])
            entry = {
                "goal_line": gl_str, "home": r["home_odds"], "draw": r["draw_odds"], "away": r["away_odds"],
                "complete": not incomplete, "jc_1x2_incomplete": incomplete,
                "note": "current_main_line_only; table/matches uses decision-time select_hhad_at_decision",
            }
            if "line_rev" in hhad_cols:
                entry["line_rev"] = r["line_rev"]
            if "source" in hhad_cols:
                entry["source"] = r["source"]
            if "captured_at" in hhad_cols:
                entry["captured_at"] = r["captured_at"]
            jc_hhad[r["phase"]] = entry
    return {
        "jc_home_win": jc_home or None,
        "jc_had": jc_had or None,
        "jc_hhad": jc_hhad or None,
    }


def build_odds(conn: sqlite3.Connection, match_pk: int, *, raw_only: bool = False) -> dict:
    """Prefer odds_raw for JSON v2 shape；v1.7 起 crown/william 水位用 odds_asian 中点水位覆盖（raw_only=True 取原样）。"""
    raw = conn.execute(
        "SELECT odds_json FROM odds_raw WHERE match_id = ?", (match_pk,)
    ).fetchone()
    if raw and raw["odds_json"]:
        odds = json.loads(raw["odds_json"])
        if raw_only:
            return odds
        odds = _overlay_asian_water(conn, match_pk, odds)
        # 0.3.21：竞彩完整盘／incomplete 标记覆盖 raw JSON（不改库内 odds_raw）
        odds.update(_load_jc_odds_blocks(conn, match_pk))
        return odds

    asian: dict[str, dict] = {}
    for r in conn.execute(
        "SELECT book, phase, handicap, home_water, away_water FROM odds_asian WHERE match_id = ?",
        (match_pk,),
    ):
        book = r["book"]
        asian.setdefault(book, {})
        if r["home_water"] is None and r["away_water"] is None and r["handicap"] is not None:
            asian[book][r["phase"]] = r["handicap"]
        else:
            asian[book][r["phase"]] = {
                "home_water": r["home_water"],
                "handicap": r["handicap"],
                "away_water": r["away_water"],
            }

    euro: dict[str, dict] = {}
    for r in conn.execute(
        "SELECT book, phase, home_win FROM odds_euro_home WHERE match_id = ?",
        (match_pk,),
    ):
        euro.setdefault(r["book"], {})[r["phase"]] = r["home_win"]

    out = {"asian": asian, "euro_home_win": euro}
    out.update(_load_jc_odds_blocks(conn, match_pk))
    return out


def build_meta(conn: sqlite3.Connection, match_pk: int) -> dict:
    m = conn.execute("SELECT * FROM match_meta WHERE match_id = ?", (match_pk,)).fetchone()
    if not m:
        return {}
    extras = loads_json(m["extras_json"]) or {}
    out: dict[str, Any] = {
        "source_file": m["source_file"],
        "month": m["month"],
    }
    if extras.get("schema_version") is not None:
        out["schema_version"] = extras["schema_version"]
    if extras.get("pipeline") is not None:
        out["pipeline"] = extras["pipeline"]
    if extras.get("note") is not None:
        out["note"] = extras["note"]
    return out


def meta_extras(conn: sqlite3.Connection, match_pk: int) -> dict:
    m = conn.execute(
        "SELECT extras_json FROM match_meta WHERE match_id = ?", (match_pk,)
    ).fetchone()
    if not m:
        return {}
    return loads_json(m["extras_json"]) or {}


def as_units(v: Any) -> int | None:
    """「份」统一输出为 int（legs.stake 列是 REAL）。"""
    if v is None:
        return None
    return int(round(float(v)))


def build_prediction_from_row(pred: sqlite3.Row) -> dict:
    from app.ledger_registry import ledger_note_for
    from app.leak_suspect import leak_suspect_for
    note, note_reason = ledger_note_for(pred["strategy"], pred["rationale_json"])
    return {
        "leak_suspect": leak_suspect_for(pred["strategy"]),  # 0.3.20：config/leak_suspect.json 叠加（不写库）
        "ledger_note": note,  # 0.3.19：旧冻结 S2/N4 条目「触发依据是换算水位」（读出时加，不改库）
        "ledger_note_reason": note_reason,
        "direction": pred["direction"],
        "strategy": pred["strategy"],
        "settle_book": pred["settle_book"],
        "rationale": loads_json(pred["rationale_json"]) or [],
        "confidence": pred["confidence"],
        "stake": as_units(row_get(pred, "stake")),
        "stake_rule": row_get(pred, "stake_rule"),
        "produced_at": row_get(pred, "produced_at"),
        "updated_at": row_get(pred, "updated_at"),
        "message_sent_at": row_get(pred, "message_sent_at"),
    }


def build_leg_from_row(leg: sqlite3.Row) -> dict:
    return {
        "id": leg["id"],
        "market": leg["market"],
        "side": leg["side"],
        "line": leg["line"],
        "line_text": leg["line_text"],
        "stake": as_units(leg["stake"]),
        "stake_rule": row_get(leg, "stake_rule"),
        "p_used": row_get(leg, "p_used"),
        "f_star": row_get(leg, "f_star"),
        "status": row_get(leg, "status", "active"),
        "strategy": leg["strategy"],
        "settle_book": leg["settle_book"],
        "rationale": loads_json(leg["rationale_json"]) or [],
        "confidence": leg["confidence"],
        "gap": loads_json(leg["gap_json"]),
        "produced_at": leg["produced_at"],
        "updated_at": row_get(leg, "updated_at"),
    }


def parse_iso_dt(value: str) -> datetime | None:
    """Parse ISO-ish datetime; assume +08:00 if naive."""
    if not value:
        return None
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                dt = datetime.strptime(value.strip(), fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ_CN)
    return dt.astimezone(TZ_CN)


def resolve_kickoff_dt(row: sqlite3.Row, extras: dict) -> datetime | None:
    """Prefer meta.kickoff_at; else synthesize from jingcai_date + kickoff_hour (+08:00).

    竞彩日规则（对齐仓库 私有仓 PR / 早场特殊带）：
    - 优先已有 kickoff_at（meta 或 matches 列），勿用 hour 猜自然日
    - 仅整点合成：早场特殊带 0<=h<=11 → 自然日 = jingcai_date + 1；
      12:00 起 → 自然日 = jingcai_date（11:31–11:59 需分钟才能排除，见 is_early_kickoff_band）
    """
    # 优先级：meta.extras.kickoff_at → matches.kickoff_at（v1.2 列）→ jingcai_date+kickoff_hour 合成
    for raw in (extras.get("kickoff_at"), row_get(row, "kickoff_at")):
        if isinstance(raw, str) and raw:
            parsed = parse_iso_dt(raw)
            if parsed is not None:
                return parsed

    date_s = row["jingcai_date"]
    hour = row["kickoff_hour"]
    if not date_s or hour is None:
        return None
    try:
        base = datetime.strptime(date_s, "%Y-%m-%d").replace(tzinfo=TZ_CN)
    except ValueError:
        return None
    h = int(hour)
    if h < 0 or h > 23:
        return None
    # 早场特殊带（仅整点回退）：次日自然日；≥23 仍属同竞彩日晚场轴上的固定钟点，合成不 +1
    if collection_schedule.is_early_kickoff_band(h):
        base = base + timedelta(days=1)
    return base.replace(hour=h, minute=0, second=0, microsecond=0)


def calc_collection_times(
    kickoff_hour: int, minute: int | None = None
) -> dict[str, str]:
    """日用 rule：≥23:00 或早场特殊带 [00:00,11:30]（仅整点 0–11）→ 15:00/22:00；白天 → T−8h/T−1h。

    旧早场→16:00/23:00 见 collection_schedule.calc_collection_times_legacy（对照，不回写）。
    """
    return collection_schedule.calc_collection_times(kickoff_hour, minute)


def resolve_collection_ats(
    jingcai_date: str, kickoff_hour: int, minute: int | None = None
) -> tuple[datetime, datetime] | None:
    """rule 通道绝对 mid/close（+08:00）。"""
    return collection_schedule.resolve_collection_ats(jingcai_date, kickoff_hour, minute)


def is_pending_for_collect(
    now: datetime,
    collect_at: datetime,
    kickoff: datetime,
    *,
    include_overdue: bool = False,
) -> bool:
    """主列表：仅 collect_at ±30min。补发兜底：collect_at≤now≤kickoff（需显式打开）。"""
    delta_min = abs((now - collect_at).total_seconds()) / 60.0
    if delta_min <= PENDING_ABS_MIN:
        return True
    if include_overdue and collect_at <= now <= kickoff:
        return True
    return False


@app.get("/health")
def health() -> dict:
    import app.db as _adb
    return {"ok": True, "version": API_VERSION,
            "dual_write_odds_asian": DUAL_WRITE_ODDS_ASIAN,
            "meta": _adb.db_meta()}


@app.get("/matches")
def list_matches(
    date: str = Query(..., description="竞彩日 YYYY-MM-DD"),
    scope: str = Query("jingcai", pattern="^(jingcai|extra|all)$"),
) -> dict:
    conn = get_conn()
    try:
        if scope == "all":
            rows = conn.execute(
                """
                SELECT m.*,
                  (SELECT direction FROM predictions p
                   WHERE p.match_id = m.id AND p.strategy = ?
                   LIMIT 1) AS direction,
                  (SELECT COUNT(1) FROM predictions p WHERE p.match_id = m.id
                   AND p.strategy NOT IN (SELECT strategy_key FROM strategy_defs
                       WHERE json_extract(config_json, '$.extras.test_only') = 1)) AS pred_count
                FROM matches m
                WHERE m.jingcai_date = ?
                ORDER BY m.jingcai_date,
                         COALESCE(m.kickoff_hour, 99),
                         COALESCE(m.jc_no, 999999)
                """,
                (DEFAULT_STRATEGY, date),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT m.*,
                  (SELECT direction FROM predictions p
                   WHERE p.match_id = m.id AND p.strategy = ?
                   LIMIT 1) AS direction,
                  (SELECT COUNT(1) FROM predictions p WHERE p.match_id = m.id
                   AND p.strategy NOT IN (SELECT strategy_key FROM strategy_defs
                       WHERE json_extract(config_json, '$.extras.test_only') = 1)) AS pred_count
                FROM matches m
                WHERE m.jingcai_date = ? AND m.scope = ?
                ORDER BY m.jingcai_date,
                         COALESCE(m.kickoff_hour, 99),
                         COALESCE(m.jc_no, 999999)
                """,
                (DEFAULT_STRATEGY, date, scope),
            ).fetchall()

        items = []
        for row in rows:
            extras = meta_extras(conn, row["id"])
            items.append(
                {
                    "id": row["match_uid"],
                    "match": build_match_obj(row, extras),
                    "has_prediction": bool(row["pred_count"]),
                    "direction": row["direction"],
                    "result": build_result(conn, row["id"]),
                }
            )
        return {"date": date, "scope": scope, "items": items}
    finally:
        conn.close()


@app.get("/matches/{match_id}")
def get_match(match_id: str) -> dict:
    conn = get_conn()
    try:
        row = resolve_match_row(conn, match_id)
        extras = meta_extras(conn, row["id"])
        return {
            "match": build_match_obj(row, extras),
            "result": build_result(conn, row["id"]),
            "stats": build_stats(conn, row["id"]),
            "odds": build_odds(conn, row["id"]),
            "meta": build_meta(conn, row["id"]),
        }
    finally:
        conn.close()


@app.get("/matches/{match_id}/odds")
def get_match_odds(
    match_id: str,
    raw: bool = Query(False, description="true=原始导入 JSON（crown/william 为档位）；默认用中点/真实水位"),
) -> dict:
    conn = get_conn()
    try:
        row = resolve_match_row(conn, match_id)
        return build_odds(conn, row["id"], raw_only=raw)
    finally:
        conn.close()


@app.get("/matches/{match_id}/odds/timeline")
def get_match_odds_timeline(
    match_id: str,
    book: str | None = Query(None, description="默认 macauslot；亦接受 macau→macauslot、william→williamhill"),
    market: str | None = Query(None, description="默认 asian；允许 asian／1x2／goalline"),
    fixture_id: str | None = Query(None, description="可选：直接指定 5DollarFootballAPI 对阵编号"),
    as_of: str | None = Query(None, description="ISO-8601；只返回 recorded_at≤as_of 的赛前 tick"),
    cache_ttl_sec: int | None = Query(None, ge=60, le=900, description="调试用缓存生存秒数；服务端有上限"),
    force_refresh: bool = Query(False, description="true 时绕过短时缓存；忙则仍 503"),
) -> dict:
    """0.3.23：赛前盘口／水位折线（点开才拉；不写主表）。契约 v2_0-prematch-odds-timeline-chart.md"""
    from fastapi.responses import JSONResponse
    conn = get_conn()
    try:
        row = None
        extras = {}
        if match_id != "by-fixture":
            row = resolve_match_row(conn, match_id)
            extras = meta_extras(conn, row["id"])
        try:
            return otc.get_timeline(
                conn,
                match_id_path=match_id,
                row=row,
                extras=extras,
                book=book,
                market=market,
                fixture_id=fixture_id,
                as_of=as_of,
                cache_ttl_sec=cache_ttl_sec,
                force_refresh=force_refresh,
            )
        except otc.TimelineError as e:
            return JSONResponse(status_code=e.status, content=e.body())
    finally:
        conn.close()



@app.get("/matches/{match_id}/prediction")
def get_match_prediction(
    match_id: str,
    strategy: str | None = Query(None),
) -> dict:
    conn = get_conn()
    try:
        row = resolve_match_row(conn, match_id)
        strat = strategy or DEFAULT_STRATEGY
        pred = conn.execute(
            """
            SELECT *
            FROM predictions
            WHERE match_id = ? AND strategy = ?
            """,
            (row["id"], strat),
        ).fetchone()
        if not pred:
            raise HTTPException(status_code=404, detail="prediction not found")
        return build_prediction_from_row(pred)
    finally:
        conn.close()


@app.get("/matches/{match_id}/predictions")
def get_match_predictions(
    match_id: str,
    strategy: str | None = Query(
        None, description="AH 默认策略；缺省 CFFXDJ_5_V3。legs 返回该场全部腿"
    ),
) -> dict:
    """AH 旧表 + 多玩法 legs。无 AH 时 ah=null，不 404。"""
    conn = get_conn()
    try:
        row = resolve_match_row(conn, match_id)
        strat = strategy or DEFAULT_STRATEGY
        pred = conn.execute(
            """
            SELECT *
            FROM predictions
            WHERE match_id = ? AND strategy = ?
            """,
            (row["id"], strat),
        ).fetchone()
        legs = conn.execute(
            """
            SELECT *
            FROM prediction_legs
            WHERE match_id = ?
            ORDER BY market, COALESCE(strategy, ''), id
            """,
            (row["id"],),
        ).fetchall()
        return {
            "ah": build_prediction_from_row(pred) if pred else None,
            "legs": [build_leg_from_row(leg) for leg in legs],
        }
    finally:
        conn.close()


@app.get("/dispatch/pending")
def dispatch_pending(
    window: str = Query(
        "close",
        pattern="^(close|mid)$",
        description="close=临盘采集点 | mid=中盘采集点（日用 rule：≥23:00/早场[00:00,11:30]→15:00/22:00）",
    ),
    scope: str = Query("jingcai", pattern="^(jingcai|extra|all)$"),
    include_overdue: bool = Query(
        False,
        description="False=主列表仅 collect_at±30min；True=额外纳入 collect_at≤now≤kickoff 补发兜底",
    ),
) -> dict:
    """待发场次：按日用 rule 采集点筛选（早场带对齐 私有仓 PR）。

    公式（+08:00，时钟落在 jingcai_date 当日）：
    - hour ≥23 或早场特殊带 [00:00,11:30]（仅整点 0–11；11:31–11:59 需分钟）：mid=15:00，close=22:00（rule）
    - 12:00–22:xx：mid=kickoff−8h，close=kickoff−1h
    - rule_legacy（仅早场特殊带）：mid=16:00，close=23:00 — 响应对照字段，不用于筛选、不回写旧数据
    kickoff 自然日：优先 kickoff_at；仅整点合成时早场带 → jingcai_date+1，否则 = jingcai_date。

    主列表：abs(now−collect_at)≤30min。
    补发兜底（include_overdue=true）：collect_at≤now≤kickoff。
    """
    now = datetime.now(TZ_CN)
    conn = get_conn()
    try:
        if scope == "all":
            rows = conn.execute("SELECT * FROM matches").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM matches WHERE scope = ?", (scope,)
            ).fetchall()

        items = []
        for row in rows:
            extras = meta_extras(conn, row["id"])
            kick = resolve_kickoff_dt(row, extras)
            if kick is None or row["kickoff_hour"] is None:
                continue
            # 有 kickoff_at 用分钟；仅 hour 合成则 minute=None（整点回退 0–11）
            has_ka = bool(
                (isinstance(extras.get("kickoff_at"), str) and extras.get("kickoff_at"))
                or row_get(row, "kickoff_at")
            )
            minute = int(kick.minute) if has_ka else None
            # 0.3.18：例外场按竞彩编号判（exception_rule=jc_code_ge_2300）；非例外 = T−8h/T−1h（分钟精确）
            coll = collection_schedule.rule_targets(
                row["jingcai_date"], row["kickoff_hour"], kick, has_ka,
                has_jc_code=bool(row_get(row, "jc_id")),
            )
            if coll is None:
                continue
            mid_at, close_at = coll
            collect_at = close_at if window == "close" else mid_at
            if not is_pending_for_collect(
                now, collect_at, kick, include_overdue=include_overdue
            ):
                continue

            minutes = (kick - now).total_seconds() / 60.0
            minutes_to_collect = (collect_at - now).total_seconds() / 60.0
            times = {"mid_time": mid_at.strftime("%H:%M"), "final_time": close_at.strftime("%H:%M")}
            legacy_times = collection_schedule.calc_collection_times_legacy(
                int(row["kickoff_hour"]), minute
            )
            legacy_ats = collection_schedule.resolve_collection_ats_legacy(
                row["jingcai_date"], int(row["kickoff_hour"]), minute
            )
            leg_count = conn.execute(
                "SELECT COUNT(1) AS c FROM prediction_legs WHERE match_id = ?",
                (row["id"],),
            ).fetchone()["c"]
            items.append(
                {
                    "id": row["match_uid"],
                    "match": build_match_obj(row, extras),
                    "kickoff_at_resolved": kick.isoformat(),
                    "mid_collect_at": mid_at.isoformat(),
                    "close_collect_at": close_at.isoformat(),
                    "collect_at": collect_at.isoformat(),
                    "collection_schedule": times,
                    "collection_schedule_rule_legacy": legacy_times,
                    "rule_legacy_mid_collect_at": (
                        legacy_ats[0].isoformat() if legacy_ats else None
                    ),
                    "rule_legacy_close_collect_at": (
                        legacy_ats[1].isoformat() if legacy_ats else None
                    ),
                    "minutes_to_kick": round(minutes, 1),
                    "minutes_to_collect": round(minutes_to_collect, 1),
                    "has_legs": bool(leg_count),
                    "leg_count": int(leg_count),
                }
            )

        items.sort(key=lambda x: (x["minutes_to_collect"], x["minutes_to_kick"]))
        return {
            "window": window,
            "scope": scope,
            "include_overdue": include_overdue,
            "now": now.isoformat(),
            "approximation": {
                "source": "app.collection_schedule (v2.0 rule; 0.3.9)",
                "channel_default": "rule",
                "overnight_or_late_hours": "0-10|23",
                "rule_overnight_mid": "jingcai_date 15:00",
                "rule_overnight_close": "jingcai_date 22:00",
                "rule_legacy_overnight_mid": "jingcai_date 16:00 (对照，不筛选)",
                "rule_legacy_overnight_close": "jingcai_date 23:00 (对照，不筛选)",
                "day_mid": "kickoff - 8h (clock on jingcai_date)",
                "day_close": "kickoff - 1h (clock on jingcai_date)",
                "kickoff_hour_0_10": "next_calendar_day_after_jingcai_date",
                "pending_rule_primary": f"abs(now-collect_at)<={PENDING_ABS_MIN}min",
                "pending_rule_overdue": "collect_at<=now<=kickoff (only if include_overdue=true)",
                "note": "日用筛选用 rule；rule_legacy_* 仅对照；不回写 odds_asian 旧 mid/close",
            },
            "items": items,
        }
    finally:
        conn.close()


# ===================== v1.2 · bankroll（注额计算器） =====================
# 语义：预测行只存整数「份」（可空）；金额只在 /bankroll/calc 里算，不入库。
# 1 份 = 基数本金 × unit_fraction（默认 monday_base_bankroll × 0.005）。
# 上限：单场 ≤ per_match（3）份、单竞彩日 ≤ per_day（10）份。
#   mode=clamp（默认）：超限先截断到单场上限，再按单场/单日总量等比例缩放后向下取整，
#                       并在条目上标 capped=true + cap_reasons；绝不静默改数。
#   mode=warn：不改份数，只在 warnings 里报超限。
# 金额上下限（拍板 2026-10-06，stake_amount_limits）：
#   单注金额 < min_stake_amount(50) → 抬到 50，标 raised；
#   单注金额 > 当前剩余资金 × max_stake_pct_of_remaining(0.5) → 截到上限，标 capped_amount；
#   抬到 50 又超过上限 → 该注不下（amount=0, skipped_below_min）。
#   「当前剩余资金」= 请求 remaining_bankroll → 最新 snapshot balance → 基数；
#   同一请求内按条目顺序逐注扣减（后一注看前面已分配后的剩余）。
# 基数缺省链：base_key 配置 → （未显式指定 base_key 时）stats_initial_bankroll；都没有才 422。

BANKROLL_AMOUNT_KEYS = {"stats_initial_bankroll", "monday_base_bankroll", "calculator_bankroll"}
BANKROLL_KNOWN_KEYS = BANKROLL_AMOUNT_KEYS | {"unit_definition", "stake_caps", "stake_amount_limits"}
DEFAULT_UNIT = {"unit_fraction": 0.005, "base_key": "monday_base_bankroll"}
DEFAULT_CAPS = {"per_match": 3, "per_day": 10, "mode": "clamp"}
DEFAULT_AMOUNT_LIMITS = {"min_stake_amount": 50, "max_stake_pct_of_remaining": 0.5}


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def load_bankroll_config(conn: sqlite3.Connection) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in conn.execute("SELECT key, value_json, updated_at, note FROM bankroll_config ORDER BY key"):
        out[r["key"]] = {
            "value": loads_json(r["value_json"]),
            "updated_at": r["updated_at"],
            "note": r["note"],
        }
    return out


def validate_config_value(key: str, value: dict) -> None:
    if key in BANKROLL_AMOUNT_KEYS:
        amt = value.get("amount")
        if amt is not None and (not _is_number(amt) or amt < 0):
            raise HTTPException(422, f"{key}.amount 必须为 null 或 ≥0 的数字")
    elif key == "unit_definition":
        uf = value.get("unit_fraction")
        if not _is_number(uf) or not (0 < uf <= 0.1):
            raise HTTPException(422, "unit_definition.unit_fraction 必须在 (0, 0.1]")
        bk = value.get("base_key")
        if bk is not None and bk not in BANKROLL_AMOUNT_KEYS:
            raise HTTPException(422, f"unit_definition.base_key 必须是 {sorted(BANKROLL_AMOUNT_KEYS)} 之一")
    elif key == "stake_caps":
        for k in ("per_match", "per_day"):
            v = value.get(k)
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                raise HTTPException(422, f"stake_caps.{k} 必须为 ≥0 的整数")
        if value.get("mode", "clamp") not in ("clamp", "warn"):
            raise HTTPException(422, "stake_caps.mode 必须为 clamp | warn")
    elif key == "stake_amount_limits":
        mn = value.get("min_stake_amount")
        if mn is not None and (not _is_number(mn) or mn < 0):
            raise HTTPException(422, "stake_amount_limits.min_stake_amount 必须为 null 或 ≥0")
        pct = value.get("max_stake_pct_of_remaining")
        if pct is not None and (not _is_number(pct) or not (0 < pct <= 1)):
            raise HTTPException(422, "stake_amount_limits.max_stake_pct_of_remaining 必须在 (0, 1]")


def latest_snapshot(conn: sqlite3.Connection) -> dict | None:
    r = conn.execute(
        "SELECT * FROM bankroll_snapshots ORDER BY reported_at DESC, id DESC LIMIT 1"
    ).fetchone()
    return dict(r) if r else None


class ConfigPut(BaseModel):
    value: dict[str, Any] = Field(..., description="整键替换（非合并）")
    note: str | None = None


class SnapshotIn(BaseModel):
    balance: float = Field(..., ge=0)
    reported_at: str | None = Field(None, description="ISO-8601；缺省=now(+08:00)；无时区按 +08:00")
    source: str = "user_report"
    note: str | None = None
    set_as_monday_base: bool = Field(False, description="True 时同步写 monday_base_bankroll.amount")


class CalcItem(BaseModel):
    match_id: str | None = Field(None, description="match_uid 或内部 id；用于查份数与竞彩日")
    strategy: str | None = Field(None, description=f"缺省 {DEFAULT_STRATEGY}")
    stake_units: int | None = Field(None, ge=0, description="显式份数；缺省则取 predictions.stake")
    jingcai_date: str | None = Field(None, description="无 match_id 时用于单日分组")
    label: str | None = None


class CalcRequest(BaseModel):
    bankroll: float | None = Field(None, gt=0, description="显式基数本金；缺省用 base_key 配置")
    remaining_bankroll: float | None = Field(
        None, ge=0, description="当前剩余资金（单注 ≤ 其 50%）；缺省=最新 snapshot balance，再缺省=基数"
    )
    base_key: str | None = Field(None, description="缺省 unit_definition.base_key（monday_base_bankroll）")
    cap_mode: Literal["clamp", "warn"] | None = Field(None, description="缺省 stake_caps.mode")
    items: list[CalcItem] = Field(default_factory=list)


@app.get("/bankroll/config")
def get_bankroll_config() -> dict:
    conn = get_conn()
    try:
        cfg = load_bankroll_config(conn)
        todo = [
            k for k in sorted(BANKROLL_AMOUNT_KEYS)
            if k in cfg and isinstance(cfg[k]["value"], dict) and cfg[k]["value"].get("amount") is None
        ]
        return {"items": cfg, "amount_todo": todo, "latest_snapshot": latest_snapshot(conn)}
    finally:
        conn.close()


@app.put("/bankroll/config/{key}")
def put_bankroll_config(
    body: ConfigPut,
    key: str = PathParam(..., pattern=r"^[a-z][a-z0-9_]{1,63}$"),
) -> dict:
    if key not in BANKROLL_KNOWN_KEYS:
        raise HTTPException(404, f"unknown config key; known: {sorted(BANKROLL_KNOWN_KEYS)}")
    validate_config_value(key, body.value)
    conn = get_conn()
    try:
        conn.execute(
            """
            INSERT INTO bankroll_config (key, value_json, updated_at, note)
            VALUES (?, ?, datetime('now'), ?)
            ON CONFLICT(key) DO UPDATE SET
              value_json = excluded.value_json,
              updated_at = excluded.updated_at,
              note = COALESCE(excluded.note, bankroll_config.note)
            """,
            (key, json.dumps(body.value, ensure_ascii=False), body.note),
        )
        conn.commit()
        return {"key": key, **load_bankroll_config(conn)[key]}
    finally:
        conn.close()


@app.post("/bankroll/snapshots", status_code=201)
def post_bankroll_snapshot(body: SnapshotIn) -> dict:
    if body.reported_at:
        dt = parse_iso_dt(body.reported_at)
        if dt is None:
            raise HTTPException(422, "reported_at 不是合法 ISO-8601")
    else:
        dt = datetime.now(TZ_CN)
    reported_at = dt.replace(microsecond=0).isoformat()
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO bankroll_snapshots (reported_at, balance, source, note) VALUES (?, ?, ?, ?)",
            (reported_at, body.balance, body.source, body.note),
        )
        snap_id = cur.lastrowid
        monday = None
        if body.set_as_monday_base:
            val = {"amount": body.balance, "as_of": reported_at, "snapshot_id": snap_id}
            conn.execute(
                """
                INSERT INTO bankroll_config (key, value_json, updated_at)
                VALUES ('monday_base_bankroll', ?, datetime('now'))
                ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json,
                                               updated_at = excluded.updated_at
                """,
                (json.dumps(val),),
            )
            monday = val
        conn.commit()
        snap = dict(conn.execute("SELECT * FROM bankroll_snapshots WHERE id = ?", (snap_id,)).fetchone())
        return {"snapshot": snap, "monday_base_bankroll": monday}
    finally:
        conn.close()


def _scale_group(entries: list[dict], cap: int, reason: str) -> None:
    """组内份数和 > cap 时等比例缩放并向下取整（保证 ≤ cap），标记 capped。"""
    total = sum(e["stake_units"] for e in entries if e["stake_units"])
    if total <= cap:
        return
    for e in entries:
        u = e["stake_units"]
        if not u:
            continue
        new = math.floor(u * cap / total)
        if new != u:
            e["stake_units"] = new
            e["capped"] = True
            e["cap_reasons"].append(reason)


@app.post("/bankroll/calc")
def bankroll_calc(body: CalcRequest) -> dict:
    conn = get_conn()
    try:
        cfg = load_bankroll_config(conn)
        unit_def = {**DEFAULT_UNIT, **((cfg.get("unit_definition") or {}).get("value") or {})}
        caps = {**DEFAULT_CAPS, **((cfg.get("stake_caps") or {}).get("value") or {})}
        mode = body.cap_mode or caps.get("mode", "clamp")
        per_match, per_day = int(caps["per_match"]), int(caps["per_day"])
        unit_fraction = float(unit_def["unit_fraction"])

        base_key = body.base_key or unit_def.get("base_key") or "monday_base_bankroll"
        if body.base_key and body.base_key not in BANKROLL_AMOUNT_KEYS:
            raise HTTPException(422, f"base_key 必须是 {sorted(BANKROLL_AMOUNT_KEYS)} 之一")
        def _amt(k: str) -> float | None:
            a = ((cfg.get(k) or {}).get("value") or {}).get("amount")
            return float(a) if _is_number(a) and a > 0 else None

        if body.bankroll is not None:
            base, base_source = float(body.bankroll), "request"
        else:
            chain = [base_key] if body.base_key else [base_key, "stats_initial_bankroll"]
            base, base_source = None, None
            for k in dict.fromkeys(chain):
                if _amt(k) is not None:
                    base, base_source = _amt(k), f"config:{k}"
                    break
            if base is None:
                raise HTTPException(
                    422,
                    f"{'/'.join(dict.fromkeys(chain))}.amount 均未配置；请在请求里传 bankroll，"
                    f"或先 PUT /bankroll/config/{base_key}",
                )
        unit_amount = round(base * unit_fraction, 2)

        lim = {**DEFAULT_AMOUNT_LIMITS, **((cfg.get("stake_amount_limits") or {}).get("value") or {})}
        min_amt = float(lim["min_stake_amount"]) if _is_number(lim.get("min_stake_amount")) else None
        max_pct = float(lim["max_stake_pct_of_remaining"]) if _is_number(lim.get("max_stake_pct_of_remaining")) else None
        if body.remaining_bankroll is not None:
            remaining, remaining_source = float(body.remaining_bankroll), "request"
        else:
            snap = latest_snapshot(conn)
            if snap is not None:
                remaining, remaining_source = float(snap["balance"]), f"snapshot:{snap['id']}"
            else:
                remaining, remaining_source = base, "base_bankroll"
        remaining_start = remaining

        warnings: list[str] = []
        entries: list[dict] = []
        for idx, it in enumerate(body.items):
            strat = it.strategy or DEFAULT_STRATEGY
            e: dict[str, Any] = {
                "index": idx,
                "label": it.label,
                "match_id": it.match_id,
                "strategy": strat,
                "jingcai_date": it.jingcai_date,
                "stake_source": "request" if it.stake_units is not None else "none",
                "stake_units_requested": it.stake_units,
                "capped": False,
                "cap_reasons": [],
                "error": None,
                "_match_pk": None,
            }
            if it.match_id:
                try:
                    row = resolve_match_row(conn, it.match_id)
                except HTTPException:
                    e["error"] = "match not found"
                    row = None
                if row is not None:
                    e["match_id"] = row["match_uid"]
                    e["_match_pk"] = row["id"]
                    e["jingcai_date"] = row["jingcai_date"]
                    if it.stake_units is None:
                        pred = conn.execute(
                            "SELECT * FROM predictions WHERE match_id = ? AND strategy = ?",
                            (row["id"], strat),
                        ).fetchone()
                        if pred is None:
                            e["stake_source"] = "none"
                            e["error"] = "prediction not found"
                        else:
                            e["stake_source"] = "prediction"
                            e["stake_units_requested"] = as_units(row_get(pred, "stake"))
            e["stake_units"] = e["stake_units_requested"]
            entries.append(e)

        # 1) 单条截断到单场上限
        for e in entries:
            u = e["stake_units"]
            if u is not None and u > per_match:
                if mode == "clamp":
                    e["stake_units"] = per_match
                    e["capped"] = True
                    e["cap_reasons"].append("per_match")
                else:
                    warnings.append(f"item[{e['index']}] {u} 份 > 单场上限 {per_match}")

        # 2) 同一场多条（多策略）合计 ≤ 单场上限
        by_match: dict[Any, list[dict]] = {}
        for e in entries:
            if e["_match_pk"] is not None:
                by_match.setdefault(e["_match_pk"], []).append(e)
        for pk, group in by_match.items():
            if len(group) > 1:
                tot = sum(x["stake_units"] or 0 for x in group)
                if tot > per_match:
                    if mode == "clamp":
                        _scale_group(group, per_match, "per_match_total")
                    else:
                        warnings.append(f"match {group[0]['match_id']} 合计 {tot} 份 > 单场上限 {per_match}")

        # 3) 单竞彩日合计 ≤ 单日上限
        by_day: dict[str, list[dict]] = {}
        for e in entries:
            by_day.setdefault(e["jingcai_date"] or "_undated", []).append(e)
        day_summary: dict[str, dict] = {}
        for day, group in by_day.items():
            req_tot = sum(x["stake_units"] or 0 for x in group)
            if req_tot > per_day:
                if mode == "clamp":
                    _scale_group(group, per_day, "per_day")
                else:
                    warnings.append(f"{day} 合计 {req_tot} 份 > 单日上限 {per_day}")

        # 4) 金额：份 × unit_amount，再套单注下限 / 剩余资金比例上限（按条目顺序逐注扣减剩余）
        items_out = []
        for e in entries:
            e.pop("_match_pk", None)
            u = e["stake_units"]
            e["amount_from_units"] = round(u * unit_amount, 2) if u is not None else None
            e["raised"] = False
            e["capped_amount"] = False
            e["amount_limit_reasons"] = []
            e["remaining_before"] = round(remaining, 2)
            if not u:
                e["amount"] = e["amount_from_units"]  # null 或 0：不下注，不抬下限
                items_out.append(e)
                continue
            amt = e["amount_from_units"]
            max_amt = round(remaining * max_pct, 2) if max_pct is not None else None
            if min_amt is not None and amt < min_amt:
                amt = min_amt
                e["raised"] = True
                e["amount_limit_reasons"].append("min_stake_raised")
            if max_amt is not None and amt > max_amt:
                if mode == "clamp":
                    amt = max_amt
                    e["capped_amount"] = True
                    e["amount_limit_reasons"].append("max_pct_of_remaining")
                else:
                    warnings.append(f"item[{e['index']}] {amt} > 剩余资金×{max_pct} = {max_amt}")
            if mode == "clamp" and min_amt is not None and amt < min_amt:
                # 上限低于下限：无法合规下注
                amt = 0.0
                e["amount_limit_reasons"].append("skipped_below_min")
            e["amount"] = round(amt, 2)
            remaining = max(0.0, remaining - e["amount"])
            items_out.append(e)

        for day, group in by_day.items():
            units = sum(x["stake_units"] or 0 for x in group)
            day_summary[day] = {
                "units_requested": sum(x["stake_units_requested"] or 0 for x in group),
                "units": units,
                "amount": round(sum(x["amount"] or 0 for x in group), 2),
                "capped": any(x["capped"] or x["capped_amount"] for x in group),
                "raised": any(x["raised"] for x in group),
            }

        total_units = sum(e["stake_units"] or 0 for e in entries)
        return {
            "base_bankroll": base,
            "base_source": base_source,
            "unit_fraction": unit_fraction,
            "unit_amount": unit_amount,
            "caps": {"per_match": per_match, "per_day": per_day, "mode": mode},
            "amount_limits": {
                "min_stake_amount": min_amt,
                "max_stake_pct_of_remaining": max_pct,
                "currency": lim.get("currency"),
            },
            "remaining_bankroll": {
                "start": round(remaining_start, 2),
                "source": remaining_source,
                "after": round(remaining, 2),
            },
            "capped": any(e["capped"] or e["capped_amount"] for e in entries),
            "raised": any(e["raised"] for e in entries),
            "totals": {
                "units": total_units,
                "amount": round(sum(e["amount"] or 0 for e in entries), 2),
                "by_day": day_summary,
            },
            "warnings": warnings,
            "items": items_out,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# v1.3 方案工场 + v1.4 M3 编排器 + v1.5 M4 validate 加厚 / compare
# 不改 CFFXDJ_5_V3 现网默认预测语义；精算可迭代，但 cache/summary 不再空占位
# ---------------------------------------------------------------------------

KNOWN_STRATEGY_CONFIG_KEYS = {
    "settle_book",
    "juice",
    "vote",
    "gates",
    "stake_rule",
    "state_machine",
    "feature_refs",
    "extras",
}
STATE_MACHINE_TEMPLATES = {None, "null", "simple_gate"}

STRATEGY_CONFIG_TEMPLATES: dict[str, dict[str, Any]] = {
    "default": {
        "settle_book": "macau_close",
        "juice": 0.95,
        "vote": {"members": [], "threshold": 3},
        "gates": [],
        "stake_rule": "fractional_quarter_kelly_units",
        "state_machine": None,
        "feature_refs": [],
        "extras": {},
    },
    "simple_gate": {
        "settle_book": "macau_close",
        "juice": 0.95,
        "vote": {"members": [], "threshold": 3},
        "gates": [],
        "stake_rule": "fractional_quarter_kelly_units",
        "state_machine": "simple_gate",
        "feature_refs": [],
        "extras": {},
    },
}


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_hex(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def normalize_strategy_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Keep known keys; unknown top-level keys → extras; state_machine named templates only."""
    src = dict(raw or {})
    extras: dict[str, Any] = {}
    if isinstance(src.get("extras"), dict):
        extras.update(src["extras"])
    out: dict[str, Any] = {}
    for k, v in src.items():
        if k == "extras":
            continue
        if k not in KNOWN_STRATEGY_CONFIG_KEYS:
            extras[k] = v
        else:
            out[k] = v
    sm = out.get("state_machine", None)
    if sm == "null":
        sm = None
    if sm is not None and sm not in ("simple_gate",):
        extras["state_machine_unknown"] = sm
        sm = None
    out["state_machine"] = sm
    out.setdefault("settle_book", "macau_close")
    out.setdefault("juice", 0.95)
    out.setdefault("vote", {"members": [], "threshold": 3})
    out.setdefault("gates", [])
    out.setdefault("stake_rule", "fractional_quarter_kelly_units")
    out.setdefault("feature_refs", [])
    out["extras"] = extras
    return out


def config_fingerprint(config: dict[str, Any]) -> str:
    return _sha256_hex(_canonical_json(normalize_strategy_config(config)))


def run_fingerprint(
    config_fp: str,
    scope: str,
    settle_book: str,
    params: dict[str, Any],
    *,
    strategy_def_id: int | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    """Fingerprint one validation run.

    Includes ``strategy_def_id`` so two defs that share the same config template
    do not collide (M4 compare / multi-strategy).
    """
    payload = {
        "config_fingerprint": config_fp,
        "scope": scope,
        "settle_book": settle_book,
        "params": params,
        "strategy_def_id": strategy_def_id,
    }
    if extra:
        # M5+：结算版本 / 资金规则输入纳入指纹，口径变更时旧 run 不被误用
        payload["extra"] = extra
    return _sha256_hex(_canonical_json(payload))


def _loads_obj(raw: str | None, default: Any = None) -> Any:
    if raw is None or raw == "":
        return default if default is not None else {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default if default is not None else {}


def _strategy_def_row(row: sqlite3.Row) -> dict:
    cfg = _loads_obj(row["config_json"], {})
    extras = cfg.get("extras") if isinstance(cfg, dict) else {}
    if not isinstance(extras, dict):
        extras = {}
    return {
        "id": row["id"],
        "strategy_key": row["strategy_key"],
        "version": row["version"],
        "display_name": row["display_name"],
        "markets": _loads_obj(row["markets_json"], ["ah"]),
        "config": cfg,
        "config_fingerprint": row["config_fingerprint"],
        "status": row["status"],
        "is_default": bool(row["is_default"]),
        "notes": row["notes"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        # 0.3.12：列表便于前端区分影子台账（不进日用）
        "test_only": bool(extras.get("test_only")),
        "ledger_id": extras.get("ledger_id"),
        "bucket": extras.get("bucket"),
        "shadow_stub": bool(extras.get("shadow_stub")),
        # 0.3.20：leak_suspect（config/leak_suspect.json 叠加；库里有 leak_suspect 列时配置优先、列值兜底）
        **_leak.fields_for(row["strategy_key"], row["leak_suspect"] if "leak_suspect" in row.keys() else None),
    }


def _attach_ledger_metrics(summary: dict[str, Any] | None) -> dict[str, Any] | None:
    """补齐影子台账三键（n_eligible/hits/coverage）；旧缓存 run 无字段时从 n/signal_count 推导。

    映射（回测语境，见 mutex-buckets-min-n.md §1）：
      n_eligible ≈ n / sample_count（有赛果+可结算盘口进入回测的样本底座）
      hits       ≈ signal_count（有方向 side 触发；「不下注/skip 方向」未进 signal）
      coverage   = hits/n_eligible；n_eligible==0 → null（不用 0 掩盖）
      multi_hit_count：本包恒 null（不落多影子 defs）
    不改结算数字，不进指纹。
    """
    if not isinstance(summary, dict):
        return summary
    n_el = summary.get("n_eligible")
    if n_el is None:
        n_el = summary.get("n")
        if n_el is None:
            n_el = summary.get("sample_count")
    hits = summary.get("hits")
    if hits is None:
        hits = summary.get("signal_count")
    try:
        n_el_i = int(n_el) if n_el is not None else 0
    except (TypeError, ValueError):
        n_el_i = 0
    try:
        hits_i = int(hits) if hits is not None else 0
    except (TypeError, ValueError):
        hits_i = 0
    summary["n_eligible"] = n_el_i
    summary["hits"] = hits_i
    summary["coverage"] = (hits_i / n_el_i) if n_el_i > 0 else None
    if "multi_hit_count" not in summary:
        summary["multi_hit_count"] = None
    # 不可评估（N5 口径卡 §6）：旧缓存 run 无此键时补 0——改动前库内没有任何 evaluable=false 预测
    if "n_not_evaluable" not in summary:
        summary.update(shadow_ev.empty_summary_fields())
    # 分账：旧缓存 run 无 by_odds_source / by_open_basis 时补（全部行 = unknown）
    shadow_ev.backfill_breakdowns(summary, finalize=_attach_ledger_metrics)
    summary.setdefault(
        "ledger_metrics_note",
        "n_eligible←n/sample_count；hits←signal_count（有方向触发）；"
        "coverage=hits/n_eligible（无 eligible→null）；multi_hit_count 本包恒 null",
    )
    return summary


def _validation_run_row(row: sqlite3.Row, *, reused: bool | None = None) -> dict:
    out = {
        "id": row["id"],
        "strategy_def_id": row["strategy_def_id"],
        "run_label": row["run_label"],
        "scope": row["scope"],
        "settle_book": row["settle_book"],
        "params": _loads_obj(row["params_json"], {}),
        "run_fingerprint": row["run_fingerprint"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "status": row["status"],
        "summary": _attach_ledger_metrics(_loads_obj(row["summary_json"], None)),
        "error_text": row["error_text"],
    }
    if reused is not None:
        out["reused"] = reused
    # 顶层回显 data_rev 等时 summary 已含三键
    return out


def _insert_strategy_def(
    conn: sqlite3.Connection,
    *,
    strategy_key: str,
    version: str,
    display_name: str | None,
    markets: list[str],
    config: dict[str, Any],
    status: str,
    is_default: bool,
    notes: str | None,
) -> sqlite3.Row:
    cfg = normalize_strategy_config(config)
    fp = config_fingerprint(cfg)
    try:
        cur = conn.execute(
            """
            INSERT INTO strategy_defs
              (strategy_key, version, display_name, markets_json, config_json,
               config_fingerprint, status, is_default, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                strategy_key,
                version,
                display_name,
                json.dumps(markets, ensure_ascii=False),
                json.dumps(cfg, ensure_ascii=False),
                fp,
                status,
                1 if is_default else 0,
                notes,
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(409, f"({strategy_key}, {version}) already exists")
    row = conn.execute(
        "SELECT * FROM strategy_defs WHERE id = ?", (cur.lastrowid,)
    ).fetchone()
    return row


class StrategyDefCreate(BaseModel):
    strategy_key: str
    version: str
    display_name: str | None = None
    markets: list[str] = Field(default_factory=lambda: ["ah"])
    config: dict[str, Any] = Field(default_factory=dict)
    status: Literal["draft", "active", "shadow", "archived"] = "draft"
    is_default: bool = False
    notes: str | None = None


class ComposeRequest(BaseModel):
    strategy_key: str
    version: str
    display_name: str | None = None
    markets: list[str] = Field(default_factory=lambda: ["ah"])
    template: str = "default"
    config_overrides: dict[str, Any] = Field(default_factory=dict)
    status: Literal["draft", "active", "shadow", "archived"] = "draft"
    is_default: bool = False
    notes: str | None = None



def _parse_settle_book(settle_book: str) -> tuple[str, str]:
    """macau_close → (macau, close). Unknown forms fall back to (macau, close)."""
    s = (settle_book or "macau_close").strip().lower()
    if "_" in s:
        book, phase = s.rsplit("_", 1)
        if book and phase:
            return book, phase
    return "macau", "close"


DEFAULT_STAKE_RULE = {"default_units": 1, "use_prediction_stake": True, "below_min": "raise"}


def _resolve_stake_rule(cfg: dict[str, Any], override: dict[str, Any] | None) -> dict[str, Any]:
    """unified → 请求给的规则；per_strategy → config.extras.stake_rule_params；缺省 DEFAULT_STAKE_RULE。"""
    if isinstance(override, dict):
        src = override
    else:
        ex = (cfg.get("extras") or {}).get("stake_rule_params")
        src = ex if isinstance(ex, dict) else {}
    rule = {**DEFAULT_STAKE_RULE, **{k: v for k, v in src.items() if v is not None}}
    if rule.get("below_min") not in ("skip", "raise"):
        rule["below_min"] = "raise"
    return rule


def _base_data_rev(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT MAX(id) AS m FROM import_batches").fetchone()
    if row and row["m"] is not None:
        return f"ib:{int(row['m'])}"
    parts = []
    for tbl, idcol in (("results", "match_id"), ("odds_asian", "id"), ("matches", "id")):
        r = conn.execute(f"SELECT COUNT(*) AS c, MAX({idcol}) AS m FROM {tbl}").fetchone()
        parts.append(f"{tbl}:{r['c']}:{r['m']}")
    return "h:" + _sha256_hex("|".join(parts))[:12]


def _predictions_sig(conn: sqlite3.Connection, strategy_key: str) -> str:
    """该方案 predictions 内容签名：行数 + sha256(match_id,direction,stake,settle_book 按 match_id 排序)[:12]。

    不含 id / produced_at，所以删后重插同内容（seed 脚本）签名不变；改别的方案不影响本方案。
    """
    rows = conn.execute(
        "SELECT match_id, direction, stake, settle_book FROM predictions"
        " WHERE strategy = ? ORDER BY match_id",
        (strategy_key,),
    ).fetchall()
    payload = _canonical_json([[r["match_id"], r["direction"], r["stake"], r["settle_book"]] for r in rows])
    return f"p:{len(rows)}:{_sha256_hex(payload)[:12]}"


def compute_data_rev(conn: sqlite3.Connection, strategy_key: str | None = None) -> tuple[str, str]:
    """自动 data_rev = <ib:最大批次 id | h:表计数哈希> + "|" + 该方案 predictions 签名。"""
    base = _base_data_rev(conn)
    src = "import_batches.max_id" if base.startswith("ib:") else "table_counts_hash"
    # 0.3.19：人工复核 / 推迟场状态变了要让缓存失效（现网无列 → 空 → data_rev 不变）
    sp = _match_special(conn)
    if sp:
        from app.collection_schedule import POSTPONE_VOID_HOURS as _pvh
        h = hashlib.sha256(json.dumps([sorted(sp.items()), _pvh], sort_keys=True, default=str)
                           .encode()).hexdigest()[:12]
        base, src = f"{base}|sp:{h}", f"{src}+match_special"
    if strategy_key:
        return f"{base}|{_predictions_sig(conn, strategy_key)}", f"{src}+predictions_sig"
    return base, src


def _resolve_data_rev(
    conn: sqlite3.Connection, params: dict[str, Any], strategy_key: str | None = None
) -> tuple[str, str]:
    v = params.get("data_rev")
    if v not in (None, ""):
        return str(v), "explicit"
    return compute_data_rev(conn, strategy_key)


def _backtest_rules(
    conn: sqlite3.Connection,
    stake_rule: dict[str, Any],
    *,
    settle_book: str = "macau_close",
    exclude_censored: bool = False,
    initial_override: Any = None,
) -> dict[str, Any]:
    """资金规则：本金=stats_initial_bankroll；份/上下限复用 bankroll_config（同 /bankroll/calc）。"""
    cfg = load_bankroll_config(conn)
    unit_def = {**DEFAULT_UNIT, **((cfg.get("unit_definition") or {}).get("value") or {})}
    caps = {**DEFAULT_CAPS, **((cfg.get("stake_caps") or {}).get("value") or {})}
    lim = {**DEFAULT_AMOUNT_LIMITS, **((cfg.get("stake_amount_limits") or {}).get("value") or {})}
    init = ((cfg.get("stats_initial_bankroll") or {}).get("value") or {}).get("amount")
    initial = float(init) if _is_number(init) and init > 0 else 10000.0
    initial_source = "config:stats_initial_bankroll" if _is_number(init) else "default:10000"
    if _is_number(initial_override) and initial_override > 0:
        initial, initial_source = float(initial_override), "params:initial_bankroll"
    frac = stake_rule.get("unit_fraction")
    book, _phase = _parse_settle_book(settle_book)
    return {
        "initial_bankroll": initial,
        "initial_source": initial_source,
        "book": book,
        "exclude_censored": bool(exclude_censored) and book != "macau",
        "unit_fraction": float(frac) if _is_number(frac) and 0 < frac <= 0.1 else float(unit_def["unit_fraction"]),
        "unit_base": "weekly_equity",
        "per_match": int(caps["per_match"]),
        "per_day": int(caps["per_day"]),
        "min_stake": float(lim["min_stake_amount"]) if _is_number(lim.get("min_stake_amount")) else None,
        "max_pct": float(lim["max_stake_pct_of_remaining"]) if _is_number(lim.get("max_stake_pct_of_remaining")) else None,
        "below_min": stake_rule.get("below_min", "raise"),
    }


def _match_special(conn: sqlite3.Connection) -> dict[int, dict[str, Any]]:
    """0.3.19：人工复核 / 推迟待核 / 推迟作废的场（列缺失 = 现网 → 空）。"""
    from app import collection_schedule as _cs
    cols = {r[1] for r in conn.execute("PRAGMA table_info(matches)")}
    if not ({"manual_review", "kickoff_original"} & cols):
        return {}
    sel = ", ".join(c if c in cols else f"NULL AS {c}" for c in
                    ("manual_review", "manual_review_reason", "kickoff_original", "kickoff_actual",
                     "kickoff_at", "postpone_void_check"))
    out: dict[int, dict[str, Any]] = {}
    for r in conn.execute(f"SELECT id, {sel} FROM matches WHERE manual_review=1 OR kickoff_original IS NOT NULL"
                          if {"manual_review", "kickoff_original"} <= cols else
                          f"SELECT id, {sel} FROM matches"):
        d: dict[str, Any] = {}
        if r["manual_review"]:
            d["manual_review"] = r["manual_review_reason"] or "unspecified"
        if r["kickoff_original"]:
            ko = datetime.fromisoformat(r["kickoff_original"])
            ka = datetime.fromisoformat(r["kickoff_actual"] or r["kickoff_at"])
            delay_h = (ka - ko).total_seconds() / 3600.0
            if _cs.POSTPONE_VOID_HOURS is not None and delay_h > float(_cs.POSTPONE_VOID_HOURS):
                d["postpone"] = shadow_ev.VOID_POSTPONED
            elif (r["postpone_void_check"] or _cs.postpone_void_check(ko, ka)) == "pending":
                d["postpone"] = shadow_ev.POSTPONE_PENDING
            if d.get("postpone"):
                d["delay_minutes"] = round(delay_h * 60, 1)
        if d:
            out[int(r["id"])] = d
    return out


def _load_strategy_bets(
    conn: sqlite3.Connection,
    *,
    strategy_key: str,
    settle_book: str,
    scope: str,
    stake_rule: dict[str, Any],
    fallback_juice: float,
    not_evaluable_out: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """not_evaluable_out：传入 shadow_ev.new_tally() 收集不可评估场（不进 bets/n_eligible，不进 skipped）。"""
    book, phase = _parse_settle_book(settle_book)
    _special = _match_special(conn)
    sql = """
        SELECT p.match_id, p.direction, p.stake, p.rationale_json,
               r.home_goals, r.away_goals,
               oa.handicap, oa.home_water, oa.away_water, oa.water_src,
               json_extract(oa.extras_json, '$.censored.home') AS censored_home,
               json_extract(oa.extras_json, '$.censored.away') AS censored_away,
               m.jingcai_date, m.jc_no, m.match_uid, m.jc_id, m.kickoff_at,
               COALESCE(th.name_zh_canonical, m.home_team) AS home_name,
               COALESCE(ta.name_zh_canonical, m.away_team) AS away_name
        FROM predictions p
        JOIN matches m ON m.id = p.match_id
        LEFT JOIN teams th ON th.id = m.home_team_id
        LEFT JOIN teams ta ON ta.id = m.away_team_id
        LEFT JOIN results r ON r.match_id = p.match_id
        LEFT JOIN odds_asian oa
          ON oa.match_id = p.match_id AND oa.book = ? AND oa.phase = ?
        WHERE p.strategy = ?
    """
    args: list[Any] = [book, phase, strategy_key]
    if scope and scope != "all":
        sql += " AND m.scope = ?"
        args.append(scope)
    sql += " ORDER BY m.jingcai_date, m.jc_no, p.match_id"
    bets: list[dict[str, Any]] = []
    skipped = {"no_result": 0, "no_odds": 0, "bad_direction": 0}
    for r in conn.execute(sql, args).fetchall():
        if r["home_goals"] is None or r["away_goals"] is None:
            skipped["no_result"] += 1
            continue
        if r["handicap"] is None:
            skipped["no_odds"] += 1
            continue
        # 0.3.19：人工复核场 → 不可评估（reason=manual_review）；推迟待核 / 推迟作废 → 单独计数
        _sp = _special.get(int(r["match_id"]))
        if _sp is not None and not_evaluable_out is not None:
            _dims = dict(match_id=int(r["match_id"]), match_uid=r["match_uid"],
                         odds_source=shadow_ev.odds_source_of(r["rationale_json"]),
                         open_basis=shadow_ev.open_basis_of(r["rationale_json"]),
                         close_basis=shadow_ev.close_basis_of(r["rationale_json"]))
            if _sp.get("manual_review"):
                shadow_ev.tally_add(not_evaluable_out, (shadow_ev.MANUAL_REVIEW, _sp["manual_review"]), **_dims)
                continue
            if _sp.get("postpone"):
                shadow_ev.postpone_add(not_evaluable_out, _sp["postpone"],
                                       delay_minutes=_sp.get("delay_minutes"), **_dims)
                continue
        elif _sp is not None:
            continue
        # 不可评估（feature_snapshot.evaluable=false / not_evaluable_reason）：不计入 n_eligible，也不算未触发
        ne = shadow_ev.not_evaluable_of(r["rationale_json"])
        if ne is not None:
            if not_evaluable_out is not None:
                shadow_ev.tally_add(not_evaluable_out, ne, match_id=int(r["match_id"]),
                                    match_uid=r["match_uid"],
                                    odds_source=shadow_ev.odds_source_of(r["rationale_json"]),
                                    open_basis=shadow_ev.open_basis_of(r["rationale_json"]),
                                    close_basis=shadow_ev.close_basis_of(r["rationale_json"]))
            continue
        d = (r["direction"] or "").strip()
        if d not in bt.NO_BET_DIRECTIONS and bt.normalize_side(d) is None:
            skipped["bad_direction"] += 1
            continue
        snap = {}
        rat = _loads_obj(r["rationale_json"], [])
        if isinstance(rat, list):
            for item in rat:
                if isinstance(item, dict) and isinstance(item.get("feature_snapshot"), dict):
                    snap = item["feature_snapshot"]
                    break
        bets.append({
            "strategy_key": strategy_key,
            "match_id": int(r["match_id"]),
            "match_uid": r["match_uid"],
            "jingcai_date": r["jingcai_date"],
            "jc_id": r["jc_id"],
            "kickoff_at": r["kickoff_at"],
            "home_team": r["home_name"],
            "away_team": r["away_name"],
            "order_key": r["jc_no"] or 0,
            "direction": d,
            "pred_stake": r["stake"],
            "stake_rule": stake_rule,
            "home_goals": r["home_goals"],
            "away_goals": r["away_goals"],
            "handicap": float(r["handicap"]),
            "home_water": r["home_water"],
            "away_water": r["away_water"],
            "water_src": r["water_src"],
            "censored_home": bool(r["censored_home"]),
            "censored_away": bool(r["censored_away"]),
            "fallback_juice": fallback_juice,
            "filter_hit": bool(snap.get("filter_hit") or snap.get("skip_v3")),
            "feature_snapshot": snap,
        })
    return bets, skipped



def _resolve_settlement_version(params: dict[str, Any] | None) -> str:
    """请求可选 settlement_version；非法值 400；默认日用固定 0.95 口径。"""
    params = params or {}
    raw = params.get("settlement_version")
    if raw is None or raw == "":
        return bt.SETTLEMENT_VERSION
    ver = str(raw).strip()
    if ver not in bt.KNOWN_SETTLEMENT_VERSIONS:
        raise HTTPException(
            400,
            f"unknown settlement_version={ver!r}; known={sorted(bt.KNOWN_SETTLEMENT_VERSIONS)}",
        )
    return ver


def _juice_note_for(book: str, settlement_version: str, exclude_censored: bool) -> str:
    if settlement_version == bt.SETTLEMENT_MACAU_ACTUAL and book == "macau":
        return (
            "灵敏度口径 ah_v4_macau_actual_or_095：water_src=actual 且水位∈(0,2] → juice_source=actual；"
            "否则回落 0.95 → fallback（见 n_actual_water / n_fallback_095 / fallback_rate）；"
            "主对比须与对照方案共用同一 settlement_version，禁止 A 真水 B 0.95 直接比 ROI"
        )
    if book == "macau":
        return (
            "macau_close 水位为仓库固定口径 0.95（不补采，非缺陷）→ juice_source=fixed_macau；"
            "影子选边主对比默认此口径（与 V3 同结算）"
        )
    return (
        "用库里真实水位：water_src=tier_midpoint（旧手工档位 t 按 w=0.70+0.05t 中点换算）→ juice_source=tier_midpoint，"
        "API 真实水位 → actual；t=0/10 为截断值（≤0.70/≥1.20）记 water_censored"
        + ("，exclude_censored=true：截断侧不下注" if exclude_censored else "")
    )


def _prepare_strategy(
    conn: sqlite3.Connection,
    def_row: sqlite3.Row,
    *,
    scope: str,
    settle_book: str,
    stake_override: dict[str, Any] | None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    params = params or {}
    cfg = normalize_strategy_config(_loads_obj(def_row["config_json"], {}))
    stake_rule = _resolve_stake_rule(cfg, stake_override)
    deprecations = []
    if "water_tier_map" in params or "water_tier_map" in (cfg.get("extras") or {}):
        deprecations.append("water_tier_map 已废弃（0.3.8）：库里已是真实/中点水位，参数被忽略")
    ex = params.get("exclude_censored")
    if ex is None:
        ex = (cfg.get("extras") or {}).get("exclude_censored", False)
    settlement_version = _resolve_settlement_version(params)
    rules = _backtest_rules(conn, stake_rule, settle_book=settle_book,
                            exclude_censored=bool(ex),
                            initial_override=params.get("initial_bankroll"))
    rules = {**rules, "settlement_version": settlement_version}
    data_rev, data_rev_source = _resolve_data_rev(conn, params, def_row["strategy_key"])
    fallback = float(cfg.get("juice") or 0.95)
    not_evaluable = shadow_ev.new_tally()
    bets, skipped = _load_strategy_bets(
        conn, strategy_key=def_row["strategy_key"], settle_book=settle_book,
        scope=scope, stake_rule=stake_rule, fallback_juice=fallback,
        not_evaluable_out=not_evaluable,
    )
    hit_mode = (cfg.get("extras") or {}).get("ledger_hit_mode")
    if hit_mode:
        for b in bets:
            b["ledger_hit_mode"] = hit_mode
    return {"cfg": cfg, "stake_rule": stake_rule, "rules": rules,
            "fallback_juice": fallback, "bets": bets, "skipped": skipped,
            "data_rev": data_rev, "data_rev_source": data_rev_source,
            "deprecations": deprecations,
            "settlement_version": settlement_version,
            "not_evaluable": not_evaluable,
            "cfg_extras": dict(cfg.get("extras") or {})}


def _fp_extra(prep: dict[str, Any]) -> dict[str, Any]:
    """指纹附加：结算版本 + 注额/资金规则（含 exclude_censored、本金）+ data_rev。"""
    from app import collection_schedule as _cs
    fp = {"settlement_version": prep.get("settlement_version") or bt.SETTLEMENT_VERSION,
          "stake_rule": prep["stake_rule"],
          "bankroll_rules": prep["rules"], "data_rev": prep["data_rev"]}
    # 0.3.19 钩子：澳门推迟作废时限写死后才进指纹（null = 不生效，旧指纹不变）
    if _cs.POSTPONE_VOID_HOURS is not None:
        fp["postpone_void_hours"] = _cs.POSTPONE_VOID_HOURS
    return fp


def _postpone_void_hours() -> float | None:
    from app import collection_schedule as _cs
    return _cs.POSTPONE_VOID_HOURS


def _single_summary(prep: dict[str, Any], sim: dict[str, Any], *, settle_book: str,
                    scope: str, strategy_key: str, params: dict[str, Any]) -> dict[str, Any]:
    hit_mode = (prep.get("cfg_extras") or {}).get("ledger_hit_mode")
    if not hit_mode and sim["entries"]:
        hit_mode = sim["entries"][0].get("ledger_hit_mode")
    s = bt.summarize(sim["entries"], prep["rules"]["initial_bankroll"], ledger_hit_mode=hit_mode)
    skipped = prep["skipped"]
    s_ver = prep.get("settlement_version") or bt.SETTLEMENT_VERSION
    s.update({
        "placeholder": False,
        "pending": False,
        "settlement": s_ver,
        "settlement_version": s_ver,
        "settle_book": settle_book,
        "fallback_juice": prep["fallback_juice"],
        "juice": prep["fallback_juice"],
        "juice_note": _juice_note_for(
            prep["rules"]["book"], s_ver, bool(prep["rules"].get("exclude_censored")),
        ),
        "settlement_note": (
            "亚盘 settle_book 线；水位见 juice_note；"
            "注额按资金曲线：本金 stats_initial_bankroll（params.initial_bankroll 可覆盖），1份=周初权益×unit_fraction，"
            "单场/单日份数上限向下取整，单注≤剩余×max_pct（整份向下取整）；"
            "below_min=raise（默认）不足 50 抬到 50，抬后超剩余×max_pct 则不下（bankrupt_guard）；skip 则直接不下"
        ),
        "exclude_censored": prep["rules"]["exclude_censored"],
        "postpone_void_hours": _postpone_void_hours(),
        "deprecations": prep["deprecations"],
        "data_rev": prep["data_rev"],
        "data_rev_source": prep["data_rev_source"],
        "stake_rule": prep["stake_rule"],
        "bankroll_rules": prep["rules"],
        "skipped": sum(skipped.values()),
        "skipped_detail": skipped,
        "cache_rows": s["n"],
        "pnl": s["pnl_amount"],
        "units": s["pnl_units"],
        "strategy_key": strategy_key,
        "scope": scope,
        "linked_prediction_batch": params.get("linked_prediction_batch"),
    })
    s.update(shadow_ev.summary_fields(prep.get("not_evaluable")))
    # 分账（接入细则 6 + 初盘口径）：by_odds_source{live,hist,unknown} / by_open_basis{first_tick,api_opening,unknown}
    # 各桶单独模拟+summarize；顶层不变
    shadow_ev.attach_breakdowns(
        s, prep["bets"], prep.get("not_evaluable"), rules=prep["rules"],
        strategy_order=[strategy_key], ledger_hit_mode=hit_mode, finalize=_attach_ledger_metrics)
    return _attach_ledger_metrics(s)


def _cache_rows_from_entries(
    entries: list[dict[str, Any]], settle_book: str, *, settlement_version: str | None = None,
) -> list[dict[str, Any]]:
    s_ver = settlement_version or bt.SETTLEMENT_VERSION
    out = []
    for i, e in enumerate(entries):
        metrics = {
            "seq": i,
            "jingcai_date": e.get("jingcai_date"),
            "match_uid": e.get("match_uid"),
            "direction": e.get("direction"),
            "home_goals": e.get("home_goals"),
            "away_goals": e.get("away_goals"),
            "units_requested": e.get("units_req"),
            "unit_amount": e.get("unit_amount"),
            "amount": e.get("amount", 0.0),
            "pnl_amount": e.get("pnl", 0.0),
            "equity_day_start": e.get("equity_day_start"),
            "equity_after": e.get("equity_after"),
            "juice_used": e.get("juice_used"),
            "juice_source": e.get("juice_source"),
            "water_raw": e.get("water_raw"),
            "water_src": e.get("water_src") if e.get("side") else None,
            "water_censored": e.get("water_censored", False),
            "settle_code": e.get("settle_code"),
            "reasons": e.get("reasons") or [],
            "settlement": s_ver,
        }
        fp = _sha256_hex(_canonical_json({
            "match_id": e["match_id"], "market": "ah", "side": e.get("side"),
            "line": e.get("handicap"), "settle_book": settle_book,
            "juice": e.get("juice_used"), "amount": e.get("amount"),
            "settlement": s_ver,
        }))
        out.append({
            "match_id": e["match_id"], "market": "ah", "side": e.get("side"),
            "line": e.get("handicap"), "stake_units": e.get("units", 0),
            "result_code": e["result_code"], "pnl_units": e.get("pnl_units", 0.0),
            "metrics": metrics, "row_fingerprint": fp,
        })
    return out


def _insert_cache_rows(
    conn: sqlite3.Connection, run_id: int, cache_rows: list[dict[str, Any]]
) -> None:
    for cr in cache_rows:
        conn.execute(
            """
            INSERT INTO strategy_validation_cache
              (run_id, match_id, market, side, line, stake_units,
               result_code, pnl_units, metrics_json, row_fingerprint)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, cr["match_id"], cr["market"], cr["side"], cr["line"],
             cr["stake_units"], cr["result_code"], cr["pnl_units"],
             json.dumps(cr["metrics"], ensure_ascii=False), cr["row_fingerprint"]),
        )


def _validate_or_reuse(
    conn: sqlite3.Connection,
    def_row: sqlite3.Row,
    *,
    scope: str,
    settle_book: str,
    params: dict[str, Any],
    run_label: str | None,
) -> tuple[sqlite3.Row, bool, dict[str, Any]]:
    """Shared by validate + compare. Returns (run_row, reused, prep)."""
    stake_override = params.get("stake_rule") if isinstance(params.get("stake_rule"), dict) else None
    prep = _prepare_strategy(conn, def_row, scope=scope, settle_book=settle_book,
                             stake_override=stake_override, params=params)
    cfg_fp = def_row["config_fingerprint"] or config_fingerprint(prep["cfg"])
    extra = _fp_extra(prep)
    fp = run_fingerprint(cfg_fp, scope, settle_book, params,
                         strategy_def_id=int(def_row["id"]), extra=extra)
    existing = conn.execute(
        "SELECT * FROM strategy_validation_runs WHERE run_fingerprint = ? LIMIT 1", (fp,)
    ).fetchone()
    if existing:
        return existing, True, prep

    sim = bt.simulate(prep["bets"], rules=prep["rules"], strategy_order=[def_row["strategy_key"]])
    summary = _single_summary(prep, sim, settle_book=settle_book, scope=scope,
                              strategy_key=def_row["strategy_key"], params=params)
    cache_rows = _cache_rows_from_entries(
        sim["entries"], settle_book,
        settlement_version=prep.get("settlement_version"),
    )
    try:
        cur = conn.execute(
            """
            INSERT INTO strategy_validation_runs
              (strategy_def_id, run_label, scope, settle_book, params_json,
               run_fingerprint, status, finished_at, summary_json)
            VALUES (?, ?, ?, ?, ?, ?, 'ok', datetime('now'), ?)
            """,
            (int(def_row["id"]), run_label, scope, settle_book,
             json.dumps(params, ensure_ascii=False), fp,
             json.dumps(summary, ensure_ascii=False)),
        )
        run_id = int(cur.lastrowid)
        _insert_cache_rows(conn, run_id, cache_rows)
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        existing = conn.execute(
            "SELECT * FROM strategy_validation_runs WHERE run_fingerprint = ?", (fp,)
        ).fetchone()
        if existing:
            return existing, True, prep
        raise
    row = conn.execute("SELECT * FROM strategy_validation_runs WHERE id = ?", (run_id,)).fetchone()
    return row, False, prep


class ValidateRequest(BaseModel):
    run_label: str | None = None
    scope: str = "jingcai"
    settle_book: str = "macau_close"
    shadow: bool = True
    params: dict[str, Any] = Field(default_factory=dict)
    settlement_version: str | None = Field(
        None,
        description=(
            "结算版本；默认 ah_v4_water_midpoint（macau 固定 0.95，日用/V3 主对比）；"
            "可选 ah_v4_macau_actual_or_095（灵敏度：真水或回落 0.95）。写入 params。"
        ),
    )


class StakeRuleIn(BaseModel):
    default_units: int = Field(1, ge=0, description="无 prediction.stake（或不用）时每注份数")
    use_prediction_stake: bool = Field(True, description="有 predictions.stake 则优先用")
    below_min: Literal["skip", "raise"] = Field("raise", description="金额 < min_stake：raise 抬到下限（默认；超剩余×max_pct→bankrupt_guard 不下）/ skip 不下")
    unit_fraction: float | None = Field(None, gt=0, le=0.1, description="缺省 bankroll unit_definition")


class CompareRequest(BaseModel):
    """Multi-strategy equity series + stack (M5).

    公平对照（算法顾问方案卡）：主对比各方案必须同一 settlement_version（默认固定 0.95）；
    include_settlement_sensitivity=true 时另跑 ah_v4_macau_actual_or_095 作同注单灵敏度副表（两套 series）。
    """
    strategy_ids: list[int] = Field(default_factory=list, description="strategy_defs.id 列表")
    strategy_keys: list[str] = Field(default_factory=list, description="按 key 取最新 version")
    scope: str = "jingcai"
    settle_book: str = "macau_close"
    shadow: bool = True
    params: dict[str, Any] = Field(default_factory=dict)
    auto_validate: bool = Field(True, description="无匹配 run 时触发 validate")
    run_label: str | None = None
    include_stack: bool = Field(True, description="计算多方案共用资金的 stack")
    stake_mode: Literal["unified", "per_strategy"] = Field(
        "per_strategy", description="unified=全部方案用请求 stake_rule；per_strategy=各自 config"
    )
    stake_rule: StakeRuleIn | None = Field(None, description="unified 模式的统一规则（缺省 DEFAULT_STAKE_RULE）")
    settlement_version: str | None = Field(
        None,
        description=(
            "主对比结算版本（写入 params；各方案共用）。默认 ah_v4_water_midpoint=固定 0.95；"
            "可选 ah_v4_macau_actual_or_095。禁止用不同 version 的 ROI 直接比选边。"
        ),
    )
    include_settlement_sensitivity: bool = Field(
        False,
        description=(
            "true：同注单再跑 ah_v4_macau_actual_or_095，响应附 sensitivity（副表 Δ + 第二套 series）；"
            "默认 false 只返回当前 settlement_version 一条资金线"
        ),
    )


@app.get("/strategies")
def list_strategies(
    status: str | None = Query(None),
    strategy_key: str | None = Query(None),
    test_only: bool | None = Query(
        None, description="过滤 extras.test_only；true=仅影子/测试方案"
    ),
) -> dict:
    conn = get_conn()
    try:
        sql = "SELECT * FROM strategy_defs WHERE 1=1"
        args: list[Any] = []
        if status:
            sql += " AND status = ?"
            args.append(status)
        if strategy_key:
            sql += " AND strategy_key = ?"
            args.append(strategy_key)
        if test_only is True:
            sql += " AND json_extract(config_json, '$.extras.test_only') = 1"
        elif test_only is False:
            sql += " AND IFNULL(json_extract(config_json, '$.extras.test_only'), 0) = 0"
        sql += " ORDER BY strategy_key, version DESC"
        rows = conn.execute(sql, args).fetchall()
        db_keys = {r[0] for r in conn.execute("SELECT DISTINCT strategy_key FROM strategy_defs")}
        db_keys |= {r[0] for r in conn.execute("SELECT DISTINCT strategy FROM predictions")}
        # 0.3.20：leak_suspect 登记表（含本库没有的 key，如 LEGACY V4–V7；in_db=false）
        return {"items": [_strategy_def_row(r) for r in rows], "leak_suspect_registry": _leak.registry(db_keys)}
    finally:
        conn.close()


@app.get("/strategies/leak_suspect")
def list_leak_suspect() -> dict:
    """0.3.20：leak_suspect 登记表（只读；来源 config/leak_suspect.json，不写库）。前端只读这个字段，不自己判断。"""
    conn = get_conn()
    try:
        db_keys = {r[0] for r in conn.execute("SELECT DISTINCT strategy_key FROM strategy_defs")}
        db_keys |= {r[0] for r in conn.execute("SELECT DISTINCT strategy FROM predictions")}
        import app.db as _adb
        return {"meta": _adb.db_meta(), **_leak.registry(db_keys)}
    finally:
        conn.close()


@app.post("/strategies", status_code=201)
def create_strategy(body: StrategyDefCreate) -> dict:
    conn = get_conn()
    try:
        row = _insert_strategy_def(
            conn,
            strategy_key=body.strategy_key,
            version=body.version,
            display_name=body.display_name,
            markets=body.markets,
            config=body.config,
            status=body.status,
            is_default=body.is_default,
            notes=body.notes,
        )
        return _strategy_def_row(row)
    finally:
        conn.close()


@app.get("/strategies/templates")
def list_strategy_templates() -> dict:
    items = []
    for name, cfg in STRATEGY_CONFIG_TEMPLATES.items():
        items.append(
            {
                "name": name,
                "description": (
                    "最小键；state_machine=null"
                    if name == "default"
                    else "最小键；state_machine=simple_gate"
                ),
                "config": normalize_strategy_config(cfg),
            }
        )
    return {"items": items}


@app.post("/strategies/compose", status_code=201)
def compose_strategy(body: ComposeRequest) -> dict:
    if body.template not in STRATEGY_CONFIG_TEMPLATES:
        raise HTTPException(
            400,
            f"unknown template {body.template!r}; known={sorted(STRATEGY_CONFIG_TEMPLATES)}",
        )
    base = dict(STRATEGY_CONFIG_TEMPLATES[body.template])
    # shallow merge overrides (extras deep-ish merge)
    overrides = dict(body.config_overrides or {})
    base_extras = dict(base.get("extras") or {})
    ov_extras = overrides.pop("extras", None)
    base.update(overrides)
    if isinstance(ov_extras, dict):
        base_extras.update(ov_extras)
    base["extras"] = base_extras
    conn = get_conn()
    try:
        row = _insert_strategy_def(
            conn,
            strategy_key=body.strategy_key,
            version=body.version,
            display_name=body.display_name,
            markets=body.markets,
            config=base,
            status=body.status,
            is_default=body.is_default,
            notes=body.notes,
        )
        return _strategy_def_row(row)
    finally:
        conn.close()


def _load_cache_rows_for_run(conn: sqlite3.Connection, run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM strategy_validation_cache WHERE run_id = ? ORDER BY id", (run_id,)
    ).fetchall()
    out = []
    for r in rows:
        m = _loads_obj(r["metrics_json"], {}) or {}
        out.append({"row": r, "metrics": m})
    out.sort(key=lambda x: (x["metrics"].get("seq", 0), x["row"]["id"]))
    return out


def _series_from_run(conn: sqlite3.Connection, run: sqlite3.Row, strategy_key: str) -> dict[str, Any]:
    summary = _loads_obj(run["summary_json"], {}) or {}
    initial = float(summary.get("initial_bankroll") or 10000.0)
    pseudo = []
    for c in _load_cache_rows_for_run(conn, int(run["id"])):
        r, m = c["row"], c["metrics"]
        if "equity_after" not in m:
            continue  # pre-M5 rows（不会出现：M5 指纹不同）
        pseudo.append({
            "strategy_key": strategy_key, "match_id": r["match_id"],
            "match_uid": m.get("match_uid"), "jingcai_date": m.get("jingcai_date"),
            "side": r["side"], "pnl": float(m.get("pnl_amount") or 0.0),
            "amount": float(m.get("amount") or 0.0), "equity_after": float(m["equity_after"]),
        })
    ser = bt.series_by_match(pseudo, initial)
    ser.pop("sides", None)
    ser.pop("conflict", None)
    return ser


def _resolve_compare_def_ids(conn: sqlite3.Connection, body: CompareRequest) -> list[int]:
    ids: list[int] = []
    for sid in body.strategy_ids or []:
        if int(sid) not in ids:
            ids.append(int(sid))
    for key in body.strategy_keys or []:
        row = conn.execute(
            "SELECT id FROM strategy_defs WHERE strategy_key = ? ORDER BY version DESC, id DESC LIMIT 1",
            (key,),
        ).fetchone()
        if row and int(row["id"]) not in ids:
            ids.append(int(row["id"]))
    return ids


def _empty_series() -> dict[str, Any]:
    return {"x": [], "match_ids": [], "pnl": [], "stake": [], "cumulative_pnl": [],
            "bankroll": [], "drawdown_pct": []}


def _compute_stack(
    conn: sqlite3.Connection,
    defs: list[sqlite3.Row],
    body: CompareRequest,
    unified_rule: dict[str, Any] | None,
) -> dict[str, Any]:
    all_bets: list[dict[str, Any]] = []
    rules = None
    order: list[str] = []
    per_rules: dict[str, Any] = {}
    data_revs: dict[str, str] = {}
    ne_by_key: dict[str, Any] = {}
    for d in defs:
        prep = _prepare_strategy(conn, d, scope=body.scope, settle_book=body.settle_book,
                                 stake_override=unified_rule, params=dict(body.params or {}))
        key = d["strategy_key"]
        if key in order:  # 同 key 多版本：用 key@version 区分
            key = f"{d['strategy_key']}@{d['version']}"
            for b in prep["bets"]:
                b["strategy_key"] = key
        order.append(key)
        per_rules[key] = prep["stake_rule"]
        data_revs[key] = prep["data_rev"]
        ne_by_key[key] = prep.get("not_evaluable")
        all_bets.extend(prep["bets"])
        if rules is None:
            rules = prep["rules"]
    if rules is None:
        return {"status": "empty"}
    if body.stake_mode == "per_strategy":
        # 资金级规则（本金/上下限）取 bankroll_config；below_min 以第一个方案为准并在响应注明
        rules = {**rules, "below_min": per_rules[order[0]].get("below_min", "raise")}
    sim = bt.simulate(all_bets, rules=rules, strategy_order=order)
    entries = sim["entries"]
    initial = rules["initial_bankroll"]
    summary = bt.summarize(entries, initial)
    conflicts = bt.find_conflicts(entries)
    per_strategy = {}
    for k in order:
        es = [e for e in entries if e["strategy_key"] == k]
        st = sum(e.get("amount", 0.0) for e in es)
        pn = sum(e["pnl"] for e in es)
        n_el = len(es)
        hits_k = sum(1 for e in es if e.get("side"))
        per_strategy[k] = {
            "n": n_el, "bet_count": sum(1 for e in es if e.get("amount", 0) > 0),
            "n_eligible": n_el, "hits": hits_k,
            "coverage": (hits_k / n_el) if n_el > 0 else None,
            "multi_hit_count": None,
            "stake_total_amount": round(st, 2), "pnl_amount": round(pn, 2),
            "roi": round(pn / st, 6) if st > 0 else None,
            "stake_rule": per_rules[k],
            "leak_suspect": _leak.leak_suspect_for(k.split("@", 1)[0]),  # 0.3.20
        }
        _ne = shadow_ev.summary_fields(ne_by_key.get(k))
        per_strategy[k]["n_not_evaluable"] = _ne["n_not_evaluable"]
        per_strategy[k]["n_not_evaluable_by_reason"] = _ne["n_not_evaluable_by_reason"]
    _stack_ne = shadow_ev.new_tally()
    for k in order:
        _stack_ne["items"].extend({**it, "strategy_key": k}
                                  for it in (ne_by_key.get(k) or {}).get("items") or [])
    summary.update(shadow_ev.summary_fields(_stack_ne))
    shadow_ev.attach_breakdowns(summary, all_bets, _stack_ne, rules=rules, strategy_order=order,
                                finalize=_attach_ledger_metrics)
    for _dim in shadow_ev.DIMENSIONS:
        for k in order:
            per_strategy[k][f"by_{_dim}"] = {
                sk: (sv.get("per_strategy") or {}).get(k) for sk, sv in summary[f"by_{_dim}"].items()}
    summary.update({
        "conflict_count": len(conflicts),
        "conflict_staked_both_count": sum(1 for c in conflicts if c["staked_both"]),
        "conflict_net_pnl": round(sum(c["net_pnl"] for c in conflicts), 2),
        "per_strategy": per_strategy,
    })
    _attach_ledger_metrics(summary)
    # stack：hits=各腿有方向的 entry 数（非去重场次）；multi_hit 仍 null（冲突见 conflict_count）
    s_ver = (rules or {}).get("settlement_version") or bt.SETTLEMENT_VERSION
    return {
        "status": "ok",
        "stake_mode": body.stake_mode,
        "stake_rule": unified_rule if body.stake_mode == "unified" else None,
        "strategies": order,
        "bankroll_rules": rules,
        "settlement_version": s_ver,
        "n_actual_water": summary.get("n_actual_water"),
        "n_fallback_095": summary.get("n_fallback_095"),
        "fallback_rate": summary.get("fallback_rate"),
        "data_rev": _resolve_data_rev(conn, dict(body.params or {}))[0],
        "data_rev_by_strategy": data_revs,
        "conflict_policy": "same_match_opposite_sides: 各自结算、不对冲、不合并注额；序列点 conflict=true 并列入 conflicts",
        "summary": summary,
        "series": bt.series_by_match(entries, initial),
        "conflicts": conflicts,
        "persisted": False,
    }


@app.post("/strategies/compare")
def compare_strategies(body: CompareRequest) -> dict:
    """Per-strategy runs (cached) + stack on shared bankroll (computed on the fly; M5)."""
    conn = get_conn()
    try:
        def_ids = _resolve_compare_def_ids(conn, body)
        if not def_ids:
            raise HTTPException(400, "strategy_ids or strategy_keys required")
        unified_rule = None
        if body.stake_mode == "unified":
            unified_rule = _resolve_stake_rule({}, (body.stake_rule.model_dump() if body.stake_rule else {}))
        params = dict(body.params or {})
        params.setdefault("shadow", body.shadow)
        if body.settlement_version:
            params["settlement_version"] = body.settlement_version
        # 主对比：解析一次，保证各方案同一 settlement_version
        primary_sv = _resolve_settlement_version(params)
        params["settlement_version"] = primary_sv
        if unified_rule is not None:
            params["stake_rule"] = unified_rule  # 单方案 run 同口径，指纹区分

        items: list[dict[str, Any]] = []
        defs_ok: list[sqlite3.Row] = []
        for sid in def_ids:
            d = conn.execute("SELECT * FROM strategy_defs WHERE id = ?", (sid,)).fetchone()
            if not d:
                items.append({"strategy_def_id": sid, "status": "missing_def", "run_id": None,
                              "summary": None, "series": _empty_series(), "reused": None})
                continue
            defs_ok.append(d)
            if not body.auto_validate:
                stake_override = params.get("stake_rule") if isinstance(params.get("stake_rule"), dict) else None
                prep = _prepare_strategy(conn, d, scope=body.scope, settle_book=body.settle_book,
                                         stake_override=stake_override, params=params)
                cfg_fp = d["config_fingerprint"] or config_fingerprint(prep["cfg"])
                fp = run_fingerprint(cfg_fp, body.scope, body.settle_book, params,
                                     strategy_def_id=int(d["id"]), extra=_fp_extra(prep))
                run = conn.execute("SELECT * FROM strategy_validation_runs WHERE run_fingerprint = ?",
                                   (fp,)).fetchone()
                if not run:
                    items.append({"strategy_def_id": sid, "strategy_key": d["strategy_key"],
                                  "leak_suspect": _leak.leak_suspect_for(d["strategy_key"]),
                                  "version": d["version"], "status": "missing_run", "run_id": None,
                                  "summary": None, "series": _empty_series(), "reused": None,
                                  "note": "no validation run for fingerprint; pass auto_validate=true"})
                    continue
                reused = True
            else:
                run, reused, _ = _validate_or_reuse(conn, d, scope=body.scope,
                                                    settle_book=body.settle_book,
                                                    params=params, run_label=body.run_label)
            items.append({
                "strategy_def_id": sid, "strategy_key": d["strategy_key"], "version": d["version"],
                "leak_suspect": _leak.leak_suspect_for(d["strategy_key"]),  # 0.3.20
                "status": "ok", "run_id": int(run["id"]), "run_fingerprint": run["run_fingerprint"],
                "summary": _attach_ledger_metrics(_loads_obj(run["summary_json"], None)),
                "series": _series_from_run(conn, run, d["strategy_key"]),
                "reused": reused,
            })

        stack = _compute_stack(conn, defs_ok, body, unified_rule) if (body.include_stack and defs_ok) else None
        out: dict[str, Any] = {
            "scope": body.scope,
            "settle_book": body.settle_book,
            "params": params,
            "stake_mode": body.stake_mode,
            "settlement_version": primary_sv,
            "fair_compare_note": (
                "主对比各方案共用同一 settlement_version；"
                "ah_v4_macau_actual_or_095 仅作同注单灵敏度副表，禁止与固定 0.95 跨口径比 ROI"
            ),
            "data_rev": _resolve_data_rev(conn, params)[0],
            "data_rev_source": _resolve_data_rev(conn, params)[1],
            "data_rev_note": "顶层为基础版本（不含 predictions 签名）；各方案完整 data_rev 见 items[].summary.data_rev / stack.data_rev_by_strategy",
            "items": items,
            "stack": stack,
        }
        if stack and isinstance(stack.get("summary"), dict):
            out["n_actual_water"] = stack["summary"].get("n_actual_water")
            out["n_fallback_095"] = stack["summary"].get("n_fallback_095")
            out["fallback_rate"] = stack["summary"].get("fallback_rate")
        # 双结算：同注单灵敏度副表（第二套 series）；不落库、不进主指纹
        if (body.include_settlement_sensitivity and defs_ok
                and primary_sv != bt.SETTLEMENT_MACAU_ACTUAL):
            sens_params = dict(params)
            sens_params["settlement_version"] = bt.SETTLEMENT_MACAU_ACTUAL
            # 临时 body 视图：复用 CompareRequest 字段，只换 params 口径
            sens_body = body.model_copy(update={"params": sens_params, "settlement_version": bt.SETTLEMENT_MACAU_ACTUAL})
            sens_stack = _compute_stack(conn, defs_ok, sens_body, unified_rule) if body.include_stack else None
            sens_items = []
            for d in defs_ok:
                prep = _prepare_strategy(
                    conn, d, scope=body.scope, settle_book=body.settle_book,
                    stake_override=unified_rule, params=sens_params,
                )
                sim = bt.simulate(prep["bets"], rules=prep["rules"], strategy_order=[d["strategy_key"]])
                summary = _single_summary(
                    prep, sim, settle_book=body.settle_book, scope=body.scope,
                    strategy_key=d["strategy_key"], params=sens_params,
                )
                sens_items.append({
                    "strategy_def_id": int(d["id"]),
                    "strategy_key": d["strategy_key"],
                    "leak_suspect": _leak.leak_suspect_for(d["strategy_key"]),
                    "version": d["version"],
                    "settlement_version": bt.SETTLEMENT_MACAU_ACTUAL,
                    "summary": summary,
                    "series": bt.series_by_match(sim["entries"], prep["rules"]["initial_bankroll"]),
                })
            # Δ：灵敏度 − 主表（同方案）
            primary_by_key = {i["strategy_key"]: i for i in items if i.get("status") == "ok"}
            deltas = []
            for si in sens_items:
                pi = primary_by_key.get(si["strategy_key"])
                if not pi or not isinstance(pi.get("summary"), dict):
                    continue
                ps, ss = pi["summary"], si["summary"]
                deltas.append({
                    "strategy_key": si["strategy_key"],
                    "delta_pnl_amount": round((ss.get("pnl_amount") or 0) - (ps.get("pnl_amount") or 0), 2),
                    "delta_roi": (
                        None if ps.get("roi") is None or ss.get("roi") is None
                        else round((ss["roi"] or 0) - (ps["roi"] or 0), 6)
                    ),
                    "primary_settlement_version": primary_sv,
                    "sensitivity_settlement_version": bt.SETTLEMENT_MACAU_ACTUAL,
                })
            out["sensitivity"] = {
                "role": "same_bets_settlement_sensitivity",
                "settlement_version": bt.SETTLEMENT_MACAU_ACTUAL,
                "note": "同注单真实水位灵敏度；不替代主表选边结论；fallback_rate 高时标不可靠",
                "items": sens_items,
                "stack": sens_stack,
                "delta_vs_primary": deltas,
                "n_actual_water": (sens_stack or {}).get("n_actual_water") if sens_stack else None,
                "n_fallback_095": (sens_stack or {}).get("n_fallback_095") if sens_stack else None,
                "fallback_rate": (sens_stack or {}).get("fallback_rate") if sens_stack else None,
            }
        return out
    finally:
        conn.close()


@app.get("/strategies/{key}/versions")
def list_strategy_versions(key: str = PathParam(...)) -> dict:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM strategy_defs WHERE strategy_key = ? ORDER BY version DESC",
            (key,),
        ).fetchall()
        return {"strategy_key": key, "items": [_strategy_def_row(r) for r in rows]}
    finally:
        conn.close()


@app.post("/strategies/{id}/validate", status_code=202)
def start_strategy_validate(
    id: int = PathParam(..., ge=1),
    body: ValidateRequest | None = None,
) -> dict:
    """Create or reuse a validation run (M5: juice source + bankroll curve + max drawdown).

    Fingerprint includes settlement_version + stake/bankroll rules (exclude_censored) + data_rev.
    """
    req = body or ValidateRequest()
    conn = get_conn()
    try:
        def_row = conn.execute("SELECT * FROM strategy_defs WHERE id = ?", (id,)).fetchone()
        if not def_row:
            raise HTTPException(404, "strategy_def not found")
        params = dict(req.params or {})
        params.setdefault("shadow", req.shadow)
        if req.settlement_version:
            params["settlement_version"] = req.settlement_version
        params["settlement_version"] = _resolve_settlement_version(params)
        run, reused, prep = _validate_or_reuse(conn, def_row, scope=req.scope,
                                               settle_book=req.settle_book, params=params,
                                               run_label=req.run_label)
        out = _validation_run_row(run, reused=reused)
        out["strategy_key"] = def_row["strategy_key"]
        out["leak_suspect"] = _leak.leak_suspect_for(def_row["strategy_key"])  # 0.3.20
        out["data_rev"] = prep["data_rev"]
        out["data_rev_source"] = prep["data_rev_source"]
        out["settlement_version"] = prep.get("settlement_version") or params["settlement_version"]
        return out
    finally:
        conn.close()


@app.get("/strategies/runs/{run_id}")
def get_strategy_run(run_id: int = PathParam(..., ge=1)) -> dict:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM strategy_validation_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "run not found")
        out = _validation_run_row(row)
        d = conn.execute("SELECT strategy_key FROM strategy_defs WHERE id = ?", (row["strategy_def_id"],)).fetchone()
        out["strategy_key"] = d["strategy_key"] if d else None
        out["leak_suspect"] = _leak.leak_suspect_for(out["strategy_key"])  # 0.3.20
        return out
    finally:
        conn.close()


@app.get("/strategies/runs/{run_id}/cache")
def get_strategy_run_cache(
    run_id: int = PathParam(..., ge=1),
    limit: int = Query(500, ge=1, le=5000),
) -> dict:
    conn = get_conn()
    try:
        run = conn.execute(
            "SELECT id FROM strategy_validation_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if not run:
            raise HTTPException(404, "run not found")
        rows = conn.execute(
            """
            SELECT * FROM strategy_validation_cache
            WHERE run_id = ?
            ORDER BY id
            LIMIT ?
            """,
            (run_id, limit),
        ).fetchall()
        items = []
        for r in rows:
            items.append(
                {
                    "id": r["id"],
                    "run_id": r["run_id"],
                    "match_id": r["match_id"],
                    "market": r["market"],
                    "side": r["side"],
                    "line": r["line"],
                    "stake_units": r["stake_units"],
                    "result_code": r["result_code"],
                    "pnl_units": r["pnl_units"],
                    "metrics": _loads_obj(r["metrics_json"], None),
                    "row_fingerprint": r["row_fingerprint"],
                }
            )
        return {"run_id": run_id, "items": items}
    finally:
        conn.close()


# ===================== 0.3.15 · 数据表页 GET /table/matches（只读） =====================
# 放在文件末尾：table_matches 内部 lazy import app.main 的 helper，避免循环引用。
from app.table_matches import router as table_router  # noqa: E402

app.include_router(table_router)
