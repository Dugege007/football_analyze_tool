"""0.3.19：高亮口径 hl_v0.3（只管上色，不进策略）。"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app import table_matches as tm  # noqa: E402

V2D3_DB = ROOT / "data" / "v2d3" / "app.db"


class _FakeD:
    legacy_asian: dict = {}


def _c(line, ws="actual", available=True, basis="rule"):
    return {"line": line, "water_source": ws, "available": available, "basis": basis,
            "home_water": 0.9, "away_water": 0.95}


# ---------------- 1. 凯利 +0.02 ----------------

def test_config_version_and_kelly_margin():
    assert tm.CONFIG_VERSION == "hl_v0.3.1" and tm.KELLY_HL_MARGIN == 0.02
    assert tm.KELLY_HL["medium"]["threshold"] == 1.02 and tm.KELLY_HL["light"]["margin"] == 0.02


# ---------------- 2. 返还率兜底 P25 ----------------

def test_fallback_p25_as_of_and_interpolation():
    f = tm._FallbackP25()
    for i, v in enumerate([0.90, 0.92, 0.94, 0.96, 0.98]):
        f.add(("ah", "macau"), f"2026-06-0{i + 1}", v)
    f.add(("ah", "macau"), "2026-06-09", 0.10)  # 本场及之后的不进
    f.freeze()
    assert f.get(("ah", "macau"), "2026-06-01") == (None, 0)
    p, n = f.get(("ah", "macau"), "2026-06-09")
    assert n == 5 and p == pytest.approx(0.92)  # 线性插值：(5-1)*0.25 = 1 → 0.92
    p, n = f.get(("ah", "macau"), "2026-06-05")  # 0.90, 0.92, 0.94, 0.96 → pos 0.75
    assert n == 4 and p == pytest.approx(0.915)
    assert f.get(("ah", "crown"), "2026-06-09") == (None, 0)


@pytest.fixture(scope="module")
def v2d3_items():
    if not V2D3_DB.exists():
        pytest.skip("no v2d3")
    conn = sqlite3.connect(f"file:{V2D3_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    d = tm.build_table(conn, date_from="2000-01-01", date_to="2099-12-31", scope="all", strategy="none",
                       channel="rule", include_live="none", settlement_version=None, as_of=None,
                       baseline_window_days=None, baseline_min_n=20, limit=100000, offset=0)
    return d


def test_fallback_fields_match_real_water_sample(v2d3_items):
    """0.3.20（hl_v0.3.1）：按 (盘种, 公司, 阶段) 分开算 P10/P25/P90，n = 场次（0.3.19 三阶段合并的口径已换掉）。"""
    items = v2d3_items["items"]
    assert v2d3_items["config_version"] == "hl_v0.3.1"
    cfg = v2d3_items["config"]["return_rate_fallback_hl_v03"]
    assert cfg["min_n"] == 100 and cfg["cell_fields"] == [
        "fallback_p10", "fallback_p25", "fallback_p90", "fallback_n", "fallback_hl_eligible", "fallback_hl_level"]
    review = {it["match_id"] for it in items if it["match"]["manual_review"]}
    it = max(items, key=lambda i: i["match"]["jingcai_date"])
    jd = it["match"]["jingcai_date"]
    for market in ("ah", "x1x2"):
        for b in tm.BOOKS:
            for p in tm.PHASES:
                vals = sorted(o[market][b][p]["return_rate"] for o in items
                              if o["match"]["jingcai_date"] < jd and o["match_id"] not in review
                              and o[market][b][p].get("water_source") == "actual"
                              and o[market][b][p]["return_rate"] is not None)
                c = it[market][b][p]
                assert c["fallback_n"] == len(vals), (market, b, p)
                assert c["fallback_hl_eligible"] is (len(vals) >= 100)
                for name, q in (("p10", 0.10), ("p25", 0.25), ("p90", 0.90)):
                    if vals:
                        assert c[f"fallback_{name}"] == pytest.approx(tm._quantile(vals, q), abs=1e-6)
                    else:
                        assert c[f"fallback_{name}"] is None


def test_fallback_strictly_before_match_date(v2d3_items):
    items = v2d3_items["items"]
    first = min(i["match"]["jingcai_date"] for i in items)
    for it in items:
        if it["match"]["jingcai_date"] == first:
            assert it["ah"]["macau"]["open"]["fallback_n"] == 0


# ---------------- 3. 水位异动初 → 临 ----------------

def test_open_close_same_line_mid_changed():
    cells = {"open": _c(0.5), "mid": _c(0.75), "close": _c(0.5)}
    tm.apply_water_move_flags(cells, _FakeD(), 1, "macau")
    c = cells["close"]
    assert c["water_move_eligible"] is True and c["tier_cross_mid"] is True and c["tier_cross"] is None
    assert all(cells[p]["water_move_eligible"] is None and cells[p]["tier_cross_mid"] is None for p in ("open", "mid"))


def test_open_close_line_differs_is_tier_cross_hover_only():
    cells = {"open": _c(0.5), "mid": _c(0.5), "close": _c(0.75)}
    tm.apply_water_move_flags(cells, _FakeD(), 1, "macau")
    c = cells["close"]
    assert c["water_move_eligible"] is None and c["tier_cross_mid"] is None
    assert c["tier_cross"]["kind"] == "line" and c["tier_cross"]["sides"] == ["line"]
    assert c["tier_cross"]["from"]["line"] == 0.5 and c["tier_cross"]["to"]["line"] == 0.75


def test_mid_unavailable_and_old_mid_rule_gone():
    cells = {"open": _c(0.5), "mid": _c(None, available=False), "close": _c(0.5)}
    tm.apply_water_move_flags(cells, _FakeD(), 1, "macau")
    assert cells["close"]["water_move_eligible"] is True and cells["close"]["tier_cross_mid"] is False
    # hl_v0.2 时 open→mid 同盘会在 mid 格判；hl_v0.3 不再判
    cells = {"open": _c(0.5), "mid": _c(0.5), "close": _c(0.25)}
    tm.apply_water_move_flags(cells, _FakeD(), 1, "macau")
    assert cells["mid"]["water_move_eligible"] is None and cells["close"]["water_move_eligible"] is None


def test_close_unavailable_no_judgement():
    cells = {"open": _c(0.5), "mid": _c(0.5), "close": _c(0.5, available=False)}
    tm.apply_water_move_flags(cells, _FakeD(), 1, "macau")
    assert cells["close"]["water_move_eligible"] is None and cells["close"]["tier_cross"] is None


def test_table_counts_consistent(v2d3_items):
    for it in v2d3_items["items"]:
        for b in tm.BOOKS:
            o, m, c = (it["ah"][b][p] for p in ("open", "mid", "close"))
            if c.get("tier_cross_mid"):
                assert o["line"] == c["line"] and m["line"] != o["line"] and c["water_move_eligible"] is not None
            tc = c.get("tier_cross") or {}
            if tc.get("kind") == "line":
                assert o["line"] != c["line"] and c["water_move_eligible"] is None
            assert m.get("water_move_eligible") is None


def test_highlight_fields_not_used_by_strategy_code():
    import re
    pat = re.compile(r"water_move_eligible|tier_cross_mid|fallback_p(10|25|90)|fallback_hl_(eligible|level)|kelly_highlight")
    hits = [f.name for f in (ROOT / "scripts").glob("*.py") if pat.search(f.read_text(encoding="utf-8"))]
    hits += [f.name for f in (ROOT / "app").glob("*.py") if f.name != "table_matches.py"
             and pat.search(f.read_text(encoding="utf-8"))]
    assert hits == []
