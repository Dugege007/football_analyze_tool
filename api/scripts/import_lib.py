"""Shared helpers for seeding / monthly JSON import into match-analysis SQLite."""
from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from pathlib import Path
from typing import Any


def match_uid(match: dict) -> str:
    date = match["date"]
    # Infer scope: explicit scope wins; else jc present → jingcai
    scope = match.get("scope")
    if not scope:
        scope = "jingcai" if match.get("jc") else "extra"
    if scope == "jingcai":
        jc = match.get("jc") or {}
        jc_id = jc.get("id")
        if not jc_id:
            raise ValueError(f"jingcai match missing jc.id: {match}")
        return f"{date}|{jc_id}"
    home = match["teams"]["home"]
    away = match["teams"]["away"]
    return f"{date}|extra|{home}|{away}"


def infer_scope(match: dict) -> str:
    scope = match.get("scope")
    if scope in ("jingcai", "extra"):
        return scope
    return "jingcai" if match.get("jc") else "extra"


def parse_jc_no(jc: dict | None) -> int | None:
    if not jc:
        return None
    no = jc.get("no")
    if no is None:
        return None
    try:
        return int(str(no))
    except ValueError:
        digits = "".join(c for c in str(no) if c.isdigit())
        return int(digits) if digits else None


def asian_phase_to_row(phase_val):
    """Return (handicap, home_water, away_water) from JSON phase value."""
    if phase_val is None:
        return None
    if isinstance(phase_val, (int, float)):
        return (float(phase_val), None, None)
    if isinstance(phase_val, dict):
        # Skip all-null phase dicts (seen in william for some monthly rows)
        if all(phase_val.get(k) is None for k in ("handicap", "home_water", "away_water")):
            return None
        return (
            float(phase_val["handicap"]) if phase_val.get("handicap") is not None else None,
            float(phase_val["home_water"]) if phase_val.get("home_water") is not None else None,
            float(phase_val["away_water"]) if phase_val.get("away_water") is not None else None,
        )
    raise TypeError(f"unsupported asian phase value: {phase_val!r}")


TIER_WATER_BOOKS = ("crown", "william")


def _ensure_v1_7_columns(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(odds_asian)")}
    if "water_src" not in cols:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from app.db import SCHEMA_DIR, apply_sql_idempotent
        apply_sql_idempotent(conn, SCHEMA_DIR / "v1_7_water_tier_midpoint.sql")


def asian_water_fields(book: str, phase_val: Any, book_val: Any, home_water: Any, away_water: Any):
    """v1.7：决定入库水位与来源标记。

    - macau：不动（通常无水位）→ water_src=NULL（有水位则 actual）。
    - crown/william：旧手工 JSON 存的是档位 t → 中点水位 w=0.70+0.05t，water_src=tier_midpoint，
      t≤0 / t≥10 → water_censored=1；原始档位进 extras_json。
      若 phase dict 或 book dict 显式带 "water_src": "actual"（API 来的真实水位）→ 原样存，water_src=actual。
    - 其它书：有水位 → actual。
    Returns (home_water, away_water, water_src, water_censored, extras_json)。
    """
    if home_water is None and away_water is None:
        return None, None, None, None, None
    hint = None
    if isinstance(phase_val, dict):
        hint = phase_val.get("water_src")
    if hint is None and isinstance(book_val, dict):
        hint = book_val.get("water_src")
    if book in TIER_WATER_BOOKS and hint != "actual":
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from app.water import convert_tier_pair
        cv = convert_tier_pair(home_water, away_water)
        return (cv["home_water"], cv["away_water"], cv["water_src"], cv["water_censored"],
                json.dumps(cv["extras"], ensure_ascii=False))
    return home_water, away_water, "actual", 0, None


def _json_or_none(val: Any) -> str | None:
    if val is None:
        return None
    return json.dumps(val, ensure_ascii=False)


def _has_column(conn: sqlite3.Connection, table: str, col: str) -> bool:
    return any(r[1] == col for r in conn.execute(f"PRAGMA table_info({table})"))


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        is not None
    )


def normalize_name(name: str | None) -> str | None:
    """轻量规范化：NFKC + trim + 折叠空白；不改译名。"""
    if name is None:
        return None
    n = unicodedata.normalize("NFKC", str(name)).strip()
    n = re.sub(r"\s+", " ", n)
    return n or None


def _lookup_team_id(conn: sqlite3.Connection, key: str) -> int | None:
    row = conn.execute(
        "SELECT team_id FROM team_aliases WHERE alias = ?", (key,)
    ).fetchone()
    if row:
        return int(row["team_id"] if isinstance(row, sqlite3.Row) else row[0])
    row = conn.execute(
        "SELECT id FROM teams WHERE name_zh_canonical = ?", (key,)
    ).fetchone()
    if row:
        return int(row["id"] if isinstance(row, sqlite3.Row) else row[0])
    return None


def _lookup_league_id(conn: sqlite3.Connection, key: str) -> int | None:
    row = conn.execute(
        "SELECT league_id FROM league_aliases WHERE alias = ?", (key,)
    ).fetchone()
    if row:
        return int(row["league_id"] if isinstance(row, sqlite3.Row) else row[0])
    row = conn.execute(
        "SELECT id FROM leagues WHERE name_zh_canonical = ?", (key,)
    ).fetchone()
    if row:
        return int(row["id"] if isinstance(row, sqlite3.Row) else row[0])
    return None


def resolve_or_create_team(
    conn: sqlite3.Connection,
    name: str | None,
    *,
    source: str = "import",
    create: bool = True,
) -> int | None:
    """按别名/规范名解析球队；可选新建。冲突或失败返回 None（软失败）。"""
    if not _has_table(conn, "teams") or not _has_table(conn, "team_aliases"):
        return None
    raw = (str(name).strip() if name is not None else "") or None
    norm = normalize_name(name)
    if not norm:
        return None
    for key in dict.fromkeys([norm, raw] if raw and raw != norm else [norm]):
        tid = _lookup_team_id(conn, key)
        if tid is not None:
            # 确保规范名也有 alias
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO team_aliases (alias, team_id, source) VALUES (?, ?, ?)",
                    (norm, tid, source),
                )
                if raw and raw != norm:
                    conn.execute(
                        "INSERT OR IGNORE INTO team_aliases (alias, team_id, source) VALUES (?, ?, ?)",
                        (raw, tid, source),
                    )
            except sqlite3.IntegrityError:
                pass
            return tid
    if not create:
        return None
    try:
        cur = conn.execute(
            "INSERT INTO teams (name_zh_canonical) VALUES (?)", (norm,)
        )
        tid = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO team_aliases (alias, team_id, source) VALUES (?, ?, ?)",
            (norm, tid, source),
        )
        if raw and raw != norm:
            try:
                conn.execute(
                    "INSERT INTO team_aliases (alias, team_id, source) VALUES (?, ?, ?)",
                    (raw, tid, source),
                )
            except sqlite3.IntegrityError:
                # UNIQUE(alias) 撞到别的队 → 不强制合并
                pass
        return tid
    except sqlite3.IntegrityError:
        # 并发/已存在：再查一次
        tid = _lookup_team_id(conn, norm) or (raw and _lookup_team_id(conn, raw))
        return tid


def resolve_or_create_league(
    conn: sqlite3.Connection,
    name: str | None,
    *,
    source: str = "import",
    create: bool = True,
) -> int | None:
    """按别名/规范名解析联赛；可选新建。冲突或失败返回 None。"""
    if not _has_table(conn, "leagues") or not _has_table(conn, "league_aliases"):
        return None
    raw = (str(name).strip() if name is not None else "") or None
    norm = normalize_name(name)
    if not norm:
        return None
    for key in dict.fromkeys([norm, raw] if raw and raw != norm else [norm]):
        lid = _lookup_league_id(conn, key)
        if lid is not None:
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO league_aliases (alias, league_id, source) VALUES (?, ?, ?)",
                    (norm, lid, source),
                )
                if raw and raw != norm:
                    conn.execute(
                        "INSERT OR IGNORE INTO league_aliases (alias, league_id, source) VALUES (?, ?, ?)",
                        (raw, lid, source),
                    )
            except sqlite3.IntegrityError:
                pass
            return lid
    if not create:
        return None
    try:
        cur = conn.execute(
            "INSERT INTO leagues (name_zh_canonical) VALUES (?)", (norm,)
        )
        lid = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO league_aliases (alias, league_id, source) VALUES (?, ?, ?)",
            (norm, lid, source),
        )
        if raw and raw != norm:
            try:
                conn.execute(
                    "INSERT INTO league_aliases (alias, league_id, source) VALUES (?, ?, ?)",
                    (raw, lid, source),
                )
            except sqlite3.IntegrityError:
                pass
        return lid
    except sqlite3.IntegrityError:
        return _lookup_league_id(conn, norm) or (raw and _lookup_league_id(conn, raw))


def sync_match_entity_ids(
    conn: sqlite3.Connection,
    match_id: int,
    *,
    home_team: str | None,
    away_team: str | None,
    competition_name: str | None,
    source: str = "import",
    create: bool = True,
) -> None:
    """写入 matches.home/away/league_id；解不出则保持 NULL，不中断导入。"""
    if not _has_column(conn, "matches", "home_team_id"):
        return
    home_id = resolve_or_create_team(conn, home_team, source=source, create=create)
    away_id = resolve_or_create_team(conn, away_team, source=source, create=create)
    league_id = resolve_or_create_league(
        conn, competition_name, source=source, create=create
    )
    conn.execute(
        """
        UPDATE matches
        SET home_team_id = ?, away_team_id = ?, league_id = ?,
            updated_at = datetime('now')
        WHERE id = ?
        """,
        (home_id, away_id, league_id, match_id),
    )



def sync_kickoff_at(conn: sqlite3.Connection, match_id: int, match: dict) -> None:
    """v1.2：写 matches.kickoff_at / kickoff_minute_known（列不存在则跳过）。

    - JSON 带 kickoff_at → 原样写入，minute_known=1
    - 否则仅有 kickoff_hour → 合成整点（早场特殊带 0–11 记次日；
      11:31–11:59 需分钟才能排除，整点 11 全部 +1 日），minute_known=0；
      已有 minute_known=1 的精确值不覆盖
    """
    if not _has_column(conn, "matches", "kickoff_at"):
        return
    ka = match.get("kickoff_at")
    if ka:
        conn.execute(
            "UPDATE matches SET kickoff_at = ?, kickoff_minute_known = 1 WHERE id = ?",
            (ka, match_id),
        )
        return
    # 仅整点回退：0<=h<=11 → jingcai_date+1（对齐 is_early_kickoff_band hour-only）
    conn.execute(
        """
        UPDATE matches
        SET kickoff_at = (CASE WHEN kickoff_hour <= 11 THEN date(jingcai_date, '+1 day')
                               ELSE date(jingcai_date) END)
                         || 'T' || printf('%02d', kickoff_hour) || ':00:00+08:00',
            kickoff_minute_known = 0
        WHERE id = ? AND kickoff_hour BETWEEN 0 AND 23
          AND (kickoff_at IS NULL OR kickoff_minute_known = 0)
        """,
        (match_id,),
    )


def insert_match(conn: sqlite3.Connection, item: dict, batch_id: int, *, write_prediction: bool = True) -> int:
    """Upsert one match by match_uid: DELETE child rows then re-insert; UPDATE match row.

    Upsert strategy: ON CONFLICT(match_uid) DO UPDATE on matches, then DELETE+INSERT
    for child tables (results/stats/odds_*/meta/predictions). Re-import of same uid
    replaces content and reassigns import_batch_id to the new batch.
    Predictions: only written when write_prediction=True AND item has prediction;
    monthly import passes write_prediction=False and never invents directions.
    """
    match = item["match"]
    uid = match_uid(match)
    scope = infer_scope(match)
    # Normalize scope onto match for downstream
    match = {**match, "scope": scope}
    jc = match.get("jc")
    competition = match.get("competition") or {}
    teams = match["teams"]

    conn.execute(
        """
        INSERT INTO matches (
          match_uid, scope, jingcai_date, weekday, kickoff_hour,
          jc_id, jc_no, competition_name, competition_type, competition_stage,
          home_team, away_team, import_batch_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(match_uid) DO UPDATE SET
          scope=excluded.scope,
          jingcai_date=excluded.jingcai_date,
          weekday=excluded.weekday,
          kickoff_hour=excluded.kickoff_hour,
          jc_id=excluded.jc_id,
          jc_no=excluded.jc_no,
          competition_name=excluded.competition_name,
          competition_type=excluded.competition_type,
          competition_stage=excluded.competition_stage,
          home_team=excluded.home_team,
          away_team=excluded.away_team,
          import_batch_id=excluded.import_batch_id,
          updated_at=datetime('now')
        """,
        (
            uid,
            scope,
            match["date"],
            match.get("weekday"),
            match.get("kickoff_hour"),
            (jc or {}).get("id") if jc else None,
            parse_jc_no(jc),
            competition.get("name"),
            competition.get("type"),
            # stage may be int or str in monthly JSON
            str(competition["stage"]) if competition.get("stage") is not None else None,
            teams["home"],
            teams["away"],
            batch_id,
        ),
    )
    row = conn.execute("SELECT id FROM matches WHERE match_uid = ?", (uid,)).fetchone()
    match_id = int(row["id"])
    sync_kickoff_at(conn, match_id, match)
    # v1.2：解析/创建队与联赛 id（软失败，不解则 NULL）
    sync_match_entity_ids(
        conn,
        match_id,
        home_team=teams.get("home"),
        away_team=teams.get("away"),
        competition_name=competition.get("name"),
        source="import",
        create=True,
    )

    result = item.get("result")
    conn.execute("DELETE FROM results WHERE match_id = ?", (match_id,))
    if result is not None:
        # Keep row even if all nulls (pre-match placeholders in 2607)
        conn.execute(
            """
            INSERT INTO results (match_id, home_goals, away_goals, total_goals, wdl)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                match_id,
                result.get("home_goals"),
                result.get("away_goals"),
                result.get("total_goals"),
                result.get("wdl"),
            ),
        )

    stats = item.get("stats") or {}
    recent = stats.get("recent") or {}
    h2h = stats.get("h2h")
    rank = stats.get("rank") or {}
    extras: dict[str, Any] = {}
    if "support_proxy_odds" in stats:
        extras["support_proxy_odds"] = stats.get("support_proxy_odds")
    home_streak = recent.get("home_streak_last6")
    weather = stats.get("weather")
    # weather may be string ("下雨") or object — always JSON-encode
    weather_json = _json_or_none(weather) if weather is not None else None

    conn.execute("DELETE FROM stats WHERE match_id = ?", (match_id,))
    conn.execute(
        """
        INSERT INTO stats (
          match_id, recent_json, home_streak_last6, h2h_json,
          rank_home, rank_away, popularity_diff, injury_json, weather_json, extras_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            match_id,
            _json_or_none(recent) if recent else None,
            float(home_streak) if home_streak is not None else None,
            _json_or_none(h2h) if h2h is not None else None,
            rank.get("home"),
            rank.get("away"),
            stats.get("popularity_diff"),
            _json_or_none(stats["injury"]) if stats.get("injury") is not None else None,
            weather_json,
            _json_or_none(extras) if extras else None,
        ),
    )

    odds = item.get("odds") or {}
    conn.execute("DELETE FROM odds_raw WHERE match_id = ?", (match_id,))
    conn.execute(
        "INSERT INTO odds_raw (match_id, odds_json) VALUES (?, ?)",
        (match_id, json.dumps(odds, ensure_ascii=False)),
    )

    _ensure_v1_7_columns(conn)
    conn.execute("DELETE FROM odds_asian WHERE match_id = ?", (match_id,))
    asian = odds.get("asian") or {}
    for book, phases in asian.items():
        if not isinstance(phases, dict):
            continue
        for phase, val in phases.items():
            if val is None or phase == "water_src":  # book 级来源标记，不是盘口阶段
                continue
            row_vals = asian_phase_to_row(val)
            if row_vals is None:
                continue
            handicap, home_water, away_water = row_vals
            hw, aw, wsrc, wcens, wextras = asian_water_fields(book, val, phases, home_water, away_water)
            conn.execute(
                """
                INSERT INTO odds_asian (match_id, book, phase, handicap, home_water, away_water,
                                        water_src, water_censored, extras_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (match_id, book, phase, handicap, hw, aw, wsrc, wcens, wextras),
            )

    conn.execute("DELETE FROM odds_euro_home WHERE match_id = ?", (match_id,))
    euro = odds.get("euro_home_win") or {}
    for book, phases in euro.items():
        if not isinstance(phases, dict):
            continue
        for phase, val in phases.items():
            if val is None:
                continue
            conn.execute(
                """
                INSERT INTO odds_euro_home (match_id, book, phase, home_win)
                VALUES (?, ?, ?, ?)
                """,
                (match_id, book, phase, float(val)),
            )

    conn.execute("DELETE FROM odds_jc_home WHERE match_id = ?", (match_id,))
    jc_home = odds.get("jc_home_win") or {}
    for phase, val in jc_home.items():
        if val is None:
            continue
        conn.execute(
            "INSERT INTO odds_jc_home (match_id, phase, home_win) VALUES (?, ?, ?)",
            (match_id, phase, float(val)),
        )

    conn.execute("DELETE FROM odds_jc_hhad WHERE match_id = ?", (match_id,))
    jc_hhad = odds.get("jc_hhad") or {}
    for phase, val in jc_hhad.items():
        if val is None:
            continue
        gl = val.get("goal_line")
        gl_num = float(gl) if gl is not None and gl != "" else None
        conn.execute(
            """
            INSERT INTO odds_jc_hhad (match_id, phase, goal_line, home_odds, draw_odds, away_odds)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                match_id,
                phase,
                gl_num,
                float(val["home"]) if val.get("home") is not None else None,
                float(val["draw"]) if val.get("draw") is not None else None,
                float(val["away"]) if val.get("away") is not None else None,
            ),
        )

    meta = item.get("meta") or {}
    extras_meta = {
        "kickoff_at": match.get("kickoff_at"),
        "ids": match.get("ids") or {},
        "schema_version": meta.get("schema_version"),
        "pipeline": meta.get("pipeline"),
        "note": meta.get("note"),
    }
    conn.execute("DELETE FROM match_meta WHERE match_id = ?", (match_id,))
    conn.execute(
        """
        INSERT INTO match_meta (match_id, source_file, month, extras_json)
        VALUES (?, ?, ?, ?)
        """,
        (
            match_id,
            meta.get("source_file"),
            meta.get("month"),
            json.dumps(extras_meta, ensure_ascii=False),
        ),
    )

    # Predictions: never invent. Monthly import sets write_prediction=False.
    if write_prediction:
        pred = item.get("prediction")
        # 0.3.19：旧 SHADOW_S2 / SHADOW_N4 已冻结，禁止再写
        from app.ledger_registry import assert_appendable
        assert_appendable((pred or {}).get("strategy") or "CFFXDJ_5_V3")
        # 只替换本条写入的策略；不误删其他方案（如影子 TEST_V3_MIRROR）的预测
        conn.execute(
            "DELETE FROM predictions WHERE match_id = ? AND strategy = ?",
            (match_id, (pred or {}).get("strategy") or "CFFXDJ_5_V3"),
        )
        if pred:
            conn.execute(
                """
                INSERT INTO predictions (
                  match_id, strategy, direction, settle_book, rationale_json, confidence
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    match_id,
                    pred.get("strategy") or "CFFXDJ_5_V3",
                    pred["direction"],
                    pred.get("settle_book") or "macau_close",
                    json.dumps(pred.get("rationale") or [], ensure_ascii=False),
                    pred.get("confidence"),
                ),
            )
    # else: leave any existing predictions untouched on re-import of same uid
    # (monthly files have no prediction; do not DELETE)

    return match_id
