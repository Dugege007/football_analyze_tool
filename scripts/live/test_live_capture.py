"""live_capture.py 单测：目标时刻、窗口、入副本（只在临时副本上）、幂等、校验拦截。不调 5DF。"""
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

import json
import shutil
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import live_capture as lc

TZ = lc.TZ
RAW = sorted(Path(str(_ODDS_DATA_DIR / "5dollar/live/all")).glob("*/*.json"))


def _csl(D, items):
    return {"fetched_at": lc.iso(lc.now_cn()), "data": [
        {"id": fid, "league": {"name": "测试联赛"}, "teams": {"home": {"name": h}, "away": {"name": a}},
         "kickoff_ts": int(k.timestamp()), "status": "scheduled",
         "lottery": {"jingcailottery": {"number": num, "odds": None}}} for fid, num, h, a, k in items]}


@pytest.fixture
def env(tmp_path, monkeypatch):
    for n in ("D_ALL", "D_PIN", "D_CSL", "D_PLAN", "D_STAGE", "D_STATE", "D_LOG", "D_SUM", "D_BAK"):
        monkeypatch.setattr(lc, n, tmp_path / n.lower())
    monkeypatch.setattr(lc, "LIVE", tmp_path)
    db = tmp_path / "v2d3" / "app.db"
    db.parent.mkdir()
    shutil.copy(lc.REPLICA_DB, db)
    # 隔离：真实副本可能已建好测试用的竞彩日场次（如 2026-10-08|四001）。只在临时副本里把它们改名，
    # 保证"副本无对应场次"这一前提确定成立（FK 不受影响）。
    c = sqlite3.connect(db)
    c.execute("UPDATE matches SET match_uid = match_uid || ':shadowed_by_test'"
              " WHERE match_uid LIKE '2026-10-08|%' OR match_uid LIKE '2026-10-09|%'")
    c.commit()
    c.close()
    return tmp_path, db


def _plan(D="2026-10-08"):
    b = datetime(2026, 10, 8, tzinfo=TZ)
    return lc.build_plan(D, _csl(D, [
        (1, "周四001", "甲", "乙", b.replace(hour=23)),                        # 例外
        (2, "周四002", "丙", "丁", b.replace(hour=15, minute=30)),             # 白天，精确分钟
        (3, "周四003", "戊", "己", b + timedelta(days=1, hours=12)),           # 次日 12:00 但属于 D → 例外
        (4, "周五001", "庚", "辛", b + timedelta(days=1, hours=15)),           # 属于 D+1，过滤
    ]))


def test_plan_targets(env):
    p = _plan()
    assert p["n_matches"] == 3
    t = {x["target_key"]: x["target_at"][11:16] for x in p["targets"]}
    assert t["2026-10-08|四001|mid|rule"] == "15:00" and t["2026-10-08|四001|close|rule"] == "22:00"
    assert t["2026-10-08|四001|mid|real"] == "15:00" and t["2026-10-08|四001|close|real"] == "22:00"
    assert t["2026-10-08|四002|mid|rule"] == "07:30" and t["2026-10-08|四002|close|rule"] == "14:30"
    assert "2026-10-08|四002|mid|real" not in t
    assert t["2026-10-08|四003|mid|rule"] == "15:00" and t["2026-10-08|四003|close|real"] == "11:00"
    assert all(t[f"2026-10-08|四00{i}|live|rule_1110"] == "11:10" for i in (1, 2, 3))
    snap = {(x["phase"], x["phase_variant"]): (x["channel"], x["point"]) for x in p["targets"]}
    assert snap[("mid", "real")] == ("actual", "t8") and snap[("live", "rule_1110")] == ("rule", "rule_1110")


def test_due_window(env):
    p = _plan()
    at = lc.parse(next(x for x in p["targets"] if x["target_key"] == "2026-10-08|四002|close|rule")["target_at"])
    keys = lambda now: {t["target_key"] for t in lc.due_targets([p], now)[0]}
    assert "2026-10-08|四002|close|rule" in keys(at - timedelta(minutes=2))
    assert "2026-10-08|四002|close|rule" in keys(at + timedelta(minutes=10))
    assert "2026-10-08|四002|close|rule" not in keys(at - timedelta(minutes=3))
    due, missed = lc.due_targets([p], at + timedelta(minutes=11))
    assert "2026-10-08|四002|close|rule" in {t["target_key"] for t in missed}


def _stage(p, key, raw, lag_min=-1.0, mutate=None):
    t = next(x for x in p["targets"] if x["target_key"] == key)
    m = next(x for x in p["matches"] if x["match_uid"] == t["match_uid"])
    fetched = lc.parse(t["target_at"]) + timedelta(minutes=lag_min)
    rows = lc.normalize(raw, t, m, fetched, "all/x.json", "sha", lag_min > 10)
    for r in rows:
        if mutate:
            mutate(r)
        lc.append_jsonl(lc.D_STAGE / f"{p['jingcai_date']}.jsonl", r)
    return rows


@pytest.mark.skipif(not RAW, reason="no real raw capture on disk")
def test_ingest_idempotent_backup_and_refusals(env):
    tmp, db = env
    p = _plan()
    raw = json.loads(RAW[0].read_text())
    n = len(_stage(p, "2026-10-08|四001|close|rule", raw))
    r0 = lc.ingest("2026-10-08", db)
    assert r0["counts"] == {"no_replica_match": n} and r0["backup"] is None
    r1 = lc.ingest("2026-10-08", db, create_missing_matches=True)
    assert r1["written"] == n and r1["backup"] and Path(r1["backup"]).exists()
    r2 = lc.ingest("2026-10-08", db)
    assert r2["written"] == 0 and r2["unchanged"] == n
    c = sqlite3.connect(db)
    got = c.execute("SELECT book, line, water_home, water_src, source, channel, point, json_extract(extras_json,'$.capture'),"
                    " json_extract(extras_json,'$.odds_source') FROM odds_snapshot WHERE source='5df_live' AND market='asian'"
                    " AND book='macau'").fetchall()
    assert got and got[0][3:] == ("actual", "5df_live", "rule", "close", "own", "live")
    assert c.execute("SELECT count(*) FROM odds_snapshot WHERE source='5df_live'").fetchone()[0] == n
    # 0.3.20：快照 extras 必有三字段；比赛行 kickoff_rev（列 + match_meta.extras）
    ex = json.loads(c.execute("SELECT extras_json FROM odds_snapshot WHERE source='5df_live' AND book='macau'"
                              " AND market='asian'").fetchone()[0])
    assert ex["phase_pending"] is False and ex["features_ok"] is True and ex["phase_assign_late"] is False
    assert ex["kickoff_rev"] == 0
    assert "kickoff_rev" in [r[1] for r in c.execute("PRAGMA table_info(matches)")]
    mid = c.execute("SELECT id FROM matches WHERE match_uid='2026-10-08|四001'").fetchone()[0]
    assert c.execute("SELECT kickoff_rev FROM matches WHERE id=?", (mid,)).fetchone()[0] == 0
    meta = json.loads(c.execute("SELECT extras_json FROM match_meta WHERE match_id=?", (mid,)).fetchone()[0])
    assert meta["kickoff_rev"] == 0
    with pytest.raises(SystemExit):
        lc.ingest("2026-10-08", lc.PROD_DB)
    other = tmp / "elsewhere" / "app.db"
    other.parent.mkdir()
    shutil.copy(db, other)
    with pytest.raises(SystemExit):
        lc.ingest("2026-10-08", other)


@pytest.mark.skipif(not RAW, reason="no real raw capture on disk")
def test_water_late_and_conflict(env, monkeypatch):
    tmp, db = env
    p = _plan()
    raw = json.loads(RAW[0].read_text())

    def bad(r):
        if r["book"] == "crown" and r["market"] == "asian":
            r["water_home"] = 1.8
            r["qa_flags"].append("water_home_out_of_range")
    _stage(p, "2026-10-08|四001|close|rule", raw, mutate=bad)
    _stage(p, "2026-10-08|四002|close|rule", raw, lag_min=25)  # 迟到 → 不入
    rep = lc.ingest("2026-10-08", db, create_missing_matches=True)
    assert rep["counts"]["water_out_of_range"] == 1
    assert rep["counts"]["outside_window_not_ingested"] > 0
    c = sqlite3.connect(db)
    assert c.execute("SELECT count(*) FROM odds_snapshot WHERE source='5df_live' AND book='crown' AND market='asian'").fetchone()[0] == 0
    # 非 live 的同键行不被覆盖
    mid = c.execute("SELECT id FROM matches WHERE match_uid='2026-10-08|四001'").fetchone()[0]
    c.execute("UPDATE odds_snapshot SET source='5df_macauslot_history' WHERE match_id=? AND book='macau' AND market='asian'", (mid,))
    c.commit()
    _stage(p, "2026-10-08|四001|close|rule", raw, mutate=lambda r: r.__setitem__("water_away", 0.9) if r["book"] == "macau" else None)
    rep = lc.ingest("2026-10-08", db)
    assert rep["counts"].get("conflict_non_live_row_kept") == 1


@pytest.mark.skipif(not RAW, reason="no real raw capture on disk")
def test_sign_batch_block_and_retract(env):
    tmp, db = env
    D = "2026-10-08"
    b = datetime(2026, 10, 8, tzinfo=TZ)
    p = lc.build_plan(D, _csl(D, [(i, f"周四00{i}", f"H{i}", f"A{i}", b.replace(hour=23)) for i in range(1, 6)]))
    raw = json.loads(RAW[0].read_text())

    def setline(v):
        def f(r):
            if r["market"] == "asian":
                r["line_api"], r["line_home_gives"], r["open_line_api"] = v, -v, -1.0
        return f
    for i in range(1, 6):
        _stage(p, f"{D}|四00{i}|mid|rule", raw, mutate=setline(1.0 if i <= 2 else -1.0))  # 2/5 场中盘符号反
    rep = lc.ingest(D, db, create_missing_matches=True)
    assert rep["sign_check"]["status"] == "pass"  # 还没有临盘，跳过
    for i in range(1, 6):
        _stage(p, f"{D}|四00{i}|close|rule", raw, mutate=setline(-1.0))
    rep = lc.ingest(D, db)
    assert rep["sign_check"]["status"] == "block"            # 40% > 20% → 整批拦
    assert rep["counts"].get("sign_batch_blocked", 0) > 0
    c = sqlite3.connect(db)
    left = c.execute("SELECT count(*) FROM odds_snapshot WHERE source='5df_live' AND channel='rule' AND point='mid'"
                     " AND market='asian' AND book IN (SELECT book FROM odds_snapshot WHERE 0)").fetchone()[0]
    assert left == 0
    # 被拦的中盘已从副本撤下
    n_mid = c.execute("SELECT count(*) FROM odds_snapshot WHERE source='5df_live' AND point='mid' AND market='asian'").fetchone()[0]
    assert n_mid == 0


# ----------------------------------------------------------------------------- 12:00 占位 / 改期（决议第 3 条 + 18:22 补充）

def _csl_s(items):
    return {"fetched_at": lc.iso(lc.now_cn()), "data": [
        {"id": fid, "league": {"name": "测试联赛"}, "teams": {"home": {"name": h}, "away": {"name": a}},
         "kickoff_ts": int(k.timestamp()), "status": st,
         "lottery": {"jingcailottery": {"number": num, "odds": None}}} for fid, num, h, a, k, st in items]}


B = datetime(2026, 10, 8, tzinfo=TZ)
PH = B + timedelta(days=1, hours=12)            # 四003：次日 12:00 = 疑似占位


def _items(k002=B.replace(hour=15, minute=30), k003=PH, st003="scheduled"):
    return [(1, "周四001", "甲", "乙", B.replace(hour=23), "scheduled"),
            (2, "周四002", "丙", "丁", k002, "scheduled"),
            (3, "周四003", "戊", "己", k003, st003)]


def _tk(p):
    return {t["target_key"]: t for t in p["targets"]}


def test_placeholder_pending_targets_and_features(env):
    p = lc.build_plan("2026-10-08", _csl_s(_items()), now=B.replace(hour=9))
    m3 = next(m for m in p["matches"] if m["jc_code"] == "四003")
    assert m3["kickoff_placeholder_suspect"] and m3["placeholder_ever"] and not m3["kickoff_confirmed"]
    t = _tk(p)
    for var, hhmm in (("d1500", "15:00"), ("d2200", "22:00")):
        x = t[f"2026-10-08|四003|pending|{var}"]
        assert x["target_at"][11:16] == hhmm and x["target_at"][:10] == "2026-10-08"
        assert x["phase_pending"] and not x["features_ok"] and (x["channel"], x["point"]) == ("pending", var)
    # 由占位开赛推出的目标（例外场规则列 + 真实列）都不进特征；11:10 不依赖开赛时间 → 照常
    for k in ("mid|rule", "close|rule", "mid|real", "close|real"):
        x = t[f"2026-10-08|四003|{k}"]
        assert x["derived_from_placeholder"] and not x["features_ok"] and x["kickoff_dependent"]
    assert t["2026-10-08|四003|live|rule_1110"]["features_ok"]
    # 非占位场：没有待归阶段目标，全部 features_ok
    assert not any(k.startswith("2026-10-08|四002|pending") for k in t)
    assert all(x["features_ok"] for k, x in t.items() if "|四002|" in k)
    # 待归阶段行：确认前不归阶段
    ps = lc.phase_status("2026-10-08", {"phase": "pending", "target_at": t["2026-10-08|四003|pending|d2200"]["target_at"]}, m3)
    assert ps["phase_pending"] and ps["phase_assigned"] is None and ps["features_ok"] is False


def test_kickoff_change_recomputes_targets_and_logs(env):
    D = "2026-10-08"
    lc.build_plan(D, _csl_s(_items()), now=B.replace(hour=9))
    det = B.replace(hour=10)
    p = lc.build_plan(D, _csl_s(_items(k002=B.replace(hour=17, minute=30))), now=det)
    m2 = next(m for m in p["matches"] if m["jc_code"] == "四002")
    assert m2["kickoff_rev"] == 1 and m2["kickoff_original"][11:16] == "15:30" and m2["kickoff_actual"][11:16] == "17:30"
    # 发现时刻不当推迟公布时刻
    assert m2["postponed_announced_at"] is None and m2["postpone_ts_unknown"] is True
    assert m2["kickoff_known_by"] == lc.iso(det) and m2["kickoff_log"][-1]["announced_at"] is None
    assert m2["postpone_delta_h"] == 2.0 and m2["void_postponed"] is False
    t = _tk(p)
    # 旧临盘 14:30 还没到 → 作废；新临盘 16:30（带 |k1）
    assert "2026-10-08|四002|close|rule" not in t
    assert t["2026-10-08|四002|close|rule|k1"]["target_at"][11:16] == "16:30"
    assert t["2026-10-08|四002|close|rule|k1"]["target_basis"] == "kickoff_known_by_detection"
    assert not t["2026-10-08|四002|close|rule|k1"].get("catchup")
    # 旧中盘 07:30 已过（未抓）→ 留着走 missed；新中盘 09:30 发现时已过 → 立刻补抓
    assert t["2026-10-08|四002|mid|rule"]["target_at"][11:16] == "07:30"
    cu = t["2026-10-08|四002|mid|rule|k1"]
    assert cu["catchup"] and cu["phase_assign_late"] and cu["target_basis"] == "catchup_on_discovery"
    assert cu["planned_target_at"][11:16] == "09:30" and cu["target_at"][11:16] == "10:00"
    ev = [e for e in p["kickoff_log"] if e.get("match_uid") == "2026-10-08|四002"]
    assert any(e.get("kickoff_actual") == m2["kickoff_actual"] and e["postponed_announced_at"] is None for e in ev)
    assert any(e.get("event") == "targets_superseded" for e in ev)
    assert any(e.get("event") == "catchup_on_discovery" for e in ev)
    logged = [json.loads(l) for l in (lc.D_LOG / "kickoff_changes.jsonl").read_text().splitlines()]
    assert any(r.get("kickoff_from", "")[11:16] == "15:30" and r["kickoff_actual"][11:16] == "17:30" for r in logged)
    # 再刷新一次、开赛不变 → 不重复记、不再改 key
    p2 = lc.build_plan(D, _csl_s(_items(k002=B.replace(hour=17, minute=30))), now=B.replace(hour=11))
    assert p2["changed_this_build"] == [] and "2026-10-08|四002|close|rule|k1" in _tk(p2)
    assert next(m for m in p2["matches"] if m["jc_code"] == "四002")["kickoff_rev"] == 1


def test_placeholder_confirmed_after_target_is_late(env):
    D = "2026-10-08"
    lc.build_plan(D, _csl_s(_items()), now=B.replace(hour=9))
    # 16:00 发现开赛改成次日 02:00（非 12:00 → 视为确认；确认时刻 = 发现时刻，晚于 15:00、早于 22:00）
    p = lc.build_plan(D, _csl_s(_items(k003=B + timedelta(days=1, hours=2))), now=B.replace(hour=16))
    m3 = next(m for m in p["matches"] if m["jc_code"] == "四003")
    assert m3["kickoff_confirmed"] and m3["kickoff_confirm_source"] == "5df_kickoff_changed"
    assert not m3["kickoff_placeholder_suspect"] and m3["placeholder_ever"]
    assert m3["postpone_ts_unknown"] is False and m3["postpone_delta_h"] is None   # 占位转真不是推迟
    t = _tk(p)
    assert "2026-10-08|四003|pending|d1500" in t and "2026-10-08|四003|pending|d2200" in t   # 照抓（粘住）
    r15 = {"phase": "pending", "target_at": t["2026-10-08|四003|pending|d1500"]["target_at"]}
    r22 = {"phase": "pending", "target_at": t["2026-10-08|四003|pending|d2200"]["target_at"]}
    s15, s22 = lc.phase_status(D, r15, m3), lc.phase_status(D, r22, m3)
    # 15:00 那时只知道占位 12:00（例外场）→ 归 mid|rule，但确认晚于目标 → late
    assert s15["phase_assigned"] == ["mid|rule"] and s15["phase_assign_late"] is True and not s15["phase_pending"]
    assert s15["kickoff_used"] == lc.iso(PH)
    # 22:00 时已知次日 02:00（例外场）→ close|rule，不 late
    assert s22["phase_assigned"] == ["close|rule"] and s22["phase_assign_late"] is False and s22["features_ok"]
    # 由占位推出的真实列（次日 11:00）：开赛变了 → features_ok=false、superseded
    old = {"phase": "close", "target_at": (PH - timedelta(hours=1)).isoformat(), "derived_from_placeholder": True,
           "kickoff_at": lc.iso(PH)}
    so = lc.phase_status(D, old, m3)
    assert so["features_ok"] is False and so["superseded_by_kickoff_change"] is True
    # 新真实列按次日 02:00 重算：t8 = 18:00（未过）、t1 = 次日 01:00；带 |k1
    assert t["2026-10-08|四003|mid|real|k1"]["target_at"][11:16] == "18:00"
    assert t["2026-10-08|四003|close|real|k1"]["features_ok"] is True


def test_placeholder_confirmed_by_status_started(env):
    D = "2026-10-08"
    lc.build_plan(D, _csl_s(_items()), now=B.replace(hour=9))
    p = lc.build_plan(D, _csl_s(_items(st003="finished")), now=PH + timedelta(hours=2))
    m3 = next(m for m in p["matches"] if m["jc_code"] == "四003")
    assert m3["kickoff_confirmed_at"] == lc.iso(PH) and m3["kickoff_confirm_source"] == "5df_status_started"
    assert m3["kickoff_rev"] == 0 and not m3["kickoff_placeholder_suspect"]
    s22 = lc.phase_status(D, {"phase": "pending", "target_at": B.replace(hour=22).isoformat()}, m3)
    assert s22["phase_assigned"] == ["close|rule"] and s22["phase_assign_late"] is True and s22["features_ok"]
    srow = lc.phase_status(D, {"phase": "close", "target_at": (PH - timedelta(hours=1)).isoformat(),
                                "derived_from_placeholder": True, "kickoff_at": lc.iso(PH)}, m3)
    assert srow["features_ok"] is True and srow["phase_assign_late"] is True
    # 状态没开赛（scheduled）→ 不确认
    p0 = lc.build_plan("2026-10-09", _csl_s([(9, "周五001", "a", "b", PH.replace(day=10), "scheduled")]),
                       now=PH.replace(day=10) + timedelta(hours=1))
    assert next(iter(p0["matches"]))["kickoff_placeholder_suspect"] is True





@pytest.mark.skipif(not RAW, reason="no real raw capture on disk")
def test_ingest_writes_phase_assign_late_on_catchup(env):
    """补抓行入库：extras.phase_assign_late=true、kickoff_rev 来自比赛计划。"""
    tmp, db = env
    D = "2026-10-08"
    items0 = [(2, "周四002", "丙", "丁", B.replace(hour=22), "scheduled")]
    lc.build_plan(D, _csl_s(items0), prev={}, now=B.replace(hour=9))
    p = lc.build_plan(D, _csl_s([(2, "周四002", "丙", "丁", B.replace(hour=18), "scheduled")]),
                      now=B.replace(hour=12))
    cu = _tk(p)["2026-10-08|四002|mid|rule|k1"]
    assert cu["catchup"] and cu["phase_assign_late"]
    raw = json.loads(RAW[0].read_text())
    n = len(_stage(p, cu["target_key"], raw))
    rep = lc.ingest(D, db, create_missing_matches=True)
    assert rep["written"] == n
    c = sqlite3.connect(db)
    ex = json.loads(c.execute(
        "SELECT extras_json FROM odds_snapshot WHERE source='5df_live' AND channel='rule' AND point='mid'"
        " AND book='macau' AND market='asian'").fetchone()[0])
    assert ex["phase_pending"] is False and ex["features_ok"] is True and ex["phase_assign_late"] is True
    assert ex["catchup"] is True and ex["kickoff_rev"] == 1
    mid = c.execute("SELECT id, kickoff_rev FROM matches WHERE match_uid='2026-10-08|四002'").fetchone()
    assert mid[1] == 1
    meta = json.loads(c.execute("SELECT extras_json FROM match_meta WHERE match_id=?", (mid[0],)).fetchone()[0])
    assert meta["kickoff_rev"] == 1
    c.close()


def test_kickoff_advance_gap_catchup(env):
    """原定 22:00、中盘 14:00；改成 18:00、新中盘 10:00；12:00 才发现 → 立刻补抓并 phase_assign_late。"""
    D = "2026-10-08"
    items0 = [(2, "周四002", "丙", "丁", B.replace(hour=22), "scheduled")]
    lc.build_plan(D, _csl_s(items0), now=B.replace(hour=9))
    t0 = _tk(lc.load_plan(D))
    assert t0["2026-10-08|四002|mid|rule"]["target_at"][11:16] == "14:00"
    assert t0["2026-10-08|四002|close|rule"]["target_at"][11:16] == "21:00"
    det = B.replace(hour=12)
    p = lc.build_plan(D, _csl_s([(2, "周四002", "丙", "丁", B.replace(hour=18), "scheduled")]), now=det)
    m2 = next(m for m in p["matches"] if m["jc_code"] == "四002")
    assert m2["kickoff_rev"] == 1 and m2["postponed_announced_at"] is None and m2["postpone_ts_unknown"] is True
    t = _tk(p)
    # 旧中盘 14:00 发现时还没到 → 作废；新中盘 10:00 已过 → 补抓
    assert "2026-10-08|四002|mid|rule" not in t
    cu = t["2026-10-08|四002|mid|rule|k1"]
    assert cu["catchup"] and cu["phase_assign_late"] is True
    assert cu["planned_target_at"][11:16] == "10:00" and cu["target_at"][11:16] == "12:00"
    assert cu["target_basis"] == "catchup_on_discovery" and cu["features_ok"] is True
    # 新临盘 17:00 未到 → 正常加；旧临盘 21:00 作废
    assert "2026-10-08|四002|close|rule" not in t
    assert t["2026-10-08|四002|close|rule|k1"]["target_at"][11:16] == "17:00"
    assert not t["2026-10-08|四002|close|rule|k1"].get("catchup")
    # 发现时刻立刻 due
    due, missed = lc.due_targets([p], det)
    assert "2026-10-08|四002|mid|rule|k1" in {x["target_key"] for x in due}
    assert "2026-10-08|四002|mid|rule|k1" not in {x["target_key"] for x in missed}
    # phase_status：补抓行 phase_assign_late
    ps = lc.phase_status(D, {"phase": "mid", "target_at": cu["target_at"], "catchup": True,
                             "phase_assign_late": True, "planned_target_at": cu["planned_target_at"],
                             "kickoff_at": cu["kickoff_at"], "features_ok": True}, m2)
    assert ps["phase_assign_late"] is True and ps.get("catchup") is True
    # 再刷新开赛不变 → 补抓目标保留、不重复建
    p2 = lc.build_plan(D, _csl_s([(2, "周四002", "丙", "丁", B.replace(hour=18), "scheduled")]),
                       now=B.replace(hour=12, minute=5))
    cu2 = _tk(p2)["2026-10-08|四002|mid|rule|k1"]
    assert cu2["target_at"][11:16] == "12:00" and cu2["catchup"] and cu2["planned_target_at"][11:16] == "10:00"
    assert sum(1 for e in p2["kickoff_log"] if e.get("event") == "catchup_on_discovery"
               and e.get("match_uid") == "2026-10-08|四002") == 1
    # 旧目标已抓过：state 里保留；另起一场从零计划，先抓旧中盘再改期
    D2 = "2026-10-09"
    items_a = [(5, "周五005", "A", "B", B.replace(day=9, hour=22), "scheduled")]
    lc.build_plan(D2, _csl_s(items_a), prev={}, now=B.replace(day=9, hour=9))
    lc.jdump(lc.state_path(D2), {
        "2026-10-09|五005|mid|rule": {"status": "captured",
                                      "fetched_at": lc.iso(B.replace(day=9, hour=13, minute=59)),
                                      "target_at": "2026-10-09T14:00:00+08:00"}})
    p3 = lc.build_plan(D2, _csl_s([(5, "周五005", "A", "B", B.replace(day=9, hour=18), "scheduled")]),
                       now=B.replace(day=9, hour=14, minute=1))
    st3 = lc.jload(lc.state_path(D2), {})
    assert st3["2026-10-09|五005|mid|rule"]["status"] == "captured"   # 已抓旧目标不丢
    assert _tk(p3)["2026-10-09|五005|mid|rule|k1"]["catchup"] is True  # 新中盘仍补抓


@pytest.mark.skipif(not RAW, reason="no real raw capture on disk")
def test_ingest_pending_rows_then_assign(env):
    tmp, db = env
    D = "2026-10-08"
    p = lc.build_plan(D, _csl_s(_items()), now=B.replace(hour=9))
    raw = json.loads(RAW[0].read_text())
    n = len(_stage(p, "2026-10-08|四003|pending|d2200", raw))
    r1 = lc.ingest(D, db, create_missing_matches=True)
    assert r1["written"] == n and r1["backup"]
    c = sqlite3.connect(db)
    q = ("SELECT json_extract(extras_json,'$.phase_pending'), json_extract(extras_json,'$.phase'),"
         " json_extract(extras_json,'$.phase_assigned'), json_extract(extras_json,'$.features_ok'),"
         " json_extract(extras_json,'$.phase_assign_late') FROM odds_snapshot"
         " WHERE source='5df_live' AND channel='pending' AND point='d2200' AND book='macau' AND market='asian'")
    assert c.execute(q).fetchone() == (1, None, None, 0, 0)  # phase_assign_late 未确认时写 false（0.3.20 必写）
    # 两条视图不受影响（只看 channel=rule）
    assert c.execute("SELECT count(*) FROM v_odds_asian_rule WHERE source='5df_live'").fetchone()[0] == 0
    c.close()
    lc.build_plan(D, _csl_s(_items(k003=B + timedelta(days=1, hours=2))), now=B.replace(hour=16))
    r2 = lc.ingest(D, db)
    assert r2["written"] == n
    c = sqlite3.connect(db)
    got = c.execute(q).fetchone()
    assert got[0] == 0 and json.loads(got[2]) == ["close|rule"] and got[3] == 1 and got[4] == 0
    ex = json.loads(c.execute("SELECT extras_json FROM odds_snapshot WHERE source='5df_live' AND channel='pending'"
                              " AND book='macau' AND market='asian'").fetchone()[0])
    assert ex["postponed_announced_at"] is None and ex["kickoff_original"] == lc.iso(PH)
    assert ex["postpone_void_hours"] == 24 and ex["postpone_void_src"] == "OE67/2018-art11"
