#!/usr/bin/env python3
"""补澳门亚盘中盘水位（+ open/close 空水位）——只写副本库。

默认 DB：api/data/macau_water_fill/app.db
源：5DollarFootballAPI
  1) GET /v1/chinasportslottery?types=jingcailottery&lang=zh-cn  → number↔jc_id → fixture_id
  2) GET /v1/fixtures/{id}/odds/history?bookmaker=macauslot&market=asian

水位：欧式小数 → 港盘 = decimal−1；water_src=actual；extras.source=5df_macauslot_history
中盘目标：collection_schedule.channel_targets rule.mid
  （≥23:00 或 0–10 → 竞彩日 15:00；否则 T−8h）
取 last pre-match tick（minute IS null）且 recorded_at ≤ target。
tick 早于 target >30min → extras.approx=true + tick_age_hours。

硬约束：
  - 默认拒绝写现网 app.db / v2d1 / v2d2（除非 --i-know-this-is-production）
  - 只 UPDATE home_water/away_water 为 NULL 的行；永不覆盖非空水位
  - mid 仅 INSERT；已存在 mid 且水位非空则跳过
  - 不改 DUAL_WRITE_ODDS_ASIAN

例：
  python3 scripts/backfill/backfill_macau_mid_water.py
  python3 ... --dry-run
  python3 ... --db /path/to/replica.db --limit 5
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
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# allow importing collection_schedule from match-analysis-api
API_ROOT = Path(str(_MA_API_ROOT))
sys.path.insert(0, str(API_ROOT))
from app.collection_schedule import TZ_CN, channel_targets  # noqa: E402

# 0.3.19：共享数据接口补数让路（北京 11:05–11:20 / 14:55–15:15 / 21:55–22:15 不发请求；补数合计 ≤16 次/分钟；
# 剩余 ≤24 停到 Reset）。共用判断：api/app/shared_api_yield.py
import sys as _yield_sys  # noqa: E402
_yield_sys.path.append(str(_MA_API_ROOT / "app"))  # 追加在末尾，不遮蔽其它模块
import shared_api_yield as _yield_mod  # noqa: E402
_YIELD = _yield_mod.Gate("backfill_macau_mid_water")


def _gated_urlopen(req, timeout=60):
    _YIELD.before_request()
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        _YIELD.after_response(e.headers)
        raise
    _YIELD.after_response(r.headers)
    return r



BASE = "https://api.5dollarfootballapi.com/v1"
DEFAULT_DB = API_ROOT / "data" / "macau_water_fill" / "app.db"
LIVE_DB = (API_ROOT / "data" / "app.db").resolve()
FORBIDDEN = {
    LIVE_DB,
    (API_ROOT / "data" / "v2d1" / "app.db").resolve(),
    (API_ROOT / "data" / "v2d2" / "app.db").resolve(),
}
WORK = Path(str(_ODDS_DATA_DIR / "5dollar/macau-mid-backfill-2026-10-07"))
RAW_CSL = WORK / "raw" / "csl"
RAW_HIST = WORK / "raw" / "hist"
LOG_DIR = WORK / "logs"
SOURCE_TAG = "5df_macauslot_history"
GAP_SEC = 2.1  # ≤30/min
RESERVE = 5

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
# also accept already-full
for k, v in list(WEEKDAY_FULL.items()):
    WEEKDAY_FULL[v] = v


def parse_dt(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def hk_water(price) -> float | None:
    if price is None:
        return None
    try:
        return round(float(price) - 1.0, 6)
    except (TypeError, ValueError):
        return None


def normalize_jc_number(number: str | None) -> str | None:
    """周六201 / 六201 → 六201"""
    if not number:
        return None
    s = str(number).strip()
    for full, short in WEEKDAY_SHORT.items():
        if s.startswith(full):
            return short + s[len(full) :]
    if s and s[0] in WEEKDAY_FULL and s[0] not in WEEKDAY_SHORT:
        # already short like 六201
        return s
    return s


def jc_id_to_full(jc_id: str) -> str:
    if not jc_id:
        return jc_id
    ch = jc_id[0]
    return (WEEKDAY_FULL.get(ch, ch) + jc_id[1:]) if ch in WEEKDAY_FULL else jc_id


class RateClient:
    def __init__(self, key: str, gap: float = GAP_SEC, reserve: int = RESERVE):
        self.key = key
        self.gap = gap
        self.reserve = reserve
        self.last = 0.0
        self.calls = 0
        self.remaining = None
        self.reset_at = None
        self.log_path = LOG_DIR / "call_log.tsv"
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        if not self.log_path.exists():
            self.log_path.write_text(
                "ts\tname\tpath\thttp\telapsed_s\tlimit\tremaining\treset\n", encoding="utf-8"
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
            headers={"Authorization": f"Bearer {self.key}", "Accept": "application/json"},
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
                f"{datetime.now(TZ_CN).strftime('%H:%M:%S')}\t{name}\t{path}\t{code}\t"
                f"{time.time()-t0:.2f}\t{headers.get('X-RateLimit-Limit')}\t{rem}\t"
                f"{headers.get('X-RateLimit-Reset')}\n"
            )
        try:
            data = json.loads(body)
        except Exception:
            data = {"_raw": body[:500].decode("utf-8", "ignore")}
        return code, data, headers

    def stop_for_quota(self) -> bool:
        return self.remaining is not None and self.remaining <= self.reserve

    def wait_if_needed(self) -> None:
        """额度将尽时睡到 X-RateLimit-Reset，而不是整轮 abort。"""
        if self.remaining is None or self.remaining > self.reserve:
            return
        if self.reset_at is None:
            time.sleep(65)
            self.remaining = None
            return
        now = time.time()
        sleep_s = max(1.0, self.reset_at - now + 1.0)
        sleep_s = min(sleep_s, 120.0)
        time.sleep(sleep_s)
        self.remaining = None


def assert_db_allowed(db: Path, *, force_live: bool) -> None:
    resolved = db.resolve()
    if resolved in FORBIDDEN and not force_live:
        raise SystemExit(
            f"拒绝写入 {resolved}（现网/v2d1/v2d2）。"
            f"请用默认 macau_water_fill 或其它新副本；"
            f"确写现网须 --i-know-this-is-production"
        )
    if resolved == LIVE_DB and force_live:
        if os.environ.get("DUAL_WRITE_ODDS_ASIAN", "0").strip().lower() not in (
            "1",
            "true",
            "yes",
            "on",
        ):
            raise SystemExit("现网双写开关关闭（DUAL_WRITE_ODDS_ASIAN!=1）")


def day_window(jingcai_date: str) -> tuple[int, int]:
    """竞彩日 12:00 BJT → +1日 12:00 BJT（≤24h；对齐 probe）。覆盖下午至次日凌晨场。"""
    base = datetime.strptime(jingcai_date, "%Y-%m-%d").replace(
        hour=12, minute=0, second=0, microsecond=0, tzinfo=TZ_CN
    )
    start = int(base.timestamp())
    end = int((base + timedelta(hours=24)).timestamp())
    return start, end


def load_matches(conn: sqlite3.Connection, limit: int | None) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    q = """
      SELECT m.id, m.match_uid, m.jc_id, m.jingcai_date, m.kickoff_at, m.kickoff_hour,
             m.home_team, m.away_team
      FROM matches m
      WHERE m.match_uid NOT LIKE 'probe:%'
      ORDER BY m.jingcai_date, m.jc_id
    """
    rows = conn.execute(q).fetchall()
    if limit:
        rows = rows[:limit]
    return rows


def fetch_csl_day(client: RateClient, jingcai_date: str) -> list[dict]:
    RAW_CSL.mkdir(parents=True, exist_ok=True)
    cache = RAW_CSL / f"{jingcai_date}.json"
    if cache.exists():
        cached = json.loads(cache.read_text(encoding="utf-8"))
        if isinstance(cached, dict) and cached.get("success") and isinstance(cached.get("data"), list):
            return cached["data"]
        # 坏缓存（旧错误体）删掉重拉
        cache.unlink(missing_ok=True)
    start, end = day_window(jingcai_date)
    code, data, _ = client.get(
        f"csl_{jingcai_date}",
        "/chinasportslottery",
        {
            "types": "jingcailottery",
            "start_time": start,
            "end_time": end,
            "per_page": 100,
            "lang": "zh-cn",
        },
    )
    if code != 200 or not (isinstance(data, dict) and data.get("success")):
        err_path = RAW_CSL / f"{jingcai_date}.err.json"
        err_path.write_text(json.dumps({"http": code, "body": data}, ensure_ascii=False, indent=2), encoding="utf-8")
        return []
    cache.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data.get("data") or []


def build_jc_map(client: RateClient, dates: list[str], report: dict) -> dict[str, dict]:
    """key = f'{jingcai_date}|{short_jc}' → fixture info"""
    mapping: dict[str, dict] = {}
    for d in dates:
        client.wait_if_needed()
        items = fetch_csl_day(client, d)
        for it in items:
            lot = (it.get("lottery") or {}).get("jingcailottery") or {}
            num = normalize_jc_number(lot.get("number"))
            if not num:
                continue
            key = f"{d}|{num}"
            mapping[key] = {
                "fixture_id": str(it["id"]),
                "number": lot.get("number"),
                "short_jc": num,
                "home": (it.get("teams") or {}).get("home", {}).get("name"),
                "away": (it.get("teams") or {}).get("away", {}).get("name"),
                "kickoff_utc": it.get("kickoff_utc"),
                "jingcai_date": d,
            }
    report["csl_mapped"] = len(mapping)
    return mapping


def fetch_history(client: RateClient, fixture_id: str) -> list[dict]:
    RAW_HIST.mkdir(parents=True, exist_ok=True)
    cache = RAW_HIST / f"{fixture_id}_macauslot_asian.json"
    if cache.exists():
        data = json.loads(cache.read_text(encoding="utf-8"))
        return ((data.get("data") or {}).get("ticks") or []) if isinstance(data, dict) else []
    code, data, _ = client.get(
        f"hist_{fixture_id}",
        f"/fixtures/{fixture_id}/odds/history",
        {"bookmaker": "macauslot", "market": "asian", "per_page": 500},
    )
    if code != 200 or not (isinstance(data, dict) and data.get("success")):
        err_path = RAW_HIST / f"{fixture_id}_macauslot_asian.err.json"
        err_path.write_text(json.dumps({"http": code, "body": data}, ensure_ascii=False, indent=2), encoding="utf-8")
        return []
    cache.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return (data.get("data") or {}).get("ticks") or []


def prematch_ticks(ticks: list[dict], kickoff: datetime) -> list[dict]:
    out = []
    for t in ticks:
        if t.get("minute") is not None:
            continue
        if t.get("suspended"):
            continue
        if t.get("home") is None and t.get("away") is None and t.get("line") is None:
            continue
        ra = parse_dt(t["recorded_at"])
        if ra >= kickoff:
            continue
        out.append(t)
    out.sort(key=lambda x: parse_dt(x["recorded_at"]))
    return out


def last_tick_at_or_before(ticks: list[dict], target: datetime) -> dict | None:
    best = None
    for t in ticks:
        ra = parse_dt(t["recorded_at"])
        if ra <= target:
            best = t
        else:
            break
    return best


def tick_payload(tick: dict, target: datetime | None) -> dict:
    ra = parse_dt(tick["recorded_at"])
    age_h = None
    approx = False
    if target is not None:
        age_h = round((target - ra).total_seconds() / 3600.0, 4)
        if (target - ra).total_seconds() > 30 * 60:
            approx = True
    return {
        "handicap": tick.get("line"),
        "home_water": hk_water(tick.get("home")),
        "away_water": hk_water(tick.get("away")),
        "recorded_at": tick["recorded_at"],
        "target_at": target.isoformat() if target else None,
        "tick_age_hours": age_h,
        "approx": approx,
    }


def extras_blob(phase: str, fixture_id: str, payload: dict) -> str:
    return json.dumps(
        {
            "source": SOURCE_TAG,
            "fixture_id": fixture_id,
            "from_channel": "rule",
            "from_point": phase,
            "snapshot_recorded_at": payload.get("recorded_at"),
            "snapshot_target_at": payload.get("target_at"),
            "tick_age_hours": payload.get("tick_age_hours"),
            "approx": payload.get("approx", False),
            "water_scale": "hk_water_from_decimal",
        },
        ensure_ascii=False,
    )


def update_null_waters(
    conn: sqlite3.Connection,
    *,
    match_id: int,
    phase: str,
    payload: dict,
    fixture_id: str,
    dry_run: bool,
) -> str:
    """UPDATE waters only if currently NULL. Return status tag."""
    row = conn.execute(
        "SELECT id, home_water, away_water, handicap, extras_json FROM odds_asian "
        "WHERE match_id=? AND book='macau' AND phase=?",
        (match_id, phase),
    ).fetchone()
    if row is None:
        return "missing_row"
    hw, aw = row["home_water"], row["away_water"]
    if hw is not None or aw is not None:
        return "skip_nonnull"
    if payload["home_water"] is None and payload["away_water"] is None:
        return "no_water_in_tick"
    if dry_run:
        return "would_update"
    # merge extras if any
    old_ex = {}
    if row["extras_json"]:
        try:
            old_ex = json.loads(row["extras_json"])
        except Exception:
            old_ex = {}
    new_ex = json.loads(extras_blob(phase, fixture_id, payload))
    old_ex.update(new_ex)
    conn.execute(
        """UPDATE odds_asian
           SET home_water=?, away_water=?, water_src='actual', water_censored=NULL,
               extras_json=?
           WHERE id=? AND home_water IS NULL AND away_water IS NULL""",
        (
            payload["home_water"],
            payload["away_water"],
            json.dumps(old_ex, ensure_ascii=False),
            row["id"],
        ),
    )
    return "updated"


def insert_mid(
    conn: sqlite3.Connection,
    *,
    match_id: int,
    payload: dict,
    fixture_id: str,
    dry_run: bool,
) -> str:
    row = conn.execute(
        "SELECT id, home_water, away_water FROM odds_asian "
        "WHERE match_id=? AND book='macau' AND phase='mid'",
        (match_id,),
    ).fetchone()
    if row is not None:
        if row["home_water"] is not None or row["away_water"] is not None:
            return "skip_existing_mid"
        # mid exists but null waters → update
        if dry_run:
            return "would_update_mid"
        conn.execute(
            """UPDATE odds_asian
               SET handicap=COALESCE(handicap, ?), home_water=?, away_water=?,
                   water_src='actual', water_censored=NULL, extras_json=?
               WHERE id=? AND home_water IS NULL AND away_water IS NULL""",
            (
                payload["handicap"],
                payload["home_water"],
                payload["away_water"],
                extras_blob("mid", fixture_id, payload),
                row["id"],
            ),
        )
        return "updated_mid"
    if payload["home_water"] is None and payload["away_water"] is None:
        return "no_water_in_tick"
    if dry_run:
        return "would_insert_mid"
    conn.execute(
        """INSERT INTO odds_asian
           (match_id, book, phase, handicap, home_water, away_water,
            water_src, water_censored, extras_json)
           VALUES (?, 'macau', 'mid', ?, ?, ?, 'actual', NULL, ?)""",
        (
            match_id,
            payload["handicap"],
            payload["home_water"],
            payload["away_water"],
            extras_blob("mid", fixture_id, payload),
        ),
    )
    return "inserted_mid"


def store_fixture_id(conn: sqlite3.Connection, match_id: int, fixture_id: str, dry_run: bool):
    row = conn.execute(
        "SELECT extras_json FROM match_meta WHERE match_id=?", (match_id,)
    ).fetchone()
    if row is None:
        return
    try:
        ex = json.loads(row["extras_json"] or "{}")
    except Exception:
        ex = {}
    ids = ex.get("ids") or {}
    if ids.get("5dollar_fixture_id") == fixture_id:
        return
    ids["5dollar_fixture_id"] = fixture_id
    ex["ids"] = ids
    if dry_run:
        return
    conn.execute(
        "UPDATE match_meta SET extras_json=? WHERE match_id=?",
        (json.dumps(ex, ensure_ascii=False), match_id),
    )


def summarize_db(conn: sqlite3.Connection) -> dict:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT phase,
          COUNT(*) n,
          SUM(CASE WHEN home_water IS NOT NULL AND away_water IS NOT NULL THEN 1 ELSE 0 END) both_w,
          SUM(CASE WHEN handicap IS NOT NULL AND (home_water IS NULL OR away_water IS NULL) THEN 1 ELSE 0 END) line_no_w
        FROM odds_asian WHERE book='macau'
        GROUP BY phase ORDER BY phase
        """
    ).fetchall()
    return {r["phase"]: dict(r) for r in rows}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--i-know-this-is-production", action="store_true")
    ap.add_argument("--gap", type=float, default=GAP_SEC)
    ap.add_argument("--skip-fetch", action="store_true", help="只用已缓存 raw，不打 API")
    args = ap.parse_args()

    assert_db_allowed(args.db, force_live=args.i_know_this_is_production)
    key = os.environ.get("FIVEDOLLAR_FOOTBALL_API_KEY")
    if not key and not args.skip_fetch:
        raise SystemExit("missing FIVEDOLLAR_FOOTBALL_API_KEY")

    WORK.mkdir(parents=True, exist_ok=True)
    RAW_CSL.mkdir(parents=True, exist_ok=True)
    RAW_HIST.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    client = RateClient(key or "unused", gap=args.gap)
    report = {
        "started_at": datetime.now(TZ_CN).isoformat(),
        "db": str(args.db.resolve()),
        "dry_run": args.dry_run,
        "limit": args.limit,
        "counts": {
            "matches": 0,
            "mapped": 0,
            "unmapped": 0,
            "hist_ok": 0,
            "hist_empty": 0,
            "open_updated": 0,
            "close_updated": 0,
            "mid_inserted": 0,
            "mid_updated": 0,
            "mid_missing_tick": 0,
            "approx_mid": 0,
            "skipped_nonnull": 0,
            "errors": 0,
        },
        "samples_filled": [],
        "samples_unmapped": [],
        "samples_no_mid_tick": [],
        "stop": None,
        "before": None,
        "after": None,
        "api_calls": 0,
        "rate_remaining": None,
    }

    conn = sqlite3.connect(str(args.db))
    conn.row_factory = sqlite3.Row
    report["before"] = summarize_db(conn)
    matches = load_matches(conn, args.limit)
    report["counts"]["matches"] = len(matches)
    dates = sorted({m["jingcai_date"] for m in matches if m["jingcai_date"]})

    if args.skip_fetch:
        # rebuild map from cache only
        mapping = {}
        for d in dates:
            p = RAW_CSL / f"{d}.json"
            if not p.exists():
                continue
            data = json.loads(p.read_text(encoding="utf-8"))
            for it in data.get("data") or []:
                lot = (it.get("lottery") or {}).get("jingcailottery") or {}
                num = normalize_jc_number(lot.get("number"))
                if not num:
                    continue
                mapping[f"{d}|{num}"] = {
                    "fixture_id": str(it["id"]),
                    "number": lot.get("number"),
                    "short_jc": num,
                    "home": (it.get("teams") or {}).get("home", {}).get("name"),
                    "away": (it.get("teams") or {}).get("away", {}).get("name"),
                    "kickoff_utc": it.get("kickoff_utc"),
                    "jingcai_date": d,
                }
        report["csl_mapped"] = len(mapping)
    else:
        mapping = build_jc_map(client, dates, report)

    for m in matches:
        if not args.skip_fetch:
            client.wait_if_needed()
        key = f"{m['jingcai_date']}|{m['jc_id']}"
        info = mapping.get(key)
        if not info:
            report["counts"]["unmapped"] += 1
            if len(report["samples_unmapped"]) < 15:
                report["samples_unmapped"].append(
                    {
                        "match_uid": m["match_uid"],
                        "jc_id": m["jc_id"],
                        "home": m["home_team"],
                        "away": m["away_team"],
                    }
                )
            continue
        report["counts"]["mapped"] += 1
        fid = info["fixture_id"]
        store_fixture_id(conn, m["id"], fid, args.dry_run)

        try:
            if args.skip_fetch:
                ticks = fetch_history(client, fid)  # uses cache
            else:
                client.wait_if_needed()
                ticks = fetch_history(client, fid)
        except Exception as e:
            report["counts"]["errors"] += 1
            report.setdefault("error_samples", []).append(
                {"match_uid": m["match_uid"], "err": str(e)[:200]}
            )
            continue

        kickoff = parse_dt(m["kickoff_at"]).astimezone(TZ_CN)
        pre = prematch_ticks(ticks, kickoff)
        if not pre:
            report["counts"]["hist_empty"] += 1
            continue
        report["counts"]["hist_ok"] += 1

        kickoff_hour = m["kickoff_hour"]
        if kickoff_hour is None:
            kickoff_hour = kickoff.hour
        targets = channel_targets(
            jingcai_date=m["jingcai_date"],
            kickoff_hour=int(kickoff_hour),
            kickoff=kickoff,
        )
        rule = targets.get("rule") or {}
        mid_tgt = rule.get("mid")
        close_tgt = rule.get("close")

        open_tick = pre[0]
        close_tick = last_tick_at_or_before(pre, close_tgt) if close_tgt else pre[-1]
        if close_tick is None:
            close_tick = pre[-1]
        mid_tick = last_tick_at_or_before(pre, mid_tgt) if mid_tgt else None

        open_p = tick_payload(open_tick, parse_dt(open_tick["recorded_at"]))
        close_p = tick_payload(close_tick, close_tgt)
        st_o = update_null_waters(
            conn, match_id=m["id"], phase="open", payload=open_p, fixture_id=fid, dry_run=args.dry_run
        )
        st_c = update_null_waters(
            conn, match_id=m["id"], phase="close", payload=close_p, fixture_id=fid, dry_run=args.dry_run
        )
        if st_o in ("updated", "would_update"):
            report["counts"]["open_updated"] += 1
        if st_c in ("updated", "would_update"):
            report["counts"]["close_updated"] += 1
        if st_o == "skip_nonnull" or st_c == "skip_nonnull":
            report["counts"]["skipped_nonnull"] += 1

        st_m = "no_mid_tick"
        if mid_tick is None:
            report["counts"]["mid_missing_tick"] += 1
            if len(report["samples_no_mid_tick"]) < 10:
                report["samples_no_mid_tick"].append(
                    {
                        "match_uid": m["match_uid"],
                        "mid_target": mid_tgt.isoformat() if mid_tgt else None,
                        "first_tick": pre[0]["recorded_at"],
                        "n_pre": len(pre),
                    }
                )
        else:
            mid_p = tick_payload(mid_tick, mid_tgt)
            if mid_p.get("approx"):
                report["counts"]["approx_mid"] += 1
            st_m = insert_mid(
                conn, match_id=m["id"], payload=mid_p, fixture_id=fid, dry_run=args.dry_run
            )
            if st_m in ("inserted_mid", "would_insert_mid"):
                report["counts"]["mid_inserted"] += 1
            elif st_m in ("updated_mid", "would_update_mid"):
                report["counts"]["mid_updated"] += 1
            elif st_m == "skip_existing_mid":
                report["counts"]["skipped_nonnull"] += 1

        if len(report["samples_filled"]) < 8 and st_m in (
            "inserted_mid",
            "would_insert_mid",
            "updated_mid",
            "would_update_mid",
        ):
            report["samples_filled"].append(
                {
                    "match_uid": m["match_uid"],
                    "fixture_id": fid,
                    "open": {"hw": open_p["home_water"], "aw": open_p["away_water"], "line": open_p["handicap"]},
                    "mid": {
                        "hw": mid_p["home_water"],
                        "aw": mid_p["away_water"],
                        "line": mid_p["handicap"],
                        "approx": mid_p.get("approx"),
                        "age_h": mid_p.get("tick_age_hours"),
                        "target": mid_tgt.isoformat() if mid_tgt else None,
                    },
                    "close": {
                        "hw": close_p["home_water"],
                        "aw": close_p["away_water"],
                        "line": close_p["handicap"],
                    },
                    "status": {"open": st_o, "mid": st_m, "close": st_c},
                }
            )

        if not args.dry_run and report["counts"]["mapped"] % 20 == 0:
            conn.commit()

    if not args.dry_run:
        conn.commit()
    report["after"] = summarize_db(conn)
    report["finished_at"] = datetime.now(TZ_CN).isoformat()
    report["api_calls"] = client.calls
    report["rate_remaining"] = client.remaining

    # S1 unlock heuristic
    after = report["after"]
    mid = after.get("mid") or {}
    open_ = after.get("open") or {}
    close_ = after.get("close") or {}
    n_matches = report["counts"]["matches"]
    mid_ok = mid.get("both_w") or 0
    open_ok = open_.get("both_w") or 0
    close_ok = close_.get("both_w") or 0
    report["s1_unlock"] = {
        "need": "macau open+mid+close with real waters on enough matches",
        "open_with_water": open_ok,
        "mid_with_water": mid_ok,
        "close_with_water": close_ok,
        "n_matches": n_matches,
        "can_unlock_strict": bool(
            mid_ok >= 30 and open_ok >= 30 and close_ok >= 30
        ),  # soft gate; real L0 elsewhere
        "note": "严格 S1 还需三段盘口不动∩主水在 S1 水位带内（见 config/strategy_params.json）；本补齐只解数据阻塞。"
        if mid_ok
        else "仍缺 macau mid 水位，S1 保持 blocked。",
    }

    out = LOG_DIR / "fill_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\nWrote report → {out}", file=sys.stderr)
    conn.close()


if __name__ == "__main__":
    main()
