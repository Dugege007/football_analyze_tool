#!/usr/bin/env python3
"""每日备份：SQLite 在线快照 + 原始数据按月归档。
目录：$BACKUP_DIR/
  db/app_YYYY-MM-DD.db.gz      每日快照（保留最近 14 天 + 每月最后一份）
  raw/odds-data_YYYY-MM.tar.gz 当月新增/变动的原始数据归档（每天覆盖重建）
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

import datetime as dt, gzip, hashlib, json, os, shutil, sqlite3, sys, tarfile, time
DB=str(_APP_DB)
RAW_ROOT=str(_ODDS_DATA_DIR)
OUT=str(_BACKUP_DIR)
KEEP_DAYS=14
today=dt.date.today()
mdir=os.path.join(OUT,f"{today:%Y}",f"{today:%Y-%m}")
os.makedirs(os.path.join(mdir,"db"),exist_ok=True); os.makedirs(os.path.join(mdir,"raw"),exist_ok=True)
# 1) 在线快照
tmp=f"/tmp/app_snap_{today}.db"
src=sqlite3.connect(DB); dst=sqlite3.connect(tmp)
with dst: src.backup(dst)
ok=dst.execute("PRAGMA integrity_check").fetchone()[0]
nm=dst.execute("select count(*) from matches").fetchone()[0]
src.close(); dst.close()
if ok!="ok": sys.exit(f"integrity_check failed: {ok}")
gz=os.path.join(mdir,"db",f"app_{today}.db.gz")
with open(tmp,"rb") as f, gzip.open(gz,"wb",compresslevel=6) as g: shutil.copyfileobj(f,g)
os.remove(tmp)
sha=hashlib.sha256(open(gz,"rb").read()).hexdigest()
# 2) 原始数据当月归档（排除 venv/缓存大件之外的 json/csv/md/py 等）
cut=time.mktime(dt.date(today.year,today.month,1).timetuple())
arc=os.path.join(mdir,"raw",f"odds-data_{today:%Y-%m}.tar.gz")
n=0
with tarfile.open(arc+".tmp","w:gz") as t:
    for root,dirs,files in os.walk(RAW_ROOT):
        dirs[:]=[d for d in dirs if d not in(".venv","node_modules","__pycache__")]
        for fn in files:
            p=os.path.join(root,fn)
            try:
                if os.path.getmtime(p)>=cut: t.add(p,arcname=os.path.relpath(p,RAW_ROOT)); n+=1
            except FileNotFoundError: pass
os.replace(arc+".tmp",arc)
# 3) 清理：每日快照保留 14 天，月末（每月最新一份）永久保留
allsnaps=[]
for r,_,fs in os.walk(OUT):
    for fn in fs:
        if fn.startswith("app_") and fn.endswith(".db.gz"):
            d=dt.date.fromisoformat(fn[4:14]); allsnaps.append((d,os.path.join(r,fn)))
latest_per_month={}
for d,p in allsnaps:
    k=(d.year,d.month)
    if k not in latest_per_month or d>latest_per_month[k][0]: latest_per_month[k]=(d,p)
keepm={p for _,p in latest_per_month.values()}
removed=[p for d,p in allsnaps if (today-d).days>KEEP_DAYS and p not in keepm]
for p in removed: os.remove(p)
rep={"date":str(today),"snapshot":gz,"sha256":sha,"bytes":os.path.getsize(gz),"matches":nm,
     "raw_archive":arc,"raw_files":n,"raw_bytes":os.path.getsize(arc),"removed":removed}
open(os.path.join(OUT,"last_backup.json"),"w").write(json.dumps(rep,ensure_ascii=False,indent=1))
print(json.dumps(rep,ensure_ascii=False))
