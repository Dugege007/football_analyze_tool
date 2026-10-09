#!/usr/bin/env python3
"""D2：odds_snapshot ↔ odds_asian 副本演练工具（幂等）。

子命令：
  legacy-to-snapshot  旧 odds_asian → odds_snapshot(channel=rule_legacy)；不改 odds_asian
  sync-rule           rule×asian×{open,mid,close} → odds_asian **仅 INSERT 缺失键**；永不 UPDATE 旧行
  ensure-view         重建 v_odds_asian_rule（仅 open/mid/close）
  rehearse            上述三步 + 对照报告（默认）

硬约束：
  - 默认拒绝写入现网 data/app.db（除非 --i-know-this-is-production 且环境 DUAL_WRITE_ODDS_ASIAN=1）
  - t8/t1 不进 odds_asian；缺 rule.mid → 跳过该 phase 并记 gaps
  - 只做 asian

例：
  .venv/bin/python scripts/sync_odds_asian_from_snapshot.py --db data/v2d2/app.db
  .venv/bin/python scripts/sync_odds_asian_from_snapshot.py --db data/v2d2/app.db --dry-run
"""
from __future__ import annotations
# --- public repo: paths are env-overridable (see config.example.env) ---
import os as _rp_os
from pathlib import Path as _RpPath
_REPO_ROOT = _RpPath(__file__).resolve().parents[2]
def _rp_load_dotenv(path=_REPO_ROOT / '.env'):
    # 读仓库根 .env（KEY=VALUE；已存在的环境变量优先，不覆盖）；不打印任何值
    if _rp_os.environ.get('FAT_DISABLE_DOTENV') == '1':  # 测试时由 conftest 设置，避免本机 .env 干扰
        return
    try:
        for _ln in path.read_text(encoding='utf-8').splitlines():
            _ln = _ln.strip()
            if not _ln or _ln.startswith('#') or '=' not in _ln:
                continue
            _k, _v = _ln.split('=', 1)
            _k = _k.strip().removeprefix('export ').strip()
            _v = _v.strip().strip('"').strip("'")
            if _v.startswith('YOUR_'):  # config.example.env 占位值视为未填写
                continue
            if _k and _k not in _rp_os.environ:
                _rp_os.environ[_k] = _v
    except FileNotFoundError:
        pass
_rp_load_dotenv()
def _rp_env(name, default, base=None):
    v = _rp_os.environ.get(name, '').strip()
    p = _RpPath(v).expanduser() if v else default
    return p if p.is_absolute() else (base or _REPO_ROOT) / p
_MA_API_ROOT = _rp_env('MA_API_ROOT', _REPO_ROOT / 'api')
_ODDS_DATA_DIR = _rp_env('ODDS_DATA_DIR', _REPO_ROOT / 'data' / 'odds-data')
_APP_DB = _rp_env('APP_DB_PATH', _MA_API_ROOT / 'data' / 'app.db', _MA_API_ROOT)
_V2D3_DB = _rp_env('V2D3_DB_PATH', _MA_API_ROOT / 'data' / 'v2d3' / 'app.db', _MA_API_ROOT)
_BACKUP_DIR = _rp_env('BACKUP_DIR', _REPO_ROOT / 'backups' / 'football')
# --- end path config ---


import argparse
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import apply_sql_idempotent, connect  # noqa: E402

DEFAULT_DB = ROOT / "data" / "v2d2" / "app.db"
LIVE_DB = (ROOT / "data" / "app.db").resolve()
VIEW_SQL = Path(str(_REPO_ROOT / "docs/schema/v2_0_d2_view_asian_rule.sql"))
V2_MIGRATION = Path(str(_REPO_ROOT / "docs/schema/v2_0_odds_timeline.sql"))
DUAL_SOURCE = "v2_dual_write"
LEGACY_SNAP_SOURCE = "legacy_import"


def _env_dual_write_on() -> bool:
    return os.environ.get("DUAL_WRITE_ODDS_ASIAN", "0").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def assert_db_allowed(db: Path, *, force_live: bool) -> None:
    resolved = db.resolve()
    if resolved == LIVE_DB:
        if not force_live:
            raise SystemExit(
                f"拒绝写入现网 {LIVE_DB}。副本请用 --db data/v2d2/app.db；"
                "若确要写现网须同时：环境 DUAL_WRITE_ODDS_ASIAN=1 且 --i-know-this-is-production"
            )
        if not _env_dual_write_on():
            raise SystemExit(
                "现网双写开关关闭（DUAL_WRITE_ODDS_ASIAN!=1）。按拍板保持关闭。"
            )


def oa_fingerprint(conn: sqlite3.Connection, *, exclude_probe: bool = True) -> dict:
    if exclude_probe:
        rows = conn.execute(
            """
            SELECT oa.id, oa.match_id, oa.book, oa.phase, oa.handicap,
                   oa.home_water, oa.away_water, oa.water_src, oa.water_censored
            FROM odds_asian oa
            JOIN matches m ON m.id = oa.match_id
            WHERE m.match_uid NOT LIKE 'probe:%'
            ORDER BY oa.id
            """
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, match_id, book, phase, handicap, home_water, away_water,
                   water_src, water_censored
            FROM odds_asian ORDER BY id
            """
        ).fetchall()
    # 必须转 tuple：sqlite3.Row 的 repr 不稳定，会导致同内容指纹漂移
    tup = [tuple(r) for r in rows]
    return {
        "count": len(tup),
        "fp": hashlib.sha256(repr(tup).encode()).hexdigest()[:16],
        "exclude_probe": exclude_probe,
    }


def ensure_schema(conn: sqlite3.Connection) -> None:
    if V2_MIGRATION.exists():
        apply_sql_idempotent(conn, V2_MIGRATION)
    if VIEW_SQL.exists():
        # DROP+CREATE：不用 idempotent 逐句（DROP 后 CREATE）
        conn.executescript(VIEW_SQL.read_text(encoding="utf-8"))
    conn.commit()


def legacy_to_snapshot(conn: sqlite3.Connection, *, dry_run: bool) -> dict:
    """旧 odds_asian → odds_snapshot(rule_legacy)。不改 odds_asian。"""
    rows = conn.execute(
        """
        SELECT oa.match_id, oa.book, oa.phase, oa.handicap, oa.home_water, oa.away_water,
               oa.water_src, oa.water_censored, oa.extras_json, m.match_uid
        FROM odds_asian oa
        JOIN matches m ON m.id = oa.match_id
        WHERE oa.phase IN ('open', 'mid', 'close')
          AND m.match_uid NOT LIKE 'probe:%'
        ORDER BY oa.match_id, oa.book, oa.phase
        """
    ).fetchall()
    inserted = 0
    skipped = 0
    for r in rows:
        exists = conn.execute(
            """
            SELECT 1 FROM odds_snapshot
            WHERE match_id=? AND book=? AND market='asian'
              AND channel='rule_legacy' AND point=?
            """,
            (r["match_id"], r["book"], r["phase"]),
        ).fetchone()
        if exists:
            skipped += 1
            continue
        extras = {
            "from": "odds_asian",
            "match_uid": r["match_uid"],
            "phase": r["phase"],
        }
        if r["extras_json"]:
            try:
                extras["odds_asian_extras"] = json.loads(r["extras_json"])
            except json.JSONDecodeError:
                extras["odds_asian_extras_raw"] = r["extras_json"]
        if dry_run:
            inserted += 1
            continue
        conn.execute(
            """
            INSERT INTO odds_snapshot (
              match_id, book, market, channel, point, recorded_at, target_at,
              lag_hours, stale_gap, line, water_home, water_away,
              water_src, water_censored, source, extras_json
            ) VALUES (?,?,?,?,?,?,NULL,NULL,0,?,?,?,?,?,?,?)
            """,
            (
                r["match_id"],
                r["book"],
                "asian",
                "rule_legacy",
                r["phase"],
                None,
                r["handicap"],
                r["home_water"],
                r["away_water"],
                r["water_src"],
                r["water_censored"],
                LEGACY_SNAP_SOURCE,
                json.dumps(extras, ensure_ascii=False),
            ),
        )
        inserted += 1
    if not dry_run:
        conn.commit()
    return {
        "action": "legacy_to_snapshot",
        "candidates": len(rows),
        "inserted": inserted,
        "skipped_existing": skipped,
        "dry_run": dry_run,
    }


def sync_rule_to_asian(conn: sqlite3.Connection, *, dry_run: bool) -> dict:
    """rule snapshot → odds_asian：仅 INSERT 缺失 (match_id,book,phase)。"""
    snaps = conn.execute(
        """
        SELECT match_id, book, point, line, water_home, water_away,
               water_src, water_censored, recorded_at, target_at, source, extras_json
        FROM odds_snapshot
        WHERE market = 'asian' AND channel = 'rule'
          AND point IN ('open', 'mid', 'close')
        ORDER BY match_id, book, point
        """
    ).fetchall()

    # gap 日志：同一 match×book 有任一 open/mid/close 但缺某相
    present: dict[tuple, set[str]] = {}
    for s in snaps:
        present.setdefault((s["match_id"], s["book"]), set()).add(s["point"])
    gaps = []
    for (mid, book), pts in sorted(present.items()):
        missing = [p for p in ("open", "mid", "close") if p not in pts]
        if missing:
            gaps.append({"match_id": mid, "book": book, "missing_phases": missing})

    inserted = 0
    skipped_existing = 0
    for s in snaps:
        phase = s["point"]
        ex = conn.execute(
            "SELECT id, extras_json FROM odds_asian WHERE match_id=? AND book=? AND phase=?",
            (s["match_id"], s["book"], phase),
        ).fetchone()
        if ex:
            skipped_existing += 1
            continue
        extras = {
            "source": DUAL_SOURCE,
            "from_channel": "rule",
            "from_point": phase,
            "snapshot_recorded_at": s["recorded_at"],
            "snapshot_target_at": s["target_at"],
            "snapshot_source": s["source"],
            "sign_convention": "positive_home_gives",
        }
        if dry_run:
            inserted += 1
            continue
        conn.execute(
            """
            INSERT INTO odds_asian (
              match_id, book, phase, handicap, home_water, away_water,
              water_src, water_censored, extras_json
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                s["match_id"],
                s["book"],
                phase,
                # 2026-10-08：快照 API 记法(负=主让) → odds_asian 记法(正=主让)；入库前取反
                (None if s["line"] is None else (0.0 if float(s["line"]) == 0 else -float(s["line"]))),
                s["water_home"],
                s["water_away"],
                s["water_src"],
                s["water_censored"],
                json.dumps(extras, ensure_ascii=False),
            ),
        )
        inserted += 1
    if not dry_run:
        # 入库前正负号校验（validate_ah_sign）：整批拦截或单场待复核 → 回滚，不入库
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from validate_ah_sign import validate_conn
        for ph in ("mid",):
            rep = validate_conn(conn, phase=ph)
            if rep["status"] != "pass":
                conn.rollback()
                raise SystemExit(f"validate_ah_sign {ph}: {rep['status']} blocked={rep['blocked']} "
                                 f"review={len(rep['review'])} → 已回滚，未入库")
        conn.commit()
    return {
        "action": "sync_rule",
        "snapshot_rows": len(snaps),
        "inserted": inserted,
        "skipped_existing": skipped_existing,
        "gaps_missing_phase": gaps,
        "gap_count": len(gaps),
        "dry_run": dry_run,
    }


def view_compare(conn: sqlite3.Connection) -> dict:
    """VIEW vs 物理表：probe 双写行 + 整体计数。"""
    try:
        view_n = conn.execute("SELECT COUNT(*) AS c FROM v_odds_asian_rule").fetchone()["c"]
    except sqlite3.Error as e:
        return {"ok": False, "error": str(e)}
    # VIEW 行与物理表同键对照（仅 VIEW 有的键）
    rows = conn.execute(
        """
        SELECT v.match_id, v.book, v.phase, v.handicap AS v_h, v.home_water AS v_hw,
               a.handicap AS a_h, a.home_water AS a_hw,
               json_extract(a.extras_json, '$.source') AS a_src,
               m.match_uid
        FROM v_odds_asian_rule v
        LEFT JOIN odds_asian a
          ON a.match_id = v.match_id AND a.book = v.book AND a.phase = v.phase
        LEFT JOIN matches m ON m.id = v.match_id
        """
    ).fetchall()
    matched = 0
    missing_physical = 0
    value_mismatch = 0
    dual_write_ok = 0
    for r in rows:
        if r["a_h"] is None and r["a_hw"] is None and r["a_src"] is None:
            # LEFT JOIN 无行：sqlite 全 NULL
            # distinguish: no physical row
            exists = conn.execute(
                "SELECT 1 FROM odds_asian WHERE match_id=? AND book=? AND phase=?",
                (r["match_id"], r["book"], r["phase"]),
            ).fetchone()
            if not exists:
                missing_physical += 1
                continue
        matched += 1
        if r["a_src"] == DUAL_SOURCE:
            if r["v_h"] == r["a_h"] and r["v_hw"] == r["a_hw"]:
                dual_write_ok += 1
            else:
                value_mismatch += 1
        # 旧行：不要求与 rule VIEW 数值一致（旧为 rule_legacy 语义）
    return {
        "ok": True,
        "view_rows": view_n,
        "joined": len(rows),
        "physical_present": matched,
        "missing_physical": missing_physical,
        "dual_write_value_ok": dual_write_ok,
        "dual_write_value_mismatch": value_mismatch,
        "note": "旧 177 物理行可与 rule VIEW 数值不同（legacy 钟点）；仅 dual_write 行要求一致",
    }


def rehearse(conn: sqlite3.Connection, *, dry_run: bool) -> dict:
    fp_before = oa_fingerprint(conn, exclude_probe=True)
    ensure_schema(conn)
    legacy = legacy_to_snapshot(conn, dry_run=dry_run)
    sync = sync_rule_to_asian(conn, dry_run=dry_run)
    fp_after = oa_fingerprint(conn, exclude_probe=True)
    probe_oa = conn.execute(
        """
        SELECT COUNT(*) AS c FROM odds_asian oa
        JOIN matches m ON m.id = oa.match_id
        WHERE m.match_uid LIKE 'probe:%'
        """
    ).fetchone()["c"]
    dual_n = conn.execute(
        """
        SELECT COUNT(*) AS c FROM odds_asian
        WHERE json_extract(extras_json, '$.source') = ?
        """,
        (DUAL_SOURCE,),
    ).fetchone()["c"]
    legacy_snap = conn.execute(
        """
        SELECT COUNT(*) AS c FROM odds_snapshot
        WHERE channel = 'rule_legacy' AND market = 'asian'
        """
    ).fetchone()["c"]
    cmp = view_compare(conn) if not dry_run else {"ok": None, "note": "dry_run skip view compare"}
    return {
        "dry_run": dry_run,
        "legacy_to_snapshot": legacy,
        "sync_rule": sync,
        "oa_legacy_fp_before": fp_before,
        "oa_legacy_fp_after": fp_after,
        "oa_legacy_unchanged": fp_before == fp_after,
        "probe_odds_asian_rows": probe_oa,
        "dual_write_rows": dual_n,
        "rule_legacy_asian_snapshots": legacy_snap,
        "view_compare": cmp,
        "dual_write_env": os.environ.get("DUAL_WRITE_ODDS_ASIAN", "0"),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="D2 snapshot↔odds_asian rehearsal")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--i-know-this-is-production",
        action="store_true",
        help="允许写现网（仍需 DUAL_WRITE_ODDS_ASIAN=1）",
    )
    ap.add_argument(
        "command",
        nargs="?",
        default="rehearse",
        choices=["rehearse", "legacy-to-snapshot", "sync-rule", "ensure-view", "fp"],
    )
    args = ap.parse_args()
    if args.command != "fp":
        assert_db_allowed(args.db, force_live=args.i_know_this_is_production)
    args.db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(args.db)
    try:
        if args.command == "fp":
            print(json.dumps({"db": str(args.db), **oa_fingerprint(conn)}, ensure_ascii=False, indent=2))
            return
        ensure_schema(conn)
        if args.command == "ensure-view":
            print(json.dumps({"ok": True, "view": "v_odds_asian_rule"}, ensure_ascii=False))
            return
        if args.command == "legacy-to-snapshot":
            print(json.dumps(legacy_to_snapshot(conn, dry_run=args.dry_run), ensure_ascii=False, indent=2))
            return
        if args.command == "sync-rule":
            before = oa_fingerprint(conn)
            out = sync_rule_to_asian(conn, dry_run=args.dry_run)
            out["oa_legacy_fp_before"] = before
            out["oa_legacy_fp_after"] = oa_fingerprint(conn)
            out["oa_legacy_unchanged"] = out["oa_legacy_fp_before"] == out["oa_legacy_fp_after"]
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return
        # rehearse
        print(json.dumps(rehearse(conn, dry_run=args.dry_run), ensure_ascii=False, indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
