"""0.3.22：ah.macau 手工主列 + ah.macau_5df 并列；同 lane 异动；现网 macau_5df 空。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app.db as adb  # noqa: E402
from app import table_matches as tm  # noqa: E402
from app.main import API_VERSION, app  # noqa: E402

V2D3_DB = ROOT / "data" / "v2d3" / "app.db"
LIVE_DB = ROOT / "data" / "app.db"
JUNE_UID = "2026-06-01|一001"


@pytest.fixture()
def v2d3(monkeypatch):
    monkeypatch.setattr(adb, "DB_PATH", V2D3_DB)
    monkeypatch.setattr(adb, "DB_LABEL", "v2d3")
    monkeypatch.setattr(adb, "READONLY", True)
    return V2D3_DB


@pytest.fixture()
def live(monkeypatch):
    monkeypatch.setattr(adb, "DB_PATH", LIVE_DB)
    monkeypatch.setattr(adb, "DB_LABEL", "live")
    monkeypatch.setattr(adb, "READONLY", False)
    return LIVE_DB


def test_api_version():
    assert API_VERSION == "0.3.26"


def test_is_5df_classifier():
    assert tm.is_5df_macau_row({"source": "5df_macauslot_history", "water_src": "actual"})
    assert tm.is_5df_macau_row({
        "water_src": "actual",
        "extras_json": '{"source":"5df_macauslot_history","from_channel":"rule","from_point":"mid"}',
    })
    assert not tm.is_5df_macau_row({
        "source": "legacy_import", "water_src": None, "extras_json": None,
    })
    assert not tm.is_5df_macau_row({
        "water_src": None, "extras_json": None, "source": None,
    })


def test_v2d3_macau_5df_mid_available(v2d3):
    c = TestClient(app)
    r = c.get("/table/matches", params={
        "date_from": "2026-06-01", "date_to": "2026-06-01", "scope": "jc", "limit": 5,
    })
    assert r.status_code == 200
    d = r.json()
    assert d["api_version"] == "0.3.26"
    assert "macau_5df" in d["config"]
    assert d["config"]["macau_5df"]["book_lane"]["ah.macau"] == "macau_manual"
    it = next(x for x in d["items"] if x["match_id"] == JUNE_UID)
    m5 = it["ah"]["macau_5df"]["mid"]
    assert m5["available"] is True
    assert m5["source"] == "5df"
    assert m5["water_source"] == "actual"
    assert m5["water_src"] == "actual"
    assert m5["book_lane"] == "macau_5df"
    assert m5["home_water"] is not None
    # primary mid: manual never had mid → no_data / unavailable, no 5DF water
    prim = it["ah"]["macau"]["mid"]
    assert prim["book_lane"] == "macau_manual"
    assert prim.get("home_water") is None
    assert prim.get("water_source") is None
    assert prim["available"] is False or prim.get("hidden_reason") == "no_data"


def test_v2d3_primary_open_close_manual_no_5df_water(v2d3):
    c = TestClient(app)
    r = c.get("/table/matches", params={
        "date_from": "2026-06-01", "date_to": "2026-06-01", "scope": "jc", "limit": 5,
    })
    it = next(x for x in r.json()["items"] if x["match_id"] == JUNE_UID)
    for ph in ("open", "close"):
        prim = it["ah"]["macau"][ph]
        assert prim["book_lane"] == "macau_manual"
        assert prim["available"] is True
        assert prim["line"] is not None
        assert prim.get("home_water") is None  # 手工无水位
        assert prim.get("water_source") is None
        m5 = it["ah"]["macau_5df"][ph]
        assert m5["available"] is True
        assert m5["source"] == "5df"
        assert m5["water_source"] == "actual"
        assert m5["home_water"] is not None


def test_same_lane_no_cross_compare(v2d3):
    """水位异动只在同 lane；macau close 不拿 macau_5df 水位。"""
    c = TestClient(app)
    r = c.get("/table/matches", params={
        "date_from": "2026-06-01", "date_to": "2026-06-01", "scope": "jc", "limit": 5,
    })
    it = next(x for x in r.json()["items"] if x["match_id"] == JUNE_UID)
    # 主列无水 → close.water_move_eligible 不会是 true（两侧都非 actual）
    assert it["ah"]["macau"]["close"].get("water_move_eligible") in (None, False)
    # 5df 列两侧 actual 且同盘时可 eligible
    m5c = it["ah"]["macau_5df"]["close"]
    assert m5c["book_lane"] == "macau_5df"
    if m5c.get("line") == it["ah"]["macau_5df"]["open"].get("line"):
        assert m5c.get("water_move_eligible") is True


def test_live_macau_mid_no_data_and_5df_unavailable(live):
    c = TestClient(app)
    r = c.get("/table/matches", params={
        "date_from": "2026-06-01", "date_to": "2026-06-01", "scope": "jc", "limit": 5,
    })
    assert r.status_code == 200
    d = r.json()
    assert d["api_version"] == "0.3.26"
    it = next(x for x in d["items"] if x["match_id"] == JUNE_UID)
    prim = it["ah"]["macau"]
    assert prim["mid"]["available"] is False
    assert prim["mid"].get("hidden_reason") == "no_data"
    assert prim["open"]["available"] is True
    assert prim["open"].get("home_water") is None
    m5 = it["ah"]["macau_5df"]
    for ph in ("open", "mid", "close"):
        assert m5[ph]["available"] is False
        assert m5[ph].get("hidden_reason") in ("no_data", None) or m5[ph]["available"] is False
        assert m5[ph].get("book_lane") == "macau_5df"


def test_primary_shape_unchanged_keys(v2d3):
    c = TestClient(app)
    r = c.get("/table/matches", params={
        "date_from": "2026-06-01", "date_to": "2026-06-01", "scope": "jc", "limit": 1,
    })
    ah = r.json()["items"][0]["ah"]["macau"]["open"]
    for k in ("line", "home_water", "away_water", "water_source", "basis", "source",
              "available", "hidden_reason", "recorded_at", "target_at"):
        assert k in ah
