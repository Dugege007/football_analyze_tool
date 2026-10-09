#!/usr/bin/env python3
"""football-data.co.uk 2627 季 CSV -> backfill/merged 月 JSON（skip-existing，不覆盖已有记录）。

用法：
  python3 football_data_2627_merge.py --download     # 重下 9 个联赛 CSV 到 raw/ 后合并
  python3 football_data_2627_merge.py                # 仅用 raw/ 现有 CSV 合并
  python3 football_data_2627_merge.py --dry-run      # 只统计不写

口径：
- CSV 的 Time 为英国当地时间（Europe/London，含夏令时），转 UTC+8 用 zoneinfo 精确换算
  （meta.kickoff_tz_method = "europe_london_zoneinfo"）。2026-10-05 首批 512 场旧记录用的是
  固定 +6h 近似（meta.source_kickoff_utc8_approx），本脚本不改旧记录，只在报告里列出偏差。
- 竞彩日：UTC+8 开赛 0–10 点记入前一日；kickoff_hour 为 UTC+8 小时。
- 亚盘：open=AHh + B365AHH/AHA，close=AHCh + B365CAHH/CAHA，水位 = 欧赔 - 1。
- 竞彩字段全部 null，不编造。
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

import argparse, csv, json, os, sys, time, urllib.request, datetime as dt
from zoneinfo import ZoneInfo

BASE = str(_ODDS_DATA_DIR / "backfill")
RAW = f"{BASE}/raw/football-data-co-uk"
MERGED = f"{BASE}/merged"
ALL = f"{MERGED}/2627_major_eu_football-data.json"
CODES = ["E0", "E1", "SP1", "D1", "I1", "F1", "N1", "P1", "SC0"]
NAMES = {"E0": "英超", "E1": "英冠", "SP1": "西甲", "D1": "德甲", "I1": "意甲",
         "F1": "法甲", "N1": "荷甲", "P1": "葡超", "SC0": "苏超"}
WD = "一二三四五六日"
UK, CN = ZoneInfo("Europe/London"), ZoneInfo("Asia/Shanghai")

def fnum(v):
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None

def water(v):
    x = fnum(v)
    return round(x - 1, 2) if x is not None else None

def ah(h, hw, aw):
    return {"home_water": water(hw), "handicap": fnum(h), "away_water": water(aw)}

def empty3():
    return {k: {"home_water": None, "handicap": None, "away_water": None} for k in ("open", "mid", "close")}

def download():
    log = []
    for c in CODES:
        url = f"https://football-data.co.uk/mmz4281/2627/{c}.csv"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
        p = f"{RAW}/2627_{c}.csv"
        if len(data) > 200:
            open(p, "wb").write(data)
        log.append({"code": c, "status": 200, "bytes": len(data), "path": p})
        time.sleep(1)
    return log

def convert(row, code, collected_at):
    d = dt.datetime.strptime(f"{row['Date']} {row.get('Time') or '15:00'}", "%d/%m/%Y %H:%M")
    uk = d.replace(tzinfo=UK)
    cn = uk.astimezone(CN)
    jc_day = (cn - dt.timedelta(days=1)).date() if cn.hour <= 10 else cn.date()
    hg, ag = int(row["FTHG"]), int(row["FTAG"])
    wdl = "胜" if hg > ag else ("平" if hg == ag else "负")
    wd = WD[jc_day.weekday()]
    b365 = empty3()
    b365["open"] = ah(row.get("AHh"), row.get("B365AHH"), row.get("B365AHA"))
    b365["close"] = ah(row.get("AHCh"), row.get("B365CAHH"), row.get("B365CAHA"))
    b365["_note"] = "waters converted from football-data European decimal via water=decimal-1"
    uid = f"fd_{code}_{d.strftime('%d%m%Y')}_{row['HomeTeam']}_{row['AwayTeam']}"
    return {
        "match": {"date": jc_day.isoformat(), "weekday": wd, "jc": {"id": None, "weekday": wd, "no": None},
                  "competition": {"name": NAMES[code], "type": "联赛", "stage": None},
                  "kickoff_hour": cn.hour, "teams": {"home": row["HomeTeam"], "away": row["AwayTeam"]}},
        "result": {"home_goals": hg, "away_goals": ag, "total_goals": hg + ag, "wdl": wdl},
        "stats": {"recent": {"last10": {"home": {"gf": None, "ga": None}, "away": {"gf": None, "ga": None}},
                             "last6": {"home": {"gf": None, "ga": None}, "away": {"gf": None, "ga": None}},
                             "home_streak_last6": None},
                  "h2h": {"matches": None, "last6": {"home_gf": None, "home_ga": None}, "home_streak_last6": None},
                  "rank": {"home": None, "away": None}, "popularity_diff": None, "injury": None, "weather": None},
        "odds": {"asian": {"macau": {"open": None, "close": None}, "crown": empty3(), "william": empty3(), "bet365": b365},
                 "euro_home_win": {"macau": {"open": None, "close": None}, "william": {"open": None, "close": None},
                                   "bet365": {"open": fnum(row.get("B365H")), "close": fnum(row.get("B365CH"))}},
                 "jc_home_win": {"open": None, "close": None}},
        "meta": {"source_file": f"backfill/raw/football-data-co-uk/2627_{code}.csv",
                 "month": jc_day.strftime("%y%m"), "source": "football-data.co.uk", "source_div": code,
                 "source_kickoff_uk_local": uk.strftime("%Y-%m-%d %H:%M:%S"),
                 "kickoff_at": cn.isoformat(), "kickoff_tz_method": "europe_london_zoneinfo",
                 "collected_at": collected_at, "water_scale": "hk_water_from_decimal",
                 "jingcai_fields": "null_not_invented", "uid": uid},
    }

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--download", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    dl = download() if a.download else None
    collected_at = dt.datetime.now(CN).isoformat()
    existing = json.load(open(ALL)) if os.path.exists(ALL) else []
    have = {r["meta"]["uid"]: r for r in existing}
    new, skipped_unplayed, tz_drift = [], 0, []
    for c in CODES:
        p = f"{RAW}/2627_{c}.csv"
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                if not row.get("Date") or row.get("FTHG") in (None, "") or row.get("FTAG") in (None, ""):
                    skipped_unplayed += 1
                    continue
                rec = convert(row, c, collected_at)
                uid = rec["meta"]["uid"]
                if uid in have:
                    old = have[uid]["match"]
                    if (old["date"], old["kickoff_hour"]) != (rec["match"]["date"], rec["match"]["kickoff_hour"]):
                        tz_drift.append({"uid": uid, "old": [old["date"], old["kickoff_hour"]],
                                         "zoneinfo": [rec["match"]["date"], rec["match"]["kickoff_hour"]]})
                    continue
                new.append(rec)
    by_month = {}
    for r in new:
        by_month.setdefault(r["meta"]["month"], []).append(r)
    report = {"run_at": collected_at, "download": dl, "existing": len(existing), "new": len(new),
              "new_by_month": {k: len(v) for k, v in sorted(by_month.items())},
              "new_by_div": {c: sum(1 for r in new if r["meta"]["source_div"] == c) for c in CODES},
              "skipped_unplayed": skipped_unplayed, "existing_tz_drift_count": len(tz_drift),
              "existing_tz_drift_jcday_changed": sum(1 for t in tz_drift if t["old"][0] != t["zoneinfo"][0]),
              "dry_run": a.dry_run}
    if not a.dry_run and new:
        stamp = dt.datetime.now(CN).strftime("%Y%m%d_%H%M%S")
        if os.path.exists(ALL):
            os.replace(ALL, f"{ALL}.bak-{stamp}")
        json.dump(existing + new, open(ALL, "w"), ensure_ascii=False, indent=1)
        for m, recs in by_month.items():
            mp = f"{MERGED}/{m}_football-data.json"
            cur = json.load(open(mp)) if os.path.exists(mp) else []
            if cur:
                os.replace(mp, f"{mp}.bak-{stamp}")
            json.dump(cur + recs, open(mp, "w"), ensure_ascii=False, indent=1)
    os.makedirs(f"{BASE}/logs", exist_ok=True)
    with open(f"{BASE}/logs/football_data_merge_runs.jsonl", "a") as f:
        f.write(json.dumps(report, ensure_ascii=False) + "\n")
    json.dump(tz_drift, open(f"{BASE}/logs/football_data_tz_drift_existing.json", "w"), ensure_ascii=False, indent=1)
    print(json.dumps(report, ensure_ascii=False, indent=1))

if __name__ == "__main__":
    main()
