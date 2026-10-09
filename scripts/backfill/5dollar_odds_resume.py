#!/usr/bin/env python3
"""Hourly resume: pull 5Dollar Bet365 odds for finished Top-5 fixtures lacking asian_handicap, then light merge."""
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

import json, os, glob, time, datetime, urllib.request, urllib.error

# 0.3.19：共享数据接口补数让路（北京 11:05–11:20 / 14:55–15:15 / 21:55–22:15 不发请求；补数合计 ≤16 次/分钟；
# 剩余 ≤24 停到 Reset）。共用判断：api/app/shared_api_yield.py
import sys as _yield_sys  # noqa: E402
_yield_sys.path.append(str(_MA_API_ROOT / "app"))  # 追加在末尾，不遮蔽其它模块
import shared_api_yield as _yield_mod  # noqa: E402
_YIELD = _yield_mod.Gate("5dollar_odds_resume")


def _gated_urlopen(req, timeout=60):
    _YIELD.before_request()
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        _YIELD.after_response(e.headers)
        raise
    _YIELD.after_response(r.headers)
    return r


BASE="https://api.5dollarfootballapi.com/v1"
ROOT=str(_ODDS_DATA_DIR / "backfill")
RAW=f"{ROOT}/raw/5dollar"; ODDS=f"{RAW}/odds"; MERGED=f"{ROOT}/merged"
STATE=f"{ROOT}/logs/5dollar_resume_state.json"; LOG=f"{ROOT}/logs/5dollar_resume_runs.jsonl"
KEY=os.environ.get("FIVEDOLLAR_FOOTBALL_API_KEY")
RESERVE=10; GAP=3.5; MAX_NO_AH_TRIES=2
TZ=datetime.timezone(datetime.timedelta(hours=8))
now=lambda: datetime.datetime.now(TZ).isoformat()

def load(p,d):
    try: return json.load(open(p))
    except Exception: return d

def has_ah(p):
    d=load(p,None)
    if not d: return False
    for b in (d.get("data") or {}).get("bookmakers") or []:
        ah=(b.get("odds") or {}).get("asian_handicap") or {}
        if ah.get("opening") or ah.get("closing"): return True
    return False

def get(path):
    req=urllib.request.Request(BASE+path,headers={"Authorization":f"Bearer {KEY}","Accept":"application/json"})
    try:
        with _gated_urlopen(req, timeout=30) as r:
            return r.status, dict(r.headers), json.loads(r.read())
    except urllib.error.HTTPError as e:
        body=e.read()
        try: body=json.loads(body)
        except Exception: body=body[:300].decode("utf8","ignore")
        return e.code, dict(e.headers), body

state=load(STATE,{"empty_rounds":0,"no_ah_tries":{}})
fixtures={}
for f in glob.glob(f"{RAW}/fixtures_finished_*.json"):
    for m in (load(f,{}).get("data") or []):
        if m.get("status")=="finished": fixtures[str(m["id"])]=m
pending=[i for i in fixtures if not has_ah(f"{ODDS}/{i}.json") and state["no_ah_tries"].get(i,0)<MAX_NO_AH_TRIES]
pending.sort(key=lambda i: fixtures[i].get("kickoff_ts",0), reverse=True)
run={"at":now(),"finished_total":len(fixtures),"pending_before":len(pending),"pulled":0,"with_ah":0,"no_ah":0,"errors":[],"stop":None}
if not KEY: run["stop"]="missing_key"; pending=[]
if not pending and run["stop"] is None:
    state["empty_rounds"]=state.get("empty_rounds",0)+1; run["stop"]="no_pending"
else:
    state["empty_rounds"]=0
    minute=[]
    for i in pending:
        t=time.time(); minute=[x for x in minute if t-x<60]
        if len(minute)>=18: time.sleep(60-(t-minute[0])+1)
        st,h,body=get(f"/fixtures/{i}/odds"); minute.append(time.time())
        hl={k.lower():v for k,v in h.items()}
        rem=hl.get("x-ratelimit-remaining"); run["remaining"]=rem; run["reset"]=hl.get("x-ratelimit-reset")
        if st==200:
            json.dump(body,open(f"{ODDS}/{i}.json","w"),ensure_ascii=False,indent=2)
            run["pulled"]+=1
            if has_ah(f"{ODDS}/{i}.json"): run["with_ah"]+=1
            else: run["no_ah"]+=1; state["no_ah_tries"][i]=state["no_ah_tries"].get(i,0)+1
        elif st==429: run["stop"]="rate_limited_429"; break
        elif st in (401,403): run["errors"].append({"id":i,"status":st,"body":body}); run["stop"]=f"auth_{st}"; break
        else:
            run["errors"].append({"id":i,"status":st,"body":str(body)[:200]})
            if len(run["errors"])>=5: run["stop"]="too_many_errors"; break
        if rem is not None and int(rem)<=RESERVE: run["stop"]="hourly_reserve_reached"; break
        time.sleep(GAP)
    else:
        run["stop"]=run["stop"] or "pending_exhausted"

# light merge
def w(o): return None if o is None else round(o-1,3)
def merge():
    allp=f"{MERGED}/5dollar_top5_all.json"; rows=load(allp,[])
    n_upd=0
    for m in rows:
        fid=str(m["meta"]["fixture_id"]); p=f"{ODDS}/{fid}.json"
        if not os.path.exists(p): continue
        d=load(p,{}); b=next((x for x in (d.get("data") or {}).get("bookmakers") or [] if x.get("slug")=="bet365"),None)
        if not b: continue
        o=b.get("odds") or {}; ah=o.get("asian_handicap") or {}; x12=o.get("1x2") or {}; gl=o.get("goal_line") or {}
        def side(s): return {"home_water":w(s.get("home")),"handicap":s.get("line"),"away_water":w(s.get("away"))} if s else {"home_water":None,"handicap":None,"away_water":None}
        new={"open":side(ah.get("opening")),"mid":{"home_water":None,"handicap":None,"away_water":None},"close":side(ah.get("closing")),"_note":"waters=decimal-1 from 5Dollar Bet365"}
        had=m["meta"].get("has_bet365_odds")
        m["odds"]["asian"]["bet365"]=new
        m["odds"]["euro_home_win"]["bet365"]={"open":(x12.get("opening") or {}).get("home"),"close":(x12.get("closing") or {}).get("home")}
        def ou(s): return {"line":s.get("line"),"over":s.get("over"),"under":s.get("under")} if s else None
        m["odds"]["ou_bet365"]={"open":ou(gl.get("opening")),"close":ou(gl.get("closing"))}
        m["meta"]["has_bet365_odds"]=bool(ah.get("opening") or ah.get("closing"))
        if not had and m["meta"]["has_bet365_odds"]: n_upd+=1
    json.dump(rows,open(allp,"w"),ensure_ascii=False,indent=1)
    summ={"total":len(rows),"with_odds":0,"by_month":{}}
    for mon in sorted({m["meta"]["month"] for m in rows}):
        sub=[m for m in rows if m["meta"]["month"]==mon]
        json.dump(sub,open(f"{MERGED}/{mon}_5dollar-top5.json","w"),ensure_ascii=False,indent=1)
        k=sum(1 for m in sub if m["meta"]["has_bet365_odds"])
        summ["by_month"][mon]={"n":len(sub),"with_odds":k}; summ["with_odds"]+=k
    summ["updated_at"]=now()
    json.dump(summ,open(f"{MERGED}/5dollar_convert_summary.json","w"),ensure_ascii=False,indent=2)
    return n_upd, summ
if run["pulled"]:
    try: run["merge_newly_with_ah"], s = merge(); run["merged_with_odds"]=s["with_odds"]
    except Exception as e: run["errors"].append({"merge":repr(e)})
run["pending_after"]=len([i for i in fixtures if not has_ah(f"{ODDS}/{i}.json") and state["no_ah_tries"].get(i,0)<MAX_NO_AH_TRIES])
run["empty_rounds"]=state["empty_rounds"]
json.dump(state,open(STATE,"w"),indent=2)
open(LOG,"a").write(json.dumps(run,ensure_ascii=False)+"\n")
print(json.dumps(run,ensure_ascii=False,indent=1))
