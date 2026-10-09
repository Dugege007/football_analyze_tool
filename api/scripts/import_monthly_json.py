#!/usr/bin/env python3
"""Import 作者的私有分析仓 monthly JSON (array of match/result/stats/odds/meta) into app.db.

Upsert strategy: ON CONFLICT(match_uid) DO UPDATE on matches; child tables DELETE+INSERT.
Each file → one import_batches row; matches.import_batch_id points to it.
Does NOT invent predictions (write_prediction=False).
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
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.db import DB_PATH, apply_schema, connect  # noqa: E402
from import_lib import infer_scope, insert_match, match_uid  # noqa: E402

DEFAULT_FILES = [
    Path(str(_ODDS_DATA_DIR / "imports/2026/2606.json")),
    Path(str(_ODDS_DATA_DIR / "imports/2026/2607.json")),
]


def import_file(conn, path: Path) -> tuple[int, dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise SystemExit(f"{path}: expected JSON array")

    month = None
    source_file = str(path)
    if data:
        meta = data[0].get("meta") or {}
        month = meta.get("month") or path.stem
        source_file = meta.get("source_file") or str(path)

    cur = conn.execute(
        """
        INSERT INTO import_batches (source_file, month, row_count, status)
        VALUES (?, ?, ?, 'ok')
        """,
        (source_file, month, len(data)),
    )
    batch_id = int(cur.lastrowid)

    counts = {"jingcai": 0, "extra": 0, "total": 0}
    for item in data:
        m = item.get("match") or {}
        scope = infer_scope(m)
        insert_match(conn, item, batch_id, write_prediction=False)
        counts[scope] = counts.get(scope, 0) + 1
        counts["total"] += 1

    conn.execute(
        "UPDATE import_batches SET row_count = ? WHERE id = ?",
        (counts["total"], batch_id),
    )
    return batch_id, counts


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "files",
        nargs="*",
        type=Path,
        default=DEFAULT_FILES,
        help="Monthly JSON paths (default: 2606+2607 under odds-data/imports/2026)",
    )
    ap.add_argument(
        "--reset-db",
        action="store_true",
        help="Delete app.db and re-apply schema before import (drops demo seed too)",
    )
    ap.add_argument(
        "--keep-demo",
        action="store_true",
        help="Keep existing rows (including demo seed); only upsert imported uids",
    )
    args = ap.parse_args()

    for p in args.files:
        if not p.exists():
            raise SystemExit(f"missing file: {p}")

    if args.reset_db and DB_PATH.exists():
        DB_PATH.unlink()
        print(f"reset {DB_PATH}")

    conn = connect(DB_PATH)
    try:
        apply_schema(conn)
        if not args.keep_demo and not args.reset_db:
            # Soft reset: clear match graph but keep schema; remove prior demo/batches
            # Prefer clean slate for monthly import so UI dates are real
            print("Clearing existing matches/batches for clean monthly import...")
            conn.execute("PRAGMA foreign_keys = OFF")
            for t in (
                "predictions",
                "match_meta",
                "odds_raw",
                "odds_jc_hhad",
                "odds_jc_home",
                "odds_euro_home",
                "odds_asian",
                "stats",
                "results",
                "matches",
                "import_batches",
            ):
                conn.execute(f"DELETE FROM {t}")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.commit()

        summary = []
        for path in args.files:
            batch_id, counts = import_file(conn, path)
            summary.append((path, batch_id, counts))
            print(
                f"OK batch_id={batch_id} file={path.name} "
                f"total={counts['total']} jingcai={counts['jingcai']} extra={counts['extra']}"
            )
        conn.commit()

        # Report dates + samples
        rows = conn.execute(
            """
            SELECT jingcai_date, COUNT(*) AS n
            FROM matches WHERE scope='jingcai'
            GROUP BY jingcai_date ORDER BY jingcai_date
            """
        ).fetchall()
        print(f"distinct jingcai dates: {len(rows)}")
        if rows:
            print(f"  first={rows[0]['jingcai_date']} last={rows[-1]['jingcai_date']}")
            print("  all:", ", ".join(r["jingcai_date"] for r in rows))
        samples = conn.execute(
            """
            SELECT match_uid, jingcai_date, home_team, away_team
            FROM matches WHERE scope='jingcai'
            ORDER BY jingcai_date, jc_no LIMIT 3
            """
        ).fetchall()
        print("samples:")
        for s in samples:
            print(f"  {s['match_uid']}  {s['home_team']} vs {s['away_team']}  ({s['jingcai_date']})")
        print(f"DB: {DB_PATH}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
