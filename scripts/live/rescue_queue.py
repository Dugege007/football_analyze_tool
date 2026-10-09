"""实时漏点补救队列（R-C）：只入队 / 读队列，不调 5DF。

口径：schema/v2_0-live-miss-rescue-rules.md
目录：5dollar/live/rescue_queue/{pending,done,failed,shadow,logs}
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


import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

TZ = timezone(timedelta(hours=8))
LIVE = Path(str(_ODDS_DATA_DIR / "5dollar/live"))
QROOT = LIVE / "rescue_queue"
PENDING = QROOT / "pending"
DONE = QROOT / "done"
FAILED = QROOT / "failed"
SHADOW = QROOT / "shadow"
QLOG = QROOT / "logs" / "enqueue.jsonl"

# 入队范围：规则 mid/close；真实通道也可入队（执行走 R-F，worker 未上线前仅占位）
ENQUEUE_POINTS = {("rule", "mid"), ("rule", "close"), ("actual", "t8"), ("actual", "t1")}
# plan 里 point 可能是 mid/close + phase_variant rule/real
ENQUEUE_VIA_VARIANT = {("rule", "mid"), ("rule", "close"), ("real", "mid"), ("real", "close")}


def now_cn() -> datetime:
    return datetime.now(TZ)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ).isoformat(timespec="seconds")


def _parse(s: str | None) -> datetime | None:
    if not s:
        return None
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def _ensure_dirs() -> None:
    for p in (PENDING, DONE, FAILED, SHADOW, QLOG.parent):
        p.mkdir(parents=True, exist_ok=True)


def _hash8(target_key: str) -> str:
    return hashlib.sha1(target_key.encode("utf-8")).hexdigest()[:8]


def _short_code(match_uid: str) -> str:
    # match_uid like 2026-10-09|五001
    parts = match_uid.split("|")
    return parts[-1] if len(parts) >= 2 else match_uid.replace("|", "_")


def priority_for(target_at: str | None, kickoff_at: str | None, point: str, channel: str) -> int:
    """开赛越近越大；mid > close；rule > actual。"""
    kick = _parse(kickoff_at) or _parse(target_at) or now_cn()
    hours = max(0.0, (kick - now_cn()).total_seconds() / 3600.0)
    base = int(1000 - hours * 10)
    if point in ("mid", "t8"):
        base += 10
    if channel == "rule" or point in ("mid", "close"):
        base += 5
    return base


def should_enqueue(tgt: dict) -> bool:
    """是否把该 missed 目标写入 rescue_queue。"""
    if tgt.get("catchup"):
        return False
    if tgt.get("phase") == "pending":
        return False
    ch = tgt.get("channel")
    pt = tgt.get("point")
    var = tgt.get("phase_variant")  # rule / real / rule_1110
    if var == "rule_1110" or pt == "rule_1110":
        return False
    if (ch, pt) in ENQUEUE_POINTS:
        return True
    # live_capture targets: channel=rule|actual, point=mid|close|t8|t1, phase_variant=rule|real
    if var in ("rule", "real") and pt in ("mid", "close", "t8", "t1"):
        return True
    if (var, pt) in ENQUEUE_VIA_VARIANT:
        return True
    return False


def queue_path(tgt: dict, *, folder: Path = PENDING) -> Path:
    D = tgt.get("match_uid", "").split("|")[0] or "unknown"
    code = _short_code(tgt.get("match_uid") or "x")
    pt = tgt.get("point") or "x"
    var = tgt.get("phase_variant") or tgt.get("channel") or "x"
    h = _hash8(tgt["target_key"])
    return folder / f"{D}__{code}__{pt}__{var}__{h}.json"


def build_item(tgt: dict, *, reason: str = "missed_after_window", scenario: str = "S1") -> dict[str, Any]:
    ch = tgt.get("channel") or "rule"
    pt = tgt.get("point") or "mid"
    var = tgt.get("phase_variant") or ("real" if ch == "actual" else "rule")
    plan = ["R-C", "R-F" if var == "real" or ch == "actual" else "R-A"]
    return {
        "schema": "live_rescue_queue_v1",
        "target_key": tgt["target_key"],
        "jingcai_date": (tgt.get("match_uid") or "").split("|")[0],
        "match_uid": tgt.get("match_uid"),
        "fixture_id": tgt.get("fixture_id"),
        "channel": ch,
        "point": pt,
        "phase_variant": var,
        "target_at": tgt.get("target_at"),
        "kickoff_at": tgt.get("kickoff_at"),
        "enqueued_at": iso(now_cn()),
        "reason": reason,
        "scenario": scenario,
        "priority": priority_for(tgt.get("target_at"), tgt.get("kickoff_at"), pt, ch),
        "attempts": 0,
        "max_attempts": 5,
        "status": "pending",
        "rescue_plan": plan,
        "expires_mode": "kickoff_plus_3h_recommend_live_off",
        "notes": "",
    }


def _append_log(obj: dict) -> None:
    _ensure_dirs()
    with QLOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def enqueue_missed(targets: list[dict], *, reason: str = "missed_after_window") -> list[Path]:
    """幂等入队：已存在 pending/done 同 target_key 则跳过。返回新写入路径。"""
    _ensure_dirs()
    written: list[Path] = []
    existing_keys = set()
    for folder in (PENDING, DONE, SHADOW):
        if not folder.exists():
            continue
        for p in folder.glob("*.json"):
            try:
                existing_keys.add(json.loads(p.read_text(encoding="utf-8")).get("target_key"))
            except (OSError, json.JSONDecodeError):
                continue
    for t in targets:
        if not should_enqueue(t):
            continue
        tk = t.get("target_key")
        if not tk:
            continue
        if tk in existing_keys:
            _append_log({"at": iso(now_cn()), "event": "rescue_enqueue_skip_dup", "target_key": tk})
            continue
        item = build_item(t, reason=reason)
        path = queue_path(t, folder=PENDING)
        path.write_text(json.dumps(item, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        existing_keys.add(tk)
        written.append(path)
        _append_log({
            "at": iso(now_cn()),
            "event": "rescue_enqueued",
            "target_key": tk,
            "path": str(path),
            "priority": item["priority"],
            "rescue_plan": item["rescue_plan"],
        })
    return written


# ---- 消费侧辅助（gap_scan / worker 共用）----

SUCCESS_STATUSES = frozenset({
    "captured", "captured_late", "asof_backfilled", "no_odds", "rescued",
})
# 开赛后超过此时长：仍 as-of 写入规则格（sim/features 可用），但 recommend_live_ok=false
RECOMMEND_LIVE_OFF_HOURS_AFTER_KICKOFF = 3.0
# 兼容旧名（语义已改为「即时推荐关」，不再等于 features 全禁）
SHADOW_HOURS_AFTER_KICKOFF = RECOMMEND_LIVE_OFF_HOURS_AFTER_KICKOFF
# worker 额外禁消费：含 11:10 高峰前缘
CONSUME_BLOCK_WINDOWS = (
    ((11, 0), (11, 20)),   # 覆盖 11:05–11:20 让路 + 11:00–11:05
)


def recommend_live_ok(kickoff_at: str | None, *, now: datetime | None = None,
                     hours: float = RECOMMEND_LIVE_OFF_HOURS_AFTER_KICKOFF) -> bool:
    """开赛后 ≤ hours：日用即时推荐通道仍可用；之后 false（仍可 sim/features）。"""
    kick = _parse(kickoff_at)
    if kick is None:
        return True
    n = now or now_cn()
    return n < kick + timedelta(hours=hours)


def features_usable(kickoff_at: str | None, *, now: datetime | None = None,
                    shadow_hours: float = SHADOW_HOURS_AFTER_KICKOFF) -> bool:
    """兼容旧调用名 → 实际表示 recommend_live_ok（非 features 全禁）。"""
    return recommend_live_ok(kickoff_at, now=now, hours=shadow_hours)


def consume_block_reason(now: datetime | None = None) -> str | None:
    """若当前禁止消费队列，返回原因字符串；否则 None。
    含 shared 让路窗 + 11:00–11:20 额外禁区。不 import shared_api_yield（避免路径耦合），
    让路窗时刻与口径卡 / shared_api_yield.YIELD_WINDOWS 对齐。
    """
    n = now or now_cn()
    hm = (n.hour, n.minute)
    # shared 让路（左闭右开）
    for (h0, m0), (h1, m1) in (
        ((11, 5), (11, 20)),
        ((14, 55), (15, 15)),
        ((21, 55), (22, 15)),
    ):
        if (h0, m0) <= hm < (h1, m1):
            return f"yield_window_{h0:02d}{m0:02d}_{h1:02d}{m1:02d}"
    for (h0, m0), (h1, m1) in CONSUME_BLOCK_WINDOWS:
        if (h0, m0) <= hm < (h1, m1):
            return f"consume_block_{h0:02d}{m0:02d}_{h1:02d}{m1:02d}"
    return None


def list_queue_items(folder: Path = PENDING) -> list[tuple[Path, dict]]:
    _ensure_dirs()
    out: list[tuple[Path, dict]] = []
    if not folder.exists():
        return out
    for p in sorted(folder.glob("*.json")):
        try:
            out.append((p, json.loads(p.read_text(encoding="utf-8"))))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def sort_pending_for_consume(items: list[tuple[Path, dict]], *, now: datetime | None = None
                             ) -> list[tuple[Path, dict]]:
    """积压排序：① recommend_live 仍可用 ② priority 降序 ③ jingcai_date 旧→新 ④ mid 先于 close。"""
    n = now or now_cn()

    def key(pair: tuple[Path, dict]):
        _p, it = pair
        usable = 0 if recommend_live_ok(it.get("kickoff_at"), now=n) else 1
        pri = -int(it.get("priority") or 0)
        day = it.get("jingcai_date") or ""
        pt = it.get("point") or ""
        mid_first = 0 if pt in ("mid", "t8") else 1
        return (usable, pri, day, mid_first, it.get("target_key") or "")

    return sorted(items, key=key)


def move_item(path: Path, dest_folder: Path, *, status: str, extra: dict | None = None) -> Path:
    _ensure_dirs()
    dest_folder.mkdir(parents=True, exist_ok=True)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["status"] = status
    data["resolved_at"] = iso(now_cn())
    if extra:
        data.update(extra)
    dest = dest_folder / path.name
    dest.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    _append_log({
        "at": iso(now_cn()),
        "event": f"rescue_moved_{status}",
        "target_key": data.get("target_key"),
        "from": str(path),
        "to": str(dest),
    })
    return dest


def existing_target_keys() -> set[str]:
    keys: set[str] = set()
    for folder in (PENDING, DONE, SHADOW, FAILED):
        if not folder.exists():
            continue
        for p in folder.glob("*.json"):
            try:
                keys.add(json.loads(p.read_text(encoding="utf-8")).get("target_key"))
            except (OSError, json.JSONDecodeError):
                continue
    return {k for k in keys if k}


def enqueue_gap_targets(targets: list[dict], *, scenario: str = "S17",
                        reason: str = "gap_scan_missing") -> list[Path]:
    """缺口扫描入队：同 enqueue_missed，但带 scenario/reason；幂等。"""
    _ensure_dirs()
    written: list[Path] = []
    existing = existing_target_keys()
    for t in targets:
        if not should_enqueue(t):
            continue
        tk = t.get("target_key")
        if not tk or tk in existing:
            _append_log({"at": iso(now_cn()), "event": "rescue_enqueue_skip_dup",
                         "target_key": tk, "reason": reason})
            continue
        item = build_item(t, reason=reason, scenario=scenario)
        # 多日积压：即时推荐仍可用的 +200；晚补仍走 R-A（recommend_live_off）
        if recommend_live_ok(item.get("kickoff_at")):
            item["priority"] = int(item["priority"]) + 200
        else:
            item["rescue_plan"] = ["R-C", "R-D"]
            item["recommend_live_ok"] = False
            item["notes"] = (item.get("notes") or "") + "gap_scan:kickoff_plus_3h→recommend_live_off"
        path = queue_path(t, folder=PENDING)
        path.write_text(json.dumps(item, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        existing.add(tk)
        written.append(path)
        _append_log({
            "at": iso(now_cn()),
            "event": "rescue_enqueued",
            "target_key": tk,
            "path": str(path),
            "priority": item["priority"],
            "scenario": scenario,
            "reason": reason,
            "rescue_plan": item["rescue_plan"],
        })
    return written
