"""0.3.19：推迟场 / kickoff_jc_conflict / S2-V2·N4-V2 / 探针场人工复核 / 旧 S2·N4 冻结与备注。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import app.db as adb  # noqa: E402
from app import collection_schedule as cs  # noqa: E402
from app import ledger_registry as lr  # noqa: E402
from app import shadow_evaluable as se  # noqa: E402
from app.main import API_VERSION, app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

PROD_DB = ROOT / "data" / "app.db"
V2D3_DB = ROOT / "data" / "v2d3" / "app.db"


def _dt(s):
    return datetime.fromisoformat(s)


def _copy(src: Path, dst: Path) -> Path:
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    o = sqlite3.connect(str(dst))
    s.backup(o)
    s.close()
    o.close()
    return dst


@pytest.fixture()
def v2d3_copy(tmp_path, monkeypatch):
    p = _copy(V2D3_DB, tmp_path / "app.db")
    monkeypatch.setattr(adb, "DB_PATH", p)
    monkeypatch.setattr(adb, "DB_LABEL", "v2d3")
    return p


@pytest.fixture()
def live_copy(tmp_path, monkeypatch):
    p = _copy(PROD_DB, tmp_path / "app.db")
    monkeypatch.setattr(adb, "DB_PATH", p)
    return p


def _table(client, **kw):
    r = client.get("/table/matches", params={"limit": 500, "scope": "all", **kw})
    assert r.status_code == 200, r.text
    return r.json()


def _ids(p: Path) -> dict[str, int]:
    c = sqlite3.connect(str(p))
    try:
        return {k: i for k, i in c.execute("SELECT strategy_key, MAX(id) FROM strategy_defs GROUP BY strategy_key")}
    finally:
        c.close()


def _validate(client, def_id: int) -> dict:
    r = client.post(f"/strategies/{def_id}/validate", json={"scope": "all", "shadow": True})
    assert r.status_code == 202, r.text
    run = client.get(f"/strategies/runs/{r.json()['id']}").json()
    s = run.get("summary") or {}
    return json.loads(s) if isinstance(s, str) else s


def test_api_version():
    assert API_VERSION == "0.3.24"


# ---------------- 1. 推迟场：目标时刻只用到当时已公布的开赛时间 ----------------

O, A = _dt("2026-07-06T08:00:00+08:00"), _dt("2026-07-06T09:00:00+08:00")


@pytest.mark.parametrize("announced,delta_h,want,basis", [
    ("2026-07-06T07:10:00+08:00", 1, "2026-07-06T07:00:00+08:00", "original"),       # 消息晚于目标 → 原定
    ("2026-07-06T07:10:00+08:00", 8, "2026-07-06T00:00:00+08:00", "original"),
    ("2026-07-05T20:00:00+08:00", 8, "2026-07-06T01:00:00+08:00", "announced_new"),  # 消息早于目标 → 新时间
    ("2026-07-06T00:00:00+08:00", 8, "2026-07-06T01:00:00+08:00", "announced_new"),  # 恰好等于目标 → 已公布
    (None, 1, "2026-07-06T07:00:00+08:00", "original_ts_unknown"),                    # 查不到 → 原定 + 标记
])
def test_known_kickoff_target(announced, delta_h, want, basis):
    t, b = cs.known_kickoff_target(O, A, _dt(announced) if announced else None, timedelta(hours=delta_h))
    assert t.isoformat() == want and b == basis


def test_postpone_void_check_threshold():
    assert cs.postpone_void_check(O, A) is None  # 恰好 1h：照实际赛果结算（日092）
    assert cs.postpone_void_check(O, A + timedelta(minutes=1)) == "pending"
    assert cs.POSTPONE_VOID_HOURS is None  # 钩子：澳门时限未核实前不生效


def test_channel_targets_postpone_exception_and_non_exception():
    # 日092：按编号 + 原定 08:00 是例外场 → rule 15:00/22:00；actual 00:00/07:00（07:10 才公布）
    tg = cs.channel_targets(jingcai_date="2026-07-05", kickoff_hour=9, kickoff=A, has_jc_code=True,
                            postpone={"kickoff_original": O, "kickoff_actual": A,
                                      "announced_at": _dt("2026-07-06T07:10:00+08:00")})
    assert tg["rule"]["mid"].isoformat() == "2026-07-05T15:00:00+08:00"
    assert tg["rule"]["close"].isoformat() == "2026-07-05T22:00:00+08:00"
    assert tg["actual"]["t8"].isoformat() == "2026-07-06T00:00:00+08:00"
    assert tg["actual"]["t1"].isoformat() == "2026-07-06T07:00:00+08:00"
    # 非例外场：原定 19:30 → 22:00，10:00 已公布 → mid 用新时间 14:00；close 用新时间 21:00
    o2, a2 = _dt("2026-06-05T19:30:00+08:00"), _dt("2026-06-05T22:00:00+08:00")
    tg2 = cs.channel_targets(jingcai_date="2026-06-05", kickoff_hour=22, kickoff=a2, has_jc_code=True,
                             postpone={"kickoff_original": o2, "kickoff_actual": a2,
                                       "announced_at": _dt("2026-06-05T12:00:00+08:00")})
    assert tg2["rule"]["mid"].isoformat() == "2026-06-05T11:30:00+08:00"  # 12:00 公布晚于 11:30 → 原定
    assert tg2["rule"]["close"].isoformat() == "2026-06-05T21:00:00+08:00"  # 18:30 时已公布 → 新时间


def test_092_schedule_and_match_fields_v2d3(v2d3_copy):
    d = _table(TestClient(app), date_from="2026-07-05", date_to="2026-07-05")
    it = next(i for i in d["items"] if i["match_id"] == "2026-07-05|日092")
    s, m = it["schedule"], it["match"]
    assert s["phase_exception"] is True and s["kickoff_for_exception"] == "2026-07-06T08:00:00+08:00"
    assert (s["mid_target_time"], s["close_target_time"]) == ("2026-07-05T15:00:00+08:00", "2026-07-05T22:00:00+08:00")
    assert (s["mid_real_target_time"], s["close_real_target_time"]) == \
        ("2026-07-06T00:00:00+08:00", "2026-07-06T07:00:00+08:00")
    assert s["postpone_target_basis"] == {"mid": "original", "close": "original"}
    assert m["postponed"] is True and m["kickoff_at"] == "2026-07-06T09:00:00+08:00"
    assert m["kickoff_original"] == "2026-07-06T08:00:00+08:00"
    assert m["kickoff_actual"] == "2026-07-06T09:00:00+08:00"
    assert m["postponed_announced_at"] == "2026-07-06T07:10:00+08:00"
    assert m["postpone_ts_unknown"] is False and m["postpone_void_check"] is None
    assert m["postpone_delay_minutes"] == 60.0 and m["daily_check"] == []
    # 照实际赛果结算：赛果可见、不被推迟挡住
    assert it["settlement_hidden_reason"] != "postpone_void_pending"


def test_092_actual_snapshots_reexported():
    c = sqlite3.connect(f"file:{V2D3_DB}?mode=ro", uri=True)
    rows = dict(c.execute("SELECT point, target_at FROM odds_snapshot WHERE match_id=200 AND book='macau' "
                          "AND market='asian' AND channel='actual'").fetchall())
    c.close()
    assert rows == {"t8": "2026-07-06T00:00:00+08:00", "t1": "2026-07-06T07:00:00+08:00"}


def test_postpone_columns_absent_on_live_is_null(live_copy):
    d = _table(TestClient(app), date_from="2026-07-05", date_to="2026-07-05")
    it = next(i for i in d["items"] if i["match_id"] == "2026-07-05|日092")
    m = it["match"]
    assert m["postponed"] is False and m["kickoff_original"] is None and m["kickoff_jc_conflict"] is None
    assert m["manual_review"] is False and m["daily_check"] == []


def _set_postpone(p: Path, mid: int, delay_min: int, announced: str | None):
    c = sqlite3.connect(str(p))
    ka = c.execute("SELECT kickoff_at FROM matches WHERE id=?", (mid,)).fetchone()[0]
    ko = (_dt(ka) - timedelta(minutes=delay_min)).isoformat()
    c.execute("UPDATE matches SET kickoff_original=?, kickoff_actual=?, postponed_announced_at=?, "
              "postpone_ts_unknown=?, postpone_void_check=? WHERE id=?",
              (ko, ka, announced, 0 if announced else 1, cs.postpone_void_check(_dt(ko), _dt(ka)), mid))
    c.commit()
    c.close()


def test_postpone_pending_excluded_from_validate_and_settlement(v2d3_copy):
    ids = _ids(v2d3_copy)
    client = TestClient(app)
    base = _validate(client, ids["SHADOW_S2_V2"])
    assert base["n_eligible"] == 19 and base["n_postpone_pending"] == 0 and base["postpone_void_hours"] is None
    c = sqlite3.connect(str(v2d3_copy))
    mid, uid = c.execute("SELECT p.match_id, m.match_uid FROM predictions p JOIN matches m ON m.id=p.match_id "
                         "WHERE p.strategy='SHADOW_S2_V2' ORDER BY p.match_id LIMIT 1").fetchone()
    c.close()
    _set_postpone(v2d3_copy, mid, 120, None)  # 推迟 2h、查不到公告
    s = _validate(client, ids["SHADOW_S2_V2"])
    assert s["n_eligible"] == 18 and s["hits"] == 18
    assert s["n_postpone_pending"] == 1 and s["n_void_postponed"] == 0 and s["n_not_evaluable"] == 0
    assert s["postpone_items"][0]["match_id"] == mid and s["postpone_items"][0]["delay_minutes"] == 120.0
    jd = uid.split("|")[0]
    it = next(i for i in _table(client, date_from=jd, date_to=jd, strategy="SHADOW_S2_V2")["items"]
              if i["match_id"] == uid)
    assert it["match"]["postpone_void_check"] == "pending" and it["match"]["postpone_ts_unknown"] is True
    assert set(it["match"]["daily_check"]) >= {"postpone_ts_unknown", "postpone_void_pending"}
    assert it["settlement"] is None and it["settlement_hidden_reason"] == "postpone_void_pending"
    assert it["schedule"]["postpone_target_basis"] == {"mid": "original_ts_unknown", "close": "original_ts_unknown"}


def test_void_postponed_hook(v2d3_copy, monkeypatch):
    ids = _ids(v2d3_copy)
    c = sqlite3.connect(str(v2d3_copy))
    mid = c.execute("SELECT match_id FROM predictions WHERE strategy='SHADOW_S2_V2' ORDER BY match_id LIMIT 1").fetchone()[0]
    c.close()
    _set_postpone(v2d3_copy, mid, 180, "2026-06-01T00:00:00+08:00")
    monkeypatch.setattr(cs, "POSTPONE_VOID_HOURS", 2.0)
    s = _validate(TestClient(app), ids["SHADOW_S2_V2"])
    assert s["n_void_postponed"] == 1 and s["n_postpone_pending"] == 0 and s["n_eligible"] == 18
    assert s["postpone_void_hours"] == 2.0


# ---------------- 2. kickoff_jc_conflict ----------------

def test_kickoff_jc_conflict_v2d3(v2d3_copy):
    items = _table(TestClient(app), date_from="2026-01-01", date_to="2026-12-31")["items"]
    hit = [i["match_id"] for i in items if i["match"]["kickoff_jc_conflict"]]
    assert hit == ["2026-06-05|五201"]
    it = next(i for i in items if i["match_id"] == "2026-06-05|五201")
    assert "kickoff_jc_conflict" in it["match"]["daily_check"]
    # 日092 差 60 分钟 < 90 → false
    assert next(i for i in items if i["match_id"] == "2026-07-05|日092")["match"]["kickoff_jc_conflict"] is False


# ---------------- 3. S2-V2 / N4-V2 + 旧 key 冻结 + 备注 ----------------

def test_s2v2_n4v2_rows_v2d3():
    c = sqlite3.connect(f"file:{V2D3_DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    n = dict(c.execute("SELECT strategy, COUNT(*) FROM predictions WHERE strategy IN "
                       "('SHADOW_S2_V2','SHADOW_N4_V2','SHADOW_S2','SHADOW_N4') GROUP BY 1").fetchall())
    assert n == {"SHADOW_S2_V2": 19}
    defs = {r["strategy_key"]: json.loads(r["config_json"]) for r in c.execute(
        "SELECT strategy_key, config_json FROM strategy_defs WHERE strategy_key IN ('SHADOW_S2_V2','SHADOW_N4_V2')")}
    assert set(defs) == {"SHADOW_S2_V2", "SHADOW_N4_V2"}
    for d in defs.values():
        assert d["extras"]["water_rule"] == "real_water_only" and d["extras"]["odds_source"] == "hist"
        assert d["extras"]["postpone_void_hours"] is None
    for r in c.execute("SELECT rationale_json FROM predictions WHERE strategy='SHADOW_S2_V2'"):
        snap = next(x["feature_snapshot"] for x in json.loads(r[0]) if isinstance(x, dict))
        assert snap["odds_source"] == "hist" and snap["ledger_id"] == "S2-V2" and snap["branch"] == "a_low_open_rise"
    c.close()


def test_old_keys_blocked():
    for k in ("SHADOW_S2", "SHADOW_N4"):
        with pytest.raises(lr.FrozenLedgerError):
            lr.assert_appendable(k)
    lr.assert_appendable("SHADOW_S2_V2")
    lr.assert_appendable("SHADOW_S8")
    import generate_shadow_predictions as g
    conn = sqlite3.connect(":memory:")
    with pytest.raises(lr.FrozenLedgerError):
        g.replace_predictions(conn, "SHADOW_S2", [])
    with pytest.raises(lr.FrozenLedgerError):
        g.append_predictions(conn, [], protect_existing="SHADOW_N4")


def test_generator_main_refuses_old_keys(tmp_path, monkeypatch):
    import generate_shadow_predictions as g
    db = tmp_path / "x.db"
    monkeypatch.setattr(sys, "argv", ["g", "--db", str(db), "--only", "S2,S8"])
    with pytest.raises(lr.FrozenLedgerError):
        g.main()
    assert not db.exists()  # 写库前就拦


def test_ledger_note_on_frozen_old_entries_live(live_copy):
    client = TestClient(app)
    items = _table(client, date_from="2026-01-01", date_to="2026-12-31", strategy="SHADOW_S2")["items"]
    noted = sorted(i["match_id"] for i in items if (i.get("prediction") or {}).get("ledger_note"))
    assert noted == ["2026-06-19|五030", "2026-06-22|一044"]
    with_pred = [i for i in items if i.get("prediction")]
    assert len(with_pred) == 24
    it = next(i for i in items if i["match_id"] == "2026-06-19|五030")
    assert it["prediction"]["ledger_note"] == "触发依据是换算水位"
    assert it["prediction"]["ledger_note_reason"] == "tier_water_trigger"
    n4 = [i for i in _table(client, date_from="2026-01-01", date_to="2026-12-31", strategy="SHADOW_N4")["items"]
          if i.get("prediction")]
    assert len(n4) == 13 and all(i["prediction"]["ledger_note"] == "触发依据是换算水位" for i in n4)
    r = client.get("/matches/2026-06-22|一044/prediction", params={"strategy": "SHADOW_S2"})
    assert r.status_code == 200 and r.json()["ledger_note"] == "触发依据是换算水位"
    r = client.get("/matches/2026-06-22|一044/prediction", params={"strategy": "CFFXDJ_5_V3"})
    assert r.status_code == 200 and r.json()["ledger_note"] is None


def test_live_db_untouched_by_reads():
    h = hashlib.sha256(PROD_DB.read_bytes()).hexdigest()
    c = sqlite3.connect(f"file:{PROD_DB}?mode=ro", uri=True)
    cols = {r[1] for r in c.execute("PRAGMA table_info(matches)")}
    c.close()
    assert not ({"kickoff_original", "manual_review"} & cols)  # 现网不加列
    assert hashlib.sha256(PROD_DB.read_bytes()).hexdigest() == h


# ---------------- 4. 探针场 209/210 人工复核 ----------------

def test_probe_manual_review_flags_v2d3(v2d3_copy):
    items = _table(TestClient(app), date_from="2026-10-05", date_to="2026-10-06")["items"]
    mr = {i["match_id"]: i["match"] for i in items if i["match"]["manual_review"]}
    assert set(mr) == {"probe:1263863300", "probe:515799156"}
    assert all(m["manual_review_reason"] == "ah_sign_mismatch_probe" and "manual_review" in m["daily_check"]
               for m in mr.values())


def test_manual_review_not_in_features():
    import generate_shadow_predictions as g
    c = sqlite3.connect(f"file:{V2D3_DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    mids = set(g.match_ids(c))
    c.close()
    assert not ({209, 210} & mids)


def test_manual_review_reason_in_validate(v2d3_copy):
    ids = _ids(v2d3_copy)
    c = sqlite3.connect(str(v2d3_copy))
    c.execute("INSERT INTO results (match_id, home_goals, away_goals) SELECT 209, 1, 0 "
              "WHERE NOT EXISTS (SELECT 1 FROM results WHERE match_id=209)")
    if not c.execute("SELECT 1 FROM odds_asian WHERE match_id=209 AND book='macau' AND phase='close'").fetchone():
        c.execute("INSERT INTO odds_asian (match_id, book, phase, handicap, home_water, away_water, water_src) "
                  "VALUES (209,'macau','close',0.25,0.9,0.9,'actual')")
    rat = json.loads(c.execute("SELECT rationale_json FROM predictions WHERE strategy='SHADOW_S2_V2' LIMIT 1").fetchone()[0])
    c.execute("INSERT INTO predictions (match_id, strategy, direction, settle_book, rationale_json, stake, stake_rule) "
              "VALUES (209,'SHADOW_S2_V2','主','macau_close',?,1,'shadow_landing_v0')", (json.dumps(rat),))
    c.commit()
    c.close()
    s = _validate(TestClient(app), ids["SHADOW_S2_V2"])
    assert s["n_eligible"] == 19
    assert s["n_not_evaluable"] == 1
    assert s["n_not_evaluable_by_reason"].get("manual_review") == 1
    assert s["n_not_evaluable_by_subreason"] == {"manual_review": {"ah_sign_mismatch_probe": 1}}
    assert se.MANUAL_REVIEW in se.SUBREASONS


def test_flat_columns_stable_v2d3(v2d3_copy):
    rows = TestClient(app).get("/table/matches", params={"date_from": "2026-01-01", "date_to": "2026-12-31",
                                                         "scope": "all", "format": "flat", "limit": 500,
                                                         "strategy": "SHADOW_S2_V2"}).json()["items"]
    assert len({frozenset(r) for r in rows}) == 1
    for k in ("kickoff_original", "kickoff_actual", "postponed_announced_at", "postpone_ts_unknown",
              "postpone_void_check", "kickoff_jc_conflict", "manual_review", "manual_review_reason",
              "schedule_postpone_target_basis_mid", "schedule_kickoff_for_exception", "prediction_ledger_note"):
        assert k in rows[0]
