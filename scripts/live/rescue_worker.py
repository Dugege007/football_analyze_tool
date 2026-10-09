#!/usr/bin/env python3
"""rescue_queue 消费 worker：空闲窗 as-of 补写（不堵 live due）。

口径：schema/v2_0-live-miss-rescue-rules.md
- 让路窗 + 11:00–11:20 不消费
- 下一 due 目标前 15min 跳过（避免抢 live 配额）
- 开赛后 >3h → R-D 晚补：仍 as-of 写入规则格（sim/features 可用），recommend_live_ok=false
- 规则 mid/close → 调 asof_backfill_mid_rule（R-A / R-D）
- 真实通道：暂 defer（R-F 未接线），留 pending 并记日志

例：
  python scripts/rescue_worker.py --dry-run
  python scripts/rescue_worker.py --max 5
  python scripts/rescue_worker.py --once   # 同默认：跑一轮
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import rescue_queue as rq  # noqa: E402

TZ = timezone(timedelta(hours=8))
LIVE = Path(str(_ODDS_DATA_DIR / "5dollar/live"))
D_PLAN = LIVE / "plan"
D_STATE = LIVE / "state"
D_LOG = LIVE / "logs"
QLOG = rq.QROOT / "logs" / "consume.jsonl"
REPORTS = rq.QROOT / "reports"
REPLICA_DB = Path(str(_V2D3_DB))
DUE_AVOID_MIN = 15
DEFAULT_MAX_PER_RUN = 8
DEFAULT_DAILY_CAP = 40
SHADOW_HOURS = rq.SHADOW_HOURS_AFTER_KICKOFF


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


def jdump(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def append_consume_log(obj: dict) -> None:
    QLOG.parent.mkdir(parents=True, exist_ok=True)
    with QLOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def daily_done_count(day: str | None = None) -> int:
    D = day or now_cn().date().isoformat()
    n = 0
    if not QLOG.exists():
        return 0
    for line in QLOG.read_text(encoding="utf-8").splitlines():
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        if o.get("event") in ("rescued", "rescue_late", "rescue_shadow", "rescue_no_odds") and str(o.get("at", "")).startswith(D):
            n += 1
    return n


def nearest_due_seconds(now: datetime) -> float | None:
    """扫本地 plan（昨/今/明）找最近未完成目标距 now 的秒数。"""
    best = None
    for off in (-1, 0, 1):
        D = (now.date() + timedelta(days=off)).isoformat()
        plan = jload(D_PLAN / f"{D}.json")
        if not plan:
            continue
        st = jload(D_STATE / f"captured_{D}.json", {}) or {}
        for t in plan.get("targets") or []:
            if t["target_key"] in st:
                continue
            at = parse_dt(t.get("target_at"))
            kick = parse_dt(t.get("kickoff_at"))
            if at is None:
                continue
            if kick and now >= kick:
                continue
            sec = (at - now).total_seconds()
            if sec < -600:
                continue  # 已过窗口太久，due_targets 会变 missed
            if best is None or abs(sec) < abs(best):
                # 用「还有多久到 T」；已进入窗口的用负值表示紧迫
                best = sec
    return best


def should_skip_for_due(now: datetime) -> str | None:
    sec = nearest_due_seconds(now)
    if sec is None:
        return None
    # 进入窗口或距 T ≤ 15min
    if -600 <= sec <= DUE_AVOID_MIN * 60:
        return f"due_avoid_sec={int(sec)}"
    return None


def is_rule_asof_item(it: dict) -> bool:
    var = it.get("phase_variant") or ""
    ch = it.get("channel") or ""
    pt = it.get("point") or ""
    return (var == "rule" or ch == "rule") and pt in ("mid", "close")


def is_real_item(it: dict) -> bool:
    var = it.get("phase_variant") or ""
    ch = it.get("channel") or ""
    return var == "real" or ch == "actual"


def run_asof(D: str, target_key: str, db: Path, dry_run: bool,
             recommend_live_ok: bool | None = None) -> dict:
    from asof_backfill_mid_rule import run as asof_run  # noqa: WPS433
    return asof_run(D, [target_key], False, db, dry_run, False,
                    recommend_live_ok=recommend_live_ok)


def process_one(path: Path, it: dict, *, db: Path, dry_run: bool, now: datetime) -> dict:
    tk = it.get("target_key") or ""
    D = it.get("jingcai_date") or (tk.split("|")[0] if tk else "")
    result = {"target_key": tk, "path": str(path)}

    if is_real_item(it) and not is_rule_asof_item(it):
        # R-F 未接线：留 pending，记 defer
        it["attempts"] = int(it.get("attempts") or 0)
        it["notes"] = (it.get("notes") or "") + "|defer_R-F_not_wired"
        if not dry_run:
            path.write_text(json.dumps(it, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        result["action"] = "defer_real_R-F"
        append_consume_log({"at": iso(now), "event": "rescue_deferred", "target_key": tk,
                            "reason": "R-F_not_wired"})
        return result

    # 开赛后 >3h：R-D 晚补标签（仍写规则格；禁即时推荐；sim/features 仍可用）
    reco_ok = rq.recommend_live_ok(it.get("kickoff_at"), now=now, hours=SHADOW_HOURS)
    rescue_rule = "R-A" if reco_ok else "R-D"
    result["recommend_live_ok"] = reco_ok
    result["rescue_rule"] = rescue_rule

    if dry_run:
        result["action"] = "would_asof_late" if not reco_ok else "would_asof"
        return result

    try:
        rep = run_asof(D, tk, db, dry_run=False, recommend_live_ok=reco_ok)
    except Exception as e:
        attempts = int(it.get("attempts") or 0) + 1
        it["attempts"] = attempts
        it["last_error"] = str(e)[:300]
        path.write_text(json.dumps(it, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        max_a = int(it.get("max_attempts") or 5)
        if attempts >= max_a:
            rq.move_item(path, rq.FAILED, status="failed",
                         extra={"last_error": it["last_error"]})
            append_consume_log({"at": iso(now), "event": "rescue_failed", "target_key": tk,
                                "error": it["last_error"]})
            result["action"] = "failed"
        else:
            append_consume_log({"at": iso(now), "event": "rescue_deferred", "target_key": tk,
                                "error": it["last_error"], "attempts": attempts})
            result["action"] = "retry_later"
        return result

    # 看目标结果
    treps = [t for t in (rep.get("targets") or []) if t.get("target_key") == tk]
    trep = treps[0] if treps else {}
    status = trep.get("target_status") or ("asof_backfilled" if treps else "error")
    if trep.get("error") == "no_replica_match":
        it["status"] = "waiting_match"
        it["attempts"] = int(it.get("attempts") or 0) + 1
        path.write_text(json.dumps(it, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        append_consume_log({"at": iso(now), "event": "rescue_waiting_match", "target_key": tk})
        result["action"] = "waiting_match"
        return result

    if status in ("asof_backfilled", "no_odds"):
        extra = {
            "asof_report": rep.get("report_path"),
            "rescue_rule": rescue_rule,
            "recommend_live_ok": reco_ok,
            "sim_ok": True if status == "asof_backfilled" else False,
            "features_ok": True if status == "asof_backfilled" else False,
        }
        # R-D：另落一份晚补索引到 shadow/（对照标签；主值已写入规则格）
        if not reco_ok and status == "asof_backfilled":
            sp = rq.SHADOW / f"{path.stem}__late_report.json"
            jdump(sp, {
                "schema": "live_rescue_late_v1",
                "target_key": tk,
                "item": it,
                "reason": "kickoff_plus_3h_recommend_live_off",
                "recommend_live_ok": False,
                "sim_ok": True,
                "features_ok": True,
                "rescue_rule": "R-D",
                "noted_at": iso(now),
                "note": "准确 as-of 已写入规则 mid/close；禁即时推荐；可参与模拟与调参",
                "asof_report": rep.get("report_path"),
            })
            extra["late_report"] = str(sp)
        rq.move_item(path, rq.DONE, status=status, extra=extra)
        if status == "asof_backfilled":
            ev = "rescued" if reco_ok else "rescue_late"
        else:
            ev = "rescue_no_odds"
        append_consume_log({"at": iso(now), "event": ev, "target_key": tk,
                            "asof_report": rep.get("report_path"),
                            "rescue_rule": rescue_rule,
                            "recommend_live_ok": reco_ok})
        result["action"] = status if reco_ok else f"{status}_late"
    else:
        attempts = int(it.get("attempts") or 0) + 1
        it["attempts"] = attempts
        it["last_asof_status"] = status
        path.write_text(json.dumps(it, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if attempts >= int(it.get("max_attempts") or 5):
            rq.move_item(path, rq.FAILED, status="failed",
                         extra={"last_asof_status": status})
            append_consume_log({"at": iso(now), "event": "rescue_failed", "target_key": tk,
                                "asof_status": status})
            result["action"] = "failed"
        else:
            append_consume_log({"at": iso(now), "event": "rescue_deferred", "target_key": tk,
                                "asof_status": status, "attempts": attempts})
            result["action"] = "retry_later"
    result["asof_status"] = status
    return result


def run(*, max_n: int, daily_cap: int, db: Path, dry_run: bool,
        ignore_due_avoid: bool = False) -> dict:
    now = now_cn()
    report = {
        "schema": "live_rescue_worker_v1",
        "started_at": iso(now),
        "dry_run": dry_run,
        "actions": [],
    }

    block = rq.consume_block_reason(now)
    if block:
        report["skipped"] = block
        report["finished_at"] = iso(now_cn())
        return report

    if not ignore_due_avoid:
        due_skip = should_skip_for_due(now)
        if due_skip:
            report["skipped"] = due_skip
            report["finished_at"] = iso(now_cn())
            return report

    done_today = daily_done_count()
    budget = max(0, daily_cap - done_today)
    report["daily_done"] = done_today
    report["daily_cap"] = daily_cap
    report["budget"] = budget
    if budget <= 0:
        report["skipped"] = "daily_cap_reached"
        report["finished_at"] = iso(now_cn())
        return report

    items = rq.sort_pending_for_consume(rq.list_queue_items(rq.PENDING), now=now)
    take = min(max_n, budget, len(items))
    report["pending_total"] = len(items)
    report["taking"] = take

    for path, it in items[:take]:
        # 每条前再查禁区（长跑跨窗）
        block = rq.consume_block_reason(now_cn())
        if block:
            report["stopped_early"] = block
            break
        if not ignore_due_avoid:
            due_skip = should_skip_for_due(now_cn())
            if due_skip:
                report["stopped_early"] = due_skip
                break
        act = process_one(path, it, db=db, dry_run=dry_run, now=now_cn())
        report["actions"].append(act)

    report["finished_at"] = iso(now_cn())
    REPORTS.mkdir(parents=True, exist_ok=True)
    tag = "dryrun" if dry_run else "run"
    out = REPORTS / f"rescue_worker_{tag}_{now_cn():%Y%m%dT%H%M%S}.json"
    jdump(out, report)
    report["report_path"] = str(out)
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max", type=int, default=DEFAULT_MAX_PER_RUN, help="本轮最多消费条数")
    ap.add_argument("--daily-cap", type=int, default=DEFAULT_DAILY_CAP, help="每日成功/late/no_odds 上限")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--db", default=str(REPLICA_DB))
    ap.add_argument("--ignore-due-avoid", action="store_true",
                    help="测试用：不因邻近 live due 跳过")
    ap.add_argument("--once", action="store_true", help="显式跑一轮（默认即一轮）")
    a = ap.parse_args(argv)
    rep = run(max_n=a.max, daily_cap=a.daily_cap, db=Path(a.db), dry_run=a.dry_run,
              ignore_due_avoid=a.ignore_due_avoid)
    print(json.dumps({
        "ok": True,
        "skipped": rep.get("skipped"),
        "stopped_early": rep.get("stopped_early"),
        "pending_total": rep.get("pending_total"),
        "taking": rep.get("taking"),
        "daily_done": rep.get("daily_done"),
        "actions": [{"target_key": x.get("target_key"), "action": x.get("action")}
                    for x in rep.get("actions") or []],
        "report_path": rep.get("report_path"),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
