"""0.3.25：GET /matches 同一竞彩日内按竞彩编号升序；无编号的排在最后并按开赛时间。"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app.db as adb  # noqa: E402
from app.main import app  # noqa: E402


def _build_db(path: Path) -> None:
    c = sqlite3.connect(str(path))
    c.executescript(
        """
        CREATE TABLE matches (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          match_uid TEXT NOT NULL UNIQUE,
          scope TEXT NOT NULL DEFAULT 'jingcai',
          jingcai_date TEXT NOT NULL,
          weekday TEXT,
          kickoff_hour INTEGER,
          jc_id TEXT,
          jc_no INTEGER,
          competition_name TEXT,
          competition_type TEXT,
          competition_stage TEXT,
          home_team TEXT NOT NULL,
          away_team TEXT NOT NULL,
          kickoff_at TEXT,
          kickoff_minute_known INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE predictions (
          match_id INTEGER, strategy TEXT, direction TEXT
        );
        CREATE TABLE strategy_defs (
          strategy_key TEXT, status TEXT, config_json TEXT
        );
        CREATE TABLE results (
          match_id INTEGER PRIMARY KEY,
          home_goals INTEGER, away_goals INTEGER,
          total_goals INTEGER, wdl TEXT
        );
        CREATE TABLE match_meta (
          match_id INTEGER PRIMARY KEY, extras_json TEXT
        );
        """
    )
    # 故意按错误顺序插入：先插凌晨场（编号大、小时小），再插白天场（编号小、小时大），
    # 再插两场没有编号的扩展场，确保排序结果不是碰巧按插入顺序。
    rows = [
        ("2026-06-06|六208", "jingcai", "2026-06-06", "六", 1, "六208", 208,
         "友谊赛", "葡萄牙", "智利", "2026-06-07T01:45:00+08:00"),
        ("2026-06-06|六217", "jingcai", "2026-06-06", "六", 8, "六217", 217,
         "友谊赛", "阿根廷", "洪都拉斯", "2026-06-07T08:00:00+08:00"),
        ("2026-06-06|六201", "jingcai", "2026-06-06", "六", 13, "六201", 201,
         "日职联", "鹿岛鹿角", "神户胜利船", "2026-06-06T13:00:00+08:00"),
        ("2026-06-06|六207", "jingcai", "2026-06-06", "六", 21, "六207", 207,
         "友谊赛", "比利时", "突尼斯", "2026-06-06T21:00:00+08:00"),
        # 无编号：开赛较晚的先插入，较晚的应排在较晚位置
        ("extra|late", "extra", "2026-06-06", None, 20, None, None,
         "其他", "晚场主", "晚场客", "2026-06-06T20:00:00+08:00"),
        ("extra|early", "extra", "2026-06-06", None, 12, None, None,
         "其他", "早场主", "早场客", "2026-06-06T12:00:00+08:00"),
    ]
    for r in rows:
        c.execute(
            "INSERT INTO matches (match_uid, scope, jingcai_date, weekday, kickoff_hour,"
            " jc_id, jc_no, competition_name, home_team, away_team, kickoff_at,"
            " kickoff_minute_known) VALUES (?,?,?,?,?,?,?,?,?,?,?,1)",
            r,
        )
    c.commit()
    c.close()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "sort.db"
    _build_db(db)
    monkeypatch.setattr(adb, "DB_PATH", db)
    monkeypatch.setattr(adb, "READONLY", False)
    monkeypatch.setattr(adb, "DB_LABEL", "live")
    return TestClient(app)


def test_matches_sorted_by_jc_no_then_kickoff(client):
    r = client.get("/matches", params={"date": "2026-06-06", "scope": "all"})
    assert r.status_code == 200
    items = r.json()["items"]
    ids = [it["id"] for it in items]
    assert ids == [
        "2026-06-06|六201",
        "2026-06-06|六207",
        "2026-06-06|六208",
        "2026-06-06|六217",
        "extra|early",
        "extra|late",
    ]


def test_jingcai_scope_also_sorted_by_jc_no(client):
    r = client.get("/matches", params={"date": "2026-06-06", "scope": "jingcai"})
    assert r.status_code == 200
    nos = [it["match"]["jc"]["id"] for it in r.json()["items"]]
    assert nos == ["六201", "六207", "六208", "六217"]
