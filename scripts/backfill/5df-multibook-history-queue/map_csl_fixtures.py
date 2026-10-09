#!/usr/bin/env python3
"""Map sporttery JC matches → 5DF fixture_id via /v1/chinasportslottery day windows.

Bounded first pass: --days N (recent unmapped days) OR --max-new M new mappings,
whichever comes first. Writes raw/csl/{YYYY-MM-DD}.json, updates csl_fixture_map.json,
then re-runs build_queue.py so pending grows.

Hard rules:
  - Key from FIVEDOLLAR_FOOTBALL_API_KEY only; never print it
  - ≤30/min (gap 2.1s); stop if X-RateLimit-Remaining ≤ 5
  - No prod DB; dual_write stays OFF
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
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 0.3.19：共享数据接口补数让路（北京 11:05–11:20 / 14:55–15:15 / 21:55–22:15 不发请求；补数合计 ≤16 次/分钟；
# 剩余 ≤24 停到 Reset）。共用判断：api/app/shared_api_yield.py
import sys as _yield_sys  # noqa: E402
_yield_sys.path.append(str(_MA_API_ROOT / "app"))  # 追加在末尾，不遮蔽其它模块
import shared_api_yield as _yield_mod  # noqa: E402
_YIELD = _yield_mod.Gate("map_csl_fixtures")


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
RAW_CSL = ROOT / "raw" / "csl"
LOG_DIR = ROOT / "logs"
MAP_PATH = ROOT / "csl_fixture_map.json"
FIXTURE_MAP = Path(str(_ODDS_DATA_DIR / "backfill/macau_mid_water_fixture_map.json"))
EXTRA_CSL_DIRS = [
    Path(str(_ODDS_DATA_DIR / "5dollar/macau-mid-backfill-2026-10-07/raw/csl")),
]
BASE = "https://api.5dollarfootballapi.com/v1"
GAP_SEC = 2.1
RESERVE = 5
CSL_NUMBER_FROM = "2025-10-25"
TZ = timezone(timedelta(hours=8))

WEEKDAY_FULL = {
    "一": "周一",
    "二": "周二",
    "三": "周三",
    "四": "周四",
    "五": "周五",
    "六": "周六",
    "日": "周日",
}
WEEKDAY_SHORT = {v: k for k, v in WEEKDAY_FULL.items()}


def now_iso() -> str:
    return datetime.now(TZ).isoformat()


def normalize_jc(number: str | None) -> str | None:
    if not number:
        return None
    s = str(number).strip()
    for full, short in WEEKDAY_SHORT.items():
        if s.startswith(full):
            return short + s[len(full) :]
    return s


def jc_full(short: str | None) -> str | None:
    if not short:
        return None
    ch = short[0]
    return (WEEKDAY_FULL.get(ch, ch) + short[1:]) if ch in WEEKDAY_FULL else short


def day_window(jingcai_date: str) -> tuple[int, int]:
    """竞彩日 12:00 BJ → +24h（对齐 probe / macau mid backfill）。"""
    base = datetime.strptime(jingcai_date, "%Y-%m-%d").replace(
        hour=12, minute=0, second=0, microsecond=0, tzinfo=TZ
    )
    start = int(base.timestamp())
    end = int((base + timedelta(hours=24)).timestamp())
    return start, end


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


class RateClient:
    def __init__(self, key: str, gap: float = GAP_SEC, reserve: int = RESERVE):
        self.key = key
        self.gap = gap
        self.reserve = reserve
        self.last = 0.0
        self.calls = 0
        self.remaining: int | None = None
        self.reset_at: float | None = None
        self.stopped_quota = False
        self.log_path = LOG_DIR / "csl_map_call_log.tsv"
        LOG_DIR.mkdir(parents=True, exist_ok=True)
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
        rst = headers.get("X-RateLimit-Reset")
        if rst:
            try:
                self.reset_at = float(rst)
            except ValueError:
                pass
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


def existing_mapped_keys() -> set[str]:
    """Keys already known from macau map + any CSL day caches."""
    keys: set[str] = set()
    if FIXTURE_MAP.exists():
        m = json.loads(FIXTURE_MAP.read_text(encoding="utf-8"))
        for it in m.get("items") or []:
            jd = it.get("jingcai_date")
            short = normalize_jc(it.get("jc_id") or it.get("jc_norm"))
            if jd and short:
                keys.add(f"{jd}|{short}")
    if MAP_PATH.exists():
        m = json.loads(MAP_PATH.read_text(encoding="utf-8"))
        for it in m.get("items") or []:
            jd = it.get("jingcai_date")
            short = normalize_jc(it.get("jc_id") or it.get("jc_norm"))
            if jd and short:
                keys.add(f"{jd}|{short}")
    date_re_ok = True
    for d in [RAW_CSL, *EXTRA_CSL_DIRS]:
        if not d.is_dir():
            continue
        for p in d.glob("*.json"):
            jd = p.stem
            if len(jd) != 10:
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            for it in data.get("data") or []:
                lot = (it.get("lottery") or {}).get("jingcailottery") or {}
                short = normalize_jc(lot.get("number"))
                if short and it.get("id"):
                    keys.add(f"{jd}|{short}")
    _ = date_re_ok
    return keys


def parse_csl_day(data: dict, jingcai_date: str) -> list[dict]:
    out = []
    for it in data.get("data") or []:
        lot = (it.get("lottery") or {}).get("jingcailottery") or {}
        short = normalize_jc(lot.get("number"))
        fid = it.get("id")
        if not short or not fid:
            continue
        teams = it.get("teams") or {}
        out.append(
            {
                "fixture_id": str(fid),
                "jingcai_date": jingcai_date,
                "jc_id": short,
                "jc_norm": lot.get("number") or jc_full(short),
                "home_team": (teams.get("home") or {}).get("name"),
                "away_team": (teams.get("away") or {}).get("name"),
                "kickoff_at": it.get("kickoff_utc"),
                "league_name": (it.get("league") or {}).get("name"),
                "match_uid": f"{jingcai_date}|{short}",
                "source": "5df_chinasportslottery",
            }
        )
    return out




def iter_local_csl_payloads() -> list[tuple[str, dict, str]]:
    """Yield (jingcai_date, payload, source_tag) from on-disk CSL caches only."""
    out: list[tuple[str, dict, str]] = []
    seen_dates: set[str] = set()
    for d in [RAW_CSL, *EXTRA_CSL_DIRS]:
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.json")):
            jd = p.stem
            if len(jd) != 10 or jd in seen_dates:
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not (isinstance(data, dict) and data.get("success") and isinstance(data.get("data"), list)):
                continue
            seen_dates.add(jd)
            out.append((jd, data, f"cache:{p}"))
    return out


def index_csl_by_file_jc(payloads: list[tuple[str, dict, str]]) -> dict[tuple[str, str], dict]:
    """(file_date, jc_short) → parsed map item (jingcai_date=file_date)."""
    idx: dict[tuple[str, str], dict] = {}
    for jd, data, _src in payloads:
        for it in parse_csl_day(data, jd):
            idx[(jd, it["jc_id"])] = it
    return idx


def neighbor_dates(jingcai_date: str) -> list[str]:
    base = datetime.strptime(jingcai_date, "%Y-%m-%d")
    return [(base + timedelta(days=off)).strftime("%Y-%m-%d") for off in (-1, 0, 1)]


def team_soft_score(u: dict, csl: dict) -> int:
    uh = (u.get("home_team") or "").strip()
    ua = (u.get("away_team") or "").strip()
    ch = (csl.get("home_team") or "").strip()
    ca = (csl.get("away_team") or "").strip()
    score = 0
    if uh and ch and (uh in ch or ch in uh):
        score += 1
    if ua and ca and (ua in ca or ca in ua):
        score += 1
    return score


def neighbor_join_unmapped(
    unmapped: list[dict],
    csl_index: dict[tuple[str, str], dict],
) -> tuple[dict[str, dict], dict]:
    """Match unmapped sporttery rows to CSL fixtures on file_date ∈ {jd-1,jd,jd+1}.

    Returns (items_by_sporttery_key, stats). Keys use sporttery jingcai_date|jc_id
    so build_queue universe join succeeds. Zero HTTP.
    """
    items: dict[str, dict] = {}
    stats = {
        "candidates_near_raw": 0,
        "exact_file_day": 0,
        "neighbor_jc": 0,
        "neighbor_jc_team_tiebreak": 0,
        "ambiguous": 0,
        "no_hit": 0,
        "offset_counts": {},
    }
    raw_days = {fd for (fd, _jc) in csl_index.keys()}
    offset_counts: dict[str, int] = {}

    for u in unmapped:
        jd = u.get("jingcai_date")
        jc = normalize_jc(u.get("jc_id"))
        if not jd or not jc:
            continue
        neigh = neighbor_dates(jd)
        if not any(n in raw_days for n in neigh):
            continue
        stats["candidates_near_raw"] += 1

        # Prefer exact file-day key, then ±1 with same jc
        ordered_days = [jd] + [n for n in neigh if n != jd]
        cands: list[tuple[str, dict]] = []
        for nd in ordered_days:
            hit = csl_index.get((nd, jc))
            if hit:
                cands.append((nd, hit))
        # unique by fixture_id
        seen: set[str] = set()
        uniq: list[tuple[str, dict]] = []
        for nd, hit in cands:
            fid = str(hit["fixture_id"])
            if fid in seen:
                continue
            seen.add(fid)
            uniq.append((nd, hit))

        chosen: dict | None = None
        file_date: str | None = None
        how = None
        if len(uniq) == 1:
            file_date, chosen = uniq[0]
            how = "exact_file_day" if file_date == jd else "neighbor_jc"
        elif len(uniq) > 1:
            scored = sorted(
                ((team_soft_score(u, h), nd, h) for nd, h in uniq),
                key=lambda t: -t[0],
            )
            if scored[0][0] >= 1 and (
                len(scored) == 1 or scored[0][0] > scored[1][0]
            ):
                _sc, file_date, chosen = scored[0]
                how = "neighbor_jc_team_tiebreak"
            else:
                stats["ambiguous"] += 1
                continue
        else:
            stats["no_hit"] += 1
            continue

        assert chosen is not None and file_date is not None and how is not None
        stats[how] = stats.get(how, 0) + 1
        off = (
            datetime.strptime(file_date, "%Y-%m-%d")
            - datetime.strptime(jd, "%Y-%m-%d")
        ).days
        offset_counts[str(off)] = offset_counts.get(str(off), 0) + 1

        key = f"{jd}|{jc}"
        items[key] = {
            "fixture_id": str(chosen["fixture_id"]),
            "jingcai_date": jd,  # sporttery date (join key)
            "jc_id": jc,
            "jc_norm": chosen.get("jc_norm") or jc_full(jc),
            "home_team": chosen.get("home_team") or u.get("home_team"),
            "away_team": chosen.get("away_team") or u.get("away_team"),
            "kickoff_at": chosen.get("kickoff_at"),
            "league_name": chosen.get("league_name") or u.get("league_name"),
            "match_uid": f"{jd}|{jc}",
            "source": "csl_neighbor_join",
            "csl_file_date": file_date,
            "join_offset_days": off,
            "join_method": how,
        }

    stats["offset_counts"] = offset_counts
    stats["joined"] = len(items)
    return items, stats


def load_or_fetch_day(
    client: RateClient | None, jingcai_date: str, *, force: bool = False
) -> tuple[dict | None, bool, str]:
    """Return (payload, fetched_from_api, source_tag). Prefer local caches."""
    local = RAW_CSL / f"{jingcai_date}.json"
    if local.exists() and not force:
        try:
            data = json.loads(local.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("success") and isinstance(
                data.get("data"), list
            ):
                return data, False, f"cache:{local}"
        except Exception:
            pass
    for d in EXTRA_CSL_DIRS:
        p = d / f"{jingcai_date}.json"
        if p.exists() and not force:
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("success") and isinstance(
                    data.get("data"), list
                ):
                    # copy into queue raw for build_queue
                    RAW_CSL.mkdir(parents=True, exist_ok=True)
                    local.write_text(
                        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                    return data, False, f"cache:{p}"
            except Exception:
                continue

    if client is None:
        return None, False, "no_client"

    if client.stopped_quota:
        return None, False, "quota_stop"

    start, end = day_window(jingcai_date)
    all_rows: list = []
    page = 1
    last_payload: dict | None = None
    while True:
        if client.stopped_quota:
            break
        code, data, _ = client.get(
            f"csl_{jingcai_date}_p{page}",
            "/chinasportslottery",
            {
                "types": "jingcailottery",
                "start_time": start,
                "end_time": end,
                "per_page": 100,
                "page": page,
                "lang": "zh-cn",
            },
        )
        if code != 200 or not (isinstance(data, dict) and data.get("success")):
            err = RAW_CSL / f"{jingcai_date}.err.json"
            RAW_CSL.mkdir(parents=True, exist_ok=True)
            err.write_text(
                json.dumps({"http": code, "body": data}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return None, True, f"http_{code}"
        last_payload = data
        rows = data.get("data") or []
        all_rows.extend(rows)
        pag = data.get("pagination") or {}
        if not pag.get("has_more"):
            break
        page += 1
        if page > 10:
            break

    if last_payload is None:
        return None, True, "empty"
    payload = {
        "success": 1,
        "data": all_rows,
        "pagination": {
            "page": 1,
            "per_page": 100,
            "count": len(all_rows),
            "has_more": False,
            "merged_pages": page,
        },
        "_meta": {
            "jingcai_date": jingcai_date,
            "start_time": start,
            "end_time": end,
            "fetched_at": now_iso(),
        },
    }
    RAW_CSL.mkdir(parents=True, exist_ok=True)
    local.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return payload, True, f"api:{local}"


def _day_cached(d: str) -> bool:
    for base in [RAW_CSL, *EXTRA_CSL_DIRS]:
        if (base / f"{d}.json").exists():
            return True
    return False


def select_target_days(unmapped: list[dict], days: int, skip_cached: bool = True) -> list[str]:
    """Prefer densest unmapped jingcai days in CSL-number era, up to `days`.

    Tie-break: earlier date first. Then fill remaining budget with ±1 neighbors
    of those cores (helps neighbor_join; prior cycles often need file_date=jd±1).
    """
    from collections import Counter

    c = Counter(
        r["jingcai_date"]
        for r in unmapped
        if r.get("jingcai_date") and r["jingcai_date"] >= CSL_NUMBER_FROM
    )
    cores = [d for d, _n in sorted(c.items(), key=lambda t: (-t[1], t[0]))]
    # 2026-10-07 fix: densest days were already cached-but-unmatched, so the
    # planner never reached uncached days (api_calls=0 forever). Skip cached.
    if skip_cached:
        cores = [d for d in cores if not _day_cached(d)]
    chosen: list[str] = []
    seen: set[str] = set()
    for d in cores:
        if len(chosen) >= days:
            break
        if d not in seen:
            chosen.append(d)
            seen.add(d)
    # Fill leftover slots with ±1 around densest cores (core order preserved)
    for d in list(chosen):
        if len(chosen) >= days:
            break
        for nd in neighbor_dates(d):
            if len(chosen) >= days:
                break
            if nd >= CSL_NUMBER_FROM and nd not in seen and not (skip_cached and _day_cached(nd)):
                chosen.append(nd)
                seen.add(nd)
    return chosen


def save_map(items_by_key: dict[str, dict], report: dict) -> None:
    items = [items_by_key[k] for k in sorted(items_by_key.keys())]
    doc = {
        "generated_at": now_iso(),
        "n": len(items),
        "source": "5df_chinasportslottery",
        "window_note": "jingcai day 12:00 BJ → +24h; types=jingcailottery",
        "csl_number_from": CSL_NUMBER_FROM,
        "items": items,
        "last_run": report,
    }
    MAP_PATH.write_text(
        json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--days",
        type=int,
        default=14,
        help="Max densest unmapped jingcai days to process (default 14)",
    )
    ap.add_argument(
        "--max-new",
        type=int,
        default=200,
        help="Stop after this many NEW mappings vs prior keys (default 200)",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="Re-fetch even if local CSL cache exists",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan days only; no API",
    )
    ap.add_argument(
        "--local-only",
        action="store_true",
        help="Zero-API: neighbor-day join using raw/csl (+ EXTRA) already on disk",
    )
    ap.add_argument(
        "--skip-rebuild",
        action="store_true",
        help="Do not re-run build_queue.py",
    )
    args = ap.parse_args()

    pending_before = len(load_jsonl(QUEUE / "pending.jsonl"))
    unmapped = load_jsonl(QUEUE / "unmapped.jsonl")
    unmapped_before = len(unmapped)
    unmapped_uids = {r.get("match_uid") for r in unmapped if r.get("match_uid")}

    target_days = select_target_days(unmapped, args.days, skip_cached=not args.force)
    prior_keys = existing_mapped_keys()

    report: dict = {
        "started_at": now_iso(),
        "pending_before": pending_before,
        "unmapped_before": unmapped_before,
        "prior_mapped_keys": len(prior_keys),
        "target_days_planned": target_days,
        "days_arg": args.days,
        "max_new": args.max_new,
        "days_pulled": [],
        "days_from_cache": [],
        "days_from_api": [],
        "days_failed": [],
        "days_empty_jc": [],
        "new_mappings": 0,
        "new_mapping_keys": [],
        "api_calls": 0,
        "rate_remaining": None,
        "stopped_reason": None,
        "dual_write": "OFF",
    }

    if args.dry_run:
        report["stopped_reason"] = "dry_run"
        report["finished_at"] = now_iso()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    # Seed map items from prior map file
    items_by_key: dict[str, dict] = {}
    if MAP_PATH.exists():
        for it in json.loads(MAP_PATH.read_text(encoding="utf-8")).get("items") or []:
            k = f"{it.get('jingcai_date')}|{normalize_jc(it.get('jc_id'))}"
            if it.get("fixture_id") and "|" in k:
                items_by_key[k] = it

    new_count = 0

    if args.local_only:
        report["mode"] = "local_only_neighbor_join"
        payloads = iter_local_csl_payloads()
        report["local_csl_days"] = [jd for jd, _d, _s in payloads]
        report["days_from_cache"] = list(report["local_csl_days"])
        report["days_pulled"] = list(report["local_csl_days"])
        # Keep file-date keys from caches (orphan / exact)
        for jd, data, _src in payloads:
            for it in parse_csl_day(data, jd):
                items_by_key[f"{it['jingcai_date']}|{it['jc_id']}"] = it
        csl_index = index_csl_by_file_jc(payloads)
        joined, jstats = neighbor_join_unmapped(unmapped, csl_index)
        report["neighbor_join"] = jstats
        for k, it in joined.items():
            if k not in prior_keys and k in unmapped_uids:
                new_count += 1
                prior_keys.add(k)
                report["new_mapping_keys"].append(k)
            items_by_key[k] = it
        report["new_mappings"] = new_count
        report["api_calls"] = 0
        report["rate_remaining"] = None
        report["stopped_reason"] = "local_only_complete"
    else:
        key = os.environ.get("FIVEDOLLAR_FOOTBALL_API_KEY", "").strip()
        if not key:
            print("ERROR: FIVEDOLLAR_FOOTBALL_API_KEY not set", file=sys.stderr)
            return 2

        client = RateClient(key)

        for jd in target_days:
            if new_count >= args.max_new:
                report["stopped_reason"] = f"max_new_{args.max_new}"
                break
            if client.stopped_quota:
                report["stopped_reason"] = "rate_remaining_le_5"
                break

            data, fetched, src = load_or_fetch_day(client, jd, force=args.force)
            if data is None:
                report["days_failed"].append({"date": jd, "src": src})
                if client.stopped_quota:
                    report["stopped_reason"] = "rate_remaining_le_5"
                    break
                continue

            report["days_pulled"].append(jd)
            if fetched:
                report["days_from_api"].append(jd)
            else:
                report["days_from_cache"].append(jd)

            parsed = parse_csl_day(data, jd)
            if not parsed:
                report["days_empty_jc"].append(jd)

            for it in parsed:
                k = f"{it['jingcai_date']}|{it['jc_id']}"
                items_by_key[k] = it
                is_new = k not in prior_keys
                joins_unmapped = it["match_uid"] in unmapped_uids
                if is_new and joins_unmapped:
                    new_count += 1
                    prior_keys.add(k)
                    report["new_mapping_keys"].append(k)
                    if new_count >= args.max_new:
                        report["stopped_reason"] = f"max_new_{args.max_new}"
                        break

            if report["stopped_reason"]:
                break

        # After API/cache day pull, also neighbor-join using ALL local payloads
        payloads = iter_local_csl_payloads()
        csl_index = index_csl_by_file_jc(payloads)
        joined, jstats = neighbor_join_unmapped(unmapped, csl_index)
        report["neighbor_join"] = jstats
        for k, it in joined.items():
            if k not in items_by_key:
                items_by_key[k] = it
            else:
                # prefer sporttery-keyed neighbor item when prior was file-date only
                items_by_key[k] = it
            if k not in prior_keys and k in unmapped_uids:
                if k not in report["new_mapping_keys"]:
                    new_count += 1
                    prior_keys.add(k)
                    report["new_mapping_keys"].append(k)

        report["new_mappings"] = new_count
        report["api_calls"] = client.calls
        report["rate_remaining"] = client.remaining
        if not report["stopped_reason"]:
            report["stopped_reason"] = "days_complete"

    save_map(items_by_key, {k: v for k, v in report.items() if k != "new_mapping_keys"})

    pending_after = pending_before
    unmapped_after = unmapped_before
    if not args.skip_rebuild:
        rc = subprocess.call([sys.executable, str(ROOT / "build_queue.py")], cwd=str(ROOT))
        report["build_queue_rc"] = rc
        pending_after = len(load_jsonl(QUEUE / "pending.jsonl"))
        unmapped_after = len(load_jsonl(QUEUE / "unmapped.jsonl"))

    report["pending_after"] = pending_after
    report["unmapped_after"] = unmapped_after
    report["pending_delta"] = pending_after - pending_before
    report["unmapped_delta"] = unmapped_after - unmapped_before
    report["finished_at"] = now_iso()
    report["map_path"] = str(MAP_PATH)
    report["raw_csl_dir"] = str(RAW_CSL)

    # trim keys list in printed report if huge
    out = dict(report)
    if len(out.get("new_mapping_keys") or []) > 30:
        out["new_mapping_keys_sample"] = out["new_mapping_keys"][:30]
        out["new_mapping_keys_n"] = len(out["new_mapping_keys"])
        del out["new_mapping_keys"]

    (LOG_DIR / "csl_map_last_run.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
