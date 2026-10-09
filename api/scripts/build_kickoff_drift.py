#!/usr/bin/env python3
"""生成 config/kickoff_drift.json（0.3.20；只读输入，不碰任何库）。

输入：推迟排查清单 postpone_scan_all.csv（列 db, match_uid, kickoff_at, first_inplay_at, first_inplay_minute,
est_kickoff_from_ticks, n_inplay）。同一 match_uid 优先取 v2d3 行，live 行不一致时报出来。
用法：.venv/bin/python scripts/build_kickoff_drift.py [--csv PATH] [--out config/kickoff_drift.json] [--dry-run]
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
import csv
import json
import shutil
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CSV = Path(str(_ODDS_DATA_DIR / "backfill/postpone-v2-2026-10-08/reports/postpone_scan_all.csv"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=str(DEFAULT_CSV))
    ap.add_argument("--out", default=str(ROOT / "config" / "kickoff_drift.json"))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    rows = list(csv.DictReader(open(a.csv, encoding="utf-8")))
    items: dict[str, dict] = {}
    mismatch = []
    for pref in ("v2d3", "live"):
        for r in rows:
            if r["db"] != pref or not r.get("est_kickoff_from_ticks") or not r.get("first_inplay_at"):
                continue
            it = {"est_kickoff_at": r["est_kickoff_from_ticks"], "first_inplay_at": r["first_inplay_at"],
                  "first_inplay_minute": int(r["first_inplay_minute"]) if r.get("first_inplay_minute") else None,
                  "n_inplay": int(r["n_inplay"]) if r.get("n_inplay") else None, "src_db": pref}
            uid = r["match_uid"]
            if uid in items:
                if items[uid]["est_kickoff_at"] != it["est_kickoff_at"]:
                    mismatch.append(uid)
                continue
            items[uid] = it
    out = {"generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
           "generated_from": a.csv, "method": "est_kickoff_at = first macau in-play tick − match minute (≈1 min precision)",
           "info_only": True, "n": len(items), "mismatch_live_vs_v2d3": mismatch,
           "items": dict(sorted(items.items()))}
    print(json.dumps({k: v for k, v in out.items() if k != "items"}, ensure_ascii=False))
    if a.dry_run:
        return 0
    op = Path(a.out)
    if op.exists():
        shutil.copy2(op, op.with_name(op.name + ".bak-" + datetime.now().strftime("%Y%m%dT%H%M%S")))
    op.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
