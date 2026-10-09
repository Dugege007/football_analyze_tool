#!/usr/bin/env python3
"""Apply v1_sqlite.sql and load json-v2-sample.json into app.db."""
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


import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.db import DB_PATH, SCHEMA_PATH, apply_schema, connect  # noqa: E402
from import_lib import insert_match, match_uid  # noqa: E402

SAMPLE_PATH = Path(str(_REPO_ROOT / "docs/backfill-schema/json-v2-sample.json"))


def main() -> None:
    sample = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
    if DB_PATH.exists():
        if "--reset" not in sys.argv[1:]:
            raise SystemExit(f"{DB_PATH} 已存在：seed 会删除并重建该库。确认是演示库再加 --reset；"
                             "切勿对自己的备份库/现网库执行（APP_DB_PATH 指向备份时请勿运行 seed）")
        DB_PATH.unlink()

    conn = connect(DB_PATH)
    try:
        apply_schema(conn, SCHEMA_PATH)
        cur = conn.execute(
            """
            INSERT INTO import_batches (source_file, month, row_count, status)
            VALUES (?, ?, ?, 'ok')
            """,
            ("demo/json-v2-sample.json", "2610", len(sample)),
        )
        batch_id = cur.lastrowid
        for item in sample:
            uid = match_uid(item["match"])
            mid = insert_match(conn, item, batch_id, write_prediction=True)
            print(f"imported match_id={mid} uid={uid}")
        conn.commit()
        print(f"DB ready: {DB_PATH}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
