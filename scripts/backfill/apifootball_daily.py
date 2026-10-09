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

import os, json, time, datetime, urllib.request, urllib.parse, sys
KEY=os.environ.get('API_FOOTBALL_KEY', '').strip()
if not KEY:
    raise SystemExit('缺少 API_FOOTBALL_KEY：请在仓库根 .env 中填写（见 config.example.env / docs/SENSITIVE.md）')
BASE='https://v3.football.api-sports.io/'
OUT=str(_ODDS_DATA_DIR / "backfill/raw/api-football"); LOG=str(_ODDS_DATA_DIR / "backfill/logs/apifootball_daily_runs.jsonl")
RESERVE=10; LIMIT=100
state={'calls':0,'remaining':None}
def get(path, **params):
    if state['remaining'] is not None and state['remaining']<=RESERVE+1:
        raise SystemExit('reserve reached')
    url=BASE+path+('?'+urllib.parse.urlencode(params) if params else '')
    req=urllib.request.Request(url,headers={'x-apisports-key':KEY})
    with urllib.request.urlopen(req,timeout=40) as r:
        h=r.headers; body=json.load(r)
    state['calls']+=1
    rem=h.get('x-ratelimit-requests-remaining'); state['remaining']=int(rem) if rem and rem.isdigit() else state['remaining']
    rec={'ts':datetime.datetime.now().isoformat(timespec='seconds'),'path':path,'params':params,'results':body.get('results'),'errors':body.get('errors'),'paging':body.get('paging'),'day_remaining':rem,'min_remaining':h.get('x-ratelimit-remaining')}
    with open(LOG,'a') as f: f.write(json.dumps(rec,ensure_ascii=False)+'\n')
    print(json.dumps(rec,ensure_ascii=False)); sys.stdout.flush()
    time.sleep(7)
    return body
def save(name,obj):
    with open(os.path.join(OUT,name),'w') as f: json.dump(obj,f,ensure_ascii=False,indent=1)
