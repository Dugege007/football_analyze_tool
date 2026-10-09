#!/usr/bin/env python3
"""补澳门中盘/初临真实水位（5DF history → 副本）。

口径（2026-10-07 与足球分析师对齐）：
- 源：GET /v1/fixtures/{id}/odds/history?bookmaker=macauslot&market=asian
- 欧式小数 → 港盘水位 = 小数 − 1
- 中盘：channel=rule · point=mid（≥23:00 北京 → 竞彩日 15:00；其余 T−8h）
  取 recorded_at ≤ 中盘时刻、minute 为空的最后一条赛前 tick；稀 tick 标 approx + tick_age_hours
- 同时尽量补 open/close 真实水位：只填 odds_asian 中 NULL，不覆盖非空旧手工
- 标 source=5df_macauslot_history、water_src=actual
- 默认只写副本（v2d3）；现网需 --i-know-this-is-production 且 DUAL_WRITE_ODDS_ASIAN=1（本任务不启用）
- 限速 ≤30/分钟

用法：
  .venv/bin/python scripts/fill_macau_mid_water.py fetch
  .venv/bin/python scripts/fill_macau_mid_water.py import --db data/v2d3/app.db
  .venv/bin/python scripts/fill_macau_mid_water.py report --db data/v2d3/app.db
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
import hashlib
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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.collection_schedule import TZ_CN, channel_targets  # noqa: E402
from app.db import apply_sql_idempotent, connect  # noqa: E402

# 0.3.19：共享数据接口补数让路（北京 11:05–11:20 / 14:55–15:15 / 21:55–22:15 不发请求；补数合计 ≤16 次/分钟；
# 剩余 ≤24 停到 Reset）。共用判断：api/app/shared_api_yield.py
import sys as _yield_sys  # noqa: E402
_yield_sys.path.append(str(_MA_API_ROOT / "app"))  # 追加在末尾，不遮蔽其它模块
import shared_api_yield as _yield_mod  # noqa: E402
_YIELD = _yield_mod.Gate("fill_macau_mid_water")


def _gated_urlopen(req, timeout=60):
    _YIELD.before_request()
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        _YIELD.after_response(e.headers)
        raise
    _YIELD.after_response(r.headers)
    return r



MAP_PATH = Path(str(_ODDS_DATA_DIR / "backfill/macau_mid_water_fixture_map.json"))
RAW_DIR = Path(str(_ODDS_DATA_DIR / "5dollar/macau-mid-water-fill-2026-10-07/raw"))
LOG_DIR = Path(str(_ODDS_DATA_DIR / "5dollar/macau-mid-water-fill-2026-10-07/logs"))
MIGRATION = Path(str(_REPO_ROOT / "docs/schema/v2_0_odds_timeline.sql"))
DEFAULT_DB = ROOT / "data" / "v2d3" / "app.db"
BASE = "https://api.5dollarfootballapi.com/v1"
SOURCE = "5df_macauslot_history"
BOOK = "macau"
MARKET = "asian"
MIN_INTERVAL = 2.1  # ≤30/min


def _hk_water(price) -> float | None:
    if price is None:
        return None
    return round(float(price) - 1.0, 6)


def _parse_dt(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _env_dual_write_on() -> bool:
    return os.environ.get("DUAL_WRITE_ODDS_ASIAN", "0").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def assert_db_allowed(db: Path, *, force_live: bool) -> None:
    resolved = db.resolve()
    live = (ROOT / "data" / "app.db").resolve()
    if resolved == live:
        if not force_live:
            raise SystemExit(
                "拒绝写现网 data/app.db。请用副本（如 data/v2d3/app.db）。"
                "若确要写现网须同时：DUAL_WRITE_ODDS_ASIAN=1 且 --i-know-this-is-production"
            )
        if not _env_dual_write_on():
            raise SystemExit("现网双写开关关闭（DUAL_WRITE_ODDS_ASIAN!=1）。按拍板保持关闭。")


def load_map() -> dict:
    return json.loads(MAP_PATH.read_text(encoding="utf-8"))


def api_get(path: str, params: dict) -> tuple[dict, int, dict]:
    key = os.environ.get("FIVEDOLLAR_FOOTBALL_API_KEY")
    if not key:
        raise SystemExit("FIVEDOLLAR_FOOTBALL_API_KEY 未设置")
    q = urllib.parse.urlencode(params)
    url = f"{BASE}{path}?{q}" if q else f"{BASE}{path}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    try:
        with _gated_urlopen(req, timeout=90) as r:
            body = r.read()
            return json.loads(body), r.status, dict(r.headers)
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            j = json.loads(body)
        except Exception:
            j = {"raw": body[:500].decode("utf-8", "replace")}
        return j, e.code, dict(e.headers)


def fetch_one(fixture_id: str, *, force: bool = False) -> dict:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    out = RAW_DIR / f"{fixture_id}_macauslot_asian.json"
    if out.exists() and out.stat().st_size > 50 and not force:
        j = json.loads(out.read_text(encoding="utf-8"))
        ticks = ((j.get("data") or {}).get("ticks")) or []
        return {
            "fixture_id": fixture_id,
            "path": str(out),
            "skipped": True,
            "http": 200,
            "ticks": len(ticks),
            "pre": sum(1 for t in ticks if t.get("minute") is None),
        }

    all_ticks: list = []
    page = 1
    http = 200
    last_j: dict = {}
    while True:
        j, http, hdr = api_get(
            f"/fixtures/{fixture_id}/odds/history",
            {
                "bookmaker": "macauslot",
                "market": "asian",
                "per_page": 500,
                "page": page,
            },
        )
        last_j = j
        if http == 429:
            ra = float(hdr.get("Retry-After", 3))
            time.sleep(ra + 0.5)
            continue
        if http != 200 or not j.get("success"):
            out.write_text(json.dumps(j, ensure_ascii=False), encoding="utf-8")
            return {
                "fixture_id": fixture_id,
                "path": str(out),
                "skipped": False,
                "http": http,
                "ticks": 0,
                "pre": 0,
                "error": True,
            }
        data = j.get("data") or {}
        ticks = data.get("ticks") or []
        all_ticks.extend(ticks)
        if not (j.get("pagination") or {}).get("has_more"):
            break
        page += 1
        if page > 20:
            break
        time.sleep(MIN_INTERVAL)

    # rewrite with merged ticks
    merged = {
        "success": 1,
        "data": {
            "fixture_id": int(fixture_id) if str(fixture_id).isdigit() else fixture_id,
            "bookmaker": (last_j.get("data") or {}).get("bookmaker")
            or {"name": "Macauslot", "slug": "macauslot"},
            "market": "asian",
            "ticks": all_ticks,
        },
        "pagination": {
            "page": 1,
            "per_page": len(all_ticks),
            "count": len(all_ticks),
            "has_more": False,
            "merged_pages": page,
        },
    }
    out.write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")
    pre = [t for t in all_ticks if t.get("minute") is None]
    return {
        "fixture_id": fixture_id,
        "path": str(out),
        "skipped": False,
        "http": http,
        "ticks": len(all_ticks),
        "pre": len(pre),
    }


def cmd_fetch(args: argparse.Namespace) -> None:
    m = load_map()
    items = m["items"]
    if args.limit:
        items = items[: args.limit]
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / "fetch_log.jsonl"
    results = []
    t_last = 0.0
    for i, it in enumerate(items):
        fid = str(it["fixture_id"])
        # pace
        wait = MIN_INTERVAL - (time.time() - t_last)
        if wait > 0 and not (
            (RAW_DIR / f"{fid}_macauslot_asian.json").exists() and not args.force
        ):
            time.sleep(wait)
        t_last = time.time()
        r = fetch_one(fid, force=args.force)
        r["match_id"] = it["match_id"]
        r["match_uid"] = it["match_uid"]
        results.append(r)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({**r, "ts": datetime.now(TZ_CN).isoformat()}, ensure_ascii=False) + "\n")
        flag = "skip" if r.get("skipped") else ("ERR" if r.get("error") else "ok")
        print(
            f"[{i+1}/{len(items)}] {flag} mid={it['match_id']} fid={fid} "
            f"http={r.get('http')} ticks={r.get('ticks')} pre={r.get('pre')}",
            flush=True,
        )
    ok = sum(1 for r in results if not r.get("error") and r.get("pre", 0) > 0)
    empty = sum(1 for r in results if not r.get("error") and r.get("pre", 0) == 0)
    err = sum(1 for r in results if r.get("error"))
    summary = {
        "n": len(results),
        "with_pre_ticks": ok,
        "empty_pre": empty,
        "errors": err,
        "skipped_cached": sum(1 for r in results if r.get("skipped")),
    }
    (LOG_DIR / "fetch_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def pre_ticks(ticks: list[dict]) -> list[dict]:
    out = [t for t in ticks if t.get("minute") is None]
    out.sort(key=lambda t: _parse_dt(t["recorded_at"]))
    return out


def last_tick_at_or_before(ticks: list[dict], target: datetime) -> dict | None:
    best = None
    for t in ticks:
        ra = _parse_dt(t["recorded_at"])
        if ra <= target:
            best = t
        else:
            break
    return best


def snap_row(tick: dict | None, *, target: datetime | None, point: str, channel: str) -> dict | None:
    if tick is None:
        return None
    ra = _parse_dt(tick["recorded_at"])
    lag = None
    approx = False
    if target is not None:
        lag = (target - ra).total_seconds() / 3600.0
        # 澳门稀：>2h 标 approx
        if lag > 2.0:
            approx = True
    extras = {
        "source": SOURCE,
        "approx": approx,
        "tick_age_hours": round(lag, 4) if lag is not None else 0.0,
    }
    return {
        "channel": channel,
        "point": point,
        "recorded_at": _iso(ra),
        "target_at": _iso(target) if target else _iso(ra),
        "lag_hours": round(lag, 4) if lag is not None else 0.0,
        "stale_gap": 1 if approx else 0,
        "line": tick.get("line"),
        "price_home": tick.get("home"),
        "price_away": tick.get("away"),
        "water_home": _hk_water(tick.get("home")),
        "water_away": _hk_water(tick.get("away")),
        "water_src": "actual",
        "water_censored": None,
        "source": SOURCE,
        "extras_json": json.dumps(extras, ensure_ascii=False),
        "approx": approx,
        "tick_age_hours": extras["tick_age_hours"],
    }


def upsert_snapshot(conn: sqlite3.Connection, match_id: int, snap: dict) -> str:
    """INSERT or UPDATE snapshot for (match, macau, asian, channel, point)."""
    ex = conn.execute(
        """SELECT id, water_home, source FROM odds_snapshot
           WHERE match_id=? AND book=? AND market=? AND channel=? AND point=?""",
        (match_id, BOOK, MARKET, snap["channel"], snap["point"]),
    ).fetchone()
    if ex:
        # 只在本源或空水位时覆盖；不覆盖其它手工源非空水
        if ex["water_home"] is not None and ex["source"] not in (None, SOURCE, "5dollar_history"):
            return "skip_manual"
        conn.execute(
            """UPDATE odds_snapshot SET recorded_at=?, target_at=?, lag_hours=?, stale_gap=?,
               line=?, price_home=?, price_away=?, water_home=?, water_away=?,
               water_src=?, water_censored=?, source=?, extras_json=?
               WHERE id=?""",
            (
                snap["recorded_at"],
                snap["target_at"],
                snap["lag_hours"],
                snap["stale_gap"],
                snap["line"],
                snap.get("price_home"),
                snap.get("price_away"),
                snap["water_home"],
                snap["water_away"],
                snap["water_src"],
                snap["water_censored"],
                snap["source"],
                snap["extras_json"],
                ex["id"],
            ),
        )
        return "updated"
    conn.execute(
        """INSERT INTO odds_snapshot (
            match_id, book, market, channel, point, recorded_at, target_at, lag_hours,
            stale_gap, line, price_home, price_away, price_draw, price_over, price_under,
            water_home, water_away, water_over, water_under, water_src, water_censored,
            source, extras_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,?,?,NULL,NULL,?,?,?,?)""",
        (
            match_id,
            BOOK,
            MARKET,
            snap["channel"],
            snap["point"],
            snap["recorded_at"],
            snap["target_at"],
            snap["lag_hours"],
            snap["stale_gap"],
            snap["line"],
            snap.get("price_home"),
            snap.get("price_away"),
            snap["water_home"],
            snap["water_away"],
            snap["water_src"],
            snap["water_censored"],
            snap["source"],
            snap["extras_json"],
        ),
    )
    return "inserted"


def insert_segments(conn: sqlite3.Connection, match_id: int, ticks: list[dict], kickoff: datetime) -> int:
    """change-point 压缩赛前 tick → timeline_seg（幂等 UNIQUE）。"""
    if not ticks:
        return 0
    segs = []
    cur = None
    for t in ticks:
        ra = _parse_dt(t["recorded_at"])
        if ra >= kickoff:
            break
        key = (t.get("line"), t.get("home"), t.get("away"))
        if cur is None:
            cur = {"start": ra, "tick": t, "key": key, "n": 1}
        elif key == cur["key"]:
            cur["n"] += 1
        else:
            segs.append(cur)
            cur = {"start": ra, "tick": t, "key": key, "n": 1}
    if cur:
        segs.append(cur)
    n = 0
    for i, s in enumerate(segs):
        end = segs[i + 1]["start"] if i + 1 < len(segs) else kickoff
        t = s["tick"]
        try:
            conn.execute(
                """INSERT INTO odds_timeline_seg (
                    match_id, book, market, seg_start_at, seg_end_at, line,
                    price_home, price_away, price_draw, price_over, price_under,
                    water_home, water_away, water_over, water_under,
                    tick_count, compression, is_inplay, source, water_src, extras_json
                ) VALUES (?,?,?,?,?,?,?,?,NULL,NULL,NULL,?,?,NULL,NULL,?,'change_point',0,?,?,?)""",
                (
                    match_id,
                    BOOK,
                    MARKET,
                    _iso(s["start"]),
                    _iso(end),
                    t.get("line"),
                    t.get("home"),
                    t.get("away"),
                    _hk_water(t.get("home")),
                    _hk_water(t.get("away")),
                    s["n"],
                    SOURCE,
                    "actual",
                    json.dumps({"source": SOURCE}, ensure_ascii=False),
                ),
            )
            n += 1
        except sqlite3.IntegrityError:
            pass
    return n


def apply_odds_asian_fill(conn: sqlite3.Connection, match_id: int, snaps: dict[str, dict]) -> dict:
    """按原则写入 odds_asian：mid INSERT 若缺；open/close 只填 NULL 水位。"""
    stats = {
        "mid_inserted": 0,
        "mid_skipped_exists": 0,
        "water_filled": 0,
        "water_skipped_nonnull": 0,
        "phases": {},
    }
    for phase in ("open", "mid", "close"):
        snap = snaps.get(phase)
        if not snap:
            stats["phases"][phase] = "no_snap"
            continue
        row = conn.execute(
            "SELECT id, handicap, home_water, away_water, water_src, extras_json FROM odds_asian WHERE match_id=? AND book=? AND phase=?",
            (match_id, BOOK, phase),
        ).fetchone()
        extras = {
            "source": SOURCE,
            "from_channel": "rule",
            "from_point": phase,
            "snapshot_recorded_at": snap["recorded_at"],
            "snapshot_target_at": snap["target_at"],
            "approx": snap.get("approx", False),
            "tick_age_hours": snap.get("tick_age_hours"),
            "sign_convention": "positive_home_gives",
        }
        if row is None:
            if phase == "mid" or True:
                # 缺行则插入（含 open/close 若库中意外缺失）
                conn.execute(
                    """INSERT INTO odds_asian (
                        match_id, book, phase, handicap, home_water, away_water,
                        water_src, water_censored, extras_json
                    ) VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        match_id,
                        BOOK,
                        phase,
                        # 2026-10-08：tick/快照 line 为 API 记法(负=主让) → odds_asian 正数=主让
                        (None if snap["line"] is None
                         else (0.0 if float(snap["line"]) == 0 else -float(snap["line"]))),
                        snap["water_home"],
                        snap["water_away"],
                        "actual",
                        None,
                        json.dumps(extras, ensure_ascii=False),
                    ),
                )
                if phase == "mid":
                    stats["mid_inserted"] += 1
                stats["water_filled"] += 1
                stats["phases"][phase] = "inserted"
            continue

        # 已存在：不覆盖非空水位；可补 NULL
        hw, aw = row["home_water"], row["away_water"]
        if hw is not None and aw is not None:
            stats["water_skipped_nonnull"] += 1
            if phase == "mid":
                stats["mid_skipped_exists"] += 1
            stats["phases"][phase] = "skip_nonnull"
            continue
        # 识别旧手工：water_src 已有且非 actual/本源，且水位非空 → 上面已 skip
        # extras 合并
        old_ex = {}
        if row["extras_json"]:
            try:
                old_ex = json.loads(row["extras_json"])
            except Exception:
                old_ex = {}
        old_ex.update(extras)
        new_hw = hw if hw is not None else snap["water_home"]
        new_aw = aw if aw is not None else snap["water_away"]
        # handicap：若原有盘口保留（旧手工盘口优先）；仅当 NULL 才用 snap
        new_h = row["handicap"] if row["handicap"] is not None else snap["line"]
        conn.execute(
            """UPDATE odds_asian SET home_water=?, away_water=?, water_src=?,
               extras_json=? WHERE id=? AND (home_water IS NULL OR away_water IS NULL)""",
            (
                new_hw,
                new_aw,
                "actual",
                json.dumps(old_ex, ensure_ascii=False),
                row["id"],
            ),
        )
        stats["water_filled"] += 1
        stats["phases"][phase] = "water_filled"
        if phase == "mid":
            stats["mid_skipped_exists"] += 1  # mid 行已存在，只补水
    return stats


def index_blob(conn: sqlite3.Connection, match_id: int, fixture_id: str, path: Path) -> None:
    try:
        conn.execute(
            """INSERT INTO odds_fetch_blob (
                match_id, fixture_id, kind, book, market, path, sha256,
                bytes_raw, bytes_gz, fetched_at, http_status
            ) VALUES (?,?,?,?,?,?,?,?,NULL,?,?)""",
            (
                match_id,
                fixture_id,
                "history",
                BOOK,
                MARKET,
                str(path),
                _sha256_file(path),
                path.stat().st_size,
                datetime.now(timezone.utc).isoformat(),
                200,
            ),
        )
    except sqlite3.IntegrityError:
        pass


def cmd_import(args: argparse.Namespace) -> None:
    db = Path(args.db)
    assert_db_allowed(db, force_live=args.i_know_this_is_production)
    m = load_map()
    items = m["items"]
    if args.limit:
        items = items[: args.limit]

    conn = connect(db)
    if MIGRATION.exists():
        apply_sql_idempotent(conn, MIGRATION)
    conn.commit()

    summary = {
        "matches_attempted": 0,
        "matches_with_mid_water": 0,
        "matches_no_raw": 0,
        "matches_no_pre_ticks": 0,
        "mid_inserted": 0,
        "water_filled_phases": 0,
        "water_skipped_nonnull": 0,
        "seg_inserted": 0,
        "snap_actions": {},
        "approx_mid": 0,
        "details": [],
    }

    for it in items:
        mid = it["match_id"]
        fid = str(it["fixture_id"])
        path = RAW_DIR / f"{fid}_macauslot_asian.json"
        summary["matches_attempted"] += 1
        if not path.exists():
            summary["matches_no_raw"] += 1
            continue
        j = json.loads(path.read_text(encoding="utf-8"))
        ticks_all = ((j.get("data") or {}).get("ticks")) or []
        # 主客与竞彩相反时：线取反、主客水位对调（对齐 matches 主队）
        if it.get("swapped"):
            flipped = []
            for t in ticks_all:
                t2 = dict(t)
                if t2.get("line") is not None:
                    try:
                        t2["line"] = -float(t2["line"])
                    except (TypeError, ValueError):
                        pass
                t2["home"], t2["away"] = t2.get("away"), t2.get("home")
                flipped.append(t2)
            ticks_all = flipped
        ticks = pre_ticks(ticks_all)
        if not ticks:
            summary["matches_no_pre_ticks"] += 1
            continue

        # match kickoff / jingcai
        mrow = conn.execute(
            "SELECT id, jingcai_date, kickoff_hour, kickoff_at, jc_id FROM matches WHERE id=?",
            (mid,),
        ).fetchone()
        if not mrow:
            continue
        kickoff = _parse_dt(mrow["kickoff_at"]).astimezone(TZ_CN)
        targets = channel_targets(
            jingcai_date=mrow["jingcai_date"],
            kickoff_hour=int(mrow["kickoff_hour"]),
            kickoff=kickoff,
            has_jc_code=bool(mrow["jc_id"]),  # 0.3.18 exception_rule=jc_code_ge_2300
        )
        rule = targets.get("rule") or {}
        mid_at = rule.get("mid")
        close_at = rule.get("close")

        # open = first pre tick; close = last pre tick ≤ close_at (or last pre)
        open_tick = ticks[0]
        mid_tick = last_tick_at_or_before(ticks, mid_at) if mid_at else None
        close_tick = last_tick_at_or_before(ticks, close_at) if close_at else ticks[-1]
        if close_tick is None:
            close_tick = ticks[-1]

        snaps: dict[str, dict] = {}
        for point, tick, tgt in (
            ("open", open_tick, _parse_dt(open_tick["recorded_at"])),
            ("mid", mid_tick, mid_at),
            ("close", close_tick, close_at or _parse_dt(close_tick["recorded_at"])),
        ):
            if tick is None:
                continue
            s = snap_row(tick, target=tgt, point=point, channel="rule")
            if s:
                snaps[point] = s
                act = upsert_snapshot(conn, mid, s)
                summary["snap_actions"][act] = summary["snap_actions"].get(act, 0) + 1
                if point == "mid" and s.get("approx"):
                    summary["approx_mid"] += 1

        # also actual t8/t1
        act = targets.get("actual") or {}
        for point, tgt in (("t8", act.get("t8")), ("t1", act.get("t1"))):
            if tgt is None:
                continue
            tick = last_tick_at_or_before(ticks, tgt)
            s = snap_row(tick, target=tgt, point=point, channel="actual")
            if s:
                actn = upsert_snapshot(conn, mid, s)
                summary["snap_actions"][actn] = summary["snap_actions"].get(actn, 0) + 1

        nseg = insert_segments(conn, mid, ticks, kickoff)
        summary["seg_inserted"] += nseg
        index_blob(conn, mid, fid, path)

        fill = apply_odds_asian_fill(conn, mid, snaps)
        summary["mid_inserted"] += fill["mid_inserted"]
        summary["water_filled_phases"] += fill["water_filled"]
        summary["water_skipped_nonnull"] += fill["water_skipped_nonnull"]
        if snaps.get("mid") and snaps["mid"].get("water_home") is not None:
            summary["matches_with_mid_water"] += 1
        summary["details"].append(
            {
                "match_id": mid,
                "fixture_id": fid,
                "pre_ticks": len(ticks),
                "fill": fill["phases"],
                "mid_approx": (snaps.get("mid") or {}).get("approx"),
                "mid_age_h": (snaps.get("mid") or {}).get("tick_age_hours"),
            }
        )

    # 入库前正负号校验（validate_ah_sign）：不通过 → 回滚本次全部写入
    from validate_ah_sign import validate_conn
    _rep = validate_conn(conn, book=BOOK, phase="mid")
    summary["validate_ah_sign"] = {k: _rep[k] for k in ("status", "blocked")} | {"review": len(_rep["review"])}
    if _rep["status"] != "pass":
        conn.rollback()
        raise SystemExit(f"validate_ah_sign: {_rep['status']} blocked={_rep['blocked']} → 已回滚，未入库")
    conn.commit()

    # coverage report
    cov = conn.execute(
        """
        SELECT
          SUM(CASE WHEN phase='mid' THEN 1 ELSE 0 END) AS mid_rows,
          SUM(CASE WHEN phase='mid' AND home_water IS NOT NULL AND away_water IS NOT NULL THEN 1 ELSE 0 END) AS mid_with_water,
          SUM(CASE WHEN phase='open' AND home_water IS NOT NULL THEN 1 ELSE 0 END) AS open_w,
          SUM(CASE WHEN phase='close' AND home_water IS NOT NULL THEN 1 ELSE 0 END) AS close_w,
          SUM(CASE WHEN phase IN ('open','close') AND home_water IS NULL THEN 1 ELSE 0 END) AS oc_still_null
        FROM odds_asian WHERE book='macau'
          AND match_id IN (SELECT id FROM matches WHERE match_uid NOT LIKE 'probe:%')
        """
    ).fetchone()
    summary["coverage"] = dict(cov)
    # S1 readiness: need macau open+mid+close handicap AND upper water on all three
    # (strict: water on all three phases)
    s1 = conn.execute(
        """
        WITH mac AS (
          SELECT match_id,
            MAX(CASE WHEN phase='open' THEN handicap END) AS o_h,
            MAX(CASE WHEN phase='mid' THEN handicap END) AS m_h,
            MAX(CASE WHEN phase='close' THEN handicap END) AS c_h,
            MAX(CASE WHEN phase='open' THEN home_water END) AS o_hw,
            MAX(CASE WHEN phase='open' THEN away_water END) AS o_aw,
            MAX(CASE WHEN phase='mid' THEN home_water END) AS m_hw,
            MAX(CASE WHEN phase='mid' THEN away_water END) AS m_aw,
            MAX(CASE WHEN phase='close' THEN home_water END) AS c_hw,
            MAX(CASE WHEN phase='close' THEN away_water END) AS c_aw
          FROM odds_asian WHERE book='macau'
          GROUP BY match_id
        )
        SELECT
          COUNT(*) AS n,
          SUM(CASE WHEN o_h IS NOT NULL AND m_h IS NOT NULL AND c_h IS NOT NULL THEN 1 ELSE 0 END) AS has_three_lines,
          SUM(CASE WHEN o_hw IS NOT NULL AND o_aw IS NOT NULL
                    AND m_hw IS NOT NULL AND m_aw IS NOT NULL
                    AND c_hw IS NOT NULL AND c_aw IS NOT NULL THEN 1 ELSE 0 END) AS has_three_waters,
          SUM(CASE WHEN o_h IS NOT NULL AND m_h IS NOT NULL AND c_h IS NOT NULL
                    AND o_hw IS NOT NULL AND o_aw IS NOT NULL
                    AND m_hw IS NOT NULL AND m_aw IS NOT NULL
                    AND c_hw IS NOT NULL AND c_aw IS NOT NULL THEN 1 ELSE 0 END) AS s1_feature_ready
        FROM mac
        JOIN matches m ON m.id = mac.match_id
        WHERE m.match_uid NOT LIKE 'probe:%'
        """
    ).fetchone()
    summary["s1_readiness"] = dict(s1)
    summary["dual_write_env"] = os.environ.get("DUAL_WRITE_ODDS_ASIAN", "0")
    summary["db"] = str(db)

    out_path = LOG_DIR / "import_summary.json"
    # details can be large; keep in file
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    brief = {k: v for k, v in summary.items() if k != "details"}
    print(json.dumps(brief, ensure_ascii=False, indent=2))


def cmd_report(args: argparse.Namespace) -> None:
    db = Path(args.db)
    conn = connect(db)
    conn.row_factory = sqlite3.Row

    def gap(label, path):
        c = sqlite3.connect(path)
        c.row_factory = sqlite3.Row
        rows = list(
            c.execute(
                """
                SELECT phase,
                  COUNT(*) n,
                  SUM(CASE WHEN home_water IS NULL OR away_water IS NULL THEN 1 ELSE 0 END) water_null,
                  SUM(CASE WHEN home_water IS NOT NULL AND away_water IS NOT NULL THEN 1 ELSE 0 END) water_ok
                FROM odds_asian WHERE book='macau'
                  AND match_id IN (SELECT id FROM matches WHERE match_uid NOT LIKE 'probe:%')
                GROUP BY phase ORDER BY phase
                """
            )
        )
        mid_n = c.execute(
            "SELECT COUNT(*) FROM odds_asian WHERE book='macau' AND phase='mid' AND match_id IN (SELECT id FROM matches WHERE match_uid NOT LIKE 'probe:%')"
        ).fetchone()[0]
        print(f"\n== {label} ==")
        for r in rows:
            print(dict(r))
        print("macau mid rows (non-probe):", mid_n)
        c.close()

    gap("prod", ROOT / "data" / "app.db")
    gap("v2d1", ROOT / "data" / "v2d1" / "app.db")
    gap("v2d2", ROOT / "data" / "v2d2" / "app.db")
    gap("v2d3", ROOT / "data" / "v2d3" / "app.db")
    # map + raw coverage
    m = load_map()
    raw_ok = sum(
        1
        for it in m["items"]
        if (RAW_DIR / f"{it['fixture_id']}_macauslot_asian.json").exists()
    )
    print(f"\nmap fixtures={m['n']} unmapped={len(m.get('unmapped',[]))} raw_files={raw_ok}")
    print("dual_write", os.environ.get("DUAL_WRITE_ODDS_ASIAN", "0"))
    # fingerprints
    sys.path.insert(0, str(ROOT / "scripts"))
    from sync_odds_asian_from_snapshot import oa_fingerprint

    for label, path in [
        ("prod", ROOT / "data" / "app.db"),
        ("v2d3", ROOT / "data" / "v2d3" / "app.db"),
    ]:
        c = connect(path)
        print(label, oa_fingerprint(c, exclude_probe=True))


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_f = sub.add_parser("fetch")
    p_f.add_argument("--limit", type=int, default=0)
    p_f.add_argument("--force", action="store_true")
    p_f.set_defaults(func=cmd_fetch)

    p_i = sub.add_parser("import")
    p_i.add_argument("--db", type=Path, default=DEFAULT_DB)
    p_i.add_argument("--limit", type=int, default=0)
    p_i.add_argument("--i-know-this-is-production", action="store_true")
    p_i.set_defaults(func=cmd_import)

    p_r = sub.add_parser("report")
    p_r.add_argument("--db", type=Path, default=DEFAULT_DB)
    p_r.set_defaults(func=cmd_report)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
