#!/usr/bin/env python3
"""day_skew 8 场 fixture 复核（只读证据 / 可选写标志）。

口径（足球分析师 2026-10-07）：
- 不改竞彩日规则
- 队名 + JC 号唯一 + 旧整点±12h 无同队替代 → mapping_ok → 保留 5DF 时刻并标 day_skew_confirmed
- 映射错才改 map 并回滚 kickoff
- 默认不写现网

用法：
  .venv/bin/python scripts/review_day_skew_kickoff.py report
  # 标志已由 executor 写入；本脚本主要用于复现证据摘要
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

import json
from pathlib import Path

REPORT = Path(str(_ODDS_DATA_DIR / "backfill/kickoff-minute-fill-2026-10-07/reports/day_skew_review.json"))

def main() -> int:
    if not REPORT.exists():
        print("missing", REPORT)
        return 1
    r = json.loads(REPORT.read_text(encoding="utf-8"))
    print(json.dumps({
        "all_verdict": r.get("all_verdict"),
        "n": r.get("n"),
        "action_summary": r.get("action_summary"),
        "production_modified": r.get("db", {}).get("production_modified"),
        "production_sha256": r.get("db", {}).get("production_sha256_after"),
        "rows": [
            {
                "match_id": x["match_id"],
                "mapping": x["mapping"],
                "fixture_id": x["fixture_id"],
                "kickoff_at": x["kickoff_at"],
                "known": x["kickoff_minute_known"],
                "day_skew_confirmed": x["day_skew_confirmed"],
                "rolled_back": x["rolled_back"],
            }
            for x in r.get("rows", [])
        ],
    }, ensure_ascii=False, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
