#!/usr/bin/env python3
"""0.3.18 第二批口径 B：v2d3 副本 matches 加一列 kickoff_jc，存竞彩官方开赛时刻（只读竞彩月度 JSON，不改 kickoff_at）。

- 竞彩 JSON 只有整点 kickoff_hour、没有日期：日期取 D 或 D+1 中离 matches.kickoff_at 最近者
  （0–11 点按早场惯例默认 D+1；世界杯 12:00 场竞彩时刻为 D+1 12:00，同此取法）。日期只是把整点落到自然日，
  竞彩日归属仍按编号（jingcai_date），不由它反推。
- 找不到竞彩 JSON 条目（无编号 / 探针场）→ NULL。
- 拒绝写现网 data/app.db；先导出前后清单。
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
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.collection_schedule import TZ_CN  # noqa: E402

LIVE_DB = (ROOT / "data" / "app.db").resolve()
JC_DIR = Path(str(_ODDS_DATA_DIR / "imports/2026"))


def load_jc() -> dict[tuple[str, str], int]:
    out = {}
    for f in sorted(JC_DIR.glob("*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        ms = d if isinstance(d, list) else d.get("matches") or d.get("items") or []
        for x in ms:
            m = x.get("match", x)
            jc = (m.get("jc") or {}).get("id")
            if jc and m.get("date") and isinstance(m.get("kickoff_hour"), int):
                out[(m["date"], jc)] = m["kickoff_hour"]
    return out


def jc_time(d: str, h: int, kick: datetime | None) -> datetime:
    base = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=TZ_CN, hour=h)
    cands = [base, base + timedelta(days=1)]
    if kick is None:
        return cands[1] if h <= 11 else cands[0]
    return min(cands, key=lambda c: (abs((c - kick).total_seconds()), c != (cands[1] if h <= 11 else cands[0])))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    db = Path(a.db).resolve()
    if db == LIVE_DB:
        raise SystemExit("拒绝写现网 data/app.db")
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    cols = {r[1] for r in conn.execute("PRAGMA table_info(matches)")}
    added = False
    if "kickoff_jc" not in cols and not a.dry_run:  # DDL 会隐式提交，dry-run 不建列
        conn.execute("ALTER TABLE matches ADD COLUMN kickoff_jc TEXT")
        added = True
    jc = load_jc()
    rows = []
    kj = "kickoff_jc" if ("kickoff_jc" in cols or added) else "NULL AS kickoff_jc"
    for r in conn.execute(f"SELECT id, match_uid, jingcai_date, jc_id, kickoff_at, kickoff_hour, {kj} FROM matches ORDER BY id").fetchall():
        h = jc.get((r["jingcai_date"], r["jc_id"])) if r["jc_id"] else None
        kick = datetime.fromisoformat(r["kickoff_at"]).astimezone(TZ_CN) if r["kickoff_at"] else None
        new = jc_time(r["jingcai_date"], h, kick).isoformat() if h is not None else None
        diff_min = round((kick - datetime.fromisoformat(new)).total_seconds() / 60) if (new and kick) else None
        rows.append({"match_id": r["id"], "match_uid": r["match_uid"], "kickoff_at": r["kickoff_at"],
                     "jc_hour": h, "kickoff_jc_before": r["kickoff_jc"] if not added else None,
                     "kickoff_jc_after": new, "kickoff_minus_jc_min": diff_min})
        if not a.dry_run:
            conn.execute("UPDATE matches SET kickoff_jc=? WHERE id=?", (new, r["id"]))
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    with open(a.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    stats = {"column_added": added, "rows": len(rows), "filled": sum(1 for x in rows if x["kickoff_jc_after"]),
             "null": sum(1 for x in rows if not x["kickoff_jc_after"]),
             "differs_from_kickoff_at": [(x["match_uid"], x["kickoff_at"], x["kickoff_jc_after"], x["kickoff_minus_jc_min"])
                                         for x in rows if x["kickoff_minus_jc_min"] not in (None, 0)],
             "dry_run": a.dry_run}
    if a.dry_run:
        conn.rollback()
    else:
        conn.commit()
    conn.close()
    print(json.dumps(stats, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
