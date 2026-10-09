"""0.3.17 前端 §13 对账：baseline_method 随返还率为空、open_basis 全覆盖（空则带原因）、
行级 kickoff_source / kickoff_placeholder、n_avg 与凯利同在、legacy_import usable 已判。只读现网/副本（临时拷贝）。"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app.db as adb  # noqa: E402
from app.main import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

PROD_DB = ROOT / "data" / "app.db"
V2D3_DB = ROOT / "data" / "v2d3" / "app.db"
BASES = {"first_tick", "api_opening", "legacy_import"}


def _copy(src, tmp_path, monkeypatch):
    p = tmp_path / "app.db"
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    o = sqlite3.connect(str(p))
    s.backup(o)
    s.close()
    o.close()
    monkeypatch.setattr(adb, "DB_PATH", p)
    return p


@pytest.fixture(params=["live", "v2d3"])
def table(request, tmp_path, monkeypatch):
    if request.param == "v2d3" and not V2D3_DB.exists():
        pytest.skip("no v2d3 replica")
    _copy(PROD_DB if request.param == "live" else V2D3_DB, tmp_path, monkeypatch)
    r = TestClient(app).get("/table/matches", params={"date_from": "2026-06-01", "date_to": "2026-06-30",
                                                      "limit": 2000})
    assert r.status_code == 200, r.text
    return request.param, r.json()


def _opens(it):
    for b, ph in it["ah"].items():
        yield ("ah", b), ph["open"]
    for b, ph in it["x1x2"].items():
        yield ("x1x2", b), ph["open"]
    yield ("jc", "jc"), it["jc_1x2"]["open"]


def test_baseline_method_null_iff_return_rate_null(table):
    _, d = table
    n_set = 0
    for it in d["items"]:
        for mk in ("ah", "x1x2"):
            for ph in it[mk].values():
                for p, c in ph.items():
                    if not isinstance(c, dict) or "baseline_method" not in c:
                        continue
                    if c["return_rate"] is None:
                        assert c["baseline_method"] is None, (it["match_id"], mk, p)
                    else:
                        assert c["baseline_method"] in ("empirical", "fixed_fallback")
                        n_set += 1
    assert n_set > 0


def test_every_open_has_basis_or_null_reason(table):
    which, d = table
    seen_null = set()
    for it in d["items"]:
        for key, o in _opens(it):
            assert "open_basis" in o and "open_basis_reason" in o, key
            if o["open_basis"] is None:
                assert o["open_basis_reason"] in ("no_open_data", "after_as_of"), (it["match_id"], key)
                assert o["usable_at_mid"] is None and o["usable_at_close"] is None
                seen_null.add(key)
            else:
                assert o["open_basis"] in BASES and o["open_basis_reason"] is None, (it["match_id"], key)
                # 0.3.17：三种口径都已判 usable（legacy_import 不再 null）
                assert isinstance(o["usable_at_mid"], bool) and isinstance(o["usable_at_close"], bool)
                assert isinstance(o["ts_inferred"], bool)
    if which == "live":  # 前端点名的几条路径：现网无平博亚盘 / 皇冠·平博欧赔；竞彩缺 1 场
        assert {("ah", "pinnacle"), ("x1x2", "crown"), ("x1x2", "pinnacle"), ("jc", "jc")} <= seen_null


def test_open_basis_reason_in_flat(table):
    _, d = table
    r = TestClient(app).get("/table/matches", params={"date_from": "2026-06-06", "date_to": "2026-06-06",
                                                      "format": "flat"})
    flat = r.json()["items"][0]
    for k in ("ah_pinnacle_open_open_basis_reason", "x1x2_crown_open_open_basis_reason",
              "jc_1x2_open_open_basis_reason", "ah_macau_open_ts_inferred",
              "kickoff_source", "kickoff_placeholder", "schedule_kickoff_source"):
        assert k in flat, k


def test_row_level_kickoff_source_and_placeholder(table):
    _, d = table
    for it in d["items"]:
        m, s = it["match"], it["schedule"]
        assert "kickoff_source" in m and "kickoff_placeholder" in m
        assert m["kickoff_source"] == s["kickoff_source"] and m["kickoff_placeholder"] == s["kickoff_placeholder"]
        if m["kickoff_at"]:
            assert m["kickoff_source"] in ("5df", "jingcai", "jingcai_hour_synth")


def test_n_avg_present_wherever_kelly_computed(table):
    which, d = table
    n_kelly = 0
    for it in d["items"]:
        for ph in it["x1x2"].values():
            for p, c in ph.items():
                if not isinstance(c, dict) or "kelly" not in c:
                    continue
                if c["kelly"] is not None or c.get("kelly_multi_avg") is not None:
                    n_kelly += 1
                    assert isinstance(c["n_avg"], int) and c["n_avg"] >= 1
                else:
                    assert c["n_avg"] is None
                if c["available"] and not c["complete"]:  # 现网旧手工只有主胜赔 → 无凯利、无 n_avg
                    assert c["kelly"] is None and c["n_avg"] is None
    if which == "v2d3":
        assert n_kelly > 0
    # 亚盘没有「多家平均」比较（偏离是对本家×阶段基准），故无 n_avg 字段
    it = d["items"][0]
    assert "n_avg" not in it["ah"]["macau"]["close"]


# ---------------------------------------------------------------- hl_v0.2：返还率只看真实水位

def test_hl_v02_config_and_real_only_baseline_counts(table):
    which, _ = table
    # 全量日期：基准样本是库内所有更早竞彩日（默认 7 天窗口会漏掉窗口外的样本；v2d3 18:17 起有新竞彩日）
    r = TestClient(app).get("/table/matches", params={"limit": 2000, "date_from": "2000-01-01"})
    d = r.json()
    assert d["total"] <= 2000
    assert d["config_version"] == "hl_v0.3.1" and d["config"]["return_hl_water"] == "real_only"
    items = d["items"]
    for b in ("macau", "crown", "william", "pinnacle"):
        for p in ("open", "mid", "close"):
            for it in items:
                jd = it["match"]["jingcai_date"]
                exp = sum(1 for o in items if o["match"]["jingcai_date"] < jd
                          and o["ah"][b][p]["water_source"] == "actual"
                          and o["ah"][b][p]["return_rate"] is not None)
                c = it["ah"][b][p]
                assert c["return_rate_baseline_n"] == exp, (which, b, p, it["match_id"])
                if c["return_rate"] is not None and c["baseline_method"] == "empirical":
                    assert exp >= 20
    if which == "live":  # 现网皇冠/威廉全为档位中点 → 不进样本
        assert all(it["ah"][b][p]["return_rate_baseline_n"] == 0
                   for it in items for b in ("crown", "william") for p in ("open", "mid", "close"))
