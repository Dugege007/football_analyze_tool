#!/usr/bin/env python3
"""竞彩日比赛行（v2d3 研究副本）：按「竞彩日 + 竞彩编号」给 2026-10-08 起的竞彩日建 matches 行。

场次来源：复用足球分析师前向采集已拿到的场次清单 $ODDS_DATA_DIR/5dollar/live/plan/{D}.json
（live_capture.py 从 5DF /v1/chinasportslottery?types=jingcailottery 取，按后端
app.collection_schedule.jingcai_date_from_code 归属竞彩日）。本脚本**不调任何外部接口**（只读文件），
所以不占共享限额。

唯一键：match_uid = "{竞彩日}|{编号}"（如 2026-10-08|四001），与月度导入 import_lib.match_uid、
live_capture 暂存行的 match_uid 同一格式。幂等：INSERT ... ON CONFLICT(match_uid) DO NOTHING，
已有行一律不改（含开赛时间变化：只记 drift 日志，等人决定）。

字段口径（0.3.18/0.3.19）：
- jingcai_date 只认编号（plan 已按编号归属，这里用后端同一函数复核，不从开赛时间反推）；
- weekday = 编号首字（四/五/…，与库内现有行一致），jc_id = 编号去「周」，jc_no = 序号；
- kickoff_at = 5DF 开赛（分钟精确，kickoff_minute_known=1），kickoff_hour = 开赛钟点；
- exception_rule / phase_exception / kickoff_jc_conflict 由 API 读时按 jc_id + kickoff_at 计算，不落列；
- kickoff_jc（竞彩官方时刻）：只有竞彩月度 JSON / 竞彩官网有，5DF CSL 时刻就是 fixture 开赛，
  不能当独立的竞彩时刻 → 置 NULL（与 add_kickoff_jc.py「找不到竞彩条目 → NULL」一致）；
- 队名/联赛：先查 v2d3 teams/team_aliases、leagues/league_aliases（只查不建）；查到用规范名 + id，
  查不到保留 5DF 中文名、id 置 NULL，并列入未匹配清单。

安全：拒绝写现网 data/app.db；要求 DUAL_WRITE_ODDS_ASIAN 关；写前 sqlite backup 整库备份；
每次写入出 manifest（插入的 match id），可 rollback（只删本脚本建的、且没有挂任何子表数据的行）。

用法：
  build_jc_match_rows.py dry-run [--since 2026-10-08]
  build_jc_match_rows.py apply   [--since 2026-10-08]
  build_jc_match_rows.py daemon                 # 常驻：每 10 分钟（:03 :13 … :53）跑一次 apply
  build_jc_match_rows.py ensure-daemon          # 没在跑就后台拉起（幂等）
  build_jc_match_rows.py stop                   # 停常驻（SIGTERM）
  build_jc_match_rows.py rollback --manifest PATH [--apply]
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
import csv
# 跨平台文件锁：Linux 与 macOS 使用 fcntl，Windows 使用 api/app/portable_lock.py 中基于 msvcrt 的实现。
import importlib.util as _pl_util
import pathlib as _pl_pathlib
_pl_spec = _pl_util.spec_from_file_location(
    "_portable_lock", _pl_pathlib.Path(__file__).resolve().parents[1] / "app/portable_lock.py")
fcntl = _pl_util.module_from_spec(_pl_spec)
_pl_spec.loader.exec_module(fcntl)
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
import unicodedata
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import collection_schedule as cs  # noqa: E402

TZ = timezone(timedelta(hours=8))
REPLICA_DB = ROOT / "data" / "v2d3" / "app.db"
PROD_DB = (ROOT / "data" / "app.db").resolve()
PLAN_DIR = Path(str(_ODDS_DATA_DIR / "5dollar/live/plan"))
LOG_DIR = ROOT / "logs"
LOG = LOG_DIR / "jc_match_rows.jsonl"
STATE_DIR = LOG_DIR / "jc_match_rows"
BAK_ROOT = ROOT / "backups"
OUT_DIR = Path(str(_ODDS_DATA_DIR / "backfill/jc-match-rows-2026-10-08"))
PROPOSED_ALIASES = Path(str(_ODDS_DATA_DIR / "research/dc_train/team_aliases_proposed.csv"))
SOURCE_TAG = "jc_match_rows_from_live_plan"
DEFAULT_SINCE = "2026-10-08"
KEEP_BACKUPS = 30


def now_cn() -> datetime:
    return datetime.now(TZ).replace(microsecond=0)


def iso(dt):
    return dt.astimezone(TZ).isoformat() if dt else None


def log(event: str, **kw) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": iso(now_cn()), "event": event, **kw}, ensure_ascii=False) + "\n")


def norm(name):
    if name is None:
        return None
    n = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(name)).strip())
    return n or None


def lookup(conn, kind: str, name):
    """只查不建：alias 精确 → 规范名精确（原文 + NFKC 两种键）。返回 (id, canonical) 或 (None, None)。"""
    tbl, atbl, fk = ("teams", "team_aliases", "team_id") if kind == "team" else ("leagues", "league_aliases", "league_id")
    for key in dict.fromkeys([k for k in (norm(name), (name or "").strip() or None) if k]):
        r = conn.execute(f"SELECT {fk} FROM {atbl} WHERE alias=?", (key,)).fetchone()
        if not r:
            r = conn.execute(f"SELECT id FROM {tbl} WHERE name_zh_canonical=?", (key,)).fetchone()
        if r:
            c = conn.execute(f"SELECT name_zh_canonical FROM {tbl} WHERE id=?", (r[0],)).fetchone()
            return int(r[0]), (c[0] if c else key)
    return None, None


def load_proposed() -> dict[str, str]:
    """只读：待批的别名定稿（未写库），仅用于在未匹配清单里标「批准后可匹配」。"""
    out = {}
    try:
        with PROPOSED_ALIASES.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                for k in (r.get("alias"), r.get("team_canonical")):
                    if k:
                        out[norm(k)] = r.get("team_canonical") or k
    except FileNotFoundError:
        pass
    return out


def parse_dt(s):
    if not s:
        return None
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return (d if d.tzinfo else d.replace(tzinfo=TZ)).astimezone(TZ)


def plan_candidates(since: str) -> tuple[list[dict], list[dict]]:
    """读 plan/{D}.json（D ≥ since）的 matches + missing_mapping（没有 5DF fixture id 的竞彩场也建行）。"""
    cands, problems = [], []
    for p in sorted(PLAN_DIR.glob("????-??-??.json")):
        D = p.stem
        if D < since:
            continue
        try:
            plan = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            problems.append({"jingcai_date": D, "problem": f"plan_unreadable: {e}"})
            continue
        items = [(m, "plan") for m in plan.get("matches") or []] + \
                [(m, "plan_missing_mapping") for m in plan.get("missing_mapping") or []]
        for m, src in items:
            code = (m.get("jc_code") or "").strip().lstrip("周")
            kick = parse_dt(m.get("kickoff_at"))
            uid = f"{D}|{code}"
            prob = None
            if not code or kick is None:
                prob = "missing_code_or_kickoff"
            elif m.get("match_uid") != uid or m.get("jingcai_date") != D:
                prob = f"uid_mismatch plan={m.get('match_uid')}"
            elif cs.jingcai_date_from_code(m.get("jc_number") or code, kick) != D:
                prob = "jingcai_date_from_code_disagrees"
            if prob:
                problems.append({"jingcai_date": D, "match_uid": m.get("match_uid"), "problem": prob})
                continue
            cands.append({**m, "_uid": uid, "_D": D, "_code": code, "_kick": kick, "_src": src,
                          "_plan_built_at": plan.get("built_at"), "_csl_fetched_at": plan.get("csl_fetched_at")})
    # 同一 uid 多份（不该发生）：只留第一份并记问题
    seen, uniq = set(), []
    for c in cands:
        if c["_uid"] in seen:
            problems.append({"jingcai_date": c["_D"], "match_uid": c["_uid"], "problem": "duplicate_uid_in_plan"})
            continue
        seen.add(c["_uid"])
        uniq.append(c)
    return uniq, problems


def build_rows(conn, cands: list[dict]) -> dict:
    proposed = load_proposed()
    new, existing, drift, unmatched_team, unmatched_league = [], [], [], [], []
    for c in cands:
        code, kick = c["_code"], c["_kick"]
        hid, hcan = lookup(conn, "team", c.get("home"))
        aid, acan = lookup(conn, "team", c.get("away"))
        lid, lcan = lookup(conn, "league", c.get("league"))
        for side, name, tid in (("home", c.get("home"), hid), ("away", c.get("away"), aid)):
            if tid is None:
                unmatched_team.append({"name_5df": name, "side": side, "match_uid": c["_uid"], "league_5df": c.get("league"),
                                       "in_pending_alias_final": norm(name) in proposed,
                                       "pending_alias_canonical": proposed.get(norm(name))})
        if lid is None:
            unmatched_league.append({"league_5df": c.get("league"), "match_uid": c["_uid"]})
        row = {
            "match_uid": c["_uid"], "scope": "jingcai", "jingcai_date": c["_D"], "weekday": code[:1],
            "kickoff_hour": kick.hour, "jc_id": code, "jc_no": int(code[1:]) if code[1:].isdigit() else None,
            "competition_name": lcan or c.get("league"), "home_team": hcan or c.get("home"),
            "away_team": acan or c.get("away"), "kickoff_at": iso(kick), "kickoff_minute_known": 1,
            "home_team_id": hid, "away_team_id": aid, "league_id": lid, "kickoff_jc": None,
        }
        extras = {"source": SOURCE_TAG, "ids": {"5df_fixture_id": str(c["fixture_id"])} if c.get("fixture_id") else {},
                  "fixture_id": c.get("fixture_id"), "jc_number": c.get("jc_number"), "kickoff_source": "5df",
                  "kickoff_at": iso(kick), "league_5df": c.get("league"), "home_5df": c.get("home"),
                  "away_5df": c.get("away"), "plan_source": c["_src"], "plan_built_at": c["_plan_built_at"],
                  "csl_fetched_at": c["_csl_fetched_at"], "status_5df": c.get("status"),
                  "kickoff_placeholder_suspect": bool(c.get("kickoff_placeholder_suspect")),
                  "kickoff_jc_note": "no_jingcai_official_time_source (5DF CSL time = fixture kickoff)",
                  "exception_rule": cs.EXCEPTION_RULE,
                  "phase_exception_at_build": bool(cs.phase_exception(c["_D"], kick, kick.hour, True, True))}
        old = conn.execute("SELECT id, kickoff_at, home_team, away_team, jc_id FROM matches WHERE match_uid=?",
                           (c["_uid"],)).fetchone()
        if old:
            existing.append(c["_uid"])
            if old[1] != row["kickoff_at"]:
                drift.append({"match_uid": c["_uid"], "match_id": old[0], "field": "kickoff_at",
                              "db": old[1], "plan": row["kickoff_at"]})
            continue
        new.append({"row": row, "extras": extras})
    return {"new": new, "existing": existing, "drift": drift,
            "unmatched_team": unmatched_team, "unmatched_league": unmatched_league}


def guard(db: Path) -> Path:
    db = db.resolve()
    if db == PROD_DB or db.name != "app.db" or db.parent.name != "v2d3":
        raise SystemExit(f"拒绝写入 {db}：只允许 v2d3 研究副本")
    if os.environ.get("DUAL_WRITE_ODDS_ASIAN", "0").strip().lower() in ("1", "true", "yes", "on"):
        raise SystemExit("DUAL_WRITE_ODDS_ASIAN 被打开；本脚本要求保持关闭")
    return db


def backup(db: Path) -> Path:
    d = BAK_ROOT / f"jc-match-rows-{now_cn():%Y%m%dT%H%M%S}"
    d.mkdir(parents=True, exist_ok=True)
    out = d / "app.db"
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    dst = sqlite3.connect(str(out))
    src.backup(dst)
    dst.close()
    src.close()
    olds = sorted(BAK_ROOT.glob("jc-match-rows-*"))
    for o in olds[:-KEEP_BACKUPS]:
        for f in o.iterdir():
            f.unlink()
        o.rmdir()
    return out


def run(since: str, apply: bool, db: Path = REPLICA_DB, quiet=False) -> dict:
    db = guard(db)
    cands, problems = plan_candidates(since)
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    try:
        res = build_rows(conn, cands)
    finally:
        conn.close()
    rep = {"at": iso(now_cn()), "mode": "apply" if apply else "dry-run", "db": str(db), "since": since,
           "candidates": len(cands), "would_insert" if not apply else "inserted": len(res["new"]),
           "existing_skipped": len(res["existing"]),
           "by_jingcai_date": {}, "drift": res["drift"], "problems": problems,
           "unmatched_team": res["unmatched_team"], "unmatched_league": res["unmatched_league"],
           "backup": None, "manifest": None}
    for n in res["new"]:
        b = rep["by_jingcai_date"].setdefault(n["row"]["jingcai_date"], {"new": 0, "existing": 0})
        b["new"] += 1
    for u in res["existing"]:
        b = rep["by_jingcai_date"].setdefault(u.split("|")[0], {"new": 0, "existing": 0})
        b["existing"] += 1
    rep["new_rows"] = [{k: n["row"][k] for k in ("match_uid", "kickoff_at", "competition_name", "home_team",
                                                  "away_team", "home_team_id", "away_team_id", "league_id")}
                       | {"phase_exception": n["extras"]["phase_exception_at_build"]} for n in res["new"]]
    if apply and res["new"]:
        rep["backup"] = str(backup(db))
        conn = sqlite3.connect(str(db), timeout=30)
        inserted = []
        try:
            conn.execute("BEGIN IMMEDIATE")
            for n in res["new"]:
                r = n["row"]
                cols = list(r)
                cur = conn.execute(f"INSERT INTO matches ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
                                   " ON CONFLICT(match_uid) DO NOTHING", [r[c] for c in cols])
                if cur.rowcount != 1:
                    continue  # 并发下别处先建了：不改
                mid = conn.execute("SELECT id FROM matches WHERE match_uid=?", (r["match_uid"],)).fetchone()[0]
                conn.execute("INSERT OR IGNORE INTO match_meta (match_id, source_file, month, extras_json) VALUES (?,?,?,?)",
                             (mid, SOURCE_TAG, r["jingcai_date"][2:4] + r["jingcai_date"][5:7],
                              json.dumps(n["extras"], ensure_ascii=False, sort_keys=True)))
                inserted.append({"match_id": mid, "match_uid": r["match_uid"]})
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        rep["inserted"] = len(inserted)
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        mf = STATE_DIR / f"manifest-{now_cn():%Y%m%dT%H%M%S}.json"
        mf.write_text(json.dumps({"at": rep["at"], "db": str(db), "backup": rep["backup"], "inserted": inserted},
                                 ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        rep["manifest"] = str(mf)
    log("run", mode=rep["mode"], candidates=rep["candidates"],
        new=rep.get("inserted", rep.get("would_insert")), existing=rep["existing_skipped"],
        by_jingcai_date=rep["by_jingcai_date"], drift=len(rep["drift"]), problems=len(problems),
        unmatched_team=len(rep["unmatched_team"]), unmatched_league=len(rep["unmatched_league"]),
        backup=rep["backup"], manifest=rep["manifest"])
    for d in rep["drift"]:
        log("drift", **d)
    return rep


def rollback(manifest: Path, apply: bool) -> dict:
    m = json.loads(manifest.read_text(encoding="utf-8"))
    db = guard(Path(m["db"]))
    conn = sqlite3.connect(str(db) if apply else f"file:{db}?mode=ro", uri=not apply, timeout=30)
    child = ["odds_snapshot", "odds_asian", "odds_timeline_seg", "predictions", "prediction_legs", "results",
             "stats", "odds_raw", "odds_euro_home", "odds_jc_home", "odds_jc_hhad", "strategy_validation_cache"]
    out = {"manifest": str(manifest), "apply": apply, "delete": [], "kept_has_children": []}
    for it in m["inserted"]:
        meta = conn.execute("SELECT source_file FROM match_meta WHERE match_id=?", (it["match_id"],)).fetchone()
        row = conn.execute("SELECT match_uid FROM matches WHERE id=?", (it["match_id"],)).fetchone()
        if not row or row[0] != it["match_uid"] or not meta or meta[0] != SOURCE_TAG:
            continue
        n = {t: conn.execute(f"SELECT COUNT(*) FROM {t} WHERE match_id=?", (it["match_id"],)).fetchone()[0] for t in child}
        if any(n.values()):
            out["kept_has_children"].append({**it, "children": {k: v for k, v in n.items() if v}})
        else:
            out["delete"].append(it)
    if apply and out["delete"]:
        out["backup"] = str(backup(db))
        conn.execute("BEGIN IMMEDIATE")
        for it in out["delete"]:
            conn.execute("DELETE FROM match_meta WHERE match_id=?", (it["match_id"],))
            conn.execute("DELETE FROM matches WHERE id=?", (it["match_id"],))
        conn.commit()
        log("rollback", manifest=str(manifest), deleted=len(out["delete"]), kept=len(out["kept_has_children"]))
    conn.close()
    return out


# ------------------------------------------------------------------ daemon

PID = STATE_DIR / "daemon.pid"
LOCK = STATE_DIR / "daemon.lock"
RUN_MINUTES = {3, 13, 23, 33, 43, 53}  # 每 10 分钟；10:53 正好在采集 10:50 强制刷新清单之后、11:10 之前


def daemon(since: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lk = open(LOCK, "w")
    try:
        fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("daemon already running")
        return
    PID.write_text(str(os.getpid()))
    stop = {"v": False}
    signal.signal(signal.SIGTERM, lambda *a: stop.__setitem__("v", True))
    log("daemon_start", pid=os.getpid(), run_minutes=sorted(RUN_MINUTES), since=since)
    last = None
    first = True
    while not stop["v"]:
        n = now_cn()
        key = n.strftime("%Y%m%d%H%M")
        if first or (n.minute in RUN_MINUTES and key != last):
            first = False
            last = key
            try:
                run(since, apply=True)
            except SystemExit as e:
                log("run_refused", error=str(e))
            except Exception as e:  # noqa: BLE001
                log("run_error", error=str(e)[:500])
            (STATE_DIR / "heartbeat.json").write_text(json.dumps({"ts": iso(now_cn()), "pid": os.getpid()}) + "\n")
        time.sleep(20)
    log("daemon_stop", pid=os.getpid())


def ensure_daemon(since: str) -> str:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lk = open(LOCK, "w")
    try:
        fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(lk, fcntl.LOCK_UN)
        lk.close()
    except BlockingIOError:
        return "running pid=" + PID.read_text().strip()
    out = open(LOG_DIR / "jc_match_rows_daemon.out", "a")
    p = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "daemon", "--since", since],
                         stdout=out, stderr=out, stdin=subprocess.DEVNULL, start_new_session=True, cwd=str(ROOT))
    return f"started pid={p.pid}"


def stop_daemon() -> str:
    if not PID.exists():
        return "not running (no pid file)"
    pid = int(PID.read_text().strip())
    try:
        os.kill(pid, signal.SIGTERM)
        return f"SIGTERM sent to {pid}（≤20s 内退出）"
    except ProcessLookupError:
        return f"pid {pid} not running"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for n in ("dry-run", "apply", "daemon", "ensure-daemon"):
        s = sub.add_parser(n)
        s.add_argument("--since", default=DEFAULT_SINCE)
        if n in ("dry-run", "apply"):
            s.add_argument("--out", help="把报告 JSON 另存到此路径")
    sub.add_parser("stop")
    r = sub.add_parser("rollback")
    r.add_argument("--manifest", required=True)
    r.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd in ("dry-run", "apply"):
        rep = run(a.since, apply=a.cmd == "apply")
        txt = json.dumps(rep, ensure_ascii=False, indent=1)
        if a.out:
            Path(a.out).write_text(txt + "\n", encoding="utf-8")
        print(txt)
    elif a.cmd == "daemon":
        daemon(a.since)
    elif a.cmd == "ensure-daemon":
        print(ensure_daemon(a.since))
    elif a.cmd == "stop":
        print(stop_daemon())
    elif a.cmd == "rollback":
        print(json.dumps(rollback(Path(a.manifest), a.apply), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
