"""0.3.17 决策 1：5DF 开赛 12:00 占位符（collection_schedule.check_kickoff_placeholder + /table/matches 读时 + 导入前校验）。"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import app.db as adb  # noqa: E402
from app import collection_schedule as cs  # noqa: E402
from app.main import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

PROD_DB = ROOT / "data" / "app.db"


def _dt(s):
    return datetime.fromisoformat(s)


def test_not_noon_not_applicable():
    r = cs.check_kickoff_placeholder(_dt("2026-06-06T16:00:00+08:00"), "2026-06-06", 15)
    assert r["status"] == "not_applicable" and r["kickoff_source"] == "5df" and r["kickoff_placeholder"] is None


def test_noon_agrees_with_jingcai_hour_keeps_5df_even_if_date_differs():
    # 世界杯 3 场的真实情形：竞彩只有整点 12，5DF 次日 12:00（in-play tick 与公开赛程均印证）
    r = cs.check_kickoff_placeholder(_dt("2026-06-14T12:00:00+08:00"), "2026-06-13", 12)
    assert r["status"] == "agrees_jingcai_hour" and r["kickoff_source"] == "5df"
    assert r["kickoff"] == _dt("2026-06-14T12:00:00+08:00") and r["minute_known"] is True
    assert r["kickoff_placeholder"] is None and r["date_differs_from_jingcai_synth"] is True


def test_noon_disagrees_with_jingcai_hour_uses_jingcai_hour_floor():
    r = cs.check_kickoff_placeholder(_dt("2026-06-06T12:00:00+08:00"), "2026-06-06", 19)
    assert r["status"] == "placeholder_use_jingcai" and r["kickoff_source"] == "jingcai"
    assert r["kickoff"] == _dt("2026-06-06T19:00:00+08:00") and r["minute_known"] is False
    assert r["kickoff_placeholder"] == "5df_1200" and r["phase_target"] == "hour_floor"
    # 早场特殊带：竞彩 03 点 → 次日
    r2 = cs.check_kickoff_placeholder(_dt("2026-06-06T12:00:00+08:00"), "2026-06-06", 3)
    assert r2["kickoff"] == _dt("2026-06-07T03:00:00+08:00")


def test_noon_vs_full_jingcai_time():
    r = cs.check_kickoff_placeholder(_dt("2026-06-06T12:00:00+08:00"), "2026-06-06", 19,
                                     _dt("2026-06-06T19:35:00+08:00"))
    assert r["status"] == "placeholder_use_jingcai" and r["minute_known"] is True
    assert r["kickoff"] == _dt("2026-06-06T19:35:00+08:00") and r["phase_target"] == "exact_minute"
    ok = cs.check_kickoff_placeholder(_dt("2026-06-06T12:00:00+08:00"), "2026-06-06", 12,
                                      _dt("2026-06-06T12:00:00+08:00"))
    assert ok["status"] == "agrees_jingcai" and ok["kickoff_placeholder"] is None


def test_noon_without_jingcai_time_minute_unknown():
    r = cs.check_kickoff_placeholder(_dt("2026-06-06T12:00:00+08:00"), "2026-06-06", None)
    assert r["status"] == "placeholder_no_jingcai" and r["kickoff_source"] == "5df"
    assert r["minute_known"] is False and r["phase_target"] == "hour_floor"
    assert r["kickoff_placeholder"] == "5df_1200"


@pytest.fixture()
def db(tmp_path, monkeypatch):
    p = tmp_path / "app.db"
    src = sqlite3.connect(f"file:{PROD_DB}?mode=ro", uri=True)
    out = sqlite3.connect(str(p))
    src.backup(out)
    src.close()
    out.close()
    monkeypatch.setattr(adb, "DB_PATH", p)
    return p


def _row(client, date, uid):
    r = client.get("/table/matches", params={"date_from": date, "date_to": date})
    assert r.status_code == 200, r.text
    return next(i for i in r.json()["items"] if i["match_id"] == uid)


def test_api_world_cup_three_keep_5df(db):
    c = TestClient(app)
    for date, uid in (("2026-06-13", "2026-06-13|六008"), ("2026-06-16", "2026-06-16|二020"),
                      ("2026-06-20", "2026-06-20|六036")):
        it = _row(c, date, uid)
        s = it["schedule"]
        assert s["kickoff_check"] == "agrees_jingcai_hour" and s["kickoff_source"] == "5df"
        assert s["kickoff_placeholder"] is None and s["phase_target"] == "exact_minute"
        k = datetime.fromisoformat(it["match"]["kickoff_at"])
        assert (k.hour, k.minute) == (12, 0) and str(k.date()) > date
        # 0.3.18：有竞彩编号、属于 D、开赛 ≥ D 23:00 → 例外场（jc_code_ge_2300），规则目标 = D 15:00 / 22:00
        assert s["phase_exception"] is True and s["exception_rule"] == "jc_code_ge_2300"
        assert s["mid_target_time"] == f"{date}T15:00:00+08:00"
        assert s["close_target_time"] == f"{date}T22:00:00+08:00"
        assert s["mid_real_target_time"] == k.replace(hour=4).isoformat()
        assert s["close_real_target_time"] == k.replace(hour=11).isoformat()


def test_api_placeholder_recomputes_targets(db):
    c0 = sqlite3.connect(str(db))
    c0.execute("UPDATE matches SET kickoff_hour=19 WHERE match_uid='2026-06-13|六008'")
    c0.commit()
    c0.close()
    it = _row(TestClient(app), "2026-06-13", "2026-06-13|六008")
    s = it["schedule"]
    assert s["kickoff_check"] == "placeholder_use_jingcai" and s["kickoff_source"] == "jingcai"
    assert s["kickoff_placeholder"] == "5df_1200" and s["phase_target"] == "hour_floor"
    assert it["match"]["kickoff_at"] == "2026-06-13T19:00:00+08:00"
    assert it["match"]["kickoff_minute_known"] is False and it["match"]["kickoff_source"] == "jingcai"
    assert s["mid_target_time"] == "2026-06-13T11:00:00+08:00"
    assert s["close_target_time"] == "2026-06-13T18:00:00+08:00"


def test_api_placeholder_full_jingcai_time_from_meta(db):
    import json
    c0 = sqlite3.connect(str(db))
    mid = c0.execute("SELECT id FROM matches WHERE match_uid='2026-06-16|二020'").fetchone()[0]
    ex = json.loads(c0.execute("SELECT extras_json FROM match_meta WHERE match_id=?", (mid,)).fetchone()[0] or "{}")
    ex["jingcai_kickoff_at"] = "2026-06-16T20:30:00+08:00"
    c0.execute("UPDATE match_meta SET extras_json=? WHERE match_id=?", (json.dumps(ex), mid))
    c0.commit()
    c0.close()
    s = _row(TestClient(app), "2026-06-16", "2026-06-16|二020")["schedule"]
    assert s["kickoff_source"] == "jingcai" and s["phase_target"] == "exact_minute"
    assert s["mid_target_time"] == "2026-06-16T12:30:00+08:00"
    assert s["close_target_time"] == "2026-06-16T19:30:00+08:00"
