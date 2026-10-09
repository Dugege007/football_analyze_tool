#!/usr/bin/env python3
"""D1：从 probe-2026-10-06/raw 导入 odds_timeline_seg + odds_snapshot + odds_fetch_blob。

默认写入副本库 data/v2d1/app.db（可 --db 覆盖）。
- 不写现网 odds_asian；探针场次标 extras.source=probe_d1。
- 幂等：UNIQUE 冲突跳过；可重复执行。
- 优先 FRA–BEL，其次 KOR–UZB。

查询任意时刻：
  .venv/bin/python scripts/import_odds_timeline_probe.py --query \\
      --match-uid probe:1263863300 --book macau --market asian \\
      --at '2026-10-05T18:00:00+08:00' --db data/v2d1/app.db
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
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.collection_schedule import (  # noqa: E402
    TZ_CN,
    channel_targets,
    is_early_kickoff_band,
    jingcai_date_from_code,
)
from app.db import apply_sql_idempotent, connect  # noqa: E402

PROBE_RAW = Path(str(_ODDS_DATA_DIR / "5dollar/probe-2026-10-06/raw"))
MIGRATION = Path(str(_REPO_ROOT / "docs/schema/v2_0_odds_timeline.sql"))
DEFAULT_DB = ROOT / "data" / "v2d1" / "app.db"

BOOK_MAP = {
    "macauslot": "macau",
    "crown": "crown",
    "williamhill": "william",
    "pinnacle": "pinnacle",
    "bet365": "bet365",
    "chinasportslottery": "jc",
}
MARKET_MAP = {
    "asian": "asian",
    "goalline": "ou",
    "1x2": "euro_1x2",
}

# fixture_id → 导入清单（hist 优先；odds 用于 blob + open/close 辅助）
PROBE_MATCHES = [
    {
        "fixture_id": "1263863300",
        "label": "FRA-BEL",
        "csl_file": "04_csl_past.json",
        "hist": [
            ("09_hist_FRA_BEL_macau_asian.json", "macauslot", "asian"),
            ("10_hist_FRA_BEL_crown_asian.json", "crown", "asian"),
            ("11_hist_FRA_BEL_bet365_asian.json", "bet365", "asian"),
            ("12_hist_FRA_BEL_macau_goalline.json", "macauslot", "goalline"),
            ("13_hist_FRA_BEL_csl_1x2.json", "chinasportslottery", "1x2"),
        ],
        "odds_file": "07_odds_fin_FRA_BEL.json",
        # README 验收：赛前 tick 数
        "expect_pre": {
            ("macau", "asian"): 8,
            ("crown", "asian"): 69,
            ("bet365", "asian"): 21,  # README 写 22，实测去重后 21
            ("macau", "ou"): 6,
            ("jc", "euro_1x2"): 5,
        },
    },
    {
        "fixture_id": "515799156",
        "label": "KOR-UZB",
        "csl_file": "03_csl_upcoming.json",
        "hist": [
            ("14_hist_KOR_UZB_macau_asian.json", "macauslot", "asian"),
        ],
        "odds_file": "05_odds_up_KOR_UZB.json",
        "expect_pre": {
            ("macau", "asian"): 15,
        },
    },
]


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse_dt(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _hk_water(price: float | None) -> float | None:
    if price is None:
        return None
    return round(float(price) - 1.0, 6)


def _tick_key(t: dict) -> tuple:
    return (
        t.get("line"),
        t.get("home"),
        t.get("away"),
        t.get("draw"),
        t.get("over"),
        t.get("under"),
    )


def load_csl_fixture(csl_file: str, fixture_id: str) -> dict:
    items = json.loads((PROBE_RAW / csl_file).read_text(encoding="utf-8"))["data"]
    for it in items:
        if str(it["id"]) == str(fixture_id):
            return it
    raise KeyError(f"fixture {fixture_id} not in {csl_file}")


def ensure_probe_match(conn: sqlite3.Connection, fx: dict, fixture_id: str) -> int:
    """插入或复用探针场；返回 match.id。不改正式竞彩语义。"""
    uid = f"probe:{fixture_id}"
    row = conn.execute(
        "SELECT id FROM matches WHERE match_uid = ?", (uid,)
    ).fetchone()
    kick_utc = _parse_dt(fx["kickoff_utc"]).astimezone(TZ_CN)
    # 竞彩编号 → 日
    lot = (fx.get("lottery") or {}).get("jingcailottery") or {}
    jc_id = lot.get("number")  # 周一002 / 周二001
    # jingcai_date：早场特殊带 [00:00,11:30]（有分钟用分钟；仅整点 0–11）挂前一日
    # 11:31–11:59 需分钟才能排除出早场带
    h = kick_utc.hour
    # 0.3.18：竞彩日归属以编号为准（编号星期 → 最近同星期日），不拿开赛时间窗反推；无编号才回退时间窗
    jingcai_date = jingcai_date_from_code(jc_id, kick_utc)
    if jingcai_date is None:
        if is_early_kickoff_band(h, kick_utc.minute):
            jingcai_date = (kick_utc - timedelta(days=1)).strftime("%Y-%m-%d")
        else:
            jingcai_date = kick_utc.strftime("%Y-%m-%d")
    weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][
        datetime.strptime(jingcai_date, "%Y-%m-%d").weekday()
    ]
    home = fx["teams"]["home"]["name"]
    away = fx["teams"]["away"]["name"]
    extras = {
        "source": "probe_d1",
        "fixture_id": str(fixture_id),
        "kickoff_utc": fx["kickoff_utc"],
        "label": f"{home}-{away}",
        # 影子台账预留（本包不落影子方案）
        "shadow_reserved": {"hits": None, "coverage": None, "n_eligible": None},
    }
    if row:
        mid = int(row["id"])
        conn.execute(
            """UPDATE matches SET home_team=?, away_team=?, jingcai_date=?, weekday=?,
               kickoff_hour=?, jc_id=?, kickoff_at=?, competition_name=?, updated_at=datetime('now')
               WHERE id=?""",
            (
                home,
                away,
                jingcai_date,
                weekday,
                h,
                jc_id,
                _iso(kick_utc),
                (fx.get("league") or {}).get("name"),
                mid,
            ),
        )
        conn.execute(
            """INSERT INTO match_meta (match_id, extras_json) VALUES (?,?)
               ON CONFLICT(match_id) DO UPDATE SET extras_json=excluded.extras_json""",
            (mid, json.dumps(extras, ensure_ascii=False)),
        )
        return mid

    cur = conn.execute(
        """INSERT INTO matches (
            match_uid, scope, jingcai_date, weekday, kickoff_hour, jc_id, jc_no,
            competition_name, home_team, away_team, kickoff_at, kickoff_minute_known
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,1)""",
        (
            uid,
            "extra",
            jingcai_date,
            weekday,
            h,
            jc_id,
            None,
            (fx.get("league") or {}).get("name"),
            home,
            away,
            _iso(kick_utc),
        ),
    )
    mid = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO match_meta (match_id, extras_json) VALUES (?,?)",
        (mid, json.dumps(extras, ensure_ascii=False)),
    )
    return mid


def pre_match_ticks(ticks: list[dict], kickoff: datetime) -> list[dict]:
    out = []
    for t in ticks:
        if t.get("suspended"):
            continue
        if t.get("minute") is not None:
            continue
        ra = _parse_dt(t["recorded_at"])
        if ra >= kickoff:
            continue
        if t.get("home") is None and t.get("over") is None and t.get("draw") is None:
            continue
        out.append(t)
    out.sort(key=lambda x: x["recorded_at"])
    return out


def ticks_to_segments(
    ticks: list[dict], kickoff: datetime, market: str
) -> list[dict]:
    """变化点游程：连续同 line+价格合并。"""
    if not ticks:
        return []
    segs: list[dict] = []
    cur = None
    for t in ticks:
        key = _tick_key(t)
        ra = _parse_dt(t["recorded_at"])
        if cur is None:
            cur = {"start": ra, "end": ra, "tick": t, "key": key, "n": 1}
            continue
        if key == cur["key"]:
            cur["end"] = ra
            cur["n"] += 1
        else:
            segs.append(cur)
            cur = {"start": ra, "end": ra, "tick": t, "key": key, "n": 1}
    if cur:
        segs.append(cur)
    # seg_end_at = 下一段 start；最后一段到 kickoff
    out = []
    for i, s in enumerate(segs):
        end = segs[i + 1]["start"] if i + 1 < len(segs) else kickoff
        t = s["tick"]
        row = {
            "seg_start_at": _iso(s["start"]),
            "seg_end_at": _iso(end),
            "line": t.get("line"),
            "price_home": t.get("home"),
            "price_away": t.get("away"),
            "price_draw": t.get("draw"),
            "price_over": t.get("over"),
            "price_under": t.get("under"),
            "tick_count": s["n"],
        }
        if market == "asian":
            row["water_home"] = _hk_water(t.get("home"))
            row["water_away"] = _hk_water(t.get("away"))
        elif market == "ou":
            row["water_over"] = _hk_water(t.get("over"))
            row["water_under"] = _hk_water(t.get("under"))
        out.append(row)
    return out


def insert_segments(
    conn: sqlite3.Connection,
    match_id: int,
    book: str,
    market: str,
    segs: list[dict],
) -> int:
    n = 0
    for s in segs:
        try:
            conn.execute(
                """INSERT INTO odds_timeline_seg (
                    match_id, book, market, seg_start_at, seg_end_at, line,
                    price_home, price_away, price_draw, price_over, price_under,
                    water_home, water_away, water_over, water_under,
                    tick_count, compression, is_inplay, source, water_src
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'change_point', 0, '5dollar_history', ?)""",
                (
                    match_id,
                    book,
                    market,
                    s["seg_start_at"],
                    s["seg_end_at"],
                    s.get("line"),
                    s.get("price_home"),
                    s.get("price_away"),
                    s.get("price_draw"),
                    s.get("price_over"),
                    s.get("price_under"),
                    s.get("water_home"),
                    s.get("water_away"),
                    s.get("water_over"),
                    s.get("water_under"),
                    s["tick_count"],
                    "actual" if market in ("asian", "ou") else None,
                ),
            )
            n += 1
        except sqlite3.IntegrityError:
            pass
    return n


def last_tick_at_or_before(ticks: list[dict], target: datetime) -> dict | None:
    best = None
    for t in ticks:
        ra = _parse_dt(t["recorded_at"])
        if ra <= target:
            best = t
        else:
            break
    return best


def snapshot_from_tick(
    tick: dict | None,
    *,
    market: str,
    target: datetime | None,
    point: str,
    channel: str,
    source: str,
) -> dict | None:
    if tick is None and target is None:
        return None
    if tick is None:
        return {
            "channel": channel,
            "point": point,
            "recorded_at": None,
            "target_at": _iso(target) if target else None,
            "lag_hours": None,
            "stale_gap": 1,
            "line": None,
            "source": source,
        }
    ra = _parse_dt(tick["recorded_at"])
    lag = None
    stale = 0
    if target is not None:
        lag = (target - ra).total_seconds() / 3600.0
        # 粗标：滞后 > 2h 视为 stale（探针稀疏盘）
        if lag > 2.0:
            stale = 1
    row = {
        "channel": channel,
        "point": point,
        "recorded_at": _iso(ra),
        "target_at": _iso(target) if target else _iso(ra),
        "lag_hours": round(lag, 4) if lag is not None else 0.0,
        "stale_gap": stale,
        "line": tick.get("line"),
        "price_home": tick.get("home"),
        "price_away": tick.get("away"),
        "price_draw": tick.get("draw"),
        "price_over": tick.get("over"),
        "price_under": tick.get("under"),
        "water_src": "actual" if market in ("asian", "ou") else None,
        "water_censored": None,
        "source": source,
    }
    if market == "asian":
        row["water_home"] = _hk_water(tick.get("home"))
        row["water_away"] = _hk_water(tick.get("away"))
    elif market == "ou":
        row["water_over"] = _hk_water(tick.get("over"))
        row["water_under"] = _hk_water(tick.get("under"))
    return row


def upsert_snapshot(conn: sqlite3.Connection, match_id: int, book: str, market: str, snap: dict) -> bool:
    try:
        conn.execute(
            """INSERT INTO odds_snapshot (
                match_id, book, market, channel, point, recorded_at, target_at, lag_hours,
                stale_gap, line, price_home, price_away, price_draw, price_over, price_under,
                water_home, water_away, water_over, water_under, water_src, water_censored, source
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                match_id,
                book,
                market,
                snap["channel"],
                snap["point"],
                snap.get("recorded_at"),
                snap.get("target_at"),
                snap.get("lag_hours"),
                snap.get("stale_gap", 0),
                snap.get("line"),
                snap.get("price_home"),
                snap.get("price_away"),
                snap.get("price_draw"),
                snap.get("price_over"),
                snap.get("price_under"),
                snap.get("water_home"),
                snap.get("water_away"),
                snap.get("water_over"),
                snap.get("water_under"),
                snap.get("water_src"),
                snap.get("water_censored"),
                snap.get("source"),
            ),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def index_blob(
    conn: sqlite3.Connection,
    *,
    match_id: int | None,
    fixture_id: str,
    kind: str,
    book: str | None,
    market: str | None,
    path: Path,
    fetched_at: str | None = None,
    http_status: int = 200,
) -> bool:
    sha = _sha256_file(path)
    try:
        conn.execute(
            """INSERT INTO odds_fetch_blob (
                match_id, fixture_id, kind, book, market, path, sha256,
                bytes_raw, bytes_gz, fetched_at, http_status
            ) VALUES (?,?,?,?,?,?,?,?,NULL,?,?)""",
            (
                match_id,
                fixture_id,
                kind,
                book,
                market,
                str(path),
                sha,
                path.stat().st_size,
                fetched_at or datetime.now(TZ_CN).isoformat(),
                http_status,
            ),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def insert_channel_snapshots(
    conn: sqlite3.Connection,
    *,
    match_id: int,
    book: str,
    market: str,
    pre: list[dict],
    kickoff: datetime,
    jingcai_date: str,
    kickoff_hour: int,
) -> int:
    """写入 open / rule mid·close / rule_legacy mid·close / actual t8·t1·close。"""
    if not pre:
        return 0
    targets = channel_targets(
        jingcai_date=jingcai_date, kickoff_hour=kickoff_hour, kickoff=kickoff,
        has_jc_code=True,  # 0.3.18：探针场都来自竞彩编号（周X00N），例外按 jc_code_ge_2300
    )
    n = 0
    open_tick = pre[0]
    last_tick = pre[-1]

    def _add(channel: str, point: str, tick, target):
        nonlocal n
        snap = snapshot_from_tick(
            tick,
            market=market,
            target=target,
            point=point,
            channel=channel,
            source="5dollar_history",
        )
        if snap and upsert_snapshot(conn, match_id, book, market, snap):
            n += 1

    # open（两通道共用首 tick）
    _add("rule", "open", open_tick, _parse_dt(open_tick["recorded_at"]))
    _add("actual", "open", open_tick, _parse_dt(open_tick["recorded_at"]))

    # rule mid/close
    rule = targets.get("rule") or {}
    for point in ("mid", "close"):
        tgt = rule.get(point)
        if tgt:
            _add("rule", point, last_tick_at_or_before(pre, tgt), tgt)

    # rule_legacy
    legacy = targets.get("rule_legacy")
    if legacy:
        for point in ("mid", "close"):
            tgt = legacy.get(point)
            if tgt:
                _add("rule_legacy", point, last_tick_at_or_before(pre, tgt), tgt)

    # actual t8 / t1 / close(last pre)
    act = targets["actual"]
    _add("actual", "t8", last_tick_at_or_before(pre, act["t8"]), act["t8"])
    _add("actual", "t1", last_tick_at_or_before(pre, act["t1"]), act["t1"])
    _add("actual", "close", last_tick, _parse_dt(last_tick["recorded_at"]))
    return n


def query_at(
    conn: sqlite3.Connection,
    *,
    match_uid: str,
    book: str,
    market: str,
    at: str,
) -> dict:
    m = conn.execute(
        "SELECT id, match_uid, home_team, away_team, kickoff_at FROM matches WHERE match_uid=?",
        (match_uid,),
    ).fetchone()
    if not m:
        return {"ok": False, "error": "match_not_found", "match_uid": match_uid}
    at_dt = _parse_dt(at)
    # 半开区间 [seg_start, seg_end)
    row = conn.execute(
        """SELECT * FROM odds_timeline_seg
           WHERE match_id=? AND book=? AND market=?
             AND seg_start_at <= ? AND seg_end_at > ?
           ORDER BY seg_start_at DESC LIMIT 1""",
        (m["id"], book, market, _iso(at_dt), _iso(at_dt)),
    ).fetchone()
    if row is None:
        # 闭区间兜底：恰好落在最后一段 end
        row = conn.execute(
            """SELECT * FROM odds_timeline_seg
               WHERE match_id=? AND book=? AND market=?
                 AND seg_start_at <= ? AND seg_end_at >= ?
               ORDER BY seg_start_at DESC LIMIT 1""",
            (m["id"], book, market, _iso(at_dt), _iso(at_dt)),
        ).fetchone()
    return {
        "ok": True,
        "match_uid": match_uid,
        "home_team": m["home_team"],
        "away_team": m["away_team"],
        "book": book,
        "market": market,
        "at": _iso(at_dt),
        "seg": dict(row) if row else None,
    }


def import_all(conn: sqlite3.Connection) -> dict:
    apply_sql_idempotent(conn, MIGRATION)
    conn.commit()
    report: dict = {"matches": [], "blobs": 0, "segs": 0, "snaps": 0, "oa_before": None, "oa_after": None}
    report["oa_before"] = conn.execute("SELECT COUNT(*) FROM odds_asian").fetchone()[0]

    for spec in PROBE_MATCHES:
        fx = load_csl_fixture(spec["csl_file"], spec["fixture_id"])
        mid = ensure_probe_match(conn, fx, spec["fixture_id"])
        kick = _parse_dt(fx["kickoff_utc"]).astimezone(TZ_CN)
        mrow = conn.execute("SELECT * FROM matches WHERE id=?", (mid,)).fetchone()
        jingcai_date = mrow["jingcai_date"]
        kickoff_hour = int(mrow["kickoff_hour"])
        entry = {
            "label": spec["label"],
            "match_id": mid,
            "match_uid": mrow["match_uid"],
            "fixture_id": spec["fixture_id"],
            "jingcai_date": jingcai_date,
            "kickoff_hour": kickoff_hour,
            "kickoff_at": _iso(kick),
            "series": [],
            "targets": {
                k: (
                    {pk: _iso(pv) for pk, pv in v.items()}
                    if isinstance(v, dict)
                    else None
                )
                for k, v in channel_targets(
                    jingcai_date=jingcai_date, kickoff_hour=kickoff_hour, kickoff=kick,
                    has_jc_code=True,
                ).items()
            },
        }

        # odds blob
        odds_path = PROBE_RAW / spec["odds_file"]
        if odds_path.exists() and index_blob(
            conn,
            match_id=mid,
            fixture_id=spec["fixture_id"],
            kind="odds",
            book=None,
            market=None,
            path=odds_path,
        ):
            report["blobs"] += 1

        for fname, api_book, api_mkt in spec["hist"]:
            path = PROBE_RAW / fname
            data = json.loads(path.read_text(encoding="utf-8"))["data"]
            book = BOOK_MAP[api_book]
            market = MARKET_MAP[api_mkt]
            ticks = data["ticks"]
            pre = pre_match_ticks(ticks, kick)
            segs = ticks_to_segments(pre, kick, market)
            n_seg = insert_segments(conn, mid, book, market, segs)
            n_snap = insert_channel_snapshots(
                conn,
                match_id=mid,
                book=book,
                market=market,
                pre=pre,
                kickoff=kick,
                jingcai_date=jingcai_date,
                kickoff_hour=kickoff_hour,
            )
            if index_blob(
                conn,
                match_id=mid,
                fixture_id=spec["fixture_id"],
                kind="history",
                book=book,
                market=market,
                path=path,
            ):
                report["blobs"] += 1
            expect = spec["expect_pre"].get((book, market))
            entry["series"].append(
                {
                    "file": fname,
                    "book": book,
                    "market": market,
                    "ticks_total": len(ticks),
                    "ticks_pre": len(pre),
                    "segs_inserted": n_seg,
                    "segs_total_in_db": conn.execute(
                        "SELECT COUNT(*) FROM odds_timeline_seg WHERE match_id=? AND book=? AND market=?",
                        (mid, book, market),
                    ).fetchone()[0],
                    "snaps_inserted": n_snap,
                    "expect_pre": expect,
                    "pre_ok": (expect is None) or (len(pre) == expect),
                }
            )
            report["segs"] += n_seg
            report["snaps"] += n_snap

        report["matches"].append(entry)

    conn.commit()
    report["oa_after"] = conn.execute("SELECT COUNT(*) FROM odds_asian").fetchone()[0]
    report["oa_unchanged"] = report["oa_before"] == report["oa_after"]
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description="D1 probe → odds timeline (copy DB)")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--query", action="store_true", help="查询任意时刻水位")
    ap.add_argument("--match-uid")
    ap.add_argument("--book", default="macau")
    ap.add_argument("--market", default="asian")
    ap.add_argument("--at", help="ISO-8601 时刻")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    args.db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(args.db)
    try:
        if args.query:
            if not args.match_uid or not args.at:
                ap.error("--query 需要 --match-uid 与 --at")
            print(json.dumps(query_at(conn, match_uid=args.match_uid, book=args.book, market=args.market, at=args.at), ensure_ascii=False, indent=2))
            return
        if args.dry_run:
            apply_sql_idempotent(conn, MIGRATION)
            print(json.dumps({"dry_run": True, "db": str(args.db), "migration": str(MIGRATION)}, ensure_ascii=False))
            return
        report = import_all(conn)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
