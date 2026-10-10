#!/usr/bin/env python3
"""规则中盘/临盘漏采后的 hist as-of 补写（只写 v2d3 研究副本）。

口径（用户 2026-10-09 拍板）：
  过点后从 5DF /odds/history 取「目标钟点 T 之前最近一次赛前盘口/水位变化」
  （recorded_at ≤ T、minute IS NULL、非 suspended），写入 channel=rule point=mid|close。
  禁止用 T 之后的即时盘冒充规则中盘/临盘。

extras 必写：
  source=5df_hist_asof, asof_backfill=true, planned_target_at=T,
  observed_change_at=实际变化时刻, phase_assign_late=true
  features_ok / sim_ok：as-of≤T 准确则 true（可进模拟与调参；与 catchup 不同）
  recommend_live_ok：开赛后>3h 晚补为 false（禁即时推荐消息）；赛前/开赛后≤3h 为 true
  approx：中盘 age>120min / 临盘 age>60min；approx_threshold_min 写入实际阈值
  far_open：过远首开标记（与 approx 同触发时可标）；默认仍采纳，方案侧可降权

硬约束：只写 …/v2d3/app.db；DUAL_WRITE_ODDS_ASIAN 必须关；某书 T 前无开盘 → no_odds，不编造。

例：
  python3 scripts/asof_backfill_mid_rule.py --date 2026-10-09 \\
      --target-key 2026-10-09|五001|mid|rule --target-key 2026-10-09|五002|mid|rule
  python3 scripts/asof_backfill_mid_rule.py --date 2026-10-09 --missed-mid-rule
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
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

API_ROOT = Path(str(_MA_API_ROOT))
sys.path.insert(0, str(API_ROOT))
sys.path.insert(0, str(API_ROOT / "app"))
import shared_api_yield as _yield_mod  # noqa: E402

TZ = timezone(timedelta(hours=8))
BASE = "https://api.5dollarfootballapi.com/v1"
LIVE = Path(str(_ODDS_DATA_DIR / "5dollar/live"))
REPLICA_DB = _V2D3_DB
PROD_DB = _APP_DB.resolve()
ASOF_DIR = LIVE / "asof_backfill"
RAW_DIR = ASOF_DIR / "raw"
LOG_DIR = ASOF_DIR / "logs"
D_PLAN = LIVE / "plan"
D_STATE = LIVE / "state"
D_BAK = LIVE / "backups"
D_LOG = LIVE / "logs"

# hist bookmaker slug → 库内 book 码
HIST_BOOKS = (
    ("macauslot", "macau"),
    ("crown", "crown"),
    ("williamhill", "william"),
    ("pinnacle", "pinnacle"),
)
MARKET = "asian"
SOURCE = "5df_hist_asof"
WATER_MIN, WATER_MAX = 0.50, 1.50

# approx 分阶段（2026-10-09 拍板）：中盘 120min / 临盘 60min
APPROX_THRESHOLD_MIN = {"mid": 120, "close": 60, "t8": 120, "t1": 60}
DEFAULT_APPROX_MIN = 120  # 未知 point 回退中盘口径
# 开赛后超过此时长晚补 → recommend_live_ok=false（仍可 sim/features）
RECOMMEND_LIVE_OFF_HOURS_AFTER_KICKOFF = 3.0

_YIELD = _yield_mod.Gate("asof_backfill_mid_rule")


def now_cn() -> datetime:
    return datetime.now(TZ)


def iso(dt: datetime) -> str:
    return dt.astimezone(TZ).isoformat(timespec="seconds")


def parse(s: str | None) -> datetime | None:
    if not s:
        return None
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def hk(price) -> float | None:
    if price is None:
        return None
    return round(float(price) - 1.0, 4)


def jload(p: Path, default=None):
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding="utf-8"))


def jdump(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def assert_safe_db(db: Path) -> Path:
    db = db.resolve()
    if db == PROD_DB or db.name != "app.db" or db.parent.name != "v2d3":
        raise SystemExit(f"拒绝写入 {db}：只允许 v2d3 研究副本（…/v2d3/app.db）")
    if os.environ.get("DUAL_WRITE_ODDS_ASIAN", "0").strip().lower() in ("1", "true", "yes", "on"):
        raise SystemExit("DUAL_WRITE_ODDS_ASIAN 被打开了；本脚本要求保持关闭")
    return db


def backup_replica(db: Path) -> Path:
    D_BAK.mkdir(parents=True, exist_ok=True)
    out = D_BAK / f"v2d3_app_{now_cn():%Y%m%dT%H%M%S}_asof.db"
    shutil.copy2(db, out)
    return out


class HistClient:
    def __init__(self):
        self.key = os.environ.get("FIVEDOLLAR_FOOTBALL_API_KEY", "")
        if not self.key or self.key.startswith("YOUR_"):
            raise SystemExit("FIVEDOLLAR_FOOTBALL_API_KEY 未设置")
        self.calls = 0
        self.remaining = None
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.log_path = LOG_DIR / "call_log.tsv"
        if not self.log_path.exists():
            self.log_path.write_text(
                "ts\tname\tpath\thttp\telapsed_s\tlimit\tremaining\n", encoding="utf-8"
            )

    def get(self, name: str, path: str, params: dict | None = None):
        for attempt in range(4):
            _YIELD.before_request()
            url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
            req = urllib.request.Request(
                url,
                headers={"Authorization": "Bearer " + self.key, "Accept": "application/json"},
            )
            t0 = time.time()
            try:
                r = urllib.request.urlopen(req, timeout=60)
                code, body, h = r.status, r.read(), r.headers
            except urllib.error.HTTPError as e:
                code, body, h = e.code, e.read(), e.headers
            except Exception as e:
                code, body, h = -1, str(e).encode(), {}
            if h:
                try:
                    _YIELD.after_response(h)
                except Exception:
                    pass
            self.calls += 1
            rem = h.get("X-RateLimit-Remaining") if h else None
            lim = h.get("X-RateLimit-Limit") if h else None
            self.remaining = int(rem) if rem not in (None, "") else None
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(
                    f"{iso(now_cn())}\t{name}\t{path}\t{code}\t{time.time()-t0:.2f}\t{lim}\t{rem}\n"
                )
            if code == 429 or code >= 500 or code == -1:
                ra = float((h.get("Retry-After") if h else None) or 10)
                time.sleep(ra + 1)
                continue
            try:
                data = json.loads(body)
            except Exception:
                data = {"_raw": body[:500].decode("utf-8", "ignore")}
            return code, data
        return code, {"_error": "retries_exhausted"}


def load_plan(D: str) -> dict | None:
    return jload(D_PLAN / f"{D}.json")


def state_path(D: str) -> Path:
    return D_STATE / f"captured_{D}.json"


def _import_full_history(conn, match_id: int, book: str, ticks: list[dict], kick, orient: str,
                         raw_path: str | None) -> dict:
    """Import the whole fetched history into odds_timeline_seg (see api/scripts/import_asof_history_segments.py).
    A failure never stops the rescue itself; it is only reported."""
    try:
        sys.path.insert(0, str(API_ROOT / "scripts"))
        from import_asof_history_segments import import_ticks
        return import_ticks(conn, match_id, book, "asian", ticks, kick, orient, raw_path)
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


def fetch_hist(cl: HistClient, fid: int, slug: str, use_cache: bool = True) -> tuple[str, list[dict], dict]:
    """返回 (status, ticks, meta)。status: ok|empty|http_N|error"""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    cache = RAW_DIR / f"{fid}_{slug}_{MARKET}.json"
    if use_cache and cache.exists():
        data = json.loads(cache.read_text(encoding="utf-8"))
        ticks = ((data.get("data") or {}).get("ticks") or []) if isinstance(data, dict) else []
        return ("ok" if ticks else "empty"), ticks, {"cache": True, "path": str(cache)}

    all_ticks: list[dict] = []
    page = 1
    last_data = None
    while True:
        code, data = cl.get(
            f"hist_{fid}_{slug}_p{page}",
            f"/fixtures/{fid}/odds/history",
            {"bookmaker": slug, "market": MARKET, "per_page": 500, "page": page},
        )
        last_data = data
        if code != 200 or not (isinstance(data, dict) and data.get("success")):
            err = RAW_DIR / f"{fid}_{slug}_{MARKET}.err.json"
            err.write_text(
                json.dumps({"http": code, "body": data}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return f"http_{code}", [], {"path": str(err)}
        ticks = (data.get("data") or {}).get("ticks") or []
        all_ticks.extend(ticks)
        if not (data.get("pagination") or {}).get("has_more"):
            break
        page += 1
        if page > 20:
            break

    # rewrite cache with merged ticks
    merged = {
        "success": True,
        "data": {
            "fixture_id": fid,
            "bookmaker": slug,
            "market": MARKET,
            "ticks": all_ticks,
        },
        "pagination": {"merged_pages": page, "n_ticks": len(all_ticks)},
        "_meta": {"fetched_at": iso(now_cn()), "source": SOURCE},
    }
    cache.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    return ("ok" if all_ticks else "empty"), all_ticks, {"cache": False, "path": str(cache), "pages": page}


def prematch_asof(ticks: list[dict], target: datetime, kickoff: datetime) -> dict | None:
    """取 recorded_at ≤ T 的最近一次赛前有效变化。"""
    best = None
    for t in ticks:
        if t.get("minute") is not None:
            continue
        if t.get("suspended"):
            continue
        if t.get("home") is None and t.get("away") is None and t.get("line") is None:
            continue
        ra = parse(t["recorded_at"])
        if ra is None:
            continue
        if ra >= kickoff:
            continue
        if ra <= target:
            if best is None or ra > parse(best["recorded_at"]):
                best = t
    return best


def team_ids(conn, name: str) -> set:
    if not name:
        return set()
    rows = conn.execute(
        "SELECT id FROM teams WHERE name=? OR name_zh=? OR name_en=?",
        (name, name, name),
    ).fetchall()
    # teams table may not exist / columns vary
    return {r[0] for r in rows} if rows else set()


def orientation(conn, m: sqlite3.Row, home5: str, away5: str) -> str:
    if (m["home_team"], m["away_team"]) == (home5, away5):
        return "same"
    if (m["home_team"], m["away_team"]) == (away5, home5):
        return "swapped"
    try:
        mh = {m["home_team_id"]} if m["home_team_id"] else team_ids(conn, m["home_team"])
        ma = {m["away_team_id"]} if m["away_team_id"] else team_ids(conn, m["away_team"])
        h5, a5 = team_ids(conn, home5), team_ids(conn, away5)
        if (mh & h5) or (ma & a5):
            return "same"
        if (mh & a5) or (ma & h5):
            return "swapped"
    except Exception:
        pass
    return "unverified"


def resolve_targets(D: str, keys: list[str] | None, missed_mid_rule: bool) -> list[dict]:
    plan = load_plan(D)
    if not plan:
        raise SystemExit(f"无 plan：{D_PLAN / f'{D}.json'}")
    st = jload(state_path(D), {}) or {}
    out = []
    for t in plan["targets"]:
        if t.get("channel") != "rule" or t.get("point") not in ("mid", "close"):
            continue
        if keys and t["target_key"] not in keys:
            continue
        if missed_mid_rule and not keys:
            if t.get("point") != "mid":
                continue
            if (st.get(t["target_key"]) or {}).get("status") != "missed":
                continue
        if keys or missed_mid_rule:
            match = next(m for m in plan["matches"] if m["match_uid"] == t["match_uid"])
            out.append({**t, "_match": match})
    if keys:
        found = {t["target_key"] for t in out}
        missing = [k for k in keys if k not in found]
        if missing:
            raise SystemExit(f"plan 中找不到目标：{missing}")
    return out


def upsert_snapshot(conn, vals: dict) -> str:
    old = conn.execute(
        "SELECT * FROM odds_snapshot WHERE match_id=? AND book=? AND market=? AND channel=? AND point=?",
        (vals["match_id"], vals["book"], vals["market"], vals["channel"], vals["point"]),
    ).fetchone()
    if old and old["source"] not in (None, SOURCE, "5df_live"):
        # 保留人工/其它来源
        return "conflict_other_source"
    if old and old["source"] == "5df_live":
        # 已有按时自采 → 不覆盖
        return "keep_existing_live"
    cols = list(vals)
    conn.execute(
        f"INSERT INTO odds_snapshot ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
        "ON CONFLICT(match_id, book, market, channel, point) DO UPDATE SET "
        + ",".join(
            f"{c}=excluded.{c}"
            for c in cols
            if c not in ("match_id", "book", "market", "channel", "point")
        ),
        [vals[c] for c in cols],
    )
    return "upserted" if old else "inserted"


def approx_threshold_for(point: str | None) -> float:
    """中盘 120min、临盘 60min；t8≈mid、t1≈close。"""
    return float(APPROX_THRESHOLD_MIN.get(point or "", DEFAULT_APPROX_MIN))


def recommend_live_ok_default(kickoff_at: datetime | None, *, now: datetime | None = None) -> bool:
    """开赛后 >3h 晚补：禁即时推荐；仍允许 sim/features（准确 as-of 时）。"""
    if kickoff_at is None:
        return True
    n = now or now_cn()
    return n < kickoff_at + timedelta(hours=RECOMMEND_LIVE_OFF_HOURS_AFTER_KICKOFF)


def run(D: str, keys: list[str] | None, missed_mid_rule: bool, db: Path, dry_run: bool,
        refresh_hist: bool, recommend_live_ok: bool | None = None) -> dict:
    db = assert_safe_db(db)
    targets = resolve_targets(D, keys, missed_mid_rule)
    if not targets:
        return {"ok": False, "error": "no_targets", "jingcai_date": D}

    cl = HistClient()
    report = {
        "jingcai_date": D,
        "started_at": iso(now_cn()),
        "dry_run": dry_run,
        "db": str(db),
        "dual_write": "OFF",
        "source": SOURCE,
        "targets": [],
        "backup": None,
        "api_calls": 0,
    }

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True) if dry_run else sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row

    # backup once before first write
    need_backup = not dry_run
    try:
        for t in targets:
            m5 = t["_match"]
            T = parse(t["target_at"])
            kick = parse(t["kickoff_at"])
            thr_min = approx_threshold_for(t.get("point"))
            if recommend_live_ok is None:
                reco_ok = recommend_live_ok_default(kick)
            else:
                reco_ok = bool(recommend_live_ok)
            trep = {
                "target_key": t["target_key"],
                "match_uid": t["match_uid"],
                "fixture_id": t["fixture_id"],
                "planned_target_at": t["target_at"],
                "kickoff_at": t["kickoff_at"],
                "point": t["point"],
                "channel": t["channel"],
                "approx_threshold_min": thr_min,
                "recommend_live_ok": reco_ok,
                "books": {},
            }
            mid_row = conn.execute(
                "SELECT * FROM matches WHERE match_uid=?", (t["match_uid"],)
            ).fetchone()
            if not mid_row:
                trep["error"] = "no_replica_match"
                report["targets"].append(trep)
                continue
            orient = orientation(conn, mid_row, m5["home"], m5["away"])
            trep["orientation"] = orient
            trep["replica_match_id"] = mid_row["id"]

            for slug, book in HIST_BOOKS:
                bstat = {"slug": slug, "book": book}
                st, ticks, meta = fetch_hist(cl, t["fixture_id"], slug, use_cache=not refresh_hist)
                bstat["hist_status"] = st
                bstat["hist_meta"] = meta
                bstat["n_ticks"] = len(ticks)
                if st.startswith("http_"):
                    bstat["result"] = st
                    trep["books"][book] = bstat
                    continue
                if not ticks:
                    bstat["result"] = "no_odds"
                    trep["books"][book] = bstat
                    continue
                tick = prematch_asof(ticks, T, kick)
                if tick is None:
                    bstat["result"] = "no_odds"
                    bstat["note"] = "T前无赛前有效 tick"
                    trep["books"][book] = bstat
                    continue

                line = tick.get("line")
                price_home, price_away = tick.get("home"), tick.get("away")
                water_home, water_away = hk(price_home), hk(price_away)
                if orient == "swapped":
                    if line is not None:
                        line = -float(line)
                    price_home, price_away = price_away, price_home
                    water_home, water_away = water_away, water_home

                qa = []
                for wname, wv in (("water_home", water_home), ("water_away", water_away)):
                    if wv is not None and not (WATER_MIN <= wv <= WATER_MAX):
                        qa.append(f"{wname}_out_of_range")

                observed = parse(tick["recorded_at"])
                age_min = round((T - observed).total_seconds() / 60.0, 2)
                approx = (T - observed).total_seconds() > thr_min * 60
                # 过远首开：可标记、默认仍采纳；方案侧可降权（不作硬拒）
                far_open = bool(approx)

                ex = {
                    "source": SOURCE,
                    "asof_backfill": True,
                    "planned_target_at": t["target_at"],
                    "observed_change_at": iso(observed),
                    "phase_assign_late": True,
                    # as-of≤T 准确 → 可进模拟/调参；仅数据不可信时才关 features
                    "features_ok": True,
                    "sim_ok": True,
                    "recommend_live_ok": reco_ok,
                    "phase_pending": False,
                    "phase": t["phase"],
                    "phase_variant": t["phase_variant"],
                    "channel": t["channel"],
                    "point": t["point"],
                    "phase_assigned": [f"{t['phase']}|{t['phase_variant']}"],
                    "capture": "asof_hist",
                    "odds_source": "hist",
                    "api_field": "history_tick",
                    "fixture_id": t["fixture_id"],
                    "book_slug": slug,
                    "orientation": orient,
                    "tick_age_rule": "hist_recorded_at",
                    "tick_age_min": age_min,
                    "tick_age_hours": round(age_min / 60.0, 4),
                    "approx": approx,
                    "approx_threshold_min": thr_min,
                    "far_open": far_open,
                    "water_source": "actual",
                    "water_scale": "hk_water_from_decimal",
                    "raw_path": meta.get("path"),
                    "qa_flags": qa,
                    "match_uid": t["match_uid"],
                    "kickoff_at_5df": t["kickoff_at"],
                    "kickoff_source": "5df",
                    "catchup": False,
                    "note": "漏采后 hist as-of≤T 补写；禁止 T 后即时盘；过远首开默认采纳可降权",
                }
                # sha of tick for trace
                tick_bytes = json.dumps(tick, ensure_ascii=False, sort_keys=True).encode()
                ex["tick_sha256"] = hashlib.sha256(tick_bytes).hexdigest()

                vals = {
                    "match_id": mid_row["id"],
                    "book": book,
                    "market": "asian",
                    "channel": t["channel"],
                    "point": t["point"],
                    "recorded_at": iso(observed),
                    "target_at": t["target_at"],
                    "lag_hours": round((observed - T).total_seconds() / 3600.0, 4),
                    "stale_gap": 0,
                    "line": float(line) if line is not None else None,
                    "price_home": float(price_home) if price_home is not None else None,
                    "price_away": float(price_away) if price_away is not None else None,
                    "price_draw": None,
                    "price_over": None,
                    "price_under": None,
                    "water_home": water_home,
                    "water_away": water_away,
                    "water_over": None,
                    "water_under": None,
                    "water_src": "actual",
                    "water_censored": None,
                    "source": SOURCE,
                    "extras_json": json.dumps(ex, ensure_ascii=False, sort_keys=True),
                }
                bstat["observed_change_at"] = iso(observed)
                bstat["tick_age_min"] = age_min
                bstat["approx"] = approx
                bstat["approx_threshold_min"] = thr_min
                bstat["far_open"] = far_open
                bstat["recommend_live_ok"] = reco_ok
                bstat["line"] = vals["line"]
                bstat["water_home"] = water_home
                bstat["water_away"] = water_away
                bstat["qa_flags"] = qa

                if dry_run:
                    bstat["result"] = "would_upsert"
                else:
                    if need_backup:
                        report["backup"] = str(backup_replica(db))
                        need_backup = False
                    bstat["result"] = upsert_snapshot(conn, vals)
                    # Since 2026-10-10: the full history fetched for this rescue also goes into odds_timeline_seg,
                    # so that the first record of the match is not mistaken for a late (truncated) opening.
                    bstat["timeline_import"] = _import_full_history(conn, mid_row["id"], book, ticks, kick, orient,
                                                                    meta.get("path"))
                trep["books"][book] = bstat

            # 目标级汇总
            results = [b["result"] for b in trep["books"].values()]
            if any(r in ("inserted", "upserted", "would_upsert") for r in results):
                trep["target_status"] = "asof_backfilled"
            elif all(r == "no_odds" for r in results):
                trep["target_status"] = "no_odds"
            else:
                trep["target_status"] = "partial_or_error"
            report["targets"].append(trep)

        if not dry_run:
            conn.commit()
            # 更新 captured state
            st = jload(state_path(D), {}) or {}
            for trep in report["targets"]:
                tk = trep["target_key"]
                books_ok = {
                    b: {
                        "result": info["result"],
                        "observed_change_at": info.get("observed_change_at"),
                        "line": info.get("line"),
                        "water_home": info.get("water_home"),
                        "water_away": info.get("water_away"),
                    }
                    for b, info in trep.get("books", {}).items()
                }
                st[tk] = {
                    "status": trep.get("target_status", "asof_backfilled"),
                    "target_at": trep["planned_target_at"],
                    "noted_at": iso(now_cn()),
                    "asof_backfill": True,
                    "source": SOURCE,
                    "books": books_ok,
                    "prev_status": (jload(state_path(D), {}) or {}).get(tk, {}).get("status"),
                }
            jdump(state_path(D), st)
    except Exception:
        if not dry_run:
            conn.rollback()
        raise
    finally:
        conn.close()

    report["finished_at"] = iso(now_cn())
    report["api_calls"] = cl.calls
    report["remaining"] = cl.remaining
    # counts
    ok_n = sum(
        1
        for t in report["targets"]
        for b in t.get("books", {}).values()
        if b.get("result") in ("inserted", "upserted", "would_upsert")
    )
    no_n = sum(
        1
        for t in report["targets"]
        for b in t.get("books", {}).values()
        if b.get("result") == "no_odds"
    )
    report["summary"] = {
        "targets": len(report["targets"]),
        "book_ok": ok_n,
        "book_no_odds": no_n,
    }
    out_path = LOG_DIR / f"asof_backfill_{D}_{now_cn():%Y%m%dT%H%M%S}.json"
    jdump(out_path, report)
    report["report_path"] = str(out_path)
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", required=True, help="竞彩日 YYYY-MM-DD")
    ap.add_argument("--target-key", action="append", default=[], help="可重复；不传则配合 --missed-mid-rule")
    ap.add_argument("--missed-mid-rule", action="store_true", help="补当日 state=missed 的 mid|rule")
    ap.add_argument("--db", default=str(REPLICA_DB))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--refresh-hist", action="store_true", help="忽略 raw 缓存重拉 history")
    a = ap.parse_args(argv)
    if not a.target_key and not a.missed_mid_rule:
        raise SystemExit("需要 --target-key 或 --missed-mid-rule")
    rep = run(
        a.date,
        a.target_key or None,
        a.missed_mid_rule,
        Path(a.db),
        a.dry_run,
        a.refresh_hist,
    )
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    return 0 if rep.get("targets") else 1


if __name__ == "__main__":
    sys.exit(main())
