#!/usr/bin/env python3
"""Dry-run / optional apply: normalize matches text via aliases.

Default: print what would change. Pass --apply to UPDATE (after backup).
Does not delete teams/leagues. Safe for backend review.
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
import sqlite3
from pathlib import Path

DEFAULT_DB = Path(str(_APP_DB))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--apply", action="store_true", help="actually UPDATE (default dry-run)")
    args = ap.parse_args()
    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row

    home = list(
        con.execute(
            """
            SELECT m.id, m.home_team AS old, t.name_zh_canonical AS new
            FROM matches m
            JOIN team_aliases a ON a.alias = m.home_team
            JOIN teams t ON t.id = a.team_id
            WHERE t.name_zh_canonical <> m.home_team
            """
        )
    )
    away = list(
        con.execute(
            """
            SELECT m.id, m.away_team AS old, t.name_zh_canonical AS new
            FROM matches m
            JOIN team_aliases a ON a.alias = m.away_team
            JOIN teams t ON t.id = a.team_id
            WHERE t.name_zh_canonical <> m.away_team
            """
        )
    )
    league = list(
        con.execute(
            """
            SELECT m.id, m.competition_name AS old, l.name_zh_canonical AS new
            FROM matches m
            JOIN league_aliases a ON a.alias = m.competition_name
            JOIN leagues l ON l.id = a.league_id
            WHERE l.name_zh_canonical <> m.competition_name
            """
        )
    )
    print(f"home_team changes: {len(home)}")
    for r in home:
        print(f"  match {r['id']}: {r['old']!r} -> {r['new']!r}")
    print(f"away_team changes: {len(away)}")
    for r in away:
        print(f"  match {r['id']}: {r['old']!r} -> {r['new']!r}")
    print(f"competition_name changes: {len(league)}")
    for r in league:
        print(f"  match {r['id']}: {r['old']!r} -> {r['new']!r}")

    if not args.apply:
        print("dry-run only; re-run with --apply after DB backup")
        return 0

    sql_path = Path(__file__).resolve().parents[1] / "backfill" / "normalize_match_team_league_text.sql"
    con.executescript(sql_path.read_text(encoding="utf-8"))
    print("applied", sql_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
