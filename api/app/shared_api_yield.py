"""共享数据接口（5DF）补数让路判断（0.3.19 追加 2）。只用标准库，补数脚本 / 队列共用。

- 共享限额：同一账户窗口 （账户上限）；分析师前向采集（odds-data/scripts/live_capture.py）最多用 24 次/分钟。
  → 我们的补数（所有调这个接口的回补脚本 / 队列，**不含** live_capture 本身）合计 ≤ 16 次/分钟。
- 让路时段（北京时间，左闭右开）：11:05–11:20、14:55–15:15、21:55–22:15（11:10 即时、15:00 中盘、22:00 临盘集中到点）。
  时段内不发请求：before_request() 睡到时段结束再放行。
- 响应头 X-RateLimit-Remaining ≤ 24（分析师那份）→ 停到 X-RateLimit-Reset（缺省 61 秒）。
- 多进程：同一台机器上的补数进程共用一个时间戳账本（flock），合计 ≤ 16 次/分钟。
用法：
    import sys; sys.path.insert(0, "api/app"); import shared_api_yield as Y
    gate = Y.Gate("run_queue")
    gate.before_request(); resp = urlopen(...); gate.after_response(resp.headers)
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


import fcntl
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

TZ_CN = timezone(timedelta(hours=8))
SHARED_LIMIT_PER_MIN = 40
ANALYST_LIVE_MAX_PER_MIN = 24
BACKFILL_MAX_PER_MIN = SHARED_LIMIT_PER_MIN - ANALYST_LIVE_MAX_PER_MIN  # 16
MIN_INTERVAL_SEC = 60.0 / BACKFILL_MAX_PER_MIN  # 3.75s（单进程也不突发）
REMAINING_FLOOR = ANALYST_LIVE_MAX_PER_MIN  # 剩余 ≤ 24 → 停
YIELD_WINDOWS: tuple[tuple[tuple[int, int], tuple[int, int]], ...] = (
    ((11, 5), (11, 20)),
    ((14, 55), (15, 15)),
    ((21, 55), (22, 15)),
)
LEDGER = Path(os.environ.get("SHARED_API_YIELD_LEDGER", str(_ODDS_DATA_DIR / "5dollar/backfill_yield_ledger.json")))
LOG = Path(os.environ.get("SHARED_API_YIELD_LOG", str(_ODDS_DATA_DIR / "5dollar/backfill_yield.jsonl")))


def _cn(now: Optional[datetime] = None) -> datetime:
    n = now or datetime.now(TZ_CN)
    return (n if n.tzinfo else n.replace(tzinfo=TZ_CN)).astimezone(TZ_CN)


def yield_window(now: Optional[datetime] = None) -> Optional[tuple[datetime, datetime]]:
    """当前处在哪个让路时段 → (开始, 结束)；不在 → None。"""
    n = _cn(now)
    for (h0, m0), (h1, m1) in YIELD_WINDOWS:
        a = n.replace(hour=h0, minute=m0, second=0, microsecond=0)
        b = n.replace(hour=h1, minute=m1, second=0, microsecond=0)
        if a <= n < b:
            return a, b
    return None


def must_yield(now: Optional[datetime] = None) -> bool:
    return yield_window(now) is not None


def seconds_until_clear(now: Optional[datetime] = None) -> float:
    w = yield_window(now)
    return 0.0 if w is None else max(0.0, (w[1] - _cn(now)).total_seconds())


def _log(event: str, **kw: Any) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": _cn().isoformat(), "event": event, **kw}, ensure_ascii=False) + "\n")
    except OSError:
        pass


class Gate:
    """补数请求闸门：让路时段 + 合计 ≤16/分钟 + 剩余额度保底。"""

    def __init__(self, caller: str, *, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time,
                 now: Callable[[], datetime] = lambda: datetime.now(TZ_CN),
                 ledger: Optional[Path] = None):
        self.caller = caller
        self._sleep, self._clock, self._now = sleep, clock, now
        self.ledger = ledger if ledger is not None else LEDGER
        self.pause_until = 0.0
        self.stats = {"requests": 0, "yield_sleeps": 0, "yield_seconds": 0.0,
                      "rate_sleeps": 0, "quota_pauses": 0}

    # ---- 让路时段
    def wait_for_clear(self) -> float:
        slept = 0.0
        while True:
            s = seconds_until_clear(self._now())
            if s <= 0:
                return slept
            w = yield_window(self._now())
            _log("yield_window", caller=self.caller, until=w[1].isoformat() if w else None, sleep_s=round(s, 1))
            self.stats["yield_sleeps"] += 1
            self.stats["yield_seconds"] += s
            self._sleep(s + 1.0)
            slept += s + 1.0

    # ---- 跨进程速率账本
    def _reserve_slot(self) -> float:
        """在账本里占一个发送时刻，返回需要等待的秒数（0 = 现在就发）。"""
        t = self._clock()
        try:
            self.ledger.parent.mkdir(parents=True, exist_ok=True)
            with open(self.ledger, "a+", encoding="utf-8") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                f.seek(0)
                try:
                    stamps = [float(x) for x in json.loads(f.read() or "[]")]
                except (ValueError, TypeError):
                    stamps = []
                stamps = sorted(x for x in stamps if x > t - 60.0)
                send = t
                if stamps:
                    send = max(send, stamps[-1] + MIN_INTERVAL_SEC)
                if len(stamps) >= BACKFILL_MAX_PER_MIN:
                    send = max(send, stamps[-BACKFILL_MAX_PER_MIN] + 60.0)
                stamps.append(send)
                f.seek(0)
                f.truncate()
                f.write(json.dumps(stamps[-BACKFILL_MAX_PER_MIN * 2:]))
        except OSError:
            send = t + MIN_INTERVAL_SEC
        return max(0.0, send - t)

    def before_request(self) -> None:
        """每次调共享接口前调用：额度暂停 → 让路时段 → 速率。"""
        while True:
            wait_q = self.pause_until - self._clock()
            if wait_q > 0:
                self._sleep(wait_q)
            self.wait_for_clear()
            wait = self._reserve_slot()
            if wait > 0:
                self.stats["rate_sleeps"] += 1
                self._sleep(wait)
            if must_yield(self._now()):  # 等速率时跨进了时段 → 再让一次
                continue
            self.stats["requests"] += 1
            return

    def after_response(self, headers: Optional[Mapping[str, Any]]) -> None:
        """看 X-RateLimit-Remaining：≤24（分析师那份）→ 停到 Reset（缺省 61 秒）。"""
        if not headers:
            return
        get = headers.get
        rem, rst = get("X-RateLimit-Remaining"), get("X-RateLimit-Reset")
        try:
            rem_i = int(rem) if rem is not None else None
        except (TypeError, ValueError):
            rem_i = None
        if rem_i is None or rem_i > REMAINING_FLOOR:
            return
        pause = 61.0
        try:
            r = float(rst)
            # Reset 可能是 epoch 秒，也可能是剩余秒数
            pause = (r - self._clock()) + 1.0 if r > 1e9 else r + 1.0
        except (TypeError, ValueError):
            pass
        pause = min(max(pause, 5.0), 120.0)
        self.pause_until = self._clock() + pause
        self.stats["quota_pauses"] += 1
        _log("quota_floor", caller=self.caller, remaining=rem_i, pause_s=round(pause, 1))
