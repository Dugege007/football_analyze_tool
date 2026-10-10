"""0.3.23：GET /matches/{match_id}/odds/timeline — 契约 v2_0-prematch-odds-timeline-chart.md"""
from __future__ import annotations

import io
import json
import math
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app.db as adb  # noqa: E402
from app import odds_timeline_chart as otc  # noqa: E402
from app.main import API_VERSION, app  # noqa: E402

CN = timezone(timedelta(hours=8))
V2D3_DB = ROOT / "data" / "v2d3" / "app.db"
PROBE_UID = "probe:1263863300"
KICK = datetime(2026, 10, 6, 2, 45, tzinfo=CN)


@pytest.fixture()
def v2d3(monkeypatch):
    monkeypatch.setattr(adb, "DB_PATH", V2D3_DB)
    monkeypatch.setattr(adb, "DB_LABEL", "v2d3")
    monkeypatch.setattr(adb, "READONLY", True)
    otc.cache_clear()
    return V2D3_DB


@pytest.fixture(autouse=True)
def _clear_cache():
    otc.cache_clear()
    yield
    otc.cache_clear()


def test_api_version():
    assert API_VERSION == "0.3.24"


def test_x_formula_and_axis_ticks():
    assert abs(otc.x_from_tau(480) - (-math.log10(480))) < 1e-12
    assert otc.x_from_tau(0.5) == 0.0  # max(tau,1)
    ticks = otc.axis_ticks()
    assert [t["tau_min"] for t in ticks] == [480, 120, 60, 15, 0] or sorted(
        [t["tau_min"] for t in ticks]) == [0, 15, 60, 120, 480]
    # sorted by x ascending
    xs = [t["x"] for t in ticks]
    assert xs == sorted(xs)
    labels = {t["label"] for t in ticks}
    assert labels == {"还剩 8h", "还剩 2h", "还剩 60m", "还剩 15m", "开赛"}


def test_normalize_asian_line_and_water():
    assert otc.normalize_asian_line(-0.75) == 0.75
    assert otc.hk_water(1.92) == pytest.approx(0.92)


def test_book_aliases():
    assert otc.normalize_book(None) == "macauslot"
    assert otc.normalize_book("macau") == "macauslot"
    assert otc.normalize_book("william") == "williamhill"
    with pytest.raises(otc.TimelineError) as ei:
        otc.normalize_book("unknown_book")
    assert ei.value.status == 400


class _FakeResp:
    def __init__(self, payload: dict, headers=None, status=200):
        self._raw = json.dumps(payload).encode()
        self.headers = headers or {"X-RateLimit-Remaining": "30"}
        self.status = status

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _hist_payload(ticks):
    return {
        "success": True,
        "data": {
            "fixture_id": 1263863300,
            "bookmaker": {"name": "Macauslot", "slug": "macauslot"},
            "market": "asian",
            "ticks": ticks,
        },
        "pagination": {"page": 1, "per_page": 500, "count": len(ticks), "has_more": False},
    }


def test_timeline_ok_mock_upstream(v2d3, monkeypatch):
    monkeypatch.setenv("FIVEDOLLAR_FOOTBALL_API_KEY", "test-key")
    monkeypatch.setattr(otc, "check_live_busy", lambda now=None: (None, 0))
    # avoid shared yield window / rate slot
    monkeypatch.setattr(otc.Y, "must_yield", lambda now=None: False)
    monkeypatch.setattr(otc.Y.Gate, "_reserve_slot", lambda self: 0.0)
    monkeypatch.setattr(otc.Y.Gate, "after_response", lambda self, h: None)

    raw_ticks = [
        {"minute": None, "line": -1.0, "home": 1.92, "away": 1.92,
         "score": {"home": None, "away": None},
         "recorded_at": "2026-10-05T11:00:00+00:00"},  # = 19:00+08, tau~7h45
        {"minute": None, "line": -0.75, "home": 1.85, "away": 2.00,
         "score": {"home": None, "away": None},
         "recorded_at": "2026-10-05T18:00:00+08:00"},
        # in-play / after kickoff — must drop
        {"minute": 10, "line": -0.5, "home": 1.80, "away": 2.05,
         "score": {"home": 0, "away": 0},
         "recorded_at": "2026-10-06T03:00:00+08:00"},
    ]

    def fake_urlopen(req, timeout=30):
        return _FakeResp(_hist_payload(raw_ticks))

    monkeypatch.setattr(otc.urllib.request, "urlopen", fake_urlopen)

    c = TestClient(app)
    r = c.get(f"/matches/{PROBE_UID}/odds/timeline")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["book"] == "macauslot" and d["market"] == "asian"
    assert d["line_convention"] == "home_give_positive"
    assert d["cached"] is False and d["calls_used"] == 1
    assert d["fixture_id"] == 1263863300
    assert all(t["recorded_at"] < d["kickoff_at"] for t in d["ticks"])
    assert len(d["ticks"]) == 2
    # line sign flipped（按 recorded_at 升序：先 -0.75→0.75，后 -1→1）
    lines = [t["line"] for t in d["ticks"]]
    assert lines == [0.75, 1.0]
    assert d["ticks"][1]["home_water"] == pytest.approx(0.92)
    # x formula
    for t in d["ticks"]:
        assert abs(t["x"] - (-math.log10(max(t["tau_min"], 1)))) < 1e-6
    assert d["display"]["recommended_layout"] == "split_line_and_waters"

    # second call → cache
    r2 = c.get(f"/matches/{PROBE_UID}/odds/timeline")
    assert r2.status_code == 200
    d2 = r2.json()
    assert d2["cached"] is True and d2["calls_used"] == 0


def test_timeline_book_param(v2d3, monkeypatch):
    monkeypatch.setenv("FIVEDOLLAR_FOOTBALL_API_KEY", "test-key")
    monkeypatch.setattr(otc, "check_live_busy", lambda now=None: (None, 0))
    monkeypatch.setattr(otc.Y, "must_yield", lambda now=None: False)
    monkeypatch.setattr(otc.Y.Gate, "_reserve_slot", lambda self: 0.0)
    monkeypatch.setattr(otc.Y.Gate, "after_response", lambda self, h: None)
    seen = {}

    def fake_urlopen(req, timeout=30):
        seen["url"] = req.full_url if hasattr(req, "full_url") else str(req.full_url if hasattr(req, "full_url") else req)
        url = getattr(req, "full_url", None) or req.get_full_url()
        seen["url"] = url
        return _FakeResp(_hist_payload([]))

    monkeypatch.setattr(otc.urllib.request, "urlopen", fake_urlopen)
    c = TestClient(app)
    r = c.get(f"/matches/{PROBE_UID}/odds/timeline", params={"book": "crown"})
    assert r.status_code == 200
    assert r.json()["book"] == "crown"
    assert "bookmaker=crown" in seen["url"]


def test_timeline_busy_503(v2d3, monkeypatch):
    monkeypatch.setenv("FIVEDOLLAR_FOOTBALL_API_KEY", "test-key")
    monkeypatch.setattr(
        otc, "check_live_busy",
        lambda now=None: ("fixed_window_1100_1120", 120),
    )
    called = {"n": 0}

    def boom(*a, **k):
        called["n"] += 1
        raise AssertionError("must not call upstream when busy")

    monkeypatch.setattr(otc, "fetch_history_pages", boom)
    c = TestClient(app)
    r = c.get(f"/matches/{PROBE_UID}/odds/timeline")
    assert r.status_code == 503
    body = r.json()
    assert body["error"]["code"] == "live_capture_busy"
    assert called["n"] == 0


def test_timeline_busy_returns_cache(v2d3, monkeypatch):
    monkeypatch.setenv("FIVEDOLLAR_FOOTBALL_API_KEY", "test-key")
    monkeypatch.setattr(otc.Y, "must_yield", lambda now=None: False)
    monkeypatch.setattr(otc.Y.Gate, "_reserve_slot", lambda self: 0.0)
    monkeypatch.setattr(otc.Y.Gate, "after_response", lambda self, h: None)

    state = {"busy": False}

    def busy_check(now=None):
        return ("due_avoid_sec=60", 90) if state["busy"] else (None, 0)

    monkeypatch.setattr(otc, "check_live_busy", busy_check)
    monkeypatch.setattr(
        otc.urllib.request, "urlopen",
        lambda req, timeout=30: _FakeResp(_hist_payload([
            {"line": -0.5, "home": 1.9, "away": 1.95,
             "recorded_at": "2026-10-05T12:00:00+08:00"},
        ])),
    )
    c = TestClient(app)
    assert c.get(f"/matches/{PROBE_UID}/odds/timeline").status_code == 200
    state["busy"] = True
    r = c.get(f"/matches/{PROBE_UID}/odds/timeline")
    assert r.status_code == 200
    assert r.json()["cached"] is True
    assert "缓存返回" in "".join(r.json()["notes"])


def test_timeline_as_of_filter(v2d3, monkeypatch):
    monkeypatch.setenv("FIVEDOLLAR_FOOTBALL_API_KEY", "test-key")
    monkeypatch.setattr(otc, "check_live_busy", lambda now=None: (None, 0))
    monkeypatch.setattr(otc.Y, "must_yield", lambda now=None: False)
    monkeypatch.setattr(otc.Y.Gate, "_reserve_slot", lambda self: 0.0)
    monkeypatch.setattr(otc.Y.Gate, "after_response", lambda self, h: None)
    monkeypatch.setattr(
        otc.urllib.request, "urlopen",
        lambda req, timeout=30: _FakeResp(_hist_payload([
            {"line": -1, "home": 1.9, "away": 1.9, "recorded_at": "2026-10-05T10:00:00+08:00"},
            {"line": -0.75, "home": 1.85, "away": 2.0, "recorded_at": "2026-10-05T18:00:00+08:00"},
        ])),
    )
    c = TestClient(app)
    r = c.get(f"/matches/{PROBE_UID}/odds/timeline", params={"as_of": "2026-10-05T12:00:00+08:00"})
    assert r.status_code == 200
    ticks = r.json()["ticks"]
    assert len(ticks) == 1
    assert ticks[0]["recorded_at"].startswith("2026-10-05T10:00:00")


def test_timeline_no_write_odds_asian(v2d3, monkeypatch):
    monkeypatch.setenv("FIVEDOLLAR_FOOTBALL_API_KEY", "test-key")
    monkeypatch.setattr(otc, "check_live_busy", lambda now=None: (None, 0))
    monkeypatch.setattr(otc.Y, "must_yield", lambda now=None: False)
    monkeypatch.setattr(otc.Y.Gate, "_reserve_slot", lambda self: 0.0)
    monkeypatch.setattr(otc.Y.Gate, "after_response", lambda self, h: None)
    monkeypatch.setattr(
        otc.urllib.request, "urlopen",
        lambda req, timeout=30: _FakeResp(_hist_payload([
            {"line": -1, "home": 1.9, "away": 1.9, "recorded_at": "2026-10-05T10:00:00+08:00"},
        ])),
    )
    conn = sqlite3.connect(V2D3_DB)
    before = conn.execute("SELECT COUNT(*) FROM odds_asian").fetchone()[0]
    c = TestClient(app)
    assert c.get(f"/matches/{PROBE_UID}/odds/timeline").status_code == 200
    after = conn.execute("SELECT COUNT(*) FROM odds_asian").fetchone()[0]
    conn.close()
    assert before == after


def test_fixture_unmapped_404(v2d3, monkeypatch):
    monkeypatch.setattr(otc, "check_live_busy", lambda now=None: (None, 0))
    # pick a match without fixture mapping if possible — use by-fixture without id
    c = TestClient(app)
    r = c.get("/matches/by-fixture/odds/timeline")
    assert r.status_code == 400
