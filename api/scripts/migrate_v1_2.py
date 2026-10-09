"""Apply schema v1.2 to the live SQLite DB idempotently.

- 逐句执行 v1_2_bankroll_alias_kickoff.sql
- ALTER TABLE ... ADD COLUMN 遇到 "duplicate column name" 忽略（已应用过）
- 整体在一个事务里；任何其他错误回滚
- 前后打印核心表行数，确认不丢数据

Usage:
  .venv/bin/python scripts/migrate_v1_2.py [--db data/app.db] [--dry-run]
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
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SQL_PATH = Path(str(_REPO_ROOT / "docs/schema/v1_2_bankroll_alias_kickoff.sql"))
COUNT_TABLES = ["matches", "predictions", "prediction_legs", "results", "odds_asian", "odds_raw", "match_meta"]


def split_statements(sql: str) -> list[str]:
    stmts, buf = [], ""
    for line in sql.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            s = buf.strip()
            if s and not all(l.strip().startswith("--") or not l.strip() for l in s.splitlines()):
                stmts.append(s)
            buf = ""
    if buf.strip() and not all(l.strip().startswith("--") or not l.strip() for l in buf.splitlines()):
        stmts.append(buf.strip())
    return stmts


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in COUNT_TABLES}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "app.db"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db, isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    before = counts(conn)
    print("before:", before)

    stmts = split_statements(SQL_PATH.read_text(encoding="utf-8"))
    skipped = 0
    conn.execute("BEGIN")
    try:
        for s in stmts:
            if s.upper().startswith("PRAGMA"):
                continue  # PRAGMA 在事务内无效，连接已设置
            try:
                conn.execute(s)
            except sqlite3.OperationalError as e:
                if "duplicate column name" in str(e).lower():
                    skipped += 1
                    continue
                raise
        after = counts(conn)
        if after != before:
            raise RuntimeError(f"row counts changed: {before} -> {after}")
        if args.dry_run:
            conn.execute("ROLLBACK")
            print("dry-run: rolled back")
        else:
            conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    print(f"statements={len(stmts)} duplicate_columns_skipped={skipped}")
    print("after:", counts(conn))
    print("schema_migrations:", conn.execute("SELECT version, applied_at FROM schema_migrations ORDER BY applied_at").fetchall())
    return 0


if __name__ == "__main__":
    sys.exit(main())
