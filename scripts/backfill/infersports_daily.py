#!/usr/bin/env python3
"""InferSports daily finished-event paging + odds/batch(macau,crown).
Local day = UTC+8. Marks stale whenever /health is degraded or feed_live false.
Usage: infersports_daily.py [--days YYYY-MM-DD ...] (default: local yesterday + any undone gap days since 2026-09-15)
"""
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

import json, os, sys, time, datetime as dt, urllib.request, urllib.error
BASE = "https://api.infersports.dev"
KEY = os.environ.get("INFERSPORTS_API_KEY", "").strip()
if not KEY:
    raise SystemExit("缺少 INFERSPORTS_API_KEY：请在仓库根 .env 中填写（见 config.example.env / docs/SENSITIVE.md）")
ROOT = str(_ODDS_DATA_DIR / "backfill")
RAW = f"{ROOT}/raw/infersports/daily"
MERGED = f"{ROOT}/merged/infersports_daily"
STATE = f"{ROOT}/logs/infersports_daily_state.json"
RUNS = f"{ROOT}/logs/infersports_daily_runs.jsonl"
TZ = dt.timezone(dt.timedelta(hours=8))
# 每日调用上限与预留量取决于你自己的 InferSports 账户（见 config.example.env）
DAILY_LIMIT = int(os.environ.get("INFERSPORTS_DAILY_LIMIT", "1000"))
RESERVE = int(os.environ.get("INFERSPORTS_DAILY_RESERVE", "100"))
GAP_START = dt.date(2026, 9, 15)
calls = 0

def req(path, body=None):
    global calls
    for attempt in range(4):
        r = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body else None,
            headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
            method="POST" if body else "GET")
        try:
            calls += 1
            with urllib.request.urlopen(r, timeout=30) as resp:
                rem = resp.headers.get("x-ratelimit-remaining")
                data = json.load(resp)
            if rem and rem.isdigit() and int(rem) < 5: time.sleep(5)
            else: time.sleep(1.1)
            return data
        except urllib.error.HTTPError as e:
            if e.code == 429:
                if (e.headers.get("X-RateLimit-Scope") or "").lower() == "daily":
                    raise SystemExit("daily quota hit")
                time.sleep(int(e.headers.get("Retry-After") or 10)); continue
            if e.code >= 500: time.sleep(5 * (attempt + 1)); continue
            raise
    raise RuntimeError(f"failed {path}")

def page_events(utc_date):
    out, cur = [], None
    while True:
        p = f"/v1/events?sport=football&date={utc_date}&limit=100" + (f"&cursor={cur}" if cur else "")
        d = req(p); out += d.get("data", [])
        cur = (d.get("page") or {}).get("next_cursor")
        if not cur: return out

def hk(dec):
    return round(dec - 1, 3) if dec and dec > 1 else None

def pick(quotes, book, market):
    qs = [q for q in quotes if q["bookmaker"] == book and q["market_type"] == market and q.get("period") == "full_time"]
    live = [q for q in qs if q.get("status") == "open" and all((v or 0) > 1 for v in q["prices"].values())]
    if not live:
        live = [q for q in qs if all((v or 0) > 1 for v in q["prices"].values())]  # suspended but kept last prices
    if not live: return None
    # main line: most balanced prices
    q = min(live, key=lambda q: abs(max(q["prices"].values()) - min(q["prices"].values())))
    return q

def to_record(ev, od, stale_feed, health):
    st = dt.datetime.fromisoformat(ev["scheduled_at"].replace("Z", "+00:00")).astimezone(TZ)
    quotes = (od or {}).get("odds", []) or []
    asian, totals, euro = {}, {}, {}
    for b in ("macau", "crown"):
        a = pick(quotes, b, "asian_handicap"); t = pick(quotes, b, "totals"); x = pick(quotes, b, "1x2")
        asian[b] = {"close": {"home_water": hk(a["prices"]["home"]), "handicap": a["line"], "away_water": hk(a["prices"]["away"]),
                              "status": a["status"], "as_of": a.get("as_of")} if a else None}
        totals[b] = {"close": {"over_water": hk(t["prices"]["over"]), "line": t["line"], "under_water": hk(t["prices"]["under"]),
                               "status": t["status"], "as_of": t.get("as_of")} if t else None}
        euro[b] = {"close": {"home": x["prices"]["home"], "draw": x["prices"]["draw"], "away": x["prices"]["away"],
                             "status": x["status"], "as_of": x.get("as_of")} if x else None}
    wd = "一二三四五六日"[st.weekday()]
    return {
        "match": {"date": st.date().isoformat(), "weekday": wd, "jc": None,
                  "competition": {"name": (ev.get("league") or {}).get("name") if isinstance(ev.get("league"), dict) else ev.get("league"), "type": None, "stage": None},
                  "kickoff_hour": st.hour, "kickoff_local": st.strftime("%Y-%m-%d %H:%M"),
                  "teams": {"home": (ev.get("home_team") or {}).get("name") if isinstance(ev.get("home_team"), dict) else ev.get("home_team"),
                            "away": (ev.get("away_team") or {}).get("name") if isinstance(ev.get("away_team"), dict) else ev.get("away_team")}},
        "result": {"home_goals": None, "away_goals": None, "total_goals": None, "wdl": None},
        "odds": {"asian": asian, "totals": totals, "euro_1x2": euro},
        "meta": {"source": "infersports", "scope": "extra", "event_id": ev["id"], "scheduled_at": ev["scheduled_at"],
                 "event_status": ev.get("status"), "as_of": (od or {}).get("as_of"),
                 "stale": bool(stale_feed or (od or {}).get("stale", True)),
                 "feed_status": health.get("status"), "feed_live": health.get("feed_live"), "feed_as_of": health.get("as_of"),
                 "not_for_closing_production": True, "result_missing": True, "jingcai_fields": "null_not_invented",
                 "water_scale": "hk_water_from_decimal", "collected_at": dt.datetime.now(TZ).isoformat(), "uid": f"is_{ev['id']}"}}

def do_day(local_day, health, stale_feed):
    lo = dt.datetime.combine(local_day, dt.time(0), TZ); hi = lo + dt.timedelta(days=1)
    evs = {}
    for ud in {lo.astimezone(dt.timezone.utc).date(), (hi - dt.timedelta(seconds=1)).astimezone(dt.timezone.utc).date()}:
        for e in page_events(ud.isoformat()):
            t = dt.datetime.fromisoformat(e["scheduled_at"].replace("Z", "+00:00"))
            if lo <= t < hi and e.get("status") == "finished": evs[e["id"]] = e
    ids = sorted(evs); batches = []
    for i in range(0, len(ids), 20):
        batches.append(req("/v1/odds/batch", {"event_ids": ids[i:i+20], "bookmakers": "macau,crown", "markets": "1x2,asian_handicap,totals"}))
    os.makedirs(f"{RAW}/{local_day}", exist_ok=True); os.makedirs(MERGED, exist_ok=True)
    json.dump({"health": health, "events": list(evs.values())}, open(f"{RAW}/{local_day}/events_finished.json", "w"), ensure_ascii=False)
    json.dump(batches, open(f"{RAW}/{local_day}/odds_batch.json", "w"), ensure_ascii=False)
    odds = {}
    for b in batches:
        for item in b.get("data", []): odds[item["event_id"]] = item.get("odds")
    recs = [to_record(evs[i], odds.get(i), stale_feed, health) for i in ids]
    recs.sort(key=lambda r: (r["meta"]["scheduled_at"], r["meta"]["event_id"]))
    json.dump(recs, open(f"{MERGED}/{local_day}.json", "w"), ensure_ascii=False, indent=1)
    usable = sum(1 for r in recs if any((r["odds"]["asian"][b]["close"] or {}).get("handicap") is not None for b in ("macau", "crown")))
    open_q = sum(1 for r in recs if any((r["odds"][m][b]["close"] or {}).get("status") == "open" for m in ("asian", "totals", "euro_1x2") for b in ("macau", "crown")))
    return {"day": local_day.isoformat(), "finished": len(recs), "with_ah_price": usable, "with_open_quote": open_q, "batches": len(batches)}

def main():
    health = req("/health"); usage = req("/v1/usage")
    stale_feed = health.get("status") != "ok" or not health.get("feed_live") or health.get("as_of_stale")
    state = json.load(open(STATE)) if os.path.exists(STATE) else {"done": {}}
    today = dt.datetime.now(TZ).date(); yday = today - dt.timedelta(days=1)
    if len(sys.argv) > 2 and sys.argv[1] == "--days":
        days = [dt.date.fromisoformat(x) for x in sys.argv[2:]]
    else:
        days = [GAP_START + dt.timedelta(days=i) for i in range((yday - GAP_START).days + 1)]
        days = [d for d in days if d == yday or d.isoformat() not in state["done"]]
    res = []
    used0 = (usage.get("usage") or {}).get("requests", 0)
    for d in days:
        if used0 + calls > DAILY_LIMIT - RESERVE: res.append({"day": d.isoformat(), "skipped": "reserve"}); break
        r = do_day(d, health, stale_feed); res.append(r)
        state["done"][d.isoformat()] = {**r, "stale": bool(stale_feed), "feed_as_of": health.get("as_of"), "at": dt.datetime.now(TZ).isoformat()}
        json.dump(state, open(STATE, "w"), ensure_ascii=False, indent=1)  # save per day so a timeout keeps progress
    prev = state.get("last_health")
    state["last_health"] = {"status": health.get("status"), "feed_live": health.get("feed_live"), "as_of": health.get("as_of"), "as_of_stale": health.get("as_of_stale")}
    json.dump(state, open(STATE, "w"), ensure_ascii=False, indent=1)
    run = {"at": dt.datetime.now(TZ).isoformat(), "health": state["last_health"], "prev_health": prev, "stale": bool(stale_feed),
           "calls": calls, "usage_before": used0, "days": res}
    open(RUNS, "a").write(json.dumps(run, ensure_ascii=False) + "\n")
    print(json.dumps(run, ensure_ascii=False, indent=1))

main()
