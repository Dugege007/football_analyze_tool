#!/usr/bin/env python3
"""历史补数 → 让路给今日 live / rescue（只读探测，不写库、不调 5DF）。

口径：docs/schema/v2_0-hist-backfill-yield-to-live.md
与 rescue_queue.consume_block_reason / shared_api_yield 对齐，并额外：
  - due−15min（扫 live plan）
  - live heartbeat 忙（due/groups>0 且心跳新鲜）
  - live 近 N 分钟有成功写库（ingest/captured mtime）
  - heartbeat.remaining 过低（默认 ≤24，给 live 留 ≥40% 冗余；用户要求最忙日 ≥20%）
  - rescue_worker 在跑，或近 N 分钟有 rescue 消费
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

TZ = timezone(timedelta(hours=8))
# 实时采集运行目录：$ODDS_DATA_DIR/5dollar/live（ODDS_DATA_DIR 默认 <仓库根>/data/odds-data，相对路径按仓库根解析）
_REPO_ROOT = Path(__file__).resolve().parents[3]
_odds_env = os.environ.get("ODDS_DATA_DIR", "").strip()
_ODDS_DATA_DIR = (Path(_odds_env).expanduser() if _odds_env else _REPO_ROOT / "data" / "odds-data")
if not _ODDS_DATA_DIR.is_absolute():
    _ODDS_DATA_DIR = _REPO_ROOT / _ODDS_DATA_DIR
LIVE = _ODDS_DATA_DIR / "5dollar" / "live"
D_PLAN = LIVE / "plan"
D_STATE = LIVE / "state"
D_LOG = LIVE / "logs"
HB = D_LOG / "heartbeat.json"
RESCUE_CONSUME = LIVE / "rescue_queue" / "logs" / "consume.jsonl"

# 与 rescue 消费禁区对齐：固定窗覆盖 11:10 / 规则中盘·临盘集中点
# 11:00–11:20 含 shared 11:05–11:20 + 前缘；另两段与 shared_api_yield 一致
FIXED_YIELD_WINDOWS: tuple[tuple[tuple[int, int], tuple[int, int]], ...] = (
    ((11, 0), (11, 20)),
    ((14, 55), (15, 15)),
    ((21, 55), (22, 15)),
)
DUE_AVOID_MIN = 15
DUE_LATE_GRACE_SEC = 600  # 过点后仍视为紧迫的宽限（与 rescue_worker 一致）
DEFAULT_LIVE_BUSY_MIN = 3
DEFAULT_REMAINING_FLOOR = 24  # shared_api_yield.REMAINING_FLOOR；≥20% 冗余 = remaining≥8
DEFAULT_RESCUE_BUSY_MIN = 3


def now_cn(now: Optional[datetime] = None) -> datetime:
    n = now or datetime.now(TZ)
    return (n if n.tzinfo else n.replace(tzinfo=TZ)).astimezone(TZ)


def parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def jload(p: Path, default: Any = None) -> Any:
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def fixed_window_reason(now: Optional[datetime] = None) -> str | None:
    n = now_cn(now)
    hm = (n.hour, n.minute)
    for (h0, m0), (h1, m1) in FIXED_YIELD_WINDOWS:
        if (h0, m0) <= hm < (h1, m1):
            return f"fixed_window_{h0:02d}{m0:02d}_{h1:02d}{m1:02d}"
    return None


def nearest_due_seconds(now: Optional[datetime] = None) -> float | None:
    """扫昨/今/明 plan，找最近未完成目标距 now 的秒数（负=已过点仍在宽限）。"""
    n = now_cn(now)
    best: float | None = None
    for off in (-1, 0, 1):
        D = (n.date() + timedelta(days=off)).isoformat()
        plan = jload(D_PLAN / f"{D}.json")
        if not plan:
            continue
        st = jload(D_STATE / f"captured_{D}.json", {}) or {}
        for t in plan.get("targets") or []:
            if t.get("target_key") in st:
                continue
            at = parse_dt(t.get("target_at"))
            kick = parse_dt(t.get("kickoff_at"))
            if at is None:
                continue
            if kick and n >= kick:
                continue
            sec = (at - n).total_seconds()
            if sec < -DUE_LATE_GRACE_SEC:
                continue
            if best is None or abs(sec) < abs(best):
                best = sec
    return best


def due_avoid_reason(now: Optional[datetime] = None, *, avoid_min: int = DUE_AVOID_MIN) -> str | None:
    sec = nearest_due_seconds(now)
    if sec is None:
        return None
    if -DUE_LATE_GRACE_SEC <= sec <= avoid_min * 60:
        return f"due_avoid_sec={int(sec)}"
    return None


def _mtime_age_sec(path: Path, now: datetime) -> float | None:
    if not path.exists():
        return None
    try:
        mt = datetime.fromtimestamp(path.stat().st_mtime, TZ)
    except OSError:
        return None
    return (now - mt).total_seconds()


def live_heartbeat_busy_reason(
    now: Optional[datetime] = None,
    *,
    busy_min: float = DEFAULT_LIVE_BUSY_MIN,
    remaining_floor: int = DEFAULT_REMAINING_FLOOR,
) -> str | None:
    n = now_cn(now)
    hb = jload(HB)
    if not isinstance(hb, dict):
        return None
    ts = parse_dt(hb.get("ts"))
    age = (n - ts).total_seconds() if ts else None
    rem = hb.get("remaining")
    try:
        rem_i = int(rem) if rem is not None else None
    except (TypeError, ValueError):
        rem_i = None
    if rem_i is not None and rem_i <= remaining_floor:
        return f"remaining_low={rem_i}"
    due = int(hb.get("due") or 0)
    groups = int(hb.get("groups") or 0)
    if age is not None and age <= busy_min * 60 and (due > 0 or groups > 0):
        return f"live_daemon_busy_due={due}_groups={groups}_age_s={int(age)}"
    return None


def live_recent_write_reason(
    now: Optional[datetime] = None,
    *,
    busy_min: float = DEFAULT_LIVE_BUSY_MIN,
) -> str | None:
    """近 N 分钟 ingest/captured 有落盘 → 今日 live 刚写过，历史让路。"""
    n = now_cn(now)
    D = n.date().isoformat()
    candidates = [
        D_STATE / f"ingest_{D}.json",
        D_STATE / f"captured_{D}.json",
    ]
    best: tuple[float, str] | None = None
    for p in candidates:
        age = _mtime_age_sec(p, n)
        if age is None:
            continue
        if age <= busy_min * 60:
            if best is None or age < best[0]:
                best = (age, p.name)
    if best:
        return f"live_recent_write_{best[1]}_age_s={int(best[0])}"
    return None


def rescue_busy_reason(
    now: Optional[datetime] = None,
    *,
    busy_min: float = DEFAULT_RESCUE_BUSY_MIN,
) -> str | None:
    n = now_cn(now)
    # 进程探测（不杀、不发信号）
    try:
        r = subprocess.run(
            ["pgrep", "-f", "rescue_worker.py"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if r.returncode == 0 and (r.stdout or "").strip():
            return "rescue_worker_running"
    except (OSError, subprocess.TimeoutExpired):
        pass
    if not RESCUE_CONSUME.exists():
        return None
    try:
        # 只读末尾若干行
        lines = RESCUE_CONSUME.read_text(encoding="utf-8").splitlines()[-40:]
    except OSError:
        return None
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        at = parse_dt(o.get("at"))
        if at is None:
            continue
        age = (n - at).total_seconds()
        if age <= busy_min * 60 and o.get("event") in (
            "rescued",
            "rescue_late",
            "rescue_shadow",
            "rescue_no_odds",
            "rescue_deferred",
            "rescue_failed",
            "consume_start",
        ):
            return f"rescue_recent_event={o.get('event')}_age_s={int(age)}"
        if age > busy_min * 60:
            break
    return None


def check_yield_reason(
    now: Optional[datetime] = None,
    *,
    busy_min: float = DEFAULT_LIVE_BUSY_MIN,
    remaining_floor: int = DEFAULT_REMAINING_FLOOR,
    due_avoid_min: int = DUE_AVOID_MIN,
    check_recent_write: bool = True,
) -> str | None:
    """若应暂停历史补数，返回 paused_reason；否则 None（空闲可跑）。"""
    n = now_cn(now)
    for fn in (
        lambda: fixed_window_reason(n),
        lambda: due_avoid_reason(n, avoid_min=due_avoid_min),
        lambda: live_heartbeat_busy_reason(n, busy_min=busy_min, remaining_floor=remaining_floor),
        lambda: live_recent_write_reason(n, busy_min=busy_min) if check_recent_write else None,
        lambda: rescue_busy_reason(n, busy_min=busy_min),
    ):
        reason = fn()
        if reason:
            return reason
    return None


def seconds_until_fixed_window_clear(now: Optional[datetime] = None) -> float:
    n = now_cn(now)
    hm = (n.hour, n.minute)
    for (h0, m0), (h1, m1) in FIXED_YIELD_WINDOWS:
        if (h0, m0) <= hm < (h1, m1):
            end = n.replace(hour=h1, minute=m1, second=0, microsecond=0)
            return max(0.0, (end - n).total_seconds())
    return 0.0


def suggest_sleep_seconds(
    reason: str | None,
    now: Optional[datetime] = None,
    *,
    busy_min: float = DEFAULT_LIVE_BUSY_MIN,
) -> float:
    """给 --yield-wait 用的建议睡眠秒数（有上界，避免一次睡死）。"""
    if not reason:
        return 0.0
    n = now_cn(now)
    if reason.startswith("fixed_window_"):
        return min(max(seconds_until_fixed_window_clear(n), 5.0), 900.0)
    if reason.startswith("due_avoid_sec="):
        try:
            sec = int(reason.split("=", 1)[1])
        except ValueError:
            sec = 0
        # 若还未到点：睡到 T+grace 后一小段；若已过点：睡完宽限
        if sec >= 0:
            return min(sec + 30.0, DUE_AVOID_MIN * 60 + 60.0)
        return min(DUE_LATE_GRACE_SEC + sec + 15.0, 120.0)
    # busy / remaining / rescue：短睡再探
    return min(max(busy_min * 60.0 / 2.0, 30.0), 180.0)


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Probe hist→live yield (no API)")
    ap.add_argument("--busy-min", type=float, default=DEFAULT_LIVE_BUSY_MIN)
    ap.add_argument("--remaining-floor", type=int, default=DEFAULT_REMAINING_FLOOR)
    ap.add_argument("--no-recent-write", action="store_true")
    args = ap.parse_args()
    n = now_cn()
    reason = check_yield_reason(
        n,
        busy_min=args.busy_min,
        remaining_floor=args.remaining_floor,
        check_recent_write=not args.no_recent_write,
    )
    out = {
        "at": n.isoformat(),
        "idle": reason is None,
        "paused_reason": reason,
        "suggest_sleep_s": suggest_sleep_seconds(reason, n, busy_min=args.busy_min),
        "nearest_due_sec": nearest_due_seconds(n),
        "fixed_window": fixed_window_reason(n),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if reason is None else 2


if __name__ == "__main__":
    raise SystemExit(main())
