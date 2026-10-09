#!/usr/bin/env python3
"""补完整 1X2 open/close 到副本 odds_snapshot（5DF /odds → v2d3）。

口径（2026-10-07 足球分析师拍板）：
- 源：GET /v1/fixtures/{id}/odds?bookmakers=williamhill,macauslot,crown
- williamhill 必选；macauslot/crown 同次若有完整 HXD 也写入
- 写 odds_snapshot：market=euro_1x2 · channel=rule · point=open|close
  price_home/draw/away = 欧式小数原样；source=5df_odds_snap
- 只填空位（UNIQUE 已存在且三价齐全则 skip）；不覆盖非空
- 默认只写 data/v2d3/app.db；现网拒绝；DUAL_WRITE 保持关
- 限速 ≤30/分钟；本刀不做 history / N3 P90 / 挂预测

用法：
  .venv/bin/python scripts/fill_1x2_odds_snap.py fetch
  .venv/bin/python scripts/fill_1x2_odds_snap.py import --db data/v2d3/app.db
  .venv/bin/python scripts/fill_1x2_odds_snap.py report --db data/v2d3/app.db
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
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import connect  # noqa: E402

# 0.3.19：共享数据接口补数让路（北京 11:05–11:20 / 14:55–15:15 / 21:55–22:15 不发请求；补数合计 ≤16 次/分钟；
# 剩余 ≤24 停到 Reset）。共用判断：api/app/shared_api_yield.py
import sys as _yield_sys  # noqa: E402
_yield_sys.path.append(str(_MA_API_ROOT / "app"))  # 追加在末尾，不遮蔽其它模块
import shared_api_yield as _yield_mod  # noqa: E402
_YIELD = _yield_mod.Gate("fill_1x2_odds_snap")


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
RAW_DIR = Path(str(_ODDS_DATA_DIR / "5dollar/1x2-odds-snap-2026-10-07/raw"))
LOG_DIR = Path(str(_ODDS_DATA_DIR / "5dollar/1x2-odds-snap-2026-10-07/logs"))
DEFAULT_DB = ROOT / "data" / "v2d3" / "app.db"
BASE = "https://api.5dollarfootballapi.com/v1"
SOURCE = "5df_odds_snap"
MARKET = "euro_1x2"
CHANNEL = "rule"
MIN_INTERVAL = 2.1  # ≤30/min
BOOKMAKERS = "williamhill,macauslot,crown"
SLUG_TO_BOOK = {
    "williamhill": "william",
    "macauslot": "macau",
    "crown": "crown",
}
PHASE_MAP = {"opening": "open", "closing": "close"}


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
            )
        if not _env_dual_write_on():
            raise SystemExit(
                "现网双写开关关闭（DUAL_WRITE_ODDS_ASIAN!=1）。按拍板保持关闭。"
            )


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


def _complete_hxd(obj: dict | None) -> bool:
    if not isinstance(obj, dict):
        return False
    for k in ("home", "draw", "away"):
        v = obj.get(k)
        if v is None:
            return False
        try:
            if float(v) <= 0:
                return False
        except (TypeError, ValueError):
            return False
    return True


def fetch_one(fixture_id: str, *, force: bool = False) -> dict:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    out = RAW_DIR / f"{fixture_id}_odds_1x2.json"
    if out.exists() and out.stat().st_size > 50 and not force:
        j = json.loads(out.read_text(encoding="utf-8"))
        return {
            "fixture_id": fixture_id,
            "path": str(out),
            "skipped": True,
            "http": 200,
            "ok": bool(j.get("success")),
            "books": _book_completeness(j),
        }

    while True:
        j, http, hdr = api_get(
            f"/fixtures/{fixture_id}/odds",
            {"bookmakers": BOOKMAKERS},
        )
        if http == 429:
            ra = float(hdr.get("Retry-After", 3))
            time.sleep(ra + 0.5)
            continue
        break

    out.write_text(json.dumps(j, ensure_ascii=False), encoding="utf-8")
    return {
        "fixture_id": fixture_id,
        "path": str(out),
        "skipped": False,
        "http": http,
        "ok": http == 200 and bool(j.get("success")),
        "books": _book_completeness(j) if http == 200 else {},
        "error": http != 200 or not j.get("success"),
    }


def _book_completeness(j: dict) -> dict:
    out: dict[str, dict] = {}
    for bm in ((j.get("data") or {}).get("bookmakers")) or []:
        slug = (bm.get("slug") or "").lower()
        book = SLUG_TO_BOOK.get(slug)
        if not book:
            continue
        x1 = ((bm.get("odds") or {}).get("1x2")) or {}
        out[book] = {
            "open": _complete_hxd(x1.get("opening")),
            "close": _complete_hxd(x1.get("closing")),
        }
    return out


def cmd_fetch(args: argparse.Namespace) -> None:
    m = load_map()
    items = m["items"]
    if args.limit:
        items = items[: args.limit]
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    t0 = time.time()
    last_call = 0.0
    for i, it in enumerate(items, 1):
        fid = str(it["fixture_id"])
        path = RAW_DIR / f"{fid}_odds_1x2.json"
        if path.exists() and path.stat().st_size > 50 and not args.force:
            r = fetch_one(fid, force=False)
            results.append({**r, "match_id": it["match_id"]})
            print(f"[{i}/{len(items)}] skip cache {fid} books={r.get('books')}")
            continue
        wait = MIN_INTERVAL - (time.time() - last_call)
        if wait > 0:
            time.sleep(wait)
        last_call = time.time()
        r = fetch_one(fid, force=args.force)
        results.append({**r, "match_id": it["match_id"]})
        print(
            f"[{i}/{len(items)}] http={r['http']} fid={fid} "
            f"ok={r.get('ok')} books={r.get('books')}"
        )

    summary = {
        "n": len(results),
        "fetched": sum(1 for r in results if not r.get("skipped")),
        "cached": sum(1 for r in results if r.get("skipped")),
        "http_ok": sum(1 for r in results if r.get("ok")),
        "http_err": sum(1 for r in results if r.get("error")),
        "william_complete_oc": sum(
            1
            for r in results
            if (r.get("books") or {}).get("william", {}).get("open")
            and (r.get("books") or {}).get("william", {}).get("close")
        ),
        "elapsed_sec": round(time.time() - t0, 1),
        "results": results,
    }
    (LOG_DIR / "fetch_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    brief = {k: v for k, v in summary.items() if k != "results"}
    print(json.dumps(brief, ensure_ascii=False, indent=2))


def extract_snaps(j: dict) -> list[dict]:
    """Return list of snapshot dicts ready for insert."""
    snaps = []
    fetched_at = datetime.now(timezone.utc).isoformat()
    for bm in ((j.get("data") or {}).get("bookmakers")) or []:
        slug = (bm.get("slug") or "").lower()
        book = SLUG_TO_BOOK.get(slug)
        if not book:
            continue
        x1 = ((bm.get("odds") or {}).get("1x2")) or {}
        for api_phase, point in PHASE_MAP.items():
            obj = x1.get(api_phase)
            if not _complete_hxd(obj):
                continue
            snaps.append(
                {
                    "book": book,
                    "point": point,
                    "price_home": float(obj["home"]),
                    "price_draw": float(obj["draw"]),
                    "price_away": float(obj["away"]),
                    "recorded_at": fetched_at,
                    "source": SOURCE,
                    "extras_json": json.dumps(
                        {
                            "source": SOURCE,
                            "api_bookmaker": slug,
                            "api_phase": api_phase,
                            "endpoint": "/odds",
                        },
                        ensure_ascii=False,
                    ),
                }
            )
    return snaps


def insert_snap(conn: sqlite3.Connection, match_id: int, snap: dict) -> str:
    """Only fill empty slots. Skip if row exists with complete HXD."""
    ex = conn.execute(
        """SELECT id, price_home, price_draw, price_away, source
           FROM odds_snapshot
           WHERE match_id=? AND book=? AND market=? AND channel=? AND point=?""",
        (match_id, snap["book"], MARKET, CHANNEL, snap["point"]),
    ).fetchone()
    if ex:
        if (
            ex["price_home"] is not None
            and ex["price_draw"] is not None
            and ex["price_away"] is not None
        ):
            return "skip_exists"
        # row exists but incomplete → fill NULL prices only if our source / empty
        if ex["source"] not in (None, "", SOURCE, "5dollar_history", "5df_odds_snap"):
            return "skip_other_source"
        conn.execute(
            """UPDATE odds_snapshot SET
               price_home=COALESCE(price_home, ?),
               price_draw=COALESCE(price_draw, ?),
               price_away=COALESCE(price_away, ?),
               recorded_at=COALESCE(recorded_at, ?),
               source=?, extras_json=?
               WHERE id=?""",
            (
                snap["price_home"],
                snap["price_draw"],
                snap["price_away"],
                snap["recorded_at"],
                SOURCE,
                snap["extras_json"],
                ex["id"],
            ),
        )
        return "filled_nulls"
    conn.execute(
        """INSERT INTO odds_snapshot (
            match_id, book, market, channel, point, recorded_at, target_at,
            lag_hours, stale_gap, line,
            price_home, price_away, price_draw, price_over, price_under,
            water_home, water_away, water_over, water_under,
            water_src, water_censored, source, extras_json
        ) VALUES (?,?,?,?,?,?,NULL,NULL,0,NULL,?,?,?,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,?,?)""",
        (
            match_id,
            snap["book"],
            MARKET,
            CHANNEL,
            snap["point"],
            snap["recorded_at"],
            snap["price_home"],
            snap["price_away"],
            snap["price_draw"],
            snap["source"],
            snap["extras_json"],
        ),
    )
    return "inserted"


def index_blob(conn: sqlite3.Connection, match_id: int, fixture_id: str, path: Path, http: int) -> None:
    try:
        conn.execute(
            """INSERT INTO odds_fetch_blob (
                match_id, fixture_id, kind, book, market, path, sha256,
                bytes_raw, bytes_gz, fetched_at, http_status
            ) VALUES (?,?,?,?,?,?,?,?,NULL,?,?)""",
            (
                match_id,
                fixture_id,
                "odds_snap",
                "multi",
                "1x2",
                str(path),
                _sha256_file(path),
                path.stat().st_size,
                datetime.now(timezone.utc).isoformat(),
                http,
            ),
        )
    except sqlite3.IntegrityError:
        pass


def coverage_query(conn: sqlite3.Connection) -> dict:
    base_ids = [
        r[0]
        for r in conn.execute(
            "SELECT id FROM matches WHERE match_uid NOT LIKE 'probe:%'"
        )
    ]
    n_base = len(base_ids)
    # map match ids from fixture map if present in DB
    m = load_map()
    map_ids = [it["match_id"] for it in m["items"]]

    def book_cov(book: str) -> dict:
        rows = list(
            conn.execute(
                f"""
                SELECT match_id,
                  MAX(CASE WHEN point='open' AND price_home IS NOT NULL
                            AND price_draw IS NOT NULL AND price_away IS NOT NULL
                       THEN 1 ELSE 0 END) AS has_open,
                  MAX(CASE WHEN point='close' AND price_home IS NOT NULL
                            AND price_draw IS NOT NULL AND price_away IS NOT NULL
                       THEN 1 ELSE 0 END) AS has_close
                FROM odds_snapshot
                WHERE market=? AND channel=? AND book=?
                  AND match_id IN ({",".join("?" * len(map_ids))})
                GROUP BY match_id
                """,
                (MARKET, CHANNEL, book, *map_ids),
            )
        )
        open_n = sum(1 for r in rows if r[1])
        close_n = sum(1 for r in rows if r[2])
        both = sum(1 for r in rows if r[1] and r[2])
        return {
            "book": book,
            "rows_matches": len(rows),
            "complete_open": open_n,
            "complete_close": close_n,
            "complete_open_and_close": both,
            "still_empty": len(map_ids) - both,
            "mapped": len(map_ids),
        }

    return {
        "n_base_non_probe": n_base,
        "mapped": len(map_ids),
        "william": book_cov("william"),
        "macau": book_cov("macau"),
        "crown": book_cov("crown"),
    }


def cmd_import(args: argparse.Namespace) -> None:
    db = Path(args.db)
    assert_db_allowed(db, force_live=args.i_know_this_is_production)
    m = load_map()
    items = m["items"]
    if args.limit:
        items = items[: args.limit]

    conn = connect(db)
    summary = {
        "matches_attempted": 0,
        "matches_no_raw": 0,
        "matches_http_bad": 0,
        "snap_actions": {},
        "by_book_point_inserted": {},
        "william_complete_after_match": 0,
        "details": [],
        "dual_write_env": os.environ.get("DUAL_WRITE_ODDS_ASIAN", "0"),
        "db": str(db.resolve()),
        "source": SOURCE,
    }

    for it in items:
        mid = it["match_id"]
        fid = str(it["fixture_id"])
        path = RAW_DIR / f"{fid}_odds_1x2.json"
        summary["matches_attempted"] += 1
        if not path.exists():
            summary["matches_no_raw"] += 1
            continue
        j = json.loads(path.read_text(encoding="utf-8"))
        if not j.get("success"):
            summary["matches_http_bad"] += 1
            summary["details"].append(
                {"match_id": mid, "fixture_id": fid, "error": "raw_not_success"}
            )
            continue
        snaps = extract_snaps(j)
        acts = {}
        for snap in snaps:
            act = insert_snap(conn, mid, snap)
            summary["snap_actions"][act] = summary["snap_actions"].get(act, 0) + 1
            key = f"{snap['book']}:{snap['point']}"
            if act in ("inserted", "filled_nulls"):
                summary["by_book_point_inserted"][key] = (
                    summary["by_book_point_inserted"].get(key, 0) + 1
                )
            acts[key] = act
        index_blob(conn, mid, fid, path, 200)
        # per-match william complete?
        w_open = any(s["book"] == "william" and s["point"] == "open" for s in snaps)
        w_close = any(s["book"] == "william" and s["point"] == "close" for s in snaps)
        if w_open and w_close:
            summary["william_complete_after_match"] += 1
        summary["details"].append(
            {
                "match_id": mid,
                "fixture_id": fid,
                "snaps": len(snaps),
                "william_oc": w_open and w_close,
                "acts": acts,
            }
        )

    conn.commit()
    summary["coverage"] = coverage_query(conn)
    out_path = LOG_DIR / "import_summary.json"
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    brief = {k: v for k, v in summary.items() if k != "details"}
    print(json.dumps(brief, ensure_ascii=False, indent=2))


def cmd_report(args: argparse.Namespace) -> None:
    db = Path(args.db)
    conn = connect(db)
    cov = coverage_query(conn)
    print(json.dumps(cov, ensure_ascii=False, indent=2))
    # empty reasons for william
    m = load_map()
    missing = []
    for it in m["items"]:
        mid = it["match_id"]
        fid = str(it["fixture_id"])
        rows = list(
            conn.execute(
                """SELECT point, price_home, price_draw, price_away FROM odds_snapshot
                   WHERE match_id=? AND book='william' AND market=? AND channel=?""",
                (mid, MARKET, CHANNEL),
            )
        )
        by = {r[0]: r for r in rows}

        def ok(pt):
            r = by.get(pt)
            return r and r[1] is not None and r[2] is not None and r[3] is not None

        if not (ok("open") and ok("close")):
            path = RAW_DIR / f"{fid}_odds_1x2.json"
            reason = "no_raw"
            if path.exists():
                j = json.loads(path.read_text(encoding="utf-8"))
                if not j.get("success"):
                    reason = f"api_fail:{j.get('message') or j.get('error') or 'unknown'}"
                else:
                    books = {
                        (bm.get("slug") or ""): bm
                        for bm in ((j.get("data") or {}).get("bookmakers") or [])
                    }
                    wh = books.get("williamhill")
                    if not wh:
                        reason = "williamhill_absent"
                    else:
                        x1 = ((wh.get("odds") or {}).get("1x2")) or {}
                        o = _complete_hxd(x1.get("opening"))
                        c = _complete_hxd(x1.get("closing"))
                        if not o and not c:
                            reason = "william_1x2_empty"
                        elif not o:
                            reason = "william_open_incomplete"
                        elif not c:
                            reason = "william_close_incomplete"
                        else:
                            reason = "import_gap"
            missing.append(
                {
                    "match_id": mid,
                    "fixture_id": fid,
                    "home": it.get("home_team"),
                    "away": it.get("away_team"),
                    "reason": reason,
                    "has_open": ok("open"),
                    "has_close": ok("close"),
                }
            )
    print("william_missing", len(missing))
    for row in missing[:30]:
        print(row)
    (LOG_DIR / "william_missing.json").write_text(
        json.dumps(missing, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("dual_write", os.environ.get("DUAL_WRITE_ODDS_ASIAN", "0"))
    print("live_db", (ROOT / "data" / "app.db").resolve())
    print("target_db", db.resolve())


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
