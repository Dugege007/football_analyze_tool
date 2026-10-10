"""0.3.21：竞彩 had/hhad 读路径 + 基本面 as_of / 成分闸门 / hhad 决策时刻选线 / 11:10 同窗。"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app.db as adb  # noqa: E402
from app import fundamentals as fund  # noqa: E402
from app import table_matches as tm  # noqa: E402
from app.main import API_VERSION, app, build_stats  # noqa: E402

V2D3_DB = ROOT / "data" / "v2d3" / "app.db"
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


def test_api_version():
    assert API_VERSION == "0.3.26"


# ---------- pure: constituent gate ----------

def test_filter_constituent_kickoff_plus_3h():
    t = datetime(2026, 6, 10, 14, 0, tzinfo=TZ)
    payload = {
        "matches": [
            {"kickoff_at": "2026-06-10T10:00:00+08:00", "id": 1},  # +3h=13:00 ≤ 14:00 ok
            {"kickoff_at": "2026-06-10T12:00:00+08:00", "id": 2},  # +3h=15:00 > 14:00 drop
            {"kickoff_at": "2026-06-09T12:00:00+08:00", "id": 3},
            {"kickoff_at": "2026-06-08T12:00:00+08:00", "id": 4},
        ]
    }
    r = fund.filter_constituent_matches(payload, t, min_n=3)
    assert r["constituent_gate"] == "applied"
    assert r["n_before"] == 4 and r["n_after"] == 3
    assert not r["missing"]
    assert [m["id"] for m in r["usable_payload"]["matches"]] == [1, 3, 4]


def test_filter_constituent_too_thin_is_missing():
    t = datetime(2026, 6, 10, 14, 0, tzinfo=TZ)
    payload = {"matches": [
        {"kickoff_at": "2026-06-10T12:00:00+08:00"},
        {"kickoff_at": "2026-06-09T12:00:00+08:00"},
    ]}
    r = fund.filter_constituent_matches(payload, t, min_n=3)
    assert r["missing"] and r["usable_payload"] is None
    assert r["reason"] == "sample_too_thin_after_constituent_gate"


def test_filter_aggregate_only_gate_unavailable():
    r = fund.filter_constituent_matches(
        {"last10": {"home": {"gf": 1}}, "last6": {}},
        datetime(2026, 6, 10, 14, 0, tzinfo=TZ),
    )
    assert r["constituent_gate"] == "unavailable"
    assert r["usable_payload"] is not None


# ---------- pure: hhad decision-time select ----------

def test_hhad_select_prefers_hist_when_main_after_decision():
    t = datetime(2026, 6, 10, 14, 0, tzinfo=TZ)
    main = {
        "goal_line": -1.0, "home_odds": 2.1, "draw_odds": 3.2, "away_odds": 3.5,
        "captured_at": "2026-06-10T15:00:00+08:00", "line_rev": 1, "source": "sporttery_local",
    }
    hist = [{
        "goal_line": -0.5, "home_odds": 1.9, "draw_odds": 3.4, "away_odds": 3.8,
        "captured_at": "2026-06-10T11:00:00+08:00",
        "superseded_at": "2026-06-10T15:00:00+08:00", "source": "sporttery_local",
    }]
    sel = fund.select_hhad_at_decision(main, hist, t)
    assert not sel["missing"]
    assert sel["decision_line"] == -0.5
    assert sel["from_hist"] is True
    assert sel["post_decision_line_change"] is True
    assert sel["current_line"] == -1.0


def test_hhad_never_defaults_to_future_main():
    t = datetime(2026, 6, 10, 14, 0, tzinfo=TZ)
    main = {
        "goal_line": -1.0, "home_odds": 2.1, "draw_odds": 3.2, "away_odds": 3.5,
        "captured_at": "2026-06-10T16:00:00+08:00", "line_rev": 1,
    }
    sel = fund.select_hhad_at_decision(main, [], t)
    assert sel["missing"] is True


# ---------- JC 11:10 window ----------

def test_jc_1110_window_same_as_asian():
    ok, lag = fund.jc_1110_in_window("2026-06-10T11:00:00+08:00", "2026-06-10")
    assert ok and lag == -10.0
    ok2, lag2 = fund.jc_1110_in_window("2026-06-10T11:20:00+08:00", "2026-06-10")
    assert ok2 and lag2 == 10.0
    bad, _ = fund.jc_1110_in_window("2026-06-10T11:21:00+08:00", "2026-06-10")
    assert not bad


# ---------- API / DDL presence on v2d3 copy ----------

def test_legacy_home_only_incomplete(db):
    c = sqlite3.connect(str(db))
    c.row_factory = sqlite3.Row
    # pick a match with odds_jc_home
    mid = c.execute("SELECT match_id FROM odds_jc_home LIMIT 1").fetchone()[0]
    jd = c.execute("SELECT jingcai_date FROM matches WHERE id=?", (mid,)).fetchone()[0]
    c.close()
    d = TestClient(app).get("/table/matches", params={"date_from": jd, "date_to": jd, "scope": "all"}).json()
    assert d["api_version"] == "0.3.26"
    assert "jc_fundamentals" in d["config"]
    item = next(i for i in d["items"] if i["match"]["match_pk"] == mid)
    # open or close should be incomplete if only home_only
    cell = item["jc_1x2"]["open"]
    if cell.get("source") == "odds_jc_home(home_only)" or cell.get("missing_reason") == "legacy_home_only":
        assert cell["complete"] is False
        assert cell["jc_1x2_incomplete"] is True
        assert cell.get("draw") is None
    assert "jc_hhad" in item
    assert item["jc_hhad"]["open"]["available"] is False or item["jc_hhad"]["open"].get("missing_reason")


def test_stats_meta_manual_seed(db):
    c = sqlite3.connect(str(db))
    c.row_factory = sqlite3.Row
    # require migration already applied on v2d3; if not, skip
    cols = {r[1] for r in c.execute("PRAGMA table_info(stats)")}
    if "source_recent" not in cols:
        c.close()
        pytest.skip("migration not applied yet")
    mid = c.execute(
        "SELECT match_id FROM stats WHERE source_recent='manual_seed' LIMIT 1"
    ).fetchone()
    if not mid:
        c.close()
        pytest.skip("no tagged stats")
    mid = mid[0]
    st = build_stats(c, mid)
    c.close()
    assert st["meta"]["recent"]["source"] == "manual_seed"
    assert st["meta"]["recent"]["as_of"]
    assert st["injury"] is None
    assert "injury" not in st.get("obs", {}) or st["obs"].get("injury") is None


def test_hhad_cell_uses_decision_line(db):
    c = sqlite3.connect(str(db))
    c.row_factory = sqlite3.Row
    if "odds_jc_hhad_line_hist" not in {r[0] for r in c.execute("SELECT name FROM sqlite_master")}:
        c.close()
        pytest.skip("hist table missing")
    mid = c.execute("SELECT id, jingcai_date FROM matches LIMIT 1").fetchone()
    # seed main + hist
    c.execute(
        "INSERT OR REPLACE INTO odds_jc_hhad "
        "(match_id, phase, goal_line, home_odds, draw_odds, away_odds, source, captured_at, line_rev) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (mid["id"], "close", -1.0, 2.0, 3.0, 3.5, "sporttery_local", "2026-06-01T20:00:00+08:00", 1),
    )
    c.execute(
        "INSERT INTO odds_jc_hhad_line_hist "
        "(match_id, phase, goal_line, home_odds, draw_odds, away_odds, captured_at, superseded_at, source) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (mid["id"], "close", -0.5, 1.85, 3.4, 4.0, "2026-06-01T12:00:00+08:00",
         "2026-06-01T20:00:00+08:00", "sporttery_local"),
    )
    c.commit()
    c.close()
    # as_of before line change → decision line -0.5
    d = TestClient(app).get(
        "/table/matches",
        params={"date_from": mid["jingcai_date"], "date_to": mid["jingcai_date"], "scope": "all",
                "as_of": "2026-06-01T15:00:00+08:00"},
    ).json()
    item = next(i for i in d["items"] if i["match"]["match_pk"] == mid["id"])
    h = item["jc_hhad"]["close"]
    assert h["available"] is True
    assert h["goal_line"] == -0.5
    assert h["from_hist"] is True
    assert h["post_decision_line_change"] is True
    assert h["current_line"] == -1.0
