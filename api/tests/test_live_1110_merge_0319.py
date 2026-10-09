"""0.3.19 追加 1（18:15 口径）：即时（11:10）自采优先，时间线值进 alt={line, water, tick_at}；差异 → instant_src_diff。"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app.db as adb  # noqa: E402
from app.main import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

V2D3_DB = ROOT / "data" / "v2d3" / "app.db"
UID, MID, JD = "2026-06-17|三204", 85, "2026-06-17"
TARGET = "2026-06-17T11:10:00+08:00"


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


RULE = "own_capture_first;alt=timeline"


def _tl():
    """时间线表在 11:10 的澳门亚盘（API 记法）。"""
    return _live()[0][("macau", "asian")]


def test_timeline_only_has_source_no_alt(db):
    live, cfg = _live()
    e = live[("macau", "asian")]
    assert e["origin"] == "timeline" and e["odds_source"] == "hist" and e["capture"] is None
    assert e["captured_at"] == e["recorded_at"] and e["alt"] is None and e["instant_src_diff"] is None
    assert e["merge_rule"] == RULE and cfg["live_rule_1110_merge_rule"] == RULE
    it, _ = _live.last
    assert it["match"]["instant_src_diff"] is None and "instant_src_diff" not in it["match"]["daily_check"]


def test_own_wins_timeline_in_alt_same_values_no_diff(db):
    t = _tl()
    _own(db, "macau", "2026-06-17T11:10:40+08:00", line=-t["line"], wh=t["home_water"] + 0.03, wa=t["away_water"] - 0.02)
    e = _live()[0][("macau", "asian")]
    assert e["origin"] == "own_capture" and e["odds_source"] == "live" and e["capture"] == "own"
    assert e["captured_at"] == "2026-06-17T11:10:40+08:00" and e["fetch_lag_min"] == pytest.approx(0.67, abs=0.01)
    assert e["alt"] == {"line": t["line"], "water": {"home": t["home_water"], "away": t["away_water"]},
                        "tick_at": t["recorded_at"],
                        # 0.3.20：alt 统一结构多带来源 / 抓取时间 / 是否超窗
                        "origin": "timeline", "captured_at": t["recorded_at"], "fetch_lag_min": None,
                        "out_of_window": False}
    assert e["own_capture_out_of_window"] is False
    assert e["home_water"] == pytest.approx(t["home_water"] + 0.03)  # 自采值不被时间线覆盖
    assert e["instant_src_diff"] is False  # 0.03 不算超
    it, d = _live.last
    assert it["match"]["instant_src_diff"] is False and d["daily_check_summary"]["by_reason"].get("instant_src_diff") is None


def test_own_after_1110_still_used(db):
    """18:15 口径：两边都有就以自采为准（自采 T+1~3 分钟到达也用，fetch_lag_min 标出）。"""
    _own(db, "macau", "2026-06-17T11:12:00+08:00", line=-_tl()["line"])
    e = _live()[0][("macau", "asian")]
    assert e["origin"] == "own_capture" and e["fetch_lag_min"] == 2.0 and e["alt"] is not None


@pytest.mark.parametrize("dl,dw", [(0.25, 0.0), (0.0, 0.04)])
def test_diff_line_or_water_flags_daily_check(db, dl, dw):
    t = _tl()
    _own(db, "macau", "2026-06-17T11:10:10+08:00", line=-(t["line"] + dl), wh=t["home_water"] + dw, wa=t["away_water"])
    for mode in ("rule_1110", "none"):  # 不论 include_live 都进日核对
        live, _ = _live(mode=mode)
        it, d = _live.last
        assert it["match"]["instant_src_diff"] is True and "instant_src_diff" in it["match"]["daily_check"]
        assert d["daily_check_summary"]["by_reason"]["instant_src_diff"] >= 1
    e = _live()[0][("macau", "asian")]
    assert e["instant_src_diff"] is True and e["alt"]["line"] == t["line"] and e["line"] == t["line"] + dl
    assert _live.last[1]["daily_check_summary"]["instant_src_diff_cells_in_live"] >= 1


def test_own_only_book_no_alt(db):
    _own(db, "pinnacle", "2026-06-17T11:09:00+08:00", line=0.25)
    e = _live()[0][("pinnacle", "asian")]
    assert e["origin"] == "own_capture" and e["line"] == -0.25 and e["alt"] is None and e["instant_src_diff"] is None


def test_asof_rules(db):
    _own(db, "macau", "2026-06-17T11:11:30+08:00")
    assert _live(as_of="2026-06-17T11:09:00+08:00")[0] == {}  # 11:10 未到 → 不给
    e = _live(as_of="2026-06-17T11:11:00+08:00")[0][("macau", "asian")]  # 自采还没抓到 → 用时间线
    assert e["origin"] == "timeline" and e["alt"] is None


def test_non_own_rows_ignored(db):
    c = sqlite3.connect(str(db))
    c.execute("INSERT INTO odds_snapshot (match_id, book, market, channel, point, recorded_at, target_at, line, "
              "water_home, water_away, source, extras_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
              (MID, "pinnacle", "asian", "rule", "rule_1110", "2026-06-17T11:09:00+08:00", TARGET, 0.25, 0.9, 0.9,
               "5df_history", json.dumps({"odds_source": "hist", "label": "rule_1110"})))
    c.commit()
    c.close()
    assert ("pinnacle", "asian") not in _live()[0]


def test_flat_and_all_mode(db):
    _own(db, "macau", "2026-06-17T11:10:10+08:00", line=-_tl()["line"])
    live, _ = _live(mode="all")
    it, _ = _live.last
    assert live[("macau", "asian")]["origin"] == "own_capture"
    others = [e for e in it["live"] if e["label"] != "rule_1110"]
    assert others and all(e["alt"] is None and e["origin"] is None for e in others)
    d = TestClient(app).get("/table/matches", params={"date_from": JD, "date_to": JD, "scope": "all",
                                                       "include_live": "rule_1110", "format": "flat"}).json()
    row = next(i for i in d["items"] if i["match_id"] == UID)
    assert "instant_src_diff" in row and any(e.get("alt") for e in row["live"])


def test_alt_not_used_by_strategy_code():
    """alt 只对照：除 table_matches 拼装外，没有代码读 alt。"""
    import re
    hits = []
    for f in list((ROOT / "app").glob("*.py")) + list((ROOT / "scripts").glob("*.py")):
        if f.name in ("table_matches.py", "daily_check_report.py"):  # 拼装 / 只读核对清单输出
            continue
        if re.search(r"""\[["']alt["']\]|get\(["']alt["']""", f.read_text(encoding="utf-8")):
            hits.append(f.name)
    assert hits == []
