"""Opening status and source fields, Jingcai official first quote, 11:10 fetch fills only the opening quote
(user rules 2026-10-10)."""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

from app import open_status as osx
from app import table_matches as tm

KICK = datetime.fromisoformat("2026-06-06T20:00:00+08:00")
TABLE = {"min_samples": 30, "groups": [
    {"book": "macau", "league": "LeagueA", "samples": 50, "late_p95_lead_minutes": 3000},
    {"book": "macau", "league": "LeagueB", "samples": 10, "late_p95_lead_minutes": 9999},
    {"book": "macau", "league": None, "samples": 500, "late_p95_lead_minutes": 1440},
]}
SCHED = {"mid_target_time": "2026-06-06T12:00:00+08:00", "close_target_time": "2026-06-06T19:00:00+08:00"}


def test_no_table_means_no_check(tmp_path, monkeypatch):
    monkeypatch.setenv("OPEN_TRUNCATION_QUANTILES_PATH", str(tmp_path / "absent.json"))
    assert osx.load_quantile_table() is None
    r = osx.truncation_check(None, "macau", "LeagueA", datetime.fromisoformat("2026-06-06T19:00:00+08:00"), KICK)
    assert r["checked"] is False and r["suspect"] is False and r["reason"] == "quantile_table_missing"


def test_table_is_read_from_file(tmp_path, monkeypatch):
    p = tmp_path / "q.json"
    p.write_text(json.dumps(TABLE), encoding="utf-8")
    monkeypatch.setenv("OPEN_TRUNCATION_QUANTILES_PATH", str(p))
    assert osx.load_quantile_table() == TABLE


def test_book_league_group_and_fallback():
    assert osx.lookup_threshold(TABLE, "macau", "LeagueA")["group"] == "book_league"
    g = osx.lookup_threshold(TABLE, "macau", "LeagueB")  # too few samples -> book only
    assert g["group"] == "book_only" and g["late_p95_lead_minutes"] == 1440
    assert osx.lookup_threshold(TABLE, "macau", None)["group"] == "book_only"
    assert osx.lookup_threshold(TABLE, "crown", "LeagueA") is None


def test_truncation_decision():
    late = datetime.fromisoformat("2026-06-05T12:00:00+08:00")  # 1920 minutes before kickoff
    r = osx.truncation_check(TABLE, "macau", "LeagueA", late, KICK)
    assert r["checked"] and r["suspect"] and r["first_record_lead_minutes"] == 1920.0
    r = osx.truncation_check(TABLE, "macau", "LeagueB", late, KICK)  # book only, threshold 1440
    assert r["checked"] and not r["suspect"]
    st = osx.open_status(available=True, source_kind="official_open", first_captured_at=None,
                         open_time=late.isoformat(), truncation=osx.truncation_check(TABLE, "macau", "LeagueA", late, KICK))
    assert st["status"] == "suspect_truncated" and st["backtest_eligible"] is False
    assert st["source_kind"] == "official_open"


def test_open_status_missing_and_ok():
    st = osx.open_status(available=False, source_kind="manual", first_captured_at="x", open_time=None)
    assert st["status"] == "missing" and st["source_kind"] is None and st["backtest_eligible"] is False
    assert st["first_captured_at"] == "x" and st["open_time_known"] is False
    st = osx.open_status(available=True, source_kind="manual", first_captured_at=None, open_time=None)
    assert st["status"] == "ok" and st["backtest_eligible"] is True
    assert set(st) == set(osx.OPEN_STATUS_FIELDS)


def test_open_flags_source_kinds():
    first = tm._open_flags({"basis": "first_record", "available": True,
                            "recorded_at": "2026-06-01T10:00:00+08:00"}, True, None, SCHED)
    assert first["source_kind"] == "official_open" and first["open_time"] == "2026-06-01T10:00:00+08:00"
    assert first["open_time_known"] is True and first["status"] == "ok"
    api = tm._open_flags({"basis": "api_opening", "available": True}, False, None, SCHED)
    assert api["source_kind"] == "official_open" and api["open_time"] is None and api["open_time_known"] is False
    none = tm._open_flags({"basis": None, "available": False}, False, None, SCHED)
    assert none["status"] == "missing" and none["source_kind"] is None


class _D:
    def __init__(self, had=None, hhad=None):
        self.jc_had = had or {}
        self.jc_hhad = hhad or {}
        self.jc_hhad_hist = {}
        self.legacy_jc = {}
        self.timeline = {}
        self.snap_by_match = {}
        self.snap = {}


def _had(**kw):
    return {"home_odds": 1.8, "draw_odds": 3.3, "away_odds": 4.1, "captured_at": "2026-06-06T11:10:30+08:00",
            "target_at": "2026-06-06T11:10:00+08:00", "source": "sporttery_local", "extras_json": None, **kw}


def test_jc_open_capture_is_not_opening_quote():
    d = _D(had={(1, "open"): _had()})
    c = tm.build_jc_cell(d, 1, "open", SCHED, KICK, KICK, jingcai_date="2026-06-06")
    assert c["available"] is False and c["status"] == "missing" and c["source_kind"] is None
    assert c["home"] is None and c["alt"]["home"] == 1.8 and c["alt"]["kind"] == "first_seen_capture"
    assert c["first_captured_at"] == "2026-06-06T11:10:30+08:00"
    assert c["missing_reason"] == "jc_official_first_quote_missing"
    assert c.get("out_of_window") is None  # the old 11:00 to 11:20 window check is gone


def test_jc_open_official_first_is_opening_quote():
    d = _D(had={(1, "open"): _had(extras_json=json.dumps({"quote_kind": "official_first"}),
                                  open_time="2026-06-04T09:00:00+08:00")})
    c = tm.build_jc_cell(d, 1, "open", SCHED, KICK, KICK, jingcai_date="2026-06-06")
    assert c["available"] is True and c["status"] == "ok" and c["source_kind"] == "official_open"
    assert c["home"] == 1.8 and c["open_time"] == "2026-06-04T09:00:00+08:00" and c["open_time_known"] is True


def test_jc_mid_captured_at_1110_target_no_window_check():
    d = _D(had={(1, "mid"): _had(captured_at="2026-06-06T11:40:00+08:00")})
    c = tm.build_jc_cell(d, 1, "mid", SCHED, KICK, KICK, jingcai_date="2026-06-06")
    assert c["available"] is True and c["home"] == 1.8


def test_live_capture_1110_fills_opening_only():
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root / "scripts" / "live"))
    import live_capture as lc
    r = {"_match_id": 7, "book": "macau", "market": "asian", "open_line_api": -0.5, "open_home": 1.9,
         "open_away": 1.95, "open_draw": None, "open_over": None, "open_under": None,
         "fetched_at": "2026-06-06T11:10:20+08:00", "line_api": -0.75, "price_home": 2.0}
    v = lc.open_fill_vals(r)
    assert v["point"] == "open" and v["channel"] == "rule" and v["line"] == -0.5 and v["price_home"] == 1.9
    assert v["recorded_at"] is None and v["source"] == lc.LIVE_1110_OPEN_SOURCE
    ex = json.loads(v["extras_json"])
    assert ex["api_phase"] == "opening" and ex["open_time"] is None and ex["capture"] != "own"
    assert lc.open_fill_vals({**r, "open_line_api": None}) is None


def test_real_table_bookmakers_without_group_are_not_checked():
    root = Path(__file__).resolve().parents[2]
    t = json.loads((root / "config" / "open_truncation_quantiles.json").read_text(encoding="utf-8"))
    assert t["min_samples"] == 100
    very_late = datetime.fromisoformat("2026-06-06T19:00:00+08:00")
    for book in ("crown", "william", "williamhill", "bet365"):
        r = osx.truncation_check(t, book, "AnyLeague", very_late, KICK, "asian")
        assert r["checked"] is False and r["suspect"] is False and r["reason"] == "no_group_for_book"
        st = osx.open_status(available=True, source_kind="official_open", first_captured_at=None,
                             open_time=very_late.isoformat(), truncation=r)
        assert st["status"] == "ok" and st["truncation_checked"] is False and st["backtest_eligible"] is True
    for book, thr in (("macau", 1689.0), ("pinnacle", 3448.8)):
        for market in ("asian", "euro_1x2", "ou"):
            r = osx.truncation_check(t, book, "AnyLeague", very_late, KICK, market)
            assert r["checked"] and r["suspect"] and r["late_p95_lead_minutes"] == thr and r["group"] == "book_only"


def test_market_limited_group():
    t = {"min_samples": 100, "groups": [{"book": "macau", "league": None, "markets": ["asian"], "samples": 500,
                                         "late_p95_lead_minutes": 100}]}
    assert osx.lookup_threshold(t, "macau", None, "asian") is not None
    assert osx.lookup_threshold(t, "macau", None, "euro_1x2") is None
