#!/usr/bin/env python3
"""多日/全日规则盘口缺口扫描 → rescue_queue/pending（R-C，不调 5DF）。

口径：schema/v2_0-live-miss-rescue-rules.md §7（S17+）
「应采未采」：plan 上每场规则 mid/close（及可选 real）目标已过 T+10min，且
  - capture_state 缺行，或 status=missed / 非成功态；或
  - 副本 odds_snapshot 对应 channel/point 无任一 CORE 书行。

例：
  python scripts/rescue_gap_scan.py --days 7 --dry-run
  python scripts/rescue_gap_scan.py --from 2026-10-08 --to 2026-10-09
  python scripts/rescue_gap_scan.py --days 7 --include-real
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
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import rescue_queue as rq  # noqa: E402

TZ = timezone(timedelta(hours=8))
LIVE = Path(str(_ODDS_DATA_DIR / "5dollar/live"))
D_PLAN = LIVE / "plan"
D_STATE = LIVE / "state"
REPORTS = LIVE / "rescue_queue" / "reports"
REPLICA_DB = Path(str(_V2D3_DB))
CORE_BOOKS = ("macau", "crown", "william", "pinnacle")
WIN_AFTER_MIN = 10
DEFAULT_DAYS = 7
SUCCESS = rq.SUCCESS_STATUSES


def now_cn() -> datetime:
    return datetime.now(TZ)


def iso(dt: datetime) -> str:
    return dt.astimezone(TZ).isoformat(timespec="seconds")


def parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def jload(p: Path, default=None):
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding="utf-8"))


def date_range(days: int | None, d_from: str | None, d_to: str | None) -> list[str]:
    today = now_cn().date()
    if d_from or d_to:
        a = date.fromisoformat(d_from) if d_from else today - timedelta(days=DEFAULT_DAYS - 1)
        b = date.fromisoformat(d_to) if d_to else today
        if b < a:
            a, b = b, a
        out = []
        cur = a
        while cur <= b:
            out.append(cur.isoformat())
            cur += timedelta(days=1)
        return out
    n = days if days is not None else DEFAULT_DAYS
    return [(today - timedelta(days=i)).isoformat() for i in range(n - 1, -1, -1)]


def load_plans(days: list[str]) -> list[dict]:
    plans = []
    for D in days:
        p = jload(D_PLAN / f"{D}.json")
        if p:
            plans.append(p)
    return plans


def snapshot_coverage(db: Path) -> dict[tuple[str, str, str], set[str]]:
    """(match_uid, channel, point) → {books present}。只读副本。"""
    out: dict[tuple[str, str, str], set[str]] = {}
    if not db.exists():
        return out
    conn = sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT m.match_uid, o.channel, o.point, o.book "
            "FROM odds_snapshot o JOIN matches m ON m.id=o.match_id "
            "WHERE o.market='asian' AND o.book IN (?,?,?,?)",
            CORE_BOOKS,
        ).fetchall()
        for uid, ch, pt, book in rows:
            out.setdefault((uid, ch, pt), set()).add(book)
    finally:
        conn.close()
    return out


def eligible_target(t: dict, *, include_real: bool) -> bool:
    if t.get("catchup") or t.get("phase") == "pending":
        return False
    var = t.get("phase_variant")
    pt = t.get("point")
    ch = t.get("channel")
    if var == "rule_1110" or pt == "rule_1110":
        return False
    if var == "rule" and pt in ("mid", "close") and ch == "rule":
        return True
    if include_real and var == "real" and pt in ("mid", "close", "t8", "t1"):
        return True
    return False


def classify_gap(
    t: dict,
    st: dict,
    cov: dict[tuple[str, str, str], set[str]],
    *,
    now: datetime,
) -> dict | None:
    """若应采未采，返回 gap 描述；否则 None。"""
    at = parse_dt(t.get("target_at"))
    if at is None:
        return None
    if now <= at + timedelta(minutes=WIN_AFTER_MIN):
        return None  # 仍在窗口内或未到，不算缺口

    tk = t["target_key"]
    entry = st.get(tk) or {}
    status = entry.get("status")
    ch, pt = t.get("channel"), t.get("point")
    books = cov.get((t["match_uid"], ch, pt), set())
    has_snap = bool(books & set(CORE_BOOKS))

    reasons = []
    if not entry:
        reasons.append("state_missing")
    elif status == "missed":
        reasons.append("status_missed")
    elif status not in SUCCESS:
        reasons.append(f"status_{status or 'unknown'}")
    if not has_snap and (t.get("phase_variant") == "rule" or ch == "rule"):
        # 规则通道：无 CORE 快照也算缺口（即便 state 误标 captured）
        if status in SUCCESS and status != "no_odds":
            reasons.append("odds_snapshot_empty_despite_success")
        elif "state_missing" in reasons or "status_missed" in reasons or not entry:
            reasons.append("odds_snapshot_empty")

    # 成功且有快照（或 no_odds 结案）→ 非缺口
    if status in SUCCESS and status != "no_odds" and has_snap and not any(
        r.startswith("odds_snapshot_empty") for r in reasons
    ):
        return None
    if status == "no_odds" and not any(r == "state_missing" for r in reasons):
        return None
    if not reasons:
        return None

    kick = parse_dt(t.get("kickoff_at"))
    reco_ok = rq.recommend_live_ok(t.get("kickoff_at"), now=now)
    return {
        **t,
        "gap_reasons": reasons,
        "state_status": status,
        "snapshot_books": sorted(books),
        # 兼容旧字段名：features_usable 实际表示 recommend_live_ok
        "features_usable": reco_ok,
        "recommend_live_ok": reco_ok,
        "lane": "R-A" if reco_ok else "R-D",  # R-D=晚补仍写规则格，仅关即时推荐
        "scenario": "S17",
    }


def scan(
    days: list[str],
    *,
    include_real: bool = False,
    db: Path = REPLICA_DB,
    now: datetime | None = None,
) -> dict:
    n = now or now_cn()
    plans = load_plans(days)
    cov = snapshot_coverage(db)
    gaps: list[dict] = []
    skipped_no_plan = [d for d in days if not (D_PLAN / f"{d}.json").exists()]
    per_day: dict[str, dict] = {}

    for plan in plans:
        D = plan["jingcai_date"]
        st = jload(D_STATE / f"captured_{D}.json", {}) or {}
        day_gaps = []
        n_eligible = 0
        for t in plan.get("targets") or []:
            if not eligible_target(t, include_real=include_real):
                continue
            n_eligible += 1
            g = classify_gap(t, st, cov, now=n)
            if g:
                day_gaps.append(g)
                gaps.append(g)
        per_day[D] = {
            "eligible": n_eligible,
            "gaps": len(day_gaps),
            "features_usable": sum(1 for g in day_gaps if g["features_usable"]),
            "recommend_live_off": sum(1 for g in day_gaps if not g["recommend_live_ok"]),
            "shadow_lane": sum(1 for g in day_gaps if not g["recommend_live_ok"]),  # 兼容旧名
        }

    # 排序：即时推荐仍可用优先 → kickoff 近 → mid → 日旧
    def sort_key(g: dict):
        usable = 0 if g["recommend_live_ok"] else 1
        kick = parse_dt(g.get("kickoff_at")) or n
        hours = max(0.0, (kick - n).total_seconds() / 3600.0)
        mid_first = 0 if g.get("point") in ("mid", "t8") else 1
        return (usable, hours, mid_first, g.get("jingcai_date") or "", g.get("target_key") or "")

    gaps.sort(key=sort_key)

    return {
        "schema": "live_rescue_gap_scan_v1",
        "scanned_at": iso(n),
        "days": days,
        "include_real": include_real,
        "skipped_no_plan": skipped_no_plan,
        "per_day": per_day,
        "n_gaps": len(gaps),
        "n_features_usable": sum(1 for g in gaps if g["recommend_live_ok"]),
        "n_recommend_live_ok": sum(1 for g in gaps if g["recommend_live_ok"]),
        "n_recommend_live_off": sum(1 for g in gaps if not g["recommend_live_ok"]),
        "n_shadow_lane": sum(1 for g in gaps if not g["recommend_live_ok"]),  # 兼容
        "gaps": [
            {
                "target_key": g["target_key"],
                "match_uid": g["match_uid"],
                "fixture_id": g.get("fixture_id"),
                "channel": g.get("channel"),
                "point": g.get("point"),
                "phase_variant": g.get("phase_variant"),
                "target_at": g.get("target_at"),
                "kickoff_at": g.get("kickoff_at"),
                "gap_reasons": g["gap_reasons"],
                "state_status": g.get("state_status"),
                "snapshot_books": g.get("snapshot_books"),
                "features_usable": g["recommend_live_ok"],
                "recommend_live_ok": g["recommend_live_ok"],
                "lane": g["lane"],
                "scenario": g["scenario"],
            }
            for g in gaps
        ],
        "_enqueue_targets": gaps,  # 内部：完整 tgt 供入队
    }


def apply_enqueue(report: dict) -> list[str]:
    targets = report.pop("_enqueue_targets", [])
    # 入队前剥内部字段，保留 plan 字段
    clean = []
    for g in targets:
        clean.append({
            "target_key": g["target_key"],
            "match_uid": g["match_uid"],
            "fixture_id": g.get("fixture_id"),
            "channel": g.get("channel"),
            "point": g.get("point"),
            "phase_variant": g.get("phase_variant"),
            "target_at": g.get("target_at"),
            "kickoff_at": g.get("kickoff_at"),
            "catchup": g.get("catchup", False),
            "phase": g.get("phase"),
        })
    written = rq.enqueue_gap_targets(clean, scenario="S17", reason="gap_scan_missing")
    return [str(p) for p in written]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=None, help=f"回溯天数（默认 {DEFAULT_DAYS}）")
    ap.add_argument("--from", dest="d_from", help="起始竞彩日 YYYY-MM-DD")
    ap.add_argument("--to", dest="d_to", help="结束竞彩日 YYYY-MM-DD")
    ap.add_argument("--include-real", action="store_true", help="同时扫真实 mid/close（R-F 占位入队）")
    ap.add_argument("--dry-run", action="store_true", help="只出报告，不入队")
    ap.add_argument("--db", default=str(REPLICA_DB))
    a = ap.parse_args(argv)

    days = date_range(a.days, a.d_from, a.d_to)
    if a.days is None and not a.d_from and not a.d_to:
        days = date_range(DEFAULT_DAYS, None, None)

    report = scan(days, include_real=a.include_real, db=Path(a.db))
    enqueued: list[str] = []
    if not a.dry_run:
        enqueued = apply_enqueue(report)
    else:
        report.pop("_enqueue_targets", None)

    report["dry_run"] = a.dry_run
    report["enqueued"] = len(enqueued)
    report["enqueued_paths"] = enqueued

    REPORTS.mkdir(parents=True, exist_ok=True)
    tag = "dryrun" if a.dry_run else "enqueue"
    out = REPORTS / f"gap_scan_{tag}_{now_cn():%Y%m%dT%H%M%S}.json"
    # 报告不写完整 paths 过长时可截断
    slim = {k: v for k, v in report.items() if k != "enqueued_paths"}
    slim["enqueued_paths_sample"] = enqueued[:20]
    out.write_text(json.dumps(slim, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report["report_path"] = str(out)

    print(json.dumps({
        "ok": True,
        "dry_run": a.dry_run,
        "days": days,
        "n_gaps": report["n_gaps"],
        "n_features_usable": report["n_features_usable"],
        "n_recommend_live_ok": report.get("n_recommend_live_ok"),
        "n_recommend_live_off": report.get("n_recommend_live_off"),
        "n_shadow_lane": report["n_shadow_lane"],
        "enqueued": len(enqueued),
        "per_day": report["per_day"],
        "report_path": str(out),
        "gap_keys_sample": [g["target_key"] for g in report["gaps"][:15]],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
