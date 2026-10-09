#!/usr/bin/env python3
"""Map unmapped sporttery rows → 5DF fixtures via team alias + kickoff window.

Does NOT touch csl_fixture_map.json (CSL number map). Writes shadow map:
  csl_fixture_map_alias90.json

Rules (alias90-v1):
  - Only jingcai_date >= 2025-10-25 (pre-cutoff permanent skip for CSL number era).
  - Candidate fixtures: league fixtures whose kickoff is within
      [jingcai_date 12:00 BJ − 90min, jingcai_date+1 12:00 BJ + 90min]
    (90min = identity threshold vs 竞彩日窗边界; sporttery results lack matchTime).
  - Team match: alias-normalized home/away equal (or swapped → flagged).
  - 1:1 → shadow map item; 0 → no_hit; >1 → conflict (never write).
  - Dual-write stays OFF. Rate ≤30/min; stop if Remaining ≤5.
"""
from __future__ import annotations
# --- public repo: paths are env-overridable (see config.example.env) ---
import os as _rp_os
from pathlib import Path as _RpPath
_REPO_ROOT = _RpPath(__file__).resolve().parents[3]
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
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 0.3.19：共享数据接口补数让路（北京 11:05–11:20 / 14:55–15:15 / 21:55–22:15 不发请求；补数合计 ≤16 次/分钟；
# 剩余 ≤24 停到 Reset）。共用判断：api/app/shared_api_yield.py
import sys as _yield_sys  # noqa: E402
_yield_sys.path.append(str(_MA_API_ROOT / "app"))  # 追加在末尾，不遮蔽其它模块
import shared_api_yield as _yield_mod  # noqa: E402
_YIELD = _yield_mod.Gate("map_alias90_fixtures")


def _gated_urlopen(req, timeout=60):
    _YIELD.before_request()
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        _YIELD.after_response(e.headers)
        raise
    _YIELD.after_response(r.headers)
    return r



ROOT = Path(__file__).resolve().parent
QUEUE = ROOT / "queue"
REPORTS = ROOT / "reports"
RAW_FX = ROOT / "raw" / "fixtures_league"
LOG_DIR = ROOT / "logs"
SHADOW_MAP = ROOT / "csl_fixture_map_alias90.json"
TARGETS = REPORTS / "unmapped_post_cutoff_520_alias90_targets.json"
COMP_ALIAS = Path(
    str(_ODDS_DATA_DIR / "research/jingcai-competition-whitelist/competition_alias_seed.json")
)
SPORTTERY_CACHE = Path(
    str(_ODDS_DATA_DIR / "research/jingcai-competition-whitelist/cache/sporttery")
)
APP_DB = Path(str(_APP_DB))
DONE_MAP = ROOT / "csl_fixture_map.json"
MACAU_MAP = Path(str(_ODDS_DATA_DIR / "backfill/macau_mid_water_fixture_map.json"))

BASE = "https://api.5dollarfootballapi.com/v1"
GAP_SEC = 2.1
RESERVE = 5
CSL_NUMBER_FROM = "2025-10-25"
TZ = timezone(timedelta(hours=8))
RULE_VERSION = "alias90-v1"
WINDOW_PAD_MIN = 90  # ≤90 min identity vs jingcai-day edges


def now_iso() -> str:
    return datetime.now(TZ).isoformat()


def normalize_jc(number: str | None) -> str | None:
    if not number:
        return None
    s = str(number).strip()
    full = {"一": "周一", "二": "周二", "三": "周三", "四": "周四", "五": "周五", "六": "周六", "日": "周日"}
    short = {v: k for k, v in full.items()}
    for f, sh in short.items():
        if s.startswith(f):
            return sh + s[len(f) :]
    return s


def jc_full(short: str | None) -> str | None:
    if not short:
        return None
    full = {"一": "周一", "二": "周二", "三": "周三", "四": "周四", "五": "周五", "六": "周六", "日": "周日"}
    ch = short[0]
    return (full.get(ch, ch) + short[1:]) if ch in full else short


def norm_team(s: str | None) -> str:
    if not s:
        return ""
    t = str(s).strip()
    # strip common noise
    for a, b in [
        ("\u3000", ""),
        (" ", ""),
        ("·", ""),
        ("・", ""),
        ("-", ""),
        (".", ""),
        ("'", ""),
        ("’", ""),
        ("（", ""),
        ("）", ""),
        ("(", ""),
        (")", ""),
        ("FC", ""),
        ("fc", ""),
        ("AFC", ""),
        ("CF", ""),
        ("队", ""),
    ]:
        t = t.replace(a, b)
    return t


class RateClient:
    def __init__(self, key: str, gap: float = GAP_SEC, reserve: int = RESERVE):
        self.key = key
        self.gap = gap
        self.reserve = reserve
        self.last = 0.0
        self.calls = 0
        self.remaining: int | None = None
        self.stopped_quota = False
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.log_path = LOG_DIR / "alias90_call_log.tsv"
        if not self.log_path.exists():
            self.log_path.write_text(
                "ts\tname\tpath\thttp\telapsed_s\tlimit\tremaining\treset\n",
                encoding="utf-8",
            )

    def get(self, name: str, path: str, params: dict | None = None):
        wait = self.gap - (time.time() - self.last)
        if wait > 0:
            time.sleep(wait)
        url = BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.key}",
                "Accept": "application/json",
            },
        )
        t0 = time.time()
        try:
            r = _gated_urlopen(req, timeout=60)
            code, body, headers = r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            code, body, headers = e.code, e.read(), e.headers
        self.last = time.time()
        self.calls += 1
        rem = headers.get("X-RateLimit-Remaining")
        self.remaining = int(rem) if rem is not None else self.remaining
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(
                f"{datetime.now(TZ).strftime('%H:%M:%S')}\t{name}\t{path}\t{code}\t"
                f"{time.time() - t0:.2f}\t{headers.get('X-RateLimit-Limit')}\t{rem}\t"
                f"{headers.get('X-RateLimit-Reset')}\n"
            )
        try:
            data = json.loads(body)
        except Exception:
            data = {"_raw": body[:500].decode("utf-8", "ignore")}
        if self.remaining is not None and self.remaining <= self.reserve:
            self.stopped_quota = True
        return code, data, headers


def load_team_alias_canonical() -> dict[str, str]:
    """alias_norm → canonical_norm (many→one)."""
    out: dict[str, str] = {}

    def add(alias: str, canon: str):
        an, cn = norm_team(alias), norm_team(canon)
        if not an or not cn:
            return
        out[an] = cn
        out.setdefault(cn, cn)

    # app.db
    if APP_DB.exists():
        c = sqlite3.connect(APP_DB)
        for alias, canon in c.execute(
            "SELECT a.alias, t.name_zh_canonical FROM team_aliases a JOIN teams t ON t.id=a.team_id"
        ):
            add(alias, canon)
        for (name,) in c.execute("SELECT name_zh_canonical FROM teams"):
            add(name, name)

    # sporttery short ↔ full
    if SPORTTERY_CACHE.is_dir():
        for p in SPORTTERY_CACHE.glob("*.json"):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            for mr in (d.get("value") or {}).get("matchResult") or []:
                for a, b in (
                    (mr.get("homeTeam"), mr.get("allHomeTeam")),
                    (mr.get("awayTeam"), mr.get("allAwayTeam")),
                ):
                    if a and b:
                        # prefer longer / all* as canonical
                        canon = b if len(str(b)) >= len(str(a)) else a
                        add(a, canon)
                        add(b, canon)

    # manual merges from alias-merge-canonical
    for canon, aliases in (
        ("乌兹别克斯坦", ["乌茲別克斯坦", "乌茲别克斯坦"]),
        ("奥地利", ["奧地利"]),
        ("塞纳乔其", ["塞那乔其"]),
    ):
        add(canon, canon)
        for a in aliases:
            add(a, canon)

    # common sporttery ↔ 5DF zh gaps (seed; expand via QA misses)
    for canon, aliases in (
        ("格雷特霍夫", ["菲尔特", "格罗伊特菲尔特", "格雷特霍夫"]),
        ("沙尔克", ["沙尔克04", "沙尔克04队"]),
        ("伊蒂哈德", ["吉达联合", "吉达伊蒂哈德"]),
        ("利雅得希拉尔", ["利雅得新月", "希拉尔", "利雅新月"]),
        ("利雅得青年", ["利雅青年", "青年人"]),
        ("达马克FC", ["达马克"]),
        ("墨尔本城", ["墨尔本城FC"]),
        ("珀斯光荣", ["珀斯"]),
        ("墨尔本胜利", ["墨尔本胜利队"]),
        ("AC米兰", ["米兰", "AC米兰队", "AC 米兰队"]),
        ("国际米兰", ["国米", "国际米"]),
        ("马德里竞技", ["马竞", "马德里竞技队"]),
        ("皇家马德里", ["皇马"]),
        ("巴塞罗那", ["巴萨"]),
        ("拜仁慕尼黑", ["拜仁"]),
        ("多特蒙德", ["多特"]),
        ("曼彻斯特联", ["曼联"]),
        ("曼彻斯特城", ["曼城"]),
        ("托特纳姆热刺", ["热刺"]),
        ("狼队", ["伍尔弗汉普顿"]),
        ("红星FC93", ["圣旺红星", "红星", "红星FC 93", "红星93"]),
        ("NAC布雷达", ["布雷达", "NAC", "NAC布雷达"]),
        ("希蒙体育", ["海尔蒙特", "赫尔蒙德", "Helmond"]),
        ("多德雷赫特", ["多德勒支", "多德雷赫", "FC多德雷赫", "FC 多德雷赫"]),
        ("RKC华域克", ["瓦尔韦克", "华域克", "RKC"]),
        ("坎布尔", ["金堡尔", "坎布尔"]),
        ("云达不来梅", ["不来梅", "云达不莱梅"]),
        ("柏林联合", ["联合柏林", "柏林联"]),
        ("阿尔维卡", ["阿罗卡", "FC阿罗卡"]),
        ("吉维森特", ["吉维森特", "吉尔维森特", "Gil Vicente"]),
        ("柏太阳神", ["柏市", "柏雷素尔", "柏雷索尔"]),
        ("柏雷索尔", ["柏太阳神", "柏雷素尔"]),
        ("横滨FC", ["横滨FC", "横浜FC", "FC横滨", "FC横浜"]),
        ("FC横滨", ["横滨FC", "横浜FC"]),
        ("蔚山现代", ["蔚山", "蔚山HD"]),
        ("大邱FC", ["大邱"]),
        ("国际迈阿密", ["迈阿密国际", "迈阿密"]),
        ("纳什维尔SC", ["纳什维尔"]),
        ("利兹", ["利兹联"]),
        ("西汉姆", ["西汉姆联"]),
        ("巴黎FC", ["巴黎 FC", "巴黎F C"]),
    ):
        add(canon, canon)
        for a in aliases:
            add(a, canon)

    return out


def char_sim(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b or a in b or b in a:
        return 1.0
    sa, sb = set(a), set(b)
    return len(sa & sb) / len(sa | sb)


def team_pair_score(
    uh: str, ua: str, fh: str, fa: str, alias_map: dict[str, str]
) -> tuple[float, bool]:
    """Best of (normal / swapped) char-sim after canon. Returns (score, swapped)."""
    ch, ca = canon_team(uh, alias_map), canon_team(ua, alias_map)
    xh, xa = canon_team(fh, alias_map), canon_team(fa, alias_map)
    if not ch or not ca or not xh or not xa:
        return 0.0, False
    s1 = min(char_sim(ch, xh), char_sim(ca, xa))
    s2 = min(char_sim(ch, xa), char_sim(ca, xh))
    if s2 > s1:
        return s2, True
    return s1, False


def canon_team(name: str | None, alias_map: dict[str, str]) -> str:
    n = norm_team(name)
    if not n:
        return ""
    # follow once
    return alias_map.get(n, n)


def load_league_fd_ids() -> dict[str, list[int]]:
    doc = json.loads(COMP_ALIAS.read_text(encoding="utf-8"))
    by: dict[str, list[int]] = {}
    for e in doc.get("entities") or []:
        ids = [int(x) for x in (e.get("fd_league_ids") or [])]
        names = {e.get("name_zh_canonical") or ""}
        for a in e.get("aliases") or []:
            if a.get("alias"):
                names.add(a["alias"])
        for n in names:
            if n and ids:
                by[n] = ids
    return by


def resolve_league_ids(league_name: str, by: dict[str, list[int]]) -> list[int]:
    if league_name in by:
        return by[league_name]
    for k, ids in by.items():
        if k and league_name and (k in league_name or league_name in k):
            return ids
    return []


def jingcai_window(jd: str) -> tuple[datetime, datetime]:
    """竞彩日关联窗：覆盖当日早场(00:00)至次日中午，边界 ±WINDOW_PAD_MIN。

    sporttery 历史结果无 matchTime；00:30 BJ 的欧陆周五夜赛常挂在次日竞彩日。
    标准 CSL 日窗是 [jd 12:00, jd+1 12:00)，这里放宽到 [jd 00:00, jd+1 12:00) 再 ±90min。
    """
    day0 = datetime.strptime(jd, "%Y-%m-%d").replace(
        hour=0, minute=0, second=0, microsecond=0, tzinfo=TZ
    )
    noon_next = day0 + timedelta(days=1, hours=12)
    start = day0 - timedelta(minutes=WINDOW_PAD_MIN)
    end = noon_next + timedelta(minutes=WINDOW_PAD_MIN)
    return start, end


def search_window(jd: str) -> tuple[datetime, datetime]:
    """Fetch window: padded day ±12h."""
    lo, hi = jingcai_window(jd)
    return lo - timedelta(hours=12), hi + timedelta(hours=12)


def parse_kickoff(fx: dict) -> datetime | None:
    ko = fx.get("kickoff_utc") or fx.get("kickoff_at")
    if ko:
        try:
            dt = datetime.fromisoformat(str(ko).replace("Z", "+00:00"))
            return dt.astimezone(TZ)
        except Exception:
            pass
    ts = fx.get("kickoff_ts")
    if ts:
        return datetime.fromtimestamp(int(ts), tz=TZ)
    return None


def lean_fx(fx: dict) -> dict:
    teams = fx.get("teams") or {}
    lot = (fx.get("lottery") or {}).get("jingcailottery") or {}
    return {
        "fixture_id": str(fx.get("id")),
        "home_team": (teams.get("home") or {}).get("name"),
        "away_team": (teams.get("away") or {}).get("name"),
        "kickoff_utc": fx.get("kickoff_utc"),
        "kickoff_ts": fx.get("kickoff_ts"),
        "league_id": (fx.get("league") or {}).get("id"),
        "league_name": (fx.get("league") or {}).get("name"),
        "jc_number": lot.get("number"),
        "status": fx.get("status"),
    }


def cache_path(lid: int, st: int, en: int) -> Path:
    return RAW_FX / f"{lid}_{st}_{en}.json"


def fetch_league_fixtures(
    client: RateClient | None, lid: int, st: int, en: int, *, local_only: bool
) -> list[dict]:
    RAW_FX.mkdir(parents=True, exist_ok=True)
    path = cache_path(lid, st, en)
    if path.exists():
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            if doc.get("success") and isinstance(doc.get("data"), list):
                return [lean_fx(x) for x in doc["data"]]
        except Exception:
            pass
    if local_only or client is None:
        return []
    if client.stopped_quota:
        return []
    rows: list[dict] = []
    page = 1
    raw_pages: list[dict] = []
    while page <= 20:
        code, data, _ = client.get(
            f"lgfx_{lid}",
            f"/leagues/{lid}/fixtures",
            {
                "start_time": st,
                "end_time": en,
                "per_page": 100,
                "page": page,
                "lang": "zh-cn",
            },
        )
        if code != 200:
            break
        chunk = data.get("data") or []
        raw_pages.extend(chunk)
        rows.extend(lean_fx(x) for x in chunk)
        pag = data.get("pagination") or {}
        if not pag.get("has_more") or len(chunk) < 100:
            break
        page += 1
        if client.stopped_quota:
            break
    # write only if we got a clean response (even empty)
    path.write_text(
        json.dumps(
            {"success": 1, "data": raw_pages, "fetched_at": now_iso(), "league_id": lid, "start_time": st, "end_time": en},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return rows


def teams_match(
    uh: str, ua: str, fh: str, fa: str, alias_map: dict[str, str], *, min_sim: float = 0.55
) -> tuple[bool, bool, float]:
    """Return (matched, swapped, score). Exact/soft first; else char-sim ≥ min_sim."""
    ch, ca = canon_team(uh, alias_map), canon_team(ua, alias_map)
    xh, xa = canon_team(fh, alias_map), canon_team(fa, alias_map)
    if not ch or not ca or not xh or not xa:
        return False, False, 0.0
    if ch == xh and ca == xa:
        return True, False, 1.0
    if ch == xa and ca == xh:
        return True, True, 1.0

    def soft(a: str, b: str) -> bool:
        if a == b:
            return True
        if len(a) >= 2 and len(b) >= 2 and (a in b or b in a):
            return True
        return False

    if soft(ch, xh) and soft(ca, xa):
        return True, False, 0.95
    if soft(ch, xa) and soft(ca, xh):
        return True, True, 0.95
    score, swapped = team_pair_score(uh, ua, fh, fa, alias_map)
    if score >= min_sim:
        return True, swapped, score
    return False, False, score


def load_already_mapped_fids() -> set[str]:
    fids: set[str] = set()
    for path in (DONE_MAP, MACAU_MAP, SHADOW_MAP):
        if not path.exists():
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for it in doc.get("items") or []:
            if it.get("fixture_id"):
                fids.add(str(it["fixture_id"]))
    # done queue
    for line in (QUEUE / "done.jsonl").read_text(encoding="utf-8").splitlines() if (QUEUE / "done.jsonl").exists() else []:
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("fixture_id"):
            fids.add(str(r["fixture_id"]))
    return fids


def load_targets(limit: int | None = None) -> list[dict]:
    if TARGETS.exists():
        items = json.loads(TARGETS.read_text(encoding="utf-8")).get("items") or []
    else:
        items = []
        for line in (QUEUE / "unmapped.jsonl").read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if (r.get("jingcai_date") or "") >= CSL_NUMBER_FROM:
                items.append(r)
    items = sorted(items, key=lambda x: (x["jingcai_date"], x["jc_id"]))
    if limit:
        items = items[:limit]
    return items


def match_one(
    u: dict,
    fixtures: list[dict],
    alias_map: dict[str, str],
    used_fids: set[str],
) -> dict:
    jd = u["jingcai_date"]
    wlo, whi = jingcai_window(jd)
    cands = []
    for fx in fixtures:
        ko = parse_kickoff(fx)
        if not ko:
            continue
        if not (wlo <= ko < whi):
            continue
        ok, swapped, score = teams_match(
            u.get("home_team"),
            u.get("away_team"),
            fx.get("home_team"),
            fx.get("away_team"),
            alias_map,
        )
        if not ok and (u.get("home_team_short") or u.get("away_team_short")):
            ok2, sw2, sc2 = teams_match(
                u.get("home_team_short") or u.get("home_team"),
                u.get("away_team_short") or u.get("away_team"),
                fx.get("home_team"),
                fx.get("away_team"),
                alias_map,
            )
            if ok2 and sc2 > score:
                ok, swapped, score = ok2, sw2, sc2
        if not ok:
            continue
        # delta vs jingcai-day core [jd 00:00, jd+1 12:00); 0 if inside
        day0 = datetime.strptime(jd, "%Y-%m-%d").replace(
            hour=0, minute=0, second=0, microsecond=0, tzinfo=TZ
        )
        core_end = day0 + timedelta(days=1, hours=12)
        if day0 <= ko < core_end:
            edge_delta = 0.0
        elif ko < day0:
            edge_delta = (day0 - ko).total_seconds() / 60.0
        else:
            edge_delta = (ko - core_end).total_seconds() / 60.0
        cands.append(
            {
                "fx": fx,
                "swapped": swapped,
                "score": score,
                "kickoff_bj": ko.isoformat(),
                "edge_delta_min": round(edge_delta, 2),
            }
        )

    # de-dupe by fixture_id
    seen = set()
    uniq = []
    for c in cands:
        fid = str(c["fx"]["fixture_id"])
        if fid in seen:
            continue
        seen.add(fid)
        uniq.append(c)

    if len(uniq) == 0:
        return {"status": "no_hit", "match_uid": u["match_uid"], "candidates": []}
    if len(uniq) > 1:
        uniq.sort(key=lambda c: (-c.get("score", 0), c.get("edge_delta_min", 999)))
        # uniquely dominant high-confidence match
        if (
            uniq[0].get("score", 0) >= 0.9
            and uniq[0].get("score", 0) > uniq[1].get("score", 0) + 0.05
        ):
            uniq = [uniq[0]]
        else:
            return {
                "status": "conflict",
                "match_uid": u["match_uid"],
                "candidates": [
                    {
                        "fixture_id": c["fx"]["fixture_id"],
                        "home_team": c["fx"]["home_team"],
                        "away_team": c["fx"]["away_team"],
                        "kickoff_bj": c["kickoff_bj"],
                        "swapped": c["swapped"],
                        "score": c.get("score"),
                        "edge_delta_min": c["edge_delta_min"],
                    }
                    for c in uniq
                ],
            }

        c = uniq[0]
    fid = str(c["fx"]["fixture_id"])
    if fid in used_fids:
        return {
            "status": "conflict",
            "match_uid": u["match_uid"],
            "reason": "fixture_already_mapped_elsewhere",
            "candidates": [
                {
                    "fixture_id": fid,
                    "home_team": c["fx"]["home_team"],
                    "away_team": c["fx"]["away_team"],
                    "kickoff_bj": c["kickoff_bj"],
                    "swapped": c["swapped"],
                    "edge_delta_min": c["edge_delta_min"],
                }
            ],
        }
    return {
        "status": "one_to_one",
        "match_uid": u["match_uid"],
        "item": {
            "fixture_id": fid,
            "jingcai_date": jd,
            "jc_id": normalize_jc(u.get("jc_id")),
            "jc_norm": u.get("jc_norm") or jc_full(normalize_jc(u.get("jc_id"))),
            "home_team": u.get("home_team"),
            "away_team": u.get("away_team"),
            "fixture_home": c["fx"]["home_team"],
            "fixture_away": c["fx"]["away_team"],
            "kickoff_at": c["kickoff_bj"],
            "league_name": u.get("league_name") or c["fx"].get("league_name"),
            "match_uid": u["match_uid"],
            "source": "alias90_team_ko",
            "map_source": "alias90",
            "map_confidence": "med",
            "kickoff_delta_min": c["edge_delta_min"],
            "kickoff_delta_basis": "vs_jingcai_day_edge_sporttery_date_only",
            "swapped_home_away": c["swapped"],
            "team_match_score": c.get("score"),
            "disambiguated_at": now_iso(),
            "rule_version": RULE_VERSION,
            "sporttery_match_id": u.get("sporttery_match_id"),
            "fd_league_id": c["fx"].get("league_id"),
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="process only first N targets")
    ap.add_argument("--local-only", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="do not write shadow map")
    ap.add_argument("--skip-rebuild", action="store_true")
    args = ap.parse_args()

    key = os.environ.get("FIVEDOLLAR_FOOTBALL_API_KEY")
    if not args.local_only and not key:
        print("ERROR: FIVEDOLLAR_FOOTBALL_API_KEY missing", flush=True)
        return 2

    alias_map = load_team_alias_canonical()
    league_by = load_league_fd_ids()
    targets = load_targets(args.limit or None)
    used_fids = load_already_mapped_fids()
    client = None if args.local_only else RateClient(key)

    # group fetch keys: (lid, st, en) → fixtures
    fetch_cache: dict[tuple[int, int, int], list[dict]] = {}
    results = {"one_to_one": [], "conflict": [], "no_hit": [], "no_league": []}
    api_calls_start = 0

    for i, u in enumerate(targets):
        lids = resolve_league_ids(u.get("league_name") or "", league_by)
        if not lids:
            results["no_league"].append(u["match_uid"])
            continue
        slo, shi = search_window(u["jingcai_date"])
        st, en = int(slo.timestamp()), int(shi.timestamp())
        fixtures: list[dict] = []
        for lid in lids:
            key_t = (lid, st, en)
            if key_t not in fetch_cache:
                fetch_cache[key_t] = fetch_league_fixtures(
                    client, lid, st, en, local_only=args.local_only
                )
                if client and client.stopped_quota:
                    break
            fixtures.extend(fetch_cache[key_t])
        # de-dupe fixtures
        seen_f = set()
        uniq_fx = []
        for fx in fixtures:
            fid = str(fx["fixture_id"])
            if fid in seen_f:
                continue
            seen_f.add(fid)
            uniq_fx.append(fx)

        res = match_one(u, uniq_fx, alias_map, used_fids)
        st_name = res["status"]
        if st_name == "one_to_one":
            results["one_to_one"].append(res["item"])
            used_fids.add(str(res["item"]["fixture_id"]))
        elif st_name == "conflict":
            results["conflict"].append(res)
        else:
            results["no_hit"].append({"match_uid": u["match_uid"], "home_team": u.get("home_team"), "away_team": u.get("away_team"), "league_name": u.get("league_name"), "jingcai_date": u["jingcai_date"]})

        if client and client.stopped_quota:
            print(json.dumps({"stopped_quota": True, "processed": i + 1, "remaining": client.remaining}, ensure_ascii=False))
            break

        if (i + 1) % 25 == 0:
            print(
                f"progress {i+1}/{len(targets)} hit={len(results['one_to_one'])} "
                f"conflict={len(results['conflict'])} no_hit={len(results['no_hit'])} "
                f"api={client.calls if client else 0} rem={client.remaining if client else 'n/a'}",
                flush=True,
            )

    # fixture→match_uid one-to-many check before write
    fid_to_uids: dict[str, list[str]] = defaultdict(list)
    for it in results["one_to_one"]:
        fid_to_uids[str(it["fixture_id"])].append(it["match_uid"])
    multi = {f: uids for f, uids in fid_to_uids.items() if len(uids) > 1}
    if multi:
        # demote all involved to conflict
        bad_uids = {u for uids in multi.values() for u in uids}
        keep = []
        for it in results["one_to_one"]:
            if it["match_uid"] in bad_uids:
                results["conflict"].append(
                    {
                        "status": "conflict",
                        "match_uid": it["match_uid"],
                        "reason": "fixture_one_to_many",
                        "fixture_id": it["fixture_id"],
                        "siblings": multi[str(it["fixture_id"])],
                    }
                )
            else:
                keep.append(it)
        results["one_to_one"] = keep

    summary = {
        "at": now_iso(),
        "rule_version": RULE_VERSION,
        "targets": len(targets),
        "one_to_one": len(results["one_to_one"]),
        "conflict": len(results["conflict"]),
        "no_hit": len(results["no_hit"]),
        "no_league": len(results["no_league"]),
        "fixture_one_to_many_cleared": len(multi),
        "api_calls": client.calls if client else 0,
        "rate_remaining": client.remaining if client else None,
        "stopped_quota": bool(client.stopped_quota) if client else False,
        "local_only": args.local_only,
        "shadow_map": str(SHADOW_MAP),
        "dual_write": "OFF",
    }

    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "alias90_match_raw_results.json").write_text(
        json.dumps(
            {
                "summary": summary,
                "one_to_one": results["one_to_one"],
                "conflict": results["conflict"],
                "no_hit": results["no_hit"],
                "no_league": results["no_league"],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    if not args.dry_run:
        # merge with existing shadow map
        existing = []
        if SHADOW_MAP.exists():
            try:
                existing = json.loads(SHADOW_MAP.read_text(encoding="utf-8")).get("items") or []
            except Exception:
                existing = []
        by_uid = {it["match_uid"]: it for it in existing if it.get("match_uid")}
        for it in results["one_to_one"]:
            by_uid[it["match_uid"]] = it
        items = list(by_uid.values())
        # final one-to-many guard
        f2 = defaultdict(list)
        for it in items:
            f2[str(it["fixture_id"])].append(it["match_uid"])
        clean = [it for it in items if len(f2[str(it["fixture_id"])]) == 1]
        dropped = len(items) - len(clean)
        doc = {
            "generated_at": now_iso(),
            "rule_version": RULE_VERSION,
            "n": len(clean),
            "dropped_one_to_many": dropped,
            "map_confidence": "med",
            "notes": [
                "Shadow layer only; does not overwrite csl_fixture_map.json.",
                "Neighbor med — for build_queue consumption after QA.",
                "Sporttery historical results lack matchTime; kickoff_delta_min is vs jingcai-day edge.",
            ],
            "items": clean,
        }
        SHADOW_MAP.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        summary["shadow_n"] = len(clean)
        summary["dropped_one_to_many_on_write"] = dropped

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    (REPORTS / "alias90_match_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
