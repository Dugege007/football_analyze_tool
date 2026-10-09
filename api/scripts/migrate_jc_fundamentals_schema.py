#!/usr/bin/env python3
"""v2d3 竞彩完整盘 + 基本面观测 DDL／打标（幂等；默认 dry-run）。

依据：
  docs/schema/v2_0-jc-odds-capture-schema-draft.md
  docs/schema/v2_0-fundamentals-ingest-schema-draft.md
  协作中拍板：hhad 一 phase 一行当前线 + hist；odds_jc_home 冻结；injury 不伪 known_empty

硬规则：
  - 默认 --db 指向 data/v2d3/app.db；拒绝现网 data/app.db
  - 默认 --dry-run（只探测／统计，不写）；要落库加 --apply
  - 不碰 predictions／settlements；不 seed；不开 DUAL_WRITE
  - 不投影 odds_snapshot jc_spf/jc_hhad（local collector 未通、无正式采盘行；见 ACCEPTANCE）

用法：
  .venv/bin/python scripts/migrate_jc_fundamentals_schema.py
  .venv/bin/python scripts/migrate_jc_fundamentals_schema.py --apply
  .venv/bin/python scripts/migrate_jc_fundamentals_schema.py --db data/v2d3/app.db --apply
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import LIVE_DB_PATH, SCHEMA_DIR, apply_sql_idempotent  # noqa: E402

SQL_PATH = SCHEMA_DIR / "v2_0_jc_fundamentals.sql"
DEFAULT_DB = ROOT / "data" / "v2d3" / "app.db"
TZ_CN = timezone(timedelta(hours=8))
HHAD_META_COLS = (
    "source", "captured_at", "target_at", "usable_at_mid", "usable_at_close",
    "jc_1x2_incomplete", "goal_line_raw", "sporttery_match_id", "update_date",
    "update_time", "line_rev", "extras_json",
)
STATS_META_COLS = (
    "source_recent", "as_of_recent", "source_h2h", "as_of_h2h",
    "source_rank", "as_of_rank", "source_injury", "as_of_injury",
    "source_weather", "as_of_weather", "source_popularity", "as_of_popularity",
    "extras_json_meta",
)


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _refuse_live(db: Path) -> None:
    if db.resolve() == LIVE_DB_PATH.resolve():
        raise SystemExit(
            "REFUSE: 拒绝写现网 data/app.db。本迁移只允许 v2d3 副本（DUAL_WRITE 关）。"
        )


def _as_of_for_match(jingcai_date: str | None) -> str:
    """手工底座 as_of：jingcai_date 当日 12:00+08:00；未知则用迁移日 12:00。"""
    if jingcai_date:
        try:
            d = datetime.strptime(jingcai_date, "%Y-%m-%d").replace(
                hour=12, minute=0, second=0, tzinfo=TZ_CN
            )
            return d.isoformat()
        except ValueError:
            pass
    return datetime.now(TZ_CN).replace(hour=12, minute=0, second=0, microsecond=0).isoformat()


def probe(conn: sqlite3.Connection) -> dict:
    out: dict = {
        "tables": {
            "odds_jc_had": _has_table(conn, "odds_jc_had"),
            "odds_jc_hhad_line_hist": _has_table(conn, "odds_jc_hhad_line_hist"),
            "stats_obs": _has_table(conn, "stats_obs"),
            "odds_jc_home": _has_table(conn, "odds_jc_home"),
            "odds_jc_hhad": _has_table(conn, "odds_jc_hhad"),
            "stats": _has_table(conn, "stats"),
        },
        "hhad_cols_missing": [],
        "stats_cols_missing": [],
        "counts": {},
    }
    if out["tables"]["odds_jc_hhad"]:
        have = _cols(conn, "odds_jc_hhad")
        out["hhad_cols_missing"] = [c for c in HHAD_META_COLS if c not in have]
        if "superseded_at" in have:
            out["hhad_main_has_superseded_at"] = True  # 不期望；主表当前线不用此列
    if out["tables"]["stats"]:
        have = _cols(conn, "stats")
        out["stats_cols_missing"] = [c for c in STATS_META_COLS if c not in have]
    queries = [
        ("odds_jc_home", "SELECT COUNT(*) FROM odds_jc_home"),
        ("odds_jc_hhad", "SELECT COUNT(*) FROM odds_jc_hhad"),
        ("stats", "SELECT COUNT(*) FROM stats"),
    ]
    if out["tables"]["odds_jc_had"]:
        queries.append(("odds_jc_had", "SELECT COUNT(*) FROM odds_jc_had"))
    if out["tables"]["stats_obs"]:
        queries.append(("stats_obs", "SELECT COUNT(*) FROM stats_obs"))
    if out["tables"]["odds_jc_hhad_line_hist"]:
        queries.append(("odds_jc_hhad_line_hist", "SELECT COUNT(*) FROM odds_jc_hhad_line_hist"))
    for t, q in queries:
        try:
            out["counts"][t] = conn.execute(q).fetchone()[0]
        except sqlite3.Error:
            out["counts"][t] = None
    # 待打标
    if out["tables"]["stats"] and "source_recent" in _cols(conn, "stats"):
        out["counts"]["stats_untagged_recent"] = conn.execute(
            "SELECT COUNT(*) FROM stats WHERE recent_json IS NOT NULL AND recent_json!='' "
            "AND (source_recent IS NULL OR source_recent='')"
        ).fetchone()[0]
    else:
        out["counts"]["stats_untagged_recent"] = out["counts"].get("stats")
    return out


def apply_ddl(conn: sqlite3.Connection) -> int:
    return apply_sql_idempotent(conn, SQL_PATH)


def tag_stats(conn: sqlite3.Connection, fetched_at: str) -> dict:
    """一次性：现有投影标 manual_seed；injury 空不写 known_empty、不改 injury_json。"""
    rows = conn.execute(
        "SELECT s.match_id, m.jingcai_date, s.recent_json, s.h2h_json, s.rank_home, s.rank_away, "
        "s.injury_json, s.weather_json, s.popularity_diff, "
        "s.source_recent, s.source_h2h, s.source_rank, s.source_weather, s.source_popularity "
        "FROM stats s JOIN matches m ON m.id = s.match_id"
    ).fetchall()
    tagged = {"recent": 0, "h2h": 0, "rank": 0, "weather": 0, "popularity": 0, "injury_skipped_empty": 0}
    for r in rows:
        as_of = _as_of_for_match(r["jingcai_date"])
        sets: list[str] = []
        vals: list = []
        if r["recent_json"] and not r["source_recent"]:
            sets += ["source_recent=?", "as_of_recent=?"]
            vals += ["manual_seed", as_of]
            tagged["recent"] += 1
        if r["h2h_json"] and not r["source_h2h"]:
            sets += ["source_h2h=?", "as_of_h2h=?"]
            vals += ["manual_seed", as_of]
            tagged["h2h"] += 1
        if (r["rank_home"] is not None or r["rank_away"] is not None) and not r["source_rank"]:
            sets += ["source_rank=?", "as_of_rank=?"]
            vals += ["manual_seed", as_of]
            tagged["rank"] += 1
        if r["weather_json"] and not r["source_weather"]:
            sets += ["source_weather=?", "as_of_weather=?"]
            vals += ["manual_seed", as_of]
            tagged["weather"] += 1
        if r["popularity_diff"] is not None and not r["source_popularity"]:
            sets += ["source_popularity=?", "as_of_popularity=?"]
            vals += ["manual_seed", as_of]
            tagged["popularity"] += 1
        # injury：空 → 不打 source、不写 known_empty
        if not r["injury_json"]:
            tagged["injury_skipped_empty"] += 1
        if sets:
            vals.append(r["match_id"])
            conn.execute(f"UPDATE stats SET {', '.join(sets)} WHERE match_id=?", vals)
    return tagged


def seed_stats_obs(conn: sqlite3.Connection, fetched_at: str) -> dict:
    """从现有 stats JSON 投影出 manual_seed 观测行（覆盖率分源）；injury 空不建行。"""
    inserted = {"recent": 0, "h2h": 0, "rank": 0, "weather": 0, "popularity": 0, "skip_existing": 0}
    rows = conn.execute(
        "SELECT s.match_id, m.jingcai_date, s.recent_json, s.h2h_json, s.rank_home, s.rank_away, "
        "s.weather_json, s.popularity_diff, s.as_of_recent, s.as_of_h2h, s.as_of_rank, "
        "s.as_of_weather, s.as_of_popularity "
        "FROM stats s JOIN matches m ON m.id = s.match_id"
    ).fetchall()

    def _ins(match_id: int, kind: str, as_of: str, payload: dict) -> bool:
        try:
            conn.execute(
                "INSERT INTO stats_obs (match_id, kind, as_of, source, payload_json, fetched_at, quality) "
                "VALUES (?,?,?,?,?,?,?)",
                (match_id, kind, as_of, "manual_seed", json.dumps(payload, ensure_ascii=False),
                 fetched_at, "ok"),
            )
            return True
        except sqlite3.IntegrityError:
            inserted["skip_existing"] += 1
            return False

    for r in rows:
        fallback = _as_of_for_match(r["jingcai_date"])
        if r["recent_json"]:
            as_of = r["as_of_recent"] or fallback
            try:
                payload = json.loads(r["recent_json"])
            except json.JSONDecodeError:
                payload = {"raw": r["recent_json"]}
            if _ins(r["match_id"], "recent", as_of, payload):
                inserted["recent"] += 1
        if r["h2h_json"]:
            as_of = r["as_of_h2h"] or fallback
            try:
                payload = json.loads(r["h2h_json"])
            except json.JSONDecodeError:
                payload = {"raw": r["h2h_json"]}
            if _ins(r["match_id"], "h2h", as_of, payload):
                inserted["h2h"] += 1
        if r["rank_home"] is not None or r["rank_away"] is not None:
            as_of = r["as_of_rank"] or fallback
            if _ins(r["match_id"], "rank", as_of, {"home": r["rank_home"], "away": r["rank_away"]}):
                inserted["rank"] += 1
        if r["weather_json"]:
            as_of = r["as_of_weather"] or fallback
            try:
                payload = json.loads(r["weather_json"])
            except json.JSONDecodeError:
                payload = {"raw": r["weather_json"]}
            if _ins(r["match_id"], "weather", as_of, payload):
                inserted["weather"] += 1
        if r["popularity_diff"] is not None:
            as_of = r["as_of_popularity"] or fallback
            if _ins(r["match_id"], "popularity", as_of, {"diff": r["popularity_diff"]}):
                inserted["popularity"] += 1
    return inserted


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB, help="目标库（默认 data/v2d3/app.db）")
    ap.add_argument("--apply", action="store_true", help="真正写入；默认 dry-run")
    ap.add_argument("--skip-obs-seed", action="store_true", help="只 DDL+stats 打标，不写 stats_obs")
    args = ap.parse_args()
    db = args.db if args.db.is_absolute() else (ROOT / args.db)
    _refuse_live(db)
    if not db.exists():
        raise SystemExit(f"db missing: {db}")
    if not SQL_PATH.exists():
        raise SystemExit(f"SQL missing: {SQL_PATH}")

    dry = not args.apply
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    before = probe(conn)
    summary: dict = {
        "db": str(db),
        "dry_run": dry,
        "dual_write": "off",
        "live_refused": True,
        "before": before,
        "sql": str(SQL_PATH),
        "snapshot_projection": "skipped (local collector empty; no jc_spf/jc_hhad production rows)",
    }
    if dry:
        summary["would"] = {
            "ddl": "CREATE odds_jc_had / odds_jc_hhad_line_hist / stats_obs; ALTER hhad+stats meta cols",
            "tag_stats": "source_*=manual_seed, as_of_*=jingcai_date 12:00+08",
            "seed_stats_obs": (not args.skip_obs_seed),
            "odds_jc_home_rows_untouched": before["counts"].get("odds_jc_home"),
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        conn.close()
        return

    skipped = apply_ddl(conn)
    fetched_at = datetime.now(TZ_CN).isoformat()
    tagged = tag_stats(conn, fetched_at)
    obs = {} if args.skip_obs_seed else seed_stats_obs(conn, fetched_at)
    conn.commit()
    after = probe(conn)
    # hist 必须有 superseded_at
    hist_cols = _cols(conn, "odds_jc_hhad_line_hist") if after["tables"]["odds_jc_hhad_line_hist"] else set()
    summary.update({
        "ddl_skipped_duplicate_cols": skipped,
        "tagged": tagged,
        "stats_obs_seeded": obs,
        "after": after,
        "hist_has_superseded_at": "superseded_at" in hist_cols,
        "odds_jc_home_count": after["counts"].get("odds_jc_home"),
        "fetched_at": fetched_at,
    })
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if "superseded_at" not in hist_cols:
        raise SystemExit("FAIL: odds_jc_hhad_line_hist missing superseded_at")
    conn.close()


if __name__ == "__main__":
    main()
