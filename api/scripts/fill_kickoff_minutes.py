#!/usr/bin/env python3
"""从 5DF fixture 映射回填开赛分钟到副本（默认 v2d3）。

口径（2026-10-07 与足球分析师对齐）：
- 源：backfill/macau_mid_water_fixture_map.json 的 fixture_ko（已由 5DF kickoff 映射，
  本包优先用映射，不强制再打 API；缺字段才可选 --refetch，限速 ≤30/min）
- 写回副本 matches.kickoff_at（北京 +08:00 ISO，截到分钟）+ kickoff_minute_known=1
- 仅当源有明确分钟（含确认 :00）才标 known；本映射每条均有 fixture_ko → 可标
- 冲突：|新−旧| ≥ 5min → 记日志；副本策略=写入新值并标 conflict（现网永不改）
- 同步更新 kickoff_hour = 新 kickoff_at 的北京小时（排序兼容）
- 不碰 predictions；拒绝默认写现网

用法：
  .venv/bin/python scripts/fill_kickoff_minutes.py apply --db data/v2d3/app.db
  .venv/bin/python scripts/fill_kickoff_minutes.py dry-run --db data/v2d3/app.db
  .venv/bin/python scripts/fill_kickoff_minutes.py report --db data/v2d3/app.db
  .venv/bin/python scripts/fill_kickoff_minutes.py scan-weird --db data/v2d3/app.db
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
import hashlib
import json
import os
import re
import sqlite3
import sys
import unicodedata
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAP_PATH = Path(str(_ODDS_DATA_DIR / "backfill/macau_mid_water_fixture_map.json"))
OUT_DIR = Path(str(_ODDS_DATA_DIR / "backfill/kickoff-minute-fill-2026-10-07"))
LOG_DIR = OUT_DIR / "logs"
REPORT_DIR = OUT_DIR / "reports"
DEFAULT_DB = ROOT / "data" / "v2d3" / "app.db"
PROD_DB = ROOT / "data" / "app.db"
TZ_CN = timezone(timedelta(hours=8))
CONFLICT_MIN = 5.0
SOURCE = "5df_fixture_map_ko"
sys.path.insert(0, str(ROOT / "scripts"))
from validate_kickoff_placeholder import plan_check  # noqa: E402  (0.3.17 导入前占位符校验)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse_dt(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ_CN)
    return dt.astimezone(TZ_CN)


def _to_minute_iso(dt: datetime) -> tuple[str, bool]:
    """Truncate to minute in +08:00. Returns (iso, seconds_were_nonzero)."""
    dt = dt.astimezone(TZ_CN)
    truncated = seconds_nonzero = dt.second != 0 or dt.microsecond != 0
    dt2 = dt.replace(second=0, microsecond=0)
    # normalize offset notation to +08:00
    return dt2.isoformat(), truncated


def _is_prod_path(db: Path) -> bool:
    try:
        return db.resolve() == PROD_DB.resolve()
    except FileNotFoundError:
        return str(db).endswith("/data/app.db") and "v2d" not in str(db)


def _load_map(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data.get("items") or []
    if len(items) != 177:
        print(f"WARN: map items={len(items)} (expect 177)", file=sys.stderr)
    return items


def _plan(items: list[dict], con: sqlite3.Connection) -> list[dict]:
    con.row_factory = sqlite3.Row
    by_id = {
        r["id"]: r
        for r in con.execute(
            "SELECT id, match_uid, jingcai_date, kickoff_at, kickoff_minute_known, kickoff_hour, "
            "home_team, away_team FROM matches"
        )
    }
    plans: list[dict] = []
    for it in items:
        mid = int(it["match_id"])
        row = by_id.get(mid)
        if row is None:
            plans.append(
                {
                    "match_id": mid,
                    "match_uid": it.get("match_uid"),
                    "action": "skip_missing_row",
                    "reason": "match_id not in db",
                }
            )
            continue
        fixture_ko = it.get("fixture_ko") or it.get("kickoff_utc")
        if not fixture_ko:
            plans.append(
                {
                    "match_id": mid,
                    "match_uid": row["match_uid"],
                    "action": "skip_no_source",
                    "reason": "no fixture_ko",
                }
            )
            continue
        new_dt = _parse_dt(fixture_ko)
        new_iso, sec_trunc = _to_minute_iso(new_dt)
        old_iso = row["kickoff_at"]
        old_dt = _parse_dt(old_iso) if old_iso else None
        diff_min = (
            abs((new_dt.replace(second=0, microsecond=0) - old_dt.replace(second=0, microsecond=0)).total_seconds())
            / 60.0
            if old_dt
            else None
        )
        conflict = diff_min is not None and diff_min >= CONFLICT_MIN
        if conflict and diff_min >= 1440:
            conflict_class = "day_skew"
        elif conflict and diff_min >= 60:
            conflict_class = "hour_skew"
        elif conflict:
            conflict_class = "minute_skew"
        else:
            conflict_class = None
        new_hour = int(new_iso[11:13])
        # 0.3.17 导入前校验：5DF 12:00 占位符（对竞彩官方整点 = 回填前的 kickoff_hour）
        ko_chk = plan_check(new_dt, row["jingcai_date"], row["kickoff_hour"])
        if ko_chk["hold"]:
            plans.append({"match_id": mid, "match_uid": row["match_uid"], "fixture_id": it.get("fixture_id"),
                          "old_kickoff_at": old_iso, "new_kickoff_at": new_iso,
                          "action": "hold_kickoff_placeholder", "kickoff_check": ko_chk, "source": SOURCE})
            continue
        already = (
            row["kickoff_minute_known"] == 1
            and old_iso == new_iso
            and int(row["kickoff_hour"] or -1) == new_hour
        )
        plans.append(
            {
                "match_id": mid,
                "match_uid": row["match_uid"],
                "home_team": row["home_team"],
                "away_team": row["away_team"],
                "fixture_id": it.get("fixture_id"),
                "old_kickoff_at": old_iso,
                "new_kickoff_at": new_iso,
                "old_minute_known": row["kickoff_minute_known"],
                "old_kickoff_hour": row["kickoff_hour"],
                "new_kickoff_hour": new_hour,
                "diff_min": round(diff_min, 3) if diff_min is not None else None,
                "conflict": conflict,
                "conflict_class": conflict_class,
                "seconds_truncated": sec_trunc,
                "source_raw": fixture_ko,
                "action": "noop" if already else "update",
                "source": SOURCE,
                "kickoff_check": ko_chk["status"],
            }
        )
    return plans


def cmd_dry_or_apply(args: argparse.Namespace, apply: bool) -> int:
    db = args.db
    if _is_prod_path(db) and not args.i_know_this_is_production:
        print("REFUSE: will not write production data/app.db (DUAL_WRITE still off)", file=sys.stderr)
        return 2
    if _is_prod_path(db):
        print("REFUSE even with flag for this pack: kickoff fill is replica-only", file=sys.stderr)
        return 2

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    items = _load_map(Path(args.map))
    prod_sha_before = _sha256(PROD_DB) if PROD_DB.exists() else None
    v2_sha_before = _sha256(db) if db.exists() else None

    con = sqlite3.connect(db)
    plans = _plan(items, con)
    updates = [p for p in plans if p["action"] == "update"]
    conflicts = [p for p in plans if p.get("conflict")]
    skips = [p for p in plans if p["action"].startswith("skip")]
    noops = [p for p in plans if p["action"] == "noop"]

    stamp = datetime.now(TZ_CN).strftime("%Y%m%dT%H%M%S%z")
    plan_path = LOG_DIR / f"kickoff_plan_{stamp}.jsonl"
    with plan_path.open("w", encoding="utf-8") as f:
        for p in plans:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")

    conflict_csv = REPORT_DIR / "kickoff_conflicts.csv"
    with conflict_csv.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "match_id",
                "match_uid",
                "home_team",
                "away_team",
                "old_kickoff_at",
                "new_kickoff_at",
                "diff_min",
                "conflict_class",
                "fixture_id",
                "action",
            ],
        )
        w.writeheader()
        for p in conflicts:
            w.writerow({k: p.get(k) for k in w.fieldnames})

    if apply:
        now = datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M:%S")
        cur = con.cursor()
        for p in updates:
            cur.execute(
                """
                UPDATE matches
                   SET kickoff_at = ?,
                       kickoff_minute_known = 1,
                       kickoff_hour = ?,
                       updated_at = ?
                 WHERE id = ?
                """,
                (p["new_kickoff_at"], p["new_kickoff_hour"], now, p["match_id"]),
            )
        con.commit()

    # post stats
    con.row_factory = sqlite3.Row
    known = dict(
        con.execute(
            "SELECT kickoff_minute_known, COUNT(*) FROM matches "
            "WHERE match_uid NOT LIKE 'probe:%' GROUP BY 1"
        )
    )
    minute_dist = Counter()
    for (ko,) in con.execute(
        "SELECT kickoff_at FROM matches WHERE match_uid NOT LIKE 'probe:%'"
    ):
        if ko and "T" in ko:
            hm = ko.split("T")[1].split("+")[0].split("-")[0]
            minute_dist[hm.split(":")[1] if ":" in hm else "?"] += 1

    prod_sha_after = _sha256(PROD_DB) if PROD_DB.exists() else None
    v2_sha_after = _sha256(db)

    summary = {
        "task": "kickoff-minute-fill",
        "mode": "apply" if apply else "dry-run",
        "finished_at": datetime.now(TZ_CN).isoformat(),
        "db": str(db),
        "map": str(args.map),
        "source": SOURCE,
        "policy": {
            "replica_conflict": "write_new_and_log",
            "conflict_threshold_min": CONFLICT_MIN,
            "known_rule": "mark known=1 when fixture_ko present (incl. :00)",
            "seconds": "truncate_to_minute",
            "production": "never_modified",
            "predictions": "untouched",
        },
        "mapped": len(items),
        "planned_updates": len(updates),
        "noop": len(noops),
        "skips": len(skips),
        "conflicts": len(conflicts),
        "conflict_classes": dict(Counter(p["conflict_class"] for p in conflicts)),
        "seconds_truncated": sum(1 for p in plans if p.get("seconds_truncated")),
        "after_known": {str(k): v for k, v in known.items()},
        "after_minute_dist": dict(minute_dist),
        "prod_sha256_before": prod_sha_before,
        "prod_sha256_after": prod_sha_after,
        "prod_unchanged": prod_sha_before == prod_sha_after,
        "v2d3_sha256_before": v2_sha_before,
        "v2d3_sha256_after": v2_sha_after,
        "plan_log": str(plan_path),
        "conflict_csv": str(conflict_csv),
    }
    summary_path = REPORT_DIR / ("summary_apply.json" if apply else "summary_dryrun.json")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    con.close()
    return 0


def _weird_flags(s: str) -> list[str]:
    out: list[str] = []
    if s != s.strip():
        out.append("leading_or_trailing_ws")
    if re.search(r"[\t\r\n\u00a0\u3000\u200b\u200c\u200d\ufeff]", s):
        out.append("special_ws")
    if "  " in s:
        out.append("double_space")
    has_hw = bool(re.search(r"[\u0021-\u007e]", s))
    has_fw = bool(re.search(r"[\uff01-\uff5e]", s))
    if has_hw and has_fw:
        out.append("half_full_mix")
    if re.search(r"[\uff10-\uff19\uff21-\uff3a\uff41-\uff5a]", s):
        out.append("fullwidth_alnum")
    if "\ufffd" in s:
        out.append("replacement_char")
    if re.search(r"Ã.|Â.|ðŸ", s):
        out.append("mojibake_hint")
    if any(unicodedata.category(c).startswith("C") for c in s):
        out.append("control_or_format")
    if re.search(r"[\ue000-\uf8ff]", s):
        out.append("private_use")
    if re.search(r"&[#a-zA-Z0-9]+;|<[^>]+>", s):
        out.append("htmlish")
    if s != unicodedata.normalize("NFC", s):
        out.append("not_nfc")
    if re.search(r"[\u4e00-\u9fff]\s+[\u4e00-\u9fff]", s):
        out.append("space_between_cjk")
    # review-only (not necessarily errors)
    if re.search(r"[A-Za-z]", s) and re.search(r"[\u4e00-\u9fff]", s):
        out.append("review_latin_cjk_mix")
    if re.search(r"(?:^|\s)(?:FC|SK|HD|CF)\b|\b(?:FC|SK|HD|CF)(?:\s|$)", s) or re.search(
        r"FC|SK FC|\bHD\b", s
    ):
        if re.search(r"[A-Za-z]", s):
            out.append("review_brand_token")
    return out


def cmd_scan_weird(args: argparse.Namespace) -> int:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    rows = list(
        con.execute(
            "SELECT id, match_uid, home_team, away_team, competition_name, "
            "competition_type, competition_stage FROM matches ORDER BY id"
        )
    )
    findings: list[dict] = []
    for r in rows:
        for field in (
            "home_team",
            "away_team",
            "competition_name",
            "competition_type",
            "competition_stage",
        ):
            val = r[field]
            if not val:
                continue
            flags = _weird_flags(val)
            # only keep real issues + review_* for inventory
            if flags:
                severity = (
                    "review"
                    if all(f.startswith("review_") for f in flags)
                    else "suspect"
                )
                findings.append(
                    {
                        "table": "matches",
                        "match_id": r["id"],
                        "match_uid": r["match_uid"],
                        "field": field,
                        "value": val,
                        "flags": "|".join(flags),
                        "severity": severity,
                    }
                )

    # aliases uniqueness / drift (informational)
    drift = list(
        con.execute(
            """
            SELECT 'home' AS side, m.id AS match_id, m.match_uid, m.home_team AS value,
                   t.name_zh_canonical AS canonical
              FROM matches m
              JOIN team_aliases a ON a.alias = m.home_team
              JOIN teams t ON t.id = a.team_id
             WHERE t.name_zh_canonical <> m.home_team
            UNION ALL
            SELECT 'away', m.id, m.match_uid, m.away_team, t.name_zh_canonical
              FROM matches m
              JOIN team_aliases a ON a.alias = m.away_team
              JOIN teams t ON t.id = a.team_id
             WHERE t.name_zh_canonical <> m.away_team
            """
        )
    )
    for d in drift:
        findings.append(
            {
                "table": "matches",
                "match_id": d["match_id"],
                "match_uid": d["match_uid"],
                "field": f"{d['side']}_team_alias_drift",
                "value": f"{d['value']} → {d['canonical']}",
                "flags": "alias_drift",
                "severity": "review",
            }
        )

    csv_path = REPORT_DIR / "weird_chars_inventory.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "table",
                "match_id",
                "match_uid",
                "field",
                "value",
                "flags",
                "severity",
            ],
        )
        w.writeheader()
        for row in findings:
            w.writerow(row)

    md_path = REPORT_DIR / "weird_chars_inventory.md"
    suspect = [x for x in findings if x["severity"] == "suspect"]
    review = [x for x in findings if x["severity"] == "review"]
    lines = [
        "# 旧手工怪字符扫描（v2d3 · 2026-10-07）",
        "",
        f"- DB: `{args.db}`",
        f"- 可疑（乱码/半全角/异常空白等）: **{len(suspect)}**",
        f"- 仅待审（拉丁+中文混排 / 品牌词 FC·SK·HD 等）: **{len(review)}**",
        f"- 本包**不批量改名**；清单见 `{csv_path}`",
        "",
        "## 可疑",
        "",
    ]
    if not suspect:
        lines.append("（无）")
    else:
        lines.append("| match_id | field | value | flags |")
        lines.append("|---|---|---|---|")
        for x in suspect:
            lines.append(
                f"| {x['match_id']} | {x['field']} | {x['value']} | {x['flags']} |"
            )
    lines += ["", "## 待审（非错误）", "", "| match_id | field | value | flags |", "|---|---|---|---|"]
    # unique values for review
    seen = set()
    for x in review:
        key = (x["field"], x["value"])
        if key in seen:
            continue
        seen.add(key)
        lines.append(
            f"| {x['match_id']} | {x['field']} | {x['value']} | {x['flags']} |"
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    summary = {
        "suspect": len(suspect),
        "review": len(review),
        "csv": str(csv_path),
        "md": str(md_path),
        "unique_review_values": sorted({x["value"] for x in review}),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    con.close()
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    base = list(
        con.execute(
            "SELECT kickoff_minute_known, COUNT(*) c FROM matches "
            "WHERE match_uid NOT LIKE 'probe:%' GROUP BY 1"
        )
    )
    mins = Counter()
    for (ko,) in con.execute(
        "SELECT kickoff_at FROM matches WHERE match_uid NOT LIKE 'probe:%'"
    ):
        if ko and "T" in ko:
            hm = ko.split("T")[1].split("+")[0].split("-")[0]
            mins[hm.split(":")[1] if ":" in hm else "?"] += 1
    prod = sqlite3.connect(PROD_DB)
    prod_known = list(
        prod.execute(
            "SELECT kickoff_minute_known, COUNT(*) FROM matches GROUP BY 1"
        )
    )
    out = {
        "v2d3_known": {str(r[0]): r[1] for r in base},
        "v2d3_minute_dist": dict(mins),
        "prod_known": {str(r[0]): r[1] for r in prod_known},
        "prod_sha256": _sha256(PROD_DB),
        "v2d3_sha256": _sha256(args.db),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "command",
        choices=["dry-run", "apply", "report", "scan-weird"],
    )
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--map", type=Path, default=MAP_PATH)
    ap.add_argument(
        "--i-know-this-is-production",
        action="store_true",
        help="ignored/refused for this pack (replica-only)",
    )
    args = ap.parse_args()
    if args.command == "dry-run":
        return cmd_dry_or_apply(args, apply=False)
    if args.command == "apply":
        return cmd_dry_or_apply(args, apply=True)
    if args.command == "report":
        return cmd_report(args)
    if args.command == "scan-weird":
        return cmd_scan_weird(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
