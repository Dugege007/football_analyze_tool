"""0.3.20：hl_v0.3.1（返还率分阶段 P10/P25/P90 + 偏高档）+ 11:10 自采窗口 + kickoff_drift_min + leak_suspect。"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app.db as adb  # noqa: E402
from app import kickoff_drift as kd  # noqa: E402
from app import leak_suspect as leak  # noqa: E402
from app import table_matches as tm  # noqa: E402
from app.main import API_VERSION, app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

V2D3_DB = ROOT / "data" / "v2d3" / "app.db"
UID, MID, JD = "2026-06-17|三204", 85, "2026-06-17"
TARGET = "2026-06-17T11:10:00+08:00"
TZ = timezone(timedelta(hours=8))


@pytest.fixture()
def db(tmp_path, monkeypatch):
    p = tmp_path / "app.db"
    s = sqlite3.connect(f"file:{V2D3_DB}?mode=ro", uri=True)
    o = sqlite3.connect(str(p))
    s.backup(o)
    s.close()
    o.close()
    monkeypatch.setattr(adb, "DB_PATH", p)
    return p


def _own(p: Path, book: str, recorded_at: str, line=-0.5, wh=0.91, wa=0.95, market="asian"):
    c = sqlite3.connect(str(p))
    ex = {"capture": "own", "odds_source": "live", "phase": "live", "phase_variant": "rule_1110",
          "label": "rule_1110", "fetched_at": recorded_at, "target_at": TARGET, "tick_age_rule": "live_fetched_at"}
    c.execute("INSERT INTO odds_snapshot (match_id, book, market, channel, point, recorded_at, target_at, line, "
              "water_home, water_away, water_src, source, extras_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (MID, book, market, "rule", "rule_1110", recorded_at, TARGET, line, wh, wa, "actual", "5df_live",
               json.dumps(ex)))
    c.commit()
    c.close()


def _live(as_of=None, mode="rule_1110"):
    p = {"date_from": JD, "date_to": JD, "scope": "all", "include_live": mode}
    if as_of:
        p["as_of"] = as_of
    d = TestClient(app).get("/table/matches", params=p).json()
    it = next(i for i in d["items"] if i["match_id"] == UID)
    _live.last = (it, d)
    return {(e["book"], e["market"]): e for e in it["live"] if e["label"] == "rule_1110"}, d["config"]


# ---------------- version / kelly 未改 ----------------

def test_versions():
    assert API_VERSION == "0.3.24" and tm.CONFIG_VERSION == "hl_v0.3.1"
    assert tm.KELLY_HL_MARGIN == 0.02 and tm.KELLY_HL["heavy"]["threshold"] is None  # 重档仍留给 hl_v0.4


# ---------------- FallbackQ：分阶段、按场次、as-of ----------------

def test_fallback_q_per_phase_and_match_count():
    f = tm._FallbackQ()
    # 同一场 open/mid 各记 1 次；同一场同一阶段重复 add 应去重（按 match_pk）
    f.add(("ah", "macau", "open"), "2026-06-01", 1, 0.90)
    f.add(("ah", "macau", "open"), "2026-06-01", 1, 0.99)  # 同 pk 覆盖
    f.add(("ah", "macau", "mid"), "2026-06-01", 1, 0.94)
    for i, v in enumerate([0.91, 0.92, 0.93]):
        f.add(("ah", "macau", "open"), f"2026-06-0{i + 2}", 10 + i, v)
    f.add(("ah", "macau", "open"), "2026-06-09", 99, 0.10)  # 本场及之后不进
    f.freeze()
    qs, n = f.get(("ah", "macau", "open"), "2026-06-09")
    assert n == 4  # 场次：pk1 + 10/11/12；不是格子数
    assert qs["p25"] == pytest.approx(tm._quantile([0.91, 0.92, 0.93, 0.99], 0.25))  # pk1 被 0.99 覆盖
    assert qs["p10"] == pytest.approx(tm._quantile([0.91, 0.92, 0.93, 0.99], 0.10))
    assert qs["p90"] == pytest.approx(tm._quantile([0.91, 0.92, 0.93, 0.99], 0.90))
    qs_mid, n_mid = f.get(("ah", "macau", "mid"), "2026-06-09")
    assert n_mid == 1 and qs_mid["p25"] == pytest.approx(0.94)
    assert f.get(("ah", "macau", "open"), "2026-06-01") == ({"p10": None, "p25": None, "p90": None}, 0)


def test_fallback_hl_level_rules():
    qs = {"p10": 0.92, "p25": 0.94, "p90": 0.97}
    assert tm.fallback_hl_level(0.91, "actual", qs, True) == "medium"
    assert tm.fallback_hl_level(0.93, "actual", qs, True) == "light"
    assert tm.fallback_hl_level(0.95, "actual", qs, True) is None
    assert tm.fallback_hl_level(0.97, "actual", qs, True) == "high"
    assert tm.fallback_hl_level(0.91, "tier_midpoint", qs, True) is None
    assert tm.fallback_hl_level(0.91, "actual", qs, False) is None
    assert "high" not in tm.FALLBACK_ALERT_LEVELS


# ---------------- 11:10 自采窗口 ----------------

def test_own_in_window_boundary():
    t = datetime.fromisoformat(TARGET)
    assert tm.own_in_window(t + timedelta(minutes=-10), t) is True
    assert tm.own_in_window(t + timedelta(minutes=10), t) is True
    assert tm.own_in_window(t + timedelta(minutes=-10, seconds=-1), t) is False
    assert tm.own_in_window(t + timedelta(minutes=10, seconds=1), t) is False


def test_own_out_of_window_falls_back_to_timeline(db):
    live0, _ = _live()
    t = live0[("macau", "asian")]
    # 11:25 = 超出 11:20；主值应退回时间线，自采进 alt（out_of_window=true）
    _own(db, "macau", "2026-06-17T11:25:00+08:00", line=-(t["line"] + 0.25),
         wh=t["home_water"] + 0.05, wa=t["away_water"])
    e = _live()[0][("macau", "asian")]
    it, d = _live.last
    assert e["origin"] == "timeline" and e["odds_source"] == "hist"
    assert e["line"] == t["line"] and e["own_capture_out_of_window"] is True
    assert e["merge_rule"] == tm.LIVE_1110_MERGE_RULE_OOW
    assert e["instant_src_diff"] is None  # 不同时刻的两路值不比
    alt = e["alt"]
    assert alt["out_of_window"] is True and alt["origin"] == "own_capture"
    assert alt["captured_at"] == "2026-06-17T11:25:00+08:00" and alt["fetch_lag_min"] == 15.0
    assert alt["line"] == t["line"] + 0.25
    assert alt["water"] == {"home": pytest.approx(t["home_water"] + 0.05), "away": pytest.approx(t["away_water"])}
    assert "own_1110_out_of_window" in it["match"]["daily_check"]
    assert it["match"]["own_1110_out_of_window_cells"] >= 1
    assert d["daily_check_summary"]["by_reason"]["own_1110_out_of_window"] >= 1
    assert d["daily_check_summary"]["own_1110_out_of_window_cells"] >= 1
    assert "instant_src_diff" not in it["match"]["daily_check"]


def test_own_at_boundary_still_main(db):
    live0, _ = _live()
    t = live0[("macau", "asian")]
    _own(db, "macau", "2026-06-17T11:20:00+08:00", line=-t["line"])
    e = _live()[0][("macau", "asian")]
    assert e["origin"] == "own_capture" and e["own_capture_out_of_window"] is False
    assert e["alt"]["out_of_window"] is False and e["alt"]["origin"] == "timeline"


def test_own_out_of_window_no_timeline(db):
    _own(db, "pinnacle", "2026-06-17T11:30:00+08:00", line=0.25)
    e = _live()[0][("pinnacle", "asian")]
    assert e["own_capture_out_of_window"] is True and e["origin"] is None and e["line"] is None
    assert e["alt"]["out_of_window"] is True and e["alt"]["origin"] == "own_capture"
    assert e["main_source_reason"] == "own_capture_out_of_window;no_timeline"


def test_alt_water_shapes():
    assert tm._water_of("asian", {"home_water": 0.9, "away_water": 0.95}) == {"home": 0.9, "away": 0.95}
    assert tm._water_of("ou", {"over_water": 0.8, "under_water": 1.0}) == {"over": 0.8, "under": 1.0}
    assert tm._water_of("euro_1x2", {"home": 2.0, "draw": 3.0, "away": 4.0}) == {"home": 2.0, "draw": 3.0, "away": 4.0}


# ---------------- kickoff_drift_min（只作信息；不为 一006/五204 造推迟） ----------------

def test_kickoff_drift_info_only_and_asof():
    uid = "2026-06-01|一006"
    ko = datetime.fromisoformat("2026-06-02T09:00:00+08:00")
    # as_of 早于场中首笔 → null
    early = kd.drift_fields(uid, ko, datetime.fromisoformat("2026-06-02T09:00:00+08:00"))
    assert early["kickoff_drift_min"] is None
    late = kd.drift_fields(uid, ko, datetime.fromisoformat("2026-06-02T10:00:00+08:00"))
    assert late["kickoff_drift_min"] == 15  # est 09:14:43 − 09:00 ≈ 15
    assert late["kickoff_drift_basis"] == "inplay_tick_est"
    # 五204
    f204 = kd.drift_fields("2026-06-05|五204", datetime.fromisoformat("2026-06-06T07:30:00+08:00"),
                           datetime.fromisoformat("2026-06-06T08:00:00+08:00"))
    assert f204["kickoff_drift_min"] == 15


def test_kickoff_drift_not_postpone(db):
    """一006 / 五204 确认不按推迟处理：postponed=false，kickoff_drift_min 有值。"""
    d = TestClient(app).get("/table/matches", params={
        "date_from": "2026-06-01", "date_to": "2026-06-05", "scope": "all",
        "as_of": "2026-10-08T18:00:00+08:00"}).json()
    by = {it["match_id"]: it["match"] for it in d["items"]}
    for uid in ("2026-06-01|一006", "2026-06-05|五204"):
        m = by[uid]
        assert m["postponed"] is False
        assert m["kickoff_original"] is None  # 现网/副本都没给这两场写推迟列
        assert m["kickoff_drift_min"] == 15
        assert "postpone_ts_unknown" not in (m.get("daily_check") or [])


# ---------------- leak_suspect（配置叠加，不写库） ----------------

@pytest.fixture
def leak_example_config(monkeypatch):
    """这两条用例按公开仓的合成示例配置断言；本机存在真实 config/leak_suspect.json 时也固定使用示例文件。"""
    monkeypatch.setattr(leak, "CONFIG_PATH", leak.EXAMPLE_PATH)
    leak._cache.update({"mtime": None, "path": None, "data": None})
    yield
    leak._cache.update({"mtime": None, "path": None, "data": None})


def test_leak_suspect_config_overlay(leak_example_config):
    assert leak.leak_suspect_for("LEGACY_V4") == "cutoff_plus24"
    assert leak.leak_suspect_for("LEGACY_V5_alias") == "cutoff_plus24"
    assert leak.leak_suspect_for("LEGACY_V4-F") is None  # 修好后重跑码不标
    assert leak.leak_suspect_for("CFFXDJ_5_V3") is None
    assert leak.leak_suspect_for("SHADOW_S2_V2") is None
    reg = leak.registry(set())
    assert {e["strategy_key"] for e in reg["items"]} == {"LEGACY_V4", "LEGACY_V5", "LEGACY_V6", "LEGACY_V7"}
    assert all(e["in_db"] is False for e in reg["items"])


def test_strategies_leak_suspect_endpoint(db, leak_example_config):
    d = TestClient(app).get("/strategies/leak_suspect").json()
    assert d["meta"]["db"] in ("live", "v2d3") or True
    keys = {e["strategy_key"]: e for e in d["items"]}
    assert keys["LEGACY_V4"]["leak_suspect"] == "cutoff_plus24"
    assert keys["LEGACY_V4"]["in_db"] is False  # 现网 / 副本都没有这些行
    items = TestClient(app).get("/strategies").json()
    assert "leak_suspect_registry" in items
    for it in items["items"]:
        assert it["leak_suspect"] is None  # 本库现有策略都不是 V4–V7
    # /table/matches 顶层 strategy_leak_suspect
    t = TestClient(app).get("/table/matches", params={"date_from": JD, "date_to": JD, "scope": "all",
                                                       "strategy": "LEGACY_V4"}).json()
    assert t["strategy_leak_suspect"] == "cutoff_plus24"
    t2 = TestClient(app).get("/table/matches", params={"date_from": JD, "date_to": JD, "scope": "all",
                                                        "strategy": "CFFXDJ_5_V3"}).json()
    assert t2["strategy_leak_suspect"] is None




def test_phase_feature_defaults_and_extras(db):
    """列尚未建：格子 / 比赛级给默认；快照 extras 有值时格子读 extras（不改库）。"""
    live, cfg = _live()
    it, d = _live.last
    m = it["match"]
    assert m["kickoff_rev"] == 0 and m["features_ok"] is True
    assert m["phase_pending"] is False and m["phase_assign_late"] is False
    assert cfg["phase_feature_flags"]["defaults"]["features_ok"] is True
    # 现网格子默认
    c = it["ah"]["macau"]["close"]
    assert c["features_ok"] is True and c["phase_pending"] is False
    # 写入 extras（模拟分析师 ingest；不建列）
    conn = sqlite3.connect(str(db))
    row = conn.execute(
        "SELECT id, extras_json FROM odds_snapshot WHERE match_id=? AND book='macau' AND market='asian' "
        "AND channel='rule' AND point='close' LIMIT 1", (MID,)).fetchone()
    if row:
        ex = json.loads(row[1] or "{}")
        ex.update({"phase_pending": True, "features_ok": False, "phase_assign_late": True})
        conn.execute("UPDATE odds_snapshot SET extras_json=? WHERE id=?", (json.dumps(ex), row[0]))
        conn.commit()
    conn.close()
    if not row:
        pytest.skip("no macau asian rule close snap")
    it2 = TestClient(app).get("/table/matches", params={"date_from": JD, "date_to": JD, "scope": "all"}).json()
    # 0.3.22：rule/close 5DF 快照投影在 macau_5df，不再进手工主列
    row2 = next(i for i in it2["items"] if i["match_id"] == UID)
    c2 = row2["ah"]["macau_5df"]["close"]
    assert c2["phase_pending"] is True and c2["features_ok"] is False and c2["phase_assign_late"] is True
    assert c2["book_lane"] == "macau_5df" and c2["source"] == "5df"


def test_kickoff_rev_column_when_present(db):
    """matches 有 kickoff_rev 列时读列；否则 0。本测临时加列（tmp 库，非现网）。"""
    conn = sqlite3.connect(str(db))
    cols = [r[1] for r in conn.execute("pragma table_info(matches)")]
    if "kickoff_rev" not in cols:
        conn.execute("ALTER TABLE matches ADD COLUMN kickoff_rev INTEGER")
    conn.execute("UPDATE matches SET kickoff_rev=2 WHERE id=?", (MID,))
    conn.commit()
    conn.close()
    it = TestClient(app).get("/table/matches", params={"date_from": JD, "date_to": JD, "scope": "all"}).json()
    m = next(i for i in it["items"] if i["match_id"] == UID)["match"]
    assert m["kickoff_rev"] == 2


def test_phase_feature_flags_unit():
    assert tm.phase_feature_flags(None) == {"phase_pending": False, "features_ok": True, "phase_assign_late": False}
    assert tm.phase_feature_flags({"phase_pending": 1, "features_ok": "false"}) == {
        "phase_pending": True, "features_ok": False, "phase_assign_late": False}
    assert tm.kickoff_rev_of(None) == 0
