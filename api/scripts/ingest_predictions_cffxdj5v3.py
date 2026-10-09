#!/usr/bin/env python3
"""Ingest CFFXDJ_5_V3 predictions.csv into match-analysis SQLite.

Source: 作者的私有分析仓 01_分析集/.../predictions.csv
Only writes rows whose match_uid exists in DB (typically 2606/2607 imports).
Never invents directions for unmatched CSV rows or DB rows without CSV.
Empty 建议方向 → 不下注.
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
from pathlib import Path

STRATEGY = "CFFXDJ_5_V3"
SETTLE_BOOK = "macau_close"
DEFAULT_CSV = Path(str(_ODDS_DATA_DIR / "imports/predictions/CFFXDJ_5_V3_predictions.csv"))
DEFAULT_DB = Path(str(_APP_DB))

DIR_MAP = {
    "主": "主",
    "客": "客",
    "不下注": "不下注",
    "home": "主",
    "away": "客",
    "skip": "不下注",
    "none": "不下注",
    "no_bet": "不下注",
}


def normalize_direction(raw: str | None) -> str:
    s = (raw or "").strip()
    if not s:
        return "不下注"
    if s in DIR_MAP:
        return DIR_MAP[s]
    raise ValueError(f"unknown direction: {raw!r}")


def parse_confidence(raw: str | None) -> float | None:
    s = (raw or "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def build_rationale(row: dict) -> list[str]:
    parts: list[str] = []
    bucket = (row.get("bucket_key") or "").strip()
    if bucket:
        parts.append(f"bucket={bucket}")
    dsum = (row.get("dir_sum(5)") or "").strip()
    if dsum:
        parts.append(f"dir_sum={dsum}")
    for col in (
        "FMAAH_V2_dir",
        "FSLREG_V2_2_dir",
        "FAWLE_V1_dir",
        "FWAHB_V2_dir",
        "FCAHB_V2_dir",
    ):
        v = (row.get(col) or "").strip()
        if v and v not in ("0", "0.0"):
            short = col.replace("_dir", "")
            parts.append(f"{short}={v}")
    return parts


def match_uid_from_row(row: dict) -> str:
    date = (row.get("日期") or "").strip()
    jc = (row.get("竞彩编号") or "").strip()
    if not date or not jc:
        raise ValueError(f"missing date/jc: {row.get('uid')}")
    return f"{date}|{jc}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument(
        "--months",
        default="2606,2607",
        help="Comma-separated YYMM filters (empty = all months in CSV)",
    )
    ap.add_argument(
        "--skip-existing",
        action="store_true",
        help="If (match_id, strategy) already exists, skip entirely (no UPDATE)",
    )
    args = ap.parse_args()

    months = {m.strip() for m in args.months.split(",") if m.strip()}

    if not args.csv.is_file():
        print(f"CSV not found: {args.csv}", file=sys.stderr)
        return 1
    if not args.db.is_file():
        print(f"DB not found: {args.db}", file=sys.stderr)
        return 1

    with args.csv.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        cols = reader.fieldnames or []
        all_rows = list(reader)

    if months:
        scoped = [r for r in all_rows if (r.get("month") or "").strip() in months]
    else:
        scoped = all_rows

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    uid_to_id = {
        row["match_uid"]: row["id"]
        for row in conn.execute("SELECT id, match_uid FROM matches")
    }

    existing = {
        row["match_id"]
        for row in conn.execute(
            "SELECT match_id FROM predictions WHERE strategy = ?",
            (STRATEGY,),
        )
    }

    matched = 0
    inserted = 0
    skipped = 0
    unmatched = 0
    unmatched_examples: list[str] = []
    dir_counts: dict[str, int] = {}
    samples: list[tuple[str, str, float | None]] = []

    try:
        for row in scoped:
            try:
                uid = match_uid_from_row(row)
            except ValueError as e:
                unmatched += 1
                if len(unmatched_examples) < 5:
                    unmatched_examples.append(str(e))
                continue

            mid = uid_to_id.get(uid)
            if mid is None:
                unmatched += 1
                if len(unmatched_examples) < 5:
                    unmatched_examples.append(uid)
                continue

            if args.skip_existing and mid in existing:
                skipped += 1
                continue

            direction = normalize_direction(row.get("建议方向"))
            confidence = parse_confidence(row.get("置信度(|均值|)"))
            rationale = build_rationale(row)
            settle = (row.get("settle_line") or "").strip() or SETTLE_BOOK

            if args.skip_existing:
                conn.execute(
                    """
                    INSERT INTO predictions (
                      match_id, strategy, direction, settle_book, rationale_json, confidence
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        mid,
                        STRATEGY,
                        direction,
                        settle,
                        json.dumps(rationale, ensure_ascii=False),
                        confidence,
                    ),
                )
                existing.add(mid)
                inserted += 1
            else:
                conn.execute(
                    """
                    INSERT INTO predictions (
                      match_id, strategy, direction, settle_book, rationale_json, confidence
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(match_id, strategy) DO UPDATE SET
                      direction = excluded.direction,
                      settle_book = excluded.settle_book,
                      rationale_json = excluded.rationale_json,
                      confidence = excluded.confidence,
                      produced_at = datetime('now')
                    """,
                    (
                        mid,
                        STRATEGY,
                        direction,
                        settle,
                        json.dumps(rationale, ensure_ascii=False),
                        confidence,
                    ),
                )
                inserted += 1  # upsert path counts as write; kept for compat

            matched += 1
            dir_counts[direction] = dir_counts.get(direction, 0) + 1
            if len(samples) < 5:
                samples.append((uid, direction, confidence))

        conn.commit()
    finally:
        conn.close()

    print("=== ingest CFFXDJ_5_V3 ===")
    print(f"csv: {args.csv} ({args.csv.stat().st_size} bytes, {len(all_rows)} rows)")
    print(f"columns: {cols}")
    print(f"month filter: {sorted(months) if months else 'ALL'}")
    print(f"skip_existing: {args.skip_existing}")
    print(f"scoped rows: {len(scoped)}")
    print(f"matched written: {matched}")
    print(f"inserted: {inserted}")
    print(f"skipped existing: {skipped}")
    print(f"unmatched (no DB match_uid): {unmatched}")
    if unmatched_examples:
        print(f"unmatched examples: {unmatched_examples}")
    print(f"direction counts: {dir_counts}")
    print(f"samples: {samples}")
    print(f"strategy={STRATEGY} settle_book default={SETTLE_BOOK}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
