"""SQLite helpers for match-analysis-api."""
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


import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIVE_DB_PATH = ROOT / "data" / "app.db"
# 0.3.18：实例选库（不设 = 现网 data/app.db，现网 8787 启动命令不变）
#   APP_DB_PATH   库文件路径（如 data/v2d3/app.db；相对路径按仓库根解析）
#   APP_DB_LABEL  live | v2d3（响应 meta.db；不设 → live）
#   APP_READONLY  1/true → 只读实例：sqlite 一律 mode=ro，写接口（POST/PUT/PATCH/DELETE）403
_env_path = os.environ.get("APP_DB_PATH", "").strip()
DB_PATH = (Path(_env_path) if Path(_env_path).is_absolute() else ROOT / _env_path) if _env_path else LIVE_DB_PATH
DB_LABEL = (os.environ.get("APP_DB_LABEL", "").strip().lower() or "live")
READONLY = os.environ.get("APP_READONLY", "0").strip().lower() in ("1", "true", "yes", "on")
if DB_LABEL not in ("live", "v2d3"):
    raise RuntimeError(f"APP_DB_LABEL must be live|v2d3, got {DB_LABEL!r}")
if DB_LABEL == "v2d3" and Path(DB_PATH).resolve() == LIVE_DB_PATH.resolve():
    raise RuntimeError("APP_DB_LABEL=v2d3 but APP_DB_PATH points at live data/app.db")
if DB_LABEL == "v2d3" and not READONLY:
    raise RuntimeError("v2d3 instance must run with APP_READONLY=1 (not promoted)")


def db_meta() -> dict:
    """响应 meta：db=live|v2d3，promoted=现网才 true（副本未晋升），readonly。"""
    return {"db": DB_LABEL, "promoted": DB_LABEL == "live", "readonly": bool(READONLY)}
SCHEMA_DIR = Path(str(_REPO_ROOT / "docs/schema"))
SCHEMA_PATH = SCHEMA_DIR / "v1_sqlite.sql"
# 增量 migration（按顺序；均可重复执行）
MIGRATION_PATHS = [
    SCHEMA_DIR / "v1_1_prediction_legs.sql",
    SCHEMA_DIR / "v1_2_bankroll_alias_kickoff.sql",
    SCHEMA_DIR / "v1_3_strategy_workshop.sql",
    SCHEMA_DIR / "v1_7_water_tier_midpoint.sql",  # DDL；数据换算见 scripts/migrate_v1_7_water.py
    SCHEMA_DIR / "v2_0_odds_timeline.sql",  # D1：timeline/snapshot/fetch_blob；数据见 scripts/import_odds_timeline_probe.py
]


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = Path(db_path or DB_PATH)
    if READONLY:  # 只读实例：一律 mode=ro（任何写语句 sqlite 直接报 readonly database）
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def split_sql(sql: str) -> list[str]:
    """Split a SQL script into complete statements (drops comment-only chunks)."""
    stmts: list[str] = []
    buf = ""

    def _flush(chunk: str) -> None:
        body = [l for l in chunk.splitlines() if l.strip() and not l.strip().startswith("--")]
        if body:
            stmts.append(chunk.strip())

    for line in sql.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            _flush(buf)
            buf = ""
    if buf.strip():
        _flush(buf)
    return stmts


def apply_sql_idempotent(conn: sqlite3.Connection, path: Path) -> int:
    """Run statements one by one; ignore 'duplicate column name' (ADD COLUMN 已存在). Returns skipped count."""
    skipped = 0
    for stmt in split_sql(path.read_text(encoding="utf-8")):
        if stmt.upper().startswith("PRAGMA"):
            continue
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e).lower():
                skipped += 1
                continue
            raise
    return skipped


def apply_migrations(conn: sqlite3.Connection) -> None:
    for p in MIGRATION_PATHS:
        apply_sql_idempotent(conn, p)
    conn.commit()


def apply_schema(conn: sqlite3.Connection, schema_path: Path | None = None) -> None:
    sql = (schema_path or SCHEMA_PATH).read_text(encoding="utf-8")
    conn.executescript(sql)
    conn.commit()
    # 重建库（seed / --reset-db）后补齐 v1.1 / v1.2 / v1.3，避免 API 读到缺列
    if schema_path is None or Path(schema_path) == SCHEMA_PATH:
        apply_migrations(conn)
