"""GET /table/matches（0.3.15 → 0.3.16）：只读、防泄漏、V3 一致、缺数据 null、scope、as-of 基准、凯利、live/last_prematch。

全部在临时副本上跑（sqlite backup 从现网 app.db 只读拷出）；现网文件只做 sha256 前后比对。
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


import hashlib
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app.db as adb  # noqa: E402
from app import table_matches as tm  # noqa: E402
from app.collection_schedule import TZ_CN  # noqa: E402
from app.main import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

PROD_DB = ROOT / "data" / "app.db"
TIMELINE_SQL = Path(str(_REPO_ROOT / "docs/schema/v2_0_odds_timeline.sql"))
FUTURE_DATE = "2099-01-02"


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _copy_prod(dst: Path) -> None:
    src = sqlite3.connect(f"file:{PROD_DB}?mode=ro", uri=True)
    out = sqlite3.connect(str(dst))
    src.backup(out)
    src.close()
    out.close()


def _insert_match(c: sqlite3.Connection, uid: str, *, scope: str, date: str, hour: int,
                  kickoff_at: str, jc_id: str | None, goals: tuple | None) -> int:
    cur = c.execute(
        "INSERT INTO matches (match_uid, scope, jingcai_date, weekday, kickoff_hour, jc_id, jc_no,"
        " competition_name, home_team, away_team, kickoff_at, kickoff_minute_known)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,1)",
        (uid, scope, date, "五", hour, jc_id, int(jc_id[1:]) if jc_id else None,
         "测试联赛", "测试主", "测试客", kickoff_at))
    mid = cur.lastrowid
    if goals is not None:
        hg, ag = goals
        c.execute("INSERT INTO results (match_id, home_goals, away_goals, total_goals, wdl)"
                  " VALUES (?,?,?,?,?)", (mid, hg, ag, None if hg is None else hg + ag,
                                          None if hg is None else ("胜" if hg > ag else "平" if hg == ag else "负")))
    return mid


@pytest.fixture(scope="module")
def prod_sha_before():
    return _sha(PROD_DB)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    p = tmp_path / "app.db"
    _copy_prod(p)
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


# ---------------------------------------------------------------- leakage

def test_unfinished_match_has_no_result_or_settlement(db, client):
    c = sqlite3.connect(str(db))
    # 赛果行已经有比分（模拟提前写入）但开赛在未来 → 必须隐藏
    _insert_match(c, f"{FUTURE_DATE}|五001", scope="jingcai", date=FUTURE_DATE, hour=20,
                  kickoff_at=f"{FUTURE_DATE}T20:00:00+08:00", jc_id="五001", goals=(2, 1))
    mid = c.execute("SELECT id FROM matches WHERE match_uid=?", (f"{FUTURE_DATE}|五001",)).fetchone()[0]
    c.execute("INSERT INTO predictions (match_id, strategy, direction, settle_book, produced_at)"
              " VALUES (?, 'CFFXDJ_5_V3', '主', 'macau_close', '2026-10-08 10:00:00')", (mid,))
    c.commit()
    c.close()
    d = _get(client, date_from=FUTURE_DATE, date_to=FUTURE_DATE)
    row = _row(d, f"{FUTURE_DATE}|五001")
    assert row["result"] is None and row["settlement"] is None
    assert row["result_hidden_reason"] == "before_kickoff" and row["hidden_reason"] == "before_kickoff"
    assert row["prediction"]["direction"] == "主"  # 预测照常返回（冻结只读）
    assert row["produced_at"] == "2026-10-08 10:00:00" and row["as_of"]


def test_no_score_rows_hidden(db, client):
    d = _get(client, date_from="2026-07-05", date_to="2026-07-05")
    row = _row(d, "2026-07-05|日201")  # 现网 results 行比分为空
    assert row["result"] is None and row["settlement"] is None
    assert row["result_hidden_reason"] == "no_result"


def test_as_of_before_kickoff_plus_3h_hides_result(db, client):
    # 2026-06-06|六204 开赛 16:00，比分 3-0
    early = _get(client, date_from="2026-06-06", date_to="2026-06-06", as_of="2026-06-06T18:59:00+08:00")
    row = _row(early, "2026-06-06|六204")
    assert row["result"] is None and row["settlement"] is None
    assert row["result_hidden_reason"] == "not_visible_yet"
    # as_of 早于 produced_at（2026-10 回补）→ 预测也不返回
    assert row["prediction"] is None and row["prediction_hidden_reason"] == "produced_after_as_of"
    late = _get(client, date_from="2026-06-06", date_to="2026-06-06")
    row = _row(late, "2026-06-06|六204")
    assert row["result"]["score"] == "3-0"
    assert row["settlement"]["code"] in {"win", "win_half", "push", "lose_half", "lose", "no_bet"}
    assert row["settlement"]["source"] == "backend"


def test_future_as_of_is_clamped_to_now(db, client):
    d = _get(client, date_from="2026-06-06", date_to="2026-06-06", as_of="2999-01-01T00:00:00+08:00")
    assert d["as_of"] < "2999"


# ---------------------------------------------------------------- V3 consistency + settlement

def test_v3_fields_match_single_match_endpoint(db, client):
    d = _get(client, date_from="2026-06-01", date_to="2026-07-05", limit=2000)
    checked = 0
    for row in d["items"]:
        single = client.get(f"/matches/{row['match_id']}/prediction")
        if single.status_code == 404:
            assert row["prediction"] is None
            continue
        exp = single.json()
        got = {k: v for k, v in row["prediction"].items() if k != "produced_before_kickoff"}
        assert got == exp, row["match_id"]
        assert row["produced_at"] == exp["produced_at"]
        checked += 1
    assert checked == 177


def test_settlement_matches_backtest_engine(db, client):
    """每场结算 = backtest.settle_pnl(odds_asian macau_close, 默认 0.95)。"""
    from app import backtest as bt
    d = _get(client, date_from="2026-06-01", date_to="2026-07-05", limit=2000)
    c = sqlite3.connect(str(db))
    n = 0
    for row in d["items"]:
        s = row["settlement"]
        if not s or s["code"] == "no_bet":
            continue
        h = c.execute("SELECT handicap FROM odds_asian WHERE match_id=? AND book='macau' AND phase='close'",
                      (row["match"]["match_pk"],)).fetchone()[0]
        code, pnl = bt.settle_pnl(row["result"]["home_goals"], row["result"]["away_goals"], h,
                                  s["side"], 1.0, 0.95)
        assert s["line"] == h and abs(s["pnl_units"] - pnl) < 1e-9 and s["code"] == code
        assert s["juice_source"] == "fixed_macau" and s["settlement_version"] == bt.SETTLEMENT_VERSION
        n += 1
    assert n > 0


def test_unknown_settlement_version_400(db, client):
    r = client.get("/table/matches", params={"settlement_version": "nope"})
    assert r.status_code == 400


def test_actual_or_095_falls_back_without_macau_water(db, client):
    d = _get(client, date_from="2026-06-06", date_to="2026-06-06",
             settlement_version="ah_v4_macau_actual_or_095")
    s = _row(d, "2026-06-06|六204")["settlement"]
    assert s["juice"] == 0.95 and s["juice_source"] == "fallback" and s["juice_reason"] == "water_missing"


# ---------------------------------------------------------------- missing data / coverage

def test_missing_data_is_null_with_markers(db, client):
    d = _get(client, date_from="2026-06-01", date_to="2026-06-01")
    row = _row(d, "2026-06-01|一001")
    mac = row["ah"]["macau"]
    assert mac["close"]["line"] == 0.0 and mac["close"]["basis"] == "legacy_import"
    assert mac["close"]["home_water"] is None and mac["close"]["water_source"] is None
    assert mac["close"]["return_rate"] is None
    assert mac["mid"]["available"] is False and mac["mid"]["line"] is None  # 现网澳门无 mid
    for ph in ("open", "mid", "close"):
        assert row["ah"]["pinnacle"][ph]["available"] is False
        assert row["x1x2"]["pinnacle"][ph]["home"] is None
    eu = row["x1x2"]["macau"]["close"]
    assert eu["home"] is not None and eu["draw"] is None and eu["complete"] is False
    assert eu["return_rate"] is None and eu["kelly"] is None
    assert row["x1x2_base"]["close"] == {"pinnacle": None, "multi_avg": None}
    assert row["multi_avg_prob"] is None
    assert row["ah"]["pinnacle"]["close"]["hidden_reason"] == "no_data"
    assert row["live"] == []  # 默认 include_live=none
    assert row["ah"]["macau"]["last_prematch"] is None  # 现网无即时快照


def test_william_missing_match_is_null(db, client):
    c = sqlite3.connect(str(db))
    uid = c.execute("SELECT m.match_uid FROM matches m WHERE NOT EXISTS (SELECT 1 FROM odds_asian o"
                    " WHERE o.match_id=m.id AND o.book='william')").fetchone()[0]
    c.close()
    date = uid.split("|")[0]
    row = _row(_get(client, date_from=date, date_to=date), uid)
    for ph in ("open", "mid", "close"):
        assert row["ah"]["william"][ph]["available"] is False
        assert row["ah"]["william"][ph]["line"] is None


# ---------------------------------------------------------------- scope

def test_scope_filter(db, client):
    c = sqlite3.connect(str(db))
    _insert_match(c, "2026-06-01|extra|甲|乙", scope="extra", date="2026-06-01", hour=20,
                  kickoff_at="2026-06-01T20:00:00+08:00", jc_id=None, goals=None)
    c.commit()
    c.close()
    jc = _get(client, date_from="2026-06-01", date_to="2026-06-01", scope="jc")
    ext = _get(client, date_from="2026-06-01", date_to="2026-06-01", scope="ext")
    al = _get(client, date_from="2026-06-01", date_to="2026-06-01", scope="all")
    assert all(i["match"]["scope"] == "jingcai" for i in jc["items"])
    assert [i["match_id"] for i in ext["items"]] == ["2026-06-01|extra|甲|乙"]
    assert al["total"] == jc["total"] + 1
    assert ext["items"][0]["match"]["jc_id"] is None
    # 旧写法别名
    assert _get(client, date_from="2026-06-01", date_to="2026-06-01", scope="jingcai")["total"] == jc["total"]


def test_pagination_and_defaults(db, client):
    d = _get(client)  # 缺省：库内最大竞彩日往前 6 天
    assert d["date_to"] == "2026-07-05" and d["date_from"] == "2026-06-29"
    p1 = _get(client, date_from="2026-06-01", date_to="2026-07-05", limit=10, offset=0)
    p2 = _get(client, date_from="2026-06-01", date_to="2026-07-05", limit=10, offset=10)
    assert p1["total"] == 177 and p1["count"] == 10
    assert not {i["match_id"] for i in p1["items"]} & {i["match_id"] for i in p2["items"]}


# ---------------------------------------------------------------- read-only

def test_connection_is_read_only(db):
    conn = tm.connect_ro()
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM matches")
    conn.close()


def test_prod_db_untouched(prod_sha_before, client, monkeypatch):
    monkeypatch.setattr(adb, "DB_PATH", PROD_DB)
    _get(client, date_from="2026-06-01", date_to="2026-07-05", include_live="all")
    assert _sha(PROD_DB) == prod_sha_before


# ---------------------------------------------------------------- baseline (as-of) / return rate

def test_return_rate_formulas():
    assert abs(tm.ah_return_rate(0.92, 0.92) - 1 / (2 / 1.92)) < 1e-12
    assert abs(tm.x1x2_return_rate(2.0, 3.4, 3.6) - 1 / (1 / 2 + 1 / 3.4 + 1 / 3.6)) < 1e-12
    assert tm.ah_return_rate(None, 0.9) is None and tm.x1x2_return_rate(2, None, 3) is None


def test_baseline_uses_only_strictly_earlier_days(db, client):
    target_uid = "2026-06-10|三001"
    c = sqlite3.connect(str(db))
    # 0.3.16：tier_midpoint 水位不进基准 → 临时副本里把 crown 标成真实水位再验 as-of 严格性
    c.execute("UPDATE odds_asian SET water_src='actual' WHERE book='crown'")
    c.commit()
    if not c.execute("SELECT 1 FROM matches WHERE match_uid=?", (target_uid,)).fetchone():
        target_uid = c.execute("SELECT match_uid FROM matches WHERE jingcai_date='2026-06-10' LIMIT 1").fetchone()[0]
    c.close()
    before = _row(_get(client, date_from="2026-06-10", date_to="2026-06-10"), target_uid)
    cell0 = before["ah"]["crown"]["close"]
    assert cell0["return_rate_baseline"] is not None
    # 手算：竞彩日 < 2026-06-10 的 crown close 返还率中位数
    import statistics
    c = sqlite3.connect(str(db))
    vals = [tm.ah_return_rate(h, a) for h, a in c.execute(
        "SELECT o.home_water, o.away_water FROM odds_asian o JOIN matches m ON m.id=o.match_id"
        " WHERE o.book='crown' AND o.phase='close' AND m.jingcai_date < '2026-06-10'"
        " AND COALESCE(o.water_censored,0)=0")]
    assert cell0["return_rate_baseline_n"] == len(vals)
    assert abs(cell0["return_rate_baseline"] - round(statistics.median(vals), 6)) < 1e-9
    # 把本日及之后所有 crown 水位改成极端值：基准必须不变
    c.execute("UPDATE odds_asian SET home_water=0.5, away_water=0.5 WHERE book='crown' AND match_id IN"
              " (SELECT id FROM matches WHERE jingcai_date >= '2026-06-10')")
    c.commit()
    c.close()
    after = _row(_get(client, date_from="2026-06-10", date_to="2026-06-10"), target_uid)
    cell1 = after["ah"]["crown"]["close"]
    assert cell1["return_rate_baseline"] == cell0["return_rate_baseline"]
    assert cell1["return_rate_baseline_n"] == cell0["return_rate_baseline_n"]
    assert cell1["return_rate"] != cell0["return_rate"]  # 本场自身值变了，但不进自己的基准


def test_baseline_min_n_and_window(db, client):
    c0 = sqlite3.connect(str(db))
    c0.execute("UPDATE odds_asian SET water_src='actual' WHERE book='crown'")  # 0.3.16：只有真实水位进基准
    c0.commit()
    c0.close()
    d = _get(client, date_from="2026-06-01", date_to="2026-06-01")
    assert all(i["ah"]["crown"]["close"]["return_rate_baseline"] is None for i in d["items"])  # 首日无历史
    d2 = _get(client, date_from="2026-06-20", date_to="2026-06-20", baseline_window_days=3, baseline_min_n=1)
    i = d2["items"][0]
    c = sqlite3.connect(str(db))
    n = c.execute("SELECT COUNT(*) FROM odds_asian o JOIN matches m ON m.id=o.match_id WHERE o.book='crown'"
                  " AND o.phase='close' AND m.jingcai_date >= '2026-06-17' AND m.jingcai_date < '2026-06-20'"
                  " AND COALESCE(o.water_censored,0)=0").fetchone()[0]
    assert i["ah"]["crown"]["close"]["return_rate_baseline_n"] == n


# ---------------------------------------------------------------- snapshot path: sign, kelly, real phases, live

def _with_timeline(db: Path) -> sqlite3.Connection:
    c = sqlite3.connect(str(db))
    c.executescript(TIMELINE_SQL.read_text(encoding="utf-8"))
    return c


def _snap(c, mid, book, market, channel, point, *, rec=None, tgt=None, line=None, wh=None, wa=None,
          ph=None, pd=None, pa=None, extras=None, source=None):
    c.execute("INSERT INTO odds_snapshot (match_id, book, market, channel, point, recorded_at, target_at, line,"
              " water_home, water_away, price_home, price_draw, price_away, water_src, source, extras_json)"
              " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (mid, book, market, channel, point, rec, tgt, line, wh, wa, ph, pd, pa,
               "actual" if wh is not None else None, source, extras))


def test_kelly_pinnacle_base_and_consensus(db, client):
    c = _with_timeline(db)
    mid = c.execute("SELECT id FROM matches WHERE match_uid='2026-06-06|六204'").fetchone()[0]
    for book, o in (("pinnacle", (2.0, 3.5, 4.0)), ("macau", (1.9, 3.3, 3.8)), ("crown", (2.05, 3.4, 3.7))):
        _snap(c, mid, book, "euro_1x2", "rule", "close", rec="2026-06-06T14:50:00+08:00",
              tgt="2026-06-06T15:00:00+08:00", ph=o[0], pd=o[1], pa=o[2])
    c.commit()
    c.close()
    row = _row(_get(client, date_from="2026-06-06", date_to="2026-06-06"), "2026-06-06|六204")
    pin = tm.devig(2.0, 3.5, 4.0)
    probs = [tm.devig(2.0, 3.5, 4.0), tm.devig(1.9, 3.3, 3.8), tm.devig(2.05, 3.4, 3.7)]
    # 0.3.16：某家 vs 多家平均 = 不含本家（leave-one-out）；macau 的平均 = pinnacle + crown
    loo_mac = [(probs[0][i] + probs[2][i]) / 2 for i in range(3)]
    loo_pin = [(probs[1][i] + probs[2][i]) / 2 for i in range(3)]
    mac = row["x1x2"]["macau"]["close"]
    assert mac["kelly_base"] == "pinnacle" and mac["kelly_base_n_books"] == 1
    assert abs(mac["kelly"]["home"] - round(1.9 * pin[0], 4)) < 1e-9
    assert abs(mac["kelly_multi_avg"]["draw"] - round(3.3 * loo_mac[1], 4)) < 1e-9
    assert mac["multi_avg_n_books"] == 3 and mac["n_avg"] == 2
    assert abs(mac["return_rate"] - round(tm.x1x2_return_rate(1.9, 3.3, 3.8), 6)) < 1e-9
    p = row["x1x2"]["pinnacle"]["close"]
    assert p["kelly_base"] == "multi_avg" and p["kelly_base_n_books"] == 2  # 平博自身用其他家平均
    assert abs(p["kelly"]["home"] - round(2.0 * loo_pin[0], 4)) < 1e-9 and p["n_avg"] == 2
    assert row["x1x2_base"]["close"]["multi_avg"]["n_books"] == 3  # 参考列仍含全部
    assert row["x1x2_base"]["close"]["multi_avg"]["n_avg"] == 3
    assert row["multi_avg_prob"] == row["x1x2_base"]["close"]["multi_avg"]
    assert row["x1x2_base"]["close"]["pinnacle"]["home"] == round(pin[0], 6)


def test_kelly_falls_back_to_consensus_without_pinnacle(db, client):
    c = _with_timeline(db)
    mid = c.execute("SELECT id FROM matches WHERE match_uid='2026-06-06|六204'").fetchone()[0]
    _snap(c, mid, "macau", "euro_1x2", "rule", "close", rec="2026-06-06T14:50:00+08:00",
          tgt="2026-06-06T15:00:00+08:00", ph=1.9, pd=3.3, pa=3.8)
    c.commit()
    c.close()
    mac = _row(_get(client, date_from="2026-06-06", date_to="2026-06-06"),
               "2026-06-06|六204")["x1x2"]["macau"]["close"]
    # 0.3.16 leave-one-out：只有本家一家 → 没有「其他家」可比 → 凯利留空
    assert mac["kelly_base"] is None and mac["kelly"] is None and mac["n_avg"] is None
    c = sqlite3.connect(str(db))
    _snap(c, mid, "crown", "euro_1x2", "rule", "close", rec="2026-06-06T14:50:00+08:00",
          tgt="2026-06-06T15:00:00+08:00", ph=2.05, pd=3.4, pa=3.7)
    c.commit()
    c.close()
    mac = _row(_get(client, date_from="2026-06-06", date_to="2026-06-06"),
               "2026-06-06|六204")["x1x2"]["macau"]["close"]
    assert mac["kelly_base"] == "multi_avg" and mac["kelly_base_n_books"] == 1 and mac["n_avg"] == 1
    cr = tm.devig(2.05, 3.4, 3.7)
    assert abs(mac["kelly"]["home"] - round(1.9 * cr[0], 4)) < 1e-9


def test_snapshot_line_sign_and_real_phases(db, client):
    c = _with_timeline(db)
    # 例外场：2026-06-01|一001 开赛 06-02 00:00 → phase_exception
    mid = c.execute("SELECT id FROM matches WHERE match_uid='2026-06-01|一001'").fetchone()[0]
    # 0.3.22：rule 快照标 5DF → 进 ah.macau_5df；actual/t1 无 5DF 标记 → 仍可进主列 close_real
    _snap(c, mid, "macau", "asian", "rule", "close", rec="2026-06-01T21:50:00+08:00",
          tgt="2026-06-01T22:00:00+08:00", line=-0.25, wh=0.9, wa=0.94, source="5df_macauslot_history")
    _snap(c, mid, "macau", "asian", "actual", "t1", rec="2026-06-01T22:55:00+08:00",
          tgt="2026-06-01T23:00:00+08:00", line=-0.5, wh=0.88, wa=0.96)
    _snap(c, mid, "macau", "asian", "rule", "open", rec="2026-05-30T12:00:00+08:00",
          tgt="2026-05-30T12:00:00+08:00", line=-0.25, wh=0.95, wa=0.89, source="5df_macauslot_history")
    c.commit()
    c.close()
    row = _row(_get(client, date_from="2026-06-01", date_to="2026-06-01"), "2026-06-01|一001")
    assert row["phase_exception"] is True
    m = row["ah"]["macau"]
    m5 = row["ah"]["macau_5df"]
    assert m5["close"]["line"] == 0.25 and m5["close"]["basis"] == "asof_rule"  # API 负=主让 → 正=主让
    assert m5["close"]["water_source"] == "actual" and m5["close"]["target_at"] == "2026-06-01T22:00:00+08:00"
    assert m5["close"]["source"] == "5df" and m5["close"]["book_lane"] == "macau_5df"
    assert m["close_real"]["line"] == 0.5 and m["close_real"]["basis"] == "asof_real"
    assert m["close_real"]["recorded_at"] == "2026-06-01T22:55:00+08:00"
    assert "return_rate" not in m["close_real"]  # *_real 只给原始值
    assert m5["open"]["recorded_at"] == "2026-05-30T12:00:00+08:00"
    assert m5["close"]["minutes_since_open"] == round((datetime(2026, 6, 1, 21, 50) - datetime(2026, 5, 30, 12)).total_seconds() / 60, 1)
    # 主列手工：不得带 5DF 水位
    assert m["close"].get("book_lane") == "macau_manual"
    assert m["close"].get("home_water") is None or m["close"].get("water_source") != "actual" or m["close"]["source"] != "5df"
    # 结算不动：仍取 odds_asian macau close（现网 0.0），不吃快照
    s = row["settlement"]
    assert s is None or s["code"] == "no_bet" or s["line"] == 0.0


def test_non_exception_real_fields_null(db, client):
    row = _row(_get(client, date_from="2026-06-06", date_to="2026-06-06"), "2026-06-06|六204")  # 16:00 开赛
    assert row["phase_exception"] is False
    for b in tm.BOOKS:
        assert row["ah"][b]["mid_real"] is None and row["ah"][b]["close_real"] is None
        assert row["x1x2"][b]["mid_real"] is None and row["x1x2"][b]["close_real"] is None
    assert row["schedule"]["mid_target_time"] == "2026-06-06T08:00:00+08:00"
    assert row["schedule"]["close_target_time"] == "2026-06-06T15:00:00+08:00"


def _seg(c, mid, start, end, line, wh, wa, book="pinnacle", inplay=0):
    c.execute("INSERT INTO odds_timeline_seg (match_id, book, market, seg_start_at, seg_end_at, line,"
              " water_home, water_away, is_inplay, water_src) VALUES (?,?,?,?,?,?,?,?,?, 'actual')",
              (mid, book, "asian", start, end, line, wh, wa, inplay))


def test_live_and_last_prematch(db, client):
    c = _with_timeline(db)
    mid = c.execute("SELECT id FROM matches WHERE match_uid='2026-06-06|六204'").fetchone()[0]  # 开赛 16:00
    _seg(c, mid, "2026-06-06T09:00:00+08:00", "2026-06-06T11:30:00+08:00", -0.25, 0.95, 0.95)
    _seg(c, mid, "2026-06-06T11:30:00+08:00", "2026-06-06T15:50:00+08:00", -0.5, 0.9, 1.0)
    _seg(c, mid, "2026-06-06T15:50:00+08:00", "2026-06-06T16:00:00+08:00", -0.5, 0.85, 1.05)
    _seg(c, mid, "2026-06-06T16:10:00+08:00", "2026-06-06T17:00:00+08:00", -0.75, 0.8, 1.1, inplay=1)
    c.commit()
    none = _row(_get(client, date_from="2026-06-06", date_to="2026-06-06"), "2026-06-06|六204")
    assert none["live"] == []
    # 0.1.9：include_live=rule_1110 已删除；all 只返回时间线变化点，不含 11:10 快照
    bad = client.get("/table/matches", params={"date_from": "2026-06-06", "date_to": "2026-06-06",
                                                "include_live": "rule_1110"})
    assert bad.status_code == 422
    full = _row(_get(client, date_from="2026-06-06", date_to="2026-06-06", include_live="all"),
                "2026-06-06|六204")
    labels = [e["label"] for e in full["live"]]
    assert "rule_1110" not in labels and labels.count(None) == 4
    assert "live_rule_1110_target_time" not in full["schedule"]
    lp = full["ah"]["pinnacle"]["last_prematch"]
    assert lp["recorded_at"] == "2026-06-06T15:50:00+08:00"  # 开赛后的 in-play 不算
    assert lp["minutes_before_kickoff"] == 10.0 and lp["stale"] is False
    # 最后一条改到开赛前 20 分钟 → stale=true 但仍返回
    c.execute("UPDATE odds_timeline_seg SET seg_start_at='2026-06-06T15:40:00+08:00'"
              " WHERE seg_start_at='2026-06-06T15:50:00+08:00'")
    c.commit()
    c.close()
    lp2 = _row(_get(client, date_from="2026-06-06", date_to="2026-06-06"),
               "2026-06-06|六204")["ah"]["pinnacle"]["last_prematch"]
    assert lp2["minutes_before_kickoff"] == 20.0 and lp2["stale"] is True


def test_stale_boundaries():
    assert tm.LAST_PREMATCH_FRESH_MINUTES == 15


# ---------------------------------------------------------------- format / openapi

def test_flat_format_and_openapi(db, client):
    d = _get(client, date_from="2026-06-06", date_to="2026-06-06", format="flat")
    row = d["items"][0]
    for k in ("match_id", "jc_id", "jingcai_date", "kickoff_at", "league", "home_team",
              "ah_macau_close_line", "ah_crown_open_home_water", "ah_pinnacle_mid_water_source",
              "x1x2_william_close_home", "x1x2_macau_close_kelly_home", "x1x2_macau_close_kelly_base", "multi_avg_prob_home", "hidden_reason", "settlement_code", "jc_1x2_close_home",
              "prediction_direction", "prediction_confidence", "prediction_stake", "produced_at", "as_of",
              "result_score", "settlement_pnl_units", "settlement_settlement_version",
              "phase_exception", "ah_macau_close_return_rate_baseline"):
        assert k in row, k
    assert isinstance(row["live"], list)
    spec = client.get("/openapi.json").json()
    assert "/table/matches" in spec["paths"]
    params = {p["name"] for p in spec["paths"]["/table/matches"]["get"]["parameters"]}
    assert {"date_from", "date_to", "scope", "strategy", "channel", "include_live",
            "settlement_version", "as_of", "format"} <= params
    assert d["config_version"] == "hl_v0.3.1"


def test_nested_validates_against_response_model(db, client):
    d = _get(client, date_from="2026-06-01", date_to="2026-07-05", limit=2000)
    tm.TableResponse.model_validate(d)


def test_flat_columns_stable_across_rows(db, client):
    d = _get(client, date_from="2026-06-01", date_to="2026-07-05", format="flat", limit=2000)
    keysets = {tuple(sorted(r.keys())) for r in d["items"]}
    assert len(keysets) == 1  # 例外场/非例外场、有无结算 → 同一列集合
    row = d["items"][0]
    assert "ah_macau_close_real_line" in row and "ah_pinnacle_last_prematch_stale" in row
    assert "settlement_code" in row and "result_score" in row


def test_flat_columns_stable_with_snapshot_data(db, client):
    c = _with_timeline(db)
    mid = c.execute("SELECT id FROM matches WHERE match_uid='2026-06-06|六204'").fetchone()[0]
    _seg(c, mid, "2026-06-06T15:50:00+08:00", "2026-06-06T16:00:00+08:00", -0.5, 0.85, 1.05)
    c.commit()
    c.close()
    d = _get(client, date_from="2026-06-06", date_to="2026-06-06", format="flat")
    assert len({tuple(sorted(r.keys())) for r in d["items"]}) == 1
