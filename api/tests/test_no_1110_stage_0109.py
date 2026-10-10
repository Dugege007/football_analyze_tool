"""0.1.9（用户 2026-10-10）：「初盘」只有一个定义，就是各公司开盘时的数据；竞彩日 11:10 只是取数时间。

接口不再把竞彩日 11:10 的自采快照（rule_1110）当成一种对外盘口阶段返回；库里已有的 11:10 快照不删除。
"""
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
    if not V2D3_DB.exists():
        pytest.skip("no v2d3 db")
    p = tmp_path / "app.db"
    s = sqlite3.connect(f"file:{V2D3_DB}?mode=ro", uri=True)
    o = sqlite3.connect(str(p))
    s.backup(o)
    s.close()
    o.close()
    monkeypatch.setattr(adb, "DB_PATH", p)
    return p


def _own(p: Path, recorded_at: str = "2026-06-17T11:10:30+08:00"):
    c = sqlite3.connect(str(p))
    ex = {"capture": "own", "odds_source": "live", "phase": "live", "phase_variant": "rule_1110",
          "label": "rule_1110", "fetched_at": recorded_at, "target_at": TARGET}
    c.execute("INSERT INTO odds_snapshot (match_id, book, market, channel, point, recorded_at, target_at, line, "
              "water_home, water_away, water_src, source, extras_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (MID, "pinnacle", "asian", "rule", "rule_1110", recorded_at, TARGET, -0.5, 0.9, 0.95,
               "actual", "5df_live", json.dumps(ex)))
    c.commit()
    c.close()


def _get(mode: str):
    return TestClient(app).get("/table/matches", params={"date_from": JD, "date_to": JD, "scope": "all",
                                                         "include_live": mode})


def test_rule_1110_param_removed(db):
    assert _get("rule_1110").status_code == 422


def test_all_mode_has_no_1110_entries_and_no_config(db):
    _own(db)
    r = _get("all")
    assert r.status_code == 200
    d = r.json()
    it = next(i for i in d["items"] if i["match_id"] == UID)
    assert all(e.get("label") != "rule_1110" for e in it["live"])
    assert "live_rule_1110_target_time" not in it["schedule"]
    assert not any(k.startswith("live_rule_1110") for k in d["config"])
    assert "instant_src_diff" not in it["match"]["daily_check"]
    assert "own_1110_out_of_window" not in it["match"]["daily_check"]


def test_own_1110_capture_never_becomes_open(db):
    _own(db, "2026-06-17T11:10:30+08:00")
    it = next(i for i in _get("none").json()["items"] if i["match_id"] == UID)
    op = it["ah"]["pinnacle"]["open"]
    assert op.get("recorded_at") != "2026-06-17T11:10:30+08:00"
