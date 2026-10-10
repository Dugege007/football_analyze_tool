#!/usr/bin/env python3
"""前向实时采集（todo live-capture-mid-close）：竞彩场次在规则中盘/临盘、例外场真实 T−8h/T−1h、竞彩日 11:10
各抓一次 5DF `/v1/fixtures/{id}/odds`（一次调用拿全澳门/皇冠/威廉/平博/365/马会/竞彩等），落原始 JSON +
归一化 staging，再只写 v2d3 研究副本 `odds_snapshot`（先备份、幂等 upsert），从不碰现网 app.db，DUAL_WRITE 不开。

口径依据：schema/v2_0-odds-phase-terminology.md（末节例外场按竞彩编号 jc_code_ge_2300、即时（11:10））、
v2_0-n5-poisson-spec-v1.md §7 + 接入细则 5/6（tick_age_rule=live_fetched_at、odds_source=live）、
v2_0-0316-followup-decisions.md（kickoff 12:00 占位、竞彩日归属只认编号）。
目标时刻直接调用后端 app/collection_schedule.py（rule_targets / phase_exception / actual_targets /
jingcai_date_from_code），与 /dispatch/pending 同一套函数；/dispatch/pending 读现网库，新竞彩日的场不在库里，
所以场次清单从 5DF `/v1/chinasportslottery?types=jingcailottery` 取（自带 5DF fixture id），能对上库里场次时再交叉核对。

子命令：
  plan [--date D]            刷新竞彩日 D 的场次与目标（1–2 次 CSL 调用）
  status [--date D]          列出目标（不调 API）
  tick                       一轮：按需刷新清单 → 抓到点目标 → 入副本 → 写汇总（定时器/循环调用它）
  daemon                     常驻循环：每 60s 一轮 tick（只有目标到点才调 /odds）
  ensure-daemon              没在跑就后台拉起 daemon（幂等；看门狗用）
  capture --target-key K | --nearest [--allow-early-min N] [--allow-late]   手动抓一个目标（演练用）
  asof-backfill --date D (--target-key K … | --missed-mid-rule) [--dry-run]
      漏采后 hist as-of≤T 补写 mid|rule/close|rule（委托 scripts/asof_backfill_mid_rule.py）
  gap-scan [--days 7] [--from D] [--to D] [--dry-run]   多日应采未采 → rescue_queue
  rescue-consume [--max 8] [--daily-cap 40] [--dry-run] 空闲窗消费队列（asof/shadow）
  ingest [--date D] [--dry-run] [--db PATH]
  summary [--date D]

12:00 占位 / 改期（v2_0-0316-followup-decisions.md 第 3 条 + 18:22 补充 + 18:23 推迟作废时限）：
  - 5DF 整 12:00 → kickoff_placeholder_suspect；这类场（粘住 placeholder_ever）另加 D 日 15:00 / 22:00 目标，
    快照写 channel=pending, point=d1500/d2200，extras.phase_pending=true，不归阶段；
    开赛确认后按「到目标时刻为止已公布的开赛时间」归阶段（phase_assigned），确认晚于目标 → phase_assign_late=true。
  - 由占位开赛推出的目标 features_ok=false，开赛确认（且没变）后才为 true。
  - 每次刷新都比开赛时间：变了 → kickoff_rev+1、kickoff_log / logs/kickoff_changes.jsonl 记 kickoff_original /
    kickoff_actual / detected_at；postponed_announced_at 留空、postpone_ts_unknown=true（发现时刻只作公布上界
    kickoff_known_by）；未到的旧目标作废，新目标 key 带 |k<rev>。postpone_void_hours=24（OE67/2018-art11）。
  - 开赛提前空档补抓（决议 18:36）：新目标尚未到 → 丢掉旧目标只抓新目标；新目标已过窗口 → 立刻补抓一次
    （target_at=发现时刻、planned_target_at=理论新目标、catchup=true、phase_assign_late=true，live 不计命中）；
    旧目标已抓过的保留；发现时刻只用来调度，不当 postponed_announced_at。已开赛则无法补抓，只记日志。
  - 开赛确认：改成非 12:00（确认时刻=发现时刻）、5DF 状态已开赛（=开赛时刻）、或 state/kickoff_confirm.json 人工。

窗口：目标 T 在 [T−2min, T+10min] 内到点即抓一次；超过 T+10min 未抓 → missed（同时 R-C 入
rescue_queue/pending，不调 API；自动消费：`rescue_worker.py` / `live_capture.py rescue-consume`，多日缺口：`rescue_gap_scan.py` / `gap-scan`，见 schema/v2_0-live-miss-rescue-rules.md §7）。
--allow-late：落 staging 标 late、不入副本（旧口径，仍可用）。
--asof-backfill / 独立脚本 asof_backfill_mid_rule.py（2026-10-09 拍板）：漏采后可用 5DF hist
取 as-of≤T 最近一次赛前变化写入 mid|rule / close|rule（source=5df_hist_asof，禁止用 T 之后即时盘冒充）。
开赛提前导致新目标已过窗口的补抓例外：按发现时刻立即可抓。
fetched_at 一般落在 T−2…T−1min（不晚于决策时点，不偷看）。
限速：≤24 次/分钟（间隔 2.5s），X-RateLimit-Remaining ≤ 8（40 的 20%）即暂停到窗口重置；与补数队列共用账户窗口。
Key 只从环境变量 FIVEDOLLAR_FOOTBALL_API_KEY 读，不落盘、不打印。
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
# 跨平台文件锁：Linux 与 macOS 使用 fcntl，Windows 使用 api/app/portable_lock.py 中基于 msvcrt 的实现。
import importlib.util as _pl_util
import pathlib as _pl_pathlib
_pl_spec = _pl_util.spec_from_file_location(
    "_portable_lock", _pl_pathlib.Path(__file__).resolve().parents[2] / "api/app/portable_lock.py")
fcntl = _pl_util.module_from_spec(_pl_spec)
_pl_spec.loader.exec_module(fcntl)
import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

API_ROOT = Path(str(_MA_API_ROOT))
sys.path.insert(0, str(API_ROOT))
sys.path.insert(0, str(API_ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from app import collection_schedule as cs  # noqa: E402  后端同一套目标函数
from validate_ah_sign import check_rows  # noqa: E402  后端同一套正负号校验

TZ = timezone(timedelta(hours=8))
BASE = "https://api.5dollarfootballapi.com/v1"
LIVE = Path(str(_ODDS_DATA_DIR / "5dollar/live"))
REPLICA_DB = _V2D3_DB  # V2D3_DB_PATH（研究副本，采集只写这里）
PROD_DB = _APP_DB.resolve()  # APP_DB_PATH（现网库，采集拒绝写入）

GAP_SEC = 2.5            # ≤24/分钟
RESERVE = 8              # （账户上限） × 20%
WIN_BEFORE = timedelta(minutes=2)
WIN_AFTER = timedelta(minutes=10)
LOOP_SEC = 60
PLAN_REFRESH = {0: timedelta(minutes=60), 1: timedelta(minutes=120)}  # 相对今天的竞彩日偏移
INGEST_LAG_MIN = (-15.0, 10.0)  # fetched − target（分钟）在此区间才入副本
WATER_MIN, WATER_MAX = 0.50, 1.50

# 5DF slug → 库内 book 码（v2d3 已用 macau/crown/william/bet365/jc；其余沿用 slug）
BOOKS = {
    "macauslot": "macau", "crown": "crown", "williamhill": "william", "pinnacle": "pinnacle",
    "bet365": "bet365", "hkjc": "hkjc", "12bet": "12bet", "vcbet": "vcbet", "1xbet": "1xbet",
    "18bet": "18bet", "easybets": "easybets", "interwetten": "interwetten",
    "chinasportslottery": "jc",
}
MARKETS = {"asian_handicap": "asian", "1x2": "euro_1x2", "goal_line": "ou"}
CORE_BOOKS = ("macau", "crown", "william", "pinnacle")

# phase/variant → odds_snapshot (channel, point)；与 app/table_matches.SNAP_POINTS 对齐
SNAP_KEY = {
    ("mid", "rule"): ("rule", "mid"),
    ("close", "rule"): ("rule", "close"),
    ("mid", "real"): ("actual", "t8"),
    ("close", "real"): ("actual", "t1"),
    ("live", "rule_1110"): ("rule", "rule_1110"),
    # 12:00 占位疑似场（决议 18:22 补充）：D 日 15:00 / 22:00 一律照抓，先存着、不归阶段（phase_pending=true）
    ("pending", "d1500"): ("pending", "d1500"),
    ("pending", "d2200"): ("pending", "d2200"),
}
SOURCE = "5df_live"
PENDING_CLOCKS = (("d1500", 15, 0), ("d2200", 22, 0))
# 推迟作废时限（决议 18:23）：澳门第 67/2018 号行政命令第十一條
POSTPONE_VOID_HOURS = 24
POSTPONE_VOID_SRC = "OE67/2018-art11"
# 5DF 状态里表示「已开赛/已完赛」的值（用于确认 12:00 是真实开赛）；词表未知时只认这些
STARTED_STATUSES = {"live", "inplay", "in_play", "playing", "1h", "2h", "ht", "halftime", "et", "pen",
                    "finished", "ft", "ended", "complete", "completed", "aet", "full_time", "fulltime"}

D_ALL, D_PIN, D_CSL, D_PLAN, D_STAGE, D_STATE, D_LOG, D_SUM, D_BAK = (
    LIVE / "all", LIVE / "pinnacle_ah", LIVE / "csl", LIVE / "plan", LIVE / "staging",
    LIVE / "state", LIVE / "logs", LIVE / "summary", LIVE / "backups")


# ----------------------------------------------------------------------------- utils

def now_cn() -> datetime:
    return datetime.now(TZ).replace(microsecond=0)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(TZ).isoformat() if dt else None


def parse(s: str | None) -> datetime | None:
    if not s:
        return None
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=TZ)


def jload(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def jdump(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(p)


def append_jsonl(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def health(event: str, **kw) -> None:
    append_jsonl(D_LOG / "health.jsonl", {"ts": iso(now_cn()), "event": event, **kw})


def short_code(number: str | None) -> str | None:
    if not number:
        return None
    s = str(number).strip()
    return s[1:] if s.startswith("周") else s


# ----------------------------------------------------------------------------- 5DF client

class Client:
    def __init__(self):
        self.key = os.environ.get("FIVEDOLLAR_FOOTBALL_API_KEY", "")
        if not self.key or self.key.startswith("YOUR_"):
            raise SystemExit("缺少 FIVEDOLLAR_FOOTBALL_API_KEY：请在仓库根 .env 中填写（见 config.example.env / docs/SENSITIVE.md）；"
                             "不带 key 可先跑 `python scripts/live/live_capture.py check-config`")
        self.last = 0.0
        self.calls = 0
        self.remaining: int | None = None
        self.limit: int | None = None

    def get(self, name: str, path: str, params: dict | None = None):
        for attempt in range(4):
            wait = GAP_SEC - (time.time() - self.last)
            if wait > 0:
                time.sleep(wait)
            if self.remaining is not None and self.remaining <= RESERVE:
                health("quota_pause", remaining=self.remaining, sleep_s=61)
                time.sleep(61)
                self.remaining = None
            url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
            req = urllib.request.Request(url, headers={"Authorization": "Bearer " + self.key,
                                                       "Accept": "application/json"})
            t0 = time.time()
            try:
                r = urllib.request.urlopen(req, timeout=60)
                code, body, h = r.status, r.read(), r.headers
            except urllib.error.HTTPError as e:
                code, body, h = e.code, e.read(), e.headers
            except Exception as e:  # 网络错误
                code, body, h = -1, str(e).encode(), {}
            self.last = time.time()
            self.calls += 1
            rem = h.get("X-RateLimit-Remaining") if h else None
            lim = h.get("X-RateLimit-Limit") if h else None
            self.remaining = int(rem) if rem not in (None, "") else None
            self.limit = int(lim) if lim not in (None, "") else self.limit
            D_LOG.mkdir(parents=True, exist_ok=True)
            lp = D_LOG / "call_log.tsv"
            if not lp.exists():
                lp.write_text("ts\tname\tpath\thttp\telapsed_s\tlimit\tremaining\n", encoding="utf-8")
            with lp.open("a", encoding="utf-8") as f:
                f.write(f"{iso(now_cn())}\t{name}\t{path}\t{code}\t{time.time() - t0:.2f}\t{lim}\t{rem}\n")
            if code == 429 or code >= 500 or code == -1:
                ra = float((h.get("Retry-After") if h else None) or 10)
                health("api_retry", name=name, http=code, sleep_s=ra + 1, attempt=attempt)
                time.sleep(ra + 1)
                continue
            try:
                data = json.loads(body)
            except Exception:
                data = {"_raw": body[:500].decode("utf-8", "ignore")}
            return code, data
        return code, {"_error": "retries_exhausted"}


# ----------------------------------------------------------------------------- plan

def fetch_csl_day(cl: Client, D: str) -> dict:
    """竞彩日 D 的场：CSL 两个窗口 [D 10:00, D+1 10:00) ∪ [D+1 10:00, D+1 16:00]（API 单窗 ≤24h），
    再按编号星期归属（铁律：竞彩日只认编号；次日 12:00 开赛但属于 D 的场也能收进来）。"""
    base = datetime.strptime(D, "%Y-%m-%d").replace(tzinfo=TZ)
    wins = [(base + timedelta(hours=10), base + timedelta(hours=34) - timedelta(seconds=1)),
            (base + timedelta(hours=34), base + timedelta(hours=40))]
    items: dict = {}
    for ws, we in wins:
        st, en = int(ws.timestamp()), int(we.timestamp())
        page = 1
        while True:
            code, data = cl.get(f"csl_{D}_{ws:%d%H}_p{page}", "/chinasportslottery",
                                {"types": "jingcailottery", "start_time": st, "end_time": en,
                                 "per_page": 100, "page": page, "lang": "zh-cn"})
            if code != 200 or not data.get("success"):
                raise RuntimeError(f"csl {D} http={code} {str(data)[:200]}")
            for it in data.get("data") or []:
                items[it.get("id")] = it
            if not (data.get("pagination") or {}).get("has_more"):
                break
            page += 1
    doc = {"jingcai_date": D, "fetched_at": iso(now_cn()),
           "windows": [[iso(a), iso(b)] for a, b in wins], "data": list(items.values())}
    jdump(D_CSL / f"{D}.json", doc)
    jdump(D_CSL / "history" / f"{D}_{now_cn():%Y%m%dT%H%M%S}.json", doc)
    return doc


def is_placeholder_kick(k: datetime | None) -> bool:
    """5DF 整 12:00（秒也为 0）= 只有日期、没有时刻的疑似占位（决议 1 / 第 3 条）。"""
    return bool(k) and k.astimezone(TZ).hour == 12 and k.astimezone(TZ).minute == 0 and k.second == 0


def _merge_kickoff(D: str, m: dict, pm: dict | None, kick: datetime, now: datetime, status, manual: dict | None,
                   prev_built_at: str | None, klog: list) -> bool:
    """把本次刷新的开赛时间并入场次记录；返回「开赛时间或确认状态有变化」。

    - 开赛时间变了：kickoff_rev+1，kickoff_log 记 {kickoff_from, kickoff_at, detected_at}，
      postponed_announced_at 一律留空（不拿发现时刻顶替），postpone_ts_unknown=true。
      detected_at 只是「最晚在这个时刻已公布」的上界（kickoff_known_by），不是公布时刻。
    - 占位确认：疑似 12:00 的场，开赛时间改成非 12:00（确认时刻取发现时刻作上界）、5DF 状态显示已开赛
      （确认时刻 = 开赛时刻）、或 state/kickoff_confirm.json 人工确认（需带 confirmed_at）。
    """
    ph = is_placeholder_kick(kick)
    if pm is None:
        log = [{"rev": 0, "kickoff_at": iso(kick), "detected_at": iso(now), "announced_at": None, "placeholder": ph}]
        rev, original, ph_ever = 0, kick, ph
        conf_at, conf_src = (None, None) if ph else (iso(now), "5df_non_placeholder")
        changed = False
    else:
        log = [dict(e) for e in (pm.get("kickoff_log") or [
            {"rev": 0, "kickoff_at": pm["kickoff_at"], "detected_at": prev_built_at, "announced_at": None,
             "placeholder": bool(pm.get("kickoff_placeholder_suspect"))}])]
        rev = int(pm.get("kickoff_rev") or 0)
        original = parse(pm.get("kickoff_original") or log[0]["kickoff_at"])
        ph_ever = bool(pm.get("placeholder_ever", pm.get("kickoff_placeholder_suspect"))) or ph
        conf_at = pm.get("kickoff_confirmed_at")
        conf_src = pm.get("kickoff_confirm_source")
        if pm.get("kickoff_confirmed_at") is None and "kickoff_confirmed_at" not in pm and not pm.get(
                "kickoff_placeholder_suspect"):
            conf_at, conf_src = prev_built_at, "5df_non_placeholder"  # 旧版计划：非占位场视为已确认
        prev_kick = parse(pm["kickoff_at"])
        changed = False
        if kick != prev_kick:
            rev += 1
            changed = True
            ent = {"rev": rev, "kickoff_from": iso(prev_kick), "kickoff_at": iso(kick), "detected_at": iso(now),
                   "announced_at": None, "placeholder": ph}
            log.append(ent)
            if ph:
                conf_at, conf_src = None, None          # 换成另一个 12:00：仍是疑似占位
            else:
                conf_at, conf_src = iso(now), "5df_kickoff_changed"
            rec = {"ts": iso(now), "jingcai_date": D, "match_uid": m["match_uid"], "fixture_id": m.get("fixture_id"),
                   "kickoff_original": iso(original), "kickoff_from": iso(prev_kick), "kickoff_actual": iso(kick),
                   "rev": rev, "detected_at": iso(now), "postponed_announced_at": None,
                   "postpone_ts_unknown": True, "kickoff_original_placeholder": bool(log[0].get("placeholder")),
                   "placeholder_now": ph}
            klog.append(rec)
            append_jsonl(D_LOG / "kickoff_changes.jsonl", rec)
    if conf_at is None and ph and str(status or "").strip().lower() in STARTED_STATUSES and now >= kick:
        conf_at, conf_src, changed = iso(kick), "5df_status_started", True
    if manual and conf_at is None and manual.get("confirmed_at") and parse(manual.get("kickoff_at")) == kick:
        conf_at, conf_src, changed = manual["confirmed_at"], manual.get("source") or "manual", True
    orig_ph = bool(log[0].get("placeholder"))
    delta_h = None if (orig_ph or rev == 0) else round((kick - original).total_seconds() / 3600.0, 3)
    m.update({
        "kickoff_at": iso(kick), "kickoff_original": iso(original), "kickoff_actual": iso(kick) if rev else None,
        "kickoff_rev": rev, "kickoff_log": log, "kickoff_original_placeholder": orig_ph,
        "postponed_announced_at": None, "postpone_ts_unknown": bool(rev) and not orig_ph,
        "kickoff_known_by": log[-1]["detected_at"] if rev else None,
        "placeholder_ever": ph_ever, "kickoff_placeholder_suspect": ph and conf_at is None,
        "kickoff_confirmed": conf_at is not None, "kickoff_confirmed_at": conf_at,
        "kickoff_confirm_source": conf_src,
        "postpone_delta_h": delta_h,
        "void_postponed": (delta_h is not None and delta_h > POSTPONE_VOID_HOURS),
        "postpone_void_hours": POSTPONE_VOID_HOURS, "postpone_void_src": POSTPONE_VOID_SRC,
    })
    return changed


def _exception_basis(m: dict) -> datetime:
    """例外场按编号 + 原定开赛判（与后端 channel_targets 推迟口径一致）；原定开赛本身是 12:00 占位时改用当前开赛。"""
    if m.get("kickoff_original_placeholder") or not m.get("kickoff_original"):
        return parse(m["kickoff_at"])
    return parse(m["kickoff_original"])


def phase_slots(D: str, kick: datetime, exc_basis: datetime | None = None) -> dict:
    """给定开赛时间，返回各阶段目标时刻 {(phase, variant): dt}（不含 11:10 和占位 15/22）。"""
    base = datetime.strptime(D, "%Y-%m-%d").replace(tzinfo=TZ)
    eb = exc_basis or kick
    exc = cs.phase_exception(D, eb, eb.hour, True, True)
    if exc:
        out = {("mid", "rule"): base.replace(hour=15), ("close", "rule"): base.replace(hour=22)}
        act = cs.actual_targets(kick)
        out[("mid", "real")], out[("close", "real")] = act["t8"], act["t1"]
    else:
        k = kick.replace(second=0, microsecond=0)
        out = {("mid", "rule"): k - timedelta(hours=8), ("close", "rule"): k - timedelta(hours=1)}
    return out


def kickoff_as_of(m: dict, at: datetime) -> datetime:
    """到时刻 at 为止已经知道的开赛时间：取 detected_at ≤ at 的最后一版（detected_at 是公布时刻的上界）；
    都晚于 at 时取第 0 版。"""
    log = m.get("kickoff_log") or [{"kickoff_at": m["kickoff_at"], "detected_at": None}]
    best = log[0]
    for e in log[1:]:
        d = parse(e.get("announced_at") or e.get("detected_at"))
        if d is not None and d <= at:
            best = e
    return parse(best["kickoff_at"])


def phase_at(D: str, m: dict, at: datetime) -> list[str]:
    """时刻 at 按「当时已公布的开赛时间」落在哪个阶段（可能同时是 mid|rule 和 mid|real）；不落在任何阶段 → []。"""
    k = kickoff_as_of(m, at)
    eb = k if m.get("kickoff_original_placeholder") else (parse(m.get("kickoff_original")) or k)
    return sorted(f"{ph}|{var}" for (ph, var), t in phase_slots(D, k, eb).items() if t == at)


def _match_targets(D: str, m: dict, now: datetime, prev_targets: list[dict], st: dict, klog: list) -> list[dict]:
    base = datetime.strptime(D, "%Y-%m-%d").replace(tzinfo=TZ)
    kick = parse(m["kickoff_at"])
    uid, fid, rev = m["match_uid"], m["fixture_id"], int(m.get("kickoff_rev") or 0)
    der_ph = is_placeholder_kick(kick) and not m.get("kickoff_confirmed")
    slots = [(ph, var, at, True) for (ph, var), at in phase_slots(D, kick, _exception_basis(m)).items()]
    slots.append(("live", "rule_1110", base.replace(hour=11, minute=10), False))
    if m.get("placeholder_ever"):
        slots += [("pending", var, base.replace(hour=h, minute=mi), False) for var, h, mi in PENDING_CLOCKS]
    by_slot: dict[tuple, list[dict]] = {}
    for t in prev_targets:
        by_slot.setdefault((t["phase"], t["phase_variant"]), []).append(t)
    out, superseded = [], []
    for ph, var, at, dep in slots:
        ch, pt = SNAP_KEY[(ph, var)]
        olds = by_slot.pop((ph, var), [])
        same = next((o for o in olds if o["target_at"] == iso(at)), None)
        if not same and dep and rev:
            # 复用本 rev 已建的补抓目标（target_at 是发现时刻，不等于理论 at）
            same = next((o for o in olds if o.get("catchup") and o.get("planned_target_at") == iso(at)), None)
        key = f"{uid}|{ph}|{var}" if (not dep or rev == 0) else f"{uid}|{ph}|{var}|k{rev}"
        if same:
            key = same["target_key"]
        for o in olds:
            if o is same or o["target_key"] in st or o["target_key"] == key:
                continue                                    # 已抓/已错过的旧目标留在 state 里当历史
            if parse(o["target_at"]) - WIN_BEFORE > now:
                superseded.append(o["target_key"])          # 新开赛时间在旧目标之前就已知 → 旧目标作废
            else:
                out.append(o)                               # 已进窗口/已过：按原样走 due/missed
        if same and same.get("catchup"):
            out.append(same)                                # 保留既有补抓（发现时刻 / phase_assign_late）
            continue
        if dep and rev and not same and at + WIN_AFTER < now:
            # 开赛提前空档：新目标已过窗口 → 立刻补抓一次（决议 18:36）
            if now >= kick:
                klog.append({"ts": iso(now), "jingcai_date": D, "match_uid": uid,
                             "event": "new_target_already_past", "slot": f"{ph}|{var}",
                             "target_at": iso(at), "kickoff_at": iso(kick),
                             "note": "改期发现时新目标已过且已开赛；无法补抓"})
                continue
            klog.append({"ts": iso(now), "jingcai_date": D, "match_uid": uid,
                         "event": "catchup_on_discovery", "slot": f"{ph}|{var}",
                         "planned_target_at": iso(at), "catchup_at": iso(now),
                         "note": "开赛提前导致新目标已过；立刻补抓，phase_assign_late=true"})
            out.append({"target_key": key, "match_uid": uid, "fixture_id": fid, "phase": ph, "phase_variant": var,
                        "channel": ch, "point": pt, "target_at": iso(now), "kickoff_at": iso(kick),
                        "planned_target_at": iso(at), "catchup": True, "phase_assign_late": True,
                        "kickoff_rev": rev, "kickoff_dependent": dep,
                        "derived_from_placeholder": bool(dep and der_ph),
                        "target_basis": "catchup_on_discovery",
                        "phase_pending": False,
                        "features_ok": not (dep and der_ph)})
            continue
        out.append({"target_key": key, "match_uid": uid, "fixture_id": fid, "phase": ph, "phase_variant": var,
                    "channel": ch, "point": pt, "target_at": iso(at), "kickoff_at": iso(kick),
                    "kickoff_rev": rev if dep else None, "kickoff_dependent": dep,
                    "derived_from_placeholder": bool(dep and der_ph),
                    "target_basis": ("fixed_clock" if not dep else
                                     "kickoff_known_by_detection" if rev else "original"),
                    "phase_pending": ph == "pending",
                    "features_ok": not ((dep and der_ph) or ph == "pending"),
                    "catchup": False, "phase_assign_late": False})
    for (ph, var), olds in by_slot.items():                 # 本轮不再有的阶段（如例外→非例外的真实列）
        for o in olds:
            if o["target_key"] in st:
                continue
            if parse(o["target_at"]) - WIN_BEFORE > now:
                superseded.append(o["target_key"])
            else:
                out.append(o)
    if superseded:
        rec = {"ts": iso(now), "jingcai_date": D, "match_uid": uid, "event": "targets_superseded",
               "kickoff_rev": rev, "superseded": superseded}
        klog.append(rec)
        append_jsonl(D_LOG / "kickoff_changes.jsonl", rec)
    return out


def build_plan(D: str, csl: dict, prev: dict | None = None, now: datetime | None = None) -> dict:
    """竞彩日 D 的场次与目标。每次刷新都和上一版计划比开赛时间：变了就记 kickoff_log、重算依赖开赛时间的目标
    （真实列、非例外场规则列）；12:00 占位疑似场另加 D 日 15:00 / 22:00 待归阶段目标。"""
    now = now or now_cn()
    if prev is None:
        prev = load_plan(D)
    prev = prev or {}
    prev_m = {m["match_uid"]: m for m in prev.get("matches") or []}
    prev_t: dict[str, list] = {}
    for t in prev.get("targets") or []:
        prev_t.setdefault(t["match_uid"], []).append(t)
    st = jload(state_path(D), {})
    manual = jload(D_STATE / "kickoff_confirm.json", {})
    matches, targets, missing = [], [], []
    klog = list(prev.get("kickoff_log") or [])
    n_log0 = len(klog)
    changed = []
    seen = set()
    for it in csl.get("data") or []:
        lot = (it.get("lottery") or {}).get("jingcailottery") or {}
        num = lot.get("number")
        if not num or not it.get("kickoff_ts"):
            continue
        kick = datetime.fromtimestamp(int(it["kickoff_ts"]), TZ)
        code = short_code(num)
        uid = f"{D}|{code}"
        pm = prev_m.get(uid)
        # 竞彩日只认编号；已在计划里的场改期后即使跨日也留在 D（编号不变）
        if cs.jingcai_date_from_code(num, kick) != D and pm is None:
            continue
        fid = it.get("id")
        m = {"match_uid": uid, "jingcai_date": D, "jc_code": code, "jc_number": num,
             "fixture_id": fid, "league": (it.get("league") or {}).get("name"),
             "home": ((it.get("teams") or {}).get("home") or {}).get("name"),
             "away": ((it.get("teams") or {}).get("away") or {}).get("name"),
             "kickoff_source": "5df", "status": it.get("status"),
             "has_jc_odds": bool(lot.get("odds"))}
        if _merge_kickoff(D, m, pm, kick, now, it.get("status"), manual.get(uid), prev.get("built_at"), klog):
            changed.append(uid)
        seen.add(uid)
        if not fid:
            missing.append({**m, "reason": "no_5df_fixture_id"})
            continue
        m["exception"] = cs.phase_exception(D, _exception_basis(m), _exception_basis(m).hour, True, True)
        targets += _match_targets(D, m, now, prev_t.get(uid, []), st, klog)
        matches.append(m)
    for uid in sorted(set(prev_m) - seen):
        rec = {"ts": iso(now), "jingcai_date": D, "match_uid": uid, "event": "dropped_from_csl"}
        if not any(e.get("event") == "dropped_from_csl" and e.get("match_uid") == uid for e in klog):
            klog.append(rec)
            append_jsonl(D_LOG / "kickoff_changes.jsonl", rec)
    # 编号断号 = 可能有竞彩场没进 5DF CSL（映射缺失），只记不猜
    nos = sorted(int(m["jc_code"][1:]) for m in matches if m["jc_code"][1:].isdigit())
    gaps = sorted(set(range(1, max(nos) + 1)) - set(nos)) if nos else []
    if nos and min(nos) > 200:  # 2xx 段（北欧/亚洲加开）单独看
        gaps = sorted(set(range(min(nos), max(nos) + 1)) - set(nos))
    plan = {"jingcai_date": D, "built_at": iso(now), "csl_fetched_at": csl.get("fetched_at"),
            "exception_rule": cs.EXCEPTION_RULE, "phase_target": "exact_minute",
            "postpone_void_hours": POSTPONE_VOID_HOURS, "postpone_void_src": POSTPONE_VOID_SRC,
            "n_matches": len(matches), "n_targets": len(targets), "jc_number_gaps": gaps,
            "n_placeholder_suspect": sum(1 for m in matches if m.get("kickoff_placeholder_suspect")),
            "changed_this_build": changed, "kickoff_log": klog, "kickoff_log_new": len(klog) - n_log0,
            "missing_mapping": missing, "matches": matches,
            "targets": sorted(targets, key=lambda t: t["target_at"])}
    for mm in missing:
        append_jsonl(D_LOG / "missing_mapping.jsonl", {"ts": iso(now), **mm})
    jdump(D_PLAN / f"{D}.json", plan)
    return plan


def cross_check_dispatch(plan: dict) -> dict:
    """库里（现网 + v2d3）若已有这些 match_uid，用后端同一函数按库内开赛时间重算并比对；新竞彩日一般没有。"""
    out = {"checked": 0, "mismatch": []}
    for db in (PROD_DB, REPLICA_DB):
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            c.row_factory = sqlite3.Row
        except sqlite3.Error:
            continue
        idx = {t["target_key"]: t for t in plan["targets"]}
        for m in plan["matches"]:
            r = c.execute("SELECT jingcai_date,kickoff_hour,kickoff_at,kickoff_minute_known,jc_id FROM matches"
                          " WHERE match_uid=?", (m["match_uid"],)).fetchone()
            if not r or r["kickoff_hour"] is None:
                continue
            k = parse(r["kickoff_at"]) if r["kickoff_at"] else None
            rt = cs.rule_targets(r["jingcai_date"], r["kickoff_hour"], k,
                                 bool(r["kickoff_minute_known"]), has_jc_code=bool(r["jc_id"]))
            out["checked"] += 1
            for ph, at in (("mid", rt[0]), ("close", rt[1])) if rt else ():
                t = idx.get(f"{m['match_uid']}|{ph}|rule")
                if t and parse(t["target_at"]) != at:
                    out["mismatch"].append({"db": db.name if db != REPLICA_DB else "v2d3",
                                            "match_uid": m["match_uid"], "phase": ph,
                                            "plan": t["target_at"], "db_calc": iso(at)})
        c.close()
    return out


def load_plan(D: str) -> dict | None:
    return jload(D_PLAN / f"{D}.json", None)


def ensure_plan(cl: Client | None, D: str, max_age: timedelta | None, force=False) -> dict | None:
    p = load_plan(D)
    if cl is None:
        return p
    if p and not force and max_age is not None and now_cn() - parse(p["built_at"]) < max_age:
        return p
    if p and not force and max_age is None:
        return p
    try:
        p = build_plan(D, fetch_csl_day(cl, D))
        health("plan_refreshed", jingcai_date=D, n_matches=p["n_matches"], n_targets=p["n_targets"],
               missing_mapping=len(p["missing_mapping"]), jc_number_gaps=p["jc_number_gaps"])
    except Exception as e:
        health("plan_error", jingcai_date=D, error=str(e)[:300])
    return p


# ----------------------------------------------------------------------------- capture

def state_path(D: str) -> Path:
    return D_STATE / f"captured_{D}.json"


def hk(price) -> float | None:
    return None if price is None else round(float(price) - 1.0, 4)


def normalize(raw: dict, tgt: dict, match: dict, fetched: datetime, raw_rel: str, sha: str,
              late: bool) -> list[dict]:
    rows = []
    target = parse(tgt["target_at"])
    kick = parse(tgt["kickoff_at"])
    lag = round((fetched - target).total_seconds() / 60.0, 2)
    for bk in ((raw.get("data") or {}).get("bookmakers") or []):
        slug = bk.get("slug")
        book = BOOKS.get(slug, slug)
        for mk, market in MARKETS.items():
            o = (bk.get("odds") or {}).get(mk) or {}
            cur, opn = o.get("closing"), o.get("opening")
            if not cur:
                continue
            r = {"capture_id": f"{tgt['target_key']}|{book}|{market}", "target_key": tgt["target_key"],
                 "match_uid": tgt["match_uid"], "jingcai_date": match["jingcai_date"],
                 "jc_code": match["jc_code"], "fixture_id": tgt["fixture_id"], "league": match["league"],
                 "home_5df": match["home"], "away_5df": match["away"], "kickoff_at": tgt["kickoff_at"],
                 "kickoff_source": "5df", "exception": match.get("exception"),
                 "phase": tgt["phase"], "phase_variant": tgt["phase_variant"],
                 "channel": tgt["channel"], "point": tgt["point"],
                 "label": "rule_1110" if tgt["phase"] == "live" else None,
                 "target_at": tgt["target_at"], "fetched_at": iso(fetched), "fetch_lag_min": lag,
                 "minutes_before_kickoff": round((kick - fetched).total_seconds() / 60.0, 1),
                 "late": late, "book": book, "book_slug": slug, "market": market,
                 "line_api": None, "line_home_gives": None,
                 "price_home": None, "price_draw": None, "price_away": None,
                 "price_over": None, "price_under": None,
                 "water_home": None, "water_away": None, "water_over": None, "water_under": None,
                 "water_source": "actual", "odds_source": "live", "api_field": "closing(current)",
                 "inplay_present": o.get("inplay") is not None,
                 "open_line_api": None, "open_home": None, "open_draw": None, "open_away": None,
                 "open_over": None, "open_under": None,
                 "raw_path": raw_rel, "raw_sha256": sha, "qa_flags": [],
                 # 占位/改期口径（决议第 3 条 + 18:22 补充）；入库时按最新计划重算 phase_status
                 "phase_pending": bool(tgt.get("phase_pending")), "features_ok": tgt.get("features_ok", True),
                 "kickoff_dependent": tgt.get("kickoff_dependent", tgt["phase"] != "live"),
                 "derived_from_placeholder": bool(tgt.get("derived_from_placeholder")),
                 "kickoff_rev": tgt.get("kickoff_rev"), "target_basis": tgt.get("target_basis"),
                 "kickoff_placeholder_suspect": bool(match.get("kickoff_placeholder_suspect")),
                 "catchup": bool(tgt.get("catchup")), "phase_assign_late": bool(tgt.get("phase_assign_late")),
                 "planned_target_at": tgt.get("planned_target_at")}
            if market == "asian":
                r["line_api"] = cur.get("line")
                r["line_home_gives"] = -float(cur["line"]) if cur.get("line") is not None else None
                r["price_home"], r["price_away"] = cur.get("home"), cur.get("away")
                r["water_home"], r["water_away"] = hk(cur.get("home")), hk(cur.get("away"))
            elif market == "ou":
                r["line_api"] = cur.get("line")
                r["price_over"], r["price_under"] = cur.get("over"), cur.get("under")
                r["water_over"], r["water_under"] = hk(cur.get("over")), hk(cur.get("under"))
            else:
                r["price_home"], r["price_draw"], r["price_away"] = cur.get("home"), cur.get("draw"), cur.get("away")
            if opn:
                r["open_line_api"] = opn.get("line")
                for k in ("home", "draw", "away", "over", "under"):
                    r[f"open_{k}"] = opn.get(k)
            for w in ("water_home", "water_away", "water_over", "water_under"):
                v = r[w]
                if v is not None and not (WATER_MIN <= v <= WATER_MAX):
                    r["qa_flags"].append(f"{w}_out_of_range")
            if r["inplay_present"]:
                r["qa_flags"].append("inplay_present")
            if match.get("kickoff_placeholder_suspect"):
                r["qa_flags"].append("kickoff_placeholder_5df_1200_unverified")
            if tgt.get("phase_pending"):
                r["label"] = f"placeholder_{tgt['phase_variant']}"
            rows.append(r)
    return rows


def capture_group(cl: Client, D: str, fid, tgts: list[dict], plan: dict, late=False) -> dict:
    match = next(m for m in plan["matches"] if m["fixture_id"] == fid)
    code, raw = cl.get(f"odds_{fid}", f"/fixtures/{fid}/odds", {"bookmakers": ",".join(BOOKS)})
    fetched = now_cn()
    tags = "+".join(f"{t['phase']}-{t['phase_variant']}" for t in tgts)
    rel = f"all/{D}/{fid}_{fetched:%Y%m%dT%H%M%S}_{tags}.json"
    body = {"_meta": {"fetched_at": iso(fetched), "http": code, "fixture_id": fid,
                      "match_uid": match["match_uid"], "targets": [t["target_key"] for t in tgts],
                      "target_at": [t["target_at"] for t in tgts], "odds_source": "live",
                      "request": f"/v1/fixtures/{fid}/odds?bookmakers={','.join(BOOKS)}",
                      "ratelimit_remaining": cl.remaining}, **raw}
    p = LIVE / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(body, ensure_ascii=False, indent=1).encode("utf-8")
    p.write_bytes(data)
    sha = hashlib.sha256(data).hexdigest()
    # 平博亚盘切片（口径卡 §7 指定路径）
    pin = next((b for b in ((raw.get("data") or {}).get("bookmakers") or []) if b.get("slug") == "pinnacle"), None)
    jdump(D_PIN / D / f"{fid}_{fetched:%Y%m%dT%H%M%S}_{tags}.json",
          {"_meta": {**body["_meta"], "slice_of": rel},
           "pinnacle_asian_handicap": ((pin or {}).get("odds") or {}).get("asian_handicap")})
    st = jload(state_path(D), {})
    books_seen = sorted({BOOKS.get(b.get("slug"), b.get("slug"))
                         for b in ((raw.get("data") or {}).get("bookmakers") or [])})
    nrows = 0
    for t in tgts:
        rows = normalize(raw, t, match, fetched, rel, sha, late) if code == 200 else []
        for r in rows:
            append_jsonl(D_STAGE / f"{D}.jsonl", r)
        nrows += len(rows)
        st[t["target_key"]] = {"status": ("captured_late" if late else "captured") if code == 200 and rows
                               else ("no_odds" if code == 200 else f"http_{code}"),
                               "fetched_at": iso(fetched), "target_at": t["target_at"], "raw_path": rel,
                               "rows": len(rows),
                               "books_asian": sorted({r["book"] for r in rows if r["market"] == "asian"})}
    jdump(state_path(D), st)
    health("capture", fixture_id=fid, match_uid=match["match_uid"], targets=[t["target_key"] for t in tgts],
           http=code, rows=nrows, books=books_seen, remaining=cl.remaining, late=late)
    return {"http": code, "rows": nrows, "raw": rel, "books": books_seen}


def due_targets(plans: list[dict], now: datetime) -> tuple[list[dict], list[dict]]:
    due, missed = [], []
    for plan in plans:
        st = jload(state_path(plan["jingcai_date"]), {})
        for t in plan["targets"]:
            if t["target_key"] in st:
                continue
            at, kick = parse(t["target_at"]), parse(t["kickoff_at"])
            if now >= kick:
                if now - at > WIN_AFTER:
                    missed.append(t)
                continue
            if at - WIN_BEFORE <= now <= at + WIN_AFTER:
                due.append({**t, "_D": plan["jingcai_date"]})
            elif now > at + WIN_AFTER:
                missed.append(t)
    return due, missed


def mark_missed(missed: list[dict]) -> None:
    """记 missed，并把适合补救的目标幂等写入 rescue_queue/pending（R-C，不调 API）。"""
    byD: dict[str, list] = {}
    for t in missed:
        byD.setdefault(t["match_uid"].split("|")[0], []).append(t)
    for D, ts in byD.items():
        st = jload(state_path(D), {})
        for t in ts:
            st[t["target_key"]] = {"status": "missed", "target_at": t["target_at"], "noted_at": iso(now_cn())}
            health("missed", target_key=t["target_key"], target_at=t["target_at"])
        jdump(state_path(D), st)
    # R-C：只入队，不在热路径 asof（让路窗 / 11:10 / due 避让由 rescue_worker 遵守）
    try:
        from rescue_queue import enqueue_missed  # noqa: WPS433
        written = enqueue_missed(missed, reason="missed_after_window")
        for p in written:
            health("rescue_enqueued", path=str(p))
    except Exception as e:  # 入队失败不影响 missed 记状态
        health("rescue_enqueue_error", error=str(e)[:200])


# ----------------------------------------------------------------------------- phase status（入库时重算）

def phase_status(D: str, r: dict, m: dict | None) -> dict:
    """按最新计划里的场次状态，给一行快照算阶段归属与特征可用性（决议第 3 条 + 18:22 补充）：

    - 待归阶段行（占位疑似场 D 日 15:00/22:00）：开赛时间确认前 phase_pending=true、phase_assigned=null；
      确认后按「到目标时刻为止已公布的开赛时间」归阶段（与推迟场同口径），确认晚于目标 → phase_assign_late=true。
    - 由占位开赛推出的目标：确认前 features_ok=false；确认后开赛时间没变 → features_ok=true，
      确认晚于目标 → phase_assign_late=true；开赛时间变了 → features_ok=false、superseded_by_kickoff_change=true。
    - 开赛提前空档补抓行（catchup=true）：phase_assign_late=true（live 不计命中）；target_at 为发现时刻。
    - 其余：features_ok=true、phase_assign_late=false。
    """
    T = parse(r["target_at"])
    conf = parse(m.get("kickoff_confirmed_at")) if m else None
    out = {"phase_pending": False, "phase_assigned": None, "phase_assign_late": False, "features_ok": True,
           "kickoff_confirmed": conf is not None, "kickoff_confirmed_at": iso(conf) if conf else None,
           "kickoff_confirm_source": (m or {}).get("kickoff_confirm_source")}
    if r["phase"] == "pending":
        if m is None or conf is None:
            out.update({"phase_pending": True, "phase_assign_late": None, "features_ok": False,
                        "phase_if_current_kickoff_holds": phase_at(D, m, T) if m else None})
            return out
        ph = phase_at(D, m, T)
        out.update({"phase_assigned": ph, "phase_assign_late": conf > T, "features_ok": bool(ph),
                    "kickoff_used": iso(kickoff_as_of(m, T))})
        return out
    if r.get("derived_from_placeholder"):
        if m is None or conf is None:
            out.update({"phase_assign_late": None, "features_ok": False})
            return out
        same = parse(m["kickoff_at"]) == parse(r["kickoff_at"])
        out.update({"phase_assign_late": conf > T, "features_ok": same,
                    "superseded_by_kickoff_change": not same})
        return out
    # 开赛提前空档补抓：行上已标 phase_assign_late / catchup → live 不计命中
    if r.get("catchup") or r.get("phase_assign_late"):
        out.update({"phase_assign_late": True,
                    "catchup": bool(r.get("catchup")),
                    "planned_target_at": r.get("planned_target_at"),
                    "features_ok": r.get("features_ok", True)})
    return out


# ----------------------------------------------------------------------------- ingest (v2d3 only)

def staged_rows(D: str) -> list[dict]:
    p = D_STAGE / f"{D}.jsonl"
    if not p.exists():
        return []
    rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    last: dict[str, dict] = {}
    for r in rows:  # 同一 capture_id 只认最后一次（幂等重放）
        last[r["capture_id"]] = r
    return list(last.values())


def backup_replica(db: Path) -> Path:
    D_BAK.mkdir(parents=True, exist_ok=True)
    out = D_BAK / f"v2d3_app_{now_cn():%Y%m%dT%H%M%S}.db"
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    dst = sqlite3.connect(str(out))
    src.backup(dst)
    dst.close()
    src.close()
    # 轮换：保留最近 24 份 + 每天第一份
    baks = sorted(D_BAK.glob("v2d3_app_*.db"))
    firsts = {}
    for b in baks:
        firsts.setdefault(b.name[9:17], b)
    for b in baks[:-24]:
        if b not in firsts.values():
            b.unlink()
    return out


def team_ids(conn, name: str | None) -> set[int]:
    if not name:
        return set()
    ids = {r[0] for r in conn.execute("SELECT team_id FROM team_aliases WHERE alias=?", (name,))}
    ids |= {r[0] for r in conn.execute("SELECT id FROM teams WHERE name_zh_canonical=?", (name,))}
    return ids


def orientation(conn, m: sqlite3.Row, home5: str, away5: str) -> str:
    if (m["home_team"], m["away_team"]) == (home5, away5):
        return "same"
    if (m["home_team"], m["away_team"]) == (away5, home5):
        return "swapped"
    mh = {m["home_team_id"]} if m["home_team_id"] else team_ids(conn, m["home_team"])
    ma = {m["away_team_id"]} if m["away_team_id"] else team_ids(conn, m["away_team"])
    h5, a5 = team_ids(conn, home5), team_ids(conn, away5)
    if (mh & h5) or (ma & a5):
        return "same"
    if (mh & a5) or (ma & h5):
        return "swapped"
    return "unverified"


def flip(r: dict) -> dict:
    r = dict(r)
    if r["market"] == "asian" and r["line_api"] is not None:
        r["line_api"] = -float(r["line_api"])
        r["line_home_gives"] = -float(r["line_api"])
        if r["open_line_api"] is not None:
            r["open_line_api"] = -float(r["open_line_api"])
    r["price_home"], r["price_away"] = r["price_away"], r["price_home"]
    r["water_home"], r["water_away"] = r["water_away"], r["water_home"]
    r["open_home"], r["open_away"] = r["open_away"], r["open_home"]
    return r


def ingest(D: str, db: Path = REPLICA_DB, dry_run=False, create_missing_matches: bool | None = None) -> dict:
    db = db.resolve()
    if db == PROD_DB or db.name != "app.db" or db.parent.name != "v2d3":
        raise SystemExit(f"拒绝写入 {db}：只允许 v2d3 研究副本（…/v2d3/app.db）")
    if os.environ.get("DUAL_WRITE_ODDS_ASIAN", "0").strip().lower() in ("1", "true", "yes", "on"):
        raise SystemExit("DUAL_WRITE_ODDS_ASIAN 被打开了；本脚本要求保持关闭")
    if create_missing_matches is None:  # 默认关；只有显式打开（参数或环境变量）才在副本里补建场次行
        create_missing_matches = os.environ.get("LIVE_CAPTURE_CREATE_MATCHES", "0") == "1"
    rows = staged_rows(D)
    ist_p = D_STATE / f"ingest_{D}.json"
    ist = jload(ist_p, {})
    rep = {"jingcai_date": D, "db": str(db), "dry_run": dry_run, "staged": len(rows), "counts": {},
           "backup": None, "sign_check": None, "written": 0, "unchanged": 0}

    def setst(r, s, **kw):
        ist[r["capture_id"]] = {"status": s, "fetched_at": r["fetched_at"], **kw}
        rep["counts"][s] = rep["counts"].get(s, 0) + 1

    plan_m = {m["match_uid"]: m for m in (load_plan(D) or {}).get("matches") or []}
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True) if dry_run else sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        cand, mrows = [], {}
        for r in rows:
            m = mrows.get(r["match_uid"])
            if m is None:
                m = conn.execute("SELECT * FROM matches WHERE match_uid=?", (r["match_uid"],)).fetchone()
                mrows[r["match_uid"]] = m or False
            if not m and create_missing_matches and not dry_run:
                m = create_stub_match(conn, db, D, r, rep)
                mrows[r["match_uid"]] = m or False
            if not m:
                setst(r, "no_replica_match")
                continue
            if r["late"] or not (INGEST_LAG_MIN[0] <= r["fetch_lag_min"] <= INGEST_LAG_MIN[1]):
                setst(r, "outside_window_not_ingested")
                continue
            if any(f.endswith("_out_of_range") for f in r["qa_flags"]):
                setst(r, "water_out_of_range")
                continue
            if "inplay_present" in r["qa_flags"]:
                setst(r, "inplay_not_ingested")
                continue
            o = orientation(conn, m, r["home_5df"], r["away_5df"])
            rr = flip(r) if o == "swapped" else dict(r)
            rr["orientation"] = o
            rr["_match_id"] = m["id"]
            cand.append(rr)
        # 正负号校验（后端 validate_ah_sign.check_rows，正数=主让）：open=5DF opening，mid/close=我们的规则快照
        sign_rows, seen_open = [], set()
        for r in cand:
            if r["market"] != "asian" or r["line_home_gives"] is None:
                continue
            ex = json.dumps({"source": SOURCE})
            if (r["match_uid"], r["book"]) not in seen_open and r["open_line_api"] is not None:
                seen_open.add((r["match_uid"], r["book"]))
                sign_rows.append({"match_id": r["_match_id"], "match_uid": r["match_uid"], "book": r["book"],
                                  "phase": "open", "handicap": -float(r["open_line_api"]), "extras_json": ex})
            if r["phase_variant"] == "rule" and r["phase"] in ("mid", "close"):
                sign_rows.append({"match_id": r["_match_id"], "match_uid": r["match_uid"], "book": r["book"],
                                  "phase": r["phase"], "handicap": r["line_home_gives"], "extras_json": ex})
        sc = check_rows(sign_rows, phase="mid")
        rep["sign_check"] = {k: sc[k] for k in ("status", "blocked")} | {
            "review": sc["review"], "batches": [{k: b[k] for k in ("book", "rows", "non_flat", "opposite_both",
                                                                  "opposite_ratio", "blocked")}
                                                for b in sc["batches"]]}
        blocked_books = {b.split("|")[0] for b in sc["blocked"]}
        review_keys = {(x["match_uid"], x["book"]) for x in sc["review"]}
        to_write = []
        for r in cand:
            if r["market"] == "asian" and r["book"] in blocked_books and r["phase"] == "mid" \
                    and r["phase_variant"] == "rule":
                setst(r, "sign_batch_blocked")
            elif r["market"] == "asian" and (r["match_uid"], r["book"]) in review_keys and r["phase"] == "mid" \
                    and r["phase_variant"] == "rule":
                setst(r, "sign_review")
            else:
                to_write.append(r)
        # 已入库但事后被正负号校验拦下的中盘 → 从副本撤下（留 staging，待人工复核）
        retract = [r for r in cand if ist.get(r["capture_id"], {}).get("status") in ("sign_review", "sign_batch_blocked")
                   and rep_prev_written(D, r["capture_id"])]
        if dry_run:
            rep["would_write"] = len(to_write)
            rep["would_retract"] = len(retract)
            return rep
        if to_write or retract:
            rep["backup"] = str(backup_replica(db))
        conn.execute("BEGIN")
        undo = []
        if to_write or retract:
            if ensure_kickoff_rev_column(conn):
                undo.append({"op": "alter_matches_add_kickoff_rev"})
                rep["added_kickoff_rev_column"] = True
        for r in retract:
            old = conn.execute("SELECT * FROM odds_snapshot WHERE match_id=? AND book=? AND market=? AND channel=? AND point=?"
                               " AND source=?", (r["_match_id"], r["book"], r["market"], r["channel"], r["point"],
                                                 SOURCE)).fetchone()
            if old:
                undo.append({"op": "delete", "old": dict(old)})
                conn.execute("DELETE FROM odds_snapshot WHERE id=?", (old["id"],))
        for r in to_write:
            target, fetched = parse(r["target_at"]), parse(r["fetched_at"])
            pm = plan_m.get(r["match_uid"])
            ps = phase_status(D, r, pm)
            ex = {"capture": "own", "odds_source": "live",
                  "phase": None if r["phase"] == "pending" else r["phase"], "phase_variant": r["phase_variant"],
                  "label": r["label"], "fetched_at": r["fetched_at"], "target_at": r["target_at"],
                  "fetch_lag_min": r["fetch_lag_min"], "minutes_before_kickoff": r["minutes_before_kickoff"],
                  "fixture_id": r["fixture_id"], "book_slug": r["book_slug"], "api_field": r["api_field"],
                  "orientation": r["orientation"], "kickoff_source": "5df", "kickoff_at_5df": r["kickoff_at"],
                  "phase_target": "exact_minute", "exception_rule": cs.EXCEPTION_RULE,
                  "tick_age_rule": "live_fetched_at", "water_source": "actual",
                  "open_api": {"line": r["open_line_api"], "home": r["open_home"], "draw": r["open_draw"],
                               "away": r["open_away"], "over": r["open_over"], "under": r["open_under"]},
                  "raw_path": r["raw_path"], "raw_sha256": r["raw_sha256"], "qa_flags": r["qa_flags"],
                  "match_uid": r["match_uid"],
                  **ps,
                  # 0.3.20 必写三字段（列缺失时落 extras；phase_status 已给 bool/None）
                  "phase_pending": bool(ps.get("phase_pending")),
                  "features_ok": True if ps.get("features_ok") is None else bool(ps.get("features_ok")),
                  "phase_assign_late": False if ps.get("phase_assign_late") is None
                                      else bool(ps.get("phase_assign_late")),
                  "kickoff_dependent": r.get("kickoff_dependent"),
                  "derived_from_placeholder": r.get("derived_from_placeholder", False),
                  # 比赛级 kickoff_rev（计划）；目标上的 rev 可能为 None（非依赖开赛的 11:10）
                  "kickoff_rev": int((pm or {}).get("kickoff_rev") or 0),
                  "target_basis": r.get("target_basis"),
                  "catchup": bool(r.get("catchup")), "planned_target_at": r.get("planned_target_at"),
                  "kickoff_placeholder_suspect": bool((pm or {}).get("kickoff_placeholder_suspect")),
                  "kickoff_original": (pm or {}).get("kickoff_original"),
                  "kickoff_actual": (pm or {}).get("kickoff_actual"),
                  "postponed_announced_at": None,
                  "postpone_ts_unknown": bool((pm or {}).get("postpone_ts_unknown")),
                  "kickoff_known_by": (pm or {}).get("kickoff_known_by"),
                  "void_postponed": bool((pm or {}).get("void_postponed")),
                  "postpone_void_hours": POSTPONE_VOID_HOURS, "postpone_void_src": POSTPONE_VOID_SRC}
            vals = {"match_id": r["_match_id"], "book": r["book"], "market": r["market"], "channel": r["channel"],
                    "point": r["point"], "recorded_at": r["fetched_at"], "target_at": r["target_at"],
                    "lag_hours": round((fetched - target).total_seconds() / 3600.0, 4),
                    "stale_gap": 0, "line": r["line_api"], "price_home": r["price_home"],
                    "price_away": r["price_away"], "price_draw": r["price_draw"], "price_over": r["price_over"],
                    "price_under": r["price_under"], "water_home": r["water_home"], "water_away": r["water_away"],
                    "water_over": r["water_over"], "water_under": r["water_under"], "water_src": "actual",
                    "water_censored": None, "source": SOURCE,
                    "extras_json": json.dumps(ex, ensure_ascii=False, sort_keys=True)}
            old = conn.execute("SELECT * FROM odds_snapshot WHERE match_id=? AND book=? AND market=? AND channel=?"
                               " AND point=?", (vals["match_id"], vals["book"], vals["market"], vals["channel"],
                                                vals["point"])).fetchone()
            if old and old["source"] != SOURCE:
                setst(r, "conflict_non_live_row_kept", existing_source=old["source"])
                continue
            if old and all(old[k] == v for k, v in vals.items()):
                rep["unchanged"] += 1
                setst(r, "ingested")
                continue
            undo.append({"op": "upsert", "old": dict(old) if old else None, "key": [vals[k] for k in
                         ("match_id", "book", "market", "channel", "point")]})
            cols = list(vals)
            conn.execute(
                f"INSERT INTO odds_snapshot ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
                "ON CONFLICT(match_id, book, market, channel, point) DO UPDATE SET "
                + ",".join(f"{c}=excluded.{c}" for c in cols if c not in ("match_id", "book", "market", "channel", "point")),
                [vals[c] for c in cols])
            rep["written"] += 1
            setst(r, "ingested")
        # 比赛行 kickoff_rev（0.3.20）：有列写 matches.kickoff_rev，extras 也写
        synced = set()
        for r in to_write:
            mid = r["_match_id"]
            if mid in synced:
                continue
            synced.add(mid)
            sync_match_kickoff_rev(conn, mid, plan_m.get(r["match_uid"]), undo)
        rep["matches_kickoff_rev_synced"] = len(synced)
        conn.commit()
        if undo:
            append_jsonl(D_LOG / "ingest_undo.jsonl", {"ts": iso(now_cn()), "jingcai_date": D, "backup": rep["backup"],
                                                       "ops": undo})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    jdump(ist_p, ist)
    health("ingest", jingcai_date=D, written=rep["written"], counts=rep["counts"], backup=rep["backup"],
           sign=rep["sign_check"]["status"] if rep["sign_check"] else None)
    return rep



def _matches_has_col(conn, col: str) -> bool:
    return any(r[1] == col for r in conn.execute("PRAGMA table_info(matches)"))


def ensure_kickoff_rev_column(conn) -> bool:
    """v2d3 研究副本：若 matches 尚无 kickoff_rev 列则加上（INTEGER DEFAULT 0）。现网不碰。"""
    if _matches_has_col(conn, "kickoff_rev"):
        return False
    conn.execute("ALTER TABLE matches ADD COLUMN kickoff_rev INTEGER DEFAULT 0")
    return True


def sync_match_kickoff_rev(conn, match_id: int, pm: dict | None, undo: list) -> None:
    """把计划里的 kickoff_rev 写进 matches.kickoff_rev（有列）+ match_meta.extras_json（无列也写）。
    后端 0.3.20 读 matches.kickoff_rev；extras 作对照，不读 state。"""
    if not pm:
        return
    rev = int(pm.get("kickoff_rev") or 0)
    if _matches_has_col(conn, "kickoff_rev"):
        old = conn.execute("SELECT kickoff_rev FROM matches WHERE id=?", (match_id,)).fetchone()
        prev = None if old is None else old[0]
        if prev != rev:
            undo.append({"op": "update_match_kickoff_rev", "match_id": match_id, "old": prev, "new": rev})
            conn.execute("UPDATE matches SET kickoff_rev=? WHERE id=?", (rev, match_id))
    # match_meta.extras 始终合并（无列时的落点；有列时作对照）
    row = conn.execute("SELECT extras_json FROM match_meta WHERE match_id=?", (match_id,)).fetchone()
    ex = {}
    if row and row[0]:
        try:
            ex = json.loads(row[0]) or {}
        except json.JSONDecodeError:
            ex = {}
    patch = {"kickoff_rev": rev,
             "kickoff_original": pm.get("kickoff_original"),
             "kickoff_actual": pm.get("kickoff_actual"),
             "kickoff_known_by": pm.get("kickoff_known_by"),
             "postpone_ts_unknown": bool(pm.get("postpone_ts_unknown")),
             "kickoff_placeholder_suspect": bool(pm.get("kickoff_placeholder_suspect")),
             "placeholder_ever": bool(pm.get("placeholder_ever")),
             "kickoff_confirmed": bool(pm.get("kickoff_confirmed")),
             "kickoff_confirmed_at": pm.get("kickoff_confirmed_at")}
    if all(ex.get(k) == v for k, v in patch.items()):
        return
    undo.append({"op": "update_match_meta_kickoff", "match_id": match_id,
                 "old_keys": {k: ex.get(k) for k in patch}, "new": patch})
    ex.update(patch)
    payload = json.dumps(ex, ensure_ascii=False, sort_keys=True)
    if row is None:
        conn.execute("INSERT INTO match_meta (match_id, source_file, extras_json) VALUES (?,?,?)",
                     (match_id, "live_capture", payload))
    else:
        conn.execute("UPDATE match_meta SET extras_json=? WHERE match_id=?", (payload, match_id))


def create_stub_match(conn, db: Path, D: str, r: dict, rep: dict):
    """（默认关）副本里按 5DF CSL 补建一条竞彩场次（match_uid=竞彩日|编号，队名=5DF 中文名，开赛=5DF 时间），
    match_meta.extras_json.source=5df_live_capture_stub；后端以后按 match_uid upsert 会覆盖成正式数据。先备份。"""
    if not rep.get("backup"):
        rep["backup"] = str(backup_replica(db))
    plan = load_plan(D) or {"matches": []}
    m = next((x for x in plan["matches"] if x["match_uid"] == r["match_uid"]), None)
    if not m:
        return None
    k = parse(m["kickoff_at"])
    code = m["jc_code"]
    conn.execute(
        "INSERT INTO matches (match_uid, scope, jingcai_date, weekday, kickoff_hour, jc_id, jc_no, competition_name,"
        " home_team, away_team, kickoff_at, kickoff_minute_known) VALUES (?,?,?,?,?,?,?,?,?,?,?,1)",
        (m["match_uid"], "jingcai", D, code[:1], k.hour, code, int(code[1:]) if code[1:].isdigit() else None,
         m["league"], m["home"], m["away"], iso(k)))
    mid = conn.execute("SELECT id FROM matches WHERE match_uid=?", (m["match_uid"],)).fetchone()[0]
    conn.execute("INSERT INTO match_meta (match_id, source_file, extras_json) VALUES (?,?,?)",
                 (mid, "live_capture", json.dumps({"source": "5df_live_capture_stub", "fixture_id": m["fixture_id"],
                                                   "kickoff_source": "5df", "jc_number": m["jc_number"],
                                                   "kickoff_rev": int(m.get("kickoff_rev") or 0)},
                                                  ensure_ascii=False)))
    if ensure_kickoff_rev_column(conn) or _matches_has_col(conn, "kickoff_rev"):
        conn.execute("UPDATE matches SET kickoff_rev=? WHERE id=?", (int(m.get("kickoff_rev") or 0), mid))
    conn.commit()
    append_jsonl(D_LOG / "ingest_undo.jsonl", {"ts": iso(now_cn()), "op": "create_stub_match",
                                               "match_uid": m["match_uid"], "match_id": mid, "backup": rep["backup"]})
    rep.setdefault("stub_matches_created", []).append(m["match_uid"])
    return conn.execute("SELECT * FROM matches WHERE id=?", (mid,)).fetchone()


def rep_prev_written(D: str, capture_id: str) -> bool:
    prev = jload(D_STATE / f"ingest_{D}.json", {}).get(capture_id, {})
    return prev.get("status") == "ingested"


# ----------------------------------------------------------------------------- summary

def summary(D: str) -> dict:
    plan = load_plan(D) or {"targets": [], "matches": [], "missing_mapping": [], "jc_number_gaps": []}
    st = jload(state_path(D), {})
    ist = jload(D_STATE / f"ingest_{D}.json", {})
    rows = staged_rows(D)
    by_target: dict[str, dict] = {}
    for t in plan["targets"]:
        k = f"{t['phase']}|{t['phase_variant']}"
        b = by_target.setdefault(k, {"targets": 0, "captured": 0, "missed": 0, "pending": 0, "no_odds": 0, "error": 0})
        b["targets"] += 1
        s = (st.get(t["target_key"]) or {}).get("status")
        if s in ("captured", "captured_late"):
            b["captured"] += 1
        elif s == "missed":
            b["missed"] += 1
        elif s is None:
            b["pending"] += 1
        elif s == "no_odds":
            b["no_odds"] += 1
        else:
            b["error"] += 1
    books: dict[str, dict] = {}
    for r in rows:
        if r["market"] != "asian":
            continue
        k = f"{r['phase']}|{r['phase_variant']}"
        books.setdefault(k, {}).setdefault(r["book"], 0)
        books[k][r["book"]] += 1
    ist_counts: dict[str, int] = {}
    for v in ist.values():
        ist_counts[v["status"]] = ist_counts.get(v["status"], 0) + 1
    lags = [r["fetch_lag_min"] for r in rows if r["market"] == "asian"]
    calls = 0
    lp = D_LOG / "call_log.tsv"
    if lp.exists():
        day = datetime.strptime(D, "%Y-%m-%d").date()
        for line in lp.read_text(encoding="utf-8").splitlines()[1:]:
            ts = line.split("\t", 1)[0]
            try:
                if parse(ts).date() in (day, day + timedelta(days=1)):
                    calls += 1
            except Exception:
                pass
    out = {"jingcai_date": D, "generated_at": iso(now_cn()), "n_matches": len(plan["matches"]),
           "n_exception": sum(1 for m in plan["matches"] if m.get("exception")),
           "targets_by_phase": by_target, "asian_rows_by_phase_book": books,
           "core_book_coverage": {k: {b: v.get(b, 0) for b in CORE_BOOKS} for k, v in books.items()},
           "ingest_status": ist_counts, "missing_mapping": plan["missing_mapping"],
           "jc_number_gaps": plan["jc_number_gaps"],
           "fetch_lag_min": {"min": min(lags), "max": max(lags)} if lags else None,
           "api_calls_logged_D_and_D+1": calls}
    jdump(D_SUM / f"{D}.json", out)
    if rows:  # staging CSV（由 JSONL 去重后导出；qa_flags 用 ; 连接）
        cols = list(rows[0].keys())
        with (D_STAGE / f"{D}.csv").open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in sorted(rows, key=lambda x: (x["target_at"], x["match_uid"], x["book"], x["market"])):
                w.writerow({**r, "qa_flags": ";".join(r["qa_flags"])})
    md = [f"# 实时采集日汇总 · 竞彩日 {D}", "", f"生成：{out['generated_at']}（UTC+8）", "",
          f"- 场次 {out['n_matches']}（例外场 {out['n_exception']}）；映射缺失 {len(out['missing_mapping'])}；编号断号 {out['jc_number_gaps']}",
          f"- fetched_at − target_at（分钟）：{out['fetch_lag_min']}", f"- 入副本状态：{ist_counts}", "",
          "| 阶段 | 目标 | 已抓 | 未到 | 错过 | 无赔率 | 错误 | 澳门 | 皇冠 | 威廉 | 平博 |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for k, b in sorted(by_target.items()):
        cb = out["core_book_coverage"].get(k, {})
        md.append(f"| {k} | {b['targets']} | {b['captured']} | {b['pending']} | {b['missed']} | {b['no_odds']} | {b['error']} | "
                  + " | ".join(str(cb.get(x, 0)) for x in CORE_BOOKS) + " |")
    (D_SUM / f"{D}.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    return out


# ----------------------------------------------------------------------------- tick / daemon

def tick(cl: Client) -> dict:
    now = now_cn()
    today = now.date()
    days = [(today + timedelta(days=o)).isoformat() for o in (-1, 0, 1)]
    plans = []
    changed_days = []
    for o, D in zip((-1, 0, 1), days):
        force = o == 0 and now.hour == 10 and 50 <= now.minute < 52  # 11:10 前强制刷新一次
        if o >= 0:
            p = ensure_plan(cl, D, PLAN_REFRESH.get(o), force=force)
        else:
            p = load_plan(D)
            # 前一竞彩日还有没确认的 12:00 占位场（开赛前后 3 小时内）→ 继续每小时刷新，等开赛确认
            if p and any(m.get("kickoff_placeholder_suspect") and now <= parse(m["kickoff_at"]) + timedelta(hours=3)
                         for m in p["matches"]):
                p = ensure_plan(cl, D, timedelta(minutes=60))
        if p:
            plans.append(p)
            if p.get("changed_this_build") and p.get("built_at") and now - parse(p["built_at"]) < timedelta(seconds=90) \
                    and (D_STAGE / f"{D}.jsonl").exists():
                changed_days.append(D)
    due, missed = due_targets(plans, now)
    if missed:
        mark_missed(missed)
    groups: dict[tuple, list] = {}
    for t in due:
        groups.setdefault((t["_D"], t["fixture_id"]), []).append(t)
    res = []
    for (D, fid), ts in sorted(groups.items(), key=lambda kv: min(t["target_at"] for t in kv[1])):
        plan = next(p for p in plans if p["jingcai_date"] == D)
        try:
            res.append(capture_group(cl, D, fid, ts, plan))
        except Exception as e:
            health("capture_error", fixture_id=fid, error=str(e)[:300], tb=traceback.format_exc()[-800:])
    touched = sorted({D for (D, _) in groups} | {t["match_uid"].split("|")[0] for t in missed} | set(changed_days))
    for D in touched:
        try:
            ingest(D)
        except SystemExit as e:
            health("ingest_refused", jingcai_date=D, error=str(e))
        except Exception as e:
            health("ingest_error", jingcai_date=D, error=str(e)[:300], tb=traceback.format_exc()[-800:])
        summary(D)
    hb = {"ts": iso(now_cn()), "pid": os.getpid(), "due": len(due), "groups": len(groups),
          "missed_new": len(missed), "calls_total": cl.calls, "remaining": cl.remaining,
          "plans": {p["jingcai_date"]: p["n_targets"] for p in plans}}
    jdump(D_LOG / "heartbeat.json", hb)
    if groups or missed or now.minute % 30 == 0:
        health("tick", **{k: v for k, v in hb.items() if k != "ts"})
    return hb


def daemon() -> None:
    D_LOG.mkdir(parents=True, exist_ok=True)
    lock = open(D_LOG / "daemon.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("daemon already running")
        return
    (D_LOG / "daemon.pid").write_text(str(os.getpid()))
    cl = Client()
    stop = {"v": False}
    signal.signal(signal.SIGTERM, lambda *a: stop.__setitem__("v", True))
    health("daemon_start", pid=os.getpid(), loop_sec=LOOP_SEC)
    while not stop["v"]:
        t0 = time.time()
        try:
            tick(cl)
        except Exception as e:
            health("tick_error", error=str(e)[:300], tb=traceback.format_exc()[-800:])
        # 每天 12:05 给前一竞彩日出最终汇总
        n = now_cn()
        if n.hour == 12 and n.minute == 5:
            try:
                summary((n.date() - timedelta(days=1)).isoformat())
            except Exception:
                pass
        time.sleep(max(1.0, LOOP_SEC - (time.time() - t0)))
    health("daemon_stop", pid=os.getpid())


def ensure_daemon() -> str:
    D_LOG.mkdir(parents=True, exist_ok=True)
    lock = open(D_LOG / "daemon.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
    except BlockingIOError:
        return "running pid=" + (D_LOG / "daemon.pid").read_text().strip()
    py = _rp_os.environ.get("LIVE_CAPTURE_PYTHON", "").strip() or sys.executable
    out = open(D_LOG / "daemon.out", "a")
    p = subprocess.Popen([py, str(Path(__file__).resolve()), "daemon"], stdout=out, stderr=out,
                         stdin=subprocess.DEVNULL, start_new_session=True, cwd=str(_REPO_ROOT))
    return f"started pid={p.pid}"


# ----------------------------------------------------------------------------- check-config (public repo)

def check_config() -> dict:
    """不调任何 API、不写任何文件：检查 key 是否已配置（只报 set/missing，不打印值）、库路径、双写开关。"""
    _kv = os.environ.get("FIVEDOLLAR_FOOTBALL_API_KEY", "").strip()
    key_set = bool(_kv) and not _kv.startswith("YOUR_")
    dual = os.environ.get("DUAL_WRITE_ODDS_ASIAN", "0").strip().lower() in ("1", "true", "yes", "on")
    rep = {
        "mode": "check-config (dry-run: no API call, no write)",
        "env_file": {"path": str(_REPO_ROOT / ".env"), "exists": (_REPO_ROOT / ".env").exists()},
        "FIVEDOLLAR_FOOTBALL_API_KEY": "set" if key_set else "missing",
        "DUAL_WRITE_ODDS_ASIAN": dual,
        "api_root": str(API_ROOT),
        "api_import_ok": True,
        "replica_db_V2D3_DB_PATH": {"path": str(REPLICA_DB), "exists": REPLICA_DB.exists()},
        "prod_db_APP_DB_PATH": {"path": str(PROD_DB), "exists": PROD_DB.exists()},
        "live_dir": str(LIVE),
        "base_url": BASE,
    }
    hints = []
    if not key_set:
        hints.append("缺少 FIVEDOLLAR_FOOTBALL_API_KEY：复制 config.example.env 为 .env 并填写自己的 5DF key（见 docs/SENSITIVE.md）")
    if dual:
        hints.append("DUAL_WRITE_ODDS_ASIAN 已打开：采集入库会拒绝运行；公开版默认应为 0")
    if not REPLICA_DB.exists():
        hints.append("V2D3_DB_PATH 指向的副本库不存在：采集可抓取落盘，但 ingest 会跳过；可先把自己的备份 .db 复制一份作为副本")
    if REPLICA_DB.exists() and PROD_DB.exists() and REPLICA_DB.resolve() == PROD_DB:
        hints.append("V2D3_DB_PATH 与 APP_DB_PATH 相同：采集拒绝写现网库，请给副本单独一份文件")
    rep["ready_for_capture"] = key_set and not dual
    rep["hints"] = hints
    return rep


# ----------------------------------------------------------------------------- cli

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for n in ("plan", "status", "summary", "ingest"):
        s = sub.add_parser(n)
        s.add_argument("--date", default=now_cn().date().isoformat())
        if n == "ingest":
            s.add_argument("--dry-run", action="store_true")
            s.add_argument("--db", default=str(REPLICA_DB))
            s.add_argument("--create-missing-matches", action="store_true",
                           help="（默认关）副本缺场次时按 5DF CSL 补建 stub 场次行；需用户批准后再用")
    sub.add_parser("tick")
    sub.add_parser("daemon")
    sub.add_parser("ensure-daemon")
    sub.add_parser("check-config", help="不调 API 的配置自检（dry-run）：key/库路径/双写开关")
    c = sub.add_parser("capture")
    c.add_argument("--target-key")
    c.add_argument("--nearest", action="store_true")
    c.add_argument("--allow-early-min", type=float, default=0.0)
    c.add_argument("--allow-late", action="store_true")
    c.add_argument("--no-ingest", action="store_true")
    ab = sub.add_parser("asof-backfill",
                        help="漏采后 hist as-of≤T 补写 mid|rule/close|rule（禁止 T 后即时盘）")
    ab.add_argument("--date", required=True)
    ab.add_argument("--target-key", action="append", default=[])
    ab.add_argument("--missed-mid-rule", action="store_true")
    ab.add_argument("--dry-run", action="store_true")
    ab.add_argument("--refresh-hist", action="store_true")
    ab.add_argument("--db", default=str(REPLICA_DB))
    gs = sub.add_parser("gap-scan", help="多日应采未采扫描 → rescue_queue（默认回溯 7 天）")
    gs.add_argument("--days", type=int, default=7)
    gs.add_argument("--from", dest="d_from", default=None)
    gs.add_argument("--to", dest="d_to", default=None)
    gs.add_argument("--include-real", action="store_true")
    gs.add_argument("--dry-run", action="store_true")
    gs.add_argument("--db", default=str(REPLICA_DB))
    rc = sub.add_parser("rescue-consume", help="空闲窗消费 rescue_queue（asof / shadow；不堵 live due）")
    rc.add_argument("--max", type=int, default=8)
    rc.add_argument("--daily-cap", type=int, default=40)
    rc.add_argument("--dry-run", action="store_true")
    rc.add_argument("--db", default=str(REPLICA_DB))
    rc.add_argument("--ignore-due-avoid", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "check-config":
        rep = check_config()
        print(json.dumps(rep, ensure_ascii=False, indent=1))
        return 0 if rep["ready_for_capture"] else 2
    if a.cmd == "plan":
        p = ensure_plan(Client(), a.date, None, force=True)
        if not p:
            raise SystemExit("plan failed; see logs/health.jsonl")
        p_cc = cross_check_dispatch(p) if p else None
        print(json.dumps({"n_matches": p["n_matches"], "n_targets": p["n_targets"],
                          "missing_mapping": p["missing_mapping"], "jc_number_gaps": p["jc_number_gaps"],
                          "dispatch_cross_check": p_cc}, ensure_ascii=False, indent=1))
    elif a.cmd == "status":
        p = load_plan(a.date)
        st = jload(state_path(a.date), {})
        for t in (p or {}).get("targets", []):
            print(t["target_at"][5:16], f"{t['phase']:5s} {t['phase_variant']:9s}", t["match_uid"], t["fixture_id"],
                  (st.get(t["target_key"]) or {}).get("status", "-"))
    elif a.cmd == "summary":
        print(json.dumps(summary(a.date), ensure_ascii=False, indent=1))
    elif a.cmd == "ingest":
        print(json.dumps(ingest(a.date, Path(a.db), a.dry_run, a.create_missing_matches or None),
                         ensure_ascii=False, indent=1))
    elif a.cmd == "tick":
        print(json.dumps(tick(Client()), ensure_ascii=False))
    elif a.cmd == "daemon":
        daemon()
    elif a.cmd == "ensure-daemon":
        print(ensure_daemon())
    elif a.cmd == "capture":
        now = now_cn()
        today = now.date()
        plans = [p for p in (load_plan((today + timedelta(days=o)).isoformat()) for o in (-1, 0, 1)) if p]
        cands = [{**t, "_D": p["jingcai_date"]} for p in plans for t in p["targets"]
                 if t["target_key"] not in jload(state_path(p["jingcai_date"]), {}) and parse(t["kickoff_at"]) > now]
        if a.target_key:
            cands = [t for t in cands if t["target_key"] == a.target_key]
        else:
            cands.sort(key=lambda t: abs((parse(t["target_at"]) - now).total_seconds()))
            cands = cands[:1]
        if not cands:
            raise SystemExit("no candidate target")
        t = cands[0]
        at = parse(t["target_at"])
        late = now > at + WIN_AFTER
        early = now < at - WIN_BEFORE
        if late and not a.allow_late:
            raise SystemExit(f"{t['target_key']} past window; use --allow-late (staged as late, not ingested)")
        if early and (at - now).total_seconds() / 60 > max(a.allow_early_min, 2):
            raise SystemExit(f"{t['target_key']} not due until {t['target_at']}; window opens at T-2min")
        plan = next(p for p in plans if p["jingcai_date"] == t["_D"])
        r = capture_group(Client(), t["_D"], t["fixture_id"], [t], plan, late=late)
        print(json.dumps({"target": t["target_key"], "target_at": t["target_at"], **r}, ensure_ascii=False))
        if not a.no_ingest:
            print(json.dumps(ingest(t["_D"]), ensure_ascii=False, indent=1))
            summary(t["_D"])
    elif a.cmd == "asof-backfill":
        # 委托独立脚本，避免把 hist 逻辑塞进 tick 热路径；daemon 无需重启即可用新入口。
        from asof_backfill_mid_rule import run as asof_run  # noqa: WPS433
        if not a.target_key and not a.missed_mid_rule:
            raise SystemExit("asof-backfill 需要 --target-key 或 --missed-mid-rule")
        rep = asof_run(a.date, a.target_key or None, a.missed_mid_rule, Path(a.db),
                       a.dry_run, a.refresh_hist)
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0 if rep.get("targets") else 1
    elif a.cmd == "gap-scan":
        from rescue_gap_scan import main as gap_main  # noqa: WPS433
        argv2 = ["--days", str(a.days), "--db", a.db]
        if a.d_from:
            argv2 += ["--from", a.d_from]
        if a.d_to:
            argv2 += ["--to", a.d_to]
        if a.include_real:
            argv2.append("--include-real")
        if a.dry_run:
            argv2.append("--dry-run")
        return gap_main(argv2)
    elif a.cmd == "rescue-consume":
        from rescue_worker import main as worker_main  # noqa: WPS433
        argv2 = ["--max", str(a.max), "--daily-cap", str(a.daily_cap), "--db", a.db]
        if a.dry_run:
            argv2.append("--dry-run")
        if a.ignore_due_avoid:
            argv2.append("--ignore-due-avoid")
        return worker_main(argv2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
