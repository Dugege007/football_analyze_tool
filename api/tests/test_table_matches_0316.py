"""GET /table/matches 0.3.16（§12 五条 + 算法顾问补充）：
例外场 [23:00, 次日 11:30] 两端都含；目标时刻精确到分钟；返还率基准剔除 tier_midpoint + baseline_method；
凯利 leave-one-out + n_avg；欧赔 api_closing 单独返回且仅完赛后出现。全部在临时副本上跑。
"""
from __future__ import annotations
# --- public repo: paths are env-overridable (see config.example.env) ---
import os as _rp_os
from pathlib import Path as _RpPath
_REPO_ROOT = _RpPath(__file__).resolve().parents[2]
def _rp_load_dotenv(path=_REPO_ROOT / '.env'):
    # 读仓库根 .env（KEY=VALUE；已存在的环境变量优先，不覆盖）；不打印任何值
    if _rp_os.environ.get('FAT_DISABLE_DOTENV') == '1':  # 测试时由 conftest 设置，避免本机 .env 干扰
        return
    try:
        for _ln in path.read_text(encoding='utf-8').splitlines():
            _ln = _ln.strip()
            if not _ln or _ln.startswith('#') or '=' not in _ln:
                continue
            _k, _v = _ln.split('=', 1)
            _k = _k.strip().removeprefix('export ').strip()
            _v = _v.strip().strip('"').strip("'")
            if _v.startswith('YOUR_'):  # config.example.env 占位值视为未填写
                continue
            if _k and _k not in _rp_os.environ:
                _rp_os.environ[_k] = _v
    except FileNotFoundError:
        pass
_rp_load_dotenv()
def _rp_env(name, default, base=None):
    v = _rp_os.environ.get(name, '').strip()
    p = _RpPath(v).expanduser() if v else default
    return p if p.is_absolute() else (base or _REPO_ROOT) / p
_MA_API_ROOT = _rp_env('MA_API_ROOT', _REPO_ROOT / 'api')
_ODDS_DATA_DIR = _rp_env('ODDS_DATA_DIR', _REPO_ROOT / 'data' / 'odds-data')
_APP_DB = _rp_env('APP_DB_PATH', _MA_API_ROOT / 'data' / 'app.db', _MA_API_ROOT)
_V2D3_DB = _rp_env('V2D3_DB_PATH', _MA_API_ROOT / 'data' / 'v2d3' / 'app.db', _MA_API_ROOT)
_BACKUP_DIR = _rp_env('BACKUP_DIR', _REPO_ROOT / 'backups' / 'football')
# --- end path config ---


import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app.db as adb  # noqa: E402
from app import collection_schedule as cs  # noqa: E402
from app import table_matches as tm  # noqa: E402
from app.main import API_VERSION, app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

PROD_DB = ROOT / "data" / "app.db"
TIMELINE_SQL = Path(str(_REPO_ROOT / "docs/schema/v2_0_odds_timeline.sql"))
HL_DOC = Path(str(_REPO_ROOT / "docs/schema/v2_0-data-table-highlight-rules.md"))


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


@pytest.fixture()
def client():
    return TestClient(app)


def _get(client, **params):
    r = client.get("/table/matches", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _row(d, uid):
    return next(i for i in d["items"] if i["match_id"] == uid)


def _ins(c, uid, date, hour, kickoff_at, known=1, goals=None):
    cur = c.execute(
        "INSERT INTO matches (match_uid, scope, jingcai_date, weekday, kickoff_hour, competition_name,"
        " home_team, away_team, kickoff_at, kickoff_minute_known) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (uid, "extra", date, "五", hour, "测试联赛", "测试主", "测试客", kickoff_at, known))
    mid = cur.lastrowid
    if goals:
        c.execute("INSERT INTO results (match_id, home_goals, away_goals, total_goals, wdl) VALUES (?,?,?,?,?)",
                  (mid, goals[0], goals[1], sum(goals), "胜"))
    return mid


# ---------------------------------------------------------------- 1. 例外场两端都含

def test_phase_exception_predicate_inclusive():
    assert cs.is_phase_exception(23, 0) is True
    assert cs.is_phase_exception(22, 59) is False
    assert cs.is_phase_exception(0, 0) is True
    assert cs.is_phase_exception(11, 29) is True
    assert cs.is_phase_exception(11, 30) is True  # 终版：11:30 整也算例外
    assert cs.is_phase_exception(11, 31) is False
    assert cs.is_phase_exception(11) is True  # 分钟不明按整点
    assert cs.is_phase_exception(12) is False


def test_phase_exception_in_table(db, client):
    c = sqlite3.connect(str(db))
    _ins(c, "2099-01-01|x1130", "2099-01-01", 11, "2099-01-02T11:30:00+08:00")
    _ins(c, "2099-01-02|x1131", "2099-01-02", 11, "2099-01-02T11:31:00+08:00")
    _ins(c, "2099-01-02|x2300", "2099-01-02", 23, "2099-01-02T23:00:00+08:00")
    _ins(c, "2099-01-02|x2259", "2099-01-02", 22, "2099-01-02T22:59:00+08:00")
    c.commit()
    c.close()
    d = _get(client, date_from="2099-01-01", date_to="2099-01-02", scope="all")
    r = _row(d, "2099-01-01|x1130")
    assert r["phase_exception"] is True
    assert r["schedule"]["mid_target_time"] == "2099-01-01T15:00:00+08:00"
    assert r["schedule"]["close_target_time"] == "2099-01-01T22:00:00+08:00"
    assert r["schedule"]["mid_real_target_time"] == "2099-01-02T03:30:00+08:00"
    assert r["schedule"]["close_real_target_time"] == "2099-01-02T10:30:00+08:00"
    r = _row(d, "2099-01-02|x1131")
    assert r["phase_exception"] is False
    assert r["schedule"]["mid_target_time"] == "2099-01-02T03:31:00+08:00"
    assert _row(d, "2099-01-02|x2300")["phase_exception"] is True
    assert _row(d, "2099-01-02|x2259")["phase_exception"] is False
    # 0.3.18：无竞彩编号的场（本用例插入的都无 jc_id）仍按 [D 23:00, D+1 11:30]；有编号按 jc_code_ge_2300
    assert d["config"]["phase_exception_range"].endswith("no code: [D 23:00, D+1 11:30] inclusive")
    assert d["config"]["exception_rule"] == "jc_code_ge_2300"
    assert _row(d, "2099-01-01|x1130")["schedule"]["exception_rule"] == "time_window_2300_1130"


# ---------------------------------------------------------------- 2. 目标时刻精确到分钟

def test_target_exact_minute_and_hour_fallback(db, client):
    c = sqlite3.connect(str(db))
    _ins(c, "2099-01-03|x1830", "2099-01-03", 18, "2099-01-03T18:30:00+08:00", known=1)
    _ins(c, "2099-01-03|x18h", "2099-01-03", 18, "2099-01-03T18:30:00+08:00", known=0)  # 分钟不明 → 整点
    _ins(c, "2099-01-03|x0245", "2099-01-03", 2, "2099-01-04T02:45:00+08:00", known=1)
    c.commit()
    c.close()
    d = _get(client, date_from="2099-01-03", date_to="2099-01-03", scope="all")
    s = _row(d, "2099-01-03|x1830")["schedule"]
    assert s["mid_target_time"] == "2099-01-03T10:30:00+08:00"
    assert s["close_target_time"] == "2099-01-03T17:30:00+08:00"
    assert s["phase_target"] == "exact_minute" and s["kickoff_minute_known"] is True
    s = _row(d, "2099-01-03|x18h")["schedule"]
    assert s["mid_target_time"] == "2099-01-03T10:00:00+08:00"
    assert s["close_target_time"] == "2099-01-03T17:00:00+08:00"
    assert s["kickoff_minute_known"] is False
    s = _row(d, "2099-01-03|x0245")["schedule"]  # 例外场：规则值不变，*_real 精确到分钟
    assert s["mid_target_time"] == "2099-01-03T15:00:00+08:00"
    assert s["close_target_time"] == "2099-01-03T22:00:00+08:00"
    assert s["mid_real_target_time"] == "2099-01-03T18:45:00+08:00"
    assert s["close_real_target_time"] == "2099-01-04T01:45:00+08:00"
    assert d["config"]["phase_target"] == "exact_minute"


def test_calc_collection_times_exact_minute():
    assert cs.calc_collection_times(18, 30) == {"mid_time": "10:30", "final_time": "17:30"}
    assert cs.calc_collection_times(18) == {"mid_time": "10:00", "final_time": "17:00"}
    assert cs.calc_collection_times(17, 59) == {"mid_time": "09:59", "final_time": "16:59"}
    assert cs.calc_collection_times(23, 30) == {"mid_time": "15:00", "final_time": "22:00"}


# ---------------------------------------------------------------- 3. 基准：剔除 tier_midpoint + baseline_method

def test_baseline_excludes_tier_midpoint_and_uses_fixed_fallback(db, client):
    d = _get(client, date_from="2026-06-20", date_to="2026-06-20")
    i = d["items"][0]
    cell = i["ah"]["crown"]["close"]  # 现网 crown 水位全是档位中点换算
    assert cell["return_rate"] is not None  # 本场值照常展示
    assert cell["return_rate_baseline"] is None and cell["return_rate_baseline_n"] == 0
    assert cell["baseline_method"] == "fixed_fallback" and cell["return_rate_dev"] is None
    # 同样数据标成真实水位 → 进基准 → empirical
    c = sqlite3.connect(str(db))
    c.execute("UPDATE odds_asian SET water_src='actual' WHERE book='crown'")
    c.commit()
    c.close()
    i2 = _row(_get(client, date_from="2026-06-20", date_to="2026-06-20"), i["match_id"])
    cell2 = i2["ah"]["crown"]["close"]
    assert cell2["return_rate_baseline_n"] >= 20 and cell2["baseline_method"] == "empirical"
    assert cell2["return_rate_baseline"] is not None and cell2["return_rate_dev"] is not None


def test_baseline_method_on_every_rr_cell_and_fallback_table(db, client):
    d = _get(client, date_from="2026-06-01", date_to="2026-07-05", limit=2000)
    for i in d["items"]:
        for market in ("ah", "x1x2"):
            for b in tm.BOOKS:
                for p in tm.PHASES:
                    c = i[market][b][p]
                    # 0.3.17：返还率为空 → baseline_method 为空（前端 §13 对账）
                    assert c["baseline_method"] == (None if c["return_rate"] is None else c["baseline_method"])
                    assert c["return_rate"] is None or c["baseline_method"] in ("empirical", "fixed_fallback")
                for rp in tm.REAL_PHASES:
                    cell = i[market][b][rp]
                    if cell is not None:
                        assert "baseline_method" not in cell  # *_real 只给原始值
    fb = d["config"]["return_rate_fallback"]
    # 与 hl_v0.1 §4/§5 兜底绝对值一致
    assert fb["ah"]["macau"] == {"low_light": 0.93, "low_medium": 0.91, "high_light": 0.97}
    assert fb["ah"]["pinnacle"] == {"low_light": 0.955, "low_medium": 0.94, "high_light": None}
    assert fb["x1x2"]["william"] == {"low_light": 0.90, "low_medium": 0.88, "high_light": None}
    assert fb["x1x2"]["pinnacle"] == {"low_light": 0.95, "low_medium": 0.93, "high_light": None}
    txt = HL_DOC.read_text(encoding="utf-8")
    for needle in ("<0.93 轻、<0.91 中、>0.97 轻", "<0.955 轻、<0.94 中", "<0.90 轻、<0.88 中", "<0.95 轻、<0.93 中",
                   "baseline_method"):
        assert needle in txt, needle


# ---------------------------------------------------------------- 4. leave-one-out + n_avg（flat 列）

def test_n_avg_flat_columns(db, client):
    d = _get(client, date_from="2026-06-06", date_to="2026-06-06", format="flat")
    row = d["items"][0]
    for k in ("x1x2_macau_close_n_avg", "x1x2_macau_close_multi_avg_n_books",
              "x1x2_base_close_multi_avg_n_avg", "ah_macau_close_baseline_method",
              "x1x2_pinnacle_open_baseline_method", "x1x2_macau_api_closing_label"):
        assert k in row, k
    assert d["config"]["kelly"]["fallback_base"] == "multi_avg_loo"


# ---------------------------------------------------------------- 5. api_closing

def _with_timeline(db):
    c = sqlite3.connect(str(db))
    c.executescript(TIMELINE_SQL.read_text(encoding="utf-8"))
    return c


def _snap_x(c, mid, book, point, ph, pd, pa, api_phase=None, rec="2026-10-07T10:00:00+00:00"):
    c.execute("INSERT INTO odds_snapshot (match_id, book, market, channel, point, recorded_at, target_at,"
              " price_home, price_draw, price_away, source, extras_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
              (mid, book, "euro_1x2", "rule", point, rec, None, ph, pd, pa, "5df_odds_snap",
               json.dumps({"api_phase": api_phase}) if api_phase else None))


def test_api_closing_separate_and_only_after_result(db, client):
    c = _with_timeline(db)
    done = c.execute("SELECT id FROM matches WHERE match_uid='2026-06-06|六204'").fetchone()[0]  # 3-0 已完赛
    fut = _ins(c, "2099-02-01|x2000", "2099-02-01", 20, "2099-02-01T20:00:00+08:00")
    for mid in (done, fut):
        for b, o in (("macau", (1.9, 3.3, 3.8)), ("pinnacle", (2.0, 3.5, 4.0)), ("crown", (2.05, 3.4, 3.7))):
            _snap_x(c, mid, b, "close", *o, api_phase="closing")
    c.commit()
    c.close()
    row = _row(_get(client, date_from="2026-06-06", date_to="2026-06-06"), "2026-06-06|六204")
    assert row["result"] is not None
    for b in ("macau", "pinnacle", "crown"):
        x = row["x1x2"][b]
        assert x["close"]["basis"] != "api_closing" and x["close"]["source"] != "odds_snapshot/rule/close"
        assert x["close"]["kelly"] is None and x["close"]["n_avg"] is None
        apc = x["api_closing"]
        assert apc["label"] == "api_closing" and apc["display_name"] == "收盘（时间未知）"
        assert apc["reference_only"] is True and apc["quote_time_known"] is False
        assert apc["complete"] is True and "return_rate" not in apc and "kelly" not in apc
    assert row["x1x2"]["macau"]["api_closing"]["home"] == 1.9
    assert row["x1x2_base"]["close"]["multi_avg"] is None  # 不进多家平均/凯利
    assert row["x1x2"]["william"].get("api_closing") is None
    # 未完赛：嵌套里完全没有 api_closing
    frow = _row(_get(client, date_from="2099-02-01", date_to="2099-02-01", scope="all"), "2099-02-01|x2000")
    assert frow["result"] is None
    for b in tm.BOOKS:
        assert "api_closing" not in frow["x1x2"][b]
        assert frow["x1x2"][b]["close"]["basis"] != "api_closing"
    # as_of 早于完赛可见 → 同样不出现
    early = _row(_get(client, date_from="2026-06-06", date_to="2026-06-06", as_of="2026-06-06T18:00:00+08:00"),
                 "2026-06-06|六204")
    assert all("api_closing" not in early["x1x2"][b] for b in tm.BOOKS)
    # flat：赛前列存在但全 null（列集合固定，不带数值）
    flat = _row(_get(client, date_from="2099-02-01", date_to="2099-02-01", scope="all", format="flat"),
                "2099-02-01|x2000")
    assert flat["x1x2_macau_api_closing_home"] is None and flat["x1x2_macau_api_closing_label"] is None


def test_api_closing_not_in_baseline(db, client):
    c = _with_timeline(db)
    ids = [r[0] for r in c.execute("SELECT id FROM matches WHERE jingcai_date < '2026-06-20'")]
    for mid in ids:
        _snap_x(c, mid, "macau", "close", 1.9, 3.3, 3.8, api_phase="closing")
    c.commit()
    c.close()
    i = _get(client, date_from="2026-06-20", date_to="2026-06-20")["items"][0]
    cell = i["x1x2"]["macau"]["close"]
    assert cell["return_rate_baseline_n"] == 0
    assert cell["baseline_method"] == (None if cell["return_rate"] is None else "fixed_fallback")


def test_api_version():
    assert API_VERSION == "0.3.23"


def test_api_closing_row_does_not_change_multi_avg_kelly_rr(db, client):
    """同一场：有/无 api_closing 行时，multi_avg / 凯利（任何基准）/ n_avg / 返还率 / 基准 完全一致。"""
    c = _with_timeline(db)
    mid = c.execute("SELECT id FROM matches WHERE match_uid='2026-06-06|六204'").fetchone()[0]
    for b, o in (("pinnacle", (2.0, 3.5, 4.0)), ("macau", (1.9, 3.3, 3.8)), ("crown", (2.05, 3.4, 3.7))):
        _snap_x(c, mid, b, "close", *o, rec="2026-06-06T14:50:00+08:00")  # 有真实报价时刻的临盘
    c.commit()
    c.close()

    def pick(row):
        keys = ("kelly", "kelly_base", "kelly_base_n_books", "kelly_multi_avg", "multi_avg_n_books", "n_avg",
                "return_rate", "return_rate_baseline", "return_rate_baseline_n", "return_rate_dev",
                "baseline_method", "home", "draw", "away", "basis", "source")
        return {"x1x2": {b: {p: {k: row["x1x2"][b][p].get(k) for k in keys} for p in tm.PHASES} for b in tm.BOOKS},
                "x1x2_base": row["x1x2_base"], "multi_avg_prob": row["multi_avg_prob"]}

    base = pick(_row(_get(client, date_from="2026-06-06", date_to="2026-06-06"), "2026-06-06|六204"))
    assert base["x1x2_base"]["close"]["multi_avg"]["n_books"] == 3
    c = sqlite3.connect(str(db))
    # 同一场再加 api_closing：william（新机构）+ 已有三家各一条（UNIQUE 键不同 → 放 rule/mid 与 actual/close 点）
    _snap_x(c, mid, "william", "close", 1.5, 4.5, 6.0, api_phase="closing")
    for b in ("pinnacle", "macau", "crown"):
        c.execute("INSERT INTO odds_snapshot (match_id, book, market, channel, point, recorded_at, price_home,"
                  " price_draw, price_away, source, extras_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (mid, b, "euro_1x2", "rule", "mid", "2026-10-07T10:00:00+00:00", 1.3, 5.0, 9.0,
                   "5df_odds_snap", json.dumps({"api_phase": "closing"})))
    c.commit()
    c.close()
    row = _row(_get(client, date_from="2026-06-06", date_to="2026-06-06"), "2026-06-06|六204")
    assert pick(row) == base
    assert row["x1x2"]["william"]["api_closing"]["home"] == 1.5  # 只以单独对象出现
    assert row["x1x2"]["william"]["close"]["kelly"] is None
    assert all(row["x1x2"][b]["mid"]["basis"] != "api_closing" for b in tm.BOOKS)


# ---------------------------------------------------------------- 6. open_basis / usable_at_*（术语文档「api_opening 的处理」）

U6 = "2026-06-06|六204"  # 开赛 16:00，非例外场：mid 目标 08:00，close 目标 15:00


def _seg_x(c, mid, book, market, start, *, line=None, wh=None, wa=None, ph=None, pd=None, pa=None, inplay=0):
    c.execute("INSERT INTO odds_timeline_seg (match_id, book, market, seg_start_at, seg_end_at, line, water_home,"
              " water_away, price_home, price_draw, price_away, is_inplay, water_src) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (mid, book, market, start, "2026-06-06T16:00:00+08:00", line, wh, wa, ph, pd, pa, inplay, "actual"))


def _own_1110(c, mid, book, market, rec, **px):
    c.execute("INSERT INTO odds_snapshot (match_id, book, market, channel, point, recorded_at, target_at,"
              " price_home, price_draw, price_away, line, water_home, water_away, source, extras_json)"
              " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (mid, book, market, "live", "rule_1110", rec, rec, px.get("ph"), px.get("pd"), px.get("pa"),
               px.get("line"), px.get("wh"), px.get("wa"), "own_capture", json.dumps({"label": "rule_1110"})))


def _m6(c):
    return c.execute("SELECT id FROM matches WHERE match_uid=?", (U6,)).fetchone()[0]


def test_open_first_tick_from_timeline(db, client):
    c = _with_timeline(db)
    mid = _m6(c)
    _seg_x(c, mid, "pinnacle", "asian", "2026-06-05T09:00:00+08:00", line=-0.5, wh=0.95, wa=0.95)
    _seg_x(c, mid, "pinnacle", "asian", "2026-06-04T20:00:00+08:00", line=-0.25, wh=0.9, wa=1.0)
    c.commit()
    c.close()
    o = _row(_get(client, date_from="2026-06-06", date_to="2026-06-06"), U6)["ah"]["pinnacle"]["open"]
    assert o["open_basis"] == "first_tick" and o["basis"] == "first_record"
    assert o["recorded_at"] == "2026-06-04T20:00:00+08:00" == o["earliest_ts_quote_at"]
    assert o["line"] == 0.25 and o["usable_at_mid"] is True and o["usable_at_close"] is True
    assert o["unusable_reason"] is None and o["return_rate"] is not None


def test_open_api_opening_only_is_unusable_and_has_no_durations(db, client):
    c = _with_timeline(db)
    mid = _m6(c)
    _snap_x(c, mid, "william", "open", 2.1, 3.3, 3.5, api_phase="opening", rec="2026-10-07T10:00:00+00:00")
    _snap_x(c, mid, "william", "close", 2.0, 3.4, 3.6, rec="2026-06-06T14:55:00+08:00")  # 有时间戳 → 会成为 first_tick
    c.commit()
    c.close()
    # 先删掉临盘那条：只剩 api_opening
    c = sqlite3.connect(str(db))
    c.execute("DELETE FROM odds_snapshot WHERE match_id=? AND book='william' AND point='close'", (mid,))
    c.commit()
    c.close()
    x = _row(_get(client, date_from="2026-06-06", date_to="2026-06-06"), U6)["x1x2"]["william"]
    o = x["open"]
    assert o["open_basis"] == "api_opening" and o["basis"] == "api_opening"
    assert o["recorded_at"] is None and o["fetched_at"] is not None  # 开盘时刻未知，不拿抓取时间顶替
    assert o["earliest_ts_quote_at"] is None
    assert o["usable_at_mid"] is False and o["usable_at_close"] is False
    assert o["unusable_reason"] == "open_time_unknown_after_decision_possible"
    assert o["return_rate"] is not None  # 展示/返还率仍可用
    for ph in ("mid", "close"):
        assert x[ph]["minutes_since_open"] is None


def test_open_api_opening_with_own_1110_capture_per_phase(db, client):
    c = _with_timeline(db)
    mid = _m6(c)
    _snap_x(c, mid, "william", "open", 2.1, 3.3, 3.5, api_phase="opening", rec="2026-10-07T10:00:00+00:00")
    _own_1110(c, mid, "william", "euro_1x2", "2026-06-06T11:10:00+08:00", ph=2.05, pd=3.3, pa=3.6)
    c.commit()
    c.close()
    o = _row(_get(client, date_from="2026-06-06", date_to="2026-06-06"), U6)["x1x2"]["william"]["open"]
    assert o["open_basis"] == "api_opening" and o["home"] == 2.1  # 自抓 11:10 不顶替初盘
    assert o["earliest_ts_quote_at"] == "2026-06-06T11:10:00+08:00"
    assert o["usable_at_mid"] is False  # 11:10 > 中盘目标 08:00
    assert o["usable_at_close"] is True  # 11:10 <= 临盘目标 15:00
    assert o["unusable_reason"] == "open_time_unknown_after_decision_possible"


def test_open_api_opening_usable_both_when_quote_before_mid_inclusive(db, client):
    c = _with_timeline(db)
    mid = _m6(c)
    _snap_x(c, mid, "william", "open", 2.1, 3.3, 3.5, api_phase="opening", rec="2026-10-07T10:00:00+00:00")
    _own_1110(c, mid, "william", "euro_1x2", "2026-06-06T08:00:00+08:00", ph=2.05, pd=3.3, pa=3.6)  # = 中盘目标
    c.commit()
    c.close()
    o = _row(_get(client, date_from="2026-06-06", date_to="2026-06-06"), U6)["x1x2"]["william"]["open"]
    assert o["usable_at_mid"] is True and o["usable_at_close"] is True and o["unusable_reason"] is None


def test_open_first_tick_beats_api_opening_and_respects_as_of(db, client):
    c = _with_timeline(db)
    mid = _m6(c)
    _snap_x(c, mid, "william", "open", 2.1, 3.3, 3.5, api_phase="opening", rec="2026-10-07T10:00:00+00:00")
    _seg_x(c, mid, "william", "euro_1x2", "2026-06-05T12:00:00+08:00", ph=2.2, pd=3.2, pa=3.4)
    c.commit()
    c.close()
    o = _row(_get(client, date_from="2026-06-06", date_to="2026-06-06"), U6)["x1x2"]["william"]["open"]
    assert o["open_basis"] == "first_tick" and o["home"] == 2.2 and o["usable_at_mid"] is True
    # as_of 早于该 tick → 看不到 → 退回 api_opening
    o2 = _row(_get(client, date_from="2026-06-06", date_to="2026-06-06", as_of="2026-06-05T11:00:00+08:00"),
              U6)["x1x2"]["william"]["open"]
    assert o2["open_basis"] == "api_opening" and o2["earliest_ts_quote_at"] is None


def test_open_legacy_import_inferred_1110_and_flat_columns(db, client):
    """0.3.17：legacy_import 的 earliest 推定为竞彩日 11:10（ts_inferred），usable 与 api_opening 同规则（不一刀切 true）。"""
    d = _get(client, date_from="2026-06-06", date_to="2026-06-06")
    o = _row(d, U6)["ah"]["macau"]["open"]  # 现网旧手工；开赛 16:00 → mid 08:00 < 11:10，close 15:00 ≥ 11:10
    assert o["open_basis"] == "legacy_import"
    assert o["earliest_ts_quote_at"] == "2026-06-06T11:10:00+08:00" and o["ts_inferred"] is True
    assert o["usable_at_mid"] is False and o["usable_at_close"] is True
    assert o["unusable_reason"] == "open_time_unknown_after_decision_possible"
    flat = _get(client, date_from="2026-06-06", date_to="2026-06-06", format="flat")["items"][0]
    for k in ("ah_macau_open_open_basis", "ah_macau_open_usable_at_mid", "x1x2_william_open_usable_at_close",
              "x1x2_pinnacle_open_earliest_ts_quote_at", "jc_1x2_open_open_basis", "ah_macau_open_unusable_reason"):
        assert k in flat, k
    assert "ah_macau_mid_open_basis" not in flat


def _legacy_cell():
    return {"basis": "legacy_import", "available": True}


def _sched(jd, mid, close):
    return {"live_rule_1110_target_time": f"{jd}T11:10:00+08:00",
            "mid_target_time": mid, "close_target_time": close}


@pytest.mark.parametrize("mid,close,exp_mid,exp_close", [
    # 例外场（≥23:00 / 次日 ≤11:30）→ 竞彩日 15:00 / 22:00 → 两段都 true
    ("2026-06-06T15:00:00+08:00", "2026-06-06T22:00:00+08:00", True, True),
    # 非例外 [11:30,19:10)：开赛 19:09 → mid 11:09 < 11:10 → false；close 18:09 → true
    ("2026-06-06T11:09:00+08:00", "2026-06-06T18:09:00+08:00", False, True),
    # 边界含端：开赛 19:10 → mid 11:10 → true
    ("2026-06-06T11:10:00+08:00", "2026-06-06T18:10:00+08:00", True, True),
    # [11:30,12:10)：开赛 12:09 → mid 04:09、close 11:09 → 两段都 false
    ("2026-06-06T04:09:00+08:00", "2026-06-06T11:09:00+08:00", False, False),
    # 开赛 12:10 → close 11:10（含端）→ true
    ("2026-06-06T04:10:00+08:00", "2026-06-06T11:10:00+08:00", False, True),
])
def test_open_flags_legacy_import_rule(mid, close, exp_mid, exp_close):
    from app import table_matches as tm
    out = tm._open_flags(_legacy_cell(), False, None, _sched("2026-06-06", mid, close))
    assert out["earliest_ts_quote_at"] == "2026-06-06T11:10:00+08:00" and out["ts_inferred"] is True
    assert (out["usable_at_mid"], out["usable_at_close"]) == (exp_mid, exp_close)
    assert (out["unusable_reason"] is None) == (exp_mid and exp_close)


def test_open_flags_legacy_import_real_earlier_quote_wins():
    from datetime import datetime
    from app import table_matches as tm
    early = datetime.fromisoformat("2026-06-06T09:00:00+08:00")
    out = tm._open_flags(_legacy_cell(), False, early,
                         _sched("2026-06-06", "2026-06-06T09:30:00+08:00", "2026-06-06T16:30:00+08:00"))
    assert out["earliest_ts_quote_at"] == "2026-06-06T09:00:00+08:00" and out["ts_inferred"] is False
    assert out["usable_at_mid"] is True and out["usable_at_close"] is True


def test_open_flags_first_tick_and_api_opening_ts_inferred_false():
    from app import table_matches as tm
    s = _sched("2026-06-06", "2026-06-06T08:00:00+08:00", "2026-06-06T15:00:00+08:00")
    assert tm._open_flags({"basis": "x", "available": True}, True, None, s)["ts_inferred"] is False
    o = tm._open_flags({"basis": "api_opening", "available": True}, False, None, s)
    assert o["ts_inferred"] is False and o["usable_at_mid"] is False and o["usable_at_close"] is False
    assert tm._open_flags({"basis": None, "available": False}, False, None, s)["ts_inferred"] is None
